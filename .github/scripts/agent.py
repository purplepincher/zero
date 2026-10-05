#!/usr/bin/env python3
"""
agent.py — The agent's brain.

A ReAct (Reason + Act) loop that runs inside GitHub Actions:
  1. Observe: read repo state, task, memory, scratch
  2. Think: call LLM via vault with full context
  3. Act: dispatch tool calls (file ops, API calls, shell)
  4. Observe results, loop until done or max iterations
  5. Commit everything (memory, scratch, results)

The agent is autonomous. It doesn't wait for human approval mid-loop.
Dangerous actions are constrained by the permissions model, not by
blocking on a human. The human reviews via git log and issues.

Usage: python3 .github/scripts/agent.py [--task "description"] [--max-iterations 10]
"""

import json
import os
import subprocess
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Harness, ToolContract, ContractError, norm, mentioned_paths

# Configuration
VAULT_URL = os.environ.get("VAULT_URL") or "https://superinstance-vault.casey-digennaro.workers.dev"
MAX_ITERATIONS = 10
SCRATCH_DIR = Path("scratch")
STATE_FILE = Path(".agent-state.json")


def log(msg, level="INFO"):
    """Timestamped log line (the harness v2 rewrite dropped this helper)."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def get_oidc_token():
    """Get OIDC token from GitHub Actions environment."""
    return os.environ.get("OIDC_TOKEN", "")


def get_repo():
    """Detect owner/repo from the git remote (works in forks too)."""
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        url = url.removesuffix(".git")
        if "github.com" in url:
            path = url.split("github.com", 1)[1].lstrip("/: ")
            owner, repo = path.split("/", 1)
            return owner, repo
    except Exception:
        pass
    return "purplepincher", "zero"


def vault_call(service, action, params=None):
    """Call an API through the vault."""
    import urllib.request

    payload = {
        "oidc_token": get_oidc_token(),
        "service": service,
        "action": action,
        "params": params or {},
    }

    req = urllib.request.Request(
        f"{VAULT_URL}/proxy",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "purplepincher-zero/1.0 (+https://github.com/purplepincher/zero)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log(f"Vault call failed: {service}/{action}: {e}", "ERROR")
        return {"error": str(e)}


def llm_think(messages, model=None):
    """Call LLM via vault. Returns the response text."""
    result = vault_call("llm", "complete", {
        "messages": messages,
        "model": model or "default",
    })

    if "error" in result:
        log(f"LLM call failed: {result['error']}", "ERROR")
        return None

    r = result.get("result", result)
    if isinstance(r, dict):
        return r.get("content") or r.get("text") or json.dumps(r)
    return str(r)


# ---------------------------------------------------------------------------
# Tool contracts (harness v2)
#
# Every tool is a contract: pre validates, run performs, post re-proves.
# The harness records everything in an append-only ledger; the model's
# completion claims are verified against the ledger, never trusted.

def _register_contracts(h: Harness):
    # -- read_file --
    def read_pre(h, a):
        p = norm(h.repo, a.get("path", ""))
        src = h.repo / p
        if not src.is_file():
            raise ContractError(f"not a file: {p}")
        if src.stat().st_size > 1_000_000:
            raise ContractError(f"{p}: over read limit")
        return {"path": p}

    def read_run(h, a):
        return {"content": (h.repo / a["path"]).read_text()[:10000]}

    h.register(ToolContract("read_file", read_pre, read_run))

    # -- write_scratch --
    def scratch_pre(h, a):
        fn = a.get("filename", "")
        if not isinstance(fn, str) or not fn or ".." in fn or "/" in fn:
            raise ContractError(f"invalid filename: {fn!r}")
        content = a.get("content", "")
        if not isinstance(content, str):
            raise ContractError("content must be a string")
        return {"filename": fn, "content": content}

    def scratch_run(h, a):
        SCRATCH_DIR.mkdir(exist_ok=True)
        (SCRATCH_DIR / a["filename"]).write_text(a["content"])
        return {"path": f"scratch/{a['filename']}",
                "bytes": len(a["content"])}

    def scratch_post(h, a, r):
        if not (SCRATCH_DIR / a["filename"]).is_file():
            raise ContractError("postcondition: scratch file missing")

    h.register(ToolContract("write_scratch", scratch_pre, scratch_run,
                            scratch_post))

    # -- write_file --
    def write_pre(h, a):
        p = norm(h.repo, a.get("path", ""))
        if p.startswith(".github/workflows"):
            raise ContractError(
                "workflow writes are deferred for human review")
        content = a.get("content")
        if not isinstance(content, str):
            raise ContractError("content must be a string")
        if len(content.encode("utf-8")) > 200_000:
            raise ContractError("content over 200k bytes")
        return {"path": p, "content": content}

    def write_run(h, a):
        dst = h.repo / a["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = a["content"].encode("utf-8")
        tmp = dst.parent / f".tmp-{os.getpid()}-{time.time_ns()}"
        tmp.write_bytes(data)
        tmp.replace(dst)  # atomic: postcondition never sees a half file
        return {"path": a["path"], "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def write_post(h, a, r):
        dst = h.repo / a["path"]
        if not dst.is_file():
            raise ContractError(f"postcondition: {a['path']} missing")
        if hashlib.sha256(dst.read_bytes()).hexdigest() != r["sha256"]:
            raise ContractError(
                f"postcondition: {a['path']} hash mismatch after write")

    h.register(ToolContract("write_file", write_pre, write_run, write_post))

    # -- run_shell --
    def shell_pre(h, a):
        cmd = a.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ContractError("empty command")
        blocked = ["rm -rf /", "mkfs", ":(){:|:&};:", "curl | bash"]
        if any(b in cmd for b in blocked):
            raise ContractError("blocked dangerous command")
        return {"command": cmd}

    def shell_run(h, a):
        r = subprocess.run(a["command"], shell=True, capture_output=True,
                           text=True, timeout=120, cwd=h.repo)
        return {"exit": r.returncode,
                "output": (r.stdout + r.stderr)[:5000]}

    h.register(ToolContract("run_shell", shell_pre, shell_run))

    # -- run_tests --
    def tests_run(h, a):
        if (h.repo / "tests").exists():
            r = subprocess.run(
                ["python3", "-m", "pytest", "tests/", "-x", "-q"],
                capture_output=True, text=True, timeout=300, cwd=h.repo)
        elif (h.repo / "package.json").exists():
            r = subprocess.run(["npm", "test"], capture_output=True,
                               text=True, timeout=300, cwd=h.repo)
        else:
            return {"note": "no test suite found"}
        return {"exit": r.returncode,
                "output": (r.stdout + r.stderr)[:5000]}

    h.register(ToolContract("run_tests", lambda h, a: {}, tests_run))

    # -- api_github / api_cloudflare (via vault) --
    def api_pre(h, a):
        action = a.get("action", "")
        if not action:
            raise ContractError("missing action")
        return {"action": action, "params": a.get("params", {})}

    def gh_run(h, a):
        return {"result": str(vault_call("github", a["action"],
                                         a["params"]))[:5000]}

    def cf_run(h, a):
        return {"result": str(vault_call("cloudflare", a["action"],
                                         a["params"]))[:5000]}

    h.register(ToolContract("api_github", api_pre, gh_run))
    h.register(ToolContract("api_cloudflare", api_pre, cf_run))

    # -- comment_issue --
    def comment_pre(h, a):
        try:
            issue = int(a.get("issue", 0))
        except (TypeError, ValueError):
            raise ContractError("issue must be an issue number")
        if isinstance(a.get("issue"), bool) or issue <= 0:
            raise ContractError("issue must be a positive integer")
        body = a.get("body", "")
        if not isinstance(body, str) or not body.strip():
            raise ContractError("empty body")
        if len(body) > 10000:
            raise ContractError("body over 10k chars")
        return {"issue": issue, "body": body}

    def comment_run(h, a):
        owner, repo = get_repo()
        res = vault_call("github", "comment_issue", {
            "owner": owner, "repo": repo,
            "issue_number": a["issue"], "body": a["body"]})
        return {"issue": a["issue"], "result": str(res)[:500]}

    h.register(ToolContract("comment_issue", comment_pre, comment_run))

    # -- create_issue --
    def create_pre(h, a):
        title = str(a.get("title", "")).strip()
        if not title:
            raise ContractError("empty title")
        return {"title": title, "body": str(a.get("body", ""))}

    def create_run(h, a):
        owner, repo = get_repo()
        res = vault_call("github", "create_issue", {
            "owner": owner, "repo": repo,
            "title": a["title"], "body": a["body"]})
        return {"result": str(res)[:500]}

    h.register(ToolContract("create_issue", create_pre, create_run))

    # -- git_commit (v2 gate: the harness stages the ledger's file set) --
    def commit_pre(h, a):
        msg = str(a.get("message", "")).strip()
        if not msg:
            raise ContractError("empty commit message")
        if len(msg) > 500:
            raise ContractError("commit message > 500 chars")
        if h.rejected:
            raise ContractError(
                f"commit gate: {len(h.rejected)} rejected claim(s) "
                "unresolved -- verify them with claim_done first")
        model_files = h.committable_files()
        try:
            mem_dirty = bool(h._git("status", "--porcelain", "--",
                                    "MEMORY.md").strip())
        except ContractError:
            mem_dirty = False
        files = list(model_files)
        if mem_dirty and "MEMORY.md" not in files:
            files.append("MEMORY.md")
        if not files:
            raise ContractError("commit gate: nothing to commit")
        for p in mentioned_paths(msg):
            try:
                q = norm(h.repo, p)
            except ContractError:
                raise ContractError(
                    f"commit gate: message names {p}, not a repo path")
            if not (h.repo / q).exists():
                raise ContractError(
                    f"commit gate: message names {p}, but it does not exist")
        return {"message": msg, "files": files}

    def commit_run(h, a):
        subprocess.run(["git", "config", "user.name", "zero"],
                       check=True, cwd=h.repo)
        subprocess.run(["git", "config", "user.email",
                        "zero@purplepincher.org"], check=True, cwd=h.repo)
        h._git("add", "--", *a["files"])
        staged = h._git("diff", "--cached", "--name-only").split()
        extra = sorted(set(staged) - set(a["files"]))
        if extra:
            h._git("reset")
            raise ContractError(
                f"commit gate: unexpected staged files {extra}; index reset")
        h._git("commit", "-m", a["message"])
        sha = h._git("rev-parse", "HEAD")
        return {"sha": sha, "files": a["files"]}

    def commit_post(h, a, r):
        h._git("cat-file", "-e", r["sha"])
        if h._git("rev-parse", "HEAD") != r["sha"]:
            raise ContractError("postcondition: HEAD is not the new commit")

    h.register(ToolContract("git_commit", commit_pre, commit_run,
                            commit_post))

    # -- claim_done: the model's only way to record a completed result --
    def claim_run(h, a):
        return h.claim_done(a.get("summary", ""), a.get("evidence", []))

    h.register(ToolContract(
        "claim_done",
        lambda h, a: {"summary": a.get("summary", ""),
                      "evidence": a.get("evidence", [])},
        claim_run))

    # -- deferred tools: logged, not executed (human reviews) --
    def _deferred(name):
        def pre(h, a):
            return dict(a)

        def run(h, a):
            return {"deferred": True,
                    "note": f"{name} is deferred for human review"}

        h.register(ToolContract(name, pre, run))

    for _t in ("write_workflow", "delete_file", "git_push_main"):
        _deferred(_t)


def build_context(task_description=None):
    """Build the full context for the LLM."""
    parts = []

    # Identity
    try:
        parts.append("## Who I Am\n" + Path("ONBOARDING.md").read_text()[:3000])
    except:
        parts.append("## Who I Am\nI am a Purple Pincher zero — a GitHub-native agent.")

    # Memory
    try:
        parts.append("## What I've Learned\n" + Path("MEMORY.md").read_text()[-2000:])
    except:
        pass

    # Current task
    if task_description:
        parts.append(f"## Current Task\n{task_description}")

    # Task board
    try:
        tasks = Path("TASKS.md").read_text()
        if "## Open" in tasks:
            parts.append("## Task Board\n" + tasks[:2000])
    except:
        pass

    # Open GitHub issues (fetched directly, not via LLM tool call)
    try:
        owner, repo = get_repo()
        result = vault_call("github", "list_issues",
                            {"owner": owner, "repo": repo, "state": "open"})
        issues = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(issues, list) and issues:
            lines = []
            for i in issues[:10]:
                lines.append(f"#{i.get('number')}: {i.get('title')}\n{(i.get('body') or '')[:800]}")
            parts.append("## Open Issues\n" + "\n---\n".join(lines))
    except Exception as e:
        parts.append(f"## Open Issues\n(could not fetch: {e})")

    # Recent git history (what I've been doing)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True
        )
        parts.append("## Recent Activity\n" + result.stdout)
    except:
        pass

    # Scratch directory (what I'm working on)
    if SCRATCH_DIR.exists():
        files = list(SCRATCH_DIR.glob("*"))
        if files:
            parts.append(f"## Scratch Files\n{', '.join(f.name for f in files[:20])}")

    return "\n\n".join(parts)


def build_tool_prompt():
    """Describe available tools to the LLM."""
    return """
## RESPONSE FORMAT — STRICT, NO EXCEPTIONS

Your ENTIRE response must be ONE single JSON object. Nothing before it, nothing after it.
No markdown. No code fences. No prose. No explanations outside the JSON.
If you write anything that is not valid JSON, the harness cannot read it and your turn is wasted.

Format for acting:
{"tool_calls": [{"tool": "tool_name", "args": {...}}], "thinking": "...", "done": false}

Format when the task is fully complete:
{"thinking": "...", "done": true, "summary": "..."}

## Available Tools

Tools:
- read_file(path): Read a file from the repo
- write_scratch(filename, content): Write to scratch/ (for working notes, drafts, test scripts)
- write_file(path, content): Write a file to the repo (not .github/workflows)
- run_shell(command): Run a shell command (120s timeout)
- run_tests(): Run the test suite if one exists
- api_github(action, params): Call GitHub API via vault (actions: get_repo, list_issues, etc.)
- api_cloudflare(action, params): Call Cloudflare API via vault
- comment_issue(issue, body): Comment on a GitHub issue
- create_issue(title, body): Create a GitHub issue (e.g., to delegate to another agent)
- git_commit(message): Commit all changes (except workflows)
- claim_done(summary, evidence): Record a completed result. evidence cites your
  tool calls, e.g. [{"tool": "write_file", "path": "docs/x.md"}]. The harness
  verifies each item mechanically against what actually ran.

You act only through tools. Only successful tool calls change the world.
Every completion claim ("I created X") is mechanically checked against your
tool-call history for this run. To record a completed result, call claim_done
with evidence citing your tool calls; unverified claims are rejected and block
git_commit until resolved. The git_commit tool stages exactly the files your
successful write_file calls created -- you do not choose the file list.
Prefer small, verifiable steps. You cannot mark anything done in prose --
prose is never trusted.

Permissions: write_workflow, delete_file, git_push_main are deferred for human review.
Be autonomous — don't ask for permission, just do the work and commit.
If you need something you can't do, create an issue describing what's needed.

REMEMBER: your entire response must be ONE JSON object and nothing else.
""".strip()


def run_agent_loop(task_description=None, max_iterations=None):
    """Main ReAct loop."""
    max_iter = max_iterations or MAX_ITERATIONS
    log(f"Starting agent loop (max {max_iter} iterations)")
    if task_description:
        log(f"Task: {task_description[:200]}")

    # Ensure scratch directory exists
    SCRATCH_DIR.mkdir(exist_ok=True)

    conversation = []

    # Harness v2: the model is a claimant, the ledger is the truth.
    harness = Harness(repo=".",
                      run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    _register_contracts(harness)
    harness.new_task()

    for i in range(max_iter):
        log(f"--- Iteration {i+1}/{max_iter} ---")

        # Build context
        context = build_context(task_description)
        tool_prompt = build_tool_prompt()

        messages = [
            {"role": "system", "content": f"You are a Purple Pincher, an autonomous GitHub-native AI agent.\n\n{context}\n\n{tool_prompt}"},
        ]
        # Add conversation history (tool results from previous iterations)
        messages.extend(conversation)

        if i == 0:
            messages.append({"role": "user", "content":
                f"Begin working on the task. Think step by step, use tools as needed. "
                f"Task: {task_description or 'Check TASKS.md and open issues, pick the most important thing to work on.'} "
                f"Respond with ONLY the JSON object."
            })
        else:
            messages.append({"role": "user", "content": "Continue. What's next? Respond with ONLY the JSON object."})

        # Think
        log("Thinking...")
        response = llm_think(messages)
        if not response:
            log("LLM call failed, ending loop", "ERROR")
            break

        # Parse response
        try:
            # Strip markdown code fences if present
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.rstrip().endswith("```"):
                    cleaned = cleaned.rstrip()[:-3]
                cleaned = cleaned.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
            # Find the largest balanced JSON object
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                decision = json.loads(cleaned[start:end])
            else:
                raise ValueError("No JSON found")
        except:
            log(f"Could not parse LLM response as JSON, treating as thinking", "WARN")
            log(f"Response: {response[:500]}")
            conversation.append({"role": "assistant", "content": response})
            conversation.append({"role": "user", "content": "Please respond in the JSON tool_calls format."})
            continue

        thinking = decision.get("thinking", "")
        if thinking:
            log(f"Thinking: {thinking[:300]}")

        # Check if done (claims must verify first -- prose is never trusted)
        if decision.get("done"):
            if harness.rejected:
                log(f"Done claimed but {len(harness.rejected)} "
                    "claim(s) still rejected", "WARN")
                conversation.append({"role": "assistant", "content": response})
                conversation.append({
                    "role": "user",
                    "content": f"You said done, but these claims are still "
                    f"unverified: {json.dumps(harness.rejected)}. Call "
                    f"claim_done with evidence, or do the work."})
                continue
            summary = decision.get("summary", "Task complete")
            log(f"Agent reports done: {summary}")
            # Update memory
            update_memory(f"Completed: {summary}")
            break

        # Advisory: flag claim-shaped prose with no backing tool call
        for note in harness.scan_prose(thinking):
            conversation.append({"role": "system", "content": note})

        # Execute tool calls under the harness (ledger records everything)
        tool_calls = decision.get("tool_calls", [])
        if not tool_calls:
            log("No tool calls, asking for clarification")
            conversation.append({"role": "assistant", "content": response})
            conversation.append({"role": "user", "content": "You didn't include any tool calls. What do you want to do?"})
            continue

        results = []
        for tc in tool_calls:
            success, result = harness.dispatch(tc.get("tool"),
                                               tc.get("args", {}))
            status = "OK" if success else "FAIL"
            log(f"  [{status}] {tc.get('tool')}")
            results.append({
                "tool": tc.get("tool"),
                "success": success,
                "result": result[:2000],  # Truncate for context
            })

        # Add to conversation for next iteration
        conversation.append({"role": "assistant", "content": response})
        conversation.append({
            "role": "user",
            "content": f"Tool results:\n{json.dumps(results, indent=2)}"
        })

        # Brief pause to avoid rate limits
        time.sleep(2)

    else:
        log(f"Reached max iterations ({max_iter})", "WARN")
        update_memory(f"Hit max iterations without completing task")

    # Verdict: the ledger, not the model, says what happened. Fail loud.
    verdict = harness.verdict()
    log(f"Verdict: complete={verdict['complete']} "
        f"({len(verdict['accomplished'])} actions, "
        f"{len(verdict['rejected_claims'])} rejected claims, "
        f"{len(verdict['failed_calls'])} unrecovered failures)")
    if verdict["rejected_claims"]:
        log(f"Rejected claims: {json.dumps(verdict['rejected_claims'])[:500]}",
            "ERROR")
    if not verdict["complete"]:
        update_memory("Incomplete run: unverified claims or unrecovered "
                      "failures -- see workflow log")
        log("Run incomplete: failing loud", "ERROR")
        sys.exit(1)

    log("Agent loop complete")


def update_memory(entry):
    """Append to MEMORY.md."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open("MEMORY.md", "a") as f:
        f.write(f"\n- [{ts}] {entry}\n")
    log(f"Memory updated: {entry[:100]}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", help="Task description")
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    args = parser.parse_args()

    run_agent_loop(args.task, args.max_iterations)


if __name__ == "__main__":
    main()
