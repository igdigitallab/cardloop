---
created: 2026-06-11
updated: 2026-06-11
status: shipped
phases_shipped: 1, 2, 3
card: ops:spec022
---

# Spec 022 — Cost Visibility (per-turn metrics + cache-freshness timer)

## Goal

Make the invisible token economy **visible in the web cockpit at the moment the
operator decides whether to send** — so before typing they can see if the next turn
will be cheap (cache warm) or expensive (cold reread), and after each turn they see the
factual cost of that turn (duration, cache-hit %, tokens, window %). This is the third
leg of the "economical + smart" stool: spec-021 keeps sessions lean (rotation), the
model-routing card (backlog) routes cheap work to cheap models, and **022 shows the
operator the price of each turn in the currency they actually think in (% of window)**.

Scope is the **web cockpit only**. Telegram is out of scope (it already has `/cost` and
`/usage`, and TG messages can't host a live countdown). TG was only the comparison example.

---

## Context / Motivation

The model has no memory: every turn re-sends the full prompt. The economics of that are
real but invisible — the operator only learns a turn was expensive *after* the window
% jumps. Two facts drive everything:

1. **Cache state determines turn cost.** A warm prefix is read at ~10% price; a cold
   start re-pays the full prompt (and, for hour-TTL cache, re-writes at ~2×). The
   operator currently has zero signal which one the next turn will be.
2. **The truth is already in the data.** Every `result` event from the SDK carries
   per-turn `usage` — `input_tokens`, `cache_read_input_tokens`,
   `cache_creation_input_tokens` (bot.py:662-665) — plus `total_cost_usd` and
   `duration`. The cache-hit ratio is a **fact**, not an estimate. We just don't surface it.

### Honesty principle (must be reflected in UI tooltips)

- **Header freshness timer = ESTIMATE.** Anthropic does not expose cache state to the
  client. The timer is a client-side countdown (last-turn-end + TTL), labelled as a
  *prediction*: "is the next turn likely cheap or cold". It can be wrong.
- **Per-turn footer = FACT.** Cache-hit %, tokens, duration come straight from the
  `result` event `usage`. No oauth, no guessing. The header predicts; the lente proves.

This split is the whole point — never present the estimate as fact or vice-versa.

---

## Design

### Part 1 — Backend: enrich the `result` event with per-turn facts

`bot.py` `run_engine()` already computes the per-turn prompt size at lines 660-687:

```
u = getattr(msg, "usage", None) or {}
pt = (u.get("input_tokens",0) + u.get("cache_read_input_tokens",0)
      + u.get("cache_creation_input_tokens",0))
...
yield { "type":"result", ..., "cost_usd": getattr(msg,"total_cost_usd",None),
        "context_tokens": last_ctx_tokens }
```

Add the following fields to that same `result` dict (all derived from data already in
hand — no new API calls):

| Field | Source | Meaning |
|-------|--------|---------|
| `cache_read_tokens` | `u["cache_read_input_tokens"]` | tokens served from cache this turn |
| `fresh_tokens` | `u["input_tokens"] + u["cache_creation_input_tokens"]` | tokens billed at full price this turn |
| `prompt_tokens` | the existing `pt` | total prompt this turn (= read + fresh) |
| `cache_hit_pct` | `cache_read / pt` (0 when `pt==0`) | fraction of prompt from cache |
| `duration_ms` | `getattr(msg,"duration_ms",None)` | wall time of the turn |

- **Verify the duration attribute name** on the SDK `ResultMessage` before relying on it
  (likely `duration_ms`; `duration_api_ms` also exists). If neither is present, measure
  wall-clock in `run_engine` (capture a start timestamp when the turn begins, diff at the
  `result` yield) — do NOT fabricate. `duration_ms` may be `None`; tolerate it.
- Keep `cost_usd` flowing in the event for completeness but **do NOT render dollars in the
  UI** (CLAUDE.md gotcha: "$ убран — на подписке шум"). The UI currency is tokens / % / cache.

### Part 1b — Webapp SSE forwards the new fields

`webapp.py` `api_project_chat` forwards the `result` event to the cockpit at lines
5670-5671 (currently only `context_tokens`). Extend `_send` of the result event to pass
through the new fields: `cache_read_tokens`, `fresh_tokens`, `prompt_tokens`,
`cache_hit_pct`, `duration_ms`. Also include the current window snapshot
`utilization` (0-1) from the existing cached `_get_usage_limits()` (webapp.py:4047) —
**best-effort, no fresh oauth call**: read the cache, pass `null` if stale/missing. This
feeds the optional per-turn window-delta (Part 4) without adding latency.

`history` endpoint (`_session_context_tokens`, webapp.py:5487-5537) already returns
`context_tokens` for hydration — no change needed there.

### Part 2 — Frontend lente: timestamps + per-turn metric footer + cold-start divider

In `web/src/tabs/ChatTab.tsx` (message rendering):

1. **Timestamp** on each message (user + assistant), thin and muted, e.g. right-aligned
   `HH:MM`. Stamp the assistant message when its `result` event arrives; stamp the user
   message on send. Persist enough in message state to show stamps on re-render within
   the session (history hydration without stamps is acceptable — show stamps only for
   messages produced live this session; do not invent past timestamps).
2. **Per-turn footer** under each assistant reply, built from the `result` event:
   `HH:MM · ⏱ {duration} · ♨️ cache {cache_hit_pct}% · {prompt_tokens}K`
   - `♨️` (warm) when `cache_hit_pct >= 70`, `🧊` (cold) when `cache_hit_pct < 30`,
     no glyph in between — pick thresholds as constants `CACHE_WARM_PCT=70`,
     `CACHE_COLD_PCT=30`.
   - Duration formatted human: `38s`, `2m 41s`. Omit the `⏱` clause if `duration_ms` null.
   - Tooltip on the footer: "Facts from this turn's usage — cache-read is billed ~10%,
     fresh tokens at full price."
3. **Cold-start divider** between turns when the wall gap since the previous turn exceeds
   the cache TTL: a thin centered plate `⚪ paused {gap} · cache cold`. This is the
   *pre-send* hint; the actual `🧊` on the resulting turn's footer confirms it post-fact.
   Use the same `CACHE_TTL_MIN` constant as Part 3.

### Part 3 — Frontend header: cache-freshness countdown

Next to the existing spec-021 context indicator (ChatTab.tsx:596-625, the `{N}K` badge +
Wrap & reset), add a live freshness timer:

- `♨️ cache {MM:SS}` counting **down** while warm (green), flipping to `⚪ cache cold`
  (muted) when it hits zero.
- **Resets to `CACHE_TTL_MIN:00` on every completed turn** (any `result` event for this
  project). Driven by a client `setInterval` (1s tick) — this is a web-only affordance.
- `CACHE_TTL_MIN` default **60** (basis: Claude Code writes hour-TTL cache — observed
  `ephemeral_1h_input_tokens` in usage; the value is an estimate so keep it a single
  named constant, easy to retune). Put a one-line code comment citing the basis.
- Tooltip: "Estimate — cache state isn't exposed by the API. Warm ≈ next turn cheap;
  cold ≈ next turn re-reads the full prompt at full price."

### Part 4 — (Optional / stretch) per-turn window-delta

If `utilization` is present on consecutive `result` events, show `· −{Δ}% window` in the
footer (current minus previous utilization × 100). **Mark clearly as best-effort** —
the oauth `utilization` is 60s-cached and lags, so the delta can be 0 or jumpy. If it
proves noisy in practice, drop the clause; do not block Parts 1-3 on it. Never show a
negative-looking number as if precise — round to 1 decimal, hide when `< 0.1%`.

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: emit `cache_read_tokens`/`fresh_tokens`/`prompt_tokens`/`cache_hit_pct`/`duration_ms` in `result`; SSE pass-through + `utilization` snapshot | pending |
| 2 | Lente: message timestamps + per-turn metric footer + cold-start divider | pending |
| 3 | Header: cache-freshness countdown timer (resets each turn) | pending |
| 4 | (stretch) per-turn window-delta `−X% window` | pending |

---

## Acceptance

- [ ] A `result` event from `run_engine` carries `cache_read_tokens`, `fresh_tokens`,
      `prompt_tokens`, `cache_hit_pct` (0-100), `duration_ms` (or `null`).
- [ ] `cache_hit_pct == 0` when `prompt_tokens == 0` (no divide-by-zero).
- [ ] api_project_chat SSE forwards all five fields + `utilization` (or `null`) to the client.
- [ ] No dollar amount is rendered anywhere in the cockpit UI (tokens/%/cache only).
- [ ] Each assistant reply shows a footer `HH:MM · ⏱ … · ♨️/🧊 cache N% · …K`.
- [ ] Footer omits the `⏱` clause when `duration_ms` is null (no "NaN"/"undefined").
- [ ] `♨️` shown when cache-hit ≥ 70%, `🧊` when < 30%.
- [ ] Header shows `♨️ cache MM:SS` counting down; flips to `⚪ cache cold` at zero.
- [ ] A completed turn resets the freshness countdown to `CACHE_TTL_MIN:00`.
- [ ] Cold-start divider appears between two turns separated by more than `CACHE_TTL_MIN`.
- [ ] spec-021 context indicator + Wrap & reset still work unchanged (no regression).
- [ ] Header/timer tooltips state "estimate"; footer tooltip states "facts from usage".

---

## Tests

`tests/test_cost_visibility.py` (backend):
- `result` event field presence + values for a synthetic `usage` (read/fresh mix).
- `cache_hit_pct` math incl. the `pt==0` guard.
- `duration_ms` passthrough incl. the `None` case.
- SSE forward includes the new fields + `utilization` null-when-stale.
- Regression: `result` still carries `context_tokens` and `cost_usd` (spec-021 contract).

Frontend: extend existing ChatTab test(s) if present (footer renders given a result
event; freshness timer mounts; cold-start divider appears past threshold). If no FE test
harness, document a manual check in the PR/commit.

---

## Constraints (do not violate)

- **No dollars in UI** — subscription makes `$` noise (CLAUDE.md gotcha line 66).
- **No new oauth calls per turn** — reuse the 60s `_usage_cache`; pass `null` when stale.
- **English-only** code/comments/UI strings (operator reply language is separate env).
- **Don't break spec-021** rotation indicator, Wrap & reset, or the `context_tokens` flow.
- **TG untouched** — no per-message footers in Telegram this spec.
- Honesty: header timer labelled estimate, footer labelled fact — never mixed.

---

## Risks

- **`duration_ms` attribute name / nullability.** Verify on the SDK ResultMessage; fall
  back to wall-clock measurement. Tolerate null without rendering garbage.
- **Window-delta noise (Part 4).** oauth utilization lags 60s → delta unreliable; it's
  optional and droppable, must not block Parts 1-3.
- **Freshness TTL is an estimate.** 60min is a best guess; isolated in one constant so a
  wrong value is a one-line retune, and the UI never claims it's authoritative.
- **Timestamp persistence across rotation/reload.** Live-session stamps only; do not
  fabricate stamps for hydrated history.

---

## Non-goals

- Per-message cost in dollars (deliberately excluded).
- Telegram per-message metrics / footers.
- Historical cost analytics page or `/usage` breakdown-by-project (separate idea on board).
- Reading true cache state from Anthropic (not exposed) — everything client-side is estimate.

---

## Related

- Spec 021 — Context rotation (provides the indicator + Wrap & reset this spec sits beside)
- Spec 017 — Fable orchestrator (cheaper turns ⇒ better conductor economics)
- Backlog card — model-routing (the spend-reduction leg; 022 is the spend-visibility leg)
- CLAUDE.md gotchas: `cost_usd` noise on subscription; oauth `utilization` source of %.
