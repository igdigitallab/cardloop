"""Isolated Codex runtime adapter for Cardloop.

Claude's engine.py intentionally does not import or depend on this module.  The
web layer selects this adapter only for provider-pinned Codex chats/cards.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import AsyncGenerator, Iterable


PROVIDER = "codex"
DEFAULT_CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.6-sol")
CODEX_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
_REGISTRY_TTL_SEC = 300.0
_registry_cache: dict = {"ts": 0.0, "data": None}


class CodexUnavailableError(RuntimeError):
    """Raised when a Codex run is requested but the provider is unavailable."""


def codex_enabled() -> bool:
    return os.getenv("CODEX_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _sdk():
    """Import the beta SDK lazily so disabled Codex cannot affect Claude startup."""
    try:
        from openai_codex import ApprovalMode, AsyncCodex, Sandbox
        return AsyncCodex, ApprovalMode, Sandbox
    except Exception as exc:  # pragma: no cover - exercised in deployment failures
        raise CodexUnavailableError(f"Codex SDK is unavailable: {exc}") from exc


def _account_auth(account_response) -> tuple[bool, str | None, str | None]:
    raw = account_response.model_dump(mode="json", by_alias=True)
    account = raw.get("account") or {}
    auth_type = account.get("type")
    # Cardloop intentionally accepts subscription auth only. Never silently bill
    # an API key because one happened to exist in the service environment.
    authenticated = auth_type == "chatgpt"
    return authenticated, auth_type, account.get("planType")


async def provider_info(*, force: bool = False) -> dict:
    """Return feature-flag, auth, models, efforts, and capability metadata."""
    if not codex_enabled():
        return {
            "provider": PROVIDER,
            "enabled": False,
            "available": False,
            "authenticated": False,
            "auth_type": None,
            "models": [],
            "reasoning_levels": list(CODEX_REASONING_LEVELS),
            "capabilities": _capabilities(),
            "error": "Codex is disabled by CODEX_ENABLED=false",
        }
    now = time.time()
    if not force and _registry_cache["data"] is not None and now - _registry_cache["ts"] < _REGISTRY_TTL_SEC:
        return _registry_cache["data"]
    AsyncCodex, _, _ = _sdk()
    try:
        async with AsyncCodex() as codex:
            account = await codex.account()
            authenticated, auth_type, plan_type = _account_auth(account)
            if not authenticated:
                raise CodexUnavailableError(
                    "Codex must be signed in with ChatGPT subscription auth; API-key auth is not allowed"
                )
            response = await codex.models()
            raw_models = response.model_dump(mode="json", by_alias=True).get("data") or []
            models = []
            for item in raw_models:
                if item.get("hidden"):
                    continue
                models.append({
                    "value": item.get("model") or item.get("id"),
                    "label": item.get("displayName") or item.get("model") or item.get("id"),
                    "default": bool(item.get("isDefault")),
                    "default_reasoning": item.get("defaultReasoningEffort"),
                    "reasoning_levels": [
                        effort.get("reasoningEffort")
                        for effort in item.get("supportedReasoningEfforts") or []
                        if effort.get("reasoningEffort")
                    ],
                })
            data = {
                "provider": PROVIDER,
                "enabled": True,
                "available": True,
                "authenticated": True,
                "auth_type": auth_type,
                "plan_type": plan_type,
                "models": models,
                "reasoning_levels": list(CODEX_REASONING_LEVELS),
                "capabilities": _capabilities(),
                "error": None,
            }
    except Exception as exc:
        data = {
            "provider": PROVIDER,
            "enabled": True,
            "available": False,
            "authenticated": False,
            "auth_type": None,
            "models": [],
            "reasoning_levels": list(CODEX_REASONING_LEVELS),
            "capabilities": _capabilities(),
            "error": str(exc),
        }
    _registry_cache.update(ts=now, data=data)
    return data


def _capabilities() -> dict:
    return {
        "chat": True,
        "board": True,
        "history": True,
        "search": True,
        "usage": True,
        "plan_mode": True,
        "multi_agent": True,
        "skills": True,
        "plugins": True,
        "interrupt": True,
    }


def _developer_instructions(project_name: str, cwd: str, *, plan_mode: bool, multi_agent: bool) -> str:
    instructions = [
        "You are the Codex engine inside Cardloop.",
        "Before taking task actions, read the durable global rules at $HOME/CLAUDE.md completely.",
        "Then read the nearest applicable CLAUDE.md in the project, if present; the closest file wins for project-local rules.",
        "Do not copy those rules into another file. Follow them in place.",
        f"The selected project is {project_name!r} and its working directory is {cwd!r}.",
    ]
    if plan_mode:
        instructions.extend([
            "This is a planning turn. Inspect and reason in read-only mode.",
            "Do not edit files or mutate external state. Return a concrete implementation plan for operator approval.",
        ])
    if multi_agent:
        instructions.append(
            "Use native Codex subagents when independent parallel work materially improves the result, and synthesize their findings."
        )
    return "\n".join(instructions)


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


def _item_root(item):
    return getattr(item, "root", item)


def _tool_event(item) -> dict | None:
    item = _item_root(item)
    kind = type(item).__name__
    if kind == "CommandExecutionThreadItem":
        return {"type": "tool", "name": "Bash", "input": {"command": item.command, "cwd": str(item.cwd)}}
    if kind == "FileChangeThreadItem":
        changes = []
        for change in item.changes:
            changes.append(change.model_dump(mode="json", by_alias=True))
        path = next((c.get("path") for c in changes if c.get("path")), "")
        return {"type": "tool", "name": "Edit", "input": {"file_path": path, "changes": changes}}
    if kind == "McpToolCallThreadItem":
        return {"type": "tool", "name": getattr(item, "tool", "MCP"), "input": getattr(item, "arguments", {}) or {}}
    if kind == "DynamicToolCallThreadItem":
        return {"type": "tool", "name": item.tool, "input": item.arguments if isinstance(item.arguments, dict) else {"input": item.arguments}}
    if kind == "WebSearchThreadItem":
        return {"type": "tool", "name": "WebSearch", "input": {"query": item.query}}
    if kind == "ImageViewThreadItem":
        return {"type": "tool", "name": "Read", "input": {"file_path": str(getattr(item, "path", ""))}}
    return None


def _subagent_event(item, *, completed: bool) -> dict | None:
    item = _item_root(item)
    kind = type(item).__name__
    if kind == "CollabAgentToolCallThreadItem":
        receivers = list(getattr(item, "receiver_thread_ids", []) or [])
        task_id = receivers[0] if receivers else getattr(item, "id", "")
        status = _enum_value(getattr(item, "status", "running"))
        terminal_ok = status in {"completed", "success"}
        return {
            "type": "subagent",
            "subtype": "notification" if completed else "started",
            "task_id": task_id,
            "description": getattr(item, "prompt", None) or _enum_value(getattr(item, "tool", "subagent")),
            "status": "completed" if completed and terminal_ok else ("failed" if completed else "running"),
            "summary": None,
            "last_tool_name": None,
        }
    if kind == "SubAgentActivityThreadItem":
        activity = _enum_value(getattr(item, "kind", "progress"))
        terminal = any(word in activity.lower() for word in ("complete", "fail", "close"))
        return {
            "type": "subagent",
            "subtype": "notification" if terminal else "progress",
            "task_id": getattr(item, "agent_thread_id", ""),
            "description": getattr(item, "agent_path", ""),
            "status": "failed" if "fail" in activity.lower() else ("completed" if terminal else "running"),
            "summary": activity,
            "last_tool_name": None,
        }
    return None


def normalize_notification(notification) -> Iterable[dict]:
    """Translate one SDK notification into Cardloop's provider-neutral events."""
    payload = notification.payload
    kind = type(payload).__name__
    if kind == "AgentMessageDeltaNotification":
        yield {"type": "text_delta", "text": payload.delta}
    elif kind in {"ItemStartedNotification", "ItemCompletedNotification"}:
        completed = kind == "ItemCompletedNotification"
        item = _item_root(payload.item)
        subagent = _subagent_event(item, completed=completed)
        if subagent:
            yield subagent
        if not completed:
            tool = _tool_event(item)
            if tool:
                yield tool
        elif type(item).__name__ == "AgentMessageThreadItem" and getattr(item, "text", ""):
            yield {"type": "text", "text": item.text}
    elif kind == "ErrorNotification" and not payload.will_retry:
        message = getattr(payload.error, "message", None) or str(payload.error)
        yield {"type": "error", "exc": RuntimeError(message)}
    elif kind == "AccountRateLimitsUpdatedNotification":
        yield {"type": "rate_limit", "status": "updated", "snapshot": payload.rate_limits.model_dump(mode="json", by_alias=True)}


def _usage_dict(usage) -> dict:
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
    last = usage.last
    return {
        "input_tokens": last.input_tokens,
        "output_tokens": last.output_tokens,
        "cached_input_tokens": last.cached_input_tokens,
        "reasoning_output_tokens": last.reasoning_output_tokens,
        "total_tokens": last.total_tokens,
        "context_window": usage.model_context_window,
    }


def _append_usage(data_dir: Path | None, *, thread_id: str, model: str, project_name: str,
                  session_key: str, entrypoint: str, usage: dict, duration_ms: int | None) -> None:
    if data_dir is None:
        return
    try:
        path = data_dir / "codex_usage.jsonl"
        row = {
            "ts": time.time(), "provider": PROVIDER, "thread_id": thread_id,
            "project": project_name, "session_key": session_key, "entrypoint": entrypoint,
            "model": model, "duration_ms": duration_ms, **usage,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[codex] usage ledger write failed: {exc}")


def _save_rate_limits(data_dir: Path | None, snapshot: dict) -> None:
    """Persist the newest rate-limit snapshot so the badge survives a restart.

    Codex only PUSHES limits (`account/rateLimits/updated`) during a turn — there is no
    endpoint to ask. Between runs the last snapshot is all we have, so it goes to disk
    with the time we heard it; the UI dims a stale one instead of presenting it as live.
    """
    if data_dir is None or not snapshot:
        return
    try:
        path = data_dir / "codex_rate_limits.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "snapshot": snapshot}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        print(f"[codex] rate-limit snapshot write failed: {exc}")


def _window_label(window: dict, fallback: str) -> str:
    mins = window.get("windowDurationMins")
    if not isinstance(mins, int) or mins <= 0:
        return fallback
    if mins % 10080 == 0:
        weeks = mins // 10080
        return "Week" if weeks == 1 else f"{weeks} weeks"
    if mins % 1440 == 0:
        days = mins // 1440
        return "Day" if days == 1 else f"{days} days"
    if mins % 60 == 0:
        return f"{mins // 60}-hour window"
    return f"{mins}-minute window"


def _norm_codex_window(window: dict | None, fallback_label: str, *, reached: bool) -> dict | None:
    """One RateLimitWindow → the same row shape the Claude limits use."""
    if not isinstance(window, dict):
        return None
    pct = window.get("usedPercent")
    resets_at = window.get("resetsAt")
    if isinstance(resets_at, (int, float)) and 0 < resets_at < 1_000_000_000:
        # Defensive: a relative "seconds from now" would render as 1970 and read as "soon"
        # forever. Anything below ~2001 cannot be an absolute unix timestamp.
        resets_at = time.time() + resets_at
    return {
        "status": "rejected" if reached else "allowed",
        "resets_at": int(resets_at) if isinstance(resets_at, (int, float)) else None,
        "utilization": (pct / 100.0) if isinstance(pct, (int, float)) else None,
        "label": _window_label(window, fallback_label),
    }


def rate_limits_for_ui(data_dir: Path | None) -> dict | None:
    """Last known Codex limits in the frontend's row shape, or None if never seen.

    `ts` is when the snapshot arrived, NOT when it was true — a window may have rolled
    over since. The badge shows the age; it does not silently refresh a number nobody
    reported.
    """
    if data_dir is None:
        return None
    try:
        raw = json.loads((data_dir / "codex_rate_limits.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    snapshot = raw.get("snapshot") or {}
    reached = bool(snapshot.get("rateLimitReachedType"))
    limits: dict = {}
    primary = _norm_codex_window(snapshot.get("primary"), "Primary window", reached=reached)
    if primary:
        limits["primary"] = primary
    secondary = _norm_codex_window(snapshot.get("secondary"), "Secondary window", reached=reached)
    if secondary:
        limits["secondary"] = secondary
    credits = snapshot.get("credits") or {}
    if credits and not credits.get("unlimited") and not credits.get("hasCredits"):
        # The failure Igor actually hit: auth fine, subscription fine, wallet empty.
        limits["credits"] = {
            "status": "rejected", "resets_at": None, "utilization": None,
            "label": "Credits (empty)",
        }
    if not limits:
        return None
    return {
        "ts": raw.get("ts"),
        "plan_type": snapshot.get("planType"),
        "limit_name": snapshot.get("limitName"),
        "limits": limits,
    }


async def run_codex_engine(
    *, project_name: str, cwd: str, prompt: str, session_key: str, model: str | None = None,
    resume_thread_id: str | None = None, ctx: dict | None = None, ephemeral: bool = False,
    effort: str | None = None, plan_mode: bool = False, multi_agent: bool = False,
    chat_id: str | None = None, entrypoint: str = "chat", **_ignored,
) -> AsyncGenerator[dict, None]:
    """Run one Codex turn and yield Cardloop-normalized events."""
    if not codex_enabled():
        yield {"type": "error", "exc": CodexUnavailableError("Codex is disabled by CODEX_ENABLED=false")}
        return
    AsyncCodex, ApprovalMode, Sandbox = _sdk()
    selected_model = model or DEFAULT_CODEX_MODEL
    selected_effort = effort if effort in CODEX_REASONING_LEVELS else None
    sandbox = Sandbox.read_only if plan_mode else Sandbox.full_access
    developer_instructions = _developer_instructions(
        project_name, cwd, plan_mode=plan_mode, multi_agent=multi_agent,
    )
    codex = AsyncCodex()
    turn = None
    try:
        await codex.__aenter__()
        authenticated, auth_type, _ = _account_auth(await codex.account())
        if not authenticated:
            raise CodexUnavailableError(
                f"Codex requires ChatGPT subscription auth (current auth: {auth_type or 'none'})"
            )
        thread_kwargs = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": cwd,
            "developer_instructions": developer_instructions,
            "model": selected_model,
            "sandbox": sandbox,
        }
        if resume_thread_id:
            thread = await codex.thread_resume(resume_thread_id, **thread_kwargs)
        else:
            thread = await codex.thread_start(ephemeral=ephemeral, **thread_kwargs)
        turn = await thread.turn(
            prompt,
            approval_mode=ApprovalMode.deny_all,
            cwd=cwd,
            effort=selected_effort,
            model=selected_model,
            sandbox=sandbox,
        )
        if ctx is not None:
            ctx["running"][session_key] = turn
        final_text = ""
        final_turn = None
        usage = None
        async for notification in turn.stream():
            for event in normalize_notification(notification):
                if event["type"] == "text":
                    final_text = event.get("text", "")
                elif event["type"] == "rate_limit" and event.get("snapshot"):
                    _save_rate_limits((ctx or {}).get("DATA"), event["snapshot"])
                yield event
            payload = notification.payload
            payload_kind = type(payload).__name__
            if payload_kind == "ThreadTokenUsageUpdatedNotification":
                usage = payload.token_usage
            elif payload_kind == "TurnCompletedNotification":
                final_turn = payload.turn
        usage_data = _usage_dict(usage)
        duration_ms = getattr(final_turn, "duration_ms", None)
        _append_usage(
            (ctx or {}).get("DATA"), thread_id=thread.id, model=selected_model,
            project_name=project_name, session_key=session_key, entrypoint=entrypoint,
            usage=usage_data, duration_ms=duration_ms,
        )
        if plan_mode and ctx is not None and final_text:
            create_plan = ctx.get("create_pending_plan")
            if callable(create_plan):
                create_plan(ctx, session_key, chat_id, final_text, provider=PROVIDER,
                            codex_thread_id=thread.id, model=selected_model)
        yield {
            "type": "result", "thread_id": thread.id, "session_id": None,
            "model": selected_model, "duration_ms": duration_ms,
            "context_tokens": usage_data.get("total_tokens", 0),
            "context_window": usage_data.get("context_window"), "usage": usage_data,
        }
    except Exception as exc:
        yield {"type": "error", "exc": exc}
    finally:
        if ctx is not None and turn is not None and ctx.get("running", {}).get(session_key) is turn:
            # The web handler owns final lock removal. Restore its sentinel so a
            # concurrently arriving request still observes the slot as occupied.
            ctx["running"][session_key] = True
        await codex.close()


async def read_thread(thread_id: str) -> dict:
    """Read a Codex thread with turns without consulting Claude transcripts."""
    AsyncCodex, _, _ = _sdk()
    async with AsyncCodex() as codex:
        # thread/read does not require resume and works for inactive threads.
        response = await codex._client.thread_read(thread_id, include_turns=True)
        return response.model_dump(mode="json", by_alias=True)


def history_messages(thread_payload: dict) -> list[dict]:
    thread = thread_payload.get("thread") or {}
    out: list[dict] = []
    for turn in thread.get("turns") or []:
        for raw_item in turn.get("items") or []:
            item = raw_item.get("root", raw_item)
            kind = item.get("type")
            if kind == "userMessage":
                text = "\n".join(
                    part.get("text", "") for part in item.get("content") or []
                    if part.get("type") == "text" and part.get("text")
                )
                if text:
                    out.append({"role": "user", "text": text, "tools": [], "uuid": item.get("id")})
            elif kind == "agentMessage" and item.get("text"):
                out.append({"role": "assistant", "text": item["text"], "tools": [], "uuid": item.get("id")})
    return out[-100:]


async def list_threads(*, cwd: str | None = None, limit: int = 30, search_term: str | None = None) -> list[dict]:
    AsyncCodex, _, _ = _sdk()
    async with AsyncCodex() as codex:
        response = await codex.thread_list(limit=limit, search_term=search_term)
        rows = response.model_dump(mode="json", by_alias=True).get("data") or []
    if cwd:
        rows = [row for row in rows if row.get("cwd") == cwd]
    return rows


def usage_rows(data_dir: Path, *, days: int | None = None) -> list[dict]:
    path = data_dir / "codex_usage.jsonl"
    if not path.is_file():
        return []
    cutoff = time.time() - days * 86400 if days else 0
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            row = json.loads(line)
            if row.get("ts", 0) >= cutoff:
                rows.append(row)
    except Exception:
        return rows
    return rows
