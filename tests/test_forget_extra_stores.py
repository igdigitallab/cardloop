"""spec-098: the stores found while building that the spec's inventory missed.

chat-trace.jsonl (120-char previews of operator messages), deferred.json (prompts of deferred turns
that already ran), plans/<id>.json + the plan file the CLI wrote, and the engine's pending rotate
handoff. Only the deleted session's entries go; another chat's entries, entries outside the
session's time span and a still-pending deferred prompt (future operator input) stay.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import forget
import forget_claude  # noqa: F401 - registers the adapter
import webapp
from _forget_world import K_PROMPT, SENT, TEXT_PROMPT, TEXT_REPLY, World, j
from test_forget_core import LOOP_KEY, forget_it, plan_for


@pytest.fixture
def world(tmp_path, monkeypatch):
    forget._CRASH_AFTER = None
    w = World(tmp_path, monkeypatch)
    yield w
    forget._CRASH_AFTER = None
    forget._CONFIG.clear()


def _seed(w: World) -> dict:
    t0, d, h = w.t0, w.data, w.home
    trace = d / "chat-trace.jsonl"
    rows = {
        "mine": {"ts": t0 + 0.1, "session_key": LOOP_KEY, "stage": "recv", "chat_id": "c1",
                 "preview": TEXT_PROMPT[:120]},
        "other_chat": {"ts": t0 + 30.1, "session_key": LOOP_KEY, "stage": "recv", "chat_id": "c2",
                       "preview": K_PROMPT[:120]},
        "long_ago": {"ts": t0 - 86400, "session_key": LOOP_KEY, "stage": "recv", "chat_id": "c1",
                     "preview": "an older session of the same chat"},
        "other_project": {"ts": t0 + 0.2, "session_key": "9999:1", "stage": "recv", "chat_id": "c1",
                          "preview": "another project"},
    }
    trace.write_bytes(b"".join(j(r) for r in rows.values()))
    from datetime import datetime, timezone
    inside = datetime.fromtimestamp(t0 + 1, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    (d / "deferred.json").write_text(json.dumps([
        {"id": "def-fired-mine", "session_key": LOOP_KEY, "prompt": TEXT_PROMPT, "status": "fired", "fired_at": inside},
        {"id": "def-pending-mine", "session_key": LOOP_KEY, "prompt": TEXT_PROMPT, "status": "pending", "created": inside},
        {"id": "def-fired-other", "session_key": LOOP_KEY, "prompt": K_PROMPT, "status": "fired", "fired_at": inside},
    ], indent=2))
    plans_home = h / ".claude" / "plans"
    plans_home.mkdir(parents=True, exist_ok=True)
    (plans_home / "mine.md").write_text(f"# plan\n{SENT}\n")
    (plans_home / "other.md").write_text("# other plan\n")
    pdir = d / "plans"
    pdir.mkdir(exist_ok=True)
    (pdir / "aaaa1111.json").write_text(json.dumps({
        "id": "aaaa1111", "session_key": LOOP_KEY, "chat_id": "c1", "created_at": str(t0 + 1),
        "plan_text": f"plan with {SENT}", "plan_file_path": str(plans_home / "mine.md")}))
    (pdir / "bbbb2222.json").write_text(json.dumps({
        "id": "bbbb2222", "session_key": LOOP_KEY, "chat_id": "c2", "created_at": str(t0 + 31),
        "plan_text": "other chat's plan", "plan_file_path": str(plans_home / "other.md")}))
    return rows


def _trace_rows(w: World) -> "list[dict]":
    return [json.loads(x) for x in (w.data / "chat-trace.jsonl").read_text().splitlines() if x.strip()]


async def test_extra_stores_lose_only_the_sessions_entries(world):
    rows = _seed(world)
    ctx = world.ctx()
    pend = {LOOP_KEY: f"## Where we stopped\n{TEXT_REPLY}", "other-key": f"unrelated {TEXT_REPLY}"}
    ctx["pending_handoff"] = pend
    saved = []
    ctx["save_handoff"] = lambda: saved.append(dict(pend))
    from test_forget_core import plan_for as _pf
    ctx, env, adapter, plan = await _pf(world, ctx=ctx)
    counts = await forget.execute_plan(env, adapter, ctx, plan)

    left = _trace_rows(world)
    previews = {r["preview"] for r in left}
    assert rows["mine"]["preview"] not in previews
    assert {rows["other_chat"]["preview"], rows["long_ago"]["preview"], rows["other_project"]["preview"]} <= previews
    assert counts.get("trace_lines") == 1

    ids = [r["id"] for r in json.loads((world.data / "deferred.json").read_text())]
    assert ids == ["def-pending-mine", "def-fired-other"]

    assert not (world.data / "plans" / "aaaa1111.json").exists()
    assert not (world.home / ".claude" / "plans" / "mine.md").exists()
    assert (world.data / "plans" / "bbbb2222.json").exists()
    assert (world.home / ".claude" / "plans" / "other.md").exists()

    assert LOOP_KEY not in pend and "other-key" in pend
    assert saved and LOOP_KEY not in saved[-1]


async def test_pending_record_names_the_extra_entries_without_text(world):
    _seed(world)
    ctx, env, adapter, plan = await plan_for(world)
    rec = json.dumps(plan.record())
    assert SENT not in rec and TEXT_PROMPT not in rec
    assert plan.extra["deferred"] == ["def-fired-mine"] and plan.extra["plans"] == ["aaaa1111"]


async def test_a_plan_file_outside_the_cli_plans_dir_is_never_deleted(world):
    _seed(world)
    outside = world.tmp / "elsewhere.md"
    outside.write_text("operator's own file")
    rec = json.loads((world.data / "plans" / "aaaa1111.json").read_text())
    rec["plan_file_path"] = str(outside)
    (world.data / "plans" / "aaaa1111.json").write_text(json.dumps(rec))
    await forget_it(world)
    assert outside.read_text() == "operator's own file"
    assert not (world.data / "plans" / "aaaa1111.json").exists()


async def test_armed_runtime_handoff_matched_by_its_recorded_source_session(world):
    ctx = world.ctx()
    chats = json.loads((world.data / "chats.json").read_text()) if (world.data / "chats.json").exists() else {}
    entry = chats.setdefault("proj", {"active": "cX", "chats": []})
    entry.setdefault("chats", []).append({"id": "cX", "provider": "codex", "runtime_handoff": {
        "text": "no distinctive strings here at all", "created_at": 123.0,
        "from_provider": "claude", "from_session": "11111111-aaaa-4aaa-8aaa-111111111111"}})
    (world.data / "chats.json").write_text(json.dumps(chats))
    ctx2, env, adapter, plan = await plan_for(world, ctx=ctx)
    assert {"project_id": "proj", "chat_id": "cX", "created_at": 123.0} in plan.handoffs


async def test_same_prompt_deferred_outside_the_session_span_stays(world):
    """Review G10: a fired deferred record with the same text in another (earlier) session of the
    chat is not this session's."""
    _seed(world)
    recs = json.loads((world.data / "deferred.json").read_text())
    for r in recs:
        r["fired_at"] = "2026-11-18T10:00:00Z"
    recs.append({"id": "def-fired-long-ago", "session_key": LOOP_KEY, "prompt": TEXT_PROMPT,
                 "status": "fired", "fired_at": "2020-01-01T00:00:00Z"})
    from datetime import datetime, timezone
    inside = datetime.fromtimestamp(world.t0 + 1, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    for r in recs[:3]:
        r["fired_at"] = inside
    (world.data / "deferred.json").write_text(json.dumps(recs))
    await forget_it(world)
    ids = [r["id"] for r in json.loads((world.data / "deferred.json").read_text())]
    assert ids == ["def-pending-mine", "def-fired-other", "def-fired-long-ago"]


async def test_a_symlinked_plans_dir_is_never_a_delete_root(world):
    _seed(world)
    real = world.tmp / "operator-notes"
    real.mkdir()
    (real / "keep.md").write_text("operator's own")
    plans = world.home / ".claude" / "plans"
    import shutil
    shutil.rmtree(plans)
    plans.symlink_to(real)
    rec = json.loads((world.data / "plans" / "aaaa1111.json").read_text())
    rec["plan_file_path"] = str(plans / "keep.md")
    (world.data / "plans" / "aaaa1111.json").write_text(json.dumps(rec))
    await forget_it(world)
    assert (real / "keep.md").read_text() == "operator's own"
