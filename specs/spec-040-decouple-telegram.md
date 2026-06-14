---
created: 2026-06-13
status: draft (design only — implementation is separate per-phase cards)
card: ops:4698ec
relates_to: spec-013 (multi-user), spec-015 (oss-runtime)
---

# Spec 040 — Decouple core from Telegram, remove the TG channel

## Why
Telegram was a mobile-only workaround; the mobile browser cockpit now covers that use case. TG drags in a whole surface — PTB long-polling, forum topics, 4000-char HTML chunking, TG-shaped session keys, a per-topic message queue, file inbox — that complicates the core and blocks the clean OSS / multi-user path. Goal: make the engine transport-neutral, then remove the TG channel entirely.

## Current state (verified — see G6 coupling inventory)
**The seam already mostly exists:**
- `webapp.py` never imports `bot.py`; everything crosses via the `ctx` dict (26 keys). Enforced invariant.
- `run_engine` is an opaque async generator (`session_key: str`, `cwd` param) — zero PTB types inside.
- Web-only mode already runs when `BOT_TOKEN=""` (bot.py:2400 / 2445). All 6 `ptb_app` call sites in webapp are `if ptb_app`-guarded → setting `ctx["ptb_app"]=None` already no-ops them today.
- Free-chat keys (`free-<uuid>`, webapp.py:4691) prove every state dict already handles non-TG keys.

**The coupling that remains:**
1. **Session-key format** `"{chat}:{thread}"` built by `key_of()` (bot.py:447), used as the key for topics/sessions/running/costs and parsed for TG ids in 3 spots (webapp.py:3853/3425/2697). ~15 sites — but the dicts and all readers are already neutral; only the key constructor + a one-time data migration are needed.
2. **Engine physically lives in bot.py** (the TG module): `run_engine`, `DEFAULT_AGENTS`, conductor/board prompts, `audit`, the spec-028 live-client registry, `reconcile_board`, `_graceful_shutdown`, `_build_ctx`, the state dicts + persistence. All transport-neutral — just co-located with PTB.
3. **TG-only surfaces:** PTB `Application`/polling, `cmd_*` handlers, `run_agent` (TG adapter), `_TG_QUEUE`/tg_queue.json, `fetch_files` (inbox), forum topics, `send`/chunking/`md_to_html`, `ALLOWED_USERS`/`GROUP_CHAT_ID`, `TG_CHAT_ID/TG_THREAD_ID` env injection, and the push paths `_notify_tg`/`_send_tg_ping`/`_notify_operator`.

**Latent bug to fix early:** `run_engine` uses `TELEGRAM_NUDGE` as the DEFAULT `system_prompt` when a caller passes `None` (bot.py:1280–1286) — so every COCKPIT turn currently gets the TG-flavoured prompt. Fix with a neutral default during Phase 1.

## Design — 4 phases (each shippable; flags, no big-bang)

### Phase 0 — Neutral session keys (no TG removal, no downtime)
- New projects: `api_new_project` assigns `session_key = _project_id(cwd)` instead of a `GROUP_CHAT_ID`-based key.
- One-time migration: rename existing `"{chat}:{thread}"` keys in `topics.json` + `sessions.json` to their `_project_id(cwd)` equivalents (keep the session_id values so SDK resume keeps working).
- TG keeps working (`key_of` still returns its key for TG-bound topics) until Phase 3.
- Delete the colon-parsing dead code (webapp.py:3853/3425/2697) once migrated.

### Phase 1 — Extract `engine.py`
- Move the transport-neutral block out of bot.py into `engine.py`: `run_engine` + agents/prompts + `audit` + live-client registry + `reconcile_board` + `_graceful_shutdown` + `_build_ctx` + state dicts/persistence + `resolve_project`/`build_registry`.
- Replace the `TELEGRAM_NUDGE` default with a neutral `DEFAULT_NUDGE` (or require callers to pass `system_prompt`). Fixes the latent cockpit bug above.
- Pass `_timeline_append` / `_bus_publish` as callbacks via `_build_ctx` to avoid a circular import.
- `bot.py` becomes a thin PTB shim importing `engine`; webapp imports `engine` for the entry point.

### Phase 2 — Cockpit-only run mode behind a flag
- `COPS_TG_ENABLED` env (default `0`). PTB lifecycle gated on it; the test suite runs with it off.
- Replace the operator-push paths TG provided — incident alerts (`_send_tg_ping`) and deferred-run completion (`_notify_operator`) — with a cockpit push (SSE banner / Web Push). Confirm the cockpit is self-sufficient for interactive + deferred runs, board, timeline, and error display.

### Phase 3 — Remove PTB
- Delete TG handlers/adapter/queue/forum/chunking/env-injection from bot.py and the TG push/forum helpers from webapp.py; drop `python-telegram-bot` from requirements; archive `tg_queue.json` / `inbox`. `engine.main()` becomes the sole entry point.

## Open questions (decide before Phase 2/3)
1. Replacement for background error/incident push once TG is gone — cockpit Web Push, a webhook, or accept in-cockpit-only?
2. `data/inbox/` (TG file → agent): does the cockpit need a file-upload-to-agent path before removal, or defer to a separate card?
3. Multi-user (spec-013): is one `_project_id(cwd)` slug per project enough, or must keys become `{user_id}:{cwd_slug}` from Phase 0?
4. `DEFAULT_NUDGE` content — reuse `CONDUCTOR_PROMPT` or a cockpit-optimised variant?
5. Migration: startup auto-migration vs a one-time CLI script.

## Acceptance (of this card)
This card delivers the spec only. "Done" = design approved + open questions triaged. Implementation is tracked as separate per-phase cards.
