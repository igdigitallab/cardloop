"""
A chat's feed carries on across a context reset (session_links).

A reset (↺, auto-rotation, "New session") clears the chat's session id; the next turn starts a
session the SDK names only once it answers, and the feed reads one transcript per session — so
scrolling up stopped dead at the reset. Now the reset leaves a mark on the chat, the first fresh
turn's write-back turns it into a link new → old, and session-history hands the client the
previous session (with the handoff the new one started from) once the feed reaches the new
session's first message. Sessions older than the links file are recognised from their handoff,
which quotes its source's last prompt and last reply verbatim.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import history_index
import session_links
import webapp as _webapp

from test_provider_seam import (  # noqa: F401 - fixtures used by name
    CHAT_ID, PROJECT_ID, SESSION_KEY, _auth, _chat_record, _sse_events, app, fake_ctx,
    reset_shared_state,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    history_index._CACHE.clear()
    _webapp._prev_infer_miss.clear()
    yield
    history_index._CACHE.clear()
    _webapp._prev_infer_miss.clear()


def _seed(ctx, **chat):
    rec = {"id": CHAT_ID, "name": "Main", "provider": "claude", "model": "opus", **chat}
    _webapp._save_chats(ctx, {PROJECT_ID: {"active": CHAT_ID, "chats": [rec]}})


MARK = {"session_id": "OLD-SID", "at": 1_790_000_000.0, "trigger": "auto",
        "context_tokens": 752_000, "handoff": True}


def _fresh_engine(ctx, calls):
    async def fake(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "answer"}
        yield {"type": "result", "session_id": "NEW-SID", "context_tokens": 7}
    ctx["run_engine"] = fake


# ─────────────────────────── the link is made where the new session is named ──


@pytest.mark.asyncio
async def test_the_first_turn_after_a_reset_links_the_new_session_to_the_old(
        aiohttp_client, fake_ctx, app):
    _seed(fake_ctx, session_id=None, rotated_from=dict(MARK))
    calls: list = []
    _fresh_engine(fake_ctx, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID},
                                 headers=_auth(fake_ctx))
        assert resp.status == 200
        await _sse_events(resp)
    assert calls and calls[0]["resume_session_id"] is None
    rec = _chat_record(fake_ctx)
    assert rec["session_id"] == "NEW-SID" and "rotated_from" not in rec
    link = session_links.get(fake_ctx["DATA"], "NEW-SID")
    assert link["prev"] == "OLD-SID" and link["trigger"] == "auto"
    assert link["context_tokens"] == 752_000 and link["handoff"] is True and not link["inferred"]


@pytest.mark.asyncio
async def test_a_queued_first_turn_after_a_reset_links_it_too(fake_ctx):
    _seed(fake_ctx, session_id=None, rotated_from=dict(MARK))
    pinned = _webapp._pin_chat_runtime(fake_ctx, {"id": PROJECT_ID}, CHAT_ID)
    assert _webapp._chat_queue_enqueue(SESSION_KEY, "queued", chat_id=CHAT_ID,
                                       project_id=PROJECT_ID, pinned_runtime=pinned)
    fake_ctx["sessions"].pop(SESSION_KEY, None)
    calls: list = []
    _fresh_engine(fake_ctx, calls)
    with patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: asyncio.ensure_future(coro)), \
         patch.object(_webapp, "_secrets_read", return_value={}), \
         patch.object(_webapp, "_build_agents_kwargs", return_value={}):
        assert await _webapp._chat_queue_drain_one(fake_ctx, SESSION_KEY) is True
        await asyncio.sleep(0.05)
    assert calls and calls[0]["resume_session_id"] is None
    assert "rotated_from" not in _chat_record(fake_ctx)
    assert session_links.get(fake_ctx["DATA"], "NEW-SID")["prev"] == "OLD-SID"


@pytest.mark.asyncio
async def test_a_resumed_turn_makes_no_link_and_keeps_the_mark(aiohttp_client, fake_ctx, app):
    _seed(fake_ctx, session_id="CUR-SID", rotated_from=dict(MARK))
    calls: list = []
    _fresh_engine(fake_ctx, calls)
    client = await aiohttp_client(app)
    with patch.object(_webapp, "_build_agents_kwargs", return_value={}), \
         patch.object(_webapp, "_secrets_read", return_value={}):
        resp = await client.post(f"/api/projects/{PROJECT_ID}/chat",
                                 json={"prompt": "hello", "chat_id": CHAT_ID},
                                 headers=_auth(fake_ctx))
        await _sse_events(resp)
    assert calls[0]["resume_session_id"] == "CUR-SID"
    assert _chat_record(fake_ctx).get("rotated_from") == MARK
    assert session_links.get(fake_ctx["DATA"], "NEW-SID") is None


def test_bind_ignores_a_link_to_itself_and_a_missing_mark(fake_ctx):
    chat = {"rotated_from": {"session_id": "SAME"}}
    _webapp._bind_rotation(fake_ctx, chat, "SAME", fresh=True)
    assert "rotated_from" not in chat and session_links.get(fake_ctx["DATA"], "SAME") is None
    _webapp._bind_rotation(fake_ctx, {}, "X", fresh=True)
    assert session_links.get(fake_ctx["DATA"], "X") is None


def test_a_first_hand_link_overrides_a_recognised_one_but_not_the_reverse(tmp_path):
    session_links.put(tmp_path, "S", {"prev": "GUESS", "inferred": True})
    session_links.put(tmp_path, "S", {"prev": "REAL", "inferred": False})
    session_links.put(tmp_path, "S", {"prev": "GUESS2", "inferred": True})
    assert session_links.get(tmp_path, "S")["prev"] == "REAL"
    session_links.put(tmp_path, "S", {"prev": "REAL2", "inferred": False})
    assert session_links.get(tmp_path, "S")["prev"] == "REAL"


# ─────────────────────────── the reset leaves the mark ────────────────────────


@pytest.mark.asyncio
async def test_rotation_leaves_a_mark_with_the_ended_session(fake_ctx):
    _seed(fake_ctx, session_id="ENDED-SID")
    fake_ctx.update({"pending_handoff": {}, "context_warned": set(), "live_clients": {},
                     "evict_live_client": None, "save_handoff": lambda: None})
    project = {"id": PROJECT_ID, "cwd": fake_ctx["topics"][SESSION_KEY]["cwd"],
               "session_key": SESSION_KEY}
    with patch.object(_webapp, "_chat_queue_drain_one", return_value=False), \
         patch.object(_webapp, "_spawn_bg", side_effect=lambda coro: coro.close()):
        res = await _webapp._rotate_session_core(fake_ctx, project, SESSION_KEY, False,
                                                 trigger="auto", context_tokens=751_000)
    assert res["reset"] is True
    rec = _chat_record(fake_ctx)
    assert rec["session_id"] is None
    mark = rec["rotated_from"]
    assert mark["session_id"] == "ENDED-SID" and mark["trigger"] == "auto"
    assert mark["context_tokens"] == 751_000 and mark["handoff"] is False
    assert abs(mark["at"] - time.time()) < 60


@pytest.fixture
def session_app(fake_ctx):
    from aiohttp import web
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = fake_ctx
    a.router.add_post("/api/projects/{id}/session", _webapp.api_project_set_session)
    a.router.add_get("/api/projects/{id}/session-history", _webapp.api_project_session_history)
    return a


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    d = tmp_path / "sdk"
    d.mkdir()
    monkeypatch.setattr(_webapp, "_sdk_sessions_dir", lambda cwd: d)
    return d


@pytest.mark.asyncio
async def test_new_session_marks_and_picking_an_old_one_clears(aiohttp_client, fake_ctx,
                                                               session_app, sdk):
    _seed(fake_ctx, session_id="WAS-SID")
    client = await aiohttp_client(session_app)
    r = await client.post(f"/api/projects/{PROJECT_ID}/session", json={"action": "new"},
                          headers=_auth(fake_ctx))
    assert r.status == 200
    mark = _chat_record(fake_ctx)["rotated_from"]
    assert mark["session_id"] == "WAS-SID" and mark["trigger"] == "new"
    (sdk / "PICKED.jsonl").write_text("")
    r = await client.post(f"/api/projects/{PROJECT_ID}/session",
                          json={"action": "resume", "session_id": "PICKED"},
                          headers=_auth(fake_ctx))
    assert r.status == 200
    rec = _chat_record(fake_ctx)
    assert rec["session_id"] == "PICKED" and "rotated_from" not in rec


# ─────────────────────────── session-history hands out the previous session ──


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write(path: Path, rows, mtime: "float | None" = None):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _user(text, ts, **extra):
    return {"type": "user", "timestamp": _iso(ts), "uuid": f"u-{text[:6]}-{ts}",
            "message": {"role": "user", "content": text}, **extra}


def _assistant(text, ts):
    return {"type": "assistant", "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _handoff(said: str, replied: str) -> str:
    return ("## Where we stopped\nPaging shipped.\n\n---\nFiles touched: a.py\n"
            "Last instruction (operator): " + said + "\n"
            "Where we stopped (agent's last message):\n" + replied)


def _first_prompt(handoff: str, prompt: str) -> str:
    return ("<context-pack>\nboard\n</context-pack>\n\n<prior-session-summary>\n"
            + _webapp._HANDOFF_PREAMBLE + handoff + "\n</prior-session-summary>\n\n" + prompt)


SAID = "Отлично. Можно ли сделать динамическую подгрузку чата при прокрутке вверх?"
REPLIED = "Перезапуск запущен, результат прочитаю в следующем сообщении."


def _pair(sdk: Path, now: float, *, said=SAID, replied=REPLIED, handoff=None):
    """P (ended before S began) and S (started from P's handoff), n messages in S."""
    _write(sdk / "P.jsonl", [_user("start", now - 900), _assistant("ok", now - 890),
                              _user(said, now - 600), _assistant(replied, now - 590)],
           mtime=now - 580)
    _write(sdk / "S.jsonl", [_user(_first_prompt(handoff or _handoff(said, replied), "next"), now),
                              _assistant("continuing", now + 5)])


async def _history(client, ctx, qs=""):
    r = await client.get(f"/api/projects/{PROJECT_ID}/session-history?{qs}", headers=_auth(ctx))
    assert r.status == 200
    return await r.json()


@pytest.mark.asyncio
async def test_a_recorded_link_is_served_once_the_feed_reaches_the_first_message(
        aiohttp_client, fake_ctx, session_app, sdk):
    now = time.time()
    _write(sdk / "P.jsonl", [_user("p", now - 100), _assistant("p-reply", now - 90)])
    _write(sdk / "S.jsonl", [_user(_first_prompt("the summary", "next"), now)]
           + [_assistant(f"a{i}", now + i) for i in range(1, 150)])
    session_links.put(fake_ctx["DATA"], "S", {"prev": "P", "trigger": "manual", "at": now - 50,
                                              "context_tokens": 600_000, "handoff": True})
    _webapp._save_session_labels(fake_ctx, {"P": "History paging"})
    _seed(fake_ctx, session_id="S")
    client = await aiohttp_client(session_app)
    tail = await _history(client, fake_ctx, "session_id=S")
    assert tail["messages"][0]["idx"] == 50 and tail["prev_session"] is None
    top = await _history(client, fake_ctx, "session_id=S&before=50")
    assert top["messages"][0]["idx"] == 0
    prev = top["prev_session"]
    assert prev == {"session_id": "P", "trigger": "manual", "at": now - 50,
                    "context_tokens": 600_000, "carried": True, "handoff": "the summary",
                    "label": "History paging", "available": True}
    # The first prompt still shows without the blocks the cockpit prepended to it.
    assert top["messages"][0]["text"] == "next"


@pytest.mark.asyncio
async def test_an_older_session_is_recognised_from_its_handoff_and_remembered(
        aiohttp_client, fake_ctx, session_app, sdk):
    now = time.time()
    _pair(sdk, now)
    # Decoys: the same last prompt but written after S began, and a newer unrelated session.
    _write(sdk / "LATER.jsonl", [_user(SAID, now + 300), _assistant(REPLIED, now + 301)],
           mtime=now + 400)
    _write(sdk / "OTHER.jsonl", [_user("unrelated", now - 60)], mtime=now - 50)
    _seed(fake_ctx, session_id="S")
    client = await aiohttp_client(session_app)
    body = await _history(client, fake_ctx, "session_id=S")
    prev = body["prev_session"]
    assert prev["session_id"] == "P" and prev["carried"] is True and prev["available"] is True
    assert prev["handoff"].startswith("## Where we stopped") and SAID in prev["handoff"]
    assert prev["trigger"] is None
    link = session_links.get(fake_ctx["DATA"], "S")
    assert link["prev"] == "P" and link["inferred"] is True


@pytest.mark.asyncio
async def test_a_short_last_prompt_also_needs_the_agents_reply_to_match(
        aiohttp_client, fake_ctx, session_app, sdk):
    now = time.time()
    _pair(sdk, now, said="да", replied=REPLIED, handoff=_handoff("да", "a different reply"))
    _seed(fake_ctx, session_id="S")
    client = await aiohttp_client(session_app)
    assert (await _history(client, fake_ctx, "session_id=S"))["prev_session"] is None
    assert session_links.get(fake_ctx["DATA"], "S") is None


@pytest.mark.asyncio
async def test_a_session_without_a_handoff_or_link_has_nothing_above(
        aiohttp_client, fake_ctx, session_app, sdk):
    now = time.time()
    _write(sdk / "S.jsonl", [_user("plain start", now), _assistant("hi", now + 1)])
    _seed(fake_ctx, session_id="S")
    client = await aiohttp_client(session_app)
    assert (await _history(client, fake_ctx, "session_id=S"))["prev_session"] is None


@pytest.mark.asyncio
async def test_a_chat_just_reset_shows_the_session_it_ended(aiohttp_client, fake_ctx,
                                                            session_app, sdk):
    _write(sdk / "OLD-SID.jsonl", [_user("x", time.time())])
    _seed(fake_ctx, session_id=None, rotated_from=dict(MARK))
    fake_ctx["sessions"].pop(SESSION_KEY, None)
    client = await aiohttp_client(session_app)
    body = await _history(client, fake_ctx)
    assert body["messages"] == [] and body["session_id"] is None
    prev = body["prev_session"]
    assert prev["session_id"] == "OLD-SID" and prev["trigger"] == "auto"
    assert prev["carried"] is True and prev["handoff"] is None and prev["available"] is True


def test_a_missing_predecessor_transcript_is_reported_unavailable(fake_ctx, sdk):
    _write(sdk / "S.jsonl", [_user("x", time.time())])
    session_links.put(fake_ctx["DATA"], "S", {"prev": "GONE", "trigger": "auto"})
    prev = _webapp._prev_session_info(fake_ctx, "cwd", "S")
    assert prev["session_id"] == "GONE" and prev["available"] is False
    assert prev["carried"] is False


def test_a_link_with_a_path_in_it_is_refused(fake_ctx, sdk):
    _write(sdk / "S.jsonl", [_user("x", time.time())])
    session_links.put(fake_ctx["DATA"], "S", {"prev": "../../etc/passwd"})
    assert _webapp._prev_session_info(fake_ctx, "cwd", "S") is None


@pytest.mark.asyncio
async def test_a_handoff_from_before_the_verbatim_tail_is_recognised_by_its_head(
        aiohttp_client, fake_ctx, session_app, sdk):
    """Handoffs built before 2026-10-08 quoted only the first 1000 characters of the last prompt."""
    now = time.time()
    long_said = "Сделай так, чтобы история не обрывалась. " * 40
    _pair(sdk, now, said=long_said, handoff=_handoff(long_said[:1000], REPLIED[:30]))
    _seed(fake_ctx, session_id="S")
    client = await aiohttp_client(session_app)
    assert (await _history(client, fake_ctx, "session_id=S"))["prev_session"]["session_id"] == "P"


def test_a_session_appended_to_hours_after_its_last_turn_is_still_a_candidate(fake_ctx, sdk):
    """The CLI appends untimed bookkeeping (last-prompt, mode) to a session long after its last
    turn, so the file's mtime can sit after the next session began; its last timed line does not."""
    now = time.time()
    _pair(sdk, now)
    with open(sdk / "P.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "last-prompt", "lastPrompt": SAID}) + "\n")
    os.utime(sdk / "P.jsonl", (now + 3 * 3600, now + 3 * 3600))
    assert _webapp._prev_session_info(fake_ctx, "cwd", "S")["session_id"] == "P"


def test_handoff_quotes_are_read_from_both_forms():
    said, replied = _webapp._handoff_quotes(_handoff("do it", "done\nall of it"))
    assert (said, replied) == ("do it", "done\nall of it")
    unanswered = ("x\nLast instruction (operator): line one\nline two\n"
                  "⚠️ The agent never answered this instruction — the session ended first.")
    assert _webapp._handoff_quotes(unanswered) == ("line one\nline two", "")
