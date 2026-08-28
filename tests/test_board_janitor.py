"""Board janitor tests — the Review column's automatic exit (phase 1).

Covers the decision policy (pure), the review timestamp round-trip through
TASKS.md, the manual accept-review endpoint, and one full sweep.

The policy under test has one load-bearing rule: a card is archived ONLY on
objective evidence. "No test signal" must never read as "safe to close".
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _tasks_path, _done_path, _derive_token
from features.board_janitor import logic as L


# ─────────────────────────── pure policy ───────────────────────────


def test_card_without_stamp_is_unknown_age_not_zero():
    assert L.card_age_hours({"id": "a", "text": "x"}) is None


def test_stamp_missing_rt_only_touches_unstamped():
    now = 1_700_000_000
    cards = [{"id": "a", "text": "x"}, {"id": "b", "text": "y", "rt": 123456789}]
    assert L.stamp_missing_rt(cards, now) == 1
    assert cards[0]["rt"] == now
    assert cards[1]["rt"] == 123456789


def test_run_is_settled_rejects_unapplied_worktree():
    ok, why = L.run_is_settled({"outcome": "ok", "has_changes": True, "applied": False, "discarded": False})
    assert ok is False
    assert "unapplied" in why


def test_run_is_settled_accepts_applied_and_clean_runs():
    assert L.run_is_settled({"outcome": "ok", "has_changes": True, "applied": True})[0] is True
    assert L.run_is_settled({"outcome": "ok", "has_changes": False})[0] is True


def test_run_is_settled_rejects_missing_or_failed():
    assert L.run_is_settled(None)[0] is False
    assert L.run_is_settled({"outcome": "err"})[0] is False


def _old_card(hours=30.0, now=None):
    now = now or time.time()
    return {"id": "abc123", "text": "done thing", "rt": int(now - hours * 3600)}


def test_accept_requires_green_tests():
    now = time.time()
    card = _old_card(now=now)
    meta = {"outcome": "ok", "has_changes": False}
    assert L.decide_card(card, meta, True, now)[0] == "accept"
    # No signal is NOT permission — this is the whole point of the tri-state.
    assert L.decide_card(card, meta, None, now)[0] == "hold"
    assert L.decide_card(card, meta, False, now)[0] == "hold"


def test_fresh_card_is_never_touched():
    now = time.time()
    assert L.decide_card(_old_card(1.0, now), {"outcome": "ok", "has_changes": False}, True, now)[0] == "hold"


def test_old_unacceptable_card_goes_to_digest():
    now = time.time()
    action, reason = L.decide_card(_old_card(100.0, now), None, None, now)
    assert action == "digest"
    assert reason


def test_mode_off_holds_everything():
    now = time.time()
    action, _ = L.decide_card(_old_card(500.0, now), {"outcome": "ok", "has_changes": False}, True, now, mode="off")
    assert action == "hold"


def test_digest_mode_never_accepts():
    now = time.time()
    action, _ = L.decide_card(_old_card(500.0, now), {"outcome": "ok", "has_changes": False}, True, now, mode="digest")
    assert action == "digest"


def test_build_digest_groups_and_counts():
    out = L.build_digest([
        {"project": "p1", "card_id": "a1", "text": "one", "action": "accept", "reason": "ok"},
        {"project": "p2", "card_id": "b2", "text": "two", "action": "digest", "reason": "no tests", "age_h": 99},
    ])
    assert "Auto-accepted (1)" in out
    assert "Waiting on you (1)" in out
    assert "no tests" in out


def test_build_digest_empty_is_still_readable():
    assert "Nothing parked" in L.build_digest([])


# ─────────────────────────── timestamp round-trip ───────────────────────────


def test_review_timestamp_survives_a_board_round_trip(tmp_path):
    import board
    raw = "# Tasks — t\n\n## Review\n- [?] card <!--ops:abc123 rt=1787900000-->\n"
    pre, cols = board._parse_tasks(raw)
    assert cols["review"][0]["rt"] == 1787900000
    assert "rt=1787900000" in board._serialize_tasks(pre, cols, "t")


def test_garbage_timestamp_is_ignored_not_crashing():
    import board
    _, cols = board._parse_tasks("# t\n\n## Review\n- [?] c <!--ops:abc123 rt=notanumber-->\n")
    assert "rt" not in cols["review"][0]


# ─────────────────────────── endpoint ───────────────────────────


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "myproject"
    p.mkdir()
    return p


@pytest.fixture
def ctx(tmp_path, project_dir):
    data_dir = tmp_path / "data"
    (data_dir / "runs").mkdir(parents=True)
    password = "testpass"
    c = {
        "topics": {"1001:42": {"project": "myproject", "cwd": str(project_dir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": password, "DATA": data_dir, "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects", "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    c["_auth_token"] = _derive_token(password)
    return c


@pytest.fixture
def app(ctx):
    from aiohttp import web
    from features.board_janitor.routes import add_routes
    a = web.Application(middlewares=[_webapp.auth_middleware])
    a["ctx"] = ctx
    add_routes(a)
    return a


def _board(project_dir, review_cards):
    lines = ["# Tasks — myproject", "", "## Backlog", "", "## In Progress", "", "## Review"]
    lines += [f"- [?] {t} <!--ops:{cid}{f' rt={rt}' if rt else ''}-->" for cid, t, rt in review_cards]
    lines += ["", "## Failed", ""]
    _tasks_path(str(project_dir)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_accept_all_archives_every_review_card(aiohttp_client, app, ctx, project_dir):
    _board(project_dir, [("aaa111", "first", None), ("bbb222", "second", None)])
    client = await aiohttp_client(app)
    resp = await client.post("/api/projects/myproject/cards/accept-review",
                             json={"all": True}, headers=_h(ctx))
    assert resp.status == 200
    body = await resp.json()
    assert body["accepted"] == 2
    done = _done_path(str(project_dir)).read_text(encoding="utf-8")
    assert "first" in done and "second" in done
    assert "aaa111" not in _tasks_path(str(project_dir)).read_text(encoding="utf-8")


async def test_accept_by_id_leaves_the_others(aiohttp_client, app, ctx, project_dir):
    _board(project_dir, [("aaa111", "first", None), ("bbb222", "second", None)])
    client = await aiohttp_client(app)
    resp = await client.post("/api/projects/myproject/cards/accept-review",
                             json={"ids": ["aaa111"]}, headers=_h(ctx))
    assert resp.status == 200
    assert (await resp.json())["accepted"] == 1
    tasks = _tasks_path(str(project_dir)).read_text(encoding="utf-8")
    assert "bbb222" in tasks and "aaa111" not in tasks


async def test_accept_unknown_card_is_404(aiohttp_client, app, ctx, project_dir):
    _board(project_dir, [("aaa111", "first", None)])
    client = await aiohttp_client(app)
    resp = await client.post("/api/projects/myproject/cards/accept-review",
                             json={"ids": ["zzz999"]}, headers=_h(ctx))
    assert resp.status == 404


async def test_accept_rejects_bad_card_id(aiohttp_client, app, ctx, project_dir):
    _board(project_dir, [("aaa111", "first", None)])
    client = await aiohttp_client(app)
    resp = await client.post("/api/projects/myproject/cards/accept-review",
                             json={"ids": ["../../etc/passwd"]}, headers=_h(ctx))
    assert resp.status == 400


async def test_accept_empty_body_is_rejected(aiohttp_client, app, ctx, project_dir):
    _board(project_dir, [("aaa111", "first", None)])
    client = await aiohttp_client(app)
    resp = await client.post("/api/projects/myproject/cards/accept-review", json={}, headers=_h(ctx))
    assert resp.status == 400


# ─────────────────────────── one full sweep ───────────────────────────


async def test_sweep_stamps_then_holds_on_first_sight(ctx, project_dir):
    """An unstamped legacy card is stamped, not archived on the spot."""
    from features.board_janitor.loop import _janitor_tick_once
    _board(project_dir, [("aaa111", "legacy card", None)])
    summary = await _janitor_tick_once(ctx)
    assert summary["accepted"] == 0
    assert "rt=" in _tasks_path(str(project_dir)).read_text(encoding="utf-8")


async def test_sweep_accepts_only_with_evidence(ctx, project_dir, monkeypatch):
    from features.board_janitor import loop as JL
    old = int(time.time() - 200 * 3600)
    _board(project_dir, [("aaa111", "settled work", old), ("bbb222", "no run record", old)])
    (ctx["DATA"] / "runs" / "aaa111.json").write_text(json.dumps(
        {"card_id": "aaa111", "outcome": "ok", "has_changes": False}), encoding="utf-8")

    async def _green(_project):
        return True
    monkeypatch.setattr(JL, "_test_signal", _green)

    summary = await JL._janitor_tick_once(ctx)
    assert summary["accepted"] == 1
    tasks = _tasks_path(str(project_dir)).read_text(encoding="utf-8")
    assert "aaa111" not in tasks          # archived
    assert "bbb222" in tasks              # no run record -> stays put
    assert "settled work" in _done_path(str(project_dir)).read_text(encoding="utf-8")
    digests = list((ctx["DATA"] / "inbox").glob("board-digest-*.md"))
    assert digests, "a sweep with results must leave a digest"


async def test_sweep_without_test_signal_archives_nothing(ctx, project_dir, monkeypatch):
    from features.board_janitor import loop as JL
    old = int(time.time() - 200 * 3600)
    _board(project_dir, [("aaa111", "settled work", old)])
    (ctx["DATA"] / "runs" / "aaa111.json").write_text(json.dumps(
        {"card_id": "aaa111", "outcome": "ok", "has_changes": False}), encoding="utf-8")

    async def _unknown(_project):
        return None
    monkeypatch.setattr(JL, "_test_signal", _unknown)

    summary = await JL._janitor_tick_once(ctx)
    assert summary["accepted"] == 0
    assert "aaa111" in _tasks_path(str(project_dir)).read_text(encoding="utf-8")


# ─────────────────────────── fleet-wide autonomy switch ───────────────────────────


def test_autonomy_switch_defaults_to_running(tmp_path):
    assert L.autonomy_paused(tmp_path) is False


def test_autonomy_switch_round_trips(tmp_path):
    L.set_autonomy_paused(tmp_path, True)
    assert L.autonomy_paused(tmp_path) is True
    L.set_autonomy_paused(tmp_path, False)
    assert L.autonomy_paused(tmp_path) is False


def test_unreadable_switch_does_not_freeze_the_cockpit(tmp_path):
    (tmp_path / "autonomy.json").write_text("{ not json", encoding="utf-8")
    assert L.autonomy_paused(tmp_path) is False


async def test_paused_sweep_touches_nothing(ctx, project_dir, monkeypatch):
    from features.board_janitor import loop as JL
    old = int(time.time() - 200 * 3600)
    _board(project_dir, [("aaa111", "settled work", old)])
    (ctx["DATA"] / "runs" / "aaa111.json").write_text(json.dumps(
        {"card_id": "aaa111", "outcome": "ok", "has_changes": False}), encoding="utf-8")

    async def _green(_project):
        return True
    monkeypatch.setattr(JL, "_test_signal", _green)
    L.set_autonomy_paused(ctx["DATA"], True)

    summary = await JL._janitor_tick_once(ctx)
    assert summary.get("paused") is True
    assert summary["accepted"] == 0
    assert "aaa111" in _tasks_path(str(project_dir)).read_text(encoding="utf-8")


async def test_autonomy_endpoint_toggles(aiohttp_client, app, ctx):
    client = await aiohttp_client(app)
    resp = await client.post("/api/autonomy", json={"paused": True}, headers=_h(ctx))
    assert resp.status == 200
    resp = await client.get("/api/autonomy", headers=_h(ctx))
    assert (await resp.json())["paused"] is True
    resp = await client.post("/api/autonomy", json={"paused": "yes"}, headers=_h(ctx))
    assert resp.status == 400
