"""spec-095 P3: the Grok disk history reader (``grok_history``).

Fixtures: ``tests/fixtures/grok_history/real_*`` are REAL ``chat_history.jsonl`` / ``summary.json``
/ ``signals.json`` copies from grok 1.0.46 sessions, scrubbed (paths -> /scratch/project, ids ->
placeholders, injected context blobs -> a length marker). Everything else is synthetic and built
in ``tmp_path`` so the layout under test is the layout the CLI writes:

    <home>/sessions/<urlencode(cwd)>/<session-id>/{chat_history.jsonl,summary.json,signals.json}

Every guard in the module has a test that fails when the guard is removed (see the mutation
table in the P3/P4 report).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

import grok_engine
import grok_history as gh

FIXTURES = Path(__file__).parent / "fixtures" / "grok_history"
CWD = "/scratch/project"
SID = "01a00000-0000-7000-8000-000000000001"
SID2 = "01a00000-0000-7000-8000-000000000002"
SID3 = "01a00000-0000-7000-8000-000000000003"
SID4 = "01a00000-0000-7000-8000-000000000004"


# ------------------------------------------------------------------------------------------
# builders
# ------------------------------------------------------------------------------------------

def jl(*rows) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in rows)


def q(text: str) -> dict:
    return {"type": "user", "content": [{"type": "text", "text": f"<user_query>\n{text}\n</user_query>"}]}


def a(text: str = "", calls: "list[dict] | None" = None) -> dict:
    row: dict = {"type": "assistant", "content": text}
    if calls is not None:
        row["tool_calls"] = calls
    return row


def call(name: str, args, cid: str = "call-1") -> dict:
    return {"id": cid, "name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}


def group_name(cwd: str) -> str:
    return urllib.parse.quote(cwd, safe="")


def put_session(home: Path, cwd: str, sid: str, chat: "bytes | list | None" = None, *,
                summary: "dict | None" = None, signals: "dict | None" = None,
                group: "str | None" = None) -> Path:
    sdir = home / "sessions" / (group or group_name(cwd)) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if chat is not None:
        (sdir / "chat_history.jsonl").write_bytes(chat if isinstance(chat, bytes) else jl(*chat))
    if summary is not None:
        (sdir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if signals is not None:
        (sdir / "signals.json").write_text(json.dumps(signals), encoding="utf-8")
    return sdir


def summ(updated: str = "2026-10-02T10:00:00Z", **extra) -> dict:
    return {"info": {"id": "x", "cwd": CWD}, "updated_at": updated, **extra}


def msgs(home, sid=SID, cwd=CWD, **kw):
    return gh.history_messages(sid, cwd, grok_home=home, **kw)


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "grokhome"
    (h / "sessions").mkdir(parents=True)
    return h


def install_real(home: Path, name: str, sid: str, cwd: str = CWD) -> Path:
    sdir = home / "sessions" / group_name(cwd) / sid
    shutil.copytree(FIXTURES / name, sdir)
    return sdir


# ------------------------------------------------------------------------------------------
# real fixtures: the D7 reader rules on real data
# ------------------------------------------------------------------------------------------

def test_real_edit_bash_session_shape(home):
    install_real(home, "real_edit_bash", SID)
    out = msgs(home)
    assert [m["role"] for m in out] == ["user", "assistant", "assistant", "user", "assistant"]
    assert out[0]["text"] == ("Create a file hello.txt in the current directory containing exactly "
                              "the word ok, then run ls to confirm it exists. Reply with the single word DONE.")
    assert out[1]["text"].startswith("I'll create `hello.txt`")
    assert [t["name"] for t in out[1]["tools"]] == ["Edit", "Bash"]
    assert out[1]["tools"][0] == {"name": "Edit", "kind": "edit", "file": "/scratch/project/hello.txt",
                                  "old": "", "new": "ok"}
    assert out[1]["tools"][1]["kind"] == "bash" and out[1]["tools"][1]["cmd"].startswith("ls -la hello.txt")
    assert (out[2]["text"], out[2]["tools"]) == ("DONE", [])
    assert out[3]["text"].startswith("What was the exact content")
    assert out[4]["text"] == "ok"
    assert all(set(m) == {"role", "text", "tools", "uuid"} for m in out)


def test_real_session_drops_every_non_conversation_row(home):
    install_real(home, "real_edit_bash", SID)
    blob = json.dumps(msgs(home))
    for leaked in ("scrubbed system prompt", "user_info", "<rules>", "scrubbed injected context",
                   "system-reminder", "ENCRYPTED", "exit: 0", "has been created successfully", "<user_query>"):
        assert leaked not in blob


def test_real_cancelled_turn_keeps_all_three_turns(home):
    install_real(home, "real_cancelled_turn", SID2)
    out = msgs(home, SID2)
    assert [m["role"] for m in out] == ["user", "assistant", "assistant", "user", "assistant", "user", "assistant"]
    assert out[3]["text"] == "Run: sleep 20"
    assert out[4]["tools"][0]["cmd"].startswith("sleep 20")
    assert "halted by the harness" not in json.dumps(out)


def test_real_summary_row(home):
    install_real(home, "real_edit_bash", SID)
    [row] = gh.list_sessions(CWD, grok_home=home)
    want = datetime(2026, 10, 2, 23, 0, 17, 968630, tzinfo=timezone.utc).timestamp()
    assert row["id"] == SID and row["provider"] == "grok" and row["cwd"] == CWD
    assert row["name"] == "Create hello.txt with ok then ls" == row["preview"]
    assert row["updatedAt"] == pytest.approx(want, abs=1e-3) and row["recencyAt"] == row["updatedAt"]
    assert row["createdAt"] == pytest.approx(
        datetime(2026, 10, 2, 22, 59, 54, 468485, tzinfo=timezone.utc).timestamp(), abs=1e-3)
    assert row["model"] == "grok-4.7"
    assert row["message_count"] == 5  # signals.json: 2 user + 3 assistant


# ------------------------------------------------------------------------------------------
# user rows (D7: <user_query> only)
# ------------------------------------------------------------------------------------------

def test_user_row_without_user_query_is_skipped(home):
    rows = [{"type": "user", "content": [{"type": "text", "text": "<user_info>\nOS: linux\n</user_info>"}]},
            {"type": "user", "content": [{"type": "text", "text": "just text, no wrapper"}]},
            q("real one")]
    put_session(home, CWD, SID, rows)
    assert [m["text"] for m in msgs(home)] == ["real one"]


def test_synthetic_reason_row_is_skipped_even_with_a_user_query_inside(home):
    quoted = {"type": "user", "synthetic_reason": "system_reminder",
              "content": [{"type": "text", "text": "<user_query>\nquoted old prompt\n</user_query>"}]}
    put_session(home, CWD, SID, [quoted, q("mine")])
    assert [m["text"] for m in msgs(home)] == ["mine"]


def test_user_query_is_unwrapped_and_stripped(home):
    put_session(home, CWD, SID, [q("  hello  \n\n second line ")])
    [m] = msgs(home)
    assert m["text"] == "hello  \n\n second line"


def test_user_query_containing_the_closing_tag_is_not_cut_short(home):
    put_session(home, CWD, SID, [q("explain </user_query> in XML")])
    assert msgs(home)[0]["text"] == "explain </user_query> in XML"


def test_several_blocks_in_one_part_are_each_kept(home):
    row = {"type": "user", "content": [{"type": "text", "text":
           "<user_query>\nfirst\n</user_query>\nnoise\n<user_query>\nsecond\n</user_query>"}]}
    put_session(home, CWD, SID, [row])
    assert msgs(home)[0]["text"] == "first\nsecond"


def test_user_content_as_plain_string_and_mixed_parts(home):
    rows = [{"type": "user", "content": "<user_query>plain string</user_query>"},
            {"type": "user", "content": [{"type": "image", "url": "x"},
                                         {"type": "text", "text": "<user_query>a</user_query>"},
                                         "<user_query>b</user_query>"]},
            {"type": "user", "content": None}, {"type": "user"}]
    put_session(home, CWD, SID, rows)
    assert [m["text"] for m in msgs(home)] == ["plain string", "a\nb"]


def test_empty_user_query_is_skipped(home):
    put_session(home, CWD, SID, [q("   "), q("x")])
    assert [m["text"] for m in msgs(home)] == ["x"]


def test_unicode_survives(home):
    put_session(home, CWD, SID, [q("Привет, мир 你好 🙂 ñ"), a("Ответ: «да» — ✓")])
    out = msgs(home)
    assert out[0]["text"] == "Привет, мир 你好 🙂 ñ" and out[1]["text"] == "Ответ: «да» — ✓"


# ------------------------------------------------------------------------------------------
# assistant rows, tools
# ------------------------------------------------------------------------------------------

def test_other_row_types_are_dropped(home):
    rows = [{"type": "system", "content": "You are Grok"},
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"type": "tool_result", "tool_call_id": "c", "content": "TOOL OUTPUT"},
            {"type": "weird", "content": "x"}, {"content": "no type"},
            q("hi"), a("hello")]
    put_session(home, CWD, SID, rows)
    assert [m["text"] for m in msgs(home)] == ["hi", "hello"]


def test_empty_assistant_rows_are_skipped_but_tool_only_rows_stay(home):
    rows = [q("go"), a(""), a("   \n"), a(None) | {"content": None},
            a("", [call("read_file", {"target_file": "/p/a.py"})])]
    put_session(home, CWD, SID, rows)
    out = msgs(home)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[1]["text"] == "" and out[1]["tools"][0]["file"] == "/p/a.py"


def test_assistant_content_as_parts_list(home):
    row = {"type": "assistant", "content": [{"type": "text", "text": "one "}, {"type": "text", "text": "two"}]}
    put_session(home, CWD, SID, [q("x"), row])
    assert msgs(home)[1]["text"] == "one two"


@pytest.mark.parametrize("name,args,expected", [
    ("run_terminal_command", {"command": "ls", "description": "list"},
     {"name": "Bash", "kind": "bash", "cmd": "ls", "desc": "list"}),
    ("search_replace", {"file_path": "/p/a", "old_string": "x", "new_string": "y"},
     {"name": "Edit", "kind": "edit", "file": "/p/a", "old": "x", "new": "y"}),
    ("write", {"file_path": "/p/b", "content": "data"},
     {"name": "Write", "kind": "write", "file": "/p/b", "preview": "data"}),
    ("read_file", {"target_file": "/p/c", "limit": 5},
     {"name": "Read", "kind": "read", "file": "/p/c"}),
    ("grep", {"pattern": "foo", "path": "/p"},
     {"name": "Grep", "kind": "search", "pattern": "foo", "path": "/p"}),
    ("list_dir", {"target_directory": "/p/d"},
     {"name": "LS", "kind": "other", "summary": "/p/d"}),
    ("web_fetch", {"url": "https://example.com"},
     {"name": "WebFetch", "kind": "other", "summary": "https://example.com"}),
    ("web_search", {"query": "cats"},
     {"name": "WebSearch", "kind": "other", "summary": "cats"}),
    ("todo_write", {"todos": [{"id": "1", "content": "do it", "status": "pending"}], "merge": False},
     {"name": "TodoWrite", "kind": "other",
      "summary": str([{"content": "do it", "status": "pending", "activeForm": "do it"}])}),
])
def test_tool_calls_map_like_the_live_engine(home, name, args, expected):
    put_session(home, CWD, SID, [q("x"), a("t", [call(name, args)])])
    assert msgs(home)[1]["tools"] == [expected]


def test_unknown_tool_passes_through_unchanged(home):
    put_session(home, CWD, SID, [q("x"), a("", [call("brand_new_tool", {"alpha": "1", "beta": 2})])])
    [tool] = msgs(home)[1]["tools"]
    assert tool == {"name": "brand_new_tool", "kind": "other", "summary": "1"}


def test_history_uses_the_engines_own_tool_map(home, monkeypatch):
    # If history carried a private mapping this would still say "Bash".
    monkeypatch.setitem(grok_engine.GROK_TOOL_MAP, "run_terminal_command",
                        lambda i: ("Edit", {"file_path": "/p/probe", "old_string": "", "new_string": "z"}))
    put_session(home, CWD, SID, [q("x"), a("", [call("run_terminal_command", {"command": "ls"})])])
    assert msgs(home)[1]["tools"][0]["file"] == "/p/probe"


def test_subagent_spawn_makes_no_tool_row_like_the_live_stream(home):
    calls = [call("spawn_subagent", {"description": "d", "prompt": "p"}, "c1"),
             call("read_file", {"target_file": "/p/x"}, "c2")]
    put_session(home, CWD, SID, [q("x"), a("", calls), a("", [call("spawn_subagent", {}, "c3")])])
    out = msgs(home)
    assert [t["name"] for t in out[1]["tools"]] == ["Read"]
    assert len(out) == 2  # the spawn-only assistant row has nothing to show


def test_hostile_tool_calls_never_raise(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_ARGS_BYTES", 64)
    calls = [call("run_terminal_command", "{not json"),
             call("run_terminal_command", "[1, 2]"),
             call("run_terminal_command", json.dumps({"command": "x" * 200})),  # over the cap: not parsed
             {"id": "c", "name": 7, "arguments": "{}"},
             {"id": "c", "arguments": "{}"},
             "not a dict", None,
             {"id": "c", "name": "read_file", "arguments": {"target_file": "/p/dict-args"}},
             {"id": "c", "name": "read_file", "arguments": 5}]
    put_session(home, CWD, SID, [q("x"), a("t", calls), a("u") | {"tool_calls": "oops"}])
    out = msgs(home)
    cmds = [t.get("cmd") for t in out[1]["tools"] if t["kind"] == "bash"]
    assert cmds == ["", "", ""]  # bad JSON, non-dict JSON and an oversized arguments string -> {}
    assert any(t.get("file") == "/p/dict-args" for t in out[1]["tools"])
    assert out[2]["tools"] == []


def test_injected_formatter_is_used(home):
    put_session(home, CWD, SID, [q("x"), a("", [call("read_file", {"target_file": "/p/a"})])])
    seen = []

    def fmt(name, inp):
        seen.append((name, inp))
        return {"name": name, "kind": "custom", "inp": inp}

    out = msgs(home, format_tool=fmt)
    assert out[1]["tools"] == [{"name": "Read", "kind": "custom", "inp": {"file_path": "/p/a"}}]
    assert seen == [("Read", {"file_path": "/p/a"})]


def test_a_broken_formatter_falls_back_to_the_default(home):
    put_session(home, CWD, SID, [q("x"), a("t", [call("read_file", {"target_file": "/p/a"})])])

    def boom(name, inp):
        raise RuntimeError("formatter bug")

    assert msgs(home, format_tool=boom)[1]["tools"] == [{"name": "Read", "kind": "read", "file": "/p/a"}]


def test_a_formatter_returning_junk_drops_that_tool_row(home):
    put_session(home, CWD, SID, [q("x"), a("t", [call("read_file", {"target_file": "/p/a"})])])
    assert msgs(home, format_tool=lambda n, i: "junk")[1]["tools"] == []


def test_default_formatter_matches_webapp_format_tool():
    webapp = pytest.importorskip("webapp")
    long = "z" * 2500
    cases = [("Bash", {"command": "ls", "description": "d"}), ("Bash", {}),
             ("Edit", {"file_path": "/a", "old_string": long, "new_string": long}),
             ("Edit", {"file_path": "/a", "old_string": "o", "new_string": "n"}),
             ("Write", {"file_path": "/a", "content": long}), ("Write", {"file_path": "/a", "content": "short"}),
             ("Write", {"file_path": "/a", "content": 5}),
             ("Read", {"file_path": "/a"}), ("Glob", {"pattern": "*.py", "path": "/p"}),
             ("Grep", {"pattern": "x", "path": "/p"}),
             ("LS", {"path": "/p"}), ("WebFetch", {"url": long}), ("WebSearch", {"query": "q"}),
             ("TodoWrite", {"todos": [{"content": "c", "status": "pending", "activeForm": "c"}]}),
             ("Mystery", {}), ("Mystery", {"a": 1})]
    for name, inp in cases:
        assert gh.default_format_tool(name, inp) == webapp._format_tool(name, inp), (name, inp)
    assert gh.default_format_tool("Bash", "not a dict") == webapp._format_tool("Bash", "not a dict")


# ------------------------------------------------------------------------------------------
# cap, malformed lines, uuid, bounded reads
# ------------------------------------------------------------------------------------------

def test_message_cap_is_the_last_100(home):
    rows = []
    for i in range(75):
        rows += [q(f"u{i}"), a(f"a{i}")]
    put_session(home, CWD, SID, rows)  # 150 messages
    out = msgs(home)
    assert len(out) == 100
    assert out[0]["text"] == "u25" and out[-1]["text"] == "a74"
    assert [m["text"] for m in out][:3] == ["u25", "a25", "u26"]  # order preserved, oldest dropped


def test_explicit_limit_and_clamping(home, monkeypatch):
    rows = []
    for i in range(30):
        rows.append(q(f"u{i}"))
    put_session(home, CWD, SID, rows)
    assert [m["text"] for m in msgs(home, limit=3)] == ["u27", "u28", "u29"]
    assert len(msgs(home, limit=0)) == 1 and len(msgs(home, limit=-5)) == 1
    assert len(msgs(home, limit="junk")) == 30 and len(msgs(home, limit=None)) == 30
    monkeypatch.setattr(gh, "MAX_LIMIT", 7)
    assert len(msgs(home, limit=10 ** 9)) == 7


def test_malformed_lines_are_skipped(home):
    raw = (jl(q("first"))
           + b"{this is not json\n" + b"\n" + b"   \n" + b"[1, 2, 3]\n" + b'"just a string"\n' + b"42\n"
           + b"\xff\xfe broken utf8 \xc3\x28\n"
           + jl(a("answer"))
           + b'{"type":"user","content":[{"type":"text","text":"<user_query>trunca')  # half-written tail
    put_session(home, CWD, SID, raw)
    assert [m["text"] for m in msgs(home)] == ["first", "answer"]


def test_uuid_is_session_id_and_row_byte_offset(home):
    rows = [{"type": "system", "content": "s" * 100}, q("héllo"), a("there")]
    raw = jl(*rows)
    put_session(home, CWD, SID, raw)
    out = msgs(home)
    off_user = len(jl(rows[0]))
    off_asst = off_user + len(jl(rows[1]))
    assert [m["uuid"] for m in out] == [f"{SID}:{off_user}", f"{SID}:{off_asst}"]


def test_uuid_is_stable_across_tail_windows(home):
    rows = [q(f"m{i}") for i in range(40)]
    put_session(home, CWD, SID, rows)
    full = msgs(home)
    size = (home / "sessions" / group_name(CWD) / SID / "chat_history.jsonl").stat().st_size
    tail = msgs(home, max_bytes=size // 2)
    assert 0 < len(tail) < len(full)
    assert tail == full[-len(tail):]  # same texts AND the same uuids


def test_big_file_is_read_from_its_tail(home):
    rows = [q(f"message-{i:03d}-" + "x" * 80) for i in range(200)]
    put_session(home, CWD, SID, rows)
    out = msgs(home, max_bytes=4096)
    assert out and out[-1]["text"].startswith("message-199-")
    assert out[0]["text"].startswith("message-") and not out[0]["text"].startswith("message-000")
    assert len(out) < 60
    ids = [m["text"][8:11] for m in out]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # whole rows, in order, no fragment


def test_tail_window_keeps_a_line_that_starts_exactly_at_the_window(home):
    rows = [q(f"row{i}") for i in range(10)]
    raw = jl(*rows)
    put_session(home, CWD, SID, raw)
    three = len(jl(*rows[-3:]))
    assert [m["text"] for m in msgs(home, max_bytes=three)] == ["row7", "row8", "row9"]
    # one byte more lands inside row6 -> the partial line is dropped, nothing breaks
    assert [m["text"] for m in msgs(home, max_bytes=three + 1)] == ["row7", "row8", "row9"]


def test_oversized_line_is_skipped_without_smuggling_its_tail(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_LINE_BYTES", 1000)
    smuggle = b'{"type":"assistant","content":"SMUGGLED"}\n'
    raw = (jl(q("before")) + b"x" * 1001 + smuggle + jl(a("after"))
           + b"y" * 5000)  # an oversized last line with no newline
    put_session(home, CWD, SID, raw)
    assert [m["text"] for m in msgs(home)] == ["before", "after"]


def test_missing_session_and_missing_history_are_empty(home):
    assert msgs(home) == []
    put_session(home, CWD, SID, None, summary=summ())
    assert msgs(home) == []


def test_history_dir_in_place_of_file_is_not_a_crash(home):
    sdir = put_session(home, CWD, SID, None, summary=summ())
    (sdir / "chat_history.jsonl").mkdir()
    assert msgs(home) == []


# ------------------------------------------------------------------------------------------
# security: ids, cwd, symlinks, FIFOs
# ------------------------------------------------------------------------------------------

BAD_IDS = ["", "..", ".", "../x", "a/b", "/etc/passwd", "../../../outside", SID + "x", "x" + SID,
           SID.upper(), SID.replace("-", ""), "01a00000-0000-7000-8000-00000000000g",
           "%2e%2e", "..%2f..", SID + "\n", " " + SID, SID + "\x00", "01a00000-0000-7000-8000-0000000000010",
           None, 123, b"x", ["a"]]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_bad_session_ids_are_refused_before_any_path_join(home, bad):
    # Plant files a traversal would reach if the id reached the filesystem.
    (home / "sessions" / "chat_history.jsonl").write_bytes(jl(q("PLANTED-IN-SESSIONS-ROOT")))
    (home / "chat_history.jsonl").write_bytes(jl(q("PLANTED-IN-HOME")))
    with pytest.raises(ValueError):
        gh.history_messages(bad, CWD, grok_home=home)
    assert gh.session_exists(bad, CWD, grok_home=home) is False


def test_dotdot_id_would_read_the_sessions_root_without_the_regex(home):
    # The regex is the ONLY thing stopping `<group>/..` from resolving to a real directory that
    # is itself inside sessions/ — pin that with a file that would be returned.
    put_session(home, CWD, SID, [q("legit")])
    (home / "sessions" / "chat_history.jsonl").write_bytes(jl(q("PLANTED")))
    with pytest.raises(ValueError):
        gh.history_messages("..", CWD, grok_home=home)


def test_valid_session_id_predicate():
    assert gh.valid_session_id(SID) and gh.valid_session_id("01a0fed8-77d4-7cd2-ba1e-721bcda3839d")
    for bad in BAD_IDS:
        assert gh.valid_session_id(bad) is False


@pytest.mark.parametrize("bad", ["", "relative/path", "..", "../..", "~", "~/x", ".", "x", "\x00", "/a\x00b",
                                 "/" + "a" * 5000, None, 5, ["/x"], "/lone\ud800surrogate"])
def test_bad_cwds_are_refused(home, bad):
    (home / "chat_history.jsonl").write_bytes(jl(q("PLANTED")))
    put_session(home, CWD, SID, [q("x")])
    with pytest.raises(ValueError):
        gh.history_messages(SID, bad, grok_home=home)
    with pytest.raises(ValueError):
        gh.list_sessions(bad, grok_home=home)
    with pytest.raises(ValueError):
        gh.encode_cwd(bad)
    assert gh.session_exists(SID, bad, grok_home=home) is False


def test_relative_dotdot_cwd_reaches_nothing_even_if_containment_is_off(home, monkeypatch):
    # Layer test: the absolute-path rule alone must stop cwd=".." (-> <sessions>/../<sid>).
    sdir = home / SID
    sdir.mkdir()
    (sdir / "chat_history.jsonl").write_bytes(jl(q("ESCAPED")))
    monkeypatch.setattr(gh, "_inside", lambda path, root: True)
    with pytest.raises(ValueError):
        gh.history_messages(SID, "..", grok_home=home)
    assert gh.session_exists(SID, "..", grok_home=home) is False


def test_symlinked_home_is_refused(tmp_path, home):
    put_session(home, CWD, SID, [q("x")])
    link = tmp_path / "link-home"
    link.symlink_to(home, target_is_directory=True)
    with pytest.raises(gh.GrokHistoryError):
        gh.history_messages(SID, CWD, grok_home=link)
    with pytest.raises(gh.GrokHistoryError):
        gh.list_sessions(CWD, grok_home=link)
    with pytest.raises(gh.GrokHistoryError):
        list(gh.iter_search_docs(CWD, grok_home=link))
    assert gh.session_exists(SID, CWD, grok_home=link) is False
    assert gh.history_messages(SID, CWD, grok_home=home)  # the real one still works


def test_symlinked_sessions_dir_is_refused(tmp_path, home):
    real = tmp_path / "elsewhere"
    shutil.move(str(home / "sessions"), real)
    (home / "sessions").symlink_to(real, target_is_directory=True)
    with pytest.raises(gh.GrokHistoryError):
        gh.history_messages(SID, CWD, grok_home=home)
    with pytest.raises(gh.GrokHistoryError):
        gh.list_sessions(CWD, grok_home=home)
    assert gh.session_exists(SID, CWD, grok_home=home) is False


def test_symlink_in_a_parent_of_home_is_fine(tmp_path, home):
    # The engine only refuses a symlinked GROK_HOME itself; a symlinked ancestor is normal
    # (/home -> /mnt/home) and must keep working.
    put_session(home, CWD, SID, [q("x")])
    parent_link = tmp_path / "plink"
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    via = parent_link / "grokhome"
    assert [m["text"] for m in gh.history_messages(SID, CWD, grok_home=via)] == ["x"]
    assert [r["id"] for r in gh.list_sessions(CWD, grok_home=via)] == [SID]


def _outside_session(tmp_path) -> Path:
    out = tmp_path / "outside" / SID
    out.mkdir(parents=True)
    (out / "chat_history.jsonl").write_bytes(jl(q("ESCAPED")))
    (out / "summary.json").write_text(json.dumps(summ()), encoding="utf-8")
    return out


def test_symlinked_session_dir_is_not_followed(tmp_path, home):
    out = _outside_session(tmp_path)
    group = home / "sessions" / group_name(CWD)
    group.mkdir(parents=True)
    (group / SID).symlink_to(out, target_is_directory=True)
    assert msgs(home) == [] and gh.session_exists(SID, CWD, grok_home=home) is False
    assert gh.list_sessions(CWD, grok_home=home) == []


def test_symlinked_group_dir_is_not_followed(tmp_path, home):
    out = _outside_session(tmp_path)
    (home / "sessions" / group_name(CWD)).symlink_to(out.parent, target_is_directory=True)
    assert msgs(home) == [] and gh.session_exists(SID, CWD, grok_home=home) is False
    assert gh.list_sessions(CWD, grok_home=home) == []


def test_symlinked_group_dir_is_not_followed_by_the_decode_scan_either(tmp_path, home):
    out = _outside_session(tmp_path)
    (home / "sessions" / group_name(CWD).lower().replace("%2f", "%2F")).mkdir()  # make root non-empty
    # a lowercase-hex name forces the scan path (direct candidate misses)
    (home / "sessions" / "%2fscratch%2fproject").symlink_to(out.parent, target_is_directory=True)
    assert msgs(home) == [] and gh.list_sessions(CWD, grok_home=home) == []


def test_each_containment_layer_blocks_a_symlinked_session_on_its_own(tmp_path, home, monkeypatch):
    out = _outside_session(tmp_path)
    group = home / "sessions" / group_name(CWD)
    group.mkdir(parents=True)
    (group / SID).symlink_to(out, target_is_directory=True)
    # layer 1 (lstat) off -> the resolved-path containment check must still refuse
    monkeypatch.setattr(gh, "_is_real_dir", lambda p: True)
    assert msgs(home) == [] and gh.session_exists(SID, CWD, grok_home=home) is False
    assert gh.list_sessions(CWD, grok_home=home) == []
    monkeypatch.undo()
    # layer 2 (containment) off -> the lstat check must still refuse
    monkeypatch.setattr(gh, "_inside", lambda p, r: True)
    assert msgs(home) == [] and gh.session_exists(SID, CWD, grok_home=home) is False
    assert gh.list_sessions(CWD, grok_home=home) == []


def test_symlinked_history_file_is_not_followed(tmp_path, home):
    secret = tmp_path / "secret.jsonl"
    secret.write_bytes(jl(q("SECRET FROM OUTSIDE")))
    sdir = put_session(home, CWD, SID, None, summary=summ())
    (sdir / "chat_history.jsonl").symlink_to(secret)
    assert msgs(home) == []


def test_symlinked_summary_is_not_followed(tmp_path, home):
    outside = tmp_path / "s.json"
    outside.write_text(json.dumps(summ(session_summary="LEAKED TITLE")), encoding="utf-8")
    sdir = put_session(home, CWD, SID, [q("hi")])
    (sdir / "summary.json").symlink_to(outside)
    [row] = gh.list_sessions(CWD, grok_home=home)
    assert row["name"] is None and "LEAKED" not in json.dumps(row)


def test_fifo_history_does_not_block(home):
    sdir = put_session(home, CWD, SID, None, summary=summ())
    fifo = sdir / "chat_history.jsonl"
    os.mkfifo(fifo)
    result: dict = {}

    def run():
        result["v"] = msgs(home)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    stuck = t.is_alive()
    if stuck:  # release a plain open() so the thread does not outlive the test
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(2)
    assert not stuck and result["v"] == []


# ------------------------------------------------------------------------------------------
# cwd -> group directory (the encoding round trip)
# ------------------------------------------------------------------------------------------

def test_encode_matches_the_real_directory_names():
    assert gh.encode_cwd("/tmp/gk-sb") == "%2Ftmp%2Fgk-sb"          # names seen under ~/.grok/sessions
    assert gh.encode_cwd("/tmp/grok-spike") == "%2Ftmp%2Fgrok-spike"


EDGE_CWDS = ["/work/my project", "/work/Проект/файлы", "/work/50%/done", "/work/a+b&c=d", "/work/we're", "/work/日本語",
             "/work/emoji-🙂", "/work/semi;colon", "/work/hash#tag", "/work/q?x", "/work/dot.dot/..hidden",
             "/work/colon:at@sign", "/work/with%2Fliteral", "/work/tilde~name", "/work/UPPER_lower-1.2"]


@pytest.mark.parametrize("cwd", EDGE_CWDS)
def test_cwd_round_trip_for_awkward_paths(home, cwd):
    put_session(home, cwd, SID, [q("found")], summary=summ())
    assert [m["text"] for m in msgs(home, cwd=cwd)] == ["found"]
    assert gh.session_exists(SID, cwd, grok_home=home)
    assert [r["id"] for r in gh.list_sessions(cwd, grok_home=home)] == [SID]
    # and it is not found under any other cwd
    assert msgs(home, cwd=cwd + "x") == [] and gh.list_sessions(cwd + "x", grok_home=home) == []


@pytest.mark.parametrize("cwd", EDGE_CWDS)
def test_decode_scan_finds_a_group_named_by_a_different_encoder(home, cwd):
    # An encoder that leaves reserved characters raw and writes lowercase hex: the name differs
    # from quote(cwd) but percent-decodes to the same path.
    alt = "".join(c if c in ":@+,;=$&'()*!" else urllib.parse.quote(c, safe="") for c in cwd)
    alt = alt.replace("%2F", "%2f")
    assert alt != gh.encode_cwd(cwd)
    put_session(home, cwd, SID, [q("found")], group=alt)
    assert [m["text"] for m in msgs(home, cwd=cwd)] == ["found"]


def test_long_path_group_is_found_through_its_cwd_file(home):
    cwd = "/work/" + "/".join(["a-very-long-directory-name"] * 12)
    assert len(gh.encode_cwd(cwd).encode()) > 255
    group = "work-a-very-long-directory-name-0123abcd"
    put_session(home, cwd, SID, [q("found")], group=group)
    (home / "sessions" / group / ".cwd").write_text(cwd + "\n", encoding="utf-8")
    assert [m["text"] for m in msgs(home, cwd=cwd)] == ["found"]
    assert [r["id"] for r in gh.list_sessions(cwd, grok_home=home)] == [SID]
    assert gh.session_exists(SID, cwd, grok_home=home)


def test_cwd_file_must_match_exactly(home):
    cwd = "/work/long"
    put_session(home, cwd, SID, [q("x")], group="slug-hash")
    (home / "sessions" / "slug-hash" / ".cwd").write_text("/work/other", encoding="utf-8")
    assert msgs(home, cwd=cwd) == []
    (home / "sessions" / "slug-hash" / ".cwd").write_bytes(b"\xff\xfe")
    assert msgs(home, cwd=cwd) == []


def test_cwd_file_oversize_or_symlinked_is_ignored(tmp_path, home, monkeypatch):
    cwd = "/work/long"
    put_session(home, cwd, SID, [q("x")], group="slug-a")
    (home / "sessions" / "slug-a" / ".cwd").write_text(cwd + "\n" + "z" * 100, encoding="utf-8")
    monkeypatch.setattr(gh, "MAX_SUMMARY_BYTES", 50)
    assert msgs(home, cwd=cwd) == []
    monkeypatch.undo()
    real = tmp_path / "real-cwd"
    real.write_text(cwd, encoding="utf-8")
    put_session(home, cwd, SID2, [q("y")], group="slug-b")
    (home / "sessions" / "slug-b" / ".cwd").symlink_to(real)
    assert msgs(home, SID2, cwd=cwd) == []


def test_group_scan_is_bounded(home, monkeypatch):
    cwd = "/work/deep"
    for i in range(6):
        (home / "sessions" / f"%2Fother{i}").mkdir()
    put_session(home, cwd, SID, [q("x")], group="%2fwork%2fdeep")  # lowercase -> only the scan can find it
    monkeypatch.setattr(gh, "MAX_GROUP_SCAN", 2)
    names = sorted(e.name for e in os.scandir(home / "sessions"))
    found_at = names.index("%2fwork%2fdeep")
    # entries are visited in directory order, so assert the bound structurally: with a cap of 0
    # nothing can be found, with the cap lifted it is.
    monkeypatch.setattr(gh, "MAX_GROUP_SCAN", 0)
    assert msgs(home, cwd=cwd) == [] and found_at >= 0
    monkeypatch.setattr(gh, "MAX_GROUP_SCAN", 5000)
    assert [m["text"] for m in msgs(home, cwd=cwd)] == ["x"]


def test_trailing_slash_and_symlinked_cwd_forms_are_found(tmp_path, home):
    real = tmp_path / "real-project"
    real.mkdir()
    link = tmp_path / "link-project"
    link.symlink_to(real, target_is_directory=True)
    put_session(home, str(real), SID, [q("by real path")], summary=summ())
    assert [m["text"] for m in msgs(home, cwd=str(link))] == ["by real path"]       # realpath variant
    assert [m["text"] for m in msgs(home, cwd=str(real) + "/")] == ["by real path"]  # normpath variant
    assert [r["id"] for r in gh.list_sessions(str(link), grok_home=home)] == [SID]


def test_non_session_entries_in_sessions_root_are_ignored(home):
    (home / "sessions" / "prompt_history.jsonl").write_text("{}", encoding="utf-8")
    (home / "sessions" / "sandbox-events.jsonl").write_text("{}", encoding="utf-8")
    (home / "sessions" / "session_search.sqlite").write_bytes(b"x")
    assert msgs(home) == [] and gh.list_sessions(CWD, grok_home=home) == []


def test_missing_home_or_sessions_dir_is_empty(tmp_path):
    nowhere = tmp_path / "nope"
    assert gh.history_messages(SID, CWD, grok_home=nowhere) == []
    assert gh.list_sessions(CWD, grok_home=nowhere) == []
    assert gh.session_exists(SID, CWD, grok_home=nowhere) is False
    assert list(gh.iter_search_docs(CWD, grok_home=nowhere)) == []


def test_default_home_follows_the_engines_grok_home(tmp_path, monkeypatch):
    h = tmp_path / "engine-home"
    (h / "sessions").mkdir(parents=True)
    put_session(h, CWD, SID, [q("via engine home")])
    monkeypatch.setenv("GROK_HOME", str(h))
    assert [m["text"] for m in gh.history_messages(SID, CWD)] == ["via engine home"]
    assert [r["id"] for r in gh.list_sessions(CWD)] == [SID]
    assert gh.session_exists(SID, CWD)


# ------------------------------------------------------------------------------------------
# session_exists
# ------------------------------------------------------------------------------------------

def test_session_exists_rules(home):
    assert gh.session_exists(SID, CWD, grok_home=home) is False
    put_session(home, CWD, SID, [q("x")])
    assert gh.session_exists(SID, CWD, grok_home=home) is True            # history only
    put_session(home, CWD, SID2, None, summary=summ())
    assert gh.session_exists(SID2, CWD, grok_home=home) is True           # summary only
    (home / "sessions" / group_name(CWD) / SID3).mkdir()
    assert gh.session_exists(SID3, CWD, grok_home=home) is False          # empty dir: nothing to resume
    assert gh.session_exists(SID, "/other/cwd", grok_home=home) is False  # same id, wrong project


# ------------------------------------------------------------------------------------------
# list_sessions
# ------------------------------------------------------------------------------------------

def test_title_precedence_and_first_query_fallback(home):
    put_session(home, CWD, SID, [q("first question")], summary=summ(session_summary="  Renamed   title ",
                                                                    generated_title="Generated"))
    put_session(home, CWD, SID2, [q("second")], summary=summ(session_summary="", generated_title="Only generated"))
    put_session(home, CWD, SID3, [{"type": "system", "content": "s"}, q("  spaced\n  out   question  " + "w" * 400)],
                summary=summ(session_summary="", generated_title=None))
    put_session(home, CWD, SID4, None, summary=summ())
    rows = {r["id"]: r for r in gh.list_sessions(CWD, grok_home=home)}
    assert (rows[SID]["name"], rows[SID]["preview"]) == ("Renamed title", "Renamed title")
    assert (rows[SID2]["name"], rows[SID2]["preview"]) == ("Only generated", "Only generated")
    assert rows[SID3]["name"] is None
    assert rows[SID3]["preview"].startswith("spaced out question w")
    assert len(rows[SID3]["preview"]) == gh.PREVIEW_CHARS
    assert (rows[SID4]["name"], rows[SID4]["preview"]) == (None, "")


def test_preview_lookup_reads_the_head_not_the_tail(home, monkeypatch):
    monkeypatch.setattr(gh, "HEAD_PREVIEW_BYTES", 300)
    rows = [q("the opening question")] + [a("filler " * 40) for _ in range(30)] + [q("a much later question")]
    put_session(home, CWD, SID, rows, summary=summ())
    assert gh.list_sessions(CWD, grok_home=home)[0]["preview"] == "the opening question"
    # an opening message beyond the head window is not found (bounded read), never an exception
    put_session(home, CWD, SID2, [a("filler " * 100), q("too deep")], summary=summ())
    got = {r["id"]: r for r in gh.list_sessions(CWD, grok_home=home)}
    assert got[SID2]["preview"] == ""


def test_subagent_sessions_are_not_listed(home):
    for sid, kind in ((SID, "subagent"), (SID2, "subagent_resume"), (SID3, "subagent_fork"), (SID4, "headless")):
        put_session(home, CWD, sid, [q("x")], summary=summ(session_kind=kind))
    other = "01a00000-0000-7000-8000-000000000005"
    put_session(home, CWD, other, [q("x")], summary=summ(session_kind=None))
    assert sorted(r["id"] for r in gh.list_sessions(CWD, grok_home=home)) == sorted([SID4, other])


def test_listing_is_newest_first_by_updated_at_not_by_id(home):
    put_session(home, CWD, SID, [q("x")], summary=summ("2026-10-02T12:00:00Z"))    # lowest id, newest update
    put_session(home, CWD, SID2, [q("x")], summary=summ("2026-10-01T12:00:00Z"))
    put_session(home, CWD, SID3, [q("x")], summary=summ("2026-10-02T11:00:00.5Z"))
    assert [r["id"] for r in gh.list_sessions(CWD, grok_home=home)] == [SID, SID3, SID2]
    assert [r["id"] for r in gh.list_sessions(CWD, limit=2, grok_home=home)] == [SID, SID3]
    assert len(gh.list_sessions(CWD, limit=0, grok_home=home)) == 1


def test_listing_without_summary_uses_history_mtime_and_skips_empty_dirs(home):
    sdir = put_session(home, CWD, SID, [q("x")])
    os.utime(sdir / "chat_history.jsonl", (1_700_000_000, 1_700_000_000))
    (home / "sessions" / group_name(CWD) / SID2).mkdir()
    sdir3 = put_session(home, CWD, SID3, [q("y")])
    (sdir3 / "summary.json").write_text("{garbage", encoding="utf-8")
    os.utime(sdir3 / "chat_history.jsonl", (1_700_000_500, 1_700_000_500))
    rows = gh.list_sessions(CWD, grok_home=home)
    assert [(r["id"], r["updatedAt"]) for r in rows] == [(SID3, 1_700_000_500.0), (SID, 1_700_000_000.0)]


def test_listing_without_updated_at_uses_summary_mtime(home):
    sdir = put_session(home, CWD, SID, [q("x")], summary={"info": {"id": "x"}})
    os.utime(sdir / "summary.json", (1_650_000_000, 1_650_000_000))
    assert gh.list_sessions(CWD, grok_home=home)[0]["updatedAt"] == 1_650_000_000.0


def test_last_active_at_is_the_second_choice(home):
    put_session(home, CWD, SID, [q("x")], summary={"last_active_at": "2026-10-02T00:00:00Z"})
    assert gh.list_sessions(CWD, grok_home=home)[0]["updatedAt"] == datetime(
        2026, 10, 2, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("raw,expect", [
    ("2026-10-02T23:17:36.408048591Z", 1790983056.408048),
    ("2026-10-02T23:17:36Z", 1790983056.0),
    ("2026-10-02T23:17:36+00:00", 1790983056.0),
    ("2026-10-03T02:17:36+03:00", 1790983056.0),
    ("2026-10-03T02:17:36+0300", 1790983056.0),
    ("2026-10-02T23:17:36.4+00:00", 1790983056.4),
    ("2026-10-02 23:17:36.408048591Z", 1790983056.408048),
    ("2026-10-02T23:17:36", 1790983056.0),
    ("nonsense", None), ("", None), (None, None), (12345, None), ("2026-13-45T99:99:99Z", None),
])
def test_iso_parsing(raw, expect):
    got = gh._iso_to_epoch(raw)
    assert (got is None) if expect is None else got == pytest.approx(expect, abs=1e-5)


def test_message_count_is_from_signals_or_none(home):
    put_session(home, CWD, SID, [q("x")], summary=summ(), signals={"userMessageCount": 2, "assistantMessageCount": 5})
    put_session(home, CWD, SID2, [q("x")], summary=summ(), signals={"userMessageCount": 2})
    put_session(home, CWD, SID3, [q("x")], summary=summ(), signals={"userMessageCount": True, "assistantMessageCount": 1})
    put_session(home, CWD, SID4, [q("x")], summary=summ(), signals={"userMessageCount": "2", "assistantMessageCount": 1})
    rows = {r["id"]: r["message_count"] for r in gh.list_sessions(CWD, grok_home=home)}
    assert rows == {SID: 7, SID2: None, SID3: None, SID4: None}


def test_listing_ignores_non_uuid_entries_and_files(home):
    put_session(home, CWD, SID, [q("x")], summary=summ())
    group = home / "sessions" / group_name(CWD)
    (group / "prompt_history.jsonl").write_text("{}", encoding="utf-8")
    (group / "not-a-session").mkdir()
    (group / SID2).write_text("a file named like a session", encoding="utf-8")
    assert [r["id"] for r in gh.list_sessions(CWD, grok_home=home)] == [SID]


def test_listing_scans_at_most_max_sessions_per_cwd(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_SESSIONS_PER_CWD", 2)
    for sid, day in ((SID, "01"), (SID2, "02"), (SID3, "03"), (SID4, "04")):
        put_session(home, CWD, sid, [q("x")], summary=summ(f"2026-10-{day}T00:00:00Z"))
    # the newest ids (UUIDv7 sort by creation time) are the ones scanned
    assert sorted(r["id"] for r in gh.list_sessions(CWD, grok_home=home)) == sorted([SID3, SID4])


def test_listing_merges_the_given_and_resolved_groups_without_duplicates(tmp_path, home):
    real = tmp_path / "real-p"
    real.mkdir()
    link = tmp_path / "link-p"
    link.symlink_to(real, target_is_directory=True)
    put_session(home, str(real), SID, [q("a")], summary=summ())
    put_session(home, str(link), SID2, [q("b")], summary=summ("2026-10-03T00:00:00Z"))
    put_session(home, str(link), SID, [q("a again")], summary=summ())
    rows = gh.list_sessions(str(link), grok_home=home)
    assert [r["id"] for r in rows] == [SID2, SID] and len({r["id"] for r in rows}) == 2


def test_list_rows_have_the_keys_the_codex_consumers_read(home):
    put_session(home, CWD, SID, [q("x")], summary=summ(session_summary="T"))
    [row] = gh.list_sessions(CWD, grok_home=home)
    # api_project_sessions / api_search read these from a codex_engine.list_threads row
    for key in ("id", "cwd", "name", "preview", "updatedAt", "recencyAt"):
        assert key in row
    assert isinstance(row["updatedAt"], float)
    datetime.fromtimestamp(row["recencyAt"] or row["updatedAt"] or 0, tz=timezone.utc)  # webapp does this


# ------------------------------------------------------------------------------------------
# search
# ------------------------------------------------------------------------------------------

def _search_home(home):
    put_session(home, CWD, SID, [q("How do I deploy the Zeppelin service?"), a("Run the zeppelin deploy script."),
                                 {"type": "tool_result", "tool_call_id": "c", "content": "TOOLONLYWORD output"}],
                summary=summ("2026-10-01T00:00:00Z", session_summary="Deploy help"))
    put_session(home, CWD, SID2, [q("Unrelated chat about cats"), a("Cats are great")],
                summary=summ("2026-10-02T00:00:00Z", session_summary="Feline talk"))
    put_session(home, CWD, SID3, [q("Привет Мир"), a("Ответ")], summary=summ("2026-10-03T00:00:00Z"))


def test_search_matches_all_terms_case_insensitively(home):
    _search_home(home)
    assert [r["id"] for r in gh.search_sessions("ZEPPELIN deploy", CWD, grok_home=home)] == [SID]
    assert [r["id"] for r in gh.search_sessions("zeppelin cats", CWD, grok_home=home)] == []  # AND, not OR
    assert [r["id"] for r in gh.search_sessions("привет МИР", CWD, grok_home=home)] == [SID3]


def test_search_hits_a_title_only_match(home):
    _search_home(home)
    [hit] = gh.search_sessions("feline", CWD, grok_home=home)
    assert hit["id"] == SID2 and hit["preview"] == "Feline talk"


def test_search_does_not_look_into_tool_output(home):
    _search_home(home)
    assert gh.search_sessions("TOOLONLYWORD", CWD, grok_home=home) == []


def test_search_rows_are_list_threads_shaped_and_newest_first(home):
    _search_home(home)
    hits = gh.search_sessions("the", CWD, grok_home=home)  # in two of the three chats
    assert [h["id"] for h in hits] == [SID]  # 'the' appears only in the Zeppelin chat
    put_session(home, CWD, SID4, [q("the newest thing")], summary=summ("2026-10-05T00:00:00Z"))
    hits = gh.search_sessions("the", CWD, grok_home=home)
    assert [h["id"] for h in hits] == [SID4, SID]
    for h in hits:
        assert h["provider"] == "grok" and h["cwd"] == CWD and h["recencyAt"] == h["updatedAt"]


def test_search_snippet_is_a_window_around_the_first_hit(home):
    filler = "lorem ipsum " * 60
    put_session(home, CWD, SID, [q(filler + "NEEDLE here " + filler)], summary=summ())
    [hit] = gh.search_sessions("needle", CWD, grok_home=home)
    assert "NEEDLE here" in hit["preview"] and len(hit["preview"]) <= gh.SNIPPET_CHARS


def test_search_limit_empty_query_and_term_cap(home):
    for i, sid in enumerate((SID, SID2, SID3, SID4)):
        put_session(home, CWD, sid, [q("common word")], summary=summ(f"2026-10-0{i + 1}T00:00:00Z"))
    assert len(gh.search_sessions("common", CWD, limit=2, grok_home=home)) == 2
    assert gh.search_sessions("", CWD, grok_home=home) == [] and gh.search_sessions("   ", CWD, grok_home=home) == []
    assert gh.search_sessions(None, CWD, grok_home=home) == []
    nine = "common " + " ".join(f"zzz{i}" for i in range(8))  # 9 terms: only the first 8 are used
    assert len(gh.search_sessions(nine, CWD, grok_home=home)) == 0
    eight = "common " + " ".join(["common"] * 7) + " zzz-ninth-term-is-ignored"
    assert len(gh.search_sessions(eight, CWD, grok_home=home)) == 4


def test_search_docs_are_bounded(home, monkeypatch):
    put_session(home, CWD, SID, [q("OLDEST " + "a" * 500), q("b" * 300 + " NEWEST")], summary=summ())
    [doc] = list(gh.iter_search_docs(CWD, grok_home=home, max_chars=400))
    assert set(doc) == {"id", "cwd", "title", "updatedAt", "text"} and len(doc["text"]) == 400
    assert doc["text"].endswith("NEWEST") and "OLDEST" not in doc["text"]  # the newest text survives the cap
    monkeypatch.setattr(gh, "SEARCH_READ_BYTES", 480)
    [doc] = list(gh.iter_search_docs(CWD, grok_home=home))
    assert "OLDEST" not in doc["text"] and "NEWEST" in doc["text"]  # tail-bounded read
    for sid, day in ((SID2, "03"), (SID3, "04")):
        put_session(home, CWD, sid, [q("x")], summary=summ(f"2026-10-{day}T00:00:00Z"))
    assert [d["id"] for d in gh.iter_search_docs(CWD, grok_home=home, max_sessions=2)] == [SID3, SID2]


def test_search_skips_subagent_sessions(home):
    put_session(home, CWD, SID, [q("needle")], summary=summ(session_kind="subagent"))
    assert gh.search_sessions("needle", CWD, grok_home=home) == []


def test_search_validates_cwd(home):
    with pytest.raises(ValueError):
        gh.search_sessions("x", "relative", grok_home=home)


# ------------------------------------------------------------------------------------------
# edge cases pinned by the mutation round (each fails when the named guard is removed)
# ------------------------------------------------------------------------------------------

def test_inside_helper_is_strict(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    real = root.resolve()
    assert gh._inside(root / "a", real) is True
    assert gh._inside(tmp_path / "other", real) is False
    assert gh._inside(root / "missing", real) is False  # unresolvable -> refused, never "inside"
    (root / "loop").symlink_to(root / "loop")
    assert gh._inside(root / "loop", real) is False


class _FakeEntry:
    """A DirEntry stand-in that lies: every entry claims to be a plain directory."""

    def __init__(self, entry):
        self.name = entry.name

    def is_dir(self, follow_symlinks=True):
        return True


class _FakeScan:
    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _groups(home):
    root = home / "sessions"
    return gh._group_dirs(root, root.resolve(), CWD)


def test_decode_scan_containment_holds_when_the_symlink_test_is_fooled(tmp_path, home, monkeypatch):
    out = _outside_session(tmp_path)
    (home / "sessions" / "%2fscratch%2fproject").symlink_to(out.parent, target_is_directory=True)
    assert _groups(home) == []
    real_scandir = os.scandir
    monkeypatch.setattr(gh.os, "scandir", lambda p: _FakeScan([_FakeEntry(e) for e in real_scandir(p)]))
    assert _groups(home) == []          # the lstat test lies; the resolved-path check still refuses
    assert msgs(home) == []


def test_cwd_file_scan_containment_holds_when_the_symlink_test_is_fooled(tmp_path, home, monkeypatch):
    out = _outside_session(tmp_path)
    (out.parent / ".cwd").write_text(CWD, encoding="utf-8")
    (home / "sessions" / "slug-hash").symlink_to(out.parent, target_is_directory=True)
    assert _groups(home) == []
    real_scandir = os.scandir
    monkeypatch.setattr(gh.os, "scandir", lambda p: _FakeScan([_FakeEntry(e) for e in real_scandir(p)]))
    assert _groups(home) == []
    assert msgs(home) == []


def test_each_layer_alone_blocks_a_symlinked_group(tmp_path, home, monkeypatch):
    out = _outside_session(tmp_path)
    (home / "sessions" / group_name(CWD)).symlink_to(out.parent, target_is_directory=True)
    assert _groups(home) == []
    monkeypatch.setattr(gh, "_is_real_dir", lambda p: True)       # lstat layer off -> containment holds
    assert _groups(home) == []
    assert msgs(home) == [] and gh.list_sessions(CWD, grok_home=home) == []
    monkeypatch.undo()
    monkeypatch.setattr(gh, "_inside", lambda p, r: True)         # containment off -> lstat layer holds
    assert _groups(home) == []
    assert msgs(home) == [] and gh.list_sessions(CWD, grok_home=home) == []


def test_normpath_form_of_the_cwd_is_found_when_realpath_differs(tmp_path, home):
    real = tmp_path / "real"
    (real / "p").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    recorded = str(link / "p")                       # the CLI recorded the path as given (via the link)
    put_session(home, recorded, SID, [q("by normpath")], summary=summ())
    asked = str(link) + "/./p"                       # normpath -> recorded; realpath -> .../real/p (no group)
    assert [m["text"] for m in msgs(home, cwd=asked)] == ["by normpath"]


def test_oversize_cwd_file_is_ignored_even_if_it_would_match(home, monkeypatch):
    cwd = "/work/long"
    put_session(home, cwd, SID, [q("x")], group="slug-a")
    (home / "sessions" / "slug-a" / ".cwd").write_text(cwd + "\n" + " " * 200, encoding="utf-8")
    assert [m["text"] for m in msgs(home, cwd=cwd)] == ["x"]       # fits the default cap
    monkeypatch.setattr(gh, "MAX_SUMMARY_BYTES", 50)
    assert msgs(home, cwd=cwd) == []                               # over the cap: never trusted


@pytest.mark.parametrize("payload", ["[]", '"a string"', "123", "null", "true"])
def test_non_object_summary_is_ignored_not_a_crash(home, payload):
    sdir = put_session(home, CWD, SID, [q("hello there")])
    (sdir / "summary.json").write_text(payload, encoding="utf-8")
    [row] = gh.list_sessions(CWD, grok_home=home)
    assert row["name"] is None and row["preview"] == "hello there"
    sdir2 = put_session(home, CWD, SID2, [q("x")], summary=summ())
    (sdir2 / "signals.json").write_text(payload, encoding="utf-8")
    assert {r["id"]: r["message_count"] for r in gh.list_sessions(CWD, grok_home=home)}[SID2] is None


def test_summary_oversize_is_ignored(home, monkeypatch):
    put_session(home, CWD, SID, [q("body text")], summary=summ(session_summary="Big " + "t" * 300))
    monkeypatch.setattr(gh, "MAX_SUMMARY_BYTES", 100)
    [row] = gh.list_sessions(CWD, grok_home=home)
    assert row["name"] is None and row["preview"] == "body text"


def _json_of(size: int) -> bytes:
    """A valid assistant JSON row of exactly ``size`` bytes (no newline)."""
    base = len(json.dumps({"type": "assistant", "content": ""}).encode())
    return json.dumps({"type": "assistant", "content": "w" * (size - base)}).encode()


def test_line_cap_boundary(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_LINE_BYTES", 1000)
    exact = _json_of(1000) + b"\n"             # 1000 bytes of content: within the cap
    over = _json_of(1000) + b" \n"             # 1001 bytes of content, the first 1000 a complete row
    put_session(home, CWD, SID, jl(q("a")) + exact + jl(q("b")) + over + jl(q("c")))
    texts = [m["text"][:3] for m in msgs(home)]
    assert texts == ["a", "www", "b", "c"]   # `exact` kept; `over` dropped as a whole, not truncated


def test_a_valid_but_oversized_row_is_dropped(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_LINE_BYTES", 1000)
    big = jl(a("BIG " + "z" * 5000))
    put_session(home, CWD, SID, jl(q("before")) + big + jl(q("after")))
    assert [m["text"] for m in msgs(home)] == ["before", "after"]


def test_oversized_line_longer_than_two_chunks_leaks_nothing(home, monkeypatch):
    monkeypatch.setattr(gh, "MAX_LINE_BYTES", 1000)
    smuggle = b'{"type":"assistant","content":"SMUGGLED"}\n'
    put_session(home, CWD, SID, jl(q("before")) + b"x" * 2002 + smuggle + jl(q("after")))
    assert [m["text"] for m in msgs(home)] == ["before", "after"]


def test_partial_first_line_of_a_tail_window_is_dropped_even_if_its_suffix_parses(home):
    smuggle = b'{"type":"assistant","content":"SMUGGLED"}\n'
    junk = b"JUNKJUNK"                      # makes the WHOLE line invalid; only its suffix is a valid row
    head = jl(q("early"))
    raw = head + junk + smuggle + jl(q("late"))
    put_session(home, CWD, SID, raw)
    start_of_suffix = len(head) + len(junk)
    window = len(raw) - start_of_suffix - 1   # seek lands exactly on the suffix's first byte
    assert [m["text"] for m in msgs(home, max_bytes=window)] == ["late"]


def test_empty_blocks_between_real_ones_are_not_kept(home):
    row = {"type": "user", "content": [{"type": "text", "text":
           "<user_query>\none\n</user_query>\n<user_query>\n \n</user_query>\n<user_query>\ntwo\n</user_query>"}]}
    put_session(home, CWD, SID, [row])
    assert msgs(home)[0]["text"] == "one\ntwo"


def test_only_text_parts_count_as_user_text(home):
    row = {"type": "user", "content": [
        {"type": "image", "text": "<user_query>IMAGE ALT</user_query>"},
        {"type": "text", "text": 5}, {"type": "text", "text": None},
        {"type": "text", "text": "<user_query>real</user_query>"}]}
    put_session(home, CWD, SID, [row])
    assert [m["text"] for m in msgs(home)] == ["real"]


def test_assistant_parts_ignore_non_string_text(home):
    row = {"type": "assistant", "content": [{"type": "text", "text": 5}, {"type": "text", "text": None},
                                            {"type": "text", "text": "kept"}]}
    put_session(home, CWD, SID, [q("x"), row])
    assert msgs(home)[1]["text"] == "kept"


def test_long_title_is_cut_for_preview_but_kept_for_name(home):
    title = "T" * 500
    put_session(home, CWD, SID, [q("x")], summary=summ(session_summary=title))
    [row] = gh.list_sessions(CWD, grok_home=home)
    assert row["name"] == title and row["preview"] == "T" * gh.PREVIEW_CHARS


def test_non_uuid_directories_are_never_listed_even_with_content(home):
    put_session(home, CWD, SID, [q("real")], summary=summ())
    junk = home / "sessions" / group_name(CWD) / "junk-dir"
    junk.mkdir()
    (junk / "chat_history.jsonl").write_bytes(jl(q("junk")))
    (junk / "summary.json").write_text(json.dumps(summ("2026-12-01T00:00:00Z")), encoding="utf-8")
    assert [r["id"] for r in gh.list_sessions(CWD, grok_home=home)] == [SID]


def test_first_group_wins_when_a_session_id_appears_in_two_groups(tmp_path, home):
    real = tmp_path / "real-g"
    real.mkdir()
    link = tmp_path / "link-g"
    link.symlink_to(real, target_is_directory=True)
    put_session(home, str(link), SID, [q("a")], summary=summ(session_summary="from the given cwd"))
    put_session(home, str(real), SID, [q("a")], summary=summ(session_summary="from the real path"))
    [row] = gh.list_sessions(str(link), grok_home=home)
    assert row["name"] == "from the given cwd"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_group_directory_is_an_empty_listing(home):
    put_session(home, CWD, SID, [q("x")], summary=summ())
    group = home / "sessions" / group_name(CWD)
    group.chmod(0)
    try:
        assert gh.list_sessions(CWD, grok_home=home) == []
    finally:
        group.chmod(0o700)


# ------------------------------------------------------------------------------------------
# session_context (the two numbers api_project_session_history returns for a Codex thread)
# ------------------------------------------------------------------------------------------

def test_session_context_from_a_real_signals_file(home):
    install_real(home, "real_edit_bash", SID)
    assert gh.session_context(SID, CWD, grok_home=home) == {"context_tokens": 11922, "context_window": 256000}


def test_session_context_is_none_for_whatever_is_missing_or_hostile(home):
    assert gh.session_context(SID, CWD, grok_home=home) == {"context_tokens": None, "context_window": None}
    put_session(home, CWD, SID, [q("x")], summary=summ())
    assert gh.session_context(SID, CWD, grok_home=home) == {"context_tokens": None, "context_window": None}
    for bad in (True, "5", -1, 1.5, None, [1]):
        put_session(home, CWD, SID, None, signals={"contextTokensUsed": bad, "contextWindowTokens": bad})
        assert gh.session_context(SID, CWD, grok_home=home) == {"context_tokens": None, "context_window": None}, bad
    put_session(home, CWD, SID, None, signals={"contextTokensUsed": 0, "contextWindowTokens": 500000})
    assert gh.session_context(SID, CWD, grok_home=home) == {"context_tokens": 0, "context_window": 500000}


def test_session_context_validates_its_arguments_like_history(home):
    with pytest.raises(ValueError):
        gh.session_context("../x", CWD, grok_home=home)
    with pytest.raises(ValueError):
        gh.session_context(SID, "relative", grok_home=home)
