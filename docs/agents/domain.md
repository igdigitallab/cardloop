# Domain docs

How the engineering skills should consume this repo's architecture and decision docs.

## Before exploring, read these

- **`ARCHITECTURE.md`** at the repo root — the code map and subsystem overview
  (this repo's `CONTEXT.md` equivalent).
- **`GOTCHAS.md`** and **`CLAUDE.md`** — hard-won traps and the working rules for agents.
- **`docs/internal/specs/spec-NNN-*.md`** — design decisions and their rationale
  (this repo's ADR equivalent; gitignored, operator-local). Read the specs that touch the area
  you are about to work in.

If a referenced file does not exist, proceed silently. Don't flag its absence or propose creating
it upfront.

## Use the existing vocabulary

When your output names a concept (a card title, a hypothesis, a test name), use the term already
used in `ARCHITECTURE.md`, `CLAUDE.md`, and the specs. Don't drift to synonyms.

## Flag decision conflicts

If your change contradicts a shipped spec or an ADR-equivalent decision, surface it explicitly
rather than silently overriding it:

> _Contradicts spec-039 (Stop must not kill the session) — but worth reopening because…_
