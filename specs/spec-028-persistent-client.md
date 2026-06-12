---
created: 2026-06-11
updated: 2026-06-11
status: draft
phases_shipped: none
card: ops:spec028
depends_on: spec-027 (interim mitigations land first)
---

# Spec 028 — Persistent `ClaudeSDKClient` per Session (Native Auto-Compact)

## Goal

Give ClaudeOps **native context management** (auto-compact) and remove per-turn subprocess
latency by migrating `run_engine` to a **long-lived `ClaudeSDKClient` per `session_key`**.
This makes our custom context-rotation (spec-021/027) redundant.

**Honest scope correction (post-cache-research).** This is NOT primarily a cache play. The
prompt cache is **server-side** and the CLI already uses the **1-hour extended TTL**, so
today's `query()`-per-turn already gets cache HITS when a session resumes within an hour
(measured 96–99% within active sessions). A persistent local client does **not** keep the
server cache warmer. The two real wins are: (1) **native auto-compact** fires only in a
long-lived client — it caps context growth and prevents the **200K hard wall**, replacing our
custom rotation/backstop; (2) **latency** — no ~300–500 ms subprocess respawn per turn.

This is **Track 2**. The interim mitigations (spec-027) land first and are independent.

---

## Why this is the real fix

| | Terminal CLI | ClaudeOps today (`query()`/turn) | ClaudeOps after spec-028 |
|---|---|---|---|
| Process | one, long-lived | fresh subprocess each turn | one long-lived per session |
| Auto-compact | yes (native) | **never fires** | yes (native) |
| Per-turn latency | none | subprocess respawn ~300–500 ms | none |
| Server cache (1h TTL) | hits within 1h | **hits within 1h** (already) | hits within 1h |
| Custom rotation needed | no | yes (our hack) | **no — delete it** |

Verified against SDK 0.2.96 source + real transcripts: auto-compact lives **inside the CLI
subprocess** (`ContextUsageResponse.isAutoCompactEnabled` / `autoCompactThreshold`,
`PreCompact` hook). It requires a subprocess that survives multiple turns. Our per-turn
teardown is precisely what prevents it. (Server-side prompt caching is unaffected by the
process model — it already works across subprocess deaths within the 1h TTL.)

---

## Design

### Live-client registry

A new `ctx["live_clients"]: dict[session_key, _LiveEntry]`, alongside `running`/`sessions`
(shared by reference between bot.py and webapp.py — no new import direction).

```
@dataclass
class _LiveEntry:
    client: ClaudeSDKClient
    cwd: str
    model: str          # model this client connected with
    last_used: float    # monotonic; updated each turn
    idle_task: asyncio.Task | None
```

### Lifecycle

- **First turn for a `session_key`:** `connect()` a `ClaudeSDKClient(options=opts)` with
  `resume=resume_session_id`; store the entry. The client must outlive `run_engine` — i.e.
  NOT wrapped in the current `async with`.
- **Subsequent turns:** reuse the live client; `client.query(prompt)` + iterate
  `receive_response()`; bump `last_used`; reschedule idle timer.
- **Disconnect triggers:** idle eviction (`CLIENT_IDLE_SECONDS`, default 900s — Anthropic
  cache TTL is minutes-scale, so a long-idle warm process buys nothing); `/reset`; model or
  cwd mismatch (reconnect); LRU cap (`CLIENT_MAX_LIVE`, default ~15–20 — each live CLI
  subprocess ~30–60 MB RSS); service restart (all die, see Recovery).

### `run_engine` rewrite (preserve the event contract)

The external interface — async generator of `{tool|text|result|rate_limit|error|subagent}`
— stays **byte-identical** so TG/web/card consumers don't change. Internally, replace the
per-turn `async with ClaudeSDKClient(...)` with: look up / create a live client, then
`client.query()` + `async for msg in client.receive_response()`. On any unhandled SDK
exception, **evict** the client (subprocess state unknown) and yield `{type:error}`.

`ctx` must be threaded into `run_engine` (currently not a param). Add `ctx=None,
ephemeral=False`; when `ctx is None` or `ephemeral`, fall back to the current stateless
`async with` path. This doubles as the feature-flag seam.

### Native auto-compact + UI

- Fires natively in the live client at the CLI threshold. Threshold tunable via
  `extra_args` (exact flag name = **spike**, see Open Questions); CLI default (~95% of
  window) acceptable initially.
- Wire a `PreCompact` hook (`ClaudeAgentOptions.hooks`) → `_bus_publish(session_key,
  {kind:"compact", trigger})` so the cockpit Activity stream shows `♻️ auto-compact`
  instead of our custom rotation event.
- Cache chain resets at each compaction (inherent) but every turn between compactions is
  warm — strictly better than today's cold-every-turn.

### Point-by-point

- **Watchdog:** unchanged. `running[session_key]` already holds the live client during a
  turn; `client.interrupt()` works on the persistent subprocess. After interrupt the client
  stays alive, ready for the next `query()`.
- **Model switch:** model is fixed at `connect()`. `/model` → **evict + reconnect** with the
  new model (do NOT use `set_model()` — cache invalidates per-model either way; clean
  reconnect avoids mid-session confusion). Surface `⚙️ model changed → reconnecting…`.
- **Restart recovery:** live clients are in-memory only → all die on `restart-self.sh`
  (already aborts in-flight turns). `sessions.json` persists `session_id`s on disk → next
  turn reconnects via `resume`, CLI loads history from the JSONL transcript. Warm cache lost
  (same as today), history intact. **No change needed.**
- **Deferred runs (spec-020):** `_execute_deferred` reuses the live client if present, else
  reconnects via `resume`. No change.
- **Cards (spec-021 Part 2):** cards are one-shot and must stay context-isolated → run with
  `ephemeral=True` (old `async with` path, fresh session, no live client). `cwd_locks` still
  serialize concurrent writes to the same cwd.
- **Concurrency:** `running[k]` stays "turn in progress" (not "client connected"); the live
  client lives in `live_clients`, not `running`.

### Decisions table

| Question | Decision |
|---|---|
| Client scope | one per `session_key` |
| Connect / disconnect | first turn / idle-evict, `/reset`, model|cwd change |
| Idle timeout | `CLIENT_IDLE_SECONDS=900` |
| LRU cap | `CLIENT_MAX_LIVE≈15` |
| Storage | `ctx["live_clients"]`, in-memory only |
| Cards | `ephemeral=True` → old path |
| Model switch | evict + reconnect (not `set_model`) |
| Auto-compact | native; PreCompact hook → UI bus |
| Custom rotation | **delete** after stable (Phase 4) |
| Rollout | feature flag `PERSISTENT_CLIENT=0/1` |

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Feature flag `PERSISTENT_CLIENT` (default 0 → no behaviour change) | planned |
| 1 | Thread `ctx=None, ephemeral=False` into `run_engine` + all call sites (flag off, no-op) | planned |
| 2 | Implement `_LiveEntry`, `_get_or_create_live_client`, evict / LRU / idle timer + unit tests | planned |
| 3 | Enable on one project (`PERSISTENT_CLIENT=1`); verify rising `cache_hit_pct` + auto-compact fires | planned |
| 4 | Delete spec-021/027 custom rotation once stable ~1 week; keep PreCompact UI event | planned |
| 5 | Remove flag (always-on) | planned |

---

## Acceptance

- [ ] With flag off, behaviour is byte-identical to today (all ≈950 tests green).
- [ ] With flag on, a second turn in the same session reuses the live client (no reconnect)
      and shows `cache_hit_pct` climbing turn-over-turn.
- [ ] Native auto-compact fires on a long session (PreCompact hook event observed); no custom
      rotation runs.
- [ ] `/reset` and `/model` evict the live client; next turn reconnects cleanly.
- [ ] Card runs use a fresh, isolated session (never the live chat client).
- [ ] After `restart-self.sh`, next turn reconnects via `resume`; history intact.
- [ ] Idle session evicted after `CLIENT_IDLE_SECONDS`; LRU cap enforced.

---

## Risks (ranked)

1. **Async-context affinity (HIGH).** SDK warns a client can't cross async runtime contexts.
   bot.py is single-loop → safe, but **spike**: confirm concurrent `query()` on different
   `session_key` clients is fine (almost certainly — separate clients/subprocesses).
2. **Subprocess leak on exception (HIGH).** Evict must `asyncio.wait_for(disconnect(),
   timeout=10)`; on timeout, log + force-drop the entry.
3. **State bleed across turns (MED).** A confused agent state in turn N persists to N+1
   (the point of warmth, but a footgun). Escape hatch: `/reset` = hard evict.
4. **Memory growth (MED).** Context accumulates in-subprocess until compaction (~190K at 95%
   threshold). Fine for ~10–15 live sessions on docker-core; idle-evict + LRU cap = ceiling.
5. **Card isolation regression (LOW).** Mis-flagging a card as non-ephemeral bleeds card
   output into chat history. Mitigate: test asserts card runs get a fresh `session_id`.

---

## Open questions (spike before committing)

1. Exact CLI flag for the auto-compact threshold (for `extra_args`). `claude --help | grep
   compact`.
2. Is `PreCompact` observe-only, or can `custom_instructions` steer the summary? (No
   `hookSpecificOutput` type for PreCompact in `types.py` — confirm.)
3. Idle-timer placement: must be a `ctx`-stored `asyncio.Task`, not tied to a turn coroutine
   (else cancelled when the turn's task group ends).
4. Resume-after-eviction: confirm reconnect with `resume=session_id` loads prior history and
   keeps the session_id stable (no fork unless `fork_session=True`).

---

## Honest verdict on "≤ CLI token usage"

**Achieved for active projects** (multiple turns/hour): same subprocess, same auto-compact,
same warm cache chain as the CLI. **Residual gaps, all unavoidable and equal to the CLI's
own cold paths:** first turn of a session, first turn after restart, first turn after idle
eviction — each pays a cold baseline (which spec-027's CLAUDE.md diet shrinks). For one-turn-
per-day projects there's no persistent benefit but **no regression** either. The spec-021
custom rotation is strictly worse than native auto-compact for active projects and equally
useless for idle ones — deleting it and shipping this is a pure win.

---

## Related

- Spec 027 — Interim context-cost reduction (lands first; this spec deletes its rotation
  machinery in Phase 4, keeps the CLAUDE.md diet permanently).
- Spec 020 — Deferred runs (must keep resuming the correct session).
- Spec 021 — Context rotation (superseded by native auto-compact here).
- Spec 017 — Fable orchestrator (per-session model pinning resolves the `/model` cache cost).
