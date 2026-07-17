# Domain docs

How the engineering skills should consume this project's architecture and decision docs.

## Before exploring, read these

- **`CLAUDE.md`** at the repo root — the goal, stack, working rules, and gotchas for this project.
- **`docs/specs/`** — design decisions and their rationale, if present (this project's ADR
  equivalent). Read the specs that touch the area you are about to work in.

If a referenced file does not exist, proceed silently. Don't flag its absence or propose creating
it upfront — the `/domain-modeling` skill creates these lazily when terms or decisions actually
get resolved.

## Use the existing vocabulary

When your output names a concept (a card title, a hypothesis, a test name), use the term already
used in `CLAUDE.md` and the specs. Don't drift to synonyms.

## Flag decision conflicts

If your change contradicts a recorded decision, surface it explicitly rather than silently
overriding it.
