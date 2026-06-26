# Cardloop Cockpit — Capabilities Reference

A quick guide to what the cockpit can do and how to enable each feature.

| Capability | What it does | How to set it up |
|---|---|---|
| **error handler** | **Required for services/bots.** Writes unhandled exceptions to the log → cockpit catches the incident. | Add per project type (see `## Error Handler` in CLAUDE.md and `reference/error-handler.md`). |
| **log_cmd** | Cockpit reads project logs (Logs tab, base for the error scanner). | In `topics.json` for the project: `"log_cmd": "journalctl -u my-svc -n 300 --no-pager"`. |
| **test_cmd** | "Run tests" button + quality gate for self-healing. NOT run in background. | In `topics.json`: `"test_cmd": "pytest -q"`. Path is relative to cwd. |
| **self-heal (git+clean)** | On a new incident, agent auto-fixes in a worktree → card in Review for human review. | Toggle in Overview. Requires git repo + clean tree + log_cmd. |
| **notify_on_error** | TG ping to the operator on a new incident. | Toggle "🔔 Notify on error" in Overview. |
| **healthz/liveness** | For services: project exposes `/healthz` (or `/_health`) — cockpit can ping it (future). | Add route returning 200 + `{"ok":true}`. |
| **memory** | `.claude-ops/memory/` — knowledge that travels with the repo (decisions, gotchas). | Created automatically on first agent write. |
| **secrets** | `.claude-ops/secrets/secrets.env` — keys/tokens in agent env. | "🔑 Keys" tab in cockpit, or `echo 'KEY=val' >> .claude-ops/secrets/secrets.env`. |
| **incident push** | Agent pushes incident details to the cockpit for triage. | See `reference/error-handler.md` — incident-push snippet. |
