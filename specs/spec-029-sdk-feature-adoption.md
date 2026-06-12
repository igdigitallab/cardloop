---
created: 2026-06-12
status: draft
phases_shipped: none
card: ops:spec029
---

# Spec 029 — SDK Feature Adoption (claude-agent-sdk 0.2.96)

## Goal

Adopt unused `claude-agent-sdk` 0.2.96 capabilities that improve ClaudeOps observability, UX,
and robustness. Separate from the cost work (spec-027/028) — these are features. Source: full
SDK source sweep. Each item below is independent; pick by payoff.

## Top 3 (adopt first)

1. **Live streaming — `include_partial_messages: bool` (`types.py:1776`).** Emits `StreamEvent`
   partials during token generation. `run_engine` already yields `{type:text}` chunks — forward
   `StreamEvent.event["delta"]["text"]` into the existing SSE channel for character-by-character
   display. Today the cockpit shows nothing until a full `TextBlock` lands. **Effort low, payoff
   high.**

2. **Audit/timeline enrichment — `PostToolUse` hook (`types.py:317`) + `include_hook_events`
   (`types.py:1782`).** `PostToolUse` (Python callback) receives tool name + full `tool_response`
   → write actual Bash stdout / edit diffs into `data/audit/` (today audit logs only the command,
   never the output — debugging failed card runs is painful). `include_hook_events=True` puts hook
   lifecycle (incl. what `guard-self-lifecycle` blocked) + timing into the stream → richer
   `data/timeline/`. Additive to options; no shell-script changes. **Effort low, payoff high.**

3. **Deterministic cards — `output_format` (`types.py:1889`) + `ResultMessage.structured_output`.**
   Card runner requests a JSON Schema (`{title,status,changes,summary}`) instead of prose TASKS.md
   → kills the fragile board parser (`_CARD_RE`/`_PLAIN_CARD_RE`/count-guard — the "board stirs
   tasks" gotcha class). **Effort med, payoff high.**

## Worth it (second wave)

| SDK API | Use-case | Effort/payoff |
|---|---|---|
| `can_use_tool` callback (`types.py:1748`) + `PermissionResultDeny.message` | Replace shell `guard-self-lifecycle.sh` with a Python pre-tool callback: audit Bash w/ full context, dynamic deny patterns, cleaner deny reasons in UI | med / high |
| `SubagentStart` hook (`types.py:379`) | Per-subagent lifecycle + cost breakdown in timeline (`agent_id`/`agent_type`) | low / med |
| `effort` per `AgentDefinition` (`types.py:100`) + `thinking: ThinkingConfig` (`types.py:1861`) | Subagents at `low`/`medium`, `thinking=disabled` for haiku — ties to spec-027 Part 6/7 cost levers | low / med |
| `AgentDefinition.tools` minimal set (`types.py:89`) | Lean subagents — ties to spec-027 Part 7 (biggest orchestration lever) | low / high |
| `add_dirs` (`types.py:1716`) | Inject `~/vault/` or shared spec dir per-session without changing cwd | low / med |
| `settings: str` (`types.py:1708`) | Per-project `.claude-ops/settings.json` permission override w/o touching `~/.claude/settings.json` | low / med |
| `enable_file_checkpointing` + `client.rewind_files()` (`types.py:1897`) | Worktree discard via file-level undo instead of `git branch -D` | med / med |
| `toggle_mcp_server` / `get_mcp_status` (`client.py:402,424`) | Cockpit MCP health + operator reconnect (needs spec-028 persistent client) | med / med |
| `stop_task(task_id)` (`client.py:450`) | Cockpit "stop one subagent" vs whole-session `/stop` | med / med |
| `UserPromptSubmit` / `Notification` hooks (`types.py:338,370`) | Prompt audit/sanitize; mirror Claude Code notifications to TG | low / low-med |
| `stderr: Callable` (`types.py:1742`) | Route CLI stderr (currently dropped) to project log for crash diagnostics | low / low |

## Deprecated / risky in 0.2.96 (avoid)

- `debug_stderr` — no longer read; use `stderr=` callback.
- `max_thinking_tokens` — deprecated; use `thinking: ThinkingConfig` (binary on/off on Opus 4.7+).
- `AgentDefinition.tools=["Skill", …]` / `allowed_tools` with `"Skill"` — deprecated; use the
  `skills` field. **Check `DEFAULT_AGENTS` in bot.py** for this pattern and migrate.
- Hooks on the same event fire **concurrently** — a new Python `PostToolUse` and the existing
  shell `PreToolUse` can interleave; keep them independent and side-effect-safe.

## Related
- Spec 027 — `effort`/`thinking`/`AgentDefinition.tools` overlap (cost levers; cross-reference).
- Spec 028 — persistent client unlocks `toggle_mcp_server`, `stop_task`, eager session-store.
- Spec 011 — monitoring UI (streaming + timeline enrichment land here).
