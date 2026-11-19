"""
spec-098 (Forget) — the writers: run-id stamping, audit `run=` token, turn marks, lineage.

Acceptance 5: one fixture run per run site x provider; every timeline and audit line the run
wrote carries the run id, `run_end` names the session + provider, and a Claude turn mark's offset
is the transcript's byte size BEFORE the prompt reached the CLI.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import session_lineage
import turn_marks
import webapp as _webapp
from webapp import _derive_token

SK = "1001:42"
PID = "myproject"
CHAT = "aaaaaa"
NEW = "NEW-ID"


# ─────────────────────────── fixtures ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    engine._CURRENT_RUN.clear()
    monkeypatch.setattr(engine, "AUDIT_DIR", tmp_path / "audit")
    old = dict(_webapp._CHAT_QUEUE)
    _webapp._CHAT_QUEUE.clear()
    monkeypatch.setattr(_webapp, "_CHAT_QUEUE_FILE", tmp_path / "chat-queue.json")
    _webapp._live_turns.pop(SK, None)
    monkeypatch.setattr(_webapp, "_TIMELINE_DATA_DIR", None)
    monkeypatch.setattr(_webapp, "_TIMELINE_TOPICS", None)
    yield
    _webapp._CHAT_QUEUE.clear()
    _webapp._CHAT_QUEUE.update(old)
    _webapp._bg_run_ids.clear()
    _webapp._bg_run_started.clear()
    _webapp._bg_run_buffered.clear()
    _webapp._live_turns.pop(SK, None)
    engine._CURRENT_RUN.clear()


async def _unused(**kw):
    raise AssertionError("wrong engine ran")
    yield  # pragma: no cover


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(_webapp._codex, "codex_enabled", lambda: True)
    monkeypatch.setenv("GROK_ENABLED", "true")
    data = tmp_path / "data"
    data.mkdir()
    proj = tmp_path / PID
    proj.mkdir()
    c = {
        "topics": {SK: {"project": PID, "cwd": str(proj), "model": "sonnet"}},
        "sessions": {SK: "FLAT"},
        "running": {}, "password": "testpass", "DATA": data, "HERE": ROOT,
        "VAULT_PROJECTS": None, "DEFAULT_MODEL": "sonnet",
        "MODELS": {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"},
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": _unused, "run_codex_engine": _unused, "run_grok_engine": _unused,
        "ptb_app": None, "rate_limits": {}, "cwd_locks": {},
    }
    c["_auth_token"] = _derive_token("testpass")
    _webapp._timeline_init(c)
    return c


@pytest.fixture
def app(ctx):
    from aiohttp import web
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = ctx
    a.router.add_post("/api/projects/{id}/chat", _webapp.api_project_chat)
    return a


# provider -> (ctx engine key, result key, chat continuity field, model)
PROVIDERS = {
    "claude": ("run_engine", "session_id", "session_id", "opus"),
    "codex": ("run_codex_engine", "thread_id", "codex_thread_id", "gpt-5.6-sol"),
    "grok": ("run_grok_engine", "provider_session_id", "grok_session_id", "grok-x"),
}


def _install(ctx, provider):
    key, result_key, _field, _m = PROVIDERS[provider]
    calls = []

    async def fake(**kw):
        calls.append(kw)
        sk = kw["session_key"]
        # what the real engines do mid-run: audit lines and timeline rows written by hook callbacks
        engine.audit(kw["project_name"], "TASK", "do it", session_key=sk)
        _webapp._timeline_append(sk, {"kind": "tool_result", "tool": "Bash", "status": "ok"})
        yield {"type": "text", "text": "answer"}
        engine.audit(kw["project_name"], "RESULT", "Bash ok", session_key=sk)
        engine.audit(kw["project_name"], "DENY", "rule: cmd", session_key=sk)
        yield {"type": "result", result_key: NEW, "context_tokens": 5}

    for k in ("run_engine", "run_codex_engine", "run_grok_engine"):
        ctx[k] = _unused
    ctx[key] = fake
    return calls


def _seed_chat(ctx, provider, **extra):
    _key, _rk, field, model = PROVIDERS[provider]
    chat = {"id": CHAT, "name": "Main", "provider": provider, "model": model, **extra}
    _webapp._save_chats(ctx, {PID: {"active": CHAT, "chats": [chat]}})


def _timeline_rows(ctx):
    path = _webapp._timeline_path(SK)
    assert path is not None and path.exists(), "timeline file missing"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audit_rows():
    rows = []
    for f in sorted(Path(engine.AUDIT_DIR).glob("audit-*.log")):
        for line in f.read_text(encoding="utf-8").splitlines():
            parsed = engine.parse_audit_line(line)
            assert parsed is not None, f"unparseable audit line: {line!r}"
            rows.append(parsed)
    return rows


def _check_run(ctx, provider, expect_run_id=None, expect_sid=NEW):
    tl = [r for r in _timeline_rows(ctx) if r.get("session_key") == SK]
    starts = [r for r in tl if r.get("kind") == "run_start"]
    ends = [r for r in tl if r.get("kind") == "run_end"]
    assert len(starts) == 1 and len(ends) == 1, [r.get("kind") for r in tl]
    run_id = starts[0]["run_id"]
    if expect_run_id:
        assert run_id == expect_run_id
    for r in tl:
        assert r.get("run_id") == run_id, f"timeline row without the run id: {r}"
    assert any(r.get("kind") == "tool_result" for r in tl), "the hook-written row must be stamped too"
    assert ends[0]["provider"] == provider
    assert ends[0]["session_id"] == expect_sid
    audit = _audit_rows()
    kinds = [a[2] for a in audit]
    assert kinds == ["TASK", "RESULT", "DENY"], kinds
    for ts, project, kind, rid, text in audit:
        assert rid == run_id, f"{kind} audit line lacks run={run_id}: run={rid}"
        assert project == PID
    assert engine.current_run(SK) == run_id, "never cleared at run end"
    return run_id


# ─────────────────────────── run sites x providers ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_chat_post_stamps_every_line(aiohttp_client, ctx, app, provider):
    _seed_chat(ctx, provider)
    _install(ctx, provider)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PID}/chat", json={"prompt": "hello", "chat_id": CHAT},
                                 headers={"Cookie": f"cops_auth={ctx['_auth_token']}"})
        assert resp.status == 200, await resp.text()
        await resp.read()
    _check_run(ctx, provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_queue_drain_stamps_every_line(ctx, provider):
    _seed_chat(ctx, provider)
    pinned = _webapp._pin_chat_runtime(ctx, {"id": PID}, CHAT)
    assert _webapp._chat_queue_enqueue(SK, "queued", chat_id=CHAT, project_id=PID, pinned_runtime=pinned)
    _install(ctx, provider)
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(ctx, SK) is True
        await asyncio.sleep(0.1)
    _check_run(ctx, provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_card_run_stamps_every_line(ctx, tmp_path, provider):
    cwd = tmp_path / "cardproj"
    cwd.mkdir()
    project = {"name": PID, "cwd": str(cwd), "session_key": SK, "model": "sonnet"}
    card = {"id": "aabbcc", "text": "Build", "description": None, "provider": provider}
    if provider == "grok":
        card["model"] = "grok-x"
    _install(ctx, provider)
    ctx["running"][SK] = True
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        await _webapp._run_card(ctx, None, project, card, SK, run_mode="legacy")
    _check_run(ctx, provider, expect_run_id="aabbcc")


@pytest.mark.asyncio
async def test_background_drain_run_stamps_its_lines(ctx):
    """A drain-surfaced autonomous turn (source:bg): the engine's lines between start and end."""
    ctx["sessions"][SK] = "BG-SID"
    _webapp._WEBAPP_CTX = ctx
    _webapp._bg_run_event(SK, "start")
    bg_id = _webapp._bg_run_ids[SK]
    engine.audit(PID, "RESULT", "Bash ok", session_key=SK)
    _webapp._timeline_append(SK, {"kind": "tool_result", "tool": "Bash"})
    _webapp._bg_run_event(SK, "text", "autonomous reply")
    _webapp._bg_run_event(SK, "end")
    tl = [r for r in _timeline_rows(ctx) if r.get("session_key") == SK]
    assert {r["kind"] for r in tl} >= {"run_start", "tool_result", "text", "run_end"}
    assert all(r.get("run_id") == bg_id for r in tl), tl
    end = next(r for r in tl if r["kind"] == "run_end")
    assert end["provider"] == "claude" and end["session_id"] == "BG-SID"
    assert [a[3] for a in _audit_rows()] == [bg_id]


# ─────────────────────────── stamping primitives ─────────────────────────────


def test_explicit_run_id_wins_and_unknown_session_is_unstamped(ctx):
    engine.set_current_run(SK, "cur001")
    _webapp._timeline_append(SK, {"kind": "text", "text": "a"})
    _webapp._timeline_append(SK, {"kind": "text", "text": "b", "run_id": "explicit"})
    rows = _timeline_rows(ctx)
    assert [r["run_id"] for r in rows] == ["cur001", "explicit"]
    _webapp._timeline_append("9:9", {"kind": "text", "text": "other"})
    other = [json.loads(x) for x in _webapp._timeline_path("9:9").read_text().splitlines()]
    assert "run_id" not in other[0]
    assert engine.current_run("9:9") is None and engine.current_run(None) is None


def test_audit_parser_accepts_both_formats():
    engine.audit("Proj A", "TASK", "old style no session")
    engine.set_current_run("k", "abc123")
    engine.audit("Proj A", "TASK", "new: with colon", session_key="k")
    engine.audit("Proj A", "COMPACT", "x", session_key="unknown-key")
    lines = [ln for f in sorted(Path(engine.AUDIT_DIR).glob("audit-*.log"))
             for ln in f.read_text(encoding="utf-8").splitlines()]
    assert " run=abc123: " in lines[1] and "run=" not in lines[0] and "run=" not in lines[2]
    parsed = [engine.parse_audit_line(x) for x in lines]
    assert [(p[1], p[2], p[3]) for p in parsed] == [("Proj A", "TASK", None), ("Proj A", "TASK", "abc123"),
                                                    ("Proj A", "COMPACT", None)]
    assert parsed[1][4] == "new: with colon"
    # the cockpit's own reader (activity endpoint) still matches by project marker on both forms
    assert all("[Proj A]" in x for x in lines)


@pytest.mark.asyncio
async def test_deny_hook_audit_line_carries_the_run_of_its_cwd(tmp_path):
    engine.set_current_run("deny:1", "dn0001")
    engine._guard_remember_project(str(tmp_path / "p"), "ProjD", "deny:1")
    out = await engine._dangerous_command_guard_hook(
        {"tool_input": {"command": "git reset --hard"}, "cwd": str(tmp_path / "p" / "sub")}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    (row,) = _audit_rows()
    assert row[1:4] == ("ProjD", "DENY", "dn0001")


# ─────────────────────────── turn marks (real run_engine, fake SDK client) ───


def _result_msg(session_id):
    from claude_agent_sdk import ResultMessage
    m = MagicMock(spec=ResultMessage)
    m.__class__ = ResultMessage
    m.session_id = session_id
    m.total_cost_usd = None
    m.api_error_status = None
    m.duration_ms = None
    m.duration_api_ms = None
    m.structured_output = None
    return m


def _init_msg(session_id):
    from claude_agent_sdk import SystemMessage
    return SystemMessage(subtype="init", data={"session_id": session_id, "model": "claude-sonnet-5-5"})


def _fake_client_cls(transcript: Path, sid_in_init: str):
    """The CLI stand-in: query() appends the prompt line to the transcript like the real CLI."""
    class FakeClient:
        def __init__(self, options=None):
            self.interrupt = AsyncMock()
            self.disconnect = AsyncMock()

        async def connect(self, *a, **k):
            return None

        async def query(self, prompt):
            transcript.parent.mkdir(parents=True, exist_ok=True)
            with open(transcript, "ab") as f:
                f.write(json.dumps({"type": "user", "text": prompt}).encode() + b"\n")

        async def receive_response(self):
            yield _init_msg(sid_in_init)
            yield _result_msg(sid_in_init)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return FakeClient


async def _drive(tmp_path, monkeypatch, *, resume, ephemeral, prior_lines):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cwd = tmp_path / "work dir"
    cwd.mkdir()
    sid = resume or "minted-sid"
    transcript = turn_marks.transcript_path(str(cwd), sid)
    if prior_lines:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_bytes(b"".join(
            json.dumps({"type": "user", "text": t}).encode() + b"\n" for t in prior_lines))
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    c = {"running": {}, "live_clients": {}, "DATA": data}
    engine.set_current_run("chat:1", "rn0001")
    before = transcript.stat().st_size if transcript.exists() else 0
    with patch.object(engine, "PERSISTENT_CLIENT", not ephemeral), \
         patch.object(engine, "ClaudeSDKClient", _fake_client_cls(transcript, sid)):
        async for _ in bot.run_engine(
                project_name="proj", cwd=str(cwd), prompt="the secret prompt", session_key="chat:1",
                model="sonnet", ctx=c, ephemeral=ephemeral, resume_session_id=resume, chat_id="cc1111",
                entrypoint="chat"):
            pass
    return data, sid, transcript, before


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [False, True], ids=["live-client", "cold"])
async def test_claude_mark_offset_is_the_transcript_size_before_the_prompt(tmp_path, monkeypatch, ephemeral):
    data, sid, transcript, before = await _drive(
        tmp_path, monkeypatch, resume="resume-sid", ephemeral=ephemeral, prior_lines=["one", "two"])
    rows = turn_marks.read(data, sid)
    start = next(r for r in rows if "offset" in r)
    assert start["run_id"] == "rn0001" and start["chat_id"] == "cc1111"
    assert start["source"] == "chat" and start["session_key"] == "chat:1" and start["session_id"] == sid
    assert start["offset"] == before > 0
    raw = transcript.read_bytes()
    assert len(raw) > before, "the prompt line was appended after the mark"
    # the prefix is exactly the lines written BEFORE the prompt and ends on a line boundary; the
    # byte at `offset` starts the line the prompt wrote — truncating there removes only the turn
    assert raw[before - 1:before] == b"\n"
    assert json.loads(raw[:before].splitlines()[-1])["text"] == "two"
    assert json.loads(raw[before:].splitlines()[0])["text"] == "the secret prompt"
    assert any(r.get("run_id") == "rn0001" and "ts_end" in r for r in rows)
    assert "secret prompt" not in "".join((data / "turn_marks" / f"{sid}.jsonl").read_text())


@pytest.mark.asyncio
async def test_new_session_mark_is_offset_zero_written_after_init(tmp_path, monkeypatch):
    data, sid, transcript, before = await _drive(
        tmp_path, monkeypatch, resume=None, ephemeral=False, prior_lines=[])
    assert sid == "minted-sid" and before == 0
    (start,) = [r for r in turn_marks.read(data, sid) if "offset" in r]
    assert start["offset"] == 0 and start["run_id"] == "rn0001"


@pytest.mark.asyncio
async def test_a_failing_mark_write_never_breaks_the_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(turn_marks, "record", boom)
    monkeypatch.setattr(turn_marks, "record_end", boom)
    data, sid, transcript, before = await _drive(
        tmp_path, monkeypatch, resume="r2", ephemeral=False, prior_lines=["x"])
    assert transcript.stat().st_size > before, "the run itself completed"


def test_turn_marks_module_roundtrip_and_drop(tmp_path):
    turn_marks.record(tmp_path, run_id="r1", session_id="s1", offset=10, session_key="k", chat_id="c", source="chat")
    turn_marks.record(tmp_path, run_id="r1", session_id="s1", offset=99)       # idempotent per run
    turn_marks.record_end(tmp_path, run_id="r1", session_id="s1")
    rows = turn_marks.read(tmp_path, "s1")
    assert [("offset" in r, "ts_end" in r) for r in rows] == [(True, False), (False, True)]
    assert rows[0]["offset"] == 10
    assert turn_marks.read(tmp_path, "../evil") == []
    assert turn_marks.drop(tmp_path, "s1") is True and turn_marks.drop(tmp_path, "s1") is False


# ─────────────────────────── lineage ─────────────────────────────────────────


def test_lineage_module_record_read_remove(tmp_path):
    session_lineage.record(tmp_path, kind="runtime_handoff", from_provider="claude", from_session="A",
                           to_provider="codex", to_session="B")
    session_lineage.record(tmp_path, kind="rotate", from_provider="claude", from_session="C",
                           to_provider="claude", to_session="D")
    rows = session_lineage.read(tmp_path)
    assert [r["to_session"] for r in rows] == ["B", "D"] and all("ts" in r for r in rows)
    assert session_lineage.remove_session(tmp_path, "codex", "B") == 1
    assert [r["from_session"] for r in session_lineage.read(tmp_path)] == ["C"]
    assert session_lineage.remove_session(tmp_path, "codex", "nope") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["codex", "grok"])
async def test_runtime_handoff_delivery_records_lineage(aiohttp_client, ctx, app, target):
    """Chat flipped Claude -> target with the old Claude session still on the record: the armed
    block names its source, and the turn that DELIVERS it writes the lineage row."""
    _key, _rk, field, model = PROVIDERS[target]
    chat = {"id": CHAT, "name": "Main", "provider": target, "model": model, "session_id": "OLD-CLAUDE"}
    _webapp._save_chats(ctx, {PID: {"active": CHAT, "chats": [chat]}})
    armed = {"text": "summary of the claude session", "created_at": 1.0, "from_label": "Claude",
             "to_label": target, "for_provider": target, "for_backend": "",
             **_webapp._handoff_source_ids(chat)}
    assert armed["from_provider"] == "claude" and armed["from_session"] == "OLD-CLAUDE"
    chat["runtime_handoff"] = armed
    _webapp._save_chats(ctx, {PID: {"active": CHAT, "chats": [chat]}})
    _install(ctx, target)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PID}/chat", json={"prompt": "go", "chat_id": CHAT},
                                 headers={"Cookie": f"cops_auth={ctx['_auth_token']}"})
        await resp.read()
    (row,) = session_lineage.read(ctx["DATA"])
    assert (row["kind"], row["from_provider"], row["from_session"], row["to_provider"], row["to_session"]) == \
        ("runtime_handoff", "claude", "OLD-CLAUDE", target, NEW)


def test_handoff_source_is_unknown_when_ambiguous():
    assert _webapp._handoff_source_ids({"provider": "codex", "session_id": "a", "grok_session_id": "b"}) == {}
    assert _webapp._handoff_source_ids({"provider": "codex", "codex_thread_id": "only-self"}) == {}
