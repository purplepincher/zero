#!/usr/bin/env python3
"""
harness.py -- think-loop v2 verification harness.

The model is a claimant; the ledger is the truth. Every side effect flows
through Harness.dispatch(), which records an append-only ledger event.
Completion claims are verified mechanically against the ledger AND the
current world state. No model ever checks the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ContractError(Exception):
    """A tool precondition/postcondition failed. Fail closed."""


class ClaimRejected(Exception):
    """A structured completion claim did not verify. Returned to the
    model as feedback, not a crash."""


def norm(repo: Path, path: str) -> str:
    """Normalise a model-supplied path to a repo-relative POSIX string.
    One spelling for ledger, gate, and filesystem. Rejects escapes."""
    if not isinstance(path, str) or not path.strip():
        raise ContractError(f"bad path: {path!r}")
    try:
        rel = (repo / path).resolve().relative_to(repo.resolve())
    except ValueError:
        raise ContractError(f"path escapes repo: {path!r}")
    if rel.parts and rel.parts[0] == ".git":
        raise ContractError(f"path forbidden: {path!r}")
    if str(rel) == ".":
        raise ContractError("path must name a file, not the repo root")
    return rel.as_posix()


@dataclass
class Event:
    seq: int
    tool: str
    args: dict[str, Any]
    ok: bool
    receipt: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class Ledger:
    """Append-only record of what the harness EXECUTED. Only dispatch()
    writes here. The model's words never enter."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.task_start = 0

    def record(self, tool, args, ok, receipt=None, error="") -> Event:
        ev = Event(len(self.events), tool, args, ok, receipt or {}, error)
        self.events.append(ev)
        return ev

    def effects(self, tool: str, **match: Any) -> list[Event]:
        """Successful events for `tool` since the current task started
        whose recorded args match. Old wakes cannot serve as evidence."""
        return [ev for ev in self.events
                if ev.seq >= self.task_start and ev.ok and ev.tool == tool
                and all(ev.args.get(k) == v for k, v in match.items())]


@dataclass
class ToolContract:
    name: str
    pre: Callable[["Harness", dict], dict]   # validate+normalise, or raise
    run: Callable[["Harness", dict], dict]   # perform, return receipt
    post: Callable[["Harness", dict, dict], None] | None = None  # re-prove


PATH_RE = re.compile(
    r"(?<![\w/.-])(\.?[A-Za-z0-9_][\w.-]*(?:/[\w.-]+)*\.\w{1,10}"
    r"|[A-Za-z][\w.-]*\.(md|py|txt|json|yml|yaml|toml|sh|js|ts|html|css|lock|cfg|ini)"
    r"|Makefile|Dockerfile|LICENSE|README|CHANGELOG|NOTICE|AUTHORS)(?![\w/.-])")

# Claim-shaped prose detector (advisory only; never promotes a claim).
# Tuned overnight against 142 hand-crafted samples with Jev adjudication:
# P=0.95 R=0.93 F1=0.94 (was P=0.82 R=0.20 F1=0.32).
_CLAIM_VERBS = re.compile(
    r"\b(created|wrote|written|overwrote|overwritten|added|updated|updating|modified|edited|"
    r"deleted|removed|fixed|fixing|committed|commented|pushed)\b", re.I)
_CLAIM_NOUN = re.compile(
    r"\b(files?|modules?|scripts?|configs?|docs?|changes|commits?|tests?)\b", re.I)
_CLAIM_NEG = re.compile(
    r"\b(didn'?t|did not|don'?t|do not|never|failed to|failure|"
    r"couldn'?t|could not|unable to|won'?t|will not|not yet|"
    r"nothing|none\b)", re.I)
_CLAIM_COUNTERFACT = re.compile(
    r"\b(should|would|could|might) have (written|\w+ed)\b|\bhad I\b", re.I)
_CLAIM_PASSIVE = re.compile(
    r"\b(was|were|been|being)\s+(created|written|updated|deleted|fixed|"
    r"committed|added|removed|modified)\b", re.I)
_CLAIM_FUT = re.compile(
    r"\b(will|shall|going to|plan to|planning to|intend to|"
    r"should|would|could|might|later|next|tomorrow|soon)\b", re.I)
_CLAIM_PAST = re.compile(
    r"\b(created|wrote|written|overwrote|added|updated|deleted|removed|fixed|"
    r"committed|pushed)\b", re.I)
_CLAIM_FIRST = re.compile(r"\b(I|we|my|our)\b", re.I)
_CLAIM_SUBJ_DROP = re.compile(
    r"^\s*(created|wrote|written|added|updated|deleted|removed|fixed|"
    r"committed|pushed|edited|modified)\b", re.I)


def _looks_like_claim(text: str) -> bool:
    """Advisory: does this prose read as the speaker claiming they
    completed a file action? Negations, failures, futures, hypotheticals,
    counterfactuals, and third-person/passive observations don't count."""
    text = text or ""
    if _CLAIM_NEG.search(text) or _CLAIM_COUNTERFACT.search(text):
        return False
    if not _CLAIM_VERBS.search(text):
        return False
    if _CLAIM_FUT.search(text) and not _CLAIM_PAST.search(text):
        return False
    if not (PATH_RE.search(text) or _CLAIM_NOUN.search(text)):
        return False
    if _CLAIM_PASSIVE.search(text) and not re.search(
            r"\b(I|we)\b.{0,15}\b(was|were)\b", text, re.I):
        return False
    return bool(_CLAIM_FIRST.search(text) or _CLAIM_SUBJ_DROP.search(text))


def mentioned_paths(text: str) -> list[str]:
    return sorted(set(m[0] for m in PATH_RE.findall(text or "")))


class Harness:
    """Owns ground truth for one agent run."""

    MAX_TOOL_CALLS = 40  # per run; weak models loop forever otherwise

    def __init__(self, repo: str | Path = ".", run_id: str = "local"):
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.ledger = Ledger()
        self.contracts: dict[str, ToolContract] = {}
        self.verified: list[dict] = []   # claims that passed: the only ones that count
        self.rejected: list[dict] = []   # claims that failed, unresolved
        self.calls = 0

    # -- dispatch ----------------------------------------------------
    def register(self, contract: ToolContract) -> None:
        self.contracts[contract.name] = contract

    def dispatch(self, tool_name: str, args: dict | None) -> tuple[bool, str]:
        """Execute one tool call under its contract. Always records.
        Returns (ok, result_text). Fails closed."""
        args = dict(args or {})
        contract = self.contracts.get(tool_name)
        if contract is None:
            self.ledger.record(tool_name, args, False,
                               error=f"unknown tool: {tool_name!r}")
            return False, f"Unknown tool: {tool_name}"
        self.calls += 1
        if self.calls > self.MAX_TOOL_CALLS:
            err = f"tool budget exhausted ({self.MAX_TOOL_CALLS}/run)"
            self.ledger.record(tool_name, args, False, error=err)
            return False, err
        clean: dict | None = None
        try:
            clean = contract.pre(self, args)
            receipt = contract.run(self, clean)
            if contract.post is not None:
                contract.post(self, clean, receipt)
        except (ContractError, ClaimRejected, OSError) as e:
            err = f"{type(e).__name__}: {e}"
            self.ledger.record(tool_name,
                               clean if clean is not None else args,
                               False, error=err)
            if isinstance(e, ClaimRejected):
                self.rejected.append({"summary": args.get("summary"),
                                      "reason": str(e)})
            return False, err
        self.ledger.record(tool_name, clean, True, receipt)
        return True, json.dumps(receipt)[:4000]

    def new_task(self) -> None:
        """Scope evidence to the current task: old wakes can't serve."""
        self.ledger.task_start = len(self.ledger.events)

    # -- claim verification ------------------------------------------
    def verify_claim(self, evidence: list[dict]) -> list[str]:
        """Check each evidence item against the ledger AND the world.
        Returns confirmations; raises ClaimRejected on first failure."""
        done = []
        for item in evidence:
            if not isinstance(item, dict):
                raise ClaimRejected(f"evidence item not an object: {item!r}")
            tool = item.get("tool")
            p = None
            if item.get("path") is not None:
                try:
                    p = norm(self.repo, item["path"])
                except ContractError as e:
                    raise ClaimRejected(f"bad evidence path: {e}")
            if tool == "write_file":
                evs = self.ledger.effects("write_file", path=p)
                if not evs:
                    raise ClaimRejected(
                        f"no successful write_file to {p} this task")
                disk = self._sha(p)
                if disk != evs[-1].receipt.get("sha256"):
                    raise ClaimRejected(
                        f"{p} changed or vanished after it was written")
                done.append(f"write_file {p} verified (sha256 matches)")
            elif tool == "read_file":
                if not self.ledger.effects("read_file", path=p):
                    raise ClaimRejected(
                        f"no successful read_file of {p} this task")
                done.append(f"read_file {p} verified")
            elif tool == "git_commit":
                want = item.get("sha", "")
                evs = [e for e in self.ledger.effects("git_commit")
                       if e.receipt.get("sha", "").startswith(want)]
                if not evs:
                    raise ClaimRejected("no matching git_commit this task")
                self._git("cat-file", "-e", evs[-1].receipt["sha"])
                done.append(
                    f"git_commit {evs[-1].receipt['sha'][:7]} verified")
            elif tool == "comment_issue":
                issue = item.get("issue")
                evs = (self.ledger.effects("comment_issue", issue=issue)
                       if issue else self.ledger.effects("comment_issue"))
                if not evs:
                    raise ClaimRejected("no matching comment_issue this task")
                done.append(f"comment_issue #{evs[-1].args['issue']} verified")
            else:
                raise ClaimRejected(
                    f"evidence for {tool!r} is not verifiable -- cite "
                    "write_file, read_file, git_commit or comment_issue")
        return done

    def claim_done(self, summary: str, evidence: list[dict]) -> dict:
        """The model's only way to record a completed result."""
        if not isinstance(evidence, list) or not evidence:
            raise ClaimRejected("evidence must be a non-empty list")
        confirmations = self.verify_claim(evidence)  # raises ClaimRejected
        summary = str(summary or "").strip()
        if not summary:
            # Synthesize from evidence; the evidence is what verifies,
            # the summary is for humans reading the log.
            summary = "completed: " + "; ".join(confirmations)
        self.rejected = [r for r in self.rejected if r["summary"] != summary]
        self.verified.append({"summary": summary, "evidence": evidence})
        return {"verified": True, "confirmations": confirmations}

    # -- advisory prose scan -----------------------------------------
    def scan_prose(self, text: str) -> list[str]:
        """Advisory only: flag claim-shaped prose with no ledger event and
        no disk presence. Never promotes a claim."""
        notes = []
        if not _looks_like_claim(text or ""):
            return notes
        paths = mentioned_paths(text)
        for p in paths:
            try:
                q = norm(self.repo, p)
            except ContractError:
                continue
            if not (self.repo / q).exists() and not self.ledger.effects(
                    "write_file", path=q):
                notes.append(
                    f"unverified prose claim about {p}: no tool call wrote it "
                    "and it is not on disk -- use claim_done with evidence, "
                    "or do the work")
        if not paths:
            notes.append(
                "prose reads like a completion claim but names no file -- "
                "if you finished file work, record it with claim_done and "
                "evidence, or do the work")
        return notes

    # -- commit gate ---------------------------------------------------
    def commit_gate(self, message: str, paths: list[str]) -> list[str]:
        """Pure check. Returns refusal reasons; empty means go."""
        no = []
        for p in paths:
            if not self.ledger.effects("write_file", path=p):
                no.append(f"{p}: no write_file for it this task")
        try:
            status = self._git("status", "--porcelain", "--", *paths)
        except ContractError as e:
            return [str(e)]
        if not status.strip():
            no.append("nothing to commit under the listed paths")
        for p in mentioned_paths(message):
            try:
                q = norm(self.repo, p)
            except ContractError:
                no.append(f"message names {p}, which is not a repo path")
                continue
            if not (self.repo / q).exists():
                no.append(f"message names {p}, but it does not exist")
        if self.rejected:
            no.append(f"{len(self.rejected)} rejected claim(s) unresolved -- "
                      "verify them with claim_done before committing")
        return no

    def committable_files(self) -> list[str]:
        """Exactly the paths this task verifiably wrote. The model does
        NOT choose the commit's file list."""
        return sorted({str(e.args["path"]) for e in self.ledger.events
                       if e.tool == "write_file" and e.ok
                       and e.seq >= self.ledger.task_start})

    # -- verdict -------------------------------------------------------
    def verdict(self) -> dict:
        # A failure is excused if the same tool later succeeded: the model
        # recovered, which is normal exploration. Unexcused failures and
        # unresolved rejected claims make the run incomplete -- fail loud.
        failed = []
        for e in self.ledger.events:
            if e.ok:
                continue
            recovered = any(ev.ok and ev.tool == e.tool and ev.seq > e.seq
                            for ev in self.ledger.events)
            if not recovered:
                failed.append({"tool": e.tool, "error": e.error})
        open_claims = list(self.rejected)
        accomplished = [
            {"tool": e.tool, "args": e.args, "receipt": e.receipt}
            for e in self.ledger.events if e.ok]
        # A run that did nothing is not complete, even if nothing failed:
        # the model shortcutting to done with zero actions is a silent lie.
        empty = not accomplished and not self.verified
        return {
            "run": self.run_id,
            "accomplished": accomplished,
            "rejected_claims": open_claims,
            "failed_calls": failed,
            "empty_run": empty,
            "complete": not open_claims and not failed and not empty,
        }

    # -- helpers -------------------------------------------------------
    def _git(self, *argv: str) -> str:
        try:
            out = subprocess.run(["git", *argv], cwd=self.repo,
                                 capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise ContractError("git timed out")
        if out.returncode != 0:
            raise ContractError(f"git {' '.join(argv)}: "
                                f"{out.stderr.strip()[:200]}")
        return out.stdout.strip()

    def _sha(self, relpath: str) -> str | None:
        f = self.repo / relpath
        return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
