---
created: 2026-06-13
updated: 2026-06-18
status: done
phases_shipped: core (acceptance 1-4 met)
relates_to: spec-028 (persistent client enabled), spec-021/027 (custom rotation removed)
---

> **Verification 2026-06-18** (cross-checked with Antigravity/Gemini, independently confirmed):
> Acceptance 1–4 MET — background jobs survive across turns (PERSISTENT_CLIENT live-client
> registry), `/reset` (`cmd_reset`) and cockpit New-Session (`api_project_set_session`)
> evict the live client, auto-reset-by-token removed in favour of native in-place auto-compact
> (`_make_pre_compact_hook`), mid-turn restart flushes `sessions.json` via `_graceful_shutdown`
> so the session id survives. Tests green under the canonical gate
> (`env -u WEB_COOKIE_SECURE pytest` → 1324 passed; the 7 `test_tab_activity_state` failures
> seen otherwise are a WEB_COOKIE_SECURE env artifact, not a regression).
> Acceptance 5 carve-out: two pre-existing, project-wide hygiene violations (Russian logic
> literals in webapp.py:8849; hardcoded Coolify UUID default in schedules.py:718) are NOT
> spec-039 deliverables — tracked separately under OSS-hardening spec-014 (board cards
> ops:45ae3c, ops:58412e).

# Spec 039 — Stop Killing Sessions (no auto-reset, background survives)

## Problem
The operator works 100% in the cockpit (browser). Two recurring failures destroy his work:
1. **Background tasks die between turns.** A Bash `run_in_background=true` job is killed the moment the turn ends (`<task-notification> ... <status>killed</status>`). Root cause: each turn runs as a fresh `query()` = a fresh `claude` CLI subprocess; `async with ClaudeSDKClient` tears it down on turn-end (SIGTERM→SIGKILL), reaping the detached background child (bot.py:1463 + SDK subprocess_cli.py).
2. **Sessions get auto-reset / interrupted.** Custom context rotation resets the session at 175K; auto-resume defers turns on 429; the watchdog interrupts long turns. The operator wants none of it — he resets manually.

## Constraints (verified)
- **Hard 200K context wall.** 1M context (beta `context-1m-2025-08-07`) is API-key-only + Sonnet-only → unreachable on this opus + subscription path. The wall cannot be raised.
- The only graceful way to never reset a session under a 200K wall is the CLI's **native auto-compact**, which compacts in place (~95% / ~190K) and only runs inside a **long-lived** `ClaudeSDKClient` (spec-028 `PERSISTENT_CLIENT`; machinery shipped phases 0-2, default OFF — the spec frontmatter `phases_shipped: none` is stale).
- With `PERSISTENT_CLIENT=1`, `/reset`, cockpit "New Session", and rotation do NOT currently evict the live client → a manual reset is a silent no-op (bot.py:2058, webapp.py:6926, 3985). Must fix or the operator's only control is broken.

## Design

### Enable the keystone
- `PERSISTENT_CLIENT=1`. The CLI subprocess survives across turns → background jobs survive, native auto-compact activates and replaces custom rotation.

### Remove the kill-machinery (backend)
- Delete custom rotation: `_do_session_rotation` (webapp.py:3909-4014) + its ResultMessage call site (webapp.py:7560-7583), `_maybe_rotate_tg` (bot.py:1477-1520) + call site (bot.py:1831). No code path may auto-pop a session by token count.
- Disable auto-resume: `AUTO_RESUME_ON_RATE_LIMIT` default → 0; `_maybe_auto_resume` becomes a no-op. No auto-deferred-on-429.
- Watchdog (bot.py:1610-1643): remove the stall interrupt; keep `MAX_SECONDS` only as a high last-resort ceiling (default raised to 2h) that ends the *turn*, never the session.
- Remove the Telegram context warn (bot.py:1523-1555) — operator never sees TG for this work.
- **Preserve** the `ctx_tokens` measurement (bot.py:1322) and the cockpit context info — only the auto-ACTION and the TG ping are removed.

### Keep manual control working (backend)
- `cmd_reset` (/reset, /clear, bot.py:2058), cockpit "New Session" (webapp.py:6926), and project-delete (webapp.py:1584) MUST call `_evict_live_client` so a manual reset truly starts fresh.
- Add a SIGTERM/SIGINT shutdown handler that persists in-flight `session_id`s and evicts live clients gracefully. **No self-kill** — respect the cgroup gotcha; let systemd own the process. A deploy-restart must not silently drop a session.
- Add a `PreCompact` observe-hook that emits a bus/SSE event + audit line when native auto-compact fires (evidence it works + drives the cockpit toast).

### Cockpit (browser is the only channel)
- Session-health row truth: replace "rotates at 175K" with "auto-compacts ~190K · never auto-resets". Bar reddens approaching the wall — ambient, ignorable, no popups.
- "Wrap & reset" button calls the (now-correct) eviction path.
- Toast when auto-compact fires ("context compacted · session kept").
- If the wall is ever actually hit (single-fat-turn overshoot auto-compact didn't catch): a clear chat card "session at 200K wall — [Reset session]" instead of a cryptic API error. Manual, never automatic.

## Residual risk (accepted by operator)
A single turn whose response alone jumps past ~195K can hit the wall before auto-compact fires (compaction runs between turns). Expected to self-heal on the next turn (CLI compacts before re-sending); if not, the cockpit wall-card gives one-click manual reset. **No automatic session reset under any circumstance.**

## Acceptance
- A `run_in_background=true` job started in turn N is still running in turn N+1 (not `killed`).
- No session is ever auto-reset by token count; a long session auto-compacts in place and keeps its id.
- `/reset` and cockpit "New Session" produce a genuinely fresh context (live client evicted).
- A deploy-restart mid-turn does not lose the session id.
- `pytest` green; `npm run build` clean; English-only code/UI; no personal/infra hardcode.
