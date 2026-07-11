"""
e2e_fake_engine.py — deterministic scripted stand-in for engine.run_engine.

Used ONLY when E2E_FAKE_ENGINE=1 (see bot.py:_build_ctx). Wired into ctx["run_engine"]
after the real ctx is built, so it is a drop-in replacement everywhere the cockpit
reads ctx["run_engine"] (chat, cards, deferred runs) — no SDK, no network, no tokens.

Purpose (spec-072): let the E2E Playwright suite (tests/e2e/) drive a REAL cockpit
process end to end without depending on the Claude Agent SDK or a subscription.

Script selection is a marker substring in the prompt (checked in order below):
  "e2e:error" -> a single error event, no result (tests error rendering)
  "e2e:tool"  -> one Bash-shaped tool event + text + result (tests tool-row rendering)
  "e2e:slow"  -> two text_delta events separated by a long silent gap (tests
                 mid-run reload / re-attach; the gap is what the heartbeat pump in
                 webapp.py's chat stream is designed to survive)
  "e2e:text"  -> three text_delta chunks + a final text + result (tests plain
                 streaming: no duplicate/chopped bubbles)
  anything else -> one short text + result (default ack, e.g. queued busy-path sends)

Event schema mirrors engine.py's real contract exactly (see its "ENGINE" docstring):
  {"type": "text_delta", "text": str}
  {"type": "text",       "text": str}
  {"type": "tool",       "name": str, "input": dict}
  {"type": "result",     "session_id": str|None, "cost_usd": float|None}
  {"type": "error",      "exc": BaseException}

Transcript bookkeeping: after each turn this ALSO appends a minimal, real-shaped SDK
transcript line pair to the same place the real SDK CLI writes conversation history
(~/.claude/projects/<slug>/<sid>.jsonl — see webapp.py:_sdk_sessions_dir). Without
this, GET /api/projects/{id}/session-history (webapp.py:api_project_session_history)
reads that file directly and returns `messages: []` for a fake session_id, and any
post-turn hydrate (queue drain, poll, reload after the turn ends) reconciles the
visible chat down to nothing — the live SSE buffer is only consulted while the turn
is still "running". This is pure local file I/O (no SDK call, no network, no tokens),
and it only ever touches a fake $HOME set up by the e2e harness (tests/e2e/conftest.py
overrides HOME for the whole subprocess) — never the operator's real ~/.claude/projects/.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

# Gap (seconds) between the two text_delta events in the "e2e:slow" scenario.
# Kept short by default so the local/CI suite stays fast; override via env if a
# test needs a longer silent stretch (e.g. to exercise CHAT_SSE_PING_SEC pings).
_SLOW_GAP_SEC = float(os.environ.get("E2E_SLOW_GAP_SEC", "3.0"))

# Per-chunk delay for the "typing" feel in "e2e:text"/"e2e:tool". Also gives the
# busy-path scenario (two sends back-to-back) a real window where the session is
# genuinely still running when the second send arrives.
_DELTA_GAP_SEC = float(os.environ.get("E2E_DELTA_GAP_SEC", "0.3"))


def _stable_session_id(session_key: str, resume_session_id: str | None) -> str:
    """Mirrors the real engine's resume semantics: reuse resume_session_id when the
    caller passed one (the cockpit does, once a prior turn has set ctx["sessions"]),
    otherwise mint a fresh id. This is what keeps the sid "stable per session_key"
    across turns of the same chat — the cockpit re-passes the saved session_id."""
    if resume_session_id:
        return resume_session_id
    return f"e2e-{uuid.uuid4()}"


def _transcript_path(cwd: str, sid: str) -> Path:
    """Same slugging rule as webapp.py:_sdk_sessions_dir — duplicated locally (one
    regex line) rather than imported, to keep this module import-independent from
    webapp.py."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    return Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"


def _append_transcript(cwd: str, sid: str, prompt: str, reply_text: str,
                        tool_calls: "list[dict] | None" = None) -> None:
    """Appends a minimal user+assistant line pair in the real SDK transcript shape
    (see webapp.py:_session_history for the exact fields it parses). Best-effort —
    a failure here must never break the fake engine's actual event stream."""
    try:
        path = _transcript_path(cwd, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        content: list[dict] = []
        for t in (tool_calls or []):
            content.append({"type": "tool_use", "name": t["name"], "input": t.get("input", {})})
        content.append({"type": "text", "text": reply_text})
        lines = [
            json.dumps({"type": "user", "message": {"content": prompt}, "timestamp": now_iso}),
            json.dumps({"type": "assistant", "message": {"content": content, "usage": {}}, "timestamp": now_iso}),
        ]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


async def run_engine(
    project_name: str = "",
    cwd: str = "",
    prompt: str = "",
    session_key: str = "",
    model: str | None = None,
    system_prompt: dict | None = None,
    env: dict | None = None,
    resume_session_id: str | None = None,
    agents: "dict | None" = None,
    skip_conductor_prompt: bool = False,
    *,
    ctx: "dict | None" = None,
    ephemeral: bool = False,
    output_format: "dict | None" = None,
    effort: "str | None" = None,
    ultracode: bool = False,
    entrypoint: str = "chat",
    disallowed_tools_extra: "list | None" = None,
    **_ignored: Any,
) -> AsyncGenerator[dict, None]:
    """Scripted fake for engine.run_engine — same call signature/kwargs, zero
    SDK/network calls. See module docstring for the script table."""
    sid = _stable_session_id(session_key, resume_session_id)
    prompt = prompt or ""

    if "e2e:error" in prompt:
        yield {"type": "error", "exc": RuntimeError("e2e scripted failure")}
        return

    if "e2e:tool" in prompt:
        tool_call = {"name": "Bash", "input": {"command": "echo e2e", "description": "e2e scripted tool call"}}
        yield {"type": "tool", **tool_call}
        await asyncio.sleep(_DELTA_GAP_SEC)
        text = f"e2e tool scenario done for {session_key}"
        yield {"type": "text_delta", "text": text}
        yield {"type": "text", "text": text}
        _append_transcript(cwd, sid, prompt, text, tool_calls=[tool_call])
        yield {"type": "result", "session_id": sid, "cost_usd": 0.0}
        return

    if "e2e:multiblock" in prompt:
        # Regression for the "streamed sentence appears then vanishes; Ctrl+R restores it" bug:
        # three text blocks SEPARATED BY TOOL CALLS. api_project_chat runs each {type:"tool"}
        # through _format_tool, which adds a tool-type `kind` ("bash") to the bus event. If the
        # client's bus dispatch is gated on `!evt.kind`, the tool is silently dropped live, so it
        # never splits the streaming bubble — the next block's deltas pile into the SAME bubble and
        # its {type:"text"} finalize REPLACES it, deleting the earlier block. A deliberately LONG
        # gap before `result` keeps the turn "running" so the assertion observes the LIVE canvas
        # (no post-turn hydrate correcting it from the transcript).
        blocks = ["BLOCK_ONE_alpha done.", "BLOCK_TWO_beta done.", "BLOCK_THREE_gamma done."]
        tool_calls = []
        for i, blk in enumerate(blocks):
            yield {"type": "text_delta", "text": blk[: len(blk) // 2]}
            await asyncio.sleep(_DELTA_GAP_SEC)
            yield {"type": "text_delta", "text": blk[len(blk) // 2:]}
            yield {"type": "text", "text": blk}
            if i < len(blocks) - 1:
                tc = {"name": "Bash", "input": {"command": f"echo block{i}", "description": f"e2e split {i}"}}
                tool_calls.append(tc)
                yield {"type": "tool", **tc}
                await asyncio.sleep(_DELTA_GAP_SEC)
        # Long silent tail: the turn stays running while the test asserts all three blocks survive.
        await asyncio.sleep(_SLOW_GAP_SEC)
        _append_transcript(cwd, sid, prompt, "\n".join(blocks), tool_calls=tool_calls)
        yield {"type": "result", "session_id": sid, "cost_usd": 0.0}
        return

    if "e2e:slow" in prompt:
        head = "starting slow scenario... "
        tail = "done after the long silence."
        yield {"type": "text_delta", "text": head}
        await asyncio.sleep(_SLOW_GAP_SEC)
        yield {"type": "text_delta", "text": tail}
        full = head + tail
        yield {"type": "text", "text": full}
        _append_transcript(cwd, sid, prompt, full)
        yield {"type": "result", "session_id": sid, "cost_usd": 0.0}
        return

    if "e2e:text" in prompt:
        parts = ["Hello, ", "this is ", "a scripted e2e reply."]
        for part in parts:
            yield {"type": "text_delta", "text": part}
            await asyncio.sleep(_DELTA_GAP_SEC)
        full = "".join(parts)
        yield {"type": "text", "text": full}
        _append_transcript(cwd, sid, prompt, full)
        yield {"type": "result", "session_id": sid, "cost_usd": 0.0}
        return

    # Default: short ack for any unscripted prompt (e.g. the second message of the
    # busy-path scenario, which just needs to render after the first one drains).
    text = f"e2e fake ack: {prompt[:80]}"
    yield {"type": "text_delta", "text": text}
    yield {"type": "text", "text": text}
    _append_transcript(cwd, sid, prompt, text)
    yield {"type": "result", "session_id": sid, "cost_usd": 0.0}
