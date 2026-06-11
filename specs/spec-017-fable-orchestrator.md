---
created: 2026-06-10
updated: 2026-06-11
status: implemented (all phases)
---

# Spec 017 — Fable 5 as orchestrator: conductor + executor sub-agents

## Goal

Make `claude-fable-5` the default model for the main session in every project/topic.
Fable acts as a **conductor**: it plans and delegates heavy execution to sub-agents
(`sonnet` / `haiku`) via the SDK `Task` tool, passing self-contained task descriptions
("waterfall context"). Fable never runs the long work itself — it directs and synthesizes.

## Context / Motivation

### Why Fable as conductor

`claude-fable-5` is a frontier reasoning model optimised for orchestration and synthesis.
On Claude's subscription tier it costs nothing beyond the seat, just as `opus`/`sonnet`/`haiku`
do today. On API billing the pricing asymmetry ($10 input / $50 output per M tokens vs
$3/$15 for sonnet-4) makes Fable best used for short, high-signal turns:
planning, reviewing, synthesising — not generating thousands of tokens of code or log output.

The conductor pattern matches that profile exactly:
- Main session (short turns, decisions, summaries) → Fable
- Executor sub-agents (long code/infra runs, read-only research) → Sonnet / Haiku

On subscription auth (default for this project) the cost argument is moot but the
quality argument still holds: Fable reasons better; executors run autonomously and
don't waste Fable's context with tool noise.

### Why "waterfall context" for sub-agents

A sub-agent spawned via `Task` starts a fresh Claude session. Its context is only the
agent's system prompt (`AgentDefinition.prompt`) plus the task brief the conductor passes
as the Task tool input — never the main session history. This is the right isolation
boundary: the executor gets a self-contained brief, not a 100k-token chat transcript.

### Current gaps (verified 2026-06-10)

1. `"fable"` is not in `MODELS` (bot.py:86) and not in `_ALLOWED_MODELS` (webapp.py:4057).
2. `DEFAULT_MODEL` defaults to `"opus"` (bot.py:81 / .env.example line 22).
3. `run_engine()` (bot.py:417) does not accept an `agents` parameter; sub-agents cannot
   be defined per-call.
4. Sub-agent lifecycle messages (`TaskStartedMessage`, `TaskProgressMessage`,
   `TaskNotificationMessage` — all subclasses of `SystemMessage`) are silently discarded
   at bot.py:495–496 (`pass`). They never reach the cockpit live-stream or Telegram.
5. No conductor system-prompt: Fable sessions have no explicit orchestrator directive.
6. `ClaudeAgentOptions` already has an `agents` field (`dict[str, AgentDefinition] | None`);
   it is simply not wired up yet.

---

## Design

### Conductor model: `fable`

`claude-fable-5` is added as a first-class model alias alongside `opus`/`sonnet`/`haiku`.
It becomes the default. Users can switch any project to another model via Settings UI or
`/model sonnet` — the fable default is just a starting point.

### Sub-agents defined in `run_engine()`

`run_engine()` gains a new optional parameter `agents: dict[str, AgentDefinition] | None`.
When provided it is passed straight to `ClaudeAgentOptions(agents=...)`. The SDK then makes
those agents available to Fable via the `Task` tool.

Default agent roster (global, bot.py constants — per-project override is Phase C):

| Name | Model | Role | permissionMode | disallowedTools |
|---|---|---|---|---|
| `executor` | `sonnet` | General code / infra execution, writes files, runs bash | `bypassPermissions` | — |
| `researcher` | `sonnet` | Read-only research: web, file reads, grep; no writes | `bypassPermissions` | `["Write", "Edit", "NotebookEdit"]` |
| `quick` | `haiku` | Fast lookups, simple transforms, cheap questions | `bypassPermissions` | — |

All three inherit the project `cwd` from the parent session automatically via the SDK.

`researcher` keeps Bash available for read-only commands (grep/find/git log etc.).
Caveat: Bash is not truly read-only — the agent could still mutate state through shell
commands. That residual risk is acceptable under the project's full-auto
`bypassPermissions` contract; `disallowedTools` blocks the structured write paths, which
is the practical guardrail here.

### Conductor system-prompt injection

When the resolved model starts with `"fable"` (i.e. `resolved_model.startswith("fable")`),
`run_engine()` appends an orchestrator directive to the `system_prompt.append` string:

```
You are an orchestrator. Delegate substantial execution to sub-agents via the Task tool —
pass them a self-contained brief (no chat history; just what they need). Reserve your own
turns for planning, decision-making, and synthesising results. Do not run long code
sequences or file-editing loops yourself.
```

This is appended AFTER the existing TELEGRAM_NUDGE (or cockpit's `system_prompt.append`)
so it does not override any transport-specific instructions.

### Sub-agent event forwarding

`TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage` are currently
swallowed. They must be yielded as a new event type `"subagent"` so the cockpit
live-stream and TG adapter can display sub-agent activity.

Proposed event schema:
```json
{
  "type": "subagent",
  "subtype": "started" | "progress" | "notification",
  "task_id": "...",
  "description": "...",
  "status": null | "completed" | "failed" | "stopped",
  "summary": null | "...",
  "last_tool_name": null | "..."
}
```

TG adapter: show a one-liner status update (e.g. `⚙ executor started: <description>`,
`✓ executor done: <summary>`). Cockpit SSE stream: forward as-is for future UI work.

---

## Phases

### Phase A — Model plumbing (S: small, ~1–2 h)

**Scope:** Add `"fable"` alias; swap default; no behaviour change otherwise.

Changes:
- `bot.py:86` — add `"fable": "fable"` to `MODELS`.
- `bot.py:81` — change env default: `os.environ.get("DEFAULT_MODEL", "fable")`.
- `webapp.py:4057` — add `"fable"` to `_ALLOWED_MODELS`.
- `.env.example` — update `DEFAULT_MODEL=fable` with comment explaining conductor pattern.
- No change to `run_engine()`, no new parameters.

Acceptance (Phase A):
- `pytest -q` — all existing tests green (748 baseline).
- `PUT /api/projects/{id}/model` with body `{"model":"fable"}` → 200, stored.
- `POST /api/projects/{id}/settings` with `{"model":"fable"}` → 200, stored.
- Bot `/model fable` command → accepted, stored in topics.json.
- `GET /api/projects/{id}/settings` returns `"model":"fable"` after setting it.
- Default topic model for new topics is `"fable"` (env default).

### Phase B — Agents param + sub-agent event forwarding (M: medium, ~3–5 h)

**Scope:** Wire `agents` into `run_engine()`; yield sub-agent lifecycle events; add conductor
system-prompt injection for fable model; add default agent roster as constants.

Changes:
- `bot.py` — import `AgentDefinition`, `TaskStartedMessage`, `TaskProgressMessage`,
  `TaskNotificationMessage` from `claude_agent_sdk`.
- `bot.py` — define `DEFAULT_AGENTS: dict[str, AgentDefinition]` constant with the three
  agents (`executor`, `researcher`, `quick`). Configurable via env
  `EXECUTOR_MODEL` / `RESEARCHER_MODEL` / `QUICK_MODEL` (default `sonnet`/`sonnet`/`haiku`).
  `researcher` gets `disallowedTools=["Write", "Edit", "NotebookEdit"]` (read-only by
  construction; Bash stays available — see roster caveat).
- `run_engine()` signature — add `agents: dict[str, AgentDefinition] | None = None`.
- `ClaudeAgentOptions(...)` — add `agents=agents`.
- `run_engine()` — when `resolved_model.startswith("fable")` append orchestrator
  directive to `system_prompt["append"]`.
- `run_engine()` event loop — replace `pass` at `isinstance(msg, SystemMessage)` with:
  - if `isinstance(msg, TaskStartedMessage)` → yield `subagent` started event
  - elif `isinstance(msg, TaskProgressMessage)` → yield `subagent` progress event
  - elif `isinstance(msg, TaskNotificationMessage)` → yield `subagent` notification event
  - else → `pass` (all other SystemMessage subtypes remain silent)
- `run_agent()` (TG adapter) — handle `event["type"] == "subagent"`: on `started` update
  status message; on `notification` (completed/failed) append one-liner to reply.
- `webapp.py` `api_project_chat` SSE stream — forward `subagent` events as `data:` SSE
  frames (same pattern as `tool` / `text` events). Cockpit UI display: Phase C.
- `ctx` — expose `DEFAULT_AGENTS` via ctx so webapp can reference it if needed.

Acceptance (Phase B):
- `pytest -q` — all tests green.
- Running a task with `DEFAULT_MODEL=fable` in a project that has code: Fable session
  starts, delegates to `executor` sub-agent via Task tool, sub-agent events appear in
  Telegram status message and in cockpit SSE stream.
- `TaskStartedMessage` → `subagent.started` event reaches TG adapter (test: mock SDK).
- `TaskNotificationMessage` status=`completed` → summary line appended to TG reply.
- Non-fable session (model=sonnet): `agents` still populated (no regression; sonnet can
  also delegate if it chooses to — this is allowed, not forced).

### Phase C — Settings UI + per-project agent config (M: medium, ~3–5 h)

**Scope:** Expose model-per-agent config in project Settings tab. Let operators disable
the conductor prompt per project. Future work (separate spec) for team-level agent skill
assignment.

Changes:
- `data/topics.json` schema — add optional `agents_config: {executor_model?, researcher_model?, quick_model?, conductor_prompt: bool}`.
- `webapp.py` `_project_settings_view` — include `agents_config` in serialised view.
- `webapp.py` `api_project_settings_post` — accept `agents_config` partial update; validate
  model names against `_ALLOWED_MODELS`; validate `conductor_prompt` is bool.
- `run_engine()` — accept `agents_config` kwarg (from webapp's `_run_card` /
  `api_project_chat`); build per-project `agents` dict from it; honour
  `conductor_prompt: false` to skip the orchestrator directive even for fable.
- Frontend `SettingsTab.tsx` — add "Sub-agents" section: dropdowns for
  executor/researcher/quick model; toggle for conductor prompt.
- `npm run build` — no type errors, no lint errors.
- `bot.py` `run_agent()` — pass project's `agents_config` from `topics[k]` into
  `run_engine()`.

Acceptance (Phase C):
- `pytest -q` — all tests green; add unit test for `agents_config` partial update endpoint.
- Settings UI: change executor model to `haiku` → saved → next task uses `haiku` for executor.
- Toggle conductor prompt off → fable session no longer injects orchestrator directive.
- Invalid model in `agents_config` → 400 with clear error message.

---

## Test plan

All phases require `pytest -q` green as a gate before commit. Baseline: 748 tests passing.

### Phase A tests
- `test_model_fable_accepted_by_settings` — POST settings with model=fable → 200.
- `test_model_fable_accepted_by_put_model` — PUT /model fable → 200.
- `test_default_model_is_fable` — `DEFAULT_MODEL` env unset → resolved model starts with "fable".
- `test_model_fable_in_models_dict` — `"fable" in MODELS` is True.
- `test_allowed_models_includes_fable` — `"fable" in _ALLOWED_MODELS` is True.

### Phase B tests
- `test_run_engine_yields_subagent_started` — mock SDK to emit `TaskStartedMessage`;
  assert `run_engine` yields `{"type":"subagent","subtype":"started",...}`.
- `test_run_engine_yields_subagent_notification` — mock `TaskNotificationMessage` status=completed;
  assert yield with `status="completed"` and `summary`.
- `test_run_engine_passes_agents_to_opts` — assert `ClaudeAgentOptions` receives
  `agents` kwarg when `run_engine(agents=...)` called.
- `test_conductor_prompt_injected_for_fable` — model=fable → `system_prompt["append"]`
  contains orchestrator directive.
- `test_conductor_prompt_not_injected_for_sonnet` — model=sonnet → no orchestrator directive.
- `test_non_task_system_messages_still_silenced` — other `SystemMessage` subtypes yield nothing.

### Phase C tests
- `test_agents_config_partial_update` — PATCH settings with `agents_config:{executor_model:"haiku"}` → 200, persisted.
- `test_agents_config_invalid_model_rejected` — executor_model="gpt-4" → 400.
- `test_agents_config_conductor_prompt_toggle` — `conductor_prompt:false` stored and honoured.

---

## Risks

### Fable alias availability on subscription auth
The alias `"fable"` must be recognised by the `claude` CLI binary. The SDK resolves model
strings by passing them to the CLI; if the alias is not yet in the installed CLI version,
sessions will fail with a model-not-found error. **Mitigation:** check `claude --version`
and test `claude -m fable -p "hi" --output-format json` in the shell before merging Phase A.
If unavailable, keep `DEFAULT_MODEL=opus` as fallback in `.env.example` and document the
minimum CLI version in `.env.example`.

### Fallback if fable unavailable at runtime
Add `fallback_model` in `ClaudeAgentOptions` — the SDK already supports this field
(`fallback_model: str | None`). Set `fallback_model="opus"` when model starts with `"fable"`.
This gives silent graceful degradation if the alias fails.

### Sub-agent events may be high-volume
A busy executor sub-agent emits `TaskProgressMessage` on every tool use. For long infra runs
this could be tens of events per minute. **Mitigation:** in Phase B, TG adapter only renders
`started` + `notification` (terminal states). Progress events are forwarded to SSE only, not
to Telegram. Add a per-session counter; suppress progress events beyond `MAX_SUBAGENT_PROGRESS`
(default 10) per task to prevent TG flood.

### Restart discipline
`bot.py` and `webapp.py` changes require a bot restart (`bash restart-self.sh`). The
orchestrator directive is injected at runtime (not cached), so no session migration needed.
Existing open sessions on the old model continue until their natural end; new sessions pick
up the new default.

### Backward compatibility (API + topics.json)
`agents_config` is additive and optional in topics.json; absent = use defaults. No migration
needed. `_ALLOWED_MODELS` is a set — adding `"fable"` cannot break existing validation.
All three new `run_engine()` params (`agents`, `agents_config`) are keyword-only with
`None` defaults; callers that omit them are unaffected.

---

## Non-goals

- Per-agent skill/tool whitelisting beyond what `AgentDefinition.tools` and
  `AgentDefinition.disallowedTools` already support — future spec.
- Custom agent definitions stored outside `topics.json` (YAML/JSON sidecar) — Phase C is enough.
- UI showing sub-agent trees / task graphs in the cockpit board — future spec.
- Billing/cost tracking per sub-agent (subscription auth = no cost signal).
- Changing `permissionMode` from `bypassPermissions` for sub-agents — out of scope;
  this is the project's full-auto contract.

---

## Related

- [[spec-015-oss-runtime]] — English-only; all new strings here are English.
- [[spec-014-oss-hardening]] — no new hardcoded paths; executor/researcher model names are env-configurable.
- [[spec-010-self-healing]] — removed from the project (2026-06-10); self-heal is no longer
  a consumer of `run_engine()`, so no conductor interaction to account for.
