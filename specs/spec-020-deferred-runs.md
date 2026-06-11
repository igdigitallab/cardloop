---
created: 2026-06-11
updated: 2026-06-11
status: draft
card: ops:98748d
---

# Spec 020 — Deferred Runs: scheduled and rate-limit-aware prompt execution

## Goal

Let an operator write a prompt and schedule it to fire at a specific time **or** after the
subscription's 5-hour rate-limit window resets. When the window is nearly exhausted the
operator queues work instead of waiting manually — the run starts automatically once the
window reopens.

---

## Context / Motivation

### The use-case

The Claude subscription enforces a 5-hour rolling usage window. When utilization is high
(approaching the cap), the next long run will be rate-limited mid-flight. The operator
can see this in the `/usage` command or in the cockpit Usage widget. Today the only
recourse is to wait, then manually re-fire. This spec closes that gap.

Two flavours of deferral are needed:

1. **Time-based (`fire_at`):** operator specifies an absolute UTC timestamp or a
   natural-language offset ("in 2 hours"). Simple cron-replacement for low-urgency work.

2. **Rate-limit-aware (`fire_on_reset`):** the system reads `resets_at` from the OAuth
   usage endpoint at fire-time (not at queue-time) and fires automatically after jitter.
   If the window is already free, fires immediately.

### Why not reuse the card queue drain loop?

The existing `_queue_drain_loop` (webapp.py:3188) drains `data/card_queue.json` — a FIFO
of `card_id` strings for kanban cards that are ready to run but their project slot was
busy. Its purpose is "run queued kanban cards in FIFO order". Its drain predicate is
`not running[k]` — it fires as fast as the slot frees.

Deferred runs have a different firing predicate: **a wall-clock or reset-time condition**.
Mixing the two would require adding time-gating logic to a loop that today is purely
slot-gated, and would force every card into the deferred schema. The right call is a
**separate `_deferred_loop`** with its own poll interval, storage file, and firing
predicate. The two loops are independent; they share only the `running[k]` busy-check
before actually launching a run.

### How resets_at is known

Two sources, already wired:

- **Passive (SDK):** `RateLimitEvent` is caught at `bot.py:597-605`, yielded as
  `{"type":"rate_limit", "status":..., "resets_at":..., "utilization":...}`.
  SDK gives `utilization=None` on subscription auth — so only `resets_at` is reliable.

- **Active (OAuth):** `webapp.py api_usage` (lines 3573-3600) calls
  `GET https://api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`),
  caches 60 s, returns per-limit-type `{status, resets_at, utilization}`. This is the
  authoritative source for `fire_on_reset` resolution because it gives `utilization`
  (0–1 float), which tells us whether the window is actually free right now.

---

## Design

### Storage: `data/deferred.json`

Persists across restarts. Format: a JSON array of deferred run records.

```json
[
  {
    "id": "def-<8-char hex>",
    "project": "networking-os",
    "session_key": "chat:thread",
    "prompt": "Run a full audit of the networking-os project",
    "fire_at": null,
    "fire_on_reset": true,
    "created": "2026-06-11T14:00:00Z",
    "status": "pending",
    "fired_at": null,
    "error": null,
    "attempts": 0
  }
]
```

**Field notes:**

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Stable; `def-` prefix + 8 hex chars; generated at creation |
| `project` | `string` | Project name (matches topics.json `project` field) |
| `session_key` | `string` | `"chat:thread"` key for `running[]` / `sessions[]` lookup |
| `prompt` | `string` | The full prompt text to execute |
| `fire_at` | `string \| null` | ISO-8601 UTC timestamp; mutually exclusive with `fire_on_reset` |
| `fire_on_reset` | `boolean` | If true, fire after next rate-limit reset (+ jitter); `fire_at` must be null |
| `created` | `string` | ISO-8601 UTC; set at creation |
| `status` | `"pending"\|"fired"\|"cancelled"\|"failed"` | Lifecycle state |
| `fired_at` | `string \| null` | ISO-8601 UTC; set when run actually started |
| `error` | `string \| null` | Human-readable error reason on `failed` |
| `attempts` | `int` | Re-queue attempt counter; max = `DEFERRED_MAX_ATTEMPTS` (default 5) |

Writes are atomic: write to `data/deferred.json.tmp` then `os.replace`.

### Deferred loop: `_deferred_loop`

Background async task started in `start()` alongside the queue drain loop.

**Poll interval:** `DEFERRED_POLL_SEC` (env, default `30`).

**Startup delay:** 15 s (same rationale as queue drain: let the bot settle and the OAuth
usage cache warm up).

**Per-iteration logic:**

```
for each record where status == "pending":
    if fire_on_reset:
        usage = await _get_cached_usage()         # existing api_usage cache
        limit = usage["limits"].get("five_hour")
        if limit is None:
            continue                               # can't resolve; try next poll
        if limit["utilization"] < DEFERRED_FREE_THRESHOLD:  # default 0.10
            fire_now = True
        else:
            resets_at = limit["resets_at"]        # unix timestamp
            jitter = random.randint(30, 90)        # seconds
            fire_now = (time.time() >= resets_at + jitter)
    elif fire_at is not None:
        fire_now = (time.time() >= _iso_to_unix(record["fire_at"]))
    else:
        continue                                   # malformed; skip

    if not fire_now:
        continue

    # Check busy
    k = record["session_key"]
    if ctx["running"].get(k):
        record["attempts"] += 1
        if record["attempts"] >= DEFERRED_MAX_ATTEMPTS:
            record["status"] = "failed"
            record["error"] = "project busy after max attempts"
            _save_deferred(records)
            await _notify_operator(ctx, f"Deferred run {id} failed: project busy")
            continue
        # Re-queue: push fire_at 5 min forward
        record["fire_at"] = _unix_to_iso(time.time() + 300)
        record["fire_on_reset"] = False
        _save_deferred(records)
        continue

    # Fire
    record["status"] = "fired"
    record["fired_at"] = _utcnow_iso()
    _save_deferred(records)
    await _notify_operator(ctx, f"Starting deferred run: {record['prompt'][:80]}…")
    asyncio.create_task(
        _execute_deferred(ctx, record)
    )
```

`_execute_deferred` mirrors `_run_card`: calls `run_engine(...)` with the record's
`session_key`, `project`, `cwd` (resolved from `topics[session_key]`), and `prompt`.
On completion it notifies the operator (success: last result text truncated to 200 chars;
failure: error message). On exception → sets `record["status"] = "failed"`, saves,
notifies.

**`DEFERRED_FREE_THRESHOLD`** (default `0.10`): if `utilization < 0.10` the window is
considered free and the run fires immediately without waiting for `resets_at`. This
handles the case where the operator creates a `fire_on_reset` deferred but by poll-time
the window has already refreshed.

**Timezone note:** All timestamps stored and compared in UTC. The operator's LA timezone
is relevant only for the `/later` command input parsing (Phase B), where "2pm" is
interpreted as America/Los_Angeles. The `fire_at` field is always stored in UTC ISO-8601.
The deferred loop compares `time.time()` (always UTC) against stored timestamps.

### Busy-check re-queue

When `running[k]` is set at fire time:
- Increment `attempts`.
- If `attempts < DEFERRED_MAX_ATTEMPTS`: push `fire_at` forward 5 minutes, clear
  `fire_on_reset`, save.
- If `attempts >= DEFERRED_MAX_ATTEMPTS`: mark `failed`, save, ping operator via TG.

This does **not** use the card queue drain loop — deferred runs do not create kanban
cards. They are direct `run_engine` calls.

### Operator notifications

Two notification points per deferred run:

1. **At creation:** TG message confirming the deferral: project, prompt preview (80
   chars), and scheduled trigger (`fire_at` or `"after rate-limit reset"`).

2. **At fire/completion:** TG message with run result (last `result` event text, up to
   200 chars) or failure reason. Uses the existing TG send path via the bot's token and
   the operator's `chat_id` from `ALLOWED_USERS`.

Internal implementation: a helper `_notify_operator(ctx, text)` that calls the same
`_tg_call` path already used for incident notifications.

### Schedules tab integration (spec-019)

Pending deferred runs appear in `GET /api/schedules` as records with `source: "deferred"`.
The normalised schema maps as follows:

| deferred field | schedules field |
|---|---|
| `id` | `id` |
| `"deferred"` | `source` |
| `fire_at` \| derived | `schedule` (ISO string or `"after reset"`) |
| `prompt` (truncated 60) | `command` |
| `project` | `project` |
| `fired_at` | `last_run` |
| computed next poll | `next_run` |
| pending→`unknown`, fired→`ok`, failed→`broken`, cancelled→`unknown` | `status` |
| `"Deferred run"` | `purpose` |

Cancellation: `DELETE /api/deferred/{id}` sets `status = "cancelled"`. The spec-019
Schedules tab already expects `DELETE` to work via whatever endpoint the source exposes
(the source registry is extensible). Phase C wires the Schedules tab to show deferred
records and expose a cancel button.

---

## API

### `POST /api/deferred`

Create a deferred run.

**Request body:**
```json
{
  "project": "networking-os",
  "prompt": "Run a full audit…",
  "fire_at": "2026-06-12T09:00:00Z",
  "fire_on_reset": false
}
```

Exactly one of `fire_at` or `fire_on_reset: true` must be provided; both or neither → 400.

`project` must match a key in `topics.json`; unknown project → 400.

**Response (201):**
```json
{"id": "def-a3f7c2b1", "status": "pending"}
```

### `GET /api/deferred`

Returns all deferred records. Query params: `?status=pending`, `?project=<id>`.

### `DELETE /api/deferred/{id}`

Cancel a pending deferred run. Returns 200 `{"cancelled": true}` or 404 if not found.
Already-fired or failed runs → 409 `{"error": "already fired/failed"}`.

---

## Inputs (entry points)

### (1) Cockpit chat UI

A clock-icon button (⏱) placed beside the Send button in `ChatTab.tsx`. Clicking it
opens a modal with:
- A text area pre-populated with the current draft prompt.
- Two tabs: **"At time"** (datetime-local input, defaults to now + 30 min) and
  **"After reset"** (no further input needed; shows current `resets_at` from
  `GET /api/usage` as informational text).
- Submit button: `POST /api/deferred`.
- On success: toast notification; the prompt textarea is cleared.

### (2) Telegram `/later` command

```
/later reset Run a full audit of the networking-os project
/later 14:30 Run the deployment
/later 2h Run the deployment
```

Syntax: `/later <time_spec> <prompt>`

`time_spec` options:
- `reset` — fires after rate-limit reset.
- `HH:MM` — interpreted as today at that time in America/Los_Angeles; if the time is in
  the past, next-day semantics.
- `Nh` / `Nm` — N hours or minutes from now.
- An absolute ISO-8601 string.

The command handler (`cmd_later`) is registered via:
```python
ptb_app.add_handler(CommandHandler("later", cmd_later))
```
at the same registration block as other commands (bot.py:1247-1264).

`cmd_later` resolves the session's project from `topics[key_of(update)]`, constructs the
deferred record, and confirms back to the operator with a TG reply.

### (3) API (`POST /api/deferred`)

Documented above. Available for programmatic use (n8n workflows, other scripts).

### (4) Kanban cards with deferred start — Non-goal

Cards do not get a `defer_until` field in this spec. This is a separate iteration.

---

## Phases

### Phase A — Core storage + loop + API + fire_on_reset (S/M: ~4–6 h)

**Scope:** Everything needed to fire a deferred run unattended. No UI, no TG `/later`.
Tested end-to-end via `POST /api/deferred` and logs.

Deliverables:
- New module `deferred.py` (or section in `webapp.py`): record schema, `_save_deferred`,
  `_load_deferred`, `_deferred_loop`, `_execute_deferred`, `_notify_operator`.
- `data/deferred.json` created on first write (missing → treated as empty array).
- `_deferred_loop` started in `start()` alongside `_queue_drain_loop`.
- `POST /api/deferred`, `GET /api/deferred`, `DELETE /api/deferred/{id}` endpoints.
- Integration with existing `_get_cached_usage()` (reuse `api_usage` cache) for
  `fire_on_reset` resolution.
- Operator TG notifications at creation and at completion/failure.
- `DEFERRED_POLL_SEC`, `DEFERRED_MAX_ATTEMPTS`, `DEFERRED_FREE_THRESHOLD` env-configurable.

Acceptance (Phase A):
- `pytest -q` — all existing tests green.
- `POST /api/deferred` with `fire_at` 30 s in the future → run fires within `DEFERRED_POLL_SEC + 30 s`; `status == "fired"` in `GET /api/deferred`.
- `POST /api/deferred` with `fire_on_reset: true` when utilization is 0 (mock) → fires on first poll.
- `POST /api/deferred` with `fire_on_reset: true` when utilization is 0.95 (mock) and `resets_at` 2 min from now → does NOT fire until after `resets_at + jitter`.
- Busy project at fire time: run re-queued +5 min; after `DEFERRED_MAX_ATTEMPTS` → status `failed`, TG ping received.
- Restart: `data/deferred.json` loaded on startup; pending run fires after restart without re-queuing.
- `DELETE /api/deferred/{id}` on pending → cancelled; loop does not fire it.

### Phase B — TG `/later` + operator pings (S: ~2–3 h)

**Scope:** `/later` command; improved TG notifications (creation + result).

Deliverables:
- `cmd_later` function in `bot.py`; registered at bot.py:1259 (after `cmd_stop`).
- `_parse_time_spec(spec: str, tz="America/Los_Angeles") -> (fire_at: str | None, fire_on_reset: bool)` — handles `reset`, `HH:MM`, `Nh`/`Nm`, ISO string.
- TG confirmation message on `/later` includes: project name, fire trigger, prompt preview (80 chars).
- Completion/failure TG ping includes: project, prompt preview, result text / error.
- `pytz` or `zoneinfo` (stdlib Python 3.9+) used for LA timezone; no new dependency if
  `zoneinfo` available.

Acceptance (Phase B):
- `/later reset Audit the project` in a bound project topic → confirmation message sent;
  record created with `fire_on_reset: true`.
- `/later 2h Deploy to prod` → `fire_at` = now + 7200 s (UTC); confirmation message
  includes "in ~2 hours".
- `/later 14:30 …` at 08:00 LA → `fire_at` = today 14:30 LA in UTC.
- `/later 14:30 …` at 15:00 LA (time already passed) → `fire_at` = tomorrow 14:30 LA.
- `/later` with no arguments → usage message.
- `cmd_later` in an unbound topic (no project) → error reply.

### Phase C — Cockpit UI + Schedules integration (M: ~3–5 h)

**Scope:** Clock-icon modal in ChatTab; deferred records in Schedules tab with cancel.

Deliverables:
- `ChatTab.tsx`: ⏱ icon button beside Send; `DeferModal` component with "At time" /
  "After reset" tabs; calls `POST /api/deferred`; toast on success.
- `SchedulesTab.tsx`: deferred records shown with `source: "deferred"`; Cancel button
  calls `DELETE /api/deferred/{id}`.
- `GET /api/schedules` updated to merge `data/deferred.json` pending records into the
  normalised schedule list (client-side merge or server-side, implementation choice).
- `npm run build` — no type errors, no lint errors.

Acceptance (Phase C):
- Cockpit: type a prompt, click ⏱, select "After reset", submit → toast; `/api/deferred`
  returns the new record; Schedules tab shows it with `source: "deferred"`.
- Schedules tab: Cancel button on a pending deferred → `DELETE /api/deferred/{id}` →
  record disappears from Schedules tab on next refresh.
- Already-fired record: no Cancel button visible (or button disabled).
- `npm run build` passes.

---

## Test plan

All phases gate on `pytest -q` green (baseline: all existing tests passing).

### Phase A tests

- `test_deferred_create_fire_at` — `POST /api/deferred` with `fire_at` → 201, record in
  `GET /api/deferred` with `status: "pending"`.
- `test_deferred_create_fire_on_reset` — `POST /api/deferred` with `fire_on_reset: true` →
  201.
- `test_deferred_create_both_fields_rejected` — both `fire_at` and `fire_on_reset: true` → 400.
- `test_deferred_create_neither_field_rejected` — neither field → 400.
- `test_deferred_create_unknown_project_rejected` — unknown project → 400.
- `test_deferred_loop_fires_fire_at` — mock `time.time()` past `fire_at`; assert loop calls
  `_execute_deferred`.
- `test_deferred_loop_fire_on_reset_free_window` — mock `utilization=0.05` (below threshold);
  assert fires immediately.
- `test_deferred_loop_fire_on_reset_waits_for_resets_at` — mock `utilization=0.90`,
  `resets_at` = now + 120 s; assert NOT fired; advance time past `resets_at + 90`;
  assert fired.
- `test_deferred_loop_busy_requeues` — `running[k]` is truthy at fire time; assert
  `fire_at` pushed forward, `attempts` incremented.
- `test_deferred_loop_busy_max_attempts_fails` — `attempts == DEFERRED_MAX_ATTEMPTS`;
  assert `status == "failed"`, `_notify_operator` called.
- `test_deferred_cancel` — `DELETE /api/deferred/{id}` on pending → 200; loop does not fire.
- `test_deferred_cancel_already_fired_409` — `DELETE` on fired → 409.
- `test_deferred_survives_restart` — write `data/deferred.json` with pending record; call
  `_load_deferred()`; assert record present with `status: "pending"`.
- `test_deferred_atomic_write` — concurrent saves do not corrupt file (mock `os.replace`
  to verify tmp→final pattern).
- `test_deferred_id_format` — id starts with `"def-"`, followed by 8 hex chars.

### Phase B tests

- `test_parse_time_spec_reset` — `"reset"` → `(None, True)`.
- `test_parse_time_spec_hours` — `"2h"` → `fire_at` ≈ now + 7200 s, `fire_on_reset=False`.
- `test_parse_time_spec_minutes` — `"30m"` → `fire_at` ≈ now + 1800 s.
- `test_parse_time_spec_hhmm_future` — `"23:59"` when current LA time is 08:00 → same-day
  target in UTC.
- `test_parse_time_spec_hhmm_past` — `"07:00"` when current LA time is 08:00 → next-day.
- `test_parse_time_spec_invalid` — `"tomorrow"` → raises `ValueError` or returns None.
- `test_cmd_later_creates_record` — mock `POST /api/deferred`; assert called with correct
  `fire_on_reset: true` when spec is `"reset"`.
- `test_cmd_later_no_project_replies_error` — unbound topic → TG reply with error text,
  no record created.
- `test_cmd_later_no_args_replies_usage` — `/later` with no args → usage message.

### Phase C tests

- `test_schedules_includes_deferred` — `GET /api/schedules` → at least one record with
  `source: "deferred"` when `data/deferred.json` has a pending record.
- `test_deferred_cancel_via_schedules_delete` — `DELETE /api/deferred/{id}` → record
  removed from `GET /api/schedules` response.
- Frontend build: `npm run build` in `web/` → exit 0 (run in CI-equivalent environment).

---

## Risks

### Race: deferred fires while project is also enqueued by card queue drain

The card queue drain loop (`_queue_drain_loop`) and the deferred loop both check
`running[k]` before firing. They run concurrently. There is a TOCTOU window between the
check and the `running[k] = True` assignment.

**Mitigation:** Both loops must set `running[k] = True` **synchronously** (no `await`
between check and set) within the same asyncio iteration — exactly as the existing
`on_message` pattern (bot.py:912) does. Since asyncio is single-threaded, a synchronous
read-modify-write of `running[k]` between two `await` points is safe. `_deferred_loop`
must acquire a per-session asyncio `Lock` (same lock used by `_run_card` if one exists,
or a new `asyncio.Lock` stored in `ctx["deferred_locks"]`) to protect the check+set
pair. If no such lock exists today, add one for this feature.

### Drift of resets_at between queue time and fire time

The OAuth usage `resets_at` is cached for 60 s. At fire time the cached value may be
stale by up to 60 s. If the true reset already happened but the cache says "not yet",
the deferred run will wait an extra poll cycle (up to 30 s). This is acceptable; the
30–90 s jitter already bakes in tolerance.

If the true reset is actually 5 hours away (fresh window just started before the
operator queued), `utilization` will be near 0, and the `DEFERRED_FREE_THRESHOLD` check
fires it immediately — correct behaviour.

### Restart between fire and completion

If the service restarts after `status` is set to `"fired"` but before `_execute_deferred`
completes, the record is permanently `"fired"` with no completion notification. On
restart the deferred loop ignores `status != "pending"` records, so the run is not
re-attempted.

**Mitigation:** acceptable for Phase A. Phase B can add `status: "running"` as an
intermediate state; on startup, any `"running"` record is reset to `"pending"` (or to
`"failed"` with error `"interrupted by restart"` if `attempts >= max`).

### Rate-limit utilization unavailable (OAuth failure)

`_get_cached_usage()` falls back to `ctx["rate_limits"]` (passive SDK snapshot) if the
OAuth call fails. The passive snapshot provides `resets_at` but `utilization=None`
(CLAUDE.md gotcha). With `utilization=None`, the `DEFERRED_FREE_THRESHOLD` check cannot
fire early — the loop falls through to the `resets_at + jitter` path, which still works.
`fire_on_reset` deferreds are therefore robust to OAuth outages at the cost of not firing
early when the window happens to already be free.

### Timezone handling for `/later HH:MM`

The `zoneinfo` module (Python 3.9+ stdlib) handles DST correctly for
`America/Los_Angeles`. If the host Python is 3.8 (unlikely; project baseline is 3.10+),
fall back to `pytz` (already likely in venv via other deps; add to `requirements.txt` if
absent).

Always store and compare in UTC. Never store a naive datetime. The LA ↔ UTC conversion
happens once, at parse time in `_parse_time_spec`, and is not repeated.

### Prompt injection via `/later`

The prompt text in `/later <spec> <text>` is the operator's own text in a private
bot (ALLOWED_USERS enforced). No sanitisation beyond length limiting (max 4096 chars,
same as Telegram message limit) is required.

---

## Non-goals

- **Recurring deferreds / cron-style repeats.** A fired deferred is done; it does not
  re-arm. Use the Schedules / Claude Code jobs for repeating work.
- **Kanban cards with a deferred start.** Cards fire immediately when the queue drain
  loop picks them up. Adding `defer_until` to cards is a separate iteration.
- **Per-project rate-limit budgets.** The `fire_on_reset` check uses the global
  `five_hour` limit only. Per-limit-type targeting (e.g., wait for `seven_day_opus`
  specifically) is out of scope.
- **UI to browse or edit fired/failed history.** `GET /api/deferred` provides the full
  list; any UI beyond the Schedules tab integration is out of scope.
- **Auto-retry on model error.** If `_execute_deferred` encounters an SDK error
  (not a busy slot), the run is marked `failed`. Retry logic for transient model errors
  is handled by the existing `run_engine` retry behaviour, not by the deferred layer.

---

## Related

- [[spec-019-schedules-registry]] — deferred records appear as `source: "deferred"` in
  the Schedules registry. Phase C wires `DELETE /api/deferred/{id}` into the Schedules
  tab cancel action.
- [[spec-017-fable-orchestrator]] — deferred runs call `run_engine()` with the same
  agents kwargs as `_run_card`; they benefit from the Fable conductor automatically if
  the project's model is set to `fable`.
- [[spec-012-incidents-realtime-push]] — `failed` deferred runs emit a TG notification
  but do **not** create an err-card on the board (deferred failures are operator-initiated
  work, not system anomalies). This distinguishes them from cron/schedule failures
  (spec-019 Phase C), which do create err-cards.
- [[spec-015-oss-runtime]] — all new code, strings, log lines, and API responses in
  English. LA timezone interpretation is an instance-config concern (`OPERATOR_TZ` env,
  default `America/Los_Angeles`), not hardcoded.
- [[spec-014-oss-hardening]] — `DEFERRED_POLL_SEC`, `DEFERRED_MAX_ATTEMPTS`,
  `DEFERRED_FREE_THRESHOLD`, `OPERATOR_TZ` all via env with documented defaults. No
  hardcoded user paths; `data/deferred.json` relative to `DATA` dir from `ctx`.
