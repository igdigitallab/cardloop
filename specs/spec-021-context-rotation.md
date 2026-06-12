---
created: 2026-06-11
updated: 2026-06-11
status: in-progress
phases_shipped: 1, 2, 3
card: ops:spec021
---

# Spec 021 — Context Rotation + Fresh Card Sessions + UI Context Indicator

## Goal

Prevent long-running Claude sessions from accumulating context bloat by automatically
rotating the session at a configurable token threshold, giving cards their own isolated
sessions, and surfacing the context size in the cockpit UI with a manual reset button.

---

## Context / Motivation

The Claude Agent SDK session is a single JSONL transcript that grows with every turn.
At 60K+ tokens the model starts to struggle (slower, more distracted). Three problems
follow from the current design:

1. **Chat sessions grow unbounded.** A long project session accumulates tool calls,
   text, and assistant replies. At some point the context is so large it hurts quality.
2. **Cards share the chat session.** `_run_card` resumes the same session as the chat,
   meaning card context bleeds into chat history and vice versa.
3. **Operators have no visibility into context size** until something goes wrong.

---

## Design

### Part 1 — Auto session rotation with handoff summary

When a `result` event carries `context_tokens > CONTEXT_ROTATE_AT`:
- Run haiku to summarise the current session (≤500 words, dense, English).
- Save the summary to `<cwd>/.claude-ops/memory/session-handoff.md` with YAML frontmatter.
- Clear `sessions[session_key]` and flush to disk — the next turn starts fresh.
- Send an SSE `rotation` event to the cockpit and a TG notification.
- Protection: once-per-turn flag (`_rotated_this_turn`), skip if card queue is draining,
  try/except so rotation failure never breaks the main result delivery.

Toggle: `CONTEXT_ROTATION=1` (default on) env var. Threshold: `CONTEXT_ROTATE_AT=60000` tokens.

### Part 1b — TG-channel hook

The same rotation applies to the Telegram path: `bot.py` `_maybe_rotate_tg(...)`, called
once at the end of `run_agent` (after the final reply and the auto-resume check), using
`context_tokens` from the captured final result event.

- **Wiring choice:** a direct `webapp._do_session_rotation(...)` call from bot.py —
  consistent with the existing `webapp._maybe_auto_resume` precedent. The CLAUDE.md
  constraint only forbids the reverse direction (webapp.py must not import bot.py);
  bot.py already imports webapp. No ctx indirection or shared module needed.
- **Guards:** same global toggle + threshold as the web path, plus a TG-queue-drain
  guard — rotation is skipped while `_TG_QUEUE[k]` has pending queued messages and
  triggers after the last drained turn instead. The hook runs exactly once per turn,
  so no once-per-turn flag is needed.
- **Notification:** sent via bot.py `send()` directly into the bound chat/thread
  (`_do_session_rotation`'s ctx gets no `ptb_app`, and `_notify_tg_rotation` is not
  called — exactly one notification).
- Whole hook is try/except-guarded — rotation failure never breaks the TG turn.

### Part 2 — Fresh session per card + cwd-lock

- `_run_card` always starts with `resume_sid = None` — each card is a standalone run.
- Card session_id is NOT written back to `ctx["sessions"]` — shared session is never
  polluted by card runs.
- A `ctx["cwd_locks"]` dict prevents two simultaneous runs (different session_keys)
  targeting the same working directory. The lock is set/released in `_run_card`'s try/finally.

### Part 3 — Context counter in UI

- ChatTab shows `{N}K` context indicator next to the model selector.
- Yellow (`text-yellow-500`) when tokens > 40 000; red (`text-red-500`) when > 60 000.
- Tooltip when red: "Heavy context — consider wrap & reset".
- "♻ Wrap & reset" button appears when red; calls `POST /api/projects/{id}/rotate`,
  then resets the counter to 0 and shows a brief inline notification.

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Auto session rotation backend (`_do_session_rotation`, `api_project_rotate`) | shipped |
| 2 | Fresh card sessions + cwd-lock in `_run_card` | shipped |
| 3 | UI context indicator + rotate button in ChatTab | shipped |

---

## Acceptance

- [ ] With `CONTEXT_ROTATION=1` (default), a run returning 70K tokens triggers rotation SSE event.
- [ ] With `CONTEXT_ROTATION=0`, no rotation occurs even above threshold.
- [ ] After rotation, `sessions[key]` is cleared.
- [ ] Handoff file written to `<cwd>/.claude-ops/memory/session-handoff.md`.
- [ ] Rotation failure (haiku exception) does not prevent the `result` SSE event from reaching the client.
- [ ] `POST /api/projects/{id}/rotate` with no active session → `{"rotated": false}`.
- [ ] `POST /api/projects/{id}/rotate` while project busy → 409.
- [ ] `_run_card` always starts with `resume_session_id=None`.
- [ ] After `_run_card`, `ctx["sessions"]` is unchanged.
- [ ] Two simultaneous `_run_card` calls on same cwd: second is blocked by cwd-lock.
- [ ] After `_run_card` finishes, cwd-lock for that path is released.
- [ ] ChatTab shows yellow badge at 40K tokens, red at 60K.
- [ ] "♻ Wrap & reset" button visible at 60K+ tokens.

---

## Tests

`tests/test_context_rotation.py` — 16 tests covering:
- Rotation triggered/not-triggered by threshold
- TG-path hook (`_maybe_rotate_tg`): triggered above threshold, skipped below,
  skipped while `_TG_QUEUE` non-empty
- Toggle off
- Session cleared after rotation
- Handoff file written
- Rotation failure does not break main run
- Rotate endpoint: no session / busy / success
- Card uses fresh session
- Card does not write session back
- cwd-lock blocks concurrent card
- cwd-lock released after finish

---

## Risks

- **Haiku summarisation cost.** One short run per rotation; token count is bounded by the
  session size at rotation time. Acceptable for subscription auth.
- **Rotation race with card queue.** Mitigated by the queue-drain guard: if `_QUEUE[session_key]`
  is non-empty, rotation is deferred until next `result` event.
- **Handoff file grows.** Each rotation overwrites the same `session-handoff.md` file —
  no accumulation.

---

## Non-goals

- Per-card memory accumulation (each card is fully fresh, no handoff carried to next card).
- UI to browse rotation history.

---

## Related

- Spec 006 — Project memory (handoff file uses same `.claude-ops/memory/` directory)
- Spec 017 — Fable orchestrator (context management affects orchestrator quality)
- Spec 020 — Deferred runs (deferred runs benefit from rotation before re-firing)
