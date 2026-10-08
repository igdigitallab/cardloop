"""spec-096 P9 item A: the host never executes a project's tests as the verdict for Grok work.

Grok runs inside a sandbox, but the cockpit's own test runners (the board janitor's
`test_cmd`, the card quality gate, the autopilot shadow signal) execute the PROJECT's code
(conftest.py, Makefile, venv/bin/pytest, package.json scripts) on the host, unsandboxed, with
the project's secrets in the environment. A Grok-edited conftest.py would therefore be a sandbox
escape. Rule under test: a run/card whose engine was Grok gets "no test signal: Grok work is not
executed on the host" - and, being the conservative reading for a MIXED project, so does every
other card of a project that has live Grok work (a Grok card in flight/review, or a Grok chat).

Every "refuses" test has a control that runs the SAME setup on Claude and proves the probe
fires, so a probe that never runs cannot make the refusal look green.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _tasks_path, _done_path

GROK_MSG = "no test signal: Grok work is not executed on the host"


# ─────────────────────────── helpers ───────────────────────────


def _board(project_dir, review_cards):
    lines = ["# Tasks - proj", "", "## Backlog", "", "## In Progress", "", "## Review"]
    lines += [f"- [?] {t} <!--ops:{cid}{f' rt={rt}' if rt else ''}-->" for cid, t, rt in review_cards]
    lines += ["", "## Failed", ""]
    _tasks_path(str(project_dir)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _meta(ctx, card_id, provider=None):
    m = {"card_id": card_id, "outcome": "ok", "has_changes": False}
    if provider is not None:
        m["provider"] = provider
    (ctx["DATA"] / "runs" / f"{card_id}.json").write_text(json.dumps(m), encoding="utf-8")


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    # The "project test": proves, by side effect, that the host executed project code.
    (p / "probe.py").write_text(
        "from pathlib import Path\nPath('probe_ran').write_text('x')\n", encoding="utf-8")
    return p


@pytest.fixture
def ctx(tmp_path, project_dir):
    data_dir = tmp_path / "data"
    (data_dir / "runs").mkdir(parents=True)
    return {
        "topics": {"1001:42": {"project": "proj", "cwd": str(project_dir), "model": "sonnet",
                               "test_cmd": "python3 probe.py"}},
        "sessions": {}, "running": {}, "password": "pw", "DATA": data_dir, "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects", "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }


def _pid(ctx):
    return _webapp._collect_projects(ctx)[0]["id"]


def _old():
    return int(time.time() - 200 * 3600)


def _chats(ctx, chats):
    (ctx["DATA"] / "chats.json").write_text(
        json.dumps({_pid(ctx): {"active": chats[0]["id"], "chats": chats}}), encoding="utf-8")


# ─────────────────────────── board janitor ───────────────────────────


async def test_janitor_control_claude_card_runs_tests_and_is_accepted(ctx, project_dir):
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "claude work", _old())])
    _meta(ctx, "aaa111", "claude")
    summary = await JL._janitor_tick_once(ctx)
    assert (project_dir / "probe_ran").exists(), "control: the host must run a Claude project's tests"
    assert summary["accepted"] == 1


async def test_janitor_never_executes_tests_for_a_grok_card(ctx, project_dir):
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "grok work", _old())])
    _meta(ctx, "aaa111", "grok")
    summary = await JL._janitor_tick_once(ctx)
    assert not (project_dir / "probe_ran").exists(), "project code ran on the host for Grok work"
    assert summary["accepted"] == 0
    assert "aaa111" in _tasks_path(str(project_dir)).read_text(encoding="utf-8")
    reasons = [e["reason"] for e in summary["entries"]]
    assert any(GROK_MSG in r for r in reasons), reasons


async def test_janitor_mixed_project_a_claude_card_does_not_run_the_tree_a_grok_card_edited(ctx, project_dir):
    """Conservative rule: the tests run in the PROJECT tree, which a Grok card may have edited
    in place - one Grok card in Review/In progress/Failed means no host test for the project."""
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "claude work", _old()), ("bbb222", "grok work", _old())])
    _meta(ctx, "aaa111", "claude")
    _meta(ctx, "bbb222", "grok")
    summary = await JL._janitor_tick_once(ctx)
    assert not (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 0


async def test_janitor_decides_from_the_run_record_not_the_project_default(ctx, project_dir):
    """board_provider says Grok but the card's own run record says Claude -> Claude (the record
    wins). The reverse - default Claude, record Grok - is the escape the first test covers."""
    from features.board_janitor import loop as JL
    ctx["topics"]["1001:42"]["board_provider"] = "grok"
    _board(project_dir, [("aaa111", "claude work", _old())])
    _meta(ctx, "aaa111", "claude")
    summary = await JL._janitor_tick_once(ctx)
    assert (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 1


async def test_janitor_card_without_a_provider_in_its_record_falls_back_to_the_card_default(ctx, project_dir):
    from features.board_janitor import loop as JL
    ctx["topics"]["1001:42"]["board_provider"] = "grok"
    _board(project_dir, [("aaa111", "old record", _old())])
    _meta(ctx, "aaa111")          # a record from before the provider field existed
    summary = await JL._janitor_tick_once(ctx)
    assert not (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 0


async def test_janitor_a_grok_chat_taints_the_project_tree(ctx, project_dir):
    """A Grok CHAT edits the project tree in place just like a card does."""
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "claude work", _old())])
    _meta(ctx, "aaa111", "claude")
    _chats(ctx, [{"id": "c1", "name": "g", "provider": "grok"}])
    summary = await JL._janitor_tick_once(ctx)
    assert not (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 0


async def test_janitor_a_chat_switched_off_grok_still_carries_its_grok_session(ctx, project_dir):
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "claude work", _old())])
    _meta(ctx, "aaa111", "claude")
    _chats(ctx, [{"id": "c1", "name": "was grok", "provider": "claude", "grok_session_id": "s-1"}])
    summary = await JL._janitor_tick_once(ctx)
    assert not (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 0


async def test_janitor_a_claude_chat_does_not_taint(ctx, project_dir):
    from features.board_janitor import loop as JL
    _board(project_dir, [("aaa111", "claude work", _old())])
    _meta(ctx, "aaa111", "claude")
    _chats(ctx, [{"id": "c1", "name": "c", "provider": "claude", "session_id": "x"}])
    summary = await JL._janitor_tick_once(ctx)
    assert (project_dir / "probe_ran").exists()
    assert summary["accepted"] == 1


# ─────────────────────────── card quality gate ───────────────────────────


def _gate_project(tmp_path):
    """A card worktree whose `make test` proves, by side effect, that the host ran its code."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "Makefile").write_text("test:\n\ttouch gate_ran\n", encoding="utf-8")
    return wt


async def _check(ctx, project_dir, card_id):
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    app = web.Application()
    app["ctx"] = ctx
    pid = _pid(ctx)
    req = make_mocked_request("POST", f"/api/projects/{pid}/tasks/{card_id}/check",
                              match_info={"id": pid, "card": card_id}, app=app)
    return await _webapp.api_card_check(req)


def _wt_meta(ctx, card_id, wt, provider):
    m = {"card_id": card_id, "mode": "worktree", "branch": f"card-{card_id}", "base_branch": "main",
         "wt_path": str(wt), "has_changes": True, "applied": False, "discarded": False,
         "outcome": "ok"}
    if provider is not None:
        m["provider"] = provider
    _webapp._write_run_meta(ctx["DATA"], card_id, m)


async def test_gate_control_claude_worktree_is_tested_on_the_host(ctx, project_dir, tmp_path):
    wt = _gate_project(tmp_path)
    _wt_meta(ctx, "aabbcc", wt, "claude")
    resp = await _check(ctx, project_dir, "aabbcc")
    body = json.loads(resp.body)
    assert (wt / "gate_ran").exists(), "control: the gate must run a Claude worktree's tests"
    assert body["verdict"] == "safe"


async def test_gate_never_executes_a_grok_worktree(ctx, project_dir, tmp_path):
    wt = _gate_project(tmp_path)
    _wt_meta(ctx, "aabbcc", wt, "grok")
    resp = await _check(ctx, project_dir, "aabbcc")
    body = json.loads(resp.body)
    assert not (wt / "gate_ran").exists(), "a Grok-edited Makefile ran on the host"
    assert body["verdict"] == "unknown"
    assert GROK_MSG in body["tests"]["output"]
    assert body["tests"]["detected"] is False and body["tests"]["ok"] is False
    # the verdict is still written to the sidecar, as "unknown" - never as "safe"
    assert _webapp._read_run_meta(ctx["DATA"], "aabbcc")["gate"]["verdict"] == "unknown"


async def test_gate_a_claude_card_in_a_project_with_grok_work_in_flight_is_not_run(ctx, project_dir, tmp_path):
    wt = _gate_project(tmp_path)
    _wt_meta(ctx, "aabbcc", wt, "claude")
    _board(project_dir, [("aabbcc", "claude work", _old()), ("ddeeff", "grok work", _old())])
    _meta(ctx, "ddeeff", "grok")
    resp = await _check(ctx, project_dir, "aabbcc")
    assert not (wt / "gate_ran").exists()
    assert json.loads(resp.body)["verdict"] == "unknown"


async def test_gate_a_record_without_a_provider_uses_the_card_default(ctx, project_dir, tmp_path):
    wt = _gate_project(tmp_path)
    ctx["topics"]["1001:42"]["board_provider"] = "grok"
    _wt_meta(ctx, "aabbcc", wt, None)
    _board(project_dir, [("aabbcc", "work", _old())])
    resp = await _check(ctx, project_dir, "aabbcc")
    assert not (wt / "gate_ran").exists()
    assert json.loads(resp.body)["verdict"] == "unknown"


# ─────────────────────────── autopilot shadow signal ───────────────────────────


async def test_autopilot_signal_does_not_execute_a_project_with_grok_work(ctx, project_dir):
    from features.autopilot import loop as AL
    project = _webapp._collect_projects(ctx)[0]
    # control: the configured test_cmd runs for a clean project
    failing, _summary = await AL._autopilot_test_signal(project, ctx)
    assert (project_dir / "probe_ran").exists()
    (project_dir / "probe_ran").unlink()
    _board(project_dir, [("aaa111", "grok work", _old())])
    _meta(ctx, "aaa111", "grok")
    project = _webapp._collect_projects(ctx)[0]
    failing, summary = await AL._autopilot_test_signal(project, ctx)
    assert failing is None
    assert GROK_MSG in summary
    assert not (project_dir / "probe_ran").exists()
