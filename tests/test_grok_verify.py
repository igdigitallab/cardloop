"""tools/grok-verify (spec-095 P7): the CLI-bump gate and the soak — everything that does not need the
real `grok` binary. Argument handling, the fixture-skeleton diff, the verdict logic, the per-cycle
judgement, /proc scanning, and the two paths that must clean up after themselves (a failing step, a
SIGTERM mid-run), driven through the real script with fake `grok`/pytest/recorder executables.

The real-CLI half is run by hand (see the tool's --help) and by `tools/grok-verify check`.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = Path(os.environ.get("GROK_VERIFY_PATH") or REPO / "tools" / "grok-verify")
FIXTURES = REPO / "tests" / "fixtures" / "grok"


def _load():
    loader = importlib.machinery.SourceFileLoader("grok_verify_under_test", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


gv = _load()
SECRET = "SECRET-TOKEN-VALUE-0123456789abcdef"


# ------------------------------------------------------------------------------------------
# arguments
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("5", 5.0), ("90s", 90.0), ("20m", 1200.0), ("24h", 86400.0), ("1.5h", 5400.0), ("2d", 172800.0),
    (" 7 m ", 420.0), ("3S", 3.0), ("0.5", 0.5), (30, 30.0),
])
def test_parse_duration_accepts_units(text, seconds):
    assert gv.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "-5", "0", "0m", "5x", "1h30m", "m", "1e3", "5 5"])
def test_parse_duration_rejects_junk_and_zero(text):
    with pytest.raises(gv.UsageError):
        gv.parse_duration(text)


def test_parse_duration_error_names_the_flag():
    with pytest.raises(gv.UsageError, match="--interval"):
        gv.parse_duration("nope", what="--interval")


def test_plan_cycles():
    assert gv.plan_cycles(86400, 1200, None) == 72
    assert gv.plan_cycles(86400, 1200, 2) == 2            # --cycles wins over the duration
    assert gv.plan_cycles(100, 1200, None) == 1           # never zero cycles
    assert gv.plan_cycles(3599, 1200, None) == 2          # whole intervals only
    assert gv.plan_cycles(1, 1, 1) == 1
    with pytest.raises(gv.UsageError):
        gv.plan_cycles(100, 10, 0)
    with pytest.raises(gv.UsageError):
        gv.plan_cycles(100, 10, -3)


def test_parse_version():
    assert gv.parse_version("grok 1.0.46 (2765805b9442) [stable]") == "1.0.46"
    assert gv.parse_version("  grok 12.3.456\n") == "12.3.456"
    assert gv.parse_version("grok 1.0") is None
    assert gv.parse_version("xgrok 1.0.46") is None
    assert gv.parse_version("") is None
    assert gv.parse_version(None) is None


def test_edit_line_appends_without_duplicates():
    assert gv.edit_line(("1.0.46",), "1.0.47") == 'KNOWN_GOOD_VERSIONS = ("1.0.46", "1.0.47")'
    assert gv.edit_line((), "1.0.47") == 'KNOWN_GOOD_VERSIONS = ("1.0.47",)'    # one-tuple needs the comma
    assert gv.edit_line(("1.0.46", "1.0.47"), "1.0.47") == 'KNOWN_GOOD_VERSIONS = ("1.0.46", "1.0.47")'
    assert gv.edit_line(("1.0.45", "1.0.46"), "1.0.47") == 'KNOWN_GOOD_VERSIONS = ("1.0.45", "1.0.46", "1.0.47")'


def test_edit_line_is_a_valid_python_assignment_of_the_same_shape_as_the_engine():
    ns: dict = {}
    exec(gv.edit_line(("1.0.46",), "2.0.0"), ns)
    assert ns["KNOWN_GOOD_VERSIONS"] == ("1.0.46", "2.0.0")
    src = (REPO / "grok_engine.py").read_text().splitlines()
    assert any(ln.startswith("KNOWN_GOOD_VERSIONS = (") for ln in src), "the engine's line changed shape"


def test_known_good_line_reads_the_engine_source():
    n, text = gv.known_good_line()
    assert (REPO / "grok_engine.py").read_text().splitlines()[n - 1] == text
    assert text.startswith("KNOWN_GOOD_VERSIONS")


# ------------------------------------------------------------------------------------------
# secrets and cleanup helpers
# ------------------------------------------------------------------------------------------

def test_redact_replaces_long_secrets_longest_first_and_keeps_short_words():
    out = gv.redact("a ABCDEFGHIJKLMN b ABCDEFGHIJ c abc d", ["ABCDEFGHIJ", "ABCDEFGHIJKLMN", "abc"])
    assert out == "a REDACTED b REDACTED c abc d"
    assert gv.redact("nothing here", []) == "nothing here"
    assert gv.redact("x", [None, 5, ""]) == "x"          # junk in the list is ignored


def test_secret_values_collects_nested_long_strings_only(tmp_path):
    f = tmp_path / "auth.json"
    f.write_text(json.dumps({"k": {"key": "A" * 40, "email": "a@b.c", "list": ["B" * 16, "short", {"x": "C" * 20}],
                                   "n": 12345678901234567890, "flag": True}}))
    assert sorted(gv.secret_values(f)) == sorted(["A" * 40, "B" * 16, "C" * 20])
    assert gv.secret_values(tmp_path / "missing.json") == []
    (tmp_path / "bad.json").write_text("{not json")
    assert gv.secret_values(tmp_path / "bad.json") == []


def test_rmtree_force_removes_sandbox_placeholders(tmp_path):
    root = tmp_path / "home"
    blocked = root / "sandbox-blocked-dir.123"
    blocked.mkdir(parents=True)
    (root / "sandbox-blocked.123").write_text("")
    inner = root / "sessions" / "a"
    inner.mkdir(parents=True)
    (inner / "f").write_text("x")
    os.chmod(blocked, 0)                                   # exactly what a sandboxed Grok spawn leaves
    os.chmod(inner, 0o500)
    gv.rmtree_force(root)
    assert not os.path.lexists(root)
    gv.rmtree_force(root)                                  # gone already: no error


def test_dir_bytes_and_litter_count(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 23)
    assert gv.dir_bytes(tmp_path) == 123
    assert gv.dir_bytes(tmp_path / "missing") == 0
    for name in ("sandbox-blocked.1", "sandbox-blocked-dir.22", "sandbox-blocked", "sandbox-blocked.x",
                 "sandbox-blocked-dir", "notsandbox-blocked.5", "sessions"):
        (tmp_path / name).write_text("")
    assert gv.litter_count(tmp_path) == 2
    assert gv.litter_count(tmp_path / "missing") == 0


def test_resolve_login(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    with pytest.raises(gv.UsageError, match="no login"):
        gv.resolve_login(str(home))
    (home / "auth.json").write_text("{}")
    assert gv.resolve_login(str(home)) == home / "auth.json"            # a GROK_HOME dir
    assert gv.resolve_login(str(home / "auth.json")) == home / "auth.json"   # or the file
    other = tmp_path / "creds.json"
    other.write_text("{}")
    with pytest.raises(gv.UsageError, match="auth.json"):
        gv.resolve_login(str(other))
    monkeypatch.setenv("GROK_VERIFY_LOGIN", str(home))
    assert gv.resolve_login(None) == home / "auth.json"                  # env fallback
    other_home = tmp_path / "o"
    other_home.mkdir()
    (other_home / "auth.json").write_text("{}")
    assert gv.resolve_login(str(other_home)) == other_home / "auth.json"  # the flag beats the env


def test_resolve_binary(tmp_path, monkeypatch):
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert gv.resolve_binary(str(fake)) == str(fake)
    monkeypatch.setenv("GROK_BIN", str(fake))
    assert gv.resolve_binary(None) == str(fake)
    monkeypatch.setenv("GROK_BIN", str(tmp_path / "nope"))
    with pytest.raises(gv.UsageError, match="not found"):
        gv.resolve_binary(None)                           # an explicit GROK_BIN has no silent fallback


# ------------------------------------------------------------------------------------------
# verdict
# ------------------------------------------------------------------------------------------

def P(name="live"):
    return gv.Step(name, "pass", "ok")


def F(name="live"):
    return gv.Step(name, "fail", "bad")


def S(name="live", required=True):
    return gv.Step(name, "skip", "skipped", required=required)


def test_verdict_green_unlisted_build_offers_the_edit():
    v = gv.verdict([P("live"), P("canary"), P("fixtures")], "1.0.47", ("1.0.46",))
    assert v["ok"] is True
    assert v["edit"] == 'KNOWN_GOOD_VERSIONS = ("1.0.46", "1.0.47")'
    assert v["text"].startswith("PASS") and "1.0.47 verified" in v["text"]


def test_verdict_green_listed_build_has_nothing_to_edit():
    v = gv.verdict([P(), P("canary")], "1.0.46", ("1.0.46",))
    assert v["ok"] is True and v["edit"] is None
    assert "already" in v["text"]


def test_verdict_one_failure_blocks_everything_and_the_edit():
    v = gv.verdict([P("live"), F("canary"), P("fixtures")], "1.0.47", ("1.0.46",))
    assert v["ok"] is False and v["edit"] is None
    assert v["failed"] == ["canary"] and v["text"] == "FAIL — canary"


def test_verdict_a_skipped_required_step_is_partial_not_green():
    v = gv.verdict([P("live"), S("canary"), P("fixtures")], "1.0.47", ("1.0.46",))
    assert v["ok"] is False and v["edit"] is None
    assert v["text"].startswith("PARTIAL") and v["skipped"] == ["canary"]


def test_verdict_an_optional_step_may_be_skipped():
    v = gv.verdict([P("live"), P("canary"), S("cockpit", required=False)], "1.0.47", ("1.0.46",))
    assert v["ok"] is True and v["edit"] is not None


def test_verdict_failure_wins_over_skip_in_the_text():
    v = gv.verdict([F("live"), S("canary")], "1.0.47", ())
    assert v["text"] == "FAIL — live"


def test_verdict_without_a_version_or_without_steps_is_never_green():
    assert gv.verdict([P()], None, ())["ok"] is False
    assert gv.verdict([P()], None, ())["text"].startswith("FAIL")
    assert gv.verdict([], "1.0.47", ())["ok"] is False
    assert gv.verdict([], "1.0.47", ())["text"] == "FAIL — nothing ran"
    assert gv.verdict([S("live"), S("canary")], "1.0.47", ())["ok"] is False


def test_step_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        gv.Step("x", "maybe")
    assert gv.Step("x", "pass", "d").as_dict() == {"name": "x", "status": "pass", "detail": "d", "required": True}


# ------------------------------------------------------------------------------------------
# pytest result judgement
# ------------------------------------------------------------------------------------------

def _junit(tmp_path, cases: str, name="r.xml") -> Path:
    p = tmp_path / name
    p.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>')
    return p


def test_parse_junit_counts_and_names(tmp_path):
    p = _junit(tmp_path, '<testcase name="a"/><testcase name="b"><failure message="boom"/></testcase>'
                         '<testcase name="c"><error message="x"/></testcase>'
                         '<testcase name="d"><skipped message="no binary"/></testcase><testcase name="e"/>')
    s = gv.parse_junit(p)
    assert (s["passed"], s["failed"], s["errors"], s["skipped"]) == (2, 1, 1, 1)
    assert s["failed_names"] == ["b", "c"] and s["skipped_names"] == ["d: no binary"]
    assert gv.parse_junit(tmp_path / "missing.xml") == {}
    (tmp_path / "bad.xml").write_text("<not xml")
    assert gv.parse_junit(tmp_path / "bad.xml") == {}


def _sum(p=0, f=0, e=0, s=0):
    return {"passed": p, "failed": f, "errors": e, "skipped": s,
            "failed_names": ["t_fail"] * (f + e), "skipped_names": ["t_skip: because"] * s}


def test_judge_pytest_matrix():
    assert gv.judge_pytest(_sum(p=7), 0)[0] == "pass"
    assert gv.judge_pytest(_sum(p=7, f=1), 1)[0] == "fail"
    assert gv.judge_pytest(_sum(p=7, e=1), 1)[0] == "fail"
    st, detail = gv.judge_pytest(_sum(p=7, s=1), 0)
    assert st == "fail" and "measured nothing" in detail and "t_skip: because" in detail
    assert gv.judge_pytest(_sum(p=7, s=1), 0, allow_skips=True)[0] == "pass"
    assert gv.judge_pytest(_sum(s=3), 0)[0] == "fail"
    assert gv.judge_pytest({}, 5)[0] == "fail"                          # nothing collected
    assert "no tests ran" in gv.judge_pytest({}, 5)[1]
    assert gv.judge_pytest(_sum(), 0)[0] == "fail"                      # zero tests is not a pass
    assert gv.judge_pytest(_sum(p=3), 0, timed_out=True) == ("fail", "timed out")
    st, detail = gv.judge_pytest(_sum(p=3), 2)                           # green junit but a crashed run
    assert st == "fail" and "pytest exit 2" in detail
    assert gv.judge_pytest(_sum(p=3), None)[0] == "pass"


# ------------------------------------------------------------------------------------------
# skeletons and drift
# ------------------------------------------------------------------------------------------

def R(direction, msg):
    return {"dir": direction, "msg": msg, "t": 0.5}


def _wire(text="hi", sid="SESSION_1", extra_chunk=None, usage_key="grok-4.7-build", stop="end_turn", n=5):
    chunk = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    chunk.update(extra_chunk or {})
    return [
        R("c2a", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}),
        R("a2c", {"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": {"loadSession": True}}}),
        R("c2a", {"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                  "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}}),
        R("a2c", {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": chunk}}),
        R("a2c", {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": stop, "_meta": {
            "usage": {"inputTokens": n, "modelUsage": {usage_key: {"in": n}}}}}}),
    ]


def test_skeleton_kinds_and_paths():
    sk = gv.skeleton(_wire())
    assert set(sk) == {"c2a request initialize", "a2c result initialize", "c2a request session/prompt",
                       "a2c notification session/update:agent_message_chunk", "a2c result session/prompt"}
    chunk = sk["a2c notification session/update:agent_message_chunk"]
    assert chunk[".params.update.content.text"] == ["string"]
    assert chunk[".params.update.sessionUpdate"] == ["string"]
    assert chunk[".params.update.content"] == ["object"]
    assert sk["c2a request session/prompt"][".params.prompt"] == ["array"]
    assert sk["c2a request session/prompt"][".params.prompt[].text"] == ["string"]
    assert sk["a2c result session/prompt"][".result._meta.usage.inputTokens"] == ["number"]


def test_skeleton_ignores_ids_text_numbers_and_variable_keys():
    a = gv.skeleton(_wire(text="hello", sid="SESSION_1", usage_key="grok-4.7-build", stop="end_turn", n=5))
    b = gv.skeleton(_wire(text="a completely different sentence", sid="3f2b8c1e-aaaa-bbbb-cccc-123456789abc",
                          usage_key="grok-5-build", stop="max_tokens", n=999999))
    assert a == b
    assert gv.diff_skeletons(a, b) == {"methods_added": [], "methods_removed": [], "fields_added": [],
                                       "fields_removed": [], "types_changed": []}
    assert ".result._meta.usage.modelUsage.<id>.in" in a["a2c result session/prompt"]


def test_skeleton_normalises_uuid_and_hex_and_numeric_keys():
    msg = {"method": "x/y", "params": {"3f2b8c1e-aaaa-bbbb-cccc-123456789abc": 1, "SESSION_3": 2, "UUID_4": 3,
                                       "0123456789abcdef0123": 4, "17": 5, "real_field": 6}}
    fields = gv.skeleton([R("a2c", msg)])["a2c notification x/y"]
    assert ".params.<id>" in fields and ".params.real_field" in fields
    assert len([p for p in fields if p.startswith(".params.") and p != ".params.real_field"]) == 1


def test_skeleton_opaque_payloads_keep_the_key_but_not_the_content():
    def upd(raw):
        return R("a2c", {"method": "session/update", "params": {"update": {
            "sessionUpdate": "tool_call", "rawInput": raw, "title": "t"}}})
    a = gv.skeleton([upd({"command": "ls", "block_until_ms": 100})])["a2c notification session/update:tool_call"]
    b = gv.skeleton([upd({"command": "pwd", "extra": 1})])["a2c notification session/update:tool_call"]
    assert a == b
    assert a[".params.update.rawInput"] == ["object"]
    assert not any(p.startswith(".params.update.rawInput.") for p in a)
    # a type change of the opaque value itself is still caught
    c = gv.skeleton([upd("a string now")])["a2c notification session/update:tool_call"]
    assert c[".params.update.rawInput"] == ["string"]


@pytest.mark.parametrize("key", ["rawInput", "rawOutput", "input", "arguments", "output", "stdout", "stderr"])
def test_skeleton_every_opaque_key_hides_its_content(key):
    def upd(payload):
        return R("a2c", {"method": "session/update", "params": {"update": {"sessionUpdate": "x", key: payload}}})
    a = gv.skeleton([upd({"one": 1})])["a2c notification session/update:x"]
    b = gv.skeleton([upd({"two": "2", "three": {"deep": []}})])["a2c notification session/update:x"]
    assert a == b and a[f".params.update.{key}"] == ["object"]
    assert key in gv.OPAQUE_KEYS


def test_skeleton_unions_the_types_of_one_path_over_all_rows_of_a_kind():
    def tc(title):
        return R("a2c", {"method": "session/update", "params": {"update": {"sessionUpdate": "tool_call", "title": title}}})
    sk = gv.skeleton([tc("text"), tc(7), tc(None)])["a2c notification session/update:tool_call"]
    assert sk[".params.update.title"] == ["null", "number", "string"]


def test_skeleton_response_kind_follows_the_request_of_the_other_direction():
    rows = [R("c2a", {"id": 1, "method": "session/new", "params": {}}),
            R("a2c", {"id": 1, "method": "session/request_permission", "params": {}}),
            R("a2c", {"id": 1, "result": {"sessionId": "S"}}),                 # answers c2a id 1
            R("c2a", {"id": 1, "result": {"outcome": {}}}),                    # answers a2c id 1
            R("a2c", {"id": 9, "error": {"code": -32601, "message": "m"}})]     # nothing asked it
    assert set(gv.skeleton(rows)) == {"c2a request session/new", "a2c request session/request_permission",
                                      "a2c result session/new", "c2a result session/request_permission",
                                      "a2c error ?"}


def test_skeleton_skips_markers_junk_and_non_wire_rows():
    rows = [{"dir": "marker", "msg": {"note": "x"}}, "junk", None, {"dir": "c2a"}, {"dir": "a2c", "msg": 5},
            {"dir": "zzz", "msg": {"method": "x"}}, {"dir": "a2c", "msg": {"jsonrpc": "2.0"}},
            R("a2c", {"method": "ping", "params": {}})]
    assert set(gv.skeleton(rows)) == {"a2c notification ping"}


def test_skeleton_x_ai_session_notifications_split_by_their_update_kind():
    rows = [R("a2c", {"method": "_x.ai/session_notification",
                      "params": {"update": {"sessionUpdate": "response_completed", "usage": {"input_tokens": 1}}}}),
            R("a2c", {"method": "_x.ai/session/update",
                      "params": {"update": {"sessionUpdate": "turn_completed"}}}),
            R("a2c", {"method": "_x.ai/queue/changed", "params": {"n": 1}}),
            R("a2c", {"method": "session/update", "params": {"update": {"no": "kind"}}})]
    assert set(gv.skeleton(rows)) == {"a2c notification _x.ai/session_notification:response_completed",
                                      "a2c notification _x.ai/session/update:turn_completed",
                                      "a2c notification _x.ai/queue/changed",
                                      "a2c notification session/update"}


def test_diff_reports_added_and_removed_methods_and_fields_and_types():
    old = gv.skeleton(_wire())
    new = gv.skeleton(_wire(extra_chunk={"newField": 1}))
    new["a2c notification brand/new"] = {"": ["object"]}
    del new["c2a request initialize"]
    new["a2c result session/prompt"].pop(".result._meta.usage.inputTokens")
    new["a2c result session/prompt"][".result.stopReason"] = ["number"]       # was string
    d = gv.diff_skeletons(old, new)
    assert d["methods_added"] == ["a2c notification brand/new"]
    assert d["methods_removed"] == ["c2a request initialize"]
    assert d["fields_added"] == ["a2c notification session/update:agent_message_chunk .params.update.newField"]
    assert d["fields_removed"] == ["a2c result session/prompt .result._meta.usage.inputTokens"]
    assert d["types_changed"] == ["a2c result session/prompt .result.stopReason: string -> number"]


def test_diff_null_alone_is_not_a_type_change_but_null_only_to_value_is_not_either():
    old = {"k": {".a": ["null", "string"], ".b": ["null"], ".c": ["string"], ".d": ["object"]}}
    new = {"k": {".a": ["string"], ".b": ["string"], ".c": ["null", "string"], ".d": ["array"]}}
    d = gv.diff_skeletons(old, new)
    assert d["types_changed"] == ["k .d: object -> array"]


def test_diff_root_path_is_named_dot():
    d = gv.diff_skeletons({"k": {"": ["object"], ".a": ["string"]}}, {"k": {".a": ["string"]}})
    assert d["fields_removed"] == ["k ."]
    d = gv.diff_skeletons({"k": {".a": ["string"]}}, {"k": {"": ["object"], ".a": ["string"]}})
    assert d["fields_added"] == ["k ."]


def test_merge_skeletons_unions_types_and_kinds():
    m = gv.merge_skeletons([{"k": {".a": ["string"]}}, {"k": {".a": ["number"], ".b": ["bool"]}}, {"z": {".x": ["null"]}}])
    assert m == {"k": {".a": ["number", "string"], ".b": ["bool"]}, "z": {".x": ["null"]}}


NOTHING = {"methods_added": [], "methods_removed": [], "fields_added": [], "fields_removed": [], "types_changed": []}


def _d(**kw):
    return {**NOTHING, **kw}


def test_judge_drift_modes():
    assert gv.judge_drift(NOTHING, "removed")[0] == "pass"
    assert gv.judge_drift(_d(fields_added=["a"]), "removed")[0] == "pass"            # additions only listed
    assert gv.judge_drift(_d(fields_added=["a"]), "any")[0] == "fail"
    assert gv.judge_drift(_d(methods_added=["m"]), "any")[0] == "fail"
    for key in ("methods_removed", "fields_removed", "types_changed"):
        assert gv.judge_drift(_d(**{key: ["x"]}), "removed")[0] == "fail", key
        assert gv.judge_drift(_d(**{key: ["x"]}), "any")[0] == "fail", key
        assert gv.judge_drift(_d(**{key: ["x"]}), "report")[0] == "pass", key
    st, detail = gv.judge_drift(_d(methods_removed=["a", "b"], fields_added=["c"]), "removed")
    assert st == "fail" and "2 removed/changed, 1 added" in detail and "methods -2/+0" in detail
    with pytest.raises(gv.UsageError):
        gv.judge_drift(NOTHING, "strict")


def test_render_drift_orders_breaking_first_and_truncates():
    lines = gv.render_drift(_d(methods_added=["ma"], fields_removed=["fr1", "fr2", "fr3"], types_changed=["tc"],
                               methods_removed=["mr"], fields_added=["fa"]), limit=2)
    assert lines[:3] == ["    REMOVED method: mr", "    REMOVED field: fr1", "    REMOVED field: fr2"]
    assert lines[3].strip() == "... 1 more REMOVED field entries"
    assert lines[4:] == ["    CHANGED type: tc", "    added method: ma", "    added field: fa"]
    assert gv.render_drift(NOTHING) == []


def test_skeleton_of_dir_compares_the_named_fixtures_and_skips_synthetic_ones(tmp_path):
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(r) for r in _wire()) + "\n")
    (tmp_path / "b.jsonl").write_text(json.dumps(R("a2c", {"method": "only/b", "params": {}})) + "\nnot json\n")
    (tmp_path / "synthetic_c.jsonl").write_text(json.dumps(R("a2c", {"method": "only/c", "params": {}})) + "\n")
    both = gv.skeleton_of_dir(tmp_path)
    assert "a2c notification only/b" in both and "a2c notification only/c" not in both
    only_a = gv.skeleton_of_dir(tmp_path, ["a"])
    assert "a2c notification only/b" not in only_a and "c2a request initialize" in only_a
    assert gv.skeleton_of_dir(tmp_path, ["synthetic_c"]) != {}      # explicit names are honoured
    assert gv.skeleton_of_dir(tmp_path / "missing") == {}


def test_a_recording_identical_to_the_committed_fixtures_has_no_drift():
    names = sorted(p.stem for p in FIXTURES.glob("*.jsonl") if not p.stem.startswith("synthetic_"))
    assert len(names) >= 20
    sk = gv.skeleton_of_dir(FIXTURES, names)
    assert gv.diff_skeletons(sk, sk) == NOTHING
    assert "a2c result session/prompt" in sk and "c2a request session/new" in sk
    # the engine's own inputs are in the skeleton, so losing one would be seen
    assert ".result.stopReason" in sk["a2c result session/prompt"]


# ------------------------------------------------------------------------------------------
# soak: cycle judgement, growth allowance, summary
# ------------------------------------------------------------------------------------------

LIMITS = {"litter_max": 4, "turn_timeout": 180}


def good(**kw):
    m = {"result": True, "error": None, "timed_out": False, "leftover": [], "litter": 0,
         "growth_bytes": 0, "growth_allowed": 1000, "canary": None}
    m.update(kw)
    return m


def test_evaluate_cycle_healthy():
    assert gv.evaluate_cycle(good(), LIMITS) == []


def test_evaluate_cycle_each_failure_is_named():
    assert "timed out after 180s" in gv.evaluate_cycle(good(timed_out=True, result=False), LIMITS)[0]
    assert gv.evaluate_cycle(good(error="boom", result=False), LIMITS) == ["turn error: boom"]
    assert gv.evaluate_cycle(good(result=False), LIMITS) == ["the turn ended without a result event"]
    p = gv.evaluate_cycle(good(leftover=[{"pid": 1, "age": 12.0}, {"pid": 2, "age": 99.0}]), LIMITS)
    assert len(p) == 1 and "2 grok agent process(es)" in p[0] and "oldest 99s" in p[0]
    assert "sandbox-blocked* placeholders" in gv.evaluate_cycle(good(litter=5), LIMITS)[0]
    assert gv.evaluate_cycle(good(litter=4), LIMITS) == []                       # at the limit is fine
    assert "GROK_HOME grew" in gv.evaluate_cycle(good(growth_bytes=2000, growth_allowed=1000), LIMITS)[0]
    assert gv.evaluate_cycle(good(growth_bytes=1000, growth_allowed=1000), LIMITS) == []
    assert gv.evaluate_cycle(good(growth_bytes=10 ** 9, growth_allowed=None), LIMITS) == []   # baseline cycle
    assert gv.evaluate_cycle(good(growth_bytes=None, growth_allowed=5), LIMITS) == []
    c = gv.evaluate_cycle(good(canary={"ok": False, "detail": "sent 2 MiB"}), LIMITS)
    assert c == ["egress canary failed: sent 2 MiB"]
    assert gv.evaluate_cycle(good(canary={"ok": True, "detail": "fine"}), LIMITS) == []


def test_evaluate_cycle_an_error_is_not_double_reported_as_missing_result():
    p = gv.evaluate_cycle(good(error="boom", result=False, leftover=[{"pid": 1, "age": None}]), LIMITS)
    assert len(p) == 2 and p[0] == "turn error: boom" and "oldest 0s" in p[1]


def test_growth_allowance_is_linear_in_the_cycles_since_the_baseline():
    assert gv.growth_allowance(0, 100, 10) == 100
    assert gv.growth_allowance(5, 100, 10) == 150
    assert gv.growth_allowance(-3, 100, 10) == 100


def _cyc(n, ok=True, leftover=0, litter=0, home=1000, canary=None):
    return {"kind": "cycle", "cycle": n, "ok": ok, "leftover": [{}] * leftover, "litter": litter,
            "home_bytes": home, "canary": canary}


def test_summarize_pass_fail_interrupted():
    meta = {"cycles_planned": 2, "interrupted": False}
    s = gv.summarize([{"kind": "preflight", "ok": True}, _cyc(1, home=100), _cyc(2, litter=2, home=300)], meta)
    assert s["verdict"] == "PASS" and s["cycles_run"] == 2 and s["cycles_ok"] == 2
    assert (s["home_bytes_first"], s["home_bytes_last"], s["max_litter"], s["max_leftover_agents"]) == (100, 300, 2, 0)
    bad = gv.summarize([_cyc(1), _cyc(2, ok=False, leftover=2)], meta)
    assert bad["verdict"] == "FAIL" and bad["cycles_failed"] == [2] and bad["max_leftover_agents"] == 2
    short = gv.summarize([_cyc(1)], meta)                                  # planned 2, ran 1, not interrupted
    assert short["verdict"] == "FAIL"
    assert gv.summarize([_cyc(1)], {**meta, "interrupted": True})["verdict"] == "INTERRUPTED"
    assert gv.summarize([], meta)["verdict"] == "FAIL"
    pre = gv.summarize([{"kind": "preflight", "ok": False}], meta)
    assert pre["verdict"] == "FAIL" and pre["preflight_ok"] is False
    c = gv.summarize([_cyc(1, canary={"ok": True, "max_sent": 500}), _cyc(2, canary={"ok": False, "max_sent": 9})], meta)
    assert (c["canary_runs"], c["canary_failed"], c["canary_max_sent"], c["verdict"]) == (2, 1, 500, "FAIL")
    assert gv.summarize([_cyc(1, canary={"ok": True, "max_sent": 7})], {**meta, "cycles_planned": 1})["canary_max_sent"] == 7


# ------------------------------------------------------------------------------------------
# /proc scan
# ------------------------------------------------------------------------------------------

def _proc(root: Path, pid: int, *, cmd=("grok", "agent", "--no-leader", "stdio"), home="/h", state="S",
          pgid=None, start=5000, environ=True):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
    if environ:
        env = [f"PATH=/bin"] + ([f"GROK_HOME={home}"] if home is not None else [])
        (d / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
    fields = [state, "1", str(pgid or pid), str(pid), "0", "-1", "0", "0", "0", "0", "0", "0", "0", "0", "0",
              "20", "0", "1", "0", str(start)]
    (d / "stat").write_text(f"{pid} (gro k) " + " ".join(fields) + " 0 0\n")


def test_find_agents_scopes_to_the_home_and_reports_age_and_group(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000.00 500.00\n")
    (root / "self").mkdir()
    (root / "notapid").mkdir()
    _proc(root, 100, home=str(tmp_path / "mine"), pgid=77, start=20000)       # ours
    _proc(root, 101, home=str(tmp_path / "other"))                            # another cockpit's turn
    _proc(root, 102, home=None)                                               # no GROK_HOME at all
    _proc(root, 103, home=str(tmp_path / "mine"), cmd=("grok", "agent", "stdio"))      # no --no-leader
    _proc(root, 104, home=str(tmp_path / "mine"), cmd=("grok", "-p", "x"))             # not an agent
    _proc(root, 105, home=str(tmp_path / "mine"), state="Z")                           # a zombie is not live
    _proc(root, 106, home=str(tmp_path / "mine"), cmd=("grok", "--no-leader", "stdio"))   # not the `agent` subcommand
    _proc(root, 107, home=str(tmp_path / "mine"), cmd=("grok", "agent", "--no-leader"))   # no stdio transport
    (tmp_path / "mine").mkdir()
    found = gv.find_agents(tmp_path / "mine", root)
    assert [a["pid"] for a in found] == [100]
    assert found[0]["pgid"] == 77
    assert round(found[0]["age"]) == 800                                       # 1000 - 20000/100 ticks
    link = tmp_path / "alias"
    link.symlink_to(tmp_path / "mine")
    assert [a["pid"] for a in gv.find_agents(link, root)] == [100]             # symlinked spelling of the home


def test_find_agents_counts_a_process_whose_environ_cannot_be_read(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    _proc(root, 200, home="/anything", environ=False)
    found = gv.find_agents("/some/home", root)
    assert [a["pid"] for a in found] == [200] and found[0]["age"] is None      # no uptime file: age unknown


def test_find_agents_missing_proc_root_and_bad_stat(tmp_path):
    assert gv.find_agents("/h", tmp_path / "nope") == []
    root = tmp_path / "proc"
    root.mkdir()
    _proc(root, 300, home="/h")
    (root / "300" / "stat").write_text("garbage")
    assert gv.find_agents("/h", root) == []


def test_find_agents_and_kill_agents_on_a_real_process(tmp_path):
    home = tmp_path / "gh"
    home.mkdir()
    env = {**os.environ, "GROK_HOME": str(home)}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "agent", "--no-leader", "stdio"],
                            env=env, start_new_session=True)
    try:
        for _ in range(50):
            if gv.find_agents(home):
                break
            time.sleep(0.1)
        found = gv.find_agents(home)
        assert [a["pid"] for a in found] == [proc.pid] and found[0]["pgid"] == proc.pid
        assert gv.find_agents(tmp_path / "elsewhere") == []
        assert gv.kill_agents(home) == 1
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL
        assert gv.find_agents(home) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ------------------------------------------------------------------------------------------
# Scratch, run_cmd
# ------------------------------------------------------------------------------------------

def test_scratch_holds_a_private_login_copy_and_close_verifies_removal(tmp_path):
    login = tmp_path / "src" / "auth.json"
    login.parent.mkdir()
    login.write_text(json.dumps({"k": {"key": SECRET}}))
    sc = gv.Scratch(login, str(tmp_path))
    try:
        copy = sc.home / "auth.json"
        assert copy.read_text() == login.read_text()
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert stat.S_IMODE(sc.root.stat().st_mode) == 0o700 and stat.S_IMODE(sc.home.stat().st_mode) == 0o700
        os.mkdir(sc.home / "sandbox-blocked-dir.9")
        os.chmod(sc.home / "sandbox-blocked-dir.9", 0)
    finally:
        assert sc.close() is True
    assert not os.path.lexists(sc.root)
    assert login.exists()                                                      # the source is never touched
    assert sc.close() is True                                                  # idempotent


def test_scratch_close_kills_a_process_still_using_its_home(tmp_path):
    login = tmp_path / "auth.json"
    login.write_text("{}")
    sc = gv.Scratch(login, str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "agent", "--no-leader", "stdio"],
                            env={**os.environ, "GROK_HOME": str(sc.home)}, start_new_session=True)
    try:
        for _ in range(50):
            if gv.find_agents(sc.home):
                break
            time.sleep(0.1)
        assert gv.find_agents(sc.home)
        assert sc.close() is True
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_run_cmd_registers_its_child_for_the_signal_handler_while_it_runs(tmp_path):
    import threading
    seen = []
    t = threading.Thread(target=lambda: gv.run_cmd([sys.executable, "-c", "import time; time.sleep(2)"],
                                                    env=dict(os.environ), cwd=tmp_path, timeout=30))
    t.start()
    for _ in range(40):
        if gv._ACTIVE:
            seen = list(gv._ACTIVE)
            break
        time.sleep(0.05)
    t.join(timeout=30)
    assert len(seen) == 1 and seen[0].args[0] == sys.executable
    assert not gv._ACTIVE                                                      # and forgotten afterwards


def test_scratch_removes_itself_when_the_copy_fails(tmp_path):
    before = set(os.listdir(tmp_path))
    with pytest.raises(OSError):
        gv.Scratch(tmp_path / "does-not-exist" / "auth.json", str(tmp_path))
    assert set(os.listdir(tmp_path)) == before


def test_run_cmd_redacts_and_times_out(tmp_path):
    res = gv.run_cmd([sys.executable, "-c", f"print('x {SECRET} y'); import sys; sys.exit(3)"],
                     env=dict(os.environ), cwd=tmp_path, timeout=30, secrets=[SECRET])
    assert res.rc == 3 and not res.timed_out and SECRET not in res.out and "REDACTED" in res.out
    t0 = time.monotonic()
    res = gv.run_cmd([sys.executable, "-c", "import time; time.sleep(60)"], env=dict(os.environ), cwd=tmp_path,
                     timeout=1, secrets=[])
    assert res.timed_out and time.monotonic() - t0 < 30


def test_run_cmd_kills_the_whole_group_of_a_finished_leader(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess,sys,os;"
            f"p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
            f"stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"open({str(pidfile)!r},'w').write(str(p.pid))")
    t0 = time.monotonic()
    res = gv.run_cmd([sys.executable, "-c", code], env=dict(os.environ), cwd=tmp_path, timeout=30)
    assert res.rc == 0 and not res.timed_out and time.monotonic() - t0 < 20
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
        # a zombie child of a dead parent is reaped by init; a live sleeper would still answer kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_child_env_drops_secrets_and_every_grok_variable(monkeypatch):
    for k in ("XAI_API_KEY", "ANTHROPIC_API_KEY", "WEB_PASSWORD", "WEB_COOKIE_SALT", "VAPID_PRIVATE_KEY",
              "GROK_ALLOW_ALL_PROJECTS", "GROK_SANDBOX_DENY"):
        monkeypatch.setenv(k, "v")
    monkeypatch.setenv("HARMLESS_VAR", "keep")
    env = gv._child_env({"GROK_HOME": "/h"})
    assert env["GROK_HOME"] == "/h" and env["HARMLESS_VAR"] == "keep" and "PATH" in env
    for k in ("XAI_API_KEY", "ANTHROPIC_API_KEY", "WEB_PASSWORD", "WEB_COOKIE_SALT", "VAPID_PRIVATE_KEY",
              "GROK_ALLOW_ALL_PROJECTS", "GROK_SANDBOX_DENY"):
        assert k not in env, k


def test_pytest_and_recorder_commands_are_parameterised(monkeypatch):
    monkeypatch.delenv("GROK_VERIFY_PYTEST", raising=False)
    monkeypatch.delenv("GROK_VERIFY_RECORDER", raising=False)
    assert gv.pytest_cmd() == [sys.executable, "-m", "pytest"]
    assert gv.recorder_cmd() == [sys.executable, str(gv.REPO / "tools" / "grok_record_fixtures.py")]
    monkeypatch.setenv("GROK_VERIFY_PYTEST", "/x/fake pytest --flag 'a b'")
    monkeypatch.setenv("GROK_VERIFY_RECORDER", "/y/rec")
    assert gv.pytest_cmd() == ["/x/fake", "pytest", "--flag", "a b"]
    assert gv.recorder_cmd() == ["/y/rec"]


def test_default_log_path(monkeypatch, tmp_path):
    monkeypatch.delenv("GROK_VERIFY_LOG", raising=False)
    p = gv.default_log_path(None)
    assert p.name.startswith("grok-soak-") and p.suffix == ".jsonl"
    assert gv.default_log_path(str(tmp_path / "x.jsonl")) == tmp_path / "x.jsonl"
    monkeypatch.setenv("GROK_VERIFY_LOG", str(tmp_path / "env.jsonl"))
    assert gv.default_log_path(None) == tmp_path / "env.jsonl"
    assert gv.default_log_path(str(tmp_path / "flag.jsonl")) == tmp_path / "flag.jsonl"   # the flag wins


def test_append_log_writes_one_json_line_each(tmp_path):
    p = tmp_path / "sub" / "log.jsonl"
    gv.append_log(p, {"b": 1, "a": "é"})
    gv.append_log(p, {"c": 2})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"a": "é", "b": 1}, {"c": 2}]


# ------------------------------------------------------------------------------------------
# the real script, with fake grok / pytest / recorder executables
# ------------------------------------------------------------------------------------------

def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


FAKE_GROK = '''
import sys
if "--version" in sys.argv:
    print("grok 9.9.9 (fake) [stable]")
    sys.exit(0)
sys.exit(3)
'''

FAKE_PYTEST = '''
import json, os, sys, time
args = sys.argv[1:]
junit = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
marker = args[args.index("-m") + 1] if "-m" in args else ""
home = os.environ.get("GROK_HOME", "")
auth = os.path.join(home, "auth.json")
rec = {"marker": marker, "home_login": os.path.isfile(auth),
       "mode": oct(os.stat(auth).st_mode & 0o777) if os.path.isfile(auth) else None,
       "grok_bin": os.environ.get("GROK_BIN"), "xai": os.environ.get("XAI_API_KEY"),
       "allow_all": os.environ.get("GROK_ALLOW_ALL_PROJECTS"), "web_pw": os.environ.get("WEB_PASSWORD"),
       "has_addopts_off": "addopts=" in args, "basetemp": [a for a in args if a.startswith("--basetemp=")]}
open(os.environ["FAKE_LOG"], "a").write(json.dumps(rec) + "\\n")
print("SECRET " + os.environ["FAKE_SECRET"])
if os.environ.get("FAKE_HANG") == marker:
    import signal
    def on_int(*_a):
        open(os.environ["FAKE_GRACEFUL"], "w").write("unwound")      # what pytest's finalizers would do
        sys.exit(0)
    signal.signal(signal.SIGINT, on_int)
    open(os.environ["FAKE_PIDFILE"], "w").write(str(os.getpid()))
    time.sleep(120)
if os.environ.get("FAKE_CRASH") == marker:
    sys.exit(2)                       # no junit at all
case = "<testcase name='t'/>"
if os.environ.get("FAKE_FAIL") == marker:
    case = "<testcase name='t_bad'><failure message='boom'/></testcase>"
if os.environ.get("FAKE_SKIP") == marker:
    case = "<testcase name='t_skip'><skipped message='no login'/></testcase>"
open(junit, "w").write("<testsuites><testsuite>" + case + "</testsuite></testsuites>")
sys.exit(1 if os.environ.get("FAKE_FAIL") == marker else 0)
'''

FAKE_RECORDER = '''
import json, os, shutil, sys
args = sys.argv[1:]
out = args[args.index("--out-dir") + 1]
if os.environ.get("FAKE_REC_MODE") == "crash":
    sys.exit(2)
os.makedirs(out)
src = os.environ["FAKE_REC_SRC"]
scen = [args[i + 1] for i, a in enumerate(args) if a == "--scenario"]
names = sorted(f[:-6] for f in os.listdir(src) if f.endswith(".jsonl") and not f.startswith("synthetic_"))
if scen and scen != ["all"]:
    names = [n for n in names if n in scen]
if os.environ.get("FAKE_REC_MODE") == "extra":
    shutil.copy(os.path.join(src, "simple_text.jsonl"), os.path.join(out, "zz_brand_new_scenario.jsonl"))
for n in names:
    rows = [json.loads(l) for l in open(os.path.join(src, n + ".jsonl"))]
    for r in rows:
        m = r.get("msg")
        if os.environ.get("FAKE_REC_MODE") == "drop" and isinstance(m, dict) and "result" in m \\
                and isinstance(m["result"], dict):
            m["result"].pop("stopReason", None)
        if os.environ.get("FAKE_REC_MODE") == "add" and isinstance(m, dict):
            upd = (m.get("params") or {}).get("update") if isinstance(m.get("params"), dict) else None
            if isinstance(upd, dict) and upd.get("sessionUpdate") == "agent_message_chunk":
                upd["brandNewField"] = 1
    open(os.path.join(out, n + ".jsonl"), "w").write("".join(json.dumps(r) + "\\n" for r in rows))
'''


@pytest.fixture
def rig(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    login = tmp_path / "login"
    login.mkdir()
    (login / "auth.json").write_text(json.dumps({"https://auth.x.ai::c": {
        "auth_mode": "oidc", "coding_data_retention_opt_out": True, "email": "a@b.example",
        "key": SECRET, "refresh_token": SECRET + "-refresh"}}))
    parent = tmp_path / "scratch-parent"
    parent.mkdir()
    _script(bindir / "bwrap", "")           # the engine wants a `bwrap` on PATH; these fake agents are not sandboxed
    r = {
        "tmp": tmp_path, "login": login, "parent": parent,
        "grok": _script(bindir / "grok", FAKE_GROK),
        "pytest": _script(bindir / "fakepytest", FAKE_PYTEST),
        "recorder": _script(bindir / "fakerec", FAKE_RECORDER),
        "log": tmp_path / "fake.log", "pid": tmp_path / "fake.pid", "graceful": tmp_path / "fake.graceful",
    }
    engine_before = (REPO / "grok_engine.py").read_bytes()
    yield r
    assert (REPO / "grok_engine.py").read_bytes() == engine_before, "grok-verify must never edit a file"


def _env(rig, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GROK_") and k not in ("FAKE_HANG", "FAKE_FAIL")}
    env.update({
        "GROK_VERIFY_NO_REEXEC": "1", "GROK_VERIFY_SCRATCH": str(rig["parent"]),
        "GROK_VERIFY_PYTEST": str(rig["pytest"]), "GROK_VERIFY_RECORDER": str(rig["recorder"]),
        "FAKE_LOG": str(rig["log"]), "FAKE_PIDFILE": str(rig["pid"]), "FAKE_SECRET": SECRET,
        "FAKE_GRACEFUL": str(rig["graceful"]),
        "FAKE_REC_SRC": str(FIXTURES),
        "XAI_API_KEY": "xai-must-not-leak", "WEB_PASSWORD": "pw-must-not-leak", "GROK_ALLOW_ALL_PROJECTS": "true",
    })
    env.update(extra)
    return env


def run_tool(rig, args, timeout=120, **extra_env):
    return subprocess.run([sys.executable, str(TOOL), *args], env=_env(rig, **extra_env), capture_output=True,
                          text=True, timeout=timeout)


def check_args(rig, *more):
    return ["check", "--login", str(rig["login"]), "--grok-bin", str(rig["grok"]), *more]


def _fake_runs(rig):
    return [json.loads(x) for x in rig["log"].read_text().splitlines()] if rig["log"].exists() else []


def test_cli_dry_run_touches_nothing_and_prints_the_plan(rig):
    r = run_tool(rig, check_args(rig, "--dry-run", "--with-cockpit"))
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "9.9.9" in r.stdout and "NO" in r.stdout          # fake build is unlisted
    assert "grok_live" in r.stdout and "grok_canary" in r.stdout and "grok_live_cockpit" in r.stdout
    assert os.listdir(rig["parent"]) == [] and not rig["log"].exists()
    assert SECRET not in r.stdout + r.stderr
    s = run_tool(rig, ["soak", "--login", str(rig["login"]), "--grok-bin", str(rig["grok"]), "--dry-run",
                       "--cycles", "3", "--interval", "5", "--canary-every", "3"])
    assert s.returncode == 0 and "cycles        : 3, one every 5s" in s.stdout
    assert "model turns   : about 5" in s.stdout                                         # 3 + probe + 1 canary
    assert os.listdir(rig["parent"]) == [] and SECRET not in s.stdout


def test_cli_dry_run_fails_when_the_login_or_binary_is_missing(rig):
    r = run_tool(rig, ["check", "--login", str(rig["tmp"] / "nowhere"), "--grok-bin", str(rig["grok"]), "--dry-run"])
    assert r.returncode == 1 and "no login" in r.stderr
    r = run_tool(rig, ["check", "--login", str(rig["login"]), "--grok-bin", str(rig["tmp"] / "no-grok"), "--dry-run"])
    assert r.returncode == 1 and "not found" in r.stderr


def test_cli_bad_arguments_exit_1_with_a_message(rig):
    for args, needle in ((["soak", "--cycles", "0"], "--cycles"), (["soak", "--interval", "soon"], "--interval"),
                         (["soak", "--duration", "-4"], "--duration"), (["soak", "--canary-every", "-1"], "--canary-every")):
        r = run_tool(rig, args + ["--login", str(rig["login"]), "--grok-bin", str(rig["grok"]), "--dry-run"])
        assert r.returncode == 1 and needle in r.stderr, (args, r.stderr)
    r = run_tool(rig, ["check", "--drift", "bogus"])
    assert r.returncode == 2                                                              # argparse usage error


def test_cli_green_run_prints_the_edit_never_makes_it_and_cleans_up(rig):
    r = run_tool(rig, check_args(rig, "--with-cockpit"))
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "VERDICT: PASS — 9.9.9 verified" in out
    import grok_engine
    expected = gv.edit_line(tuple(grok_engine.KNOWN_GOOD_VERSIONS), "9.9.9")
    assert f"    + {expected}" in out and "    - KNOWN_GOOD_VERSIONS" in out
    assert "removed: yes (verified)" in out
    assert os.listdir(rig["parent"]) == []                                                # scratch + login copy gone
    assert SECRET not in out + r.stderr
    runs = _fake_runs(rig)
    assert [x["marker"] for x in runs] == ["grok_live", "grok_canary", "grok_live_cockpit"]
    for x in runs:
        assert x["home_login"] and x["mode"] == "0o600"                                   # the login copy was there
        assert x["grok_bin"] == str(rig["grok"]) and x["has_addopts_off"] and x["basetemp"]
        assert x["xai"] is None and x["allow_all"] is None and x["web_pw"] is None        # nothing leaks to a child
    assert len({x["basetemp"][0] for x in runs}) == 1                                     # one scratch basetemp


def test_cli_the_cockpit_step_is_optional(rig):
    r = run_tool(rig, check_args(rig))
    assert r.returncode == 0 and "VERDICT: PASS" in r.stdout and "[skip] cockpit" in r.stdout
    assert [x["marker"] for x in _fake_runs(rig)] == ["grok_live", "grok_canary"]


def test_cli_a_listed_build_passes_without_an_edit(rig):
    import grok_engine
    listed = _script(rig["tmp"] / "bin" / "grok-listed", FAKE_GROK.replace("9.9.9", grok_engine.KNOWN_GOOD_VERSIONS[0]))
    r = run_tool(rig, ["check", "--login", str(rig["login"]), "--grok-bin", str(listed)])
    assert r.returncode == 0 and "was already on KNOWN_GOOD_VERSIONS" in r.stdout
    assert "To record this build" not in r.stdout and "listed" in r.stdout


def test_cli_a_scenario_without_a_committed_fixture_is_noted_not_compared(rig):
    r = run_tool(rig, check_args(rig, "--skip-live", "--skip-canary"), FAKE_REC_MODE="extra")
    assert "no committed fixture for zz_brand_new_scenario (not compared)" in r.stdout
    assert "FAIL — fixtures" not in r.stdout


def test_cli_a_failing_step_fails_the_gate_hides_the_edit_redacts_and_cleans_up(rig):
    r = run_tool(rig, check_args(rig), FAKE_FAIL="grok_canary")
    out = r.stdout
    assert r.returncode == 1
    assert "VERDICT: FAIL — canary" in out and "t_bad" in out
    assert "To record this build" not in out and "KNOWN_GOOD_VERSIONS =" not in out
    assert SECRET not in out + r.stderr and "REDACTED" in out                              # the fake printed it, the tool hid it
    assert os.listdir(rig["parent"]) == []
    assert [x["marker"] for x in _fake_runs(rig)] == ["grok_live", "grok_canary"]          # the other steps still ran


def test_cli_a_crashed_runner_and_a_skipped_test_both_fail(rig):
    r = run_tool(rig, check_args(rig), FAKE_CRASH="grok_live")
    assert r.returncode == 1 and "no tests ran" in r.stdout and os.listdir(rig["parent"]) == []
    rig["log"].unlink()
    r = run_tool(rig, check_args(rig), FAKE_SKIP="grok_live")
    assert r.returncode == 1 and "measured nothing" in r.stdout
    r = run_tool(rig, check_args(rig, "--allow-skips"), FAKE_SKIP="grok_live")
    assert r.returncode == 0 and "VERDICT: PASS" in r.stdout


def test_cli_a_skip_flag_makes_the_run_partial_and_exit_1(rig):
    r = run_tool(rig, check_args(rig, "--skip-canary"))
    assert r.returncode == 1 and "PARTIAL" in r.stdout and "canary" in r.stdout.split("VERDICT:")[1]
    assert "To record this build" not in r.stdout
    assert [x["marker"] for x in _fake_runs(rig)] == ["grok_live"]
    r = run_tool(rig, check_args(rig, "--skip-live", "--skip-canary", "--skip-fixtures"))
    assert r.returncode == 1 and "PARTIAL" in r.stdout


def test_cli_fixture_drift_removed_fails_added_is_listed_and_report_mode_never_fails(rig):
    base = ["--skip-live", "--skip-canary"]
    r = run_tool(rig, check_args(rig, *base), FAKE_REC_MODE="drop")
    assert r.returncode == 1 and "REMOVED field" in r.stdout and "stopReason" in r.stdout
    assert "VERDICT: FAIL — fixtures" in r.stdout
    r = run_tool(rig, check_args(rig, *base, "--drift", "report"), FAKE_REC_MODE="drop")
    assert "[report only]" in r.stdout and "REMOVED field" in r.stdout and "FAIL — fixtures" not in r.stdout
    r = run_tool(rig, check_args(rig, *base), FAKE_REC_MODE="add")
    assert "added field" in r.stdout and "brandNewField" in r.stdout and "FAIL — fixtures" not in r.stdout
    r = run_tool(rig, check_args(rig, *base, "--drift", "any"), FAKE_REC_MODE="add")
    assert "FAIL — fixtures" in r.stdout
    r = run_tool(rig, check_args(rig, *base), FAKE_REC_MODE="crash")
    assert "FAIL — fixtures" in r.stdout and "recorder exited 2" in r.stdout
    assert os.listdir(rig["parent"]) == []


def test_cli_a_faithful_recording_has_no_drift_and_scenarios_are_passed_through(rig):
    r = run_tool(rig, check_args(rig, "--skip-live", "--skip-canary", "--fixture-scenario", "simple_text",
                                 "--fixture-scenario", "tool_read"))
    assert "fixtures: 2 fixture(s) compared; 0 removed/changed, 0 added" in r.stdout


def test_cli_sigterm_during_a_step_kills_the_runner_and_removes_the_scratch_tree(rig):
    p = subprocess.Popen([sys.executable, str(TOOL), *check_args(rig)], env=_env(rig, FAKE_HANG="grok_canary"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(300):
            if rig["pid"].exists() and rig["pid"].read_text():
                break
            time.sleep(0.1)
        assert rig["pid"].exists(), "the fake runner never reached the hang"
        pid = int(rig["pid"].read_text())
        assert os.listdir(rig["parent"]), "the scratch tree should exist while a step runs"
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 128 + signal.SIGTERM, (out, err)
    assert "stopped by signal 15" in err
    assert os.listdir(rig["parent"]) == []
    assert rig["graceful"].read_text() == "unwound"       # SIGINT first: the runner got to run its finalizers
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                                                     # the runner is dead too
    assert SECRET not in out + err


def test_cli_soak_against_a_failing_binary_fails_cleanly(rig):
    log = rig["tmp"] / "soak.jsonl"
    r = run_tool(rig, ["soak", "--login", str(rig["login"]), "--grok-bin", str(rig["grok"]), "--cycles", "2",
                       "--interval", "0.2", "--no-preflight", "--canary-every", "0", "--log", str(log),
                       "--turn-timeout", "60"],
                 PATH=str(rig["tmp"] / "bin") + os.pathsep + os.environ["PATH"], GROK_BIN=str(rig["grok"]))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SUMMARY: FAIL" in r.stdout and "removed: yes (verified)" in r.stdout
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["cycle"] for x in lines] == [1, 2] and all(not x["ok"] for x in lines)
    assert all(x["problems"] for x in lines) and SECRET not in log.read_text()
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["verdict"] == "FAIL" and summary["cycles_run"] == 2 and summary["scratch_removed"] is True
    assert summary["version"] == "9.9.9" and summary["cycles_planned"] == 2
    assert os.listdir(rig["parent"]) == [] and SECRET not in r.stdout + r.stderr


FAKE_HANGING_AGENT = '''
import os, sys, time
if "--version" in sys.argv:
    print("grok 9.9.9 (fake) [stable]")
    sys.exit(0)
open({pidfile!r}, "w").write(str(os.getpid()))     # the engine hands a child no FAKE_* variable
time.sleep(300)
'''


def test_cli_soak_sigterm_mid_turn_stops_the_agent_writes_a_summary_and_cleans_up(rig):
    hang = _script(rig["tmp"] / "bin" / "grok-hang", FAKE_HANGING_AGENT.format(pidfile=str(rig["pid"])))
    log = rig["tmp"] / "soak.jsonl"
    bindir = str(rig["tmp"] / "bin")
    p = subprocess.Popen([sys.executable, str(TOOL), "soak", "--login", str(rig["login"]), "--grok-bin", str(hang),
                          "--cycles", "5", "--interval", "30", "--no-preflight", "--canary-every", "0",
                          "--log", str(log)],
                         env=_env(rig, PATH=bindir + os.pathsep + os.environ["PATH"], GROK_BIN=str(hang)),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(300):
            if rig["pid"].exists() and rig["pid"].read_text():
                break
            time.sleep(0.1)
        assert rig["pid"].exists(), "the soak never started a turn"
        pid = int(rig["pid"].read_text())
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 128 + signal.SIGTERM, (out, err)
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["verdict"] == "INTERRUPTED" and summary["interrupted"] is True and summary["signal"] == 15
    assert summary["scratch_removed"] is True and os.listdir(rig["parent"]) == []
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                                                     # the engine killed its agent
    assert "SUMMARY: INTERRUPTED" in out and SECRET not in out + err


def test_cli_soak_end_to_end_against_the_fake_acp_cli(rig):
    """The whole loop through the REAL engine against the fake ACP binary the engine tests use:
    two cycles, one JSON line each, a PASS summary, no leftover process, scratch gone."""
    sys.path.insert(0, str(REPO))
    from tests.e2e import grok_support as gs
    gs.write_fake_cli(rig["tmp"] / "fakebin")
    wrapper = rig["tmp"] / "fakebin" / "grok"
    login = rig["tmp"] / "fake-login"
    gs.write_login(login)
    log = rig["tmp"] / "e2e-soak.jsonl"
    r = run_tool(rig, ["soak", "--login", str(login), "--grok-bin", str(wrapper), "--cycles", "2", "--interval", "0.3",
                       "--no-preflight", "--canary-every", "0", "--log", str(log), "--turn-timeout", "90"],
                 PATH=str(rig["tmp"] / "fakebin") + os.pathsep + os.environ["PATH"], GROK_BIN=str(wrapper))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["cycle", "cycle"] and [x["cycle"] for x in lines] == [1, 2]
    for x in lines:
        assert x["ok"] and x["result"] and x["leftover"] == [] and x["problems"] == []
        assert x["home_bytes"] > 0 and x["litter"] == 0 and x["duration_ms"] >= 0
    assert lines[0]["growth_bytes"] == 0 and lines[0]["growth_allowed"] is None     # the baseline cycle
    assert lines[1]["growth_allowed"] is not None
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["verdict"] == "PASS" and summary["cycles_ok"] == 2 and summary["scratch_removed"] is True
    assert "SUMMARY: PASS" in r.stdout and os.listdir(rig["parent"]) == []


def _fake_acp(rig):
    from tests.e2e import grok_support as gs
    sys.path.insert(0, str(REPO))
    gs.write_fake_cli(rig["tmp"] / "fakebin")
    login = rig["tmp"] / "fake-login"
    gs.write_login(login)
    return rig["tmp"] / "fakebin" / "grok", login, str(rig["tmp"] / "fakebin") + os.pathsep + os.environ["PATH"]


def test_cli_soak_sigterm_during_the_canary_stops_the_runner_and_cleans_up(rig):
    wrapper, login, path = _fake_acp(rig)
    log = rig["tmp"] / "canary-soak.jsonl"
    p = subprocess.Popen([sys.executable, str(TOOL), "soak", "--login", str(login), "--grok-bin", str(wrapper),
                          "--cycles", "3", "--interval", "0.2", "--no-preflight", "--canary-every", "1",
                          "--log", str(log)],
                         env=_env(rig, PATH=path, GROK_BIN=str(wrapper), FAKE_HANG="grok_canary"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(600):
            if rig["pid"].exists() and rig["pid"].read_text():
                break
            time.sleep(0.1)
        assert rig["pid"].exists(), "the canary never started"
        pid = int(rig["pid"].read_text())
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=90)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 128 + signal.SIGTERM, (out, err)
    assert rig["graceful"].read_text() == "unwound"
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["verdict"] == "INTERRUPTED" and summary["scratch_removed"] is True
    assert os.listdir(rig["parent"]) == []
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cli_soak_runs_the_canary_every_k_cycles_and_records_what_it_measured(rig):
    wrapper, login, path = _fake_acp(rig)
    log = rig["tmp"] / "canary-every.jsonl"
    r = run_tool(rig, ["soak", "--login", str(login), "--grok-bin", str(wrapper), "--cycles", "3", "--interval", "0.2",
                       "--no-preflight", "--canary-every", "2", "--log", str(log)],
                 PATH=path, GROK_BIN=str(wrapper))
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [bool(x.get("canary")) for x in lines] == [False, True, False]            # cycle 2 only
    assert lines[1]["canary"]["ok"] is True
    runs = _fake_runs(rig)
    assert [x["marker"] for x in runs] == ["grok_canary"]
    assert runs[0]["home_login"] and runs[0]["mode"] == "0o600"                      # its own home with a login copy
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["canary_runs"] == 1 and summary["canary_failed"] == 0 and summary["verdict"] == "PASS"
    assert os.listdir(rig["parent"]) == []


def test_cli_soak_a_failing_canary_fails_the_cycle_and_the_summary(rig):
    wrapper, login, path = _fake_acp(rig)
    log = rig["tmp"] / "canary-fail.jsonl"
    r = run_tool(rig, ["soak", "--login", str(login), "--grok-bin", str(wrapper), "--cycles", "1", "--interval", "0.2",
                       "--no-preflight", "--canary-every", "1", "--log", str(log)],
                 PATH=path, GROK_BIN=str(wrapper), FAKE_FAIL="grok_canary")
    assert r.returncode == 1
    line = json.loads(log.read_text().splitlines()[0])
    assert line["ok"] is False and any("egress canary failed" in p for p in line["problems"])
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["canary_failed"] == 1 and summary["verdict"] == "FAIL"


def test_cli_soak_preflight_failure_stops_before_any_cycle(rig):
    """With the fake CLI the sandbox-denial probe cannot prove anything (it never reads the canary
    file), so the engine reports itself unavailable: the soak must stop, not soak an unusable engine."""
    wrapper, login, path = _fake_acp(rig)
    log = rig["tmp"] / "preflight.jsonl"
    r = run_tool(rig, ["soak", "--login", str(login), "--grok-bin", str(wrapper), "--cycles", "3", "--interval", "0.2",
                       "--canary-every", "0", "--log", str(log)], PATH=path, GROK_BIN=str(wrapper))
    assert r.returncode == 1, r.stdout + r.stderr
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["preflight"] and lines[0]["ok"] is False
    assert "preflight: FAILED" in r.stdout and "SUMMARY: FAIL" in r.stdout
    summary = json.loads(Path(str(log) + ".summary.json").read_text())
    assert summary["cycles_run"] == 0 and summary["preflight_ok"] is False
    assert os.listdir(rig["parent"]) == []
