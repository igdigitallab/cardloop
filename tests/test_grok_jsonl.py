"""spec-095 P3/P4: the shared bounded readers (``grok_jsonl``), exercised directly.

``test_grok_history.py`` and ``test_grok_usage.py`` cover the same guards through their public
APIs; these pin the helper's own contract (no exception ever escapes a read of a missing,
unreadable or hostile file)."""
from __future__ import annotations

import json
import os

import pytest

import grok_jsonl as gj


def rows(path, **kw):
    kw.setdefault("max_bytes", 1 << 20)
    kw.setdefault("max_line", 1 << 16)
    return list(gj.iter_jsonl(path, **kw))


def test_missing_file_dir_and_nul_path_yield_nothing_without_raising(tmp_path):
    assert rows(tmp_path / "nope") == [] and rows(tmp_path) == []
    assert gj.open_regular(tmp_path / "nope") is None and gj.open_regular(tmp_path) is None
    assert gj.open_regular("bad\x00path") is None
    assert gj.read_small(tmp_path / "nope", 10) is None and gj.read_small("bad\x00path", 10) is None
    assert rows("bad\x00path") == []


def test_symlink_and_fifo_are_refused(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text('{"a": 1}\n')
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert gj.open_regular(link) is None and rows(link) == []
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert gj.open_regular(fifo) is None          # returns at once instead of blocking on the open
    fh = gj.open_regular(real)
    assert fh is not None
    fh.close()


def test_fstat_failure_is_an_empty_iteration_not_an_exception(tmp_path, monkeypatch):
    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\n')

    class NoFileno:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

        def fileno(self):
            raise OSError(9, "Bad file descriptor")

    real = gj.open_regular
    monkeypatch.setattr(gj, "open_regular", lambda p: NoFileno(real(p)))
    assert rows(path) == []


def test_read_small_boundaries(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b"x" * 10)
    assert gj.read_small(path, 10) == b"x" * 10      # exactly the cap: kept
    assert gj.read_small(path, 9) is None            # one byte over: refused
    assert gj.read_small(path, 100) == b"x" * 10
    path.write_bytes(b"")
    assert gj.read_small(path, 5) == b""


def test_offsets_are_absolute_byte_positions_and_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "f.jsonl"
    lines = [b'{"n": 1}\n', b"garbage\n", b"\n", b"[1]\n", b'{"n": 2}\n']
    path.write_bytes(b"".join(lines))
    got = rows(path)
    assert [(off, r["n"]) for off, r in got] == [(0, 1), (sum(map(len, lines[:4])), 2)]


def test_head_mode_reads_the_first_bytes_only(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b"".join(json.dumps({"n": i}).encode() + b"\n" for i in range(50)))
    line = len(b'{"n": 10}\n')
    head = rows(path, max_bytes=3 * line, from_head=True)
    assert [r["n"] for _o, r in head][:2] == [0, 1] and len(head) <= 4
    tail = rows(path, max_bytes=3 * line)
    assert tail[-1][1]["n"] == 49 and tail[0][1]["n"] > 40


@pytest.mark.parametrize("size", [0, 1, 7])
def test_tiny_files_and_windows(tmp_path, size):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"n": 1}\n'[:size])
    assert len(rows(path)) == (1 if size == 9 else 0)
    assert rows(path, max_bytes=1) == []
