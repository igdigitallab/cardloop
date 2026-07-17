# Issue tracker: Cardloop board

Issues and specs for this project are **Cardloop kanban cards**, not GitHub Issues. The board
lives in `TASKS.md` and is reconciled by the cockpit after every turn.

## Conventions

- Columns are markdown H2 sections: `## Backlog` / `## In Progress` / `## Review` / `## Failed`.
  Completed work is archived to `DONE.md` (sessions do not read it — context hygiene).
- One card per line: `- [ ] <text>` strictly inside a column section. Card wording:
  verb + object + done-criterion.
- The `<!--ops:ID-->` marker is appended automatically by the reconciler on the first read —
  never write it by hand.
- No numbered lists, nested sublists, tables inside sections, or text outside a section
  (except the preamble before the first `##`).

## When a skill says "publish to the issue tracker"

Add a `- [ ] <text>` line under `## Backlog` in `TASKS.md`. Do **not** use `gh issue create` —
this project does not track work in GitHub Issues.

## When a skill says "fetch the relevant ticket"

Read the card text from `TASKS.md` (match by the `ops:ID` marker or the card text).

## Blocking edges

Cardloop cards are flat — there is no native "blocked by" link. When a `/to-tickets` effort needs
an order, list the tickets top-to-bottom under `## Backlog` and work them in that order by hand.

## PRs as a request surface

Off. External PRs are not part of the triage queue here.
