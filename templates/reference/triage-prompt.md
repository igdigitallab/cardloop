# Triage Prompt — ranking all projects before an audit

Run **once** before a series of audits. The output is a priority table that determines the order.

---

## Prompt

```
# Triage task: all active projects

Read:
1. $HOME/CLAUDE.md — list of App UUIDs (project table)
2. $VAULT/01-Projects/BOARD.md — statuses and priorities
3. $VAULT/01-Projects/*/README.md — business context for each project

## Mode: TRIAGE ONLY
Do not open project code. Metadata + README + status only.

## Task

Create a table in `$VAULT/01-Projects/Audit-Campaign/triage-<YYYY-MM-DD>.md`:

| Project | Business criticality | Prod users | Money/PII | Code complexity | Audit priority |
|---|---|---|---|---|---|

Columns:
- **Business criticality:** P0 (without it money/reputation are at risk) / P1 (important in-house tool) / P2 (useful) / P3 (hobby/disabled)
- **Prod users:** yes / no (operator only)
- **Money/PII:** yes (payments, personal data, authorization) / no
- **Code complexity:** S (<500 lines) / M (500–3k) / L (>3k) — rough estimate from repo size
- **Audit priority:** 1 (immediate) / 2 (this quarter) / 3 (can defer) / SKIP (rss-bot, disabled projects)

## Ranking rules

1. **Audit priority 1** = has prod users + (money OR PII)
2. **Audit priority 2** = has prod users without money/PII, OR in-house tool that the infrastructure depends on
3. **Audit priority 3** = operator is the only user; if it breaks — not critical
4. **SKIP** = rss-bot (disabled), any project in archive

## After the table

Give a recommendation:
"Start with: <PROJECT_NAME>. Reason: <one phrase>. Command: open a new session with the prompt from $VAULT/03-Resources/_templates/audit-prompt.md, replace <PROJECT> with <PROJECT_NAME>."
```

---

## Related templates

- [[audit-prompt]] — after triage, take the top project and audit it
- [[project-baseline]] — quality standard
