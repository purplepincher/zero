# AGENTS.md — Operator's Manual for the Purple Pincher Zero

You are an agent reading this. This document tells you everything you need to
operate, extend, and explain this system. It is written so that an agent
arriving cold can understand the whole thing from these pages and then explain
it accurately to their human — because that is how documentation travels now:
an agent digests, then tells their person. If something here is unclear, that
is a bug in the docs.

## What this is

This repo IS an agent. Not code for an agent — the repo itself, running on
GitHub Actions, is the agent. It is called a Purple Pincher zero.

- **Body**: this repo — files, workflows, scripts.
- **Heartbeat**: the `think` workflow, every 15 minutes. It runs the ReAct loop
  (`.github/scripts/agent.py`): observe → think → act → repeat.
- **Ears**: the `react` workflow (GitHub issues and comments) and the
  `message` workflow (messages via a messenger bridge worker).
- **Brain**: an LLM, reached only through the vault. Never directly, never with
  keys in the repo.
- **Hands**: vault-proxied APIs — GitHub, Cloudflare, Telegram, LLM. Extensible
  (see `docs/plugins.md`).
- **Memory**: git itself (see `docs/memory.md`).

## The one rule

This repo holds NO secrets. API keys live in the vault, a Cloudflare Worker.
The agent proves its identity with GitHub OIDC; the vault verifies the token,
checks the allowlist, and proxies the API call. The secret never leaves the
worker. If you ever feel tempted to put a key in a file, stop and read
`VAULT.md`.

## Fork and go

1. Fork this repo.
2. Register with the vault: open an issue titled "Register" — the fleet
   coordinator adds your repo to the vault allowlist.
3. Done. The heartbeat wakes every 15 minutes. Open an issue to give it a task.

## How to talk to it

- **Issues**: open an issue or comment on one. The `react` workflow
  acknowledges; the next think cycle acts. This is also the agent-to-agent
  channel (see `docs/i2i.md`).
- **Messenger**: message its bot, if the fork owner configured one (Telegram,
  Discord, WhatsApp, ... — see `docs/messengers.md`). The `message` workflow
  hears, thinks via the vault, and replies.
- **TASKS.md**: the onboard task board. The heartbeat checks it every cycle.

## How to teach it things

- Edit `ONBOARDING.md` — that is its identity, its principles, and its
  alignment. The alignment section orients it around understanding its own
  system and explaining it to humans and agents alike.
- Add tasks to `TASKS.md` or open issues.
- It learns by doing and writes what it learns into `MEMORY.md` and these docs.

## How to extend it

See `docs/plugins.md`. Short version: a plugin is a new vault service — a
handler function in the vault worker, a secret stored in the worker, and an
allowlist entry. The agent calls it through the same `/proxy` endpoint as
everything else.

## How it remembers

See `docs/memory.md`. Short version: `MEMORY.md` is the digest; the git log is
the full, rewindable history. Append, never edit.

## Explaining this system (your duty)

When a human — or another agent — asks you about this system, explain from
these docs, concretely: the vessel, the heartbeat, the ears, the vault, the
memory, the fork-and-go path. If you cannot answer from these docs, say so
plainly and note what is missing. Then consider writing it down.
