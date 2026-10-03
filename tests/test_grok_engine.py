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

async def test_text_chunks_stream_as_deltas_then_one_assembled_text(env):
    events = await env.run()
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
        ("TodoWrite", {"todos": [{"id": "1", "content": "a", "status": "pending"}]}),
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
    events = await asyncio.wait_for(env.run(), 20)
    assert types(events)[-1] == "result"


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
    monkeypatch.setenv("GROK_ALLOW_ALL_PROJECTS", raw)
    assert grok_engine.allow_all_projects() is want


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


def test_ensure_home_writes_a_custom_profile_and_config(env):
    info = ensure_home(env.ctx)
    prof = _toml(env.home / "sandbox.toml")["profiles"]["cardloop"]
    assert prof["extends"] == "workspace"
    assert str(env.secret_dir) in prof["deny"]
    assert str(env.data / "grok-canary") in prof["deny"]          # the probe's canary is always denied
    assert info["deny"] == prof["deny"] and info["profile"] == "cardloop"
    cfg = _toml(env.home / "config.toml")
    assert cfg["shell_environment_policy"]["inherit"] == "core"
    assert cfg["cli"]["auto_update"] is False
    for name in ("sandbox.toml", "config.toml"):
        assert stat.S_IMODE((env.home / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(env.home.stat().st_mode) == 0o700


def test_ensure_home_is_idempotent_and_repairs_config(env):
    ensure_home(env.ctx)
    before = {n: (env.home / n).stat().st_mtime_ns for n in ("sandbox.toml", "config.toml")}
    ensure_home(env.ctx)
    assert before == {n: (env.home / n).stat().st_mtime_ns for n in before}   # no rewrite
    (env.home / "config.toml").write_text('[shell_environment_policy]\ninherit = "all"\n')
    ensure_home(env.ctx)
    assert _toml(env.home / "config.toml")["shell_environment_policy"]["inherit"] == "core"


def test_canary_exists_before_the_deny_list_is_built(env):
    # a missing literal is dropped (C7); the canary must therefore be created FIRST or the probe
    # would run against a profile that does not deny it.
    assert not (env.data / "grok-canary").exists()
    info = ensure_home(env.ctx)
    assert str(env.data / "grok-canary") in info["deny"]
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
    assert str(dangling) in info["deny"]                 # lexists: a dangling symlink still exists
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
    ("C1", "K9", "xx C1 yy", "ok"),
    ("C1", "K9", "xx C1 K9 yy", "failed"),            # the canary leaked, even next to the control
    ("C1", "K9", "xx K9", "failed"),
    ("C1", "K9", "Permission denied", "inconclusive"),  # nothing ran: not proof of anything
    ("C1", "K9", "", "inconclusive"),
])
def test_probe_judge_table(control, canary, hay, want):
    assert grok_engine.judge_probe(control, canary, hay)[0] == want


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
    assert grok_engine.grok_home({"DATA": tmp_path}) == tmp_path / "grok-home"
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
