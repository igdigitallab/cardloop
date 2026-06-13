"""
Tests for spec-033: _session_last_turn() helper in webapp.py.

Verifies:
- Two assistant turns → returns data from the SECOND (last) turn.
- Timestamp parsed correctly from ISO-8601 with trailing 'Z' → epoch ms.
- Cache hit % formula: round(cache_read / (cache_read + input_tokens) * 100).
  cache_creation_input_tokens is NOT included in the ratio (write side only).
- Fallback to file mtime when 'timestamp' field is absent or unparseable.
- No assistant turns → (0, None, None).
"""
import sys
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import _session_last_turn


# ─── helpers ──────────────────────────────────────────────────────────────────

def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant_line(
    input_tokens: int = 100,
    cache_read: int = 0,
    cache_creation: int = 0,
    timestamp: "str | None" = "2026-06-13T03:53:41.191Z",
) -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _user_line(text: str = "hi") -> dict:
    return {
        "type": "user",
        "message": {"content": text},
    }


# ─── tests ────────────────────────────────────────────────────────────────────

def test_two_turns_returns_last(tmp_path):
    """Two assistant turns: helper must return the second turn's timestamp and metrics."""
    p = tmp_path / "sess.jsonl"
    # Turn 1: 80% hit
    turn1 = _assistant_line(
        input_tokens=100, cache_read=400, cache_creation=0,
        timestamp="2026-06-13T03:00:00.000Z",
    )
    # Turn 2: 50% hit
    turn2 = _assistant_line(
        input_tokens=200, cache_read=200, cache_creation=0,
        timestamp="2026-06-13T04:00:00.000Z",
    )
    _write_jsonl(p, [_user_line("first"), turn1, _user_line("second"), turn2])

    ctx, ts_ms, hit_pct = _session_last_turn(p)

    # context_tokens = input + cache_read + cache_creation for the last turn
    assert ctx == 400, f"expected 400 context tokens, got {ctx}"
    # cache_hit_pct = round(200 / (200 + 200) * 100) = 50
    assert hit_pct == 50, f"expected 50% cache hit, got {hit_pct}"
    # timestamp: 2026-06-13T04:00:00.000Z → epoch ms (UTC)
    from datetime import datetime, timezone
    expected_ms = int(datetime(2026, 6, 13, 4, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts_ms == expected_ms, f"expected {expected_ms} ms, got {ts_ms}"


def test_timestamp_z_suffix_parsed(tmp_path):
    """ISO-8601 timestamp with trailing 'Z' → correct epoch ms."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(p, [_assistant_line(
        input_tokens=50, cache_read=50,
        timestamp="2026-01-01T00:00:00.000Z",
    )])
    _, ts_ms, _ = _session_last_turn(p)
    from datetime import datetime, timezone
    expected_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts_ms == expected_ms


def test_cache_hit_pct_excludes_cache_creation(tmp_path):
    """cache_creation_input_tokens must NOT be included in the hit ratio denominator."""
    p = tmp_path / "sess.jsonl"
    # cache_read=100, input_tokens=100, cache_creation=500
    # Correct: hit = round(100 / (100+100) * 100) = 50
    # Wrong (if creation included): round(100 / (100+100+500) * 100) = 14
    _write_jsonl(p, [_assistant_line(
        input_tokens=100, cache_read=100, cache_creation=500,
        timestamp="2026-06-13T12:00:00.000Z",
    )])
    _, _, hit_pct = _session_last_turn(p)
    assert hit_pct == 50, f"cache_creation must not be in the ratio; got {hit_pct}"


def test_mtime_fallback_when_timestamp_absent(tmp_path):
    """When 'timestamp' key is absent, fall back to file mtime."""
    p = tmp_path / "sess.jsonl"
    line = _assistant_line(input_tokens=100, cache_read=80, cache_creation=0)
    del line["timestamp"]  # remove timestamp field
    _write_jsonl(p, [line])

    before_ms = int(time.time() * 1000) - 2000  # 2s buffer
    _, ts_ms, hit_pct = _session_last_turn(p)
    after_ms = int(time.time() * 1000) + 2000

    assert ts_ms is not None, "mtime fallback must yield a non-None timestamp"
    assert before_ms <= ts_ms <= after_ms, (
        f"mtime fallback out of expected range: {ts_ms} not in [{before_ms}, {after_ms}]"
    )
    # hit_pct = round(80 / (80+100) * 100) = round(44.4) = 44
    assert hit_pct == 44


def test_mtime_fallback_when_timestamp_unparseable(tmp_path):
    """When 'timestamp' is present but unparseable, fall back to file mtime."""
    p = tmp_path / "sess.jsonl"
    line = _assistant_line(input_tokens=200, cache_read=0)
    line["timestamp"] = "not-a-date"
    _write_jsonl(p, [line])

    _, ts_ms, _ = _session_last_turn(p)
    assert ts_ms is not None, "mtime fallback must trigger on unparseable timestamp"
    # Should be close to now
    assert abs(ts_ms - int(time.time() * 1000)) < 5000


def test_no_assistant_turns(tmp_path):
    """No assistant turns → (0, None, None)."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(p, [_user_line("hello"), _user_line("world")])
    ctx, ts_ms, hit_pct = _session_last_turn(p)
    assert ctx == 0
    assert ts_ms is None
    assert hit_pct is None


def test_empty_file(tmp_path):
    """Empty JSONL → (0, None, None)."""
    p = tmp_path / "sess.jsonl"
    p.write_text("", encoding="utf-8")
    ctx, ts_ms, hit_pct = _session_last_turn(p)
    assert ctx == 0
    assert ts_ms is None
    assert hit_pct is None


def test_zero_usage(tmp_path):
    """All zeros in usage (pt==0) → hit_pct is None (no ZeroDivisionError)."""
    p = tmp_path / "sess.jsonl"
    line = _assistant_line(input_tokens=0, cache_read=0, cache_creation=0)
    _write_jsonl(p, [line])
    ctx, _, hit_pct = _session_last_turn(p)
    assert ctx == 0
    assert hit_pct is None  # no data, not 0


def test_context_tokens_includes_all_three(tmp_path):
    """context_tokens = input + cache_read + cache_creation (all three)."""
    p = tmp_path / "sess.jsonl"
    _write_jsonl(p, [_assistant_line(input_tokens=100, cache_read=200, cache_creation=50)])
    ctx, _, _ = _session_last_turn(p)
    assert ctx == 350
