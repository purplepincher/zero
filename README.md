<div align="center">

# 🦀 Purple Pincher Zero

**The minimal git-native agent. Fork this repo, register with the vault, and it starts working your repo.**

No server. No hosting. No keys in the repo. The repo *is* the agent.

</div>

---

## What is this?

Zero is the smallest complete seed of a Purple Pincher: an AI agent that lives entirely in GitHub.

- **Commits** are work
- **Branches** are explorations
- **Issues** are the task board
- **PRs** are communication
- **Git log** is memory — rewindable, time-shaped, not a snapshot

It wakes up on a schedule, thinks (LLM through the vault), acts (vault-proxied APIs), remembers (by committing), and sleeps. Every decision is a commit. You can read its mind with `git log`.

## Quick Start

```bash
# 1. Fork this repo
# (click Fork on GitHub)

# 2. Register your fork with the vault
# (see VAULT.md — one allowlist entry, no keys in the repo)

# 3. That's it. It wakes up and starts working.
```

## The zero principle

A Purple Pincher is a small, economical creature. Zero is the smallest shell one can live in:

- **Minimum moving parts** — three workflows, two scripts, one vault. Engineers should be impressed by the economy.
- **Capability-centric** — new powers arrive as small vault service handlers (plugins), never as new infrastructure. See `docs/plugins.md`.
- **The repo is the world** — the agent's whole understanding grows from *this* repo: its code, its history, its docs. It builds its own Wikipedia as it works, for its own purpose. See `docs/clones.md`.

## The tripartite

Zero is the **Logos** agent — it understands the *logic* of the application. It is one of three:

1. **The human agent** — knows the person (presentation, judgment, conversation).
2. **The git agent** — knows the application's logic. *That's this.*
3. **The hardware agent** — knows how to render the application to hardware.

The human-facing agent never touches git mechanics; the git agent never touches hardware. Each owns one demarcation. See `docs/tripartite.md`.

## Shells

As a Pincher grows, it moves into bigger shells — but the creature stays the same:

- **Zero** — this. The minimal seed. Fork-and-go.
- **Turbo** — a self-contained application shell: one repo, one builder.
- **Tapestry** — the quilt-worker: many repos, one coherent story.

See `docs/clones.md` for the shell model.

## Layout

```
.github/workflows/   think.yml, react.yml, message.yml
.github/scripts/      agent.py (the brain — thinks and acts)
docs/                 plugins.md, i2i.md, memory.md, clones.md, tripartite.md,
                      messengers.md
AGENTS.md             operator's manual
ONBOARDING.md         first-boot checklist
TASKS.md              the task board
MEMORY.md             long-term memory (committed, rewindable)
VAULT.md              how the vault works (it holds no secrets)
```

## License

MIT — take the shell, grow your own crab.
