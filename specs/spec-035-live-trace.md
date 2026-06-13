# spec-035 — Live Trace: multi-viewer, reconnect replay, sub-agent visibility

Status: [ ] Draft · [x] Ready · [ ] In Progress · [ ] Shipped

> A turn's live trace is **server-authoritative and replayable**. Any client that connects
> mid-turn — a page refresh, a phone, a second browser — reconstructs the full current state
> and continues live, including what each **sub-agent** is doing inside the session. Fixes the
> reset timer and the "history disappeared after refresh" loss. Consolidates board card
> `847153` (see "what another agent is doing inside the session").

## Working agreement for implementing agents (read first)

This spec is built by sub-agents under a conductor. **Think while you implement.** As you
touch the code:

- If you find a **materially better design** than what is written here, stop and surface it to
  the conductor *before* building it — do not silently follow the spec if it is wrong, and do
  not silently deviate from it either. Name the trade-off in one or two sentences.
- Surface **risks, smells, and concerns** as you go (a fragile assumption, a test that only
  appears to pass, a cheaper approach). One line each; keep moving.
- **Stay disciplined.** Advising ≠ scope creep. Flag adjacent improvements as notes for new
  cards; do not gold-plate, do not expand the surface, do not refactor unrelated code. The
  acceptance criteria below define "done".

The point is judgement, not obedience and not free-wheeling.

## Context — the problem

The cockpit chat turn is the one rendering path that is **point-to-point and ephemeral**,
unlike the resilient bus + `EventSource` everything else uses.

- `api_project_chat` (`webapp.py:6537`) streams each engine event **only into the single POST
  connection** via `_send` → `resp.write` (`webapp.py:6605`). On disconnect it logs
  `task continues in background` (`webapp.py:6616`) and **drops** every further event — no one
  receives them.
- The shared bus exists — `_bus_publish` / `_bus_subscribe`, fanned out over
  `/api/projects/{id}/activity-stream` and `/api/activity-stream`
  (`webapp.py:4519`, `:4531`) — but the **web chat path never publishes per-event to it**.
  Only the Telegram consumer publishes (and only `subagent` events, `bot.py:1727`). So a
  second viewer sees, at most, a "running" badge, never the live tool/text stream.
- There is **no replay buffer**: a reconnecting client sees events only from the moment it
  reconnects; the gap is invisible.
- The timer is **client-local**: `startTs = Date.now()` is stamped when the POST begins
  (`ChatTab.tsx:534`), and on reconnect the running-poll re-stamps `startedAt: Date.now()`
  (`ChatTab.tsx:399`). There is no server "turn started at" timestamp.

Reported symptoms, all the same root cause:

1. **Counter restarts** on refresh (client re-stamps start).
2. **History disappears** — already-streamed tool calls (incl. sub-agent commands) are not
   replayed; only the running indicator returns.
3. **Sub-agent activity is opaque** in the cockpit (card `847153`): the operator orchestrates
   from chat but cannot see what the spawned agents are doing inside the session.

The engine already produces everything needed: `run_engine` yields `subagent` events carrying
`last_tool_name`, `description`, `subtype` (`bot.py:1358–1390`), plus `result` events with
cost/token metrics (`bot.py:1338–1356`). The data is there; the web delivery path loses it.

## Principle

> **The turn is the server's state, not the connection's.** The POST that starts a turn is an
> ingress event, not the rendering channel. Rendering comes from the bus, backed by a
> replayable per-turn buffer. Connect at any time, from any device → reconstruct and continue.

## Design — what this spec builds

### L0. `LiveTurn` — server-authoritative, replayable per-session buffer

A small in-memory object per `session_key`, owned next to `_bus` in `webapp.py`:

```
LiveTurn = {
  turn_id: str,            # new per turn
  started_at: float,       # wall clock (epoch) — the ONE timer source of truth
  model: str,
  status: "running" | "done" | "error",
  seq: int,                # monotonic event counter for this turn
  events: deque(maxlen=N), # each: {seq, ...event}  (N ~ 2000, ring)
  cost_usd: float | None,  # accumulated, updated on result events
}
```

- Created when a turn starts (alongside `running[session_key] = True`).
- Every event from `run_engine` is appended with an incremented `seq`.
- Retained for a short window after the turn ends (e.g. 5 min or until the next turn) so a
  just-finished turn is still replayable, then dropped. Bounded — the ring caps memory.
- This is the single source of truth for "what is happening / just happened" in a session.

### L1. Web chat path publishes every event to bus + buffer (parity with Telegram)

In `api_project_chat`'s `async for event in run_engine(...)` loop, for **each** event:

1. append to the session's `LiveTurn` (assign `seq`);
2. `_bus_publish(session_key, event_with_seq)` — fan-out to all subscribers;
3. keep the existing `_send` to the originating client (lowest-latency path; harmless
   duplicate since the originating client can dedupe by `seq`, or — preferred, see L4 — the
   originating client stops using the POST stream for rendering and reads the bus like everyone
   else).

This immediately gives multi-viewer: N clients on `/api/projects/{id}/activity-stream` see the
turn live, **including `subagent` events with `last_tool_name`** → card `847153` is satisfied
by data that already flows; it just needed to reach the web bus.

### L2. Reconnect replay via `Last-Event-ID`

Extend `_sse_stream` + `api_project_activity_stream` (`webapp.py:4482`, `:4519`):

- Emit each SSE frame with `id: <seq>` so the browser's `EventSource` tracks position.
- On connect, read the `Last-Event-ID` request header (browsers send it automatically on
  auto-reconnect) — or a `?since=<seq>` query param for manual/cold cases. Before going live,
  **replay** the `LiveTurn.events` with `seq > cursor`, then switch to the live queue.
- No cursor → replay nothing (current behaviour) unless the client used the snapshot (L3).

Result: refresh or dropped tunnel → the gap is replayed, no lost history.

### L3. Cold-open snapshot endpoint

`GET /api/projects/{id}/live` → from `LiveTurn`:

```
{ running, turn_id, started_at, model, cost_usd, cursor: <latest seq>, events: [...] }
```

A device opening fresh (phone, no `Last-Event-ID`) calls this **once** to paint the full
current turn, then subscribes to the activity-stream from `cursor`. Reuses the L0 buffer; no
separate storage. Replaces today's `projectRunning` poll (`ChatTab.tsx:384`) with a snapshot
that carries history + the real `started_at`.

### L4. Frontend — render the live turn from the bus; server-authoritative timer

In `web/src/tabs/ChatTab.tsx` (and the shared `hooks/useProjectActivity.tsx`):

- **Render the in-flight turn from the `EventSource` subscription**, the same resilient path
  `App.tsx` already uses (`App.tsx:291`), not solely from the POST `ReadableStream`
  (`ChatTab.tsx:545`). The POST still *starts* the turn; if its stream dies, the bus keeps the
  view alive. (Preferred end state: POST kicks the turn and returns; all rendering is the bus —
  one code path. Acceptable Phase-1: keep POST stream for the originating tab, bus for the
  rest, dedupe by `seq`.)
- **On tab open / project switch / mount:** call `/live`; if `running`, hydrate the transcript
  from `events`, set the timer from `started_at`, subscribe from `cursor`.
- **Timer = `now − started_at(server)`** — remove the client `Date.now()` start
  (`ChatTab.tsx:534`) and the reconnect re-stamp (`ChatTab.tsx:399`) as the elapsed source.
  Fixes the reset.
- **Sub-agent lane:** render `subagent` events as a nested, indented lane under the turn —
  `⚙ <description>` with a live `↳ [<last_tool_name>]` and a terminal `✓/✗`. Mirrors the
  Telegram rendering (`bot.py:1703–1727`) so the operator sees, in the cockpit, what each agent
  is doing inside the session (card `847153`).

## Relationship to other specs / cards

- **Consolidates card `847153`** (sub-agent visibility) — same surface, built here.
- **Substrate for spec-034 Phase 2 "Live activity on cards."** The `LiveTurn` buffer + SSE
  replay this spec builds is exactly the feed an in-progress card's live-tool indicator will
  consume. Build the substrate once, here; the on-card rendering rides it later.
- **Adjacent, not in scope: card `66055d`** — the message queue also vanishes on refresh (same
  root: client-only state). If cheap, expose queue contents in the `/live` snapshot so it
  survives reload; the *edit/delete queued message* feature stays its own card.

## Non-goals

- **Surviving a bot restart.** The buffer is in-memory and dies with the process; the durable
  record remains the SDK transcript. Restart-survival of the live buffer is a future spec if
  ever needed — out of scope.
- **No new transport.** Stay on SSE + the existing bus; no WebSockets.
- **No change to the run lock.** A second client still cannot POST a new prompt into a busy
  session (`webapp.py:6575` returns `project busy`) — by design; the second client *views*.
- **No change to engine event shapes** or the card-run lifecycle.

## Acceptance criteria

1. During a cockpit chat turn, a **second** browser tab opened on the same project shows the
   live trace (text + tool calls + sub-agent lane) without having issued the POST. Verified by
   a test subscribing to `/api/projects/{id}/activity-stream` and asserting it receives the
   same `seq`-tagged events the engine yielded.
2. **Refresh mid-turn** loses no history: after reconnect with `Last-Event-ID`, the client has
   every event of the turn (replayed gap + live), asserted by `seq` continuity with no holes.
3. **Timer is continuous** across refresh: elapsed derives from server `started_at`; a test on
   `/live` confirms `started_at` is stable across repeated calls within one turn.
4. **Cold open** (`GET /api/projects/{id}/live` with no prior connection) returns the full
   current-turn `events`, `started_at`, `cost_usd`, and a `cursor` usable to subscribe without
   duplication or gap.
5. **Sub-agent visibility:** a turn that spawns a sub-agent surfaces `subagent`
   `started`/`progress`(`last_tool_name`)/`notification` events on the web bus, and the
   frontend renders them as a nested lane (component/render test).
6. Full pytest suite green; no rewrite of existing tests (additive endpoints/buffer).
7. `BOARD_*` and all spec-034 behaviour unaffected.

## Edge cases / safety

- **Bus backpressure.** `_bus_publish` already drops on a full subscriber queue
  (`webapp.py:166`) — that is fine: the `LiveTurn` buffer + `Last-Event-ID` replay is the
  catch-up mechanism, so a momentarily-full live queue self-heals on the next replay.
- **Buffer bound.** `events` is a `deque(maxlen=N)`; an extremely long turn evicts its oldest
  events — replay then starts from the oldest retained `seq` (document this; N sized for normal
  turns). Memory is capped per session.
- **Turn-end retention.** Keep the finished turn briefly so a client reconnecting right at the
  end still replays it; then drop to free memory.
- **Multiple viewers + originating tab.** Dedupe by `seq` so the originating tab (which may
  also receive via `_send`) does not double-render.
- **Live self-host.** Claude-Ops streams its own turns; the buffer is read-only state, no board
  writes, no engine coupling — cannot corrupt anything.
- **Restart sensitivity.** `bot.py` / `webapp.py` changes need a service restart to take
  effect; the bot runs in its own cgroup, so activate only via `restart-self.sh` as the final
  action of a turn, after tests are green.
