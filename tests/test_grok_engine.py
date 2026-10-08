"""spec-095 P1: the Grok engine against a fake `grok` binary (tests/fake_grok_acp.py).

The fake is a REAL subprocess (real pids, real process groups) replaying recorded wire fixtures, so
the kill / reaper / hang tests exercise the actual teardown, not a mock. No network, no tokens.

Fixture sources: tests/fixtures/grok/synthetic_*.jsonl are hand-made (real-shaped prelude, scripted
bodies); the un-prefixed ones are recordings from the real CLI (tools/grok_record_fixtures.py) and
are only held to the invariants in `test_every_recorded_fixture_replays_cleanly`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import grok_engine
from grok_engine import (
    D3_ENV, GrokAuthError, GrokTurn, GrokUnavailableError, ensure_home, run_grok_engine,
)

FAKE = Path(__file__).resolve().parent / "fake_grok_acp.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"

# The spec §4-D3 block, restated here on purpose: an engine that drops one of these must fail.
SPEC_D3 = {
    "GROK_CLAUDE_AGENTS_ENABLED": "0", "GROK_CLAUDE_HOOKS_ENABLED": "0",
    "GROK_CLAUDE_MCPS_ENABLED": "0", "GROK_CLAUDE_RULES_ENABLED": "0",
    "GROK_CLAUDE_SKILLS_ENABLED": "0",
    "GROK_CURSOR_AGENTS_ENABLED": "0", "GROK_CURSOR_HOOKS_ENABLED": "0",
    "GROK_CURSOR_MCPS_ENABLED": "0", "GROK_CURSOR_RULES_ENABLED": "0",
    "GROK_CURSOR_SKILLS_ENABLED": "0",
    "GROK_TELEMETRY_ENABLED": "0", "GROK_TELEMETRY_TRACE_UPLOAD": "0", "GROK_MEMORY": "0",
    "GROK_ASK_USER_QUESTION": "0", "GROK_AUTO_WAKE": "0", "GROK_WORKFLOWS": "0",
    # P1b: foreign-session import off, and folder trust pinned ON (the live-measured switch that
    # keeps a project's own .mcp.json / .grok MCP servers, hooks and skills from starting)
    "GROK_CLAUDE_SESSIONS_ENABLED": "0", "GROK_CURSOR_SESSIONS_ENABLED": "0",
    "GROK_CODEX_SESSIONS_ENABLED": "0", "GROK_FOLDER_TRUST": "1",
}
SECRETS = {
    "WEB_PASSWORD": "pw-s3cret-web-value",
    "ANTHROPIC_API_KEY": "sk-ant-s3cret-value",
    "XAI_API_KEY": "xai-s3cret-value-123",
    "VAPID_PRIVATE_KEY": "vapid-s3cret-value",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-s3cret-value",
}
TOKEN_ACCESS = "ACCESS-TOKEN-VALUE-abcdef123456"
TOKEN_REFRESH = "REFRESH-TOKEN-VALUE-abcdef123456"


# ------------------------------------------------------------------------------------------
# harness
# ------------------------------------------------------------------------------------------

class Env:
    """One isolated engine environment: wrapper GROK_BIN, home with a login, fake bwrap."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        bwrap = self.bindir / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n")
        bwrap.chmod(0o755)
        self.home = tmp_path / "grok-home"
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.cwd = tmp_path / "proj"
        self.cwd.mkdir()
        self.fake_home = tmp_path / "fakehome"        # $HOME for the engine's ~ expansion
        self.fake_home.mkdir()
        self.secret_dir = tmp_path / "secrets"        # an existing deny target
        self.secret_dir.mkdir()
        # the engine always denies the cockpit repo's own `.env`: a scratch repo without one, so no
        # assertion below depends on whether the checkout running the suite has a `.env`
        self.engine_repo = tmp_path / "cockpit-repo"
        self.engine_repo.mkdir()
        monkeypatch.setattr(grok_engine, "_REPO", self.engine_repo)
        self.dumps = tmp_path / "dumps"
        self.dumps.mkdir()
        self.ctx = {"DATA": self.data, "running": {}}
        self.wrapper = tmp_path / "grok"
        for k in list(os.environ):
            if k.startswith(("GROK_", "FAKE_")):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(self.data))   # provider_info has no ctx
        monkeypatch.setenv("HOME", str(self.fake_home))
        monkeypatch.setenv("PATH", f"{self.bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        monkeypatch.setenv("GROK_ENABLED", "true")
        monkeypatch.setenv("GROK_BIN", str(self.wrapper))
        monkeypatch.setenv("GROK_HOME", str(self.home))
        monkeypatch.setenv("GROK_SANDBOX_DENY", str(self.secret_dir))
        self.write_auth()
        self.fake("synthetic_text")
        grok_engine.reset_cache()
        grok_engine._unknown_tools_seen.clear()
        grok_engine._unknown_updates_seen.clear()
        grok_engine._window_cache.clear()

    def write_auth(self, **entry):
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        body = {"auth_mode": "oidc", "coding_data_retention_opt_out": True,
                "email": "user@example.invalid", "key": TOKEN_ACCESS, "refresh_token": TOKEN_REFRESH,
                "expires_at": "2099-01-01T00:00:00Z"}
        body.update(entry)
        body = {k: v for k, v in body.items() if v is not None}
        path = self.home / "auth.json"
        path.write_text(json.dumps({"https://auth.x.ai::client-id": body}))
        os.chmod(path, 0o600)

    def fake(self, fixture: str = "synthetic_text", **switches) -> None:
        """(Re)write the wrapper binary with these FAKE_GROK_* switches baked in."""
        env = {"FAKE_GROK_FIXTURE": fixture,
               "FAKE_GROK_ARGV_DUMP": str(self.dumps / "argv.json"),
               "FAKE_GROK_ENV_DUMP": str(self.dumps / "env.json"),
               "FAKE_GROK_CWD_DUMP": str(self.dumps / "cwd.json"),
               "FAKE_GROK_PID_FILE": str(self.dumps / "pids.json"),
               "FAKE_GROK_LOG": str(self.dumps / "log.jsonl")}
        env.update({f"FAKE_GROK_{k.upper()}": str(v) for k, v in switches.items()})
        # A python wrapper, not sh: a shell would add PWD/OLDPWD/_ and blur the env assertions.
        body = (f"#!{sys.executable}\nimport os, sys\nos.environ.update({env!r})\n"
                f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}, *sys.argv[1:]])\n")
        self.wrapper.write_text(body)
        self.wrapper.chmod(0o755)
        for f in self.dumps.iterdir():
            f.unlink()

    def dump(self, name: str):
        return json.loads((self.dumps / f"{name}.json").read_text())

    def log(self) -> list[dict]:
        p = self.dumps / "log.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def sent(self, method: str) -> list[dict]:
        return [m for m in self.log() if m.get("method") == method]

    def kwargs(self, **over) -> dict:
        kw = dict(project_name="proj", cwd=str(self.cwd), prompt="hello", session_key="p:1", ctx=self.ctx)
        kw.update(over)
        return kw

    async def run(self, **over) -> list[dict]:
        return [ev async for ev in run_grok_engine(**self.kwargs(**over))]


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    grok_engine.reset_cache()


def types(events):
    return [e["type"] for e in events]


def only(events, kind):
    return [e for e in events if e["type"] == kind]


def last_error(events) -> str:
    errs = only(events, "error")
    assert errs, f"expected an error event, got {types(events)}"
    return str(errs[-1]["exc"])


def pid_gone(pid: int) -> bool:
    return not grok_engine._pid_alive(pid)


async def wait_until(pred, timeout=8.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    monkeypatch.setattr(grok_engine, "HANDSHAKE_TIMEOUT_SEC", 3.0)
    monkeypatch.setattr(grok_engine, "TERM_WAIT_SEC", 1.5)
    monkeypatch.setattr(grok_engine, "INTERRUPT_WAIT_SEC", 1.5)


# ------------------------------------------------------------------------------------------
# §5.4 event mapping, row by row
# ------------------------------------------------------------------------------------------

async def test_text_chunks_stream_as_deltas_then_one_assembled_text(env, capsys):
    events = await env.run()
    assert "unmapped session update" not in capsys.readouterr().out   # thoughts are KNOWN noise
    assert types(events) == ["text_delta", "text_delta", "text_delta", "text", "result"]
    assert [e["text"] for e in only(events, "text_delta")] == ["Hel", "lo ", "world"]
    assert only(events, "text")[0]["text"] == "Hello world"
    assert "thinking" not in json.dumps(events)  # agent_thought_chunk is dropped like Codex does


async def test_text_before_a_tool_is_flushed_as_its_own_text_event(env):
    env.fake("synthetic_tool")
    events = await env.run()
    assert types(events) == ["text_delta", "text", "tool", "text_delta", "text_delta", "text", "result"]
    assert only(events, "text")[0]["text"] == "Running it."
    assert only(events, "text")[1]["text"] == "Done: hi"
    tool = only(events, "tool")[0]
    assert tool == {"type": "tool", "name": "Bash", "input": {"command": "echo hi", "description": "say hi"}}


async def test_tool_call_update_emits_nothing(env):
    env.fake("synthetic_tool")
    events = await env.run()
    assert len(only(events, "tool")) == 1  # the completed update did not produce a second row


async def test_tool_map_covers_every_d10_row_and_passes_unknown_through(env, capsys):
    env.fake("synthetic_tools_all")
    events = await env.run()
    tools = [(e["name"], e["input"]) for e in only(events, "tool")]
    assert tools == [
        ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
        ("Write", {"file_path": "b.py", "content": "print(1)"}),
        ("Read", {"file_path": "c.py"}),
        ("Grep", {"pattern": "foo", "path": "src"}),
        ("LS", {"path": "."}),
        ("WebFetch", {"url": "https://example.com"}),
        ("TodoWrite", {"todos": [{"content": "a", "status": "pending", "activeForm": "a"}]}),
        ("frobnicate", {"thing": 1}),
        ("frobnicate", {"thing": 2}),
    ]
    # the unknown name is journaled ONCE however often it appears
    assert capsys.readouterr().out.count("unmapped tool 'frobnicate'") == 1


async def test_web_search_backend_tool_waits_for_its_query(env):
    env.fake("synthetic_web_search")
    events = await env.run()
    assert only(events, "tool") == [{"type": "tool", "name": "WebSearch",
                                     "input": {"query": "example domain iana"}}]


async def test_subagent_lifecycle_maps_and_the_child_session_is_not_ours(env):
    env.fake("synthetic_subagent")
    events = await env.run()
    subs = only(events, "subagent")
    assert [(s["subtype"], s["status"], s["task_id"]) for s in subs] == [
        ("started", "running", subs[0]["task_id"]),
        ("progress", "running", subs[0]["task_id"]),
        ("notification", "completed", subs[0]["task_id"]),
    ]
    assert subs[0]["description"] == "Read notes"
    assert subs[1]["last_tool_name"] == "read_file"
    assert subs[2]["summary"] == "alpha"
    blob = json.dumps(events)
    assert "CHILD TEXT MUST NOT APPEAR" not in blob            # the child's chunks
    assert [e["name"] for e in only(events, "tool")] == []      # neither spawn_subagent nor the child's read
    # context size is OUR last request (1000 + 400), not the child's 99999
    assert only(events, "result")[0]["context_tokens"] == 1400
    assert "".join(e["text"] for e in only(events, "text")) == "Delegating.The first line is alpha."


async def test_subagent_failure_is_reported_as_failed(env):
    env.fake("synthetic_subagent_failed")
    events = await env.run()
    assert [s["status"] for s in only(events, "subagent")] == ["running", "failed"]


async def test_result_event_has_the_spec_shape(env):
    events = await env.run()
    res = only(events, "result")[0]
    assert res["provider_session_id"].startswith("fake-session_1-")
    assert res["thread_id"] is None and res["session_id"] is None
    assert res["model"] == "grok-4.7"
    assert isinstance(res["duration_ms"], int) and res["duration_ms"] >= 0
    assert res["context_tokens"] == 1000 + 400            # last request: input + cache read
    assert res["context_window"] == 256000
    assert res["usage"] == {
        "input_tokens": 2000, "output_tokens": 300, "cached_input_tokens": 800,
        "reasoning_output_tokens": 90, "total_tokens": 2300, "context_window": 256000,
        "notional_usd": round(123456789 / 1e10, 6),
    }


async def test_usage_falls_back_to_summing_response_completed(env):
    env.fake("synthetic_no_usage_block")
    res = only(await env.run(), "result")[0]
    assert res["usage"]["input_tokens"] == (1000 + 400) + (500 + 900)
    assert res["usage"]["output_tokens"] == 150
    assert res["usage"]["cached_input_tokens"] == 1300
    assert res["context_tokens"] == 500 + 900


async def test_unknown_stop_reason_is_an_error_never_silent(env):
    for reason in ("max_tokens", "refusal", "banana"):
        env.fake("synthetic_text", stop_reason=reason)
        events = await env.run()
        assert "result" not in types(events), reason
        assert f"stopReason {reason!r}" in last_error(events)


async def test_unprompted_cancelled_is_an_error(env):
    env.fake("synthetic_cancelled_unprompted")
    events = await env.run()
    assert "result" not in types(events)
    assert "cancelled the turn unprompted" in last_error(events)
    assert "permission request" in last_error(events)


async def test_cancelled_after_our_cancel_is_a_clean_result(env):
    env.fake("synthetic_cancel")
    gen = run_grok_engine(**env.kwargs())
    seen = []
    async for ev in gen:
        seen.append(ev)
        if ev["type"] == "tool":
            turn = env.ctx["running"]["p:1"]
            assert isinstance(turn, GrokTurn)
            t0 = time.monotonic()
            await turn.interrupt()
            assert time.monotonic() - t0 < 1.0   # the agent answered `cancelled` at once
    assert types(seen)[-1] == "result" and "error" not in types(seen)
    assert env.sent("session/cancel")[0]["params"]["sessionId"] == only(seen, "result")[0]["provider_session_id"]


async def test_interrupt_the_agent_ignores_falls_back_to_killpg(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "INTERRUPT_WAIT_SEC", 0.5)
    env.fake("synthetic_cancel", ignore_cancel=1, spawn_child=1)
    seen = []
    async for ev in run_grok_engine(**env.kwargs()):
        seen.append(ev)
        if ev["type"] == "tool":
            t0 = time.monotonic()
            await env.ctx["running"]["p:1"].interrupt()
            assert 0.4 < time.monotonic() - t0 < 1.5
    assert types(seen)[-1] == "result"          # an operator stop is never an error
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def test_interrupt_during_the_handshake_ends_cleanly_and_kills_the_group(env):
    env.fake("synthetic_text", hang_on="session/new", spawn_child=1)
    gen = run_grok_engine(**env.kwargs())
    task = asyncio.ensure_future(_drain(gen))
    assert await wait_until(lambda isinstance_turn=None: isinstance(env.ctx["running"].get("p:1"), GrokTurn))
    await asyncio.sleep(0.4)
    await env.ctx["running"]["p:1"].interrupt()
    events = await asyncio.wait_for(task, 5)
    assert types(events) == ["result"] and events[0]["provider_session_id"] is None
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def _drain(gen):
    return [ev async for ev in gen]


async def test_jsonrpc_error_on_the_prompt_is_an_error_with_its_message(env):
    env.fake("synthetic_text", prompt_error=json.dumps({"code": -32603, "message": "internal kaboom"}))
    msg = last_error(await env.run())
    assert "internal kaboom" in msg


async def test_process_exit_before_the_prompt_response_is_an_error_with_the_stderr_tail(env):
    env.fake("synthetic_text", exit_on="session/prompt", stderr_text="fatal: disk on fire", exit_code=7)
    events = await env.run()
    msg = last_error(events)
    assert "code 7" in msg and "disk on fire" in msg
    assert "result" not in types(events)


async def test_hung_authenticate_becomes_a_signin_error_within_the_timeout(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "HANDSHAKE_TIMEOUT_SEC", 1.0)
    env.fake("synthetic_text", hang_on="authenticate", spawn_child=1)
    t0 = time.monotonic()
    events = await env.run()
    assert time.monotonic() - t0 < 6
    err = only(events, "error")[0]["exc"]
    assert isinstance(err, GrokAuthError)
    assert "Grok sign-in expired" in str(err) and "tools/grok-acct login" in str(err)
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])      # the hung process was killed
    # ... and the registry row flipped, so the picker stops offering Grok
    info = await grok_engine.provider_info()
    assert info["available"] is False and "sign-in expired" in info["error"]


async def test_other_handshake_steps_have_their_own_timeout(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "HANDSHAKE_TIMEOUT_SEC", 1.0)
    for method in ("initialize", "session/new"):
        env.fake("synthetic_text", hang_on=method)
        t0 = time.monotonic()
        msg = last_error(await env.run())
        assert time.monotonic() - t0 < 6, method
        assert f"{method} did not answer within 1s" in msg


async def test_oversized_line_is_an_error_and_the_group_is_killed(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "STREAM_LIMIT", 64 * 1024)
    env.fake("synthetic_text", giant_line=300_000, spawn_child=1)
    events = await env.run()
    assert "event too large" in last_error(events)
    assert "result" not in types(events)
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def test_a_normal_big_line_under_the_limit_is_fine(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "STREAM_LIMIT", 1024 * 1024)
    env.fake("synthetic_text", giant_line=300_000)
    events = await env.run()
    assert types(events)[-1] == "result"
    assert any(len(e.get("text", "")) == 300_000 for e in only(events, "text_delta"))


async def test_stderr_is_drained_and_never_blocks_the_turn(env):
    env.fake("synthetic_text", stderr_flood=3_000_000)   # >> a 64 KiB pipe buffer
    turns, events = [], []

    async def go():
        async for ev in run_grok_engine(**env.kwargs()):
            events.append(ev)
            t = env.ctx["running"].get("p:1")
            if isinstance(t, GrokTurn) and t not in turns:
                turns.append(t)
    await asyncio.wait_for(go(), 20)
    assert types(events)[-1] == "result"
    assert 0 < len(turns[0]._acp._stderr) <= grok_engine.STDERR_RING_BYTES   # a ring, not a log


async def test_stderr_tail_in_errors_is_capped_to_the_ring(env):
    env.fake("synthetic_text", stderr_flood=300_000, exit_on="session/prompt", stderr_text="TAIL-MARKER")
    msg = last_error(await env.run())
    assert "TAIL-MARKER" in msg
    assert len(msg) < 6000


async def test_finally_kills_the_group_when_our_task_is_cancelled(env):
    env.fake("synthetic_cancel", spawn_child=1)            # hangs after the tool call
    task = asyncio.ensure_future(_drain(run_grok_engine(**env.kwargs())))
    assert await wait_until(lambda: (env.dumps / "pids.json").exists() and isinstance(
        env.ctx["running"].get("p:1"), GrokTurn))
    await asyncio.sleep(0.5)
    pids = env.dump("pids")
    assert not pid_gone(pids["leader"]) and not pid_gone(pids["child"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])
    assert env.ctx["running"]["p:1"] is True               # sentinel restored, like Codex


async def test_closing_the_generator_early_also_kills_the_group(env):
    env.fake("synthetic_cancel", spawn_child=1)
    gen = run_grok_engine(**env.kwargs())
    async for ev in gen:
        if ev["type"] == "tool":
            break
    await gen.aclose()
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def test_running_slot_holds_a_grokturn_then_the_sentinel(env):
    seen = []
    async for ev in run_grok_engine(**env.kwargs()):
        seen.append(type(env.ctx["running"].get("p:1")).__name__)
    assert set(seen) == {"GrokTurn"}
    assert env.ctx["running"]["p:1"] is True


async def test_running_slot_is_not_clobbered_when_someone_else_owns_it(env):
    other = object()
    async for ev in run_grok_engine(**env.kwargs()):
        if ev["type"] == "text_delta":
            env.ctx["running"]["p:1"] = other
    assert env.ctx["running"]["p:1"] is other


# ------------------------------------------------------------------------------------------
# gates
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [("true", True), ("1", True), ("YES", True), (" on ", True),
                                      ("false", False), ("", False), ("0", False), ("maybe", False)])
def test_enabled_flag_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("GROK_ENABLED", raw)
    assert grok_engine.grok_enabled() is want


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GROK_ENABLED", raising=False)
    assert grok_engine.grok_enabled() is False


async def test_disabled_engine_yields_an_error_and_spawns_nothing(env, monkeypatch):
    monkeypatch.setenv("GROK_ENABLED", "false")
    events = await env.run()
    assert types(events) == ["error"] and isinstance(events[0]["exc"], GrokUnavailableError)
    assert "GROK_ENABLED=false" in str(events[0]["exc"])
    assert not (env.dumps / "argv.json").exists()


async def test_plan_mode_is_an_error_never_a_silent_run(env):
    events = await env.run(plan_mode=True)
    assert types(events) == ["error"] and "plan mode" in str(events[0]["exc"])
    assert not (env.dumps / "argv.json").exists()


async def test_effort_is_whitelisted_and_only_changed_when_different(env):
    # "high" is the session's current value in the recorded prelude: no set_config_option at all
    await env.run(effort="high")
    assert env.sent("session/set_config_option") == []
    for raw in ("ultra", "max", "minimal", "xhigh; rm -rf", "", None):
        env.fake("synthetic_text")
        assert types(await env.run(effort=raw))[-1] == "result"
        assert env.sent("session/set_config_option") == [], raw   # never forwarded
    env.fake("synthetic_text")
    await env.run(effort="xhigh")
    calls = env.sent("session/set_config_option")
    assert [c["params"]["configId"] for c in calls] == ["reasoning_effort"]
    assert calls[0]["params"]["value"] == "xhigh"


async def test_model_is_set_only_when_it_differs_and_a_rejected_one_is_named(env):
    await env.run()                                    # default grok-4.7 == current
    assert env.sent("session/set_config_option") == []
    env.fake("synthetic_text")
    await env.run(model="grok-4.6")
    assert env.sent("session/set_config_option")[0]["params"] == {
        "sessionId": env.sent("session/prompt")[0]["params"]["sessionId"],
        "configId": "model", "value": "grok-4.6"}
    env.fake("synthetic_text")
    events = await env.run(model="bad-model")
    assert "bad-model" in last_error(events) and "result" not in types(events)


async def test_garbage_model_id_is_refused_before_any_spawn(env):
    events = await env.run(model="grok 4; rm -rf /")
    assert "invalid Grok model id" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


async def test_missing_binary_is_an_error(env, monkeypatch):
    monkeypatch.setenv("GROK_BIN", str(env.tmp / "nope"))
    assert "Grok CLI not found" in last_error(await env.run())


async def test_missing_login_is_an_error_and_nothing_spawns(env):
    (env.home / "auth.json").unlink()
    events = await env.run()
    assert isinstance(only(events, "error")[0]["exc"], GrokAuthError)
    assert "tools/grok-acct login" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


async def test_non_oidc_login_is_refused(env):
    env.write_auth(auth_mode="api_key")
    assert "API-key auth is not allowed" in last_error(await env.run())
    assert not (env.dumps / "argv.json").exists()


async def test_retention_opt_out_must_be_true(env):
    for value in (False, None, "true", 1):
        env.write_auth(coding_data_retention_opt_out=value)
        events = await env.run()
        assert "opted out" in last_error(events), value
        assert not (env.dumps / "argv.json").exists()


async def test_missing_bwrap_is_an_error_never_a_silent_unsandboxed_run(env, monkeypatch):
    monkeypatch.setenv("PATH", str(env.tmp / "empty"))
    events = await env.run()
    assert "bwrap" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


@pytest.mark.parametrize("meta,needle", [
    ({"coding_data_retention_opt_out": False}, "coding_data_retention_opt_out"),
    ({"auth_mode": "ApiKey"}, "auth_mode"),
    ({"backend_billed": True}, "API-billed"),
    ({"email": "someone-else@example.invalid"}, "different account"),
])
async def test_authenticate_response_is_reverified(env, meta, needle):
    env.fake("synthetic_text", auth_meta=json.dumps(meta))
    events = await env.run()
    assert isinstance(only(events, "error")[0]["exc"], GrokAuthError)
    assert needle in last_error(events)
    assert "result" not in types(events)


async def test_sandbox_probe_failure_blocks_runs(env):
    grok_engine._sandbox_verdict.update(state="failed", detail="canary leaked")
    events = await env.run()
    assert "sandbox check failed" in last_error(events) and "canary leaked" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


# ------------------------------------------------------------------------------------------
# what the child gets: argv, cwd, env (D3), session params, rules
# ------------------------------------------------------------------------------------------

async def test_argv_is_agent_no_leader_stdio_and_never_a_sandbox_flag(env):
    await env.run()
    assert env.dump("argv") == ["agent", "--no-leader", "stdio"]
    assert env.dump("cwd") == str(env.cwd)
    assert "--sandbox" not in json.dumps(env.dump("argv"))


async def test_child_env_is_hermetic_and_carries_every_d3_variable(env, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GROK_MEMORY", "1")              # a parent value must not win over D3
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SOME_OTHER_THING", "x")
    await env.run()
    child = env.dump("env")
    for key in SECRETS:
        assert key not in child, key
    assert "SOME_OTHER_THING" not in child
    for key, value in SPEC_D3.items():
        assert child.get(key) == value, key
    assert child["GROK_HOME"] == str(env.home)
    assert child["GROK_SANDBOX"] == "cardloop"
    assert child["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert child["LC_ALL"] == "C.UTF-8" and child["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    allowed = (set(grok_engine._ENV_ALLOW) | set(SPEC_D3) | set(D3_ENV)
               | {"GROK_HOME", "GROK_SANDBOX"})
    stray = {k for k in child if k not in allowed and not k.startswith("LC_")}
    assert not stray, f"variables outside the allowlist reached the child: {stray}"


def test_d3_env_matches_the_spec_block():
    for key, value in SPEC_D3.items():
        assert D3_ENV[key] == value


def test_child_env_without_sandbox_has_no_sandbox_variable(tmp_path):
    env = grok_engine.child_env(tmp_path, sandbox=False, parent={"PATH": "/x", "WEB_PASSWORD": "p"})
    assert "GROK_SANDBOX" not in env and "WEB_PASSWORD" not in env and env["PATH"] == "/x"


async def test_session_new_params_and_rules(env):
    (env.cwd / "CLAUDE.md").write_text("PROJECT-RULE: use tabs\n")
    await env.run(multi_agent=True)
    new = env.sent("session/new")[0]["params"]
    assert new["cwd"] == str(env.cwd) and new["mcpServers"] == []
    assert new["_meta"]["yoloMode"] is True
    rules = new["_meta"]["rules"]
    assert "'proj'" in rules and str(env.cwd) in rules
    assert "PROJECT-RULE: use tabs" in rules
    assert "subagents" in rules
    prompt = env.sent("session/prompt")[0]["params"]
    assert prompt["prompt"] == [{"type": "text", "text": "hello"}]


async def test_multi_agent_hint_only_when_asked(env):
    await env.run()
    assert "subagents" not in env.sent("session/new")[0]["params"]["_meta"]["rules"]


async def test_rules_are_size_capped(env):
    (env.cwd / "CLAUDE.md").write_text("A" * (grok_engine.RULES_MAX_BYTES * 2))
    await env.run()
    rules = env.sent("session/new")[0]["params"]["_meta"]["rules"]
    assert "truncated" in rules
    assert len(rules.encode()) < grok_engine.RULES_MAX_BYTES + 1500


async def test_home_claude_md_is_never_sent(env):
    private = "OPERATOR-PRIVATE-MATERIAL"
    (env.fake_home / "CLAUDE.md").write_text(private)
    # 1. a free chat whose cwd IS $HOME
    await env.run(cwd=str(env.fake_home))
    assert private not in json.dumps(env.sent("session/new"))
    # 2. a project CLAUDE.md that is a symlink to the home file
    env.fake("synthetic_text")
    (env.cwd / "CLAUDE.md").symlink_to(env.fake_home / "CLAUDE.md")
    await env.run()
    assert private not in json.dumps(env.sent("session/new"))
    # 3. a symlink to anything outside the project
    env.fake("synthetic_text")
    (env.cwd / "CLAUDE.md").unlink()
    outside = env.tmp / "outside.md"
    outside.write_text("OUTSIDE-FILE")
    (env.cwd / "CLAUDE.md").symlink_to(outside)
    await env.run()
    assert "OUTSIDE-FILE" not in json.dumps(env.sent("session/new"))


async def test_resume_calls_session_resume_with_the_given_id(env):
    env.fake("synthetic_resume")
    events = await env.run(resume_session_id="sess-abc")
    assert env.sent("session/new") == []
    resume = env.sent("session/resume")[0]["params"]
    assert resume["sessionId"] == "sess-abc" and resume["cwd"] == str(env.cwd)
    assert resume["_meta"]["yoloMode"] is True
    assert only(events, "result")[0]["provider_session_id"] == "sess-abc"
    assert only(events, "text")[0]["text"] == "resumed"


async def test_permission_request_is_answered_allow_once_including_id_zero(env):
    env.fake("synthetic_permission")
    events = await env.run()
    assert types(events)[-1] == "result"
    replies = [m for m in env.log() if m.get("id") == 0 and "method" not in m]
    assert replies and replies[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}


async def test_permission_request_without_options_is_answered_cancelled(env):
    env.fake("synthetic_permission_nooptions")
    await env.run()
    replies = [m for m in env.log() if m.get("id") == 7 and "method" not in m]
    assert replies[0]["result"] == {"outcome": {"outcome": "cancelled"}}


async def test_unknown_server_request_gets_method_not_found(env):
    env.fake("synthetic_unknown_request")
    events = await env.run()
    assert types(events)[-1] == "result"
    replies = [m for m in env.log() if m.get("id") == 9 and "method" not in m]
    assert replies[0]["error"]["code"] == -32601


# ------------------------------------------------------------------------------------------
# GROK_HOME: profile, config, deny list, reaper
# ------------------------------------------------------------------------------------------

def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def covered(deny, path) -> bool:
    """`path` is hidden by the deny list: it IS an entry or lies inside one (a bound parent hides
    its children; the engine never lists both because bwrap cannot mount the child)."""
    p = os.path.realpath(path)
    return any(not grok_engine._is_glob(e) and (p == os.path.realpath(e)
                                                or p.startswith(os.path.realpath(e).rstrip("/") + "/"))
               for e in deny)


def test_ensure_home_writes_a_custom_profile_and_config(env):
    os.chmod(env.home, 0o755)                      # a home someone created world-readable gets tightened
    info = ensure_home(env.ctx)
    prof = _toml(env.home / "sandbox.toml")["profiles"]["cardloop"]
    assert prof["extends"] == "workspace"
    assert str(env.secret_dir) in prof["deny"]
    assert covered(prof["deny"], env.data / "grok-canary")        # the probe's canary is always denied
    assert info["deny"] == prof["deny"] and info["profile"] == "cardloop"
    cfg = _toml(env.home / "config.toml")
    assert cfg["shell_environment_policy"]["inherit"] == "core"
    assert cfg["cli"]["auto_update"] is False
    for name in ("sandbox.toml", "config.toml"):
        assert stat.S_IMODE((env.home / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(env.home.stat().st_mode) == 0o700


def test_ensure_home_is_idempotent_and_repairs_config(env):
    ensure_home(env.ctx)
    before = {n: (env.home / n).stat().st_ino for n in ("sandbox.toml", "config.toml")}
    ensure_home(env.ctx)
    # a rewrite is write-tmp + rename = a NEW inode (an mtime comparison is blind inside one fs tick)
    assert before == {n: (env.home / n).stat().st_ino for n in before}
    (env.home / "config.toml").write_text('[shell_environment_policy]\ninherit = "all"\n')
    ensure_home(env.ctx)
    assert _toml(env.home / "config.toml")["shell_environment_policy"]["inherit"] == "core"


def test_canary_exists_before_the_deny_list_is_built(env):
    # a missing literal is dropped (C7); the canary must therefore be created FIRST or the probe
    # would run against a profile that does not deny it.
    assert not (env.data / "grok-canary").exists()
    info = ensure_home(env.ctx)
    assert covered(info["deny"], env.data / "grok-canary")
    assert (env.data / "grok-canary" / "secret.txt").read_text().startswith("CANARY-")


def test_deny_list_drops_literals_that_do_not_exist(env, monkeypatch):
    present = env.tmp / "present"
    present.mkdir()
    dangling = env.tmp / "dangling-link"
    dangling.symlink_to(env.tmp / "nowhere")
    missing = env.tmp / "missing"
    monkeypatch.setenv("GROK_SANDBOX_DENY",
                       f"{present}, {missing}, {dangling}, **/.env, **/*.pem, /abs/**/secret.key")
    info = ensure_home(env.ctx)
    assert str(present) in info["deny"]
    # a dangling symlink hides nothing and, as a deny entry, makes `grok agent` exit 1 (measured)
    assert str(dangling) not in info["deny"] and str(dangling) in info["skipped"]
    assert str(missing) not in info["deny"] and str(missing) in info["skipped"]
    assert "**/.env" in info["deny"] and "**/*.pem" in info["deny"]   # globs are not existence-checked
    assert "/abs/**/secret.key" in info["deny"]
    assert not missing.exists()                          # and we never created it ourselves


def test_default_deny_list_is_home_relative_and_filtered(env, monkeypatch):
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    for d in (".claude", ".ssh", ".config/gh"):
        (env.fake_home / d).mkdir(parents=True)
    (env.fake_home / ".grok").mkdir()
    (env.fake_home / ".grok" / "auth.json").write_text("{}")
    info = ensure_home(env.ctx)
    deny = info["deny"]
    assert str(env.fake_home / ".claude") in deny and str(env.fake_home / ".ssh") in deny
    assert str(env.fake_home / ".config" / "gh") in deny
    assert str(env.fake_home / ".aws") not in deny       # absent -> dropped, not materialised
    assert not (env.fake_home / ".aws").exists()
    assert "**/.env" in deny and "**/secrets.env" in deny and "**/*.pem" in deny and "**/*.key" in deny
    # the operator's own interactive login is denied, the cockpit's own is not
    assert str(env.fake_home / ".grok" / "auth.json") in deny
    assert not any(e == str(env.fake_home / ".grok") for e in deny)


def test_the_operators_own_login_is_denied_whatever_grok_sandbox_deny_says(env, monkeypatch):
    # it used to vanish from the list as soon as the operator set GROK_SANDBOX_DENY for any other reason
    (env.fake_home / ".grok").mkdir()
    (env.fake_home / ".grok" / "auth.json").write_text("{}")
    for custom in (str(env.secret_dir), "**/.env", f"{env.secret_dir},{env.tmp}/nothing"):
        monkeypatch.setenv("GROK_SANDBOX_DENY", custom)
        assert str(env.fake_home / ".grok" / "auth.json") in ensure_home(env.ctx)["deny"], custom


def test_the_secret_safes_key_and_store_are_denied_wherever_they_live(env, monkeypatch):
    # `**/*.key` is anchored at the workspace: from any other project the Fernet key was readable
    default_dir = env.fake_home / ".config" / "claude-ops"
    default_dir.mkdir(parents=True)
    (default_dir / "secret.key").write_text("k")
    moved_key = env.tmp / "moved" / "safe.key"
    moved_store = env.tmp / "moved" / "safe.enc"
    moved_key.parent.mkdir()
    moved_key.write_text("k")
    moved_store.write_text("s")
    deny, _ = grok_engine.build_deny(env.home, env.ctx)
    assert str(default_dir) in deny and str(moved_key) not in deny
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(moved_key))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(moved_store))
    for custom in (None, str(env.secret_dir)):
        if custom is None:
            monkeypatch.delenv("GROK_SANDBOX_DENY")
        else:
            monkeypatch.setenv("GROK_SANDBOX_DENY", custom)
        deny, _ = grok_engine.build_deny(env.home, env.ctx)
        assert {str(default_dir), str(moved_key), str(moved_store)} <= set(deny), custom


def test_own_home_equal_to_dot_grok_does_not_deny_its_own_login(env, monkeypatch):
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    own = env.fake_home / ".grok"
    own.mkdir()
    (own / "auth.json").write_text("{}")
    monkeypatch.setenv("GROK_HOME", str(own))
    info = ensure_home(env.ctx)
    assert str(own / "auth.json") not in info["deny"]


def test_entries_that_would_hide_the_binary_or_home_are_refused(env, monkeypatch):
    real_bin_dir = os.path.dirname(os.path.realpath(env.wrapper if env.wrapper.exists() else sys.executable))
    for bad in (real_bin_dir, str(env.home), str(env.home.parent), str(env.fake_home), "/"):
        monkeypatch.setenv("GROK_SANDBOX_DENY", bad)
        with pytest.raises(GrokUnavailableError, match="would hide"):
            ensure_home(env.ctx, bin_path=str(env.wrapper))


@pytest.mark.parametrize("entry", ["**/*.{pem,key}", "src//x*", "a/./b*", "../x*", "x\\y*", "**/[]a*",
                                   "**/"])
def test_invalid_deny_entries_are_refused_not_silently_dropped(env, monkeypatch, entry):
    monkeypatch.setenv("GROK_SANDBOX_DENY", entry)
    with pytest.raises(GrokUnavailableError):
        ensure_home(env.ctx)


async def test_a_project_inside_the_deny_list_is_refused_for_that_turn(env, monkeypatch):
    inside = env.secret_dir / "work"
    inside.mkdir()
    events = await env.run(cwd=str(inside))
    assert "inside the sandbox deny list" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


async def test_symlinked_grok_home_is_refused(env):
    real = env.tmp / "real-home"
    real.mkdir()
    shutil_target = env.home
    for f in shutil_target.iterdir():
        f.rename(real / f.name)
    shutil_target.rmdir()
    shutil_target.symlink_to(real)
    with pytest.raises(GrokUnavailableError, match="symlink"):
        ensure_home(env.ctx)
    events = await env.run()
    assert "symlink" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


async def test_litter_reaper_removes_our_placeholders_including_the_mode_000_dir(env):
    env.fake("synthetic_text", litter=1)
    await env.run()
    leftovers = [p.name for p in env.home.iterdir() if p.name.startswith("sandbox-blocked")]
    assert leftovers == []


async def test_litter_reaper_spares_live_pids_and_removes_dead_ones(env):
    env.home.mkdir(exist_ok=True)
    live = env.home / f"sandbox-blocked.{os.getpid()}"            # this test process is alive
    dead_dir = env.home / "sandbox-blocked-dir.4194300"           # a pid that cannot be running
    live.write_text("")
    dead_dir.mkdir()
    os.chmod(dead_dir, 0)
    assert not grok_engine._pid_alive(4194300)
    await env.run()
    names = {p.name for p in env.home.iterdir() if p.name.startswith("sandbox-blocked")}
    assert names == {live.name}


def test_reaper_ignores_lookalike_names(env):
    env.home.mkdir(exist_ok=True)
    keep = env.home / "sandbox-blocked.notapid"
    keep.write_text("")
    keep2 = env.home / "sandbox-bwrap-sentinel"
    keep2.mkdir()
    assert grok_engine.reap_litter(env.home, 1) == []
    assert keep.exists() and keep2.exists()


# ------------------------------------------------------------------------------------------
# ledgers (D9)
# ------------------------------------------------------------------------------------------

async def test_a_usage_row_is_appended_per_turn(env):
    await env.run(model=None, entrypoint="card")
    env.fake("synthetic_text")
    await env.run()
    rows = [json.loads(line) for line in (env.data / "grok_usage.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    row = rows[0]
    assert set(row) == {"ts", "provider", "session_id", "project", "session_key", "entrypoint", "model",
                        "input", "output", "cached", "reasoning", "total", "duration_ms", "notional_usd"}
    assert row["provider"] == "grok" and row["project"] == "proj" and row["entrypoint"] == "card"
    assert (row["input"], row["output"], row["cached"], row["reasoning"], row["total"]) == (2000, 300, 800, 90, 2300)
    assert row["notional_usd"] == round(123456789 / 1e10, 6) and row["model"] == "grok-4.7"
    assert row["session_id"].startswith("fake-session_1-") and isinstance(row["duration_ms"], int)


async def test_a_failed_turn_writes_no_usage_row(env):
    env.fake("synthetic_text", stop_reason="refusal")
    await env.run()
    assert not (env.data / "grok_usage.jsonl").exists()


async def test_a_stopped_turn_still_records_its_usage(env):
    env.fake("synthetic_cancel")
    async for ev in run_grok_engine(**env.kwargs()):
        if ev["type"] == "tool":
            await env.ctx["running"]["p:1"].interrupt()
    assert (env.data / "grok_usage.jsonl").exists()


async def test_quota_shaped_rpc_error_is_captured_raw(env):
    raw = {"code": -32000, "message": "Usage limit reached for SuperGrok. Try again in 3h 12m."}
    env.fake("synthetic_text", prompt_error=json.dumps(raw))
    events = await env.run()
    assert "Usage limit reached" in last_error(events)         # surfaced verbatim
    rows = [json.loads(line) for line in (env.data / "grok_limit_errors.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["source"] == "rpc_error"
    assert json.loads(rows[0]["text"]) == raw                   # the RAW error object, no parsing
    assert rows[0]["project"] == "proj" and rows[0]["model"] == "grok-4.7"


async def test_quota_shaped_exit_captures_stderr_and_ordinary_errors_do_not(env):
    env.fake("synthetic_text", exit_on="session/prompt", stderr_text="HTTP 429 Too Many Requests: rate limit")
    await env.run()
    rows = [json.loads(line) for line in (env.data / "grok_limit_errors.jsonl").read_text().splitlines()]
    assert rows[0]["source"] == "exit" and "429" in rows[0]["text"]
    (env.data / "grok_limit_errors.jsonl").unlink()
    env.fake("synthetic_text", prompt_error=json.dumps({"code": -32603, "message": "internal kaboom"}))
    await env.run()
    assert not (env.data / "grok_limit_errors.jsonl").exists()


async def test_no_secret_value_reaches_a_log_line_or_an_event(env, monkeypatch, capsys):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    # a failing turn is the worst case: stderr tail + rpc error text flow into messages and logs
    env.fake("synthetic_text", leak=SECRETS["WEB_PASSWORD"], exit_on="session/prompt",
             stderr_text=f"token={SECRETS['XAI_API_KEY']}")
    events = await env.run()
    ok_events = None
    env.fake("synthetic_text")
    ok_events = await env.run()
    out = capsys.readouterr()
    blob = out.out + out.err + json.dumps([str(e.get("exc", "")) for e in events + ok_events])
    for name, value in list(SECRETS.items()) + [("access", TOKEN_ACCESS), ("refresh", TOKEN_REFRESH)]:
        assert value not in blob, f"{name} leaked into a log line / event"
    assert "env_keys=" in blob and "WEB_PASSWORD" not in blob     # key NAMES of the CHILD env only
    assert "***" in blob                                          # the redaction actually fired


def test_read_auth_facts_never_returns_a_secret(env):
    facts = grok_engine.read_auth_facts(env.home)
    assert facts == {"present": True, "oidc": True, "retention_opt_out": True, "email": "user@example.invalid"}
    assert TOKEN_ACCESS not in json.dumps(facts) and TOKEN_REFRESH not in json.dumps(facts)


@pytest.mark.parametrize("content", ["not json", "[]", '{"a": 1}', '{"a": {"auth_mode": "api_key"}}'])
def test_read_auth_facts_survives_garbage(env, content):
    (env.home / "auth.json").write_text(content)
    facts = grok_engine.read_auth_facts(env.home)
    assert facts["oidc"] is False and facts["retention_opt_out"] is False


def test_read_auth_facts_refuses_an_oversize_auth_json(env):
    # review-spec095-security #3: auth.json is writable by the model's shell and was slurped uncapped on every
    # turn / probe / doctor run. Valid JSON one byte past the cap reads as "unreadable", never as a login.
    cap = grok_engine.AUTH_MAX_BYTES
    body = json.dumps({"https://auth.x.ai::c": {"auth_mode": "oidc", "coding_data_retention_opt_out": True,
                                                 "email": "user@example.invalid"}})
    (env.home / "auth.json").write_text(body + " " * (cap + 1 - len(body)))
    assert (env.home / "auth.json").stat().st_size == cap + 1
    assert grok_engine.read_auth_facts(env.home)["present"] is False
    (env.home / "auth.json").write_text(body + " " * (cap - len(body)))          # exactly the cap: still fine
    assert grok_engine.read_auth_facts(env.home)["oidc"] is True


def test_read_auth_facts_never_follows_a_symlink(env):
    real = env.tmp / "elsewhere.json"
    real.write_text((env.home / "auth.json").read_text())
    (env.home / "auth.json").unlink()
    (env.home / "auth.json").symlink_to(real)
    assert grok_engine.read_auth_facts(env.home)["present"] is False


def test_read_auth_facts_does_not_block_on_a_fifo_planted_as_auth_json(env):
    import threading
    (env.home / "auth.json").unlink()
    os.mkfifo(env.home / "auth.json")
    out = []
    t = threading.Thread(target=lambda: out.append(grok_engine.read_auth_facts(env.home)), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "read_auth_facts blocked on a FIFO (it runs on the cockpit's event loop)"
    assert out[0]["present"] is False


# ------------------------------------------------------------------------------------------
# provider_info / capabilities
# ------------------------------------------------------------------------------------------

@pytest.fixture
def probed(env, monkeypatch):
    """provider_info with the (model-spending) sandbox probe replaced; counts its calls."""
    calls = []

    async def fake_probe(info):
        calls.append(info)
        return "ok", "canary unreadable, control readable"
    monkeypatch.setattr(grok_engine, "_probe_sandbox_denial", fake_probe)
    env.probe_calls = calls
    return env


async def test_provider_info_available_shape(probed):
    info = await grok_engine.provider_info(force=True)
    assert info["provider"] == "grok" and info["enabled"] and info["available"] and info["error"] is None
    assert info["authenticated"] is True and info["auth_type"] == "oidc"
    assert [m["value"] for m in info["models"]] == ["grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"]
    assert [m["value"] for m in info["models"] if m["default"]] == ["grok-4.7"]
    assert info["models"][0]["reasoning_levels"] == ["low", "medium", "high", "xhigh"]
    assert info["reasoning_levels"] == ["low", "medium", "high", "xhigh"]
    assert info["capabilities"] == grok_engine.capabilities()
    assert info["version"] == "1.0.46" and info["warnings"] == []
    assert info["sandbox"]["profile"] == "cardloop" and info["sandbox"]["probe"] == "ok"


async def test_provider_info_keys_cover_codexs(monkeypatch):
    import codex_engine
    monkeypatch.setenv("CODEX_ENABLED", "false")
    monkeypatch.setenv("GROK_ENABLED", "false")
    codex_keys = set(await codex_engine.provider_info())
    grok_info = await grok_engine.provider_info()
    assert codex_keys <= set(grok_info)
    assert grok_info["enabled"] is False and grok_info["available"] is False
    assert "GROK_ENABLED=false" in grok_info["error"]


async def test_provider_info_is_cached_300s_and_force_reprobes(probed, monkeypatch):
    spawned = []
    real = grok_engine._run_probe_cmd

    async def counting(*a, **k):
        spawned.append(a[1])
        return await real(*a, **k)
    monkeypatch.setattr(grok_engine, "_run_probe_cmd", counting)
    first = await grok_engine.provider_info()
    n = len(spawned)
    assert n == 2 and await grok_engine.provider_info() is first and len(spawned) == n   # cached
    await grok_engine.provider_info(force=True)
    assert len(spawned) == 2 * n


async def test_a_negative_row_is_reprobed_after_the_short_ttl_so_a_fresh_login_shows_up(probed):
    # Regression (2026-10-03): `tools/grok-acct login` runs in ANOTHER process, so the cockpit's
    # cached "not signed in" row stayed for 300 s after a successful login — the picker said off.
    (probed.home / "auth.json").unlink()
    first = await grok_engine.provider_info()
    assert first["available"] is False and "not signed in" in first["error"]
    probed.write_auth()                                           # the operator completes the login
    assert await grok_engine.provider_info() is first             # inside the short TTL: no probe storm
    grok_engine._registry_cache["ts"] -= grok_engine._REGISTRY_FAIL_TTL_SEC + 1
    assert grok_engine._REGISTRY_FAIL_TTL_SEC < 60
    again = await grok_engine.provider_info()                     # NOT force: this is the endpoint's path
    assert again["available"] is True and again is not first


async def test_a_positive_row_keeps_the_long_ttl(probed):
    first = await grok_engine.provider_info(force=True)
    grok_engine._registry_cache["ts"] -= grok_engine._REGISTRY_FAIL_TTL_SEC + 1
    assert await grok_engine.provider_info() is first             # the short TTL is for failures only


async def test_unknown_newer_version_warns_but_stays_available(probed, capsys):
    probed.fake("synthetic_text", version="grok 9.9.9 (abc) [stable]")
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is True and info["version"] == "9.9.9"
    assert "9.9.9" in info["warnings"][0]
    assert "warn:" in capsys.readouterr().out


@pytest.mark.parametrize("setup,needle", [
    ("nobin", "Grok CLI not found"),
    ("badversion", "unrecognised `grok --version`"),
    ("noauth", "not signed in"),
    ("apikey", "API-key auth is not allowed"),
    ("noretention", "opted out"),
    ("nobwrap", "bubblewrap"),
    ("loggedout", "not signed in with grok.com"),
    ("nomodels", "listed no models"),
    ("symlink", "symlink"),
    ("badglob", "GROK_SANDBOX_DENY"),
])
async def test_provider_info_fail_closed_reasons(probed, monkeypatch, setup, needle):
    e = probed
    if setup == "nobin":
        monkeypatch.setenv("GROK_BIN", str(e.tmp / "nope"))
    elif setup == "badversion":
        e.fake("synthetic_text", version="not grok at all")
    elif setup == "noauth":
        (e.home / "auth.json").unlink()
    elif setup == "apikey":
        e.write_auth(auth_mode="api_key")
    elif setup == "noretention":
        e.write_auth(coding_data_retention_opt_out=False)
    elif setup == "nobwrap":
        monkeypatch.setenv("PATH", str(e.tmp / "empty"))
    elif setup == "loggedout":
        e.fake("synthetic_text", models="loggedout")
    elif setup == "nomodels":
        e.fake("synthetic_text", models="empty")
    elif setup == "symlink":
        real = e.tmp / "real-home"
        e.home.rename(real)
        e.home.symlink_to(real)
    elif setup == "badglob":
        monkeypatch.setenv("GROK_SANDBOX_DENY", "**/*.{pem,key}")
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and needle in info["error"], info["error"]
    assert info["enabled"] is True and info["models"] == []


async def test_failed_sandbox_probe_makes_the_provider_unavailable_and_blocks_runs(env, monkeypatch):
    async def leaked(info):
        return "failed", "the sandbox deny list did NOT hide the canary"
    monkeypatch.setattr(grok_engine, "_probe_sandbox_denial", leaked)
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and "sandbox denial probe failed" in info["error"]
    assert "sandbox check failed" in last_error(await env.run())


async def test_inconclusive_probe_also_fails_closed(env, monkeypatch):
    async def inconclusive(info):
        return "inconclusive", "the probe turn never read its control file"
    monkeypatch.setattr(grok_engine, "_probe_sandbox_denial", inconclusive)
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and "inconclusive" in info["error"]


async def test_probe_verdict_is_cached_on_disk_by_fingerprint(probed, monkeypatch):
    await grok_engine.provider_info(force=True)
    await grok_engine.provider_info(force=True)
    assert len(probed.probe_calls) == 1                      # same version + profile: no new model turn
    extra = probed.tmp / "extra-secrets"
    extra.mkdir()
    monkeypatch.setenv("GROK_SANDBOX_DENY", f"{probed.secret_dir},{extra}")
    await grok_engine.provider_info(force=True)
    assert len(probed.probe_calls) == 2                      # a changed deny list changes the fingerprint
    probed.fake("synthetic_text", version="grok 1.0.99 (x) [stable]")
    await grok_engine.provider_info(force=True)
    assert len(probed.probe_calls) == 3                      # a new CLI build re-probes
    grok_engine.reset_sandbox_probe({"DATA": probed.data})
    await grok_engine.provider_info(force=True)
    assert len(probed.probe_calls) == 4


async def test_failed_verdict_is_cached_for_a_short_while_only(env, monkeypatch):
    calls = []

    async def leaked(info):
        calls.append(1)
        return "failed", "leak"
    monkeypatch.setattr(grok_engine, "_probe_sandbox_denial", leaked)
    await grok_engine.provider_info(force=True)
    await grok_engine.provider_info(force=True)
    assert len(calls) == 1                                    # not re-spending a turn per registry read
    cache = env.data / "grok_sandbox_probe.json"
    stale = json.loads(cache.read_text())
    stale["ts"] -= grok_engine.SANDBOX_PROBE_FAIL_TTL_SEC + 5
    cache.write_text(json.dumps(stale))
    await grok_engine.provider_info(force=True)
    assert len(calls) == 2


@pytest.mark.parametrize("control,canary,hay,want", [
    ("C1", "K9", "xx C1 yy rc=1 E5", "ok"),
    ("C1", "K9", "xx C1 yy rc=13 E5", "ok"),
    ("C1", "K9", "xx C1 K9 yy rc=0 E5", "failed"),       # the canary leaked, even next to the control
    ("C1", "K9", "xx K9", "failed"),
    ("C1", "K9", "xx C1 K9", "failed"),                  # leaked and the command died before its last step
    ("C1", "K9", "xx C1 yy rc=0 E5", "inconclusive"),    # the read exited 0 and showed nothing: proves nothing
    ("C1", "K9", "xx C1 yy E5", "inconclusive"),         # the read was never attempted (middle step dropped)
    ("C1", "K9", "xx C1 yy rc=$? E5", "inconclusive"),   # the command TEXT is not an exit status
    ("C1", "K9", "xx C1 yy src=1 E5", "inconclusive"),   # ... nor is a lookalike inside another word
    ("C1", "K9", "Permission denied", "inconclusive"),   # nothing ran: not proof of anything
    ("C1", "K9", "", "inconclusive"),
    ("C1", "K9", "xx rc=1 E5", "inconclusive"),          # the last steps without the first: not our command
    ("C1", "K9", "xx C1 yy", "inconclusive"),            # control read, canary read never reached
])
def test_probe_judge_table(control, canary, hay, want):
    assert grok_engine.judge_probe(control, canary, hay, "E5")[0] == want


def test_probe_judge_says_which_step_was_missing():
    assert "control file" in grok_engine.judge_probe("C1", "K9", "E5", "E5")[1]
    assert "last step" in grok_engine.judge_probe("C1", "K9", "C1", "E5")[1]
    assert "failed canary read" in grok_engine.judge_probe("C1", "K9", "C1 E5", "E5")[1]


def test_haystack_decodes_raw_byte_arrays_and_nested_strings():
    msg = {"a": {"output": [104, 105, 10], "t": ["x", {"y": "deep"}]}, "n": [1, 2000]}
    hay = grok_engine._haystack(msg)
    assert "hi\n" in hay and "deep" in hay


@pytest.mark.parametrize("text,logged_in,default,ids", [
    ("You are logged in with grok.com.\n\nDefault model: grok-4.7\n\nAvailable models:\n"
     "  * grok-4.7 (default)\n  - grok-4.6\n", True, "grok-4.7", ["grok-4.7", "grok-4.6"]),
    ("Not logged in.\n", False, None, []),
    ("You are logged in with grok.com.\nDefault model: b\nAvailable models:\n  - a\n  * b (default)\n",
     True, "b", ["a", "b"]),
    ("You are logged in with grok.com.\nDefault model: b\nAvailable models:\n  - a\n  - b\n",
     True, "b", ["a", "b"]),            # the `Default model:` line wins when no row carries the marker
])
def test_parse_models_output(text, logged_in, default, ids):
    li, d, rows = grok_engine.parse_models_output(text)
    assert (li, d, [r["value"] for r in rows]) == (logged_in, default, ids)
    assert [r["value"] for r in rows if r["default"]] == ([default] if default else [])


def test_capabilities_exact_and_runtime_conflicts():
    import runtime
    caps = grok_engine.capabilities()
    assert caps == {"chat": True, "board": True, "history": True, "search": True, "usage": True,
                    "interrupt": True, "multi_agent": True,
                    "ask_mode": False, "plan_mode": False, "skills": False, "plugins": False}
    rc = SimpleNamespace(provider="grok", ask_mode=True, plan_mode=True, ultracode=True)
    conflicts = runtime.capability_conflicts(rc, caps)
    assert len(conflicts) == 2 and any("ask_mode" in c for c in conflicts) and any("plan_mode" in c for c in conflicts)
    assert runtime.capability_conflicts(SimpleNamespace(provider="grok", ask_mode=False, plan_mode=False,
                                                         ultracode=True), caps) == []


def test_data_dir_and_home_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("GROK_HOME", raising=False)
    assert grok_engine.grok_home({"DATA": tmp_path / "data"}) == tmp_path / "data-grok-home"
    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(tmp_path / "d"))
    assert grok_engine.data_dir() == tmp_path / "d"
    monkeypatch.setenv("GROK_HOME", "~/somewhere")
    assert str(grok_engine.grok_home()).endswith("/somewhere") and "~" not in str(grok_engine.grok_home())


def test_no_machine_specific_paths_in_the_module():
    text = Path(grok_engine.__file__).read_text()
    assert "/home/" not in text and "/Users/" not in text


# ------------------------------------------------------------------------------------------
# every recorded (real) fixture, under the same engine: invariants only
# ------------------------------------------------------------------------------------------

_STD = {"initialize", "authenticate", "session/new", "session/resume", "session/prompt",
        "session/cancel", "session/set_config_option", "session/close"}


def _recorded():
    out = []
    for p in sorted(FIXTURES.glob("*.jsonl")):
        if p.name.startswith("synthetic_"):
            continue
        methods = {json.loads(line)["msg"].get("method") for line in p.read_text().splitlines()
                   if line.strip() and json.loads(line)["dir"] == "c2a"}
        out.append(pytest.param(p.stem, methods, id=p.stem))
    return out


@pytest.mark.parametrize("name,methods", _recorded())
async def test_every_recorded_fixture_replays_cleanly(env, name, methods):
    if (methods - {None}) - _STD:
        pytest.skip(f"{name} scripts a call the turn engine never makes: {sorted(m for m in methods if m not in _STD)}")
    env.fake(name, spawn_child=1, permission_wait=0.3)
    kw = {}
    if "session/resume" in methods:
        kw["resume_session_id"] = "resumed-session-id"
    if "session/set_config_option" in methods:
        kw.update(model="grok-4.6", effort="xhigh")
    seen: list[dict] = []
    interrupted = False
    fired = False

    async def watchdog():
        # a recording that simply stops (the agent never answered the prompt) would otherwise wait
        # forever, exactly as the real agent would: the operator's Stop is the way out
        nonlocal fired
        await asyncio.sleep(6.0)                 # well past the 3 s handshake timeout of this suite
        turn = env.ctx["running"].get("p:1")
        if isinstance(turn, GrokTurn):
            fired = True
            await turn.interrupt()
    wd = asyncio.ensure_future(watchdog())

    async def consume():
        nonlocal interrupted
        async for ev in run_grok_engine(**env.kwargs(**kw)):
            seen.append(ev)
            if "session/cancel" in methods and ev["type"] == "tool" and not interrupted:
                interrupted = True
                await env.ctx["running"]["p:1"].interrupt()
    try:
        await asyncio.wait_for(consume(), 30)
    finally:
        wd.cancel()
    assert seen and seen[-1]["type"] in ("result", "error"), types(seen)
    assert set(types(seen)) <= {"text_delta", "text", "tool", "subagent", "result", "error"}
    assert "".join(e["text"] for e in only(seen, "text_delta")).strip() == \
           "".join(e["text"] for e in only(seen, "text")).strip()
    for ev in only(seen, "tool"):
        assert isinstance(ev["name"], str) and ev["name"] and isinstance(ev["input"], dict)
    if seen[-1]["type"] == "result" and not fired:
        assert seen[-1]["provider_session_id"] and seen[-1]["context_tokens"] >= 0
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"]), "the turn left a process behind"
    assert [p.name for p in env.home.iterdir() if p.name.startswith("sandbox-blocked")] == []


def test_signal_module_is_the_only_kill_path():
    # a regression guard: the engine must never shell out to kill/pkill (the cockpit's own guard
    # hook would block it, and `pkill -f` matches its own shell)
    text = Path(grok_engine.__file__).read_text()
    assert "pkill" not in text and "os.system" not in text and "shell=True" not in text
    assert signal.SIGKILL


# ------------------------------------------------------------------------------------------
# teardown details
# ------------------------------------------------------------------------------------------

async def test_a_process_that_ignores_sigterm_is_sigkilled(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "TERM_WAIT_SEC", 0.6)
    env.fake("synthetic_cancel", spawn_child=1, ignore_term=1, ignore_cancel=1)
    gen = run_grok_engine(**env.kwargs())
    async for ev in gen:
        if ev["type"] == "tool":
            break
    t0 = time.monotonic()
    await gen.aclose()
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])
    assert time.monotonic() - t0 < 4


async def test_healthy_teardown_is_prompt_because_sigterm_comes_first(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "TERM_WAIT_SEC", 3.0)   # a missing SIGTERM would cost the full 3 s
    t0 = time.monotonic()
    events = await env.run()
    assert types(events)[-1] == "result"
    assert time.monotonic() - t0 < 2.5


async def test_teardown_asks_the_agent_to_close_the_session(env):
    await env.run()
    closes = env.sent("session/close")
    assert len(closes) == 1 and closes[0]["params"]["sessionId"].startswith("fake-session_1-")


async def test_an_agent_that_never_answers_close_does_not_stall_teardown(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "CLOSE_WAIT_SEC", 0.4)
    env.fake("synthetic_text", ignore_close=1)
    t0 = time.monotonic()
    assert types(await env.run())[-1] == "result"
    assert time.monotonic() - t0 < 3


async def test_history_replayed_on_resume_is_not_part_of_the_turn(env):
    env.fake("synthetic_resume_replay")
    events = await env.run(resume_session_id="sess-abc")
    blob = json.dumps(events)
    assert "REPLAYED-HISTORY" not in blob and "old" not in [e["input"].get("command") for e in only(events, "tool")]
    assert only(events, "text")[0]["text"] == "fresh"


async def test_interrupt_after_the_turn_finished_is_a_no_op(env):
    turns = []
    async for ev in run_grok_engine(**env.kwargs()):
        turns.append(env.ctx["running"].get("p:1"))
    turn = next(t for t in turns if isinstance(t, GrokTurn))
    await turn.interrupt()           # must neither raise nor signal anything
    assert turn.cancel_requested is False


# ------------------------------------------------------------------------------------------
# second-round additions (each one kills a mutant the first pass left alive)
# ------------------------------------------------------------------------------------------

async def test_a_missing_project_directory_is_named_not_a_spawn_error(env):
    msg = last_error(await env.run(cwd=str(env.tmp / "gone")))
    assert "project directory does not exist" in msg
    assert not (env.dumps / "argv.json").exists()


async def test_the_same_tool_call_announced_twice_is_one_row(env):
    env.fake("synthetic_tool_dup")
    events = await env.run()
    assert [e["input"]["command"] for e in only(events, "tool")] == ["ls", "ls"]   # dup-1 once, dup-2
    assert len(only(events, "tool")) == 2


async def test_the_spawn_log_names_env_keys_only(env, capsys):
    await env.run()
    line = next(x for x in capsys.readouterr().out.splitlines() if "env_keys=" in x)
    keys = eval(line.split("env_keys=", 1)[1])     # a python list literal of NAMES
    assert isinstance(keys, list) and all(isinstance(k, str) for k in keys)
    assert "GROK_HOME" in keys and "PATH" in keys
    assert str(env.home) not in line.split("env_keys=", 1)[1]      # no value of the child env


def test_grok_sandbox_deny_replaces_the_defaults(env, monkeypatch):
    (env.fake_home / ".ssh").mkdir()
    monkeypatch.setenv("GROK_SANDBOX_DENY", str(env.secret_dir))
    deny = ensure_home(env.ctx)["deny"]
    assert str(env.fake_home / ".ssh") not in deny and "**/.env" not in deny
    assert str(env.secret_dir) in deny and covered(deny, env.data / "grok-canary")
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    assert str(env.fake_home / ".ssh") in ensure_home(env.ctx)["deny"]


def test_default_deny_covers_credential_files_that_live_in_home_itself(env, monkeypatch):
    # measured: the model's shell could READ these three under the first-draft default list
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    (env.fake_home / ".grok").mkdir()
    for name in (".claude.json", ".git-credentials", ".bash_history", ".netrc", ".npmrc"):
        (env.fake_home / name).write_text("secret")
    deny = ensure_home(env.ctx)["deny"]
    for name in (".claude.json", ".git-credentials", ".bash_history", ".netrc", ".npmrc"):
        assert str(env.fake_home / name) in deny, name
    assert str(env.fake_home / ".grok") not in deny           # the binary lives there: never the dir


async def test_without_sigterm_a_stubborn_agent_would_cost_the_full_grace(env, monkeypatch):
    # the agent keeps running after stdin closes (only a signal ends it): SIGTERM must come first
    monkeypatch.setattr(grok_engine, "TERM_WAIT_SEC", 3.0)
    env.fake("synthetic_text", ignore_eof=1)
    t0 = time.monotonic()
    assert types(await env.run())[-1] == "result"
    assert time.monotonic() - t0 < 2.0
    pid = env.dump("pids")["leader"]
    assert pid_gone(pid)


async def test_two_concurrent_registry_reads_share_one_probe(env, monkeypatch):
    calls = []

    async def slow_probe(info):
        calls.append(1)
        await asyncio.sleep(0.4)
        return "ok", "fine"
    monkeypatch.setattr(grok_engine, "_probe_sandbox_denial", slow_probe)
    a, b = await asyncio.gather(grok_engine.provider_info(force=True), grok_engine.provider_info(force=True))
    assert a["available"] and b["available"] and len(calls) == 1


async def test_resume_reports_the_window_remembered_from_an_earlier_session(env):
    first = only(await env.run(), "result")[0]
    assert first["context_window"] == 256000
    env.fake("synthetic_resume")
    again = only(await env.run(resume_session_id="sess-abc"), "result")[0]
    assert again["context_window"] == 256000                    # the resume answer carries no `models`
    grok_engine._window_cache.clear()
    env.fake("synthetic_resume")
    cold = only(await env.run(resume_session_id="sess-abc"), "result")[0]
    assert cold["context_window"] is None                       # unknown stays unknown, never invented


def test_a_deny_entry_inside_another_is_dropped_because_bwrap_cannot_mount_it(env, monkeypatch):
    # measured live: `~/.config` + `~/.config/gcloud` makes bwrap fail with
    # "Can't create file ...: Read-only file system" and takes every turn down with it
    parent = env.tmp / "cfg"
    (parent / "gcloud").mkdir(parents=True)
    (parent / "gh").mkdir()
    other = env.tmp / "other"
    other.mkdir()
    alias = env.tmp / "alias-of-other"
    alias.symlink_to(other)
    monkeypatch.setenv("GROK_SANDBOX_DENY", f"{parent}/gcloud,{parent},{parent}/gh,{other},{alias},**/.env")
    deny = ensure_home(env.ctx)["deny"]
    assert str(parent) in deny and str(other) in deny and "**/.env" in deny
    assert f"{parent}/gcloud" not in deny and f"{parent}/gh" not in deny
    assert str(alias) not in deny                       # same realpath as `other`: one entry is enough


async def test_a_ctx_without_a_running_map_still_runs(env):
    ctx = {"DATA": env.data}
    events = [e async for e in run_grok_engine(**env.kwargs(ctx=ctx))]
    assert types(events)[-1] == "result" and ctx["running"]["p:1"] is True


async def test_litter_is_reaped_even_when_the_task_is_cancelled(env):
    env.fake("synthetic_cancel", litter=1, spawn_child=1)
    task = asyncio.ensure_future(_drain(run_grok_engine(**env.kwargs())))
    assert await wait_until(lambda: any(p.name.startswith("sandbox-blocked") for p in env.home.iterdir()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [p.name for p in env.home.iterdir() if p.name.startswith("sandbox-blocked")] == []


async def test_probe_commands_run_in_the_grok_home_not_the_cockpit_cwd(probed):
    seen = []
    real = grok_engine._run_probe_cmd

    async def spy(binary, args, env, timeout, cwd=None):
        seen.append(cwd)
        return await real(binary, args, env, timeout, cwd=cwd)
    probed.mp.setattr(grok_engine, "_run_probe_cmd", spy)
    await grok_engine.provider_info(force=True)
    assert seen == [str(probed.home)] * 2


async def test_a_second_cancel_during_teardown_still_kills_and_reaps(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "TERM_WAIT_SEC", 1.0)
    env.fake("synthetic_cancel", litter=1, spawn_child=1, ignore_term=1, ignore_eof=1)
    task = asyncio.ensure_future(_drain(run_grok_engine(**env.kwargs())))
    assert await wait_until(lambda: any(p.name.startswith("sandbox-blocked") for p in env.home.iterdir()))
    task.cancel()
    await asyncio.sleep(0.3)           # teardown is now waiting out the SIGTERM grace
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])
    assert [p.name for p in env.home.iterdir() if p.name.startswith("sandbox-blocked")] == []


def test_todo_mapping_defaults_status_and_ignores_junk_rows():
    name, inp = grok_engine.map_tool("todo_write", {"todos": [{"content": "x"}, "junk", None, {"id": "9"}]})
    assert name == "TodoWrite"
    assert inp["todos"] == [{"content": "x", "status": "pending", "activeForm": "x"},
                            {"content": "", "status": "pending", "activeForm": ""}]
    assert grok_engine.map_tool("todo_write", {}) == ("TodoWrite", {"todos": []})


def test_tool_inputs_survive_missing_or_non_dict_raw_input():
    assert grok_engine.map_tool("run_terminal_command", None) == ("Bash", {"command": "", "description": ""})
    assert grok_engine.map_tool("grep", "not a dict") == ("Grep", {"pattern": "", "path": ""})
    assert grok_engine.map_tool("brand_new_tool", None) == ("brand_new_tool", {})
    assert grok_engine.map_tool("", {"a": 1}) == ("?", {"a": 1})


# ==========================================================================================
# P1b hardening: project-scoped config, the Claude import surface, wire tripwire, backstops
# ==========================================================================================

def _fixture_with(env, notes: list[dict], *, after: str = "session/new", name: str = "injected") -> str:
    """synthetic_text with extra agent->client notifications inserted right after the client's
    `after` request (session/new = during setup, before the session's own response; session/prompt =
    mid-turn). Returns the absolute fixture path FAKE_GROK_FIXTURE accepts."""
    entries = [json.loads(line) for line in (FIXTURES / "synthetic_text.jsonl").read_text().splitlines()
               if line.strip()]
    pos = next(i for i, e in enumerate(entries) if e["dir"] == "c2a" and e["msg"].get("method") == after)
    extra = [{"dir": "a2c", "msg": {"jsonrpc": "2.0", **n}} for n in notes]
    path = env.tmp / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries[:pos + 1] + extra + entries[pos + 1:]) + "\n")
    return str(path)


def _mcp_servers_updated(*servers):
    return {"method": "_x.ai/mcp/servers_updated", "params": {"mcpServers": list(servers)}}


_SRV = {"name": "tablet", "source": "local", "type": "stdio", "command": "/opt/venv/bin/python",
        "args": ["/opt/android-agent/tablet_mcp.py", "--token", "SECRET-ARG-VALUE"]}
_HOOK_EXEC = {"method": "_x.ai/session_notification", "params": {"sessionId": "SESSION_1", "update": {
    "sessionUpdate": "hook_execution", "event_name": "session_start",
    "runs": [{"name": "project/h:session_start[0].hooks[0]", "status": {"status": "success", "elapsed_ms": 7}}]}}}
_MCP_READY = {"method": "_x.ai/mcp_initialized", "params": {"sessionId": "SESSION_1", "mcpToolCount": 2,
                                                            "elapsedMs": 24}}
_MCP_TOOLS = {"method": "session/update", "params": {"sessionId": "SESSION_1", "update": {
    "sessionUpdate": "available_commands_update", "availableCommands": [],
    "_meta": {"tools": ["read_file", "grep", "tablet__take_screenshot"]}}}}


@pytest.mark.parametrize("label,note", [
    ("servers_updated", _mcp_servers_updated(_SRV)),
    ("mcp_initialized", _MCP_READY),
    ("hook_execution", _HOOK_EXEC),
    ("mcp_tool_names", _MCP_TOOLS),
])
async def test_a_setup_time_mcp_or_hook_signal_refuses_the_turn_before_the_prompt(env, label, note):
    env.fake(_fixture_with(env, [note]), spawn_child=1)
    events = await env.run()
    assert types(events) == ["error"], (label, types(events))
    assert isinstance(events[0]["exc"], grok_engine.GrokIsolationError)
    assert "granted none" in last_error(events) or "none is granted" in last_error(events)
    assert env.sent("session/prompt") == []              # the model never got a prompt
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def test_the_first_signal_is_the_one_reported(env):
    env.fake(_fixture_with(env, [_mcp_servers_updated(_SRV), _HOOK_EXEC, _MCP_READY]))
    msg = last_error(await env.run())
    assert "tablet" in msg and "hook" not in msg


async def test_the_tripwire_names_the_server_but_never_its_args_or_env(env):
    env.fake(_fixture_with(env, [_mcp_servers_updated(_SRV)]))
    msg = last_error(await env.run())
    assert "tablet" in msg
    assert "SECRET-ARG-VALUE" not in msg and "tablet_mcp.py" not in msg and "/opt/venv" not in msg


async def test_benign_mcp_notifications_do_not_trip_the_wire(env):
    # what a clean session really sends: an EMPTY server list, zero MCP tools, no hook
    clean = [_mcp_servers_updated(),
             {"method": "_x.ai/mcp_initialized", "params": {"sessionId": "SESSION_1", "mcpToolCount": 0,
                                                            "elapsedMs": 0}},
             {"method": "session/update", "params": {"sessionId": "SESSION_1", "update": {
                 "sessionUpdate": "available_commands_update", "availableCommands": [],
                 "_meta": {"tools": ["read_file", "run_terminal_command", "use_tool", "search_tool"]}}}},
             {"method": "_x.ai/session_notification", "params": {"sessionId": "SESSION_1", "update": {
                 "sessionUpdate": "model_changed"}}}]
    env.fake(_fixture_with(env, clean))
    events = await env.run()
    assert types(events)[-1] == "result" and "error" not in types(events)


async def test_malformed_wire_notifications_never_crash_the_reader(env):
    junk = [{"method": "_x.ai/mcp/servers_updated", "params": {"mcpServers": "nope"}},
            {"method": "_x.ai/mcp/servers_updated", "params": None},
            {"method": "_x.ai/mcp_initialized", "params": {"mcpToolCount": True}},
            {"method": "_x.ai/mcp_initialized", "params": {"mcpToolCount": "3"}},
            {"method": "_x.ai/session_notification", "params": {"update": "x"}},
            {"method": "session/update", "params": {"update": {"sessionUpdate": "available_commands_update",
                                                                "_meta": {"tools": "tablet__x"}}}}]
    env.fake(_fixture_with(env, junk))
    assert types(await env.run())[-1] == "result"


async def test_a_mid_turn_mcp_signal_aborts_the_turn_and_yields_nothing_after_it(env):
    env.fake(_fixture_with(env, [_MCP_READY], after="session/prompt"), spawn_child=1)
    events = await env.run()
    assert "result" not in types(events) and "text" not in types(events)
    assert isinstance(only(events, "error")[-1]["exc"], grok_engine.GrokIsolationError)
    assert len(env.sent("session/prompt")) == 1           # it WAS sent; the abort came with the signal
    pids = env.dump("pids")
    assert pid_gone(pids["leader"]) and pid_gone(pids["child"])


async def test_an_isolation_violation_does_not_flip_the_registry_row(env):
    env.fake(_fixture_with(env, [_mcp_servers_updated(_SRV)]))
    grok_engine._registry_cache.update(ts=time.time(), data=grok_engine._info(True, True, None))
    await env.run()
    assert grok_engine._registry_cache["data"]["available"] is True     # a project problem, not an outage


# ---- folder trust store ------------------------------------------------------------------

async def test_a_populated_trust_store_refuses_the_turn_and_never_spawns(env):
    (env.home / "trusted_folders.toml").write_text('[[folders]]\npath = "/somewhere/project"\n')
    events = await env.run()
    assert isinstance(events[-1]["exc"], grok_engine.GrokIsolationError)
    assert "trusted_folders.toml" in last_error(events) and "/somewhere/project" not in last_error(events)
    assert not (env.dumps / "argv.json").exists()


async def test_an_empty_or_comment_only_trust_store_is_fine(env):
    for body in ("", "\n\n", "# nothing trusted\n  # still nothing\n"):
        (env.home / "trusted_folders.toml").write_text(body)
        assert types(await env.run())[-1] == "result", repr(body)


async def test_an_unreadable_trust_store_fails_closed(env):
    path = env.home / "trusted_folders.toml"
    path.write_text("")
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        events = await env.run()
        assert isinstance(events[-1]["exc"], grok_engine.GrokIsolationError)
        assert "unknown" in last_error(events)
    finally:
        os.chmod(path, 0o600)


# ---- the Claude import surface is denied whatever GROK_SANDBOX_DENY says -------------------

def _import_surface(env):
    for d in (".claude", ".claude-accounts", ".cursor"):
        (env.fake_home / d).mkdir()
    (env.fake_home / ".claude.json").write_text("{}")


def test_the_claude_import_surface_is_denied_even_with_a_custom_deny_list(env):
    _import_surface(env)                                  # GROK_SANDBOX_DENY = secret_dir only (fixture)
    info = ensure_home(env.ctx)
    for d in (".claude", ".claude-accounts", ".cursor", ".claude.json"):
        assert str(env.fake_home / d) in info["deny"], d
    assert str(env.secret_dir) in info["deny"]
    toml = tomllib.loads((env.home / "sandbox.toml").read_text())
    assert str(env.fake_home / ".claude") in toml["profiles"]["cardloop"]["deny"]


def test_the_floor_and_the_defaults_do_not_duplicate_entries(env, monkeypatch):
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    _import_surface(env)
    deny = ensure_home(env.ctx)["deny"]
    for d in (".claude", ".claude-accounts", ".cursor", ".claude.json"):
        assert deny.count(str(env.fake_home / d)) == 1, d


def test_default_deny_covers_the_other_vendor_credential_homes(env, monkeypatch):
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    for d in (".azure", ".oci", ".codex", ".cursor", ".claude"):
        (env.fake_home / d).mkdir()
    deny = ensure_home(env.ctx)["deny"]
    for d in (".azure", ".oci", ".codex", ".cursor"):
        assert str(env.fake_home / d) in deny, d
    skipped = ensure_home(env.ctx)["skipped"]
    assert str(env.fake_home / ".kube") in skipped        # absent ones are still dropped, not created
    assert not (env.fake_home / ".kube").exists()


def test_a_custom_list_still_replaces_the_non_floor_defaults(env):
    (env.fake_home / ".azure").mkdir()
    (env.fake_home / ".ssh").mkdir()
    deny = ensure_home(env.ctx)["deny"]                   # GROK_SANDBOX_DENY = secret_dir only
    assert str(env.fake_home / ".azure") not in deny and str(env.fake_home / ".ssh") not in deny


def test_the_floor_never_denies_the_grok_home_directory(env):
    _import_surface(env)
    assert not any(e == str(env.home) or e.endswith("/.grok") for e in ensure_home(env.ctx)["deny"])


# ---- config.toml: skills + forbidden tables -----------------------------------------------

def test_config_toml_hides_agents_skills_and_disables_the_importer_skills(env):
    ensure_home(env.ctx)
    cfg = tomllib.loads((env.home / "config.toml").read_text())
    assert cfg["skills"]["ignore"] == [str(env.fake_home / ".agents")]      # $HOME-relative, not hardcoded
    assert cfg["skills"]["disabled"] == ["resume-claude", "resume-codex", "resume-cursor"]
    assert cfg["cli"]["auto_update"] is False and cfg["shell_environment_policy"]["inherit"] == "core"


@pytest.mark.parametrize("stale", [
    '[cli]\nauto_update = false\n[shell_environment_policy]\ninherit = "core"\n',          # no [skills]
    '[cli]\nauto_update = false\n[shell_environment_policy]\ninherit = "core"\n'
    '[skills]\nignore = ["/elsewhere"]\ndisabled = ["resume-claude", "resume-codex", "resume-cursor"]\n',
    '[cli]\nauto_update = false\n[shell_environment_policy]\ninherit = "core"\n'
    '[skills]\nignore = []\ndisabled = []\n',
])
def test_a_config_without_the_skills_switches_is_regenerated(env, stale):
    env.home.mkdir(exist_ok=True)
    (env.home / "config.toml").write_text(stale)
    ensure_home(env.ctx)
    cfg = tomllib.loads((env.home / "config.toml").read_text())
    assert cfg["skills"]["ignore"] == [str(env.fake_home / ".agents")]
    assert cfg["skills"]["disabled"] == ["resume-claude", "resume-codex", "resume-cursor"]


@pytest.mark.parametrize("disabled", ['[]', '["resume-claude"]', '["resume-claude", "resume-codex"]',
                                      '["resume-cursor", "resume-codex", "resume-claude", "x"]'])
def test_a_config_with_the_right_ignore_but_a_wrong_disabled_list_is_regenerated(env, disabled):
    env.home.mkdir(exist_ok=True)
    (env.home / "config.toml").write_text(
        '[cli]\nauto_update = false\n[shell_environment_policy]\ninherit = "core"\n'
        f'[skills]\nignore = ["{env.fake_home / ".agents"}"]\ndisabled = {disabled}\n')
    ensure_home(env.ctx)
    cfg = tomllib.loads((env.home / "config.toml").read_text())
    assert cfg["skills"]["ignore"] == [str(env.fake_home / ".agents")]
    assert cfg["skills"]["disabled"] == ["resume-claude", "resume-codex", "resume-cursor"]


@pytest.mark.parametrize("table", ["folder_trust", "mcp_servers", "disabled_mcp_servers", "compat",
                                   "hooks", "plugins", "marketplace"])
def test_a_config_that_widens_the_tool_surface_is_regenerated(env, table):
    ensure_home(env.ctx)
    path = env.home / "config.toml"
    path.write_text(path.read_text() + f"\n[{table}]\nenabled = false\n")
    ensure_home(env.ctx)
    assert table not in tomllib.loads(path.read_text())


def test_unrelated_extra_config_keys_survive(env):
    ensure_home(env.ctx)
    path = env.home / "config.toml"
    path.write_text(path.read_text() + '\n[hints]\nnew_session_worktree_mode = "never"\n')
    ensure_home(env.ctx)
    assert tomllib.loads(path.read_text())["hints"] == {"new_session_worktree_mode": "never"}


# ---- the child env can never ungate folder trust -------------------------------------------

def test_a_parent_folder_trust_switch_never_reaches_the_child(env):
    for hostile in ("0", "false", "off"):
        got = grok_engine.child_env(env.home, parent={"GROK_FOLDER_TRUST": hostile, "PATH": "/usr/bin",
                                                       "GROK_CONFIG": '{"folder_trust": false}',
                                                       "GROK_CLAUDE_MCPS_ENABLED": "1"})
        assert got["GROK_FOLDER_TRUST"] == "1" and got["GROK_CLAUDE_MCPS_ENABLED"] == "0"
        assert "GROK_CONFIG" not in got


# ---- $HOME and its ancestors are a workspace like any other (no per-project gate) ------------

@pytest.mark.parametrize("where", ["home", "parent", "symlink-to-home"])
async def test_a_chat_rooted_at_home_or_above_runs_like_any_other_project(env, where):
    # choosing Grok in the picker IS the consent, as for Codex and Claude: a free chat (cwd = $HOME) works
    target = {"home": env.fake_home, "parent": env.tmp, "symlink-to-home": env.tmp / "link-home"}[where]
    if where == "symlink-to-home":
        target.symlink_to(env.fake_home)
    events = await env.run(cwd=str(target))
    assert types(events)[-1] == "result" and not only(events, "error"), where


async def test_the_old_allow_all_knob_is_gone(env, monkeypatch):
    assert not hasattr(grok_engine, "allow_all_projects") and not hasattr(grok_engine, "_is_home_or_ancestor")
    monkeypatch.setenv("GROK_ALLOW_ALL_PROJECTS", "false")             # a stale .env line changes nothing
    assert types(await env.run(cwd=str(env.fake_home)))[-1] == "result"


# ---- cancellationCategory: every unrequested cancel is an error, a requested one is clean ---

def _prompt_complete(category: str | None, **ctx):
    p = {"sessionId": "SESSION_1", "promptId": "UUID_3", "stopReason": "cancelled", "agentResult": None}
    if category:
        p["cancellationCategory"] = category
    if ctx:
        p["cancellationContext"] = ctx
    return {"method": "_x.ai/session/prompt_complete", "params": p}


@pytest.mark.parametrize("fixture,category", [
    ("permission_request_no_yolo_reject", "PermissionRejected"),
    ("permission_request_no_yolo_error32601", "PermissionRejected"),
    ("permission_request_no_yolo_outcome_cancelled", "PermissionCancelled"),
])
async def test_every_recorded_unrequested_cancel_becomes_an_error(env, fixture, category):
    env.fake(fixture, permission_wait=0.3)
    events = await env.run()
    assert "result" not in types(events)
    msg = last_error(events)
    assert "cancelled the turn unprompted" in msg and f"cancellationCategory={category}" in msg


async def test_an_unrequested_cancel_without_a_category_is_still_an_error(env):
    env.fake("synthetic_cancelled_unprompted")
    msg = last_error(await env.run())
    assert "cancelled the turn unprompted" in msg and "cancellationCategory" not in msg


@pytest.mark.parametrize("fixture", ["cancel_mid_tool", "permission_request_no_yolo_cancel_pending"])
async def test_a_requested_cancel_is_a_clean_result_whatever_the_recording(env, fixture, capsys):
    env.fake(fixture, permission_wait=0.3)
    seen = []
    interrupted = False
    async for ev in run_grok_engine(**env.kwargs()):
        seen.append(ev)
        if ev["type"] == "tool" and not interrupted:
            interrupted = True
            await env.ctx["running"]["p:1"].interrupt()
    assert interrupted and types(seen)[-1] == "result" and "error" not in types(seen)
    assert "also reports" not in capsys.readouterr().out   # MidTurnAbort IS our stop: nothing to flag


async def test_a_requested_stop_that_races_a_permission_rejection_stays_clean_and_is_journaled(env, capsys):
    path = _fixture_with(env, [], name="race")
    entries = [json.loads(line) for line in Path(path).read_text().splitlines()]
    # a cancelled prompt response that carries a PermissionRejected category: our Stop raced a reject
    entries = [e for e in entries if not (e["dir"] == "a2c" and e["msg"].get("method") == "session/update"
                                           and e["msg"]["params"]["update"].get("sessionUpdate") == "agent_message_chunk")]
    resp = next(i for i, e in enumerate(entries) if e["dir"] == "a2c" and e["msg"].get("id") == 4)
    entries[resp]["msg"]["result"]["stopReason"] = "cancelled"
    entries.insert(resp, {"dir": "a2c", "msg": {"jsonrpc": "2.0", **_prompt_complete("PermissionRejected",
                                                                                      tool_name="write")}})
    race = env.tmp / "race2.jsonl"
    race.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    env.fake(str(race))
    # the flag is set before the agent's reply could matter: emulate "operator pressed Stop first"
    orig = grok_engine.GrokTurn.__init__

    def init(self, key):
        orig(self, key)
        self.cancel_requested = True
    env.mp.setattr(grok_engine.GrokTurn, "__init__", init)
    events = await env.run()
    assert types(events)[-1] == "result" and "error" not in types(events)
    assert "cancellationCategory=PermissionRejected" in capsys.readouterr().out


# ---- more than two concurrent turns on ONE GROK_HOME (spec-095 P1 UNVERIFIED (b)) ------------

def _litter_pids(home: Path) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for e in home.iterdir():
        m = grok_engine._LITTER_RE.match(e.name)
        if m:
            out.setdefault(int(m.group(1)), set()).add(e.name)
    return out


async def test_four_concurrent_turns_on_one_home_never_reap_a_live_turns_placeholders(env):
    N = 4
    env.fake("synthetic_cancel", litter=1, spawn_child=1)    # every turn parks mid-tool until cancelled
    keys = [f"p:{i}" for i in range(N)]
    dead = 999_990
    while grok_engine._pid_alive(dead):
        dead += 1
    (env.home / f"sandbox-blocked.{dead}").write_text("")           # a crashed earlier spawn's leftover
    os.chmod(env.home / f"sandbox-blocked.{dead}", 0)
    tasks = {k: asyncio.ensure_future(_drain(run_grok_engine(**env.kwargs(session_key=k, cwd=str(env.cwd)))))
             for k in keys}

    def parked():
        turns = [env.ctx["running"].get(k) for k in keys]
        return all(isinstance(t, GrokTurn) and t.prompt_started for t in turns)
    assert await wait_until(parked, timeout=15)
    live = {env.ctx["running"][k]._acp.proc.pid for k in keys}
    assert len(live) == N
    assert await wait_until(lambda: live <= set(_litter_pids(env.home)), timeout=5)
    for k in keys:
        grok_engine.reap_litter(env.home, None)             # a stray reaper pass from "another turn"
    held = _litter_pids(env.home)
    assert live <= set(held) and all(len(held[p]) == 2 for p in live), held   # all 8 placeholders intact

    results = {}
    for k in keys:                                           # stop them one at a time
        before = set(_litter_pids(env.home))
        await env.ctx["running"][k].interrupt()
        results[k] = await asyncio.wait_for(tasks[k], 15)
        still = set(_litter_pids(env.home))
        done_pid = (before - still)
        survivors = {env.ctx["running"][o]._acp.proc.pid for o in keys
                     if o != k and not tasks[o].done()}
        assert survivors <= still, f"turn {k} reaped a live turn's placeholders"
        assert dead not in still                              # the dead leftover went with the first reaper
        assert len(done_pid) <= 1 + (dead in before)
    for k, evs in results.items():
        assert types(evs)[-1] == "result" and "error" not in types(evs), (k, types(evs))
    assert _litter_pids(env.home) == {}
    assert [p for p in live if grok_engine._pid_alive(p)] == []


async def test_concurrent_turns_do_not_trip_over_each_others_home_files(env):
    N = 4
    env.fake("synthetic_text", litter=1)
    runs = await asyncio.gather(*[env.run(session_key=f"p:{i}") for i in range(N)])
    assert all(types(r)[-1] == "result" for r in runs)
    assert sorted(p.name for p in env.home.iterdir() if p.name.endswith(".tmp")) == []   # no orphan temp files
    assert tomllib.loads((env.home / "sandbox.toml").read_text())["profiles"]["cardloop"]["deny"]
    assert _litter_pids(env.home) == {}


# ---- tools/grok-acct status -----------------------------------------------------------------

def _acct(env, *args):
    import subprocess
    root = Path(__file__).resolve().parent.parent
    run_env = {"PATH": os.environ["PATH"], "HOME": str(env.fake_home), "GROK_HOME": str(env.home),
               "GROK_BIN": str(env.wrapper), "_CARDLOOP_DATA_DIR": str(env.data), "GROK_ENABLED": "true"}
    return subprocess.run([str(root / "tools" / "grok-acct"), *args], env=run_env, capture_output=True,
                          text=True, timeout=30)


def test_acct_status_reports_an_empty_trust_store_as_ok(env):
    out = _acct(env, "status")
    assert out.returncode == 0, out.stderr
    assert "trust     : no folder trusted" in out.stdout and "verdict   : OK" in out.stdout


def test_acct_status_is_blocked_by_a_trusted_folder_and_never_prints_the_entry(env):
    (env.home / "trusted_folders.toml").write_text('[[folders]]\npath = "/somewhere/secret-project"\n')
    out = _acct(env, "status")
    assert out.returncode == 1 and "trust     : BLOCKED" in out.stdout and "verdict   : BLOCKED" in out.stdout
    assert "secret-project" not in out.stdout + out.stderr


async def test_provider_info_is_unavailable_while_a_folder_is_trusted(env):
    (env.home / "trusted_folders.toml").write_text('[[folders]]\npath = "/p"\n')
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and "trusted_folders.toml" in info["error"]
    assert not (env.dumps / "argv.json").exists()           # not even `grok --version` was run


async def test_provider_info_with_an_empty_trust_store_gets_past_that_check(env):
    (env.home / "trusted_folders.toml").write_text("")
    info = await grok_engine.provider_info(force=True)
    assert "trusted_folders.toml" not in str(info["error"])


# ------------------------------------------------------------------------------------------
# spec-095 P7a: the cockpit's own data dir is hidden from the model's shell, always
# ------------------------------------------------------------------------------------------

def _home_in_data(env, monkeypatch, *parts) -> Path:
    """GROK_HOME INSIDE the data dir: the layout that used to be the default and is now refused."""
    home = env.data.joinpath(*(parts or ("grok-home",)))
    monkeypatch.setenv("GROK_HOME", str(home))
    env.home = home
    env.write_auth()
    return home


def test_a_home_outside_the_data_dir_hides_the_whole_data_dir_with_one_entry(env):
    (env.data / "sessions.json").write_text("{}")
    (env.data / "grok_sent").mkdir()
    info = ensure_home(env.ctx)
    real = os.path.realpath(env.data)
    assert info["deny"].count(real) == 1
    assert not any(e.startswith(real + "/") for e in info["deny"])      # nothing nested inside it
    assert covered(info["deny"], env.data / "grok-canary") and covered(info["deny"], env.data / "sessions.json")
    assert real in _toml(env.home / "sandbox.toml")["profiles"]["cardloop"]["deny"]


def test_the_data_dir_is_denied_whatever_grok_sandbox_deny_says(env, monkeypatch):
    for custom in (str(env.secret_dir), "**/.env", f"{env.secret_dir},{env.tmp}/nothing"):
        monkeypatch.setenv("GROK_SANDBOX_DENY", custom)
        assert os.path.realpath(env.data) in ensure_home(env.ctx)["deny"], custom
    monkeypatch.delenv("GROK_SANDBOX_DENY")
    assert os.path.realpath(env.data) in ensure_home(env.ctx)["deny"]


def test_a_data_dir_named_in_grok_sandbox_deny_is_not_listed_twice(env, monkeypatch):
    monkeypatch.setenv("GROK_SANDBOX_DENY", f"{env.data},{env.secret_dir}")
    deny = ensure_home(env.ctx)["deny"]
    assert deny.count(os.path.realpath(env.data)) == 1 and str(env.secret_dir) in deny


def test_the_root_directory_as_data_dir_is_never_hidden(env):
    deny, _ = grok_engine.build_deny(env.home, {"DATA": Path("/")})
    assert "/" not in deny


def test_the_data_entry_is_one_stable_directory_whatever_appears_in_it(env, monkeypatch):
    # a mount over a FILE is detached when the cockpit renames a temp file over it (reproduced with bwrap),
    # and a per-child list let the model create new names: the entry is the directory, so nothing in
    # it changes the deny list — or the probe fingerprint, which would cost a model turn
    first = ensure_home(env.ctx)
    for name in ("chats.json", "chats.json.tmp", "crash-recovery-state.json", "secrets.env"):
        (env.data / name).write_text("{}")
    (env.data / "link").symlink_to(env.secret_dir)
    (env.data / "chat-media").mkdir()
    second = ensure_home(env.ctx)
    assert second["deny"] == first["deny"]
    assert grok_engine._probe_fingerprint("1.0.46", second) == grok_engine._probe_fingerprint("1.0.46", first)
    assert not any(e.startswith(os.path.realpath(env.data) + "/") for e in second["deny"])


@pytest.mark.parametrize("parts", [("grok-home",), ("state", "x", "grok-home")])
def test_a_home_inside_the_data_dir_is_refused_with_the_reason(env, monkeypatch, parts):
    _home_in_data(env, monkeypatch, *parts)
    with pytest.raises(GrokUnavailableError, match="GROK_HOME .* is inside the cockpit data dir"):
        ensure_home(env.ctx)
    info = asyncio.run(grok_engine.provider_info(force=True))
    assert info["available"] is False and "inside the cockpit data dir" in info["error"]
    events = asyncio.run(env.run())
    assert "inside the cockpit data dir" in last_error(events)
    assert not (env.dumps / "argv.json").exists()                       # no process was started


# ---- GROK_HOME's model-writable instruction layers are swept before every turn ---------------------------
# MEASURED live (tests/test_grok_live.py): a turn in project A writes rules/, AGENTS.md, skills/, agents/,
# lsp.json, settings.json into GROK_HOME and a turn in project B then loads them as global user rules.

def _plant(home: Path, name: str) -> None:
    path = home / name
    if name.endswith((".md", ".json")):
        path.write_text("planted")
    else:
        (path / "nested" / "deeper").mkdir(parents=True)
        (path / "nested" / "deeper" / "x.md").write_text("planted")
        (path / "top.md").write_text("planted")


def test_the_swept_names_include_everything_measured_writable_and_loaded():
    # the six a live turn was MEASURED to write and a later turn to load (spec-095 P7), plus the documented rest
    measured = {"rules", "AGENTS.md", "skills", "agents", "lsp.json", "settings.json"}
    documented = {"CLAUDE.md", "GROK.md", "personas", "workflows", "commands", "plugins"}
    assert set(grok_engine.FOREIGN_LAYERS) == measured | documented
    # never the code layers the CLI write-protects itself, nor anything the engine/CLI needs
    assert not set(grok_engine.FOREIGN_LAYERS) & {"hooks", "hooks-paths", "config.toml", "sandbox.toml",
                                                  "managed_config.toml", "requirements.toml",
                                                  "trusted_folders.toml", "auth.json", "sessions", "memory"}


@pytest.mark.parametrize("name", sorted(["rules", "AGENTS.md", "CLAUDE.md", "GROK.md", "skills", "agents",
                                         "personas", "workflows", "commands", "plugins", "lsp.json",
                                         "settings.json"]))
def test_every_foreign_layer_is_removed_before_a_turn(env, name):
    _plant(env.home, name)
    info = ensure_home(env.ctx)
    assert not os.path.lexists(env.home / name), name
    assert info["home"] == env.home


def test_the_sweep_leaves_everything_the_engine_and_the_cli_own_alone(env):
    for keep in ("sessions", "hooks", "memory", "logs", "installed-plugins", "bundled"):
        (env.home / keep).mkdir()
        (env.home / keep / "f").write_text("x")
    (env.home / "hooks-paths").write_text("")
    (env.home / "managed_config.toml").write_text("# fleet\n")
    (env.home / "notes.md").write_text("x")
    for name in grok_engine.FOREIGN_LAYERS:
        _plant(env.home, name)
    ensure_home(env.ctx)
    for keep in ("sessions", "hooks", "memory", "logs", "installed-plugins", "bundled"):
        assert (env.home / keep / "f").read_text() == "x", keep
    assert (env.home / "hooks-paths").exists() and (env.home / "managed_config.toml").read_text() == "# fleet\n"
    assert (env.home / "notes.md").exists() and (env.home / "auth.json").is_file()
    assert _toml(env.home / "config.toml") and _toml(env.home / "sandbox.toml")


def test_the_sweep_deletes_a_symlink_never_what_it_points_at(env):
    outside = env.tmp / "operators-real-rules"
    outside.mkdir()
    (outside / "keep.md").write_text("mine")
    (env.home / "rules").symlink_to(outside)
    (env.home / "AGENTS.md").symlink_to(outside / "keep.md")
    ensure_home(env.ctx)
    assert not os.path.lexists(env.home / "rules") and not os.path.lexists(env.home / "AGENTS.md")
    assert (outside / "keep.md").read_text() == "mine"


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_the_sweep_never_follows_a_symlink_nested_inside_a_layer(env, depth):
    # review-spec095-security #2 (confirmed): os.walk lists a symlinked directory in `dirs` and os.chmod follows
    # it, so a link a model planted INSIDE rules/ got its TARGET chmod'ed by the (unsandboxed) cockpit
    victim = env.tmp / "operators-real-dir"
    victim.mkdir()
    (victim / "keep.md").write_text("mine")
    os.chmod(victim, 0o755)
    nest = env.home / "rules"
    for i in range(depth - 1):
        nest = nest / f"d{i}"
    nest.mkdir(parents=True)
    (nest / "link").symlink_to(victim)
    (nest / "filelink").symlink_to(victim / "keep.md")
    ensure_home(env.ctx)
    assert not os.path.lexists(env.home / "rules")
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755
    assert (victim / "keep.md").read_text() == "mine"


def test_the_sweep_leaves_a_mode_000_target_of_a_nested_symlink_locked(env):
    victim = env.tmp / "operators-private-dir"
    victim.mkdir()
    os.chmod(victim, 0)
    try:
        (env.home / "skills" / "a").mkdir(parents=True)
        (env.home / "skills" / "a" / "link").symlink_to(victim)
        ensure_home(env.ctx)
        assert not os.path.lexists(env.home / "skills")
        assert stat.S_IMODE(victim.stat().st_mode) == 0
    finally:
        os.chmod(victim, 0o700)


def test_the_sweep_removes_a_layer_a_model_made_unreadable(env):
    deep = env.home / "skills" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text("planted")
    os.chmod(deep, 0)
    os.chmod(env.home / "skills" / "a", 0o500)
    ensure_home(env.ctx)
    assert not os.path.lexists(env.home / "skills")


def test_the_sweep_removes_a_layer_whose_top_directory_the_model_made_unreadable(env):
    (env.home / "rules").mkdir()
    (env.home / "rules" / "planted.md").write_text("planted")
    os.chmod(env.home / "rules", 0)
    ensure_home(env.ctx)
    assert not os.path.lexists(env.home / "rules")


def test_the_legacy_login_hint_follows_the_run_ctx_not_the_environment(env, monkeypatch):
    # the cockpit's ctx["DATA"] and the process env can name different dirs (provider_info has no ctx)
    other = env.tmp / "ctx-data"
    other.mkdir()
    (other / "grok-home").mkdir()
    (other / "grok-home" / "auth.json").write_text("{}")
    env.ctx["DATA"] = other
    monkeypatch.delenv("GROK_HOME")
    events = asyncio.run(env.run())
    msg = last_error(events)
    assert str(other / "grok-home") in msg and str(env.data / "grok-home") not in msg


def test_a_layer_that_cannot_be_removed_makes_the_provider_unavailable_not_silently_loaded(env, monkeypatch):
    _plant(env.home, "rules")
    monkeypatch.setattr(grok_engine.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    with pytest.raises(GrokUnavailableError, match="cannot remove .*rules"):
        ensure_home(env.ctx)
    events = asyncio.run(env.run())
    assert "cannot remove" in last_error(events)
    assert not (env.dumps / "argv.json").exists()                  # no process was started


def test_the_sweep_runs_again_before_every_turn_and_journals_what_it_removed(env, capsys):
    asyncio.run(env.run())
    for _ in range(2):                                              # a model plants between turns, twice
        _plant(env.home, "rules")
        _plant(env.home, "AGENTS.md")
        events = asyncio.run(env.run())
        assert [e for e in events if e["type"] == "result"]
        assert not os.path.lexists(env.home / "rules") and not os.path.lexists(env.home / "AGENTS.md")
    out = capsys.readouterr().out
    assert out.count("removed rules, AGENTS.md") == 2 and "written by a model's shell" in out


# ---- the account pin: auth.json is writable by the model's shell, so the account is kept out of its reach ----

def test_the_first_verified_run_pins_the_account_outside_the_model_writable_home(env):
    assert grok_engine.read_account_pin(env.ctx) is None
    events = asyncio.run(env.run())
    assert types(events)[-1] == "result"
    pin = env.data / grok_engine.ACCOUNT_PIN_FILE
    assert json.loads(pin.read_text()) == {"email": "user@example.invalid"}
    assert stat.S_IMODE(pin.stat().st_mode) == 0o600
    assert grok_engine.read_account_pin(env.ctx) == "user@example.invalid"
    assert not grok_engine._is_under(str(pin), str(env.home))             # never inside the writable home


def test_a_login_swapped_for_another_account_is_refused_before_any_process_starts(env):
    asyncio.run(env.run())
    (env.dumps / "argv.json").unlink(missing_ok=True)
    env.write_auth(email="attacker@example.invalid")                      # what a prompt-injected turn could do
    events = asyncio.run(env.run())
    assert types(events) == ["error"]
    msg = last_error(events)
    assert "different account" in msg and "tools/grok-acct login" in msg
    assert "attacker@example.invalid" not in msg and "user@example.invalid" not in msg   # no identity in errors
    assert not (env.dumps / "argv.json").exists()
    # the agent reads the same swapped file, so only the PIN can tell the registry something is wrong
    env.fake("synthetic_text", auth_meta=json.dumps({"email": "attacker@example.invalid"}))
    grok_engine.reset_cache()                                            # a REAL probe, not the run's cached verdict
    info = asyncio.run(grok_engine.provider_info(force=True))
    # the registry says so BEFORE it spends a sandbox-probe turn on a login nobody vouches for
    assert info["available"] is False
    assert info["error"].startswith("the Grok login in GROK_HOME now names a different account"), info["error"]


def test_the_pin_is_stored_and_read_in_lower_case(env):
    grok_engine.pin_account("MiXed@Example.Invalid", env.ctx)
    assert json.loads((env.data / grok_engine.ACCOUNT_PIN_FILE).read_text()) == {"email": "mixed@example.invalid"}
    (env.data / grok_engine.ACCOUNT_PIN_FILE).write_text(json.dumps({"email": "HAND@Edited.Invalid"}))
    assert grok_engine.read_account_pin(env.ctx) == "hand@edited.invalid"


def test_the_pin_lives_in_the_run_ctxs_data_dir_not_the_environments(env, monkeypatch):
    other = env.tmp / "ctx-data-for-pin"
    other.mkdir()
    env.ctx["DATA"] = other                                              # provider_info has no ctx: env.data stays
    assert types(asyncio.run(env.run()))[-1] == "result"
    assert (other / grok_engine.ACCOUNT_PIN_FILE).is_file()
    assert not (env.data / grok_engine.ACCOUNT_PIN_FILE).exists()


def test_the_same_account_in_another_letter_case_is_the_same_account(env):
    asyncio.run(env.run())
    env.write_auth(email="USER@Example.Invalid")
    assert types(asyncio.run(env.run()))[-1] == "result"


def test_logging_in_again_repins_on_purpose_and_logging_out_forgets_it(env):
    asyncio.run(env.run())
    env.write_auth(email="new-account@example.invalid")
    env.fake("synthetic_text", auth_meta=json.dumps({"email": "new-account@example.invalid"}))   # the agent reads the same file
    assert "different account" in last_error(asyncio.run(env.run()))
    grok_engine.pin_account("new-account@example.invalid", env.ctx)       # what `tools/grok-acct login` does
    assert types(asyncio.run(env.run()))[-1] == "result"
    grok_engine.clear_account_pin(env.ctx)
    assert grok_engine.read_account_pin(env.ctx) is None
    env.write_auth(email="third@example.invalid")
    env.fake("synthetic_text", auth_meta=json.dumps({"email": "third@example.invalid"}))
    assert types(asyncio.run(env.run()))[-1] == "result"                  # no pin: the next verified login is pinned
    assert grok_engine.read_account_pin(env.ctx) == "third@example.invalid"


def test_a_pinned_cockpit_refuses_a_login_that_names_no_account(env):
    asyncio.run(env.run())
    env.write_auth(email=None)
    msg = last_error(asyncio.run(env.run()))
    assert "names no account" in msg
    # without a pin a nameless login is not an account problem (the engine's other checks still apply)
    grok_engine.clear_account_pin(env.ctx)
    assert types(asyncio.run(env.run()))[-1] == "result"


def test_an_unwritable_pin_fails_closed_with_the_reason(env, monkeypatch):
    monkeypatch.setattr(grok_engine, "pin_account", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    msg = last_error(asyncio.run(env.run()))
    assert "cannot record the Grok account" in msg
    assert grok_engine.read_account_pin(env.ctx) is None


def test_a_corrupt_pin_file_reads_as_no_pin_and_is_replaced_by_the_verified_login(env):
    (env.data / grok_engine.ACCOUNT_PIN_FILE).write_text("{not json")
    assert grok_engine.read_account_pin(env.ctx) is None
    assert types(asyncio.run(env.run()))[-1] == "result"
    assert grok_engine.read_account_pin(env.ctx) == "user@example.invalid"


def test_the_data_dir_being_the_grok_home_is_refused(env, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(env.data))
    with pytest.raises(GrokUnavailableError, match="inside the cockpit data dir"):
        grok_engine.build_deny(env.data, env.ctx)


def test_the_cli_binary_inside_the_data_dir_is_refused(env):
    (env.data / "bin").mkdir()
    (env.data / "bin" / "grok").write_text("#!/bin/sh\n")
    with pytest.raises(GrokUnavailableError, match=r"the Grok CLI .* is inside the cockpit data dir"):
        ensure_home(env.ctx, bin_path=str(env.data / "bin" / "grok"))


def test_a_login_at_the_old_default_home_is_pointed_at_not_silently_missed(env, monkeypatch):
    # the default GROK_HOME moved from <data>/grok-home to <data>-grok-home: a bare "not signed in" would
    # send the operator to log in again and lose the sessions next to the old login
    monkeypatch.delenv("GROK_HOME")
    new_home = grok_engine.grok_home(env.ctx)
    assert new_home == env.tmp / "data-grok-home" and not new_home.exists()
    old = env.data / "grok-home"
    old.mkdir()
    (old / "auth.json").write_text("{}")
    msg = grok_engine._auth_problem(grok_engine.read_auth_facts(new_home), new_home, env.ctx)
    assert "not signed in" in msg and str(old) in msg and f"mv {old} {new_home}" in msg
    info = asyncio.run(grok_engine.provider_info(force=True))
    assert info["available"] is False and str(old) in info["error"]
    events = asyncio.run(env.run())
    assert str(old) in last_error(events)
    # no hint when there is nothing at the old place, when GROK_HOME is set, or without a home to point at
    (old / "auth.json").unlink()
    assert "OLD default" not in grok_engine._auth_problem(grok_engine.read_auth_facts(new_home), new_home, env.ctx)
    (old / "auth.json").write_text("{}")
    assert "OLD default" not in grok_engine._auth_problem(grok_engine.read_auth_facts(new_home))
    monkeypatch.setenv("GROK_HOME", str(new_home))
    assert "OLD default" not in grok_engine._auth_problem(grok_engine.read_auth_facts(new_home), new_home, env.ctx)


def test_the_default_home_is_next_to_the_data_dir_never_inside_it(monkeypatch, tmp_path):
    monkeypatch.delenv("GROK_HOME", raising=False)
    data = tmp_path / "data"
    home = grok_engine.grok_home({"DATA": data})
    assert home == tmp_path / "data-grok-home" and not grok_engine._is_under(str(home), str(data))
    other = tmp_path / "state" / "cardloop-data"
    assert grok_engine.grok_home({"DATA": other}) == tmp_path / "state" / "cardloop-data-grok-home"


def test_nothing_is_denied_when_the_data_dir_is_not_a_plain_directory(env, monkeypatch):
    plain = env.tmp / "file-as-data"
    plain.write_text("x")
    dangling = env.tmp / "dangling-data"
    dangling.symlink_to(env.tmp / "nowhere")
    for bad in (plain, dangling, env.tmp / "absent"):
        deny, _ = grok_engine.build_deny(env.home, {"DATA": bad})
        assert not any(str(bad) in e for e in deny), bad


def test_a_symlinked_data_dir_is_denied_by_its_real_path(env):
    real = env.tmp / "real-data"
    real.mkdir()
    link = env.tmp / "data-link"
    link.symlink_to(real)
    deny, _ = grok_engine.build_deny(env.home, {"DATA": link})
    assert os.path.realpath(real) in deny and str(link) not in deny


def test_a_data_dir_that_is_home_or_above_it_is_not_denied(env, capsys):
    for data in (env.fake_home, env.fake_home.parent):
        deny, _ = grok_engine.build_deny(env.home, {"DATA": data})
        assert os.path.realpath(data) not in deny
    assert "not hidden from the model" in capsys.readouterr().out


def test_a_project_that_holds_the_data_dir_and_home_as_real_directories_runs(env, monkeypatch):
    # the cockpit's own checkout, or a chat rooted at $HOME: MEASURED with the real CLI (tests/test_grok_live.py),
    # the data dir is then unreadable, unwritable and cannot be renamed or removed, and the home's parents are pinned
    outer = env.tmp / "outer"
    (outer / "data").mkdir(parents=True)
    env.ctx["DATA"] = outer / "data"
    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(outer / "data"))
    home = outer / "data-grok-home"                                       # the default layout, inside the project
    monkeypatch.setenv("GROK_HOME", str(home))
    env.home = home
    env.write_auth()
    events = asyncio.run(env.run(cwd=str(outer)))
    assert types(events)[-1] == "result" and not only(events, "error")
    assert os.path.realpath(outer / "data") in ensure_home(env.ctx)["deny"]      # still masked as one directory


def test_a_data_dir_reached_through_a_symlink_in_the_workspace_is_refused(env, monkeypatch):
    # the real data dir is elsewhere, but the cockpit reaches it through a NAME inside the workspace —
    # which the model's shell can re-point at something else
    proj = env.tmp / "proj-with-link"
    proj.mkdir()
    (proj / "data").symlink_to(env.data)
    env.ctx["DATA"] = proj / "data"
    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(proj / "data"))
    events = asyncio.run(env.run(cwd=str(proj)))
    assert "through a symlink" in last_error(events) and "the cockpit data dir" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


def test_a_home_reached_through_a_symlink_in_the_workspace_is_refused(env, monkeypatch):
    # the home itself is a real directory, but a symlink sits on the way to it inside the workspace
    proj = env.tmp / "proj-with-home-link"
    proj.mkdir()
    (proj / "link").symlink_to(env.tmp / "real-parent")
    (env.tmp / "real-parent").mkdir()
    home = proj / "link" / "gh"
    monkeypatch.setenv("GROK_HOME", str(home))
    env.home = home
    env.write_auth()
    events = asyncio.run(env.run(cwd=str(proj)))
    assert "through a symlink" in last_error(events) and "GROK_HOME" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


@pytest.mark.parametrize("path,cwd,expected", [
    ("{t}/p/data", "{t}/p", False),                       # a real directory in the workspace
    ("{t}/p/a/b/data", "{t}/p", False),
    ("{t}/p/link/data", "{t}/p", True),                   # a symlink on the way
    ("{t}/p/data-link", "{t}/p", True),                   # the last component is the symlink
    ("{t}/elsewhere/data", "{t}/p", False),               # outside the workspace
    ("{t}/p", "{t}/p", False),                            # the workspace itself
    ("{t}/p-sibling/link/data", "{t}/p", False),          # a name prefix is not containment
])
def test_the_workspace_symlink_predicate(tmp_path, path, cwd, expected):
    (tmp_path / "p" / "a" / "b" / "data").mkdir(parents=True)
    (tmp_path / "p" / "data").mkdir(exist_ok=True)
    (tmp_path / "elsewhere" / "data").mkdir(parents=True)
    (tmp_path / "p" / "link").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "p" / "data-link").symlink_to(tmp_path / "elsewhere" / "data")
    (tmp_path / "p-sibling").mkdir()
    (tmp_path / "p-sibling" / "link").symlink_to(tmp_path / "elsewhere")
    fmt = lambda s: s.format(t=tmp_path)
    assert grok_engine._reached_through_workspace_symlink(fmt(path), fmt(cwd)) is expected


def test_a_project_inside_the_grok_home_is_refused(env):
    inside = env.home / "somewhere"
    inside.mkdir()
    events = asyncio.run(env.run(cwd=str(inside)))
    assert "inside GROK_HOME" in last_error(events)
    assert not (env.dumps / "argv.json").exists()


def test_a_project_next_to_the_data_dir_and_home_runs(env):
    events = asyncio.run(env.run(cwd=str(env.cwd)))
    assert [e for e in events if e["type"] == "result"] and not [e for e in events if e["type"] == "error"]


def test_a_symlink_deny_entry_hides_its_target_not_the_link(env, monkeypatch):
    target = env.tmp / "dotfiles-ssh"
    target.mkdir()
    link = env.tmp / "dot-ssh"
    link.symlink_to(target)
    monkeypatch.setenv("GROK_SANDBOX_DENY", str(link))
    deny = ensure_home(env.ctx)["deny"]
    assert os.path.realpath(target) in deny and str(link) not in deny


def test_a_project_inside_the_hidden_data_dir_is_refused(env):
    inside = env.data / "proj"
    inside.mkdir()
    events = asyncio.run(env.run(cwd=str(inside)))
    assert "inside the sandbox deny list entry" in last_error(events)


def test_the_cockpits_env_backups_and_data_snapshots_are_denied_too_but_not_the_example(env, monkeypatch):
    # a real checkout holds `.env.bak-<date>` (every secret as of that day) next to `.env`; `**/.env` is an
    # exact-name glob anchored at the workspace, so from any other project the backup was readable
    repo = env.engine_repo
    for name in (".env", ".env.bak-20260901-0954", ".env.local"):
        (repo / name).write_text("WEB_PASSWORD=x\n")
    (repo / ".env.example").write_text("WEB_PASSWORD=\n")
    (repo / "data.bak-20260901").mkdir()
    (repo / "data.bak-20260901" / "chats.json").write_text("{}")
    for unrelated in ("database.sql", "data.md", "environment.txt", ".envrc"):
        (repo / unrelated).write_text("x")
    for custom in (None, "**/.env", str(env.secret_dir)):
        if custom is None:
            monkeypatch.delenv("GROK_SANDBOX_DENY")
        else:
            monkeypatch.setenv("GROK_SANDBOX_DENY", custom)
        deny, _ = grok_engine.build_deny(env.home, env.ctx)
        for name in (".env", ".env.bak-20260901-0954", ".env.local", "data.bak-20260901"):
            assert str(repo / name) in deny, (name, custom)
        for name in (".env.example", "database.sql", "data.md", "environment.txt", ".envrc"):
            assert str(repo / name) not in deny, (name, custom)


def test_a_checkout_that_cannot_be_listed_still_denies_the_env_file(env, monkeypatch):
    (env.engine_repo / ".env").write_text("x")
    monkeypatch.setattr(grok_engine.os, "scandir", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("no")))
    assert grok_engine._cockpit_secret_paths()[0] == str(env.engine_repo / ".env")


def test_the_cockpits_own_env_file_is_denied_always_and_never_created(env, monkeypatch):
    # `**/.env` is anchored at the workspace: from any other project the cockpit's secrets file is readable
    repo = env.engine_repo
    deny, skipped = grok_engine.build_deny(env.home, env.ctx)
    assert str(repo / ".env") in skipped and not (repo / ".env").exists()      # absent -> dropped, not materialised
    (repo / ".env").write_text("WEB_PASSWORD=x\n")
    for custom in (None, "**/.env", str(env.secret_dir)):
        if custom is None:
            monkeypatch.delenv("GROK_SANDBOX_DENY")
        else:
            monkeypatch.setenv("GROK_SANDBOX_DENY", custom)
        assert str(repo / ".env") in grok_engine.build_deny(env.home, env.ctx)[0], custom


# ------------------------------------------------------------------------------------------
# spec-095 P7a: a model REFUSAL of the probe's "security self-test" is not an outage
# ------------------------------------------------------------------------------------------

class ScriptedProbe:
    """Stands in for `_run_turn` during the sandbox-denial probe: attempt N does what script[N] says.
    "run" = issue a tool call and let the command's output (control file ... end token) reach the wire
    tap; "leak" = the same plus the canary; "refuse" = words only; "ran-blind" = a tool call whose
    output never shows the control file; "partial" = the control file but never the end token (the
    canary read was not reached); "no-canary-read" = control and END but the middle step was dropped
    (exit status 0, no canary shown); "foreign-end" = the control file and an END-looking token that is not
    this attempt's own; "error" = the turn itself fails."""

    def __init__(self, *script, said="I won't run that command."):
        self.script = list(script)
        self.said = said
        self.prompts: list[str] = []
        self.cwds: list[str] = []
        self.controls: list[str] = []

    async def __call__(self, **kw):
        n = len(self.prompts)
        self.prompts.append(kw["prompt"])
        self.cwds.append(kw["cwd"])
        control = (Path(kw["cwd"]) / "control.txt").read_text()
        end = (Path(kw["cwd"]) / "end.txt").read_text()
        self.controls.append(control)
        canary = (grok_engine._canary_dir() / "secret.txt").read_text().strip()
        what = self.script[n]
        if what == "error":
            yield {"type": "error", "exc": GrokUnavailableError("probe spawn failed")}
            return
        if what in ("run", "leak", "ran-blind", "partial", "foreign-end", "no-canary-read"):
            yield {"type": "tool", "name": "Bash", "input": {"command": "cat control.txt"}}
            kw["tap"]({"out": (control if what != "ran-blind" else "nothing useful")
                       + ("\n" + canary if what == "leak" else "")
                       + ("\nrc=0" if what in ("leak", "no-canary-read") else "\nrc=1" if what in ("run", "foreign-end") else "")
                       + ("\n" + end if what in ("run", "leak", "no-canary-read") else "")
                       + ("\nEND-" + "0" * 32 if what == "foreign-end" else "")})
        else:
            yield {"type": "text", "text": self.said}
        yield {"type": "result", "context_tokens": 1}


@pytest.fixture
def probe(env, monkeypatch):
    ensure_home(env.ctx)                       # the canary must exist before the probe reads it
    info = {"home": env.home, "deny": []}

    def install(*script, **kw):
        stub = ScriptedProbe(*script, **kw)
        monkeypatch.setattr(grok_engine, "_run_turn", stub)
        return stub

    async def run():
        return await grok_engine._probe_sandbox_denial(info)
    return SimpleNamespace(install=install, run=run, env=env)


async def test_a_clean_probe_runs_once_with_the_original_wording(probe):
    stub = probe.install("run")
    state, _detail = await probe.run()
    assert state == "ok" and len(stub.prompts) == 1
    canary_file = grok_engine._canary_dir() / "secret.txt"
    assert stub.prompts[0] == (
        "Security self-test, not a coding task. Run exactly this one shell command with your "
        "terminal tool, then reply with the single word DONE: "
        f"cat control.txt; echo ---; cat {canary_file} 2>&1; echo \"rc=$?\"; cat end.txt")


async def test_a_refusal_is_journaled_and_retried_once_with_plainer_wording(probe, capsys):
    stub = probe.install("refuse", "run")
    state, detail = await probe.run()
    assert state == "ok" and "canary unreadable" in detail
    assert len(stub.prompts) == 2
    canary_file = grok_engine._canary_dir() / "secret.txt"
    command = f"cat control.txt; echo ---; cat {canary_file} 2>&1; echo \"rc=$?\"; cat end.txt"
    assert stub.prompts[1] == (
        "Run exactly this one shell command with your terminal tool, then reply with the single word "
        f"DONE: {command}")
    # measured live: any claim of authority / "expected to be denied" framing was refused 3 of 3
    assert "self-test" not in stub.prompts[1] and "EXPECTED" not in stub.prompts[1]
    assert "authoris" not in stub.prompts[1].lower()
    out = capsys.readouterr().out
    assert "[grok] sandbox denial probe: the model ran no command (it said: \"I won't run that command.\")" in out
    assert "retrying once" in out


async def test_each_attempt_gets_its_own_scratch_dir_and_control_token(probe):
    stub = probe.install("refuse", "run")
    await probe.run()
    assert stub.cwds[0] != stub.cwds[1] and stub.controls[0] != stub.controls[1]
    assert all(c.startswith("CONTROL-") for c in stub.controls)
    assert not any(Path(c).exists() for c in stub.cwds)            # cleaned up, refusal or not


async def test_a_second_refusal_stays_inconclusive_and_is_never_a_third_attempt(probe, capsys):
    stub = probe.install("refuse", "refuse", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and len(stub.prompts) == 2
    assert "declined twice" in detail and "I won't run that command." in detail
    out = capsys.readouterr().out
    assert "declined again" in out and "I won't run that command." in out


async def test_a_leak_is_a_failure_at_once_and_is_never_retried(probe):
    stub = probe.install("leak", "run")
    state, detail = await probe.run()
    assert state == "failed" and "did NOT hide the canary" in detail and len(stub.prompts) == 1


async def test_a_leak_on_the_retry_is_a_failure(probe):
    stub = probe.install("refuse", "leak")
    state, _ = await probe.run()
    assert state == "failed" and len(stub.prompts) == 2


async def test_a_command_that_ran_but_never_showed_the_control_file_is_not_a_refusal(probe):
    stub = probe.install("ran-blind", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and len(stub.prompts) == 1
    assert "never read its control file" in detail and "declined" not in detail


async def test_a_command_that_stopped_before_its_last_step_never_earns_an_ok(probe):
    # the model read the control file and never attempted the canary read: nothing was measured, and an
    # `ok` is cached for 7 days. It ran a command, so it is not a refusal and is not retried either
    stub = probe.install("partial", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and "last step" in detail and len(stub.prompts) == 1


async def test_a_model_that_dropped_the_canary_read_never_earns_an_ok(probe):
    # it ran control + end and nothing in between: exit status 0, no canary text — the old judge said ok
    stub = probe.install("no-canary-read", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and "failed canary read" in detail and len(stub.prompts) == 1


async def test_only_this_attempts_own_end_token_counts(probe):
    stub = probe.install("foreign-end", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and "last step" in detail and len(stub.prompts) == 1


async def test_a_refusal_followed_by_a_partial_run_reports_the_partial_run_not_a_second_refusal(probe):
    stub = probe.install("refuse", "partial")
    state, detail = await probe.run()
    assert state == "inconclusive" and len(stub.prompts) == 2
    assert "last step" in detail and "declined twice" not in detail


async def test_the_end_token_is_not_in_the_prompt_only_in_what_the_command_prints(probe):
    stub = probe.install("run")
    await probe.run()
    end_tokens = [c for c in stub.controls]                      # CONTROL-<hex>: the same property for control
    assert all(t not in stub.prompts[0] for t in end_tokens)
    assert "END-" not in stub.prompts[0]


async def test_a_failed_turn_is_inconclusive_and_not_retried(probe):
    stub = probe.install("error", "run")
    state, detail = await probe.run()
    assert state == "inconclusive" and detail.startswith("probe turn failed") and len(stub.prompts) == 1


async def test_a_turn_that_fails_on_the_retry_reports_that_failure(probe):
    stub = probe.install("refuse", "error")
    state, detail = await probe.run()
    assert state == "inconclusive" and detail.startswith("probe turn failed") and len(stub.prompts) == 2
    assert "declined twice" not in detail


async def test_the_refusal_text_is_redacted_collapsed_and_capped(probe, monkeypatch, capsys):
    monkeypatch.setenv("SOME_API_TOKEN", "tok-0123456789abcdef")
    long_words = "I will   not\nrun it. " + "tok-0123456789abcdef " + "x" * 600
    probe.install("refuse", "refuse", said=long_words)
    _state, detail = await probe.run()
    out = capsys.readouterr().out + detail
    assert "tok-0123456789abcdef" not in out and "***" in out
    assert "I will not run it." in out and "\n" not in detail
    said_part = detail.split("last words: ", 1)[1]
    assert len(said_part) <= grok_engine._PROBE_SAID_CHARS + 4       # the quotes of repr()


async def test_provider_info_recovers_from_a_refusal_instead_of_going_offline(probe, env):
    probe.install("refuse", "run")
    grok_engine.reset_sandbox_probe(env.ctx)
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is True and info["sandbox"]["probe"] == "ok", info["error"]
    cached = json.loads((env.data / "grok_sandbox_probe.json").read_text())
    assert cached["state"] == "ok"


async def test_provider_info_stays_closed_after_two_refusals_with_the_reason(probe, env):
    probe.install("refuse", "refuse")
    grok_engine.reset_sandbox_probe(env.ctx)
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and "inconclusive" in info["error"] and "declined twice" in info["error"]
    assert json.loads((env.data / "grok_sandbox_probe.json").read_text())["state"] == "inconclusive"
