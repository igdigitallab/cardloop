# Triage labels

The skills speak in five canonical triage roles. This repo's tracker is the **Cardloop board**,
which has **no GitHub labels** — the roles are a shared vocabulary used in card wording and chat,
not label strings.

| Role in mattpocock/skills | How it maps here |
| --- | --- |
| `needs-triage`    | New card in `## Backlog`, not yet evaluated |
| `needs-info`      | Card blocked on operator input — note it in the card text |
| `ready-for-agent` | Card is specified enough for an autonomous run |
| `ready-for-human` | Requires the operator; keep in `## Backlog` |
| `wontfix`         | Remove the card (or move to `DONE.md` with a note) |

Do **not** run `gh label create` — there are no GitHub labels in this workflow.
