"""
history_index: the chat feed is served from an index of where each message's line starts.

Every history request used to parse the whole transcript (hundreds of MB) to return 100 messages,
on every hydrate and again for every page scrolled back. The index is built once, extended from
the last complete line when the file grows, and a page parses only its own lines. These tests pin
that the index returns exactly what a full parse returns, that it really does not re-read the
file, and that it notices when the file under it is no longer the file it indexed.
"""
import json
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import history_index as hix
import webapp as _webapp


@pytest.fixture(autouse=True)
def _fresh_cache():
    hix._CACHE.clear()
    yield
    hix._CACHE.clear()


def _user(i, **extra):
    return {"type": "user", "uuid": f"u{i}", "timestamp": f"2026-10-20T10:{i // 60 % 60:02d}:{i % 60:02d}Z",
            "message": {"role": "user", "content": f"q{i}"}, **extra}


def _assistant(i, tool=False):
    content = [{"type": "text", "text": f"a{i}"}]
    if tool:
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": f"echo {i}"}})
    return {"type": "assistant", "timestamp": f"2026-10-20T11:{i // 60 % 60:02d}:{i % 60:02d}Z",
            "message": {"role": "assistant", "content": content}}


def _tool_result(i):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 200}]}}


def _write(path: Path, rows, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def _mixed(n, seed=1):
    """A transcript shaped like a real one: prompts, tool chains, tool results, harness noise."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        kind = rnd.random()
        if kind < 0.25:
            rows.append(_user(i))
        elif kind < 0.55:
            rows.append(_assistant(i, tool=rnd.random() < 0.5))
        elif kind < 0.8:
            rows.append(_tool_result(i))
        elif kind < 0.86:
            rows.append(_user(i, isMeta=True))
        elif kind < 0.9:
            rows.append({"type": "attachment", "uuid": f"s{i}", "timestamp": "2026-10-20T12:00:00Z",
                         "attachment": {"type": "queued_command", "prompt": f"steer {i}"}})
        elif kind < 0.94:
            rows.append({"type": "queue-operation", "operation": "enqueue"})
        elif kind < 0.97:
            rows.append("")
        else:
            rows.append("{not json")
    return rows


class _Counting:
    """The real line parser, counting how many lines it is asked to parse."""

    def __init__(self):
        self.calls = 0

    def __call__(self, o):
        self.calls += 1
        return _webapp._history_line_msg(o)


CASES = [dict(limit=100), dict(limit=7), dict(limit=1000), dict(limit=10, before=0),
         dict(limit=10, before=25), dict(limit=10, before=10 ** 6), dict(limit=10, since=0),
         dict(limit=10, since=40), dict(limit=10, since=10 ** 6), dict(limit=10, around_uuid="u31"),
         dict(limit=10, around_uuid="missing"), dict(limit=10, around_ts=1_792_000_000_000)]


@pytest.mark.parametrize("case", CASES)
def test_the_index_returns_exactly_what_a_full_parse_returns(tmp_path, case):
    p = tmp_path / "s.jsonl"
    _write(p, _mixed(400))
    assert _webapp._session_history(p, **case) == _webapp._session_history_scan(p, **case)


def test_a_repeat_request_parses_only_the_page_it_returns(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, _mixed(400))
    parse = _Counting()
    hix.window(p, parse, limit=10)
    assert parse.calls > 300  # the first request indexes every line
    parse.calls = 0
    page = hix.window(p, parse, limit=10, before=50)
    assert len(page) == 10 and parse.calls == 10


def test_a_grown_file_is_read_from_the_last_complete_line(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, _mixed(300))
    parse = _Counting()
    hix.window(p, parse, limit=5)
    _write(p, [_user(1000), _assistant(1000)], mode="a")
    parse.calls = 0
    tail = hix.window(p, parse, limit=5)
    assert [m["text"] for m in tail[-2:]] == ["q1000", "a1000"]
    assert parse.calls == 2 + 5  # the two new lines, then the five lines of the page
    assert tail == _webapp._session_history_scan(p, limit=5)


def test_a_line_still_being_written_is_served_once_it_parses_and_indexed_once_complete(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(1), _assistant(1)])
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user(2)))  # no newline yet
    first = _webapp._session_history(p)
    assert [m["text"] for m in first] == ["q1", "a1", "q2"] and first[-1]["idx"] == 2
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n" + json.dumps(_assistant(2)) + "\n")
    assert _webapp._session_history(p) == _webapp._session_history_scan(p)
    assert [m["idx"] for m in _webapp._session_history(p)] == [0, 1, 2, 3]


def test_a_file_replaced_by_a_shorter_one_is_indexed_again(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(i) for i in range(50)])
    _webapp._session_history(p)
    _write(p, [_user(900), _assistant(900)])  # truncated and rewritten in place
    assert [m["text"] for m in _webapp._session_history(p)] == ["q900", "a900"]


def test_a_file_rewritten_under_an_unchanged_stat_is_caught_on_read(tmp_path):
    """The stat check cannot see a rewrite that keeps size and mtime; the read-back can, because
    the line at an indexed offset no longer parses to a message."""
    p = tmp_path / "s.jsonl"
    rows = [_user(i) for i in range(20)]
    _write(p, rows)
    _webapp._session_history(p)
    st = os.stat(p)
    shifted = p.read_bytes()
    shifted = b"\n" + shifted[:-1]  # every line moves one byte; same size
    p.write_bytes(shifted)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _webapp._session_history(p) == _webapp._session_history_scan(p)


def test_a_missing_file_is_an_empty_feed(tmp_path):
    assert _webapp._session_history(tmp_path / "nope.jsonl") == []
    assert hix.count(tmp_path / "nope.jsonl", _webapp._history_line_msg) == 0
    assert hix.first_user_line(tmp_path / "nope.jsonl", _webapp._history_line_msg) is None


def test_last_of_role_and_first_user_line(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_user(0, isMeta=True), _user(1), _assistant(1), _tool_result(1), _user(2),
               _assistant(2), _tool_result(2)])
    parse = _webapp._history_line_msg
    assert hix.last_of_role(p, parse, "user")["text"] == "q2"
    assert hix.last_of_role(p, parse, "assistant")["text"] == "a2"
    assert hix.last_of_role(p, parse, "user", want=lambda m: m["text"] == "q1")["text"] == "q1"
    assert hix.first_user_line(p, parse)["uuid"] == "u1"  # the isMeta harness line is skipped
    assert hix.count(p, parse) == 4


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(hix, "_CACHE_MAX", 3)
    for i in range(5):
        p = tmp_path / f"s{i}.jsonl"
        _write(p, [_user(i)])
        _webapp._session_history(p)
    assert len(hix._CACHE) == 3 and str(tmp_path / "s4.jsonl") in hix._CACHE


def test_a_same_size_rewrite_with_a_new_mtime_is_indexed_again(tmp_path):
    """A line that was noise and is now a message, at the same length: only a re-index finds it."""
    p = tmp_path / "s.jsonl"
    msg = json.dumps(_user(7))
    bare = json.dumps({"type": "queue-operation", "operation": "enqueue", "pad": ""})
    noise = json.dumps({"type": "queue-operation", "operation": "enqueue",
                        "pad": "x" * (len(msg) - len(bare))})
    assert len(noise) == len(msg)
    _write(p, [_user(1), noise])
    assert [m["text"] for m in _webapp._session_history(p)] == ["q1"]
    st = os.stat(p)
    _write(p, [_user(1), msg])
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert [m["text"] for m in _webapp._session_history(p)] == ["q1", "q7"]


def test_scan_back_yields_complete_lines_last_first_within_its_budget(tmp_path):
    p = tmp_path / "s.jsonl"
    rows = [{"i": i, "pad": "x" * (200_000 if i == 3 else 50)} for i in range(30)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"i": "unterminated', encoding="utf-8")
    assert [o["i"] for o in hix.scan_back(p)] == list(range(29, -1, -1))
    # A budget stops early and never yields a line that starts before it.
    near = [o["i"] for o in hix.scan_back(p, max_bytes=2_000)]
    assert 5 <= len(near) < 30 and near == list(range(29, 29 - len(near), -1))
    short_of_long_line = [o["i"] for o in hix.scan_back(p, max_bytes=100_000)]
    assert short_of_long_line == list(range(29, 3, -1))
    assert list(hix.scan_back(tmp_path / "missing.jsonl")) == []
