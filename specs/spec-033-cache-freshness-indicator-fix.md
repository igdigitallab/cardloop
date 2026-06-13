---
created: 2026-06-12
status: draft
phases_shipped: none
supersedes_parts_of: spec-022
---

# Spec 033 — Fix the cache-freshness indicator (persist across reload, correct TTL)

The chat header shows a "cache warm/cold" countdown (`web/src/tabs/ChatTab.tsx`, Spec-022). It is
wrong on three counts. This spec fixes it. Frontend + a small backend addition.

## Authoritative facts about Anthropic's prompt cache (do not deviate)
- **Default ephemeral cache TTL = 5 minutes.** The 1-hour TTL is opt-in (`cache_control.ttl:"1h"`)
  and this app does NOT set it (the Claude Agent SDK manages caching internally at the default).
- The TTL is a **sliding window**: every API request that re-reads the cached prefix resets the
  5-minute clock. Within one agent turn (tool calls, thinking) the cache is repeatedly re-warmed;
  the **last** refresh is ≈ turn-end. So anchoring the countdown to turn-end is correct.
- The cache is **prefix-scoped + model-scoped**. A sub-agent (Task tool) runs with a different
  prefix → a separate cache entry; it does **not** refresh the main conversation's cache.

## Current bugs
1. `const CACHE_TTL_MIN = 60` overstates freshness ~12×. The real default is **5 minutes**.
2. The countdown anchor `lastTurnEndMs` lives only in React state, set on live `result` events.
   On page reload it is `null` and the loaded history carries no per-turn timestamp or cache
   metrics, so the badge **disappears / resets** ("слетает"). It must survive reload.
3. During an **active run** the badge shows a stale countdown from the previous turn-end instead of
   reflecting that the cache is being actively kept warm.

## Backend — `webapp.py`
Extend the session transcript scan so the freshness anchor is persisted (read from the SDK
transcript, which survives reload).

- In `api_project_session_history` (≈6591) — which already returns `context_tokens` via
  `_session_context_tokens` — also return:
  - `last_turn_at`: unix **milliseconds** of the last `assistant` entry in the transcript. Read it
    from that JSONL line's `"timestamp"` field (Claude Code writes an ISO-8601 `timestamp` per
    line); parse to epoch ms. If absent/unparseable, fall back to the transcript file's mtime
    (`jsonl.stat().st_mtime * 1000`). `null` if no assistant turn.
  - `last_cache_hit_pct`: the last assistant turn's cache-hit %, computed exactly like `bot.py`
    does — `round(cache_read_input_tokens / (cache_read_input_tokens + input_tokens) * 100)` over
    the last assistant `usage` block (the "fresh" part is `input_tokens`; `cache_creation` is the
    write, not counted in the hit ratio — mirror `bot.py` lines ~1044-1047). `null` if no usage.
  - Prefer folding this into the existing single pass that `_session_context_tokens` does (one
    transcript read), e.g. a `_session_last_turn(jsonl)` helper returning `(context_tokens,
    last_turn_at_ms, last_cache_hit_pct)`, to avoid three separate file scans. Keep
    `_session_context_tokens` working (or have the endpoint use the new helper).
- Pure read; no new writes, no new endpoint. Add unit tests mirroring existing transcript-parsing
  tests: a transcript with two assistant turns yields the second turn's `timestamp` (ms) and its
  cache-hit %, and the mtime fallback path when `timestamp` is missing.

## Frontend — `web/src/tabs/ChatTab.tsx`
1. **Correct the TTL:** `const CACHE_TTL_MIN = 60` → a 5-minute constant. Prefer
   `const CACHE_TTL_MS = 5 * 60 * 1000` (the Anthropic default ephemeral TTL) and use ms
   throughout; update the two `CACHE_TTL_MIN * 60 * 1000` sites and the history-gap check at
   line ~934 accordingly. Add a comment: `// Anthropic default ephemeral prompt-cache TTL = 5 min`.
2. **Persist the anchor across reload:** when the session history loads (the effect that calls the
   session-history API and seeds messages/`context_tokens`), also read the new `last_turn_at` and
   `last_cache_hit_pct` and set `lastTurnEndMs` from `last_turn_at`. So after F5 the countdown
   resumes from the real last-turn time instead of vanishing. Also seed the empirical override:
   stash `last_cache_hit_pct` so the warm/cold ground-truth check works immediately after reload
   (e.g. a `lastCacheHitPct` state that the badge's override consults when no in-page assistant
   `metrics` exist yet).
3. **Active-run = warm:** while a run is in progress (`run != null`), treat the cache as warm and
   show e.g. `♨️ running` (or freeze the countdown), since the agent is continuously re-warming the
   prefix. Resume the normal countdown from the next `result` (turn-end) — `setLastTurnEndMs(now)`
   on result already does this; just override the display while `run` is active.
4. **Always visible:** the badge must render whenever there is any session activity — i.e. when
   `lastTurnEndMs !== null` OR `lastCacheHitPct != null` OR an in-page assistant metric exists — so
   a stale (`⚪ cold`) state is *always* shown after reload, never blank. Keep the empirical
   override (last turn's `cache_hit_pct < CACHE_COLD_PCT` ⇒ cold) — it is the ground truth and the
   exact TTL guess is only used for the live countdown estimate.
5. Keep wording honest: the tooltip should say the countdown is an **estimate** of the 5-minute
   window since the last turn, and that the actual warm/cold is confirmed by the last turn's
   measured cache-hit %.

## Out of scope / explicitly NOT changing
- Do not try to detect a sub-agent expiring the main cache mid-turn (a >5-min Task run can let the
  main prefix go cold before the turn ends; the turn's final `cache_hit_pct` reveals it post-hoc).
  A one-line comment noting this limitation is enough.
- Do not change how `bot.py` emits `result` events or how caching is configured (it isn't — the SDK
  owns it).

## Acceptance
- [ ] After a turn, reload the page: the cache badge is still present and the countdown resumes from
      the real last-turn time (does not reset/vanish).
- [ ] The countdown runs over a **5-minute** window, not 60.
- [ ] While a run is active, the badge shows warm/running rather than a stale countdown.
- [ ] A stale session always shows `⚪ cold` (never blank) after reload.
- [ ] Last turn's measured `cache_hit_pct` still overrides the timer to cold when low.
- [ ] `venv/bin/python -m pytest tests/` green; `cd web && npm run build` clean.
