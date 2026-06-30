"""Tests for usage_pricing + usage_scanner (transcript scan → SQLite → dashboard).

Synthetic transcripts exercise: token capture, message-id dedup, sub-agent
detection + dispatch attribution, incremental re-scan, and the dashboard_data
aggregation (cost, range filter, model filter, sub-agent-by-type).
"""

import json
import time
from pathlib import Path

import pytest

import usage_pricing
import usage_scanner


# ──────────────────────────── pricing ────────────────────────────
def test_is_billable():
    assert usage_pricing.is_billable("claude-opus-4-8")
    assert usage_pricing.is_billable("claude-fable-5")
    assert usage_pricing.is_billable("claude-haiku-4-5-20251001")
    assert not usage_pricing.is_billable("gpt-4o")
    assert not usage_pricing.is_billable("gemini-2.5-flash")
    assert not usage_pricing.is_billable("")
    assert not usage_pricing.is_billable(None)


def test_get_pricing_resolution():
    # exact
    assert usage_pricing.get_pricing("claude-opus-4-8")["input"] == 5.00
    # dated suffix → startswith
    assert usage_pricing.get_pricing("claude-haiku-4-5-20251001")["output"] == 5.00
    # keyword fallback onto newest of family
    assert usage_pricing.get_pricing("claude-opus-9-9") == usage_pricing.PRICING["claude-opus-4-8"]
    assert usage_pricing.get_pricing("something-sonnet-ish")["input"] == 3.00
    # unknown / local → None
    assert usage_pricing.get_pricing("llama-3") is None
    assert usage_pricing.get_pricing(None) is None


def test_calc_cost_math():
    # 1M input on opus-4-8 = $5.00 exactly
    assert usage_pricing.calc_cost("claude-opus-4-8", 1_000_000, 0, 0, 0) == pytest.approx(5.00)
    # output dominates: 1M output = $25
    assert usage_pricing.calc_cost("claude-opus-4-8", 0, 1_000_000, 0, 0) == pytest.approx(25.00)
    # cache_read is the cheap lane: 1M = $0.50
    assert usage_pricing.calc_cost("claude-opus-4-8", 0, 0, 1_000_000, 0) == pytest.approx(0.50)
    # non-billable → 0
    assert usage_pricing.calc_cost("gpt-4o", 9_999_999, 9_999_999, 0, 0) == 0.0


# ──────────────────────────── transcript fixtures ────────────────────────────
def _assistant(session_id, msg_id, model="claude-opus-4-8", out=100, inp=10,
               cr=0, cc=0, ts="2026-06-20T10:00:00Z", cwd="/home/u/proj",
               tool=None, extra=None):
    msg = {"id": msg_id, "model": model,
           "usage": {"input_tokens": inp, "output_tokens": out,
                     "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}
    if tool:
        msg["content"] = [{"type": "tool_use", "name": tool}]
    rec = {"type": "assistant", "sessionId": session_id, "timestamp": ts,
           "cwd": cwd, "gitBranch": "master", "message": msg}
    if extra:
        rec.update(extra)
    return json.dumps(rec)


def _write_jsonl(path: Path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_captures_tokens_and_dedups(tmp_path):
    # two records share a message id (streaming) → dedup keeps the last (final tally)
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [
        _assistant("S1", "m1", out=50),
        _assistant("S1", "m1", out=120),   # final usage for m1
        _assistant("S1", "m2", out=30),
    ])
    metas, turns, agents, lines = usage_scanner.parse_jsonl_file(str(f))
    assert lines == 3
    by_id = {t["message_id"]: t for t in turns}
    assert by_id["m1"]["output_tokens"] == 120  # last wins
    assert len(turns) == 2  # m1 (deduped) + m2


def test_zero_usage_turns_skipped(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [_assistant("S1", "m1", out=0, inp=0, cr=0, cc=0)])
    _, turns, _, _ = usage_scanner.parse_jsonl_file(str(f))
    assert turns == []


def test_subagent_detection_variants(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, [
        _assistant("S1", "m1"),                                   # normal
        _assistant("S1", "m2", extra={"isSidechain": True}),       # sidechain
        _assistant("S1", "m3", extra={"agentId": "ag-9"}),         # agentId
    ])
    _, turns, _, _ = usage_scanner.parse_jsonl_file(str(f))
    flags = {t["message_id"]: t["is_subagent"] for t in turns}
    assert flags["m1"] == 0
    assert flags["m2"] == 1
    assert flags["m3"] == 1


def test_subagent_path_detection(tmp_path):
    sub = tmp_path / "subagents"
    sub.mkdir()
    f = sub / "ag.jsonl"
    _write_jsonl(f, [_assistant("S1", "m1")])
    _, turns, _, _ = usage_scanner.parse_jsonl_file(str(f))
    assert turns[0]["is_subagent"] == 1


def test_dispatch_extraction():
    rec = json.loads(json.dumps({
        "type": "user", "sessionId": "S1", "timestamp": "2026-06-20T10:00:00Z",
        "toolUseResult": {"agentId": "ag-1", "agentType": "Explore", "status": "completed",
                          "totalTokens": 1234, "totalDurationMs": 5000, "totalToolUseCount": 7},
    }))
    d = usage_scanner.extract_agent_dispatch(rec)
    assert d["agent_id"] == "ag-1"
    assert d["agent_type"] == "Explore"
    assert d["tool_use_count"] == 7
    # a plain user record (no toolUseResult) → None
    assert usage_scanner.extract_agent_dispatch({"type": "user"}) is None


# ──────────────────────────── scan + incremental ────────────────────────────
def test_scan_and_incremental(tmp_path):
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    db = tmp_path / "usage.db"
    f = proj / "s.jsonl"
    _write_jsonl(f, [_assistant("S1", "m1", out=100), _assistant("S1", "m2", out=200)])

    r1 = usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    assert r1["new"] == 1 and r1["turns"] == 2

    # re-scan with no change → all skipped, no new turns
    r2 = usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    assert r2["new"] == 0 and r2["updated"] == 0 and r2["skipped"] == 1 and r2["turns"] == 0

    # append a new line → incremental picks up ONLY the new turn
    time.sleep(0.02)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_assistant("S1", "m3", out=300) + "\n")
    r3 = usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    assert r3["updated"] == 1 and r3["turns"] == 1

    import sqlite3
    conn = sqlite3.connect(db)
    total = conn.execute("SELECT SUM(output_tokens) FROM turns").fetchone()[0]
    assert total == 600  # 100 + 200 + 300, no double-count
    conn.close()


def test_dashboard_data_shape_and_cost(tmp_path):
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    db = tmp_path / "usage.db"
    f = proj / "s.jsonl"
    _write_jsonl(f, [
        _assistant("S1", "m1", model="claude-opus-4-8", inp=1_000_000, out=0),  # $5
        _assistant("S2", "m2", model="claude-sonnet-4-6", inp=1_000_000, out=0),  # $3
    ])
    usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    d = usage_scanner.dashboard_data(db_path=db, days=None)

    assert d["ready"] is True
    assert d["overview"]["turns"] == 2
    assert d["overview"]["cost"] == pytest.approx(8.0)  # 5 + 3
    models = {m["model"]: m for m in d["by_model"]}
    assert models["claude-opus-4-8"]["cost"] == pytest.approx(5.0)
    assert models["claude-sonnet-4-6"]["cost"] == pytest.approx(3.0)
    assert {"by_day", "by_project", "subagents", "recent_sessions", "all_models"} <= d.keys()
    assert d["pricing_as_of"]


def test_dashboard_model_filter(tmp_path):
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    db = tmp_path / "usage.db"
    _write_jsonl(proj / "s.jsonl", [
        _assistant("S1", "m1", model="claude-opus-4-8", inp=1_000_000, out=0),
        _assistant("S2", "m2", model="claude-sonnet-4-6", inp=1_000_000, out=0),
    ])
    usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    d = usage_scanner.dashboard_data(db_path=db, days=None, models=["claude-opus-4-8"])
    assert d["overview"]["cost"] == pytest.approx(5.0)
    assert [m["model"] for m in d["by_model"]] == ["claude-opus-4-8"]


def test_dashboard_range_filter(tmp_path):
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    db = tmp_path / "usage.db"
    old = "2020-01-01T10:00:00Z"
    new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_jsonl(proj / "s.jsonl", [
        _assistant("S1", "m1", out=100, ts=old),
        _assistant("S2", "m2", out=200, ts=new),
    ])
    usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    recent = usage_scanner.dashboard_data(db_path=db, days=7)
    alltime = usage_scanner.dashboard_data(db_path=db, days=None)
    assert recent["overview"]["turns"] == 1       # only the new one
    assert alltime["overview"]["turns"] == 2


def test_dashboard_subagent_by_type(tmp_path):
    proj = tmp_path / "projects" / "demo"
    proj.mkdir(parents=True)
    db = tmp_path / "usage.db"
    _write_jsonl(proj / "s.jsonl", [
        # parent dispatch record naming the sub-agent type
        json.dumps({"type": "user", "sessionId": "S1", "timestamp": "2026-06-20T10:00:00Z",
                    "toolUseResult": {"agentId": "ag-1", "agentType": "Explore",
                                      "status": "completed"}}),
        # the sub-agent's own usage turn, tagged with the same agent id
        _assistant("S1", "m1", out=500, extra={"agentId": "ag-1"}),
        _assistant("S1", "m2", out=100),  # a normal (non-subagent) turn
    ])
    usage_scanner.scan(projects_dir=tmp_path / "projects", db_path=db)
    d = usage_scanner.dashboard_data(db_path=db, days=None)
    subs = {s["agent_type"]: s for s in d["subagents"]}
    assert "Explore" in subs
    assert subs["Explore"]["output"] == 500
    assert subs["Explore"]["dispatches"] == 1
    assert d["overview"]["subagent_turns"] == 1


def test_dashboard_no_db(tmp_path):
    d = usage_scanner.dashboard_data(db_path=tmp_path / "missing.db", days=30)
    assert d.get("ready") is False
    assert d.get("error") == "no_data"
