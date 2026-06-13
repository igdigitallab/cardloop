# spec-034 — Board-Centric Project OS

Status: [ ] Draft · [x] Ready · [ ] In Progress · [ ] Shipped

> Consolidating spec. Supersedes the ad-hoc "board" behaviour scattered across card-run
> code. Goal: make the kanban board the **single source of truth for what is happening**,
> maintained by the system — not by the agent remembering to update it.

## Context — the problem

Today the system has **two disconnected worlds**:

1. **Card world** — moving a card to In Progress runs `_run_card` → `run_engine` →
   `_move_card_after_run` auto-moves the card to Review/Failed. The board is authoritative
   here and the agent never touches `TASKS.md`. This works.
2. **Chat world** — Telegram (`run_agent`) and cockpit chat (`api_project_chat`). Here the
   board is **absent from the agent's context entirely**. The agent receives `CLAUDE.md`
   (SDK auto-load) + the SessionStart snapshot + `TELEGRAM_NUDGE` + the conductor prompt.
   `TASKS.md` is never mentioned.

Operator lives in the chat world ~90% of the time. Consequences, both reported as pain:

- **A task is not recorded before it is worked on.** The operator describes a problem, the
  agent immediately starts fixing it; nothing lands on the board.
- **Cards rot.** Work happens in chat, bypassing the cards. The backlog accumulates 30 items,
  half already done, because nothing reconciles board state against reality.

These are not agent laziness — in the world where the operator works, the board does not exist.

## Principle

> **The board is the single source of truth for "what is happening", and it is maintained by
> the system, not by the agent's goodwill.** Soft prompt rules in a long `CLAUDE.md` decay
> over a turn; the board must be kept honest by forcing functions the engine runs, not the
> model remembers.

Everything else (specs, `CLAUDE.md`, chat) orbits the board.

## Design — what this spec builds (Phase 1)

### L0. `board.py` — shared board module

Extract the pure board primitives currently in `webapp.py` into a new top-level module
`board.py`, imported by **both** `webapp.py` and `bot.py`. This gives all three entry paths
one source of truth for reading/writing the board.

- Move (or re-home) the pure functions: `_load_board`, `_save_board`, `_serialize_tasks`,
  `_board_payload`, `BOARD_COLUMNS`, `_CARD_RE`, `_new_card_id`, `_pop_card`,
  `_tasks_path`/`_done_path`, the board lock (`_get_board_lock`), and a new
  `board_summary(cwd) -> str` (see L1).
- `webapp.py` keeps working: re-export the moved names from `board` (e.g.
  `from board import _load_board, _save_board, ...`) so existing call sites and tests are
  unchanged. Do **not** rename anything that tests import — keep backward-compatible names.
- No behaviour change in this step. The ~950-test suite must stay green after the extraction
  alone, before any new feature is added.

### L1. Board-aware context injection (all turns, read-only)

In `run_engine` (bot.py), when `cwd` contains a `TASKS.md`, prepend a compact board snapshot
+ a short board protocol to the agent's context (append to `system_prompt["append"]`, after
the existing nudges). Applies uniformly to TG chat, cockpit chat, and card runs.

`board_summary(cwd)` returns a compact, token-cheap rendering of **open** cards only
(backlog / in_progress / review — not Failed, not Done), each as `- [<id>] <text>` grouped by
column, truncated to a sane cap (e.g. 40 cards / 4000 chars). Empty board → a one-line "board
is empty" marker.

The injected **board protocol** (fixed, ~4 lines, same for every project — the cockpit owns
the workflow rules, not each project's `CLAUDE.md`):

```
## Board protocol (this project has a kanban board — it is the source of truth)
- A new task/bug/request → it belongs on the board. For multi-step work, record a card first, then do it.
- The open cards below are the live state. Do not let work happen invisibly off the board.
- The cockpit reconciles the board after each turn — you do not need to hand-edit TASKS.md.
```

This is read-only and cannot corrupt anything; it is the keystone that makes the agent aware
the board exists. Card runs additionally keep their existing specific card pointer.

### L2. Post-turn reconciler (intake + close, one cheap pass)

After a **chat** turn completes (the `result` event of `run_engine` in `run_agent` and
`api_project_chat`), fire a background task (must NOT block the operator's reply):

`reconcile_board(cwd, name, user_msg, agent_summary, git_stat, board_state)`

One cheap **haiku** one-shot (no tools), given: the user's message, the agent's final reply,
`git diff --stat` of the turn, and the current open cards. It returns a small JSON list of
board operations:

- `create` — a new card (title + optional description + target column) for work that was done
  or requested but has no card. Default column `review` if the work is already complete this
  turn, else `backlog`.
- `move` — move an existing card id to `review`/`done`/`in_progress`.
- nothing — most turns (questions, chit-chat, follow-ups) produce zero ops.

Applied under the board lock via `board.py` primitives. **Safety rules** (this runs against a
live board, including Claude-Ops' own):

- **Conservative.** Only `create`/`move`; never delete a card. Completed work is moved to
  `review` (human confirms), not silently to `done`, unless a card maps 1:1 to a fresh commit
  with matching intent.
- **Dedupe.** Before `create`, the model is given the open cards and instructed to reuse an
  existing card (emit `move`) rather than duplicate. A post-filter drops `create` ops whose
  normalised title closely matches an existing open card.
- **Idempotent / bounded.** Cap ops per turn (e.g. ≤5). Ignore malformed JSON (no-op on parse
  failure). Skip entirely if `TASKS.md` does not exist.
- **Flagged.** `BOARD_RECONCILE=1` (default on). `BOARD_RECONCILE_MODEL` (default `haiku`).
  Setting it to `0` disables the pass with zero other behaviour change.
- **Logged.** Every applied op prints one audit line; nothing silent.

L1 (soft "record first" nudge) + L2 (hard end-of-turn guarantee) together satisfy the intent:
the agent is nudged to record up front, and the board provably reflects reality afterwards —
nothing is lost within the session.

## Decisions deferred to Phase 2 (documented now, not built this turn)

- **Spec-as-card-detail (sidecar, approach "b").** A card may carry a linked detail document
  (the "spec") stored as a sidecar keyed by card id, reusing the `data/runs/<id>.md` pattern
  rather than a parallel `specs/` taxonomy or bloating `TASKS.md` descriptions. The card's
  status IS the spec's status — one lifecycle. The deleted Specs tab is **not** restored as a
  separate island; a spec surfaces by clicking its card. Heavy vault/`cwd` spec split is
  retired for OSS portability.
- **Live activity on cards (frontend).** Show the current tool the agent is running directly
  on the in-progress card, plus card age / staleness markers, prominent Failed, and a project
  dashboard. This is the screenshot that sells the product; it touches only `web/` + one SSE
  endpoint and carries no engine risk, so it ships as its own slice.

## Non-goals

- No per-message pre-classifier (cost/latency on every inbound message). Reconciliation is one
  pass at end of turn.
- No change to the card-run lifecycle (`_move_card_after_run` stays).
- No auto-delete of cards, ever.
- No restoration of the standalone Specs tab.

## Acceptance criteria

1. `board.py` exists; `webapp.py` imports board primitives from it; full pytest suite green
   with no test rewrites (backward-compatible re-exports).
2. In a chat turn (TG or cockpit) on a project with `TASKS.md`, the agent's context contains
   the open-cards snapshot + board protocol. Verified by a unit test asserting the injected
   system-prompt append contains a known card id from a fixture board.
3. After a chat turn that completes a piece of work, the reconciler creates or moves the
   relevant card (mocked haiku in tests). A chat turn that is a pure question creates/moves
   nothing.
4. Reconciler never blocks the reply, never deletes a card, caps ops, and is a no-op when
   `BOARD_RECONCILE=0` or `TASKS.md` is absent.
5. No regression to card auto-run.

## Edge cases / safety

- **Live self-host.** Claude-Ops manages its own board; a misfiring reconciler must not
  scramble it → conservative writes, dedupe, cap, flag, audit lines.
- **Concurrency.** All board writes go through the existing per-cwd board lock; respect the
  existing data-loss guard (skip write if parsed card count dropped).
- **Token cost.** Snapshot is open-cards-only and capped. Reconcile is one haiku call per chat
  turn, in the background, flag-disableable.
- **Restart sensitivity.** bot.py changes require a service restart to take effect; because the
  bot runs inside its own cgroup, activate only via `restart-self.sh`, as the final action of a
  turn, after tests are green.
