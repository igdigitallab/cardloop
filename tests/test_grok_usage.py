"""spec-095 P4: the Grok usage/limit-error readers (``grok_usage``).

The ledger rows under test are produced by the ENGINE'S OWN writers, never hand-typed from the
spec: through a real engine turn against the fake ACP binary (``tests/fake_grok_acp.py``), and
through ``grok_engine._append_usage`` fed with the usage blocks of REAL recorded wire captures
(``tests/fixtures/grok``). Garbage/edge rows are synthetic and written next to them.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import pytest

import grok_engine
import grok_jsonl
import grok_usage as gu
from tests.test_grok_engine import Env  # the P1 harness: fake `grok` binary + isolated home

FIXTURES = Path(__file__).parent / "fixtures" / "grok"
NOW = 1_800_000_000.0
H, D = 3600.0, 86400.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    grok_engine.reset_cache()


@pytest.fixture
def data(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def row(ts: float, **kw) -> dict:
    base = {"ts": ts, "provider": "grok", "session_id": "s", "project": "p", "session_key": "k",
            "entrypoint": "chat", "model": "grok-4.7", "input": 100, "output": 10, "cached": 60,
            "reasoning": 5, "total": 110, "duration_ms": 1, "notional_usd": 0.5}
    base.update(kw)
    return base


def put(data: Path, *rows, raw: bytes = b"", name: str = gu.USAGE_FILE) -> Path:
    path = data / name
    with path.open("ab") as fh:
        for r in rows:
            fh.write(json.dumps(r).encode() + b"\n")
        fh.write(raw)
    return path


def wire_usage(fixture: str) -> dict:
    """The usage block the engine derives from a REAL recorded prompt response."""
    for line in (FIXTURES / f"{fixture}.jsonl").read_text().splitlines():
        rec = json.loads(line)
        msg = rec.get("msg") or {}
        res = msg.get("result")
        if rec.get("dir") == "a2c" and isinstance(res, dict) and res.get("stopReason"):
            return grok_engine._Mapper("SESSION_1").usage_from(res["_meta"])
    raise AssertionError(f"no prompt response in {fixture}")


def engine_write(data: Path, usage: dict, *, model="grok-4.7", project="proj", session="sid") -> None:
    grok_engine._append_usage(data, session_id=session, model=model, project_name=project,
                              session_key="p:1", entrypoint="chat", usage=usage, duration_ms=1500)


def no_cost_keys(obj, path="") -> list[str]:
    """Every dict key anywhere in ``obj`` that is named like money spent."""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "cost" in k.lower() or "spend" in k.lower() or k.lower() in ("usd", "price", "dollars"):
                bad.append(f"{path}/{k}")
            bad += no_cost_keys(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += no_cost_keys(v, f"{path}[{i}]")
    return bad


# ------------------------------------------------------------------------------------------
# parity with what the engine writes
# ------------------------------------------------------------------------------------------

async def test_rows_of_a_real_engine_turn_load_unchanged_and_aggregate(env):
    await env.run(entrypoint="card")
    env.fake("synthetic_text")
    await env.run()
    path = env.data / "grok_usage.jsonl"
    written = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(written) == 2
    assert gu.usage_rows(env.data) == written            # byte-for-byte what the engine wrote
    agg = gu.aggregate(gu.usage_rows(env.data), now=time.time())
    # the fake turn: input 2000 (cache INCLUDED), output 300, cached 800, reasoning 90, notional 123456789 ticks
    assert (agg["turns"], agg["input"], agg["output"], agg["cached"], agg["reasoning"]) == (2, 4000, 600, 1600, 180)
    assert agg["notional_usd"] == pytest.approx(2 * round(123456789 / 1e10, 6), abs=1e-6)
    assert agg["by_model"] == {"grok-4.7": {"turns": 2, "input": 4000, "output": 600}}
    assert agg["local_counters"] == {"five_hour": {"turns": 2, "tokens": 4600},
                                     "seven_day": {"turns": 2, "tokens": 4600}}


async def test_every_field_the_reader_consumes_is_a_field_the_engine_writes(env):
    await env.run()
    [written] = [json.loads(line) for line in (env.data / "grok_usage.jsonl").read_text().splitlines()]
    consumed = {"ts", "model", "input", "output", "cached", "reasoning", "notional_usd"}
    assert consumed <= set(written)
    assert gu.USAGE_FILE == "grok_usage.jsonl"
    assert (env.data / gu.USAGE_FILE).exists()


@pytest.mark.parametrize("fixture", ["simple_text", "multi_message_turn", "subagent_turn"])
def test_rows_from_real_wire_usage_blocks_aggregate_to_the_wire_numbers(data, fixture):
    usage = wire_usage(fixture)
    engine_write(data, usage)
    engine_write(data, usage, model="grok-4.7-build-fast")
    agg = gu.summary(data)
    assert agg["turns"] == 2
    assert agg["input"] == 2 * usage["input"] and agg["output"] == 2 * usage["output"]
    assert agg["cached"] == 2 * usage["cached"] and agg["reasoning"] == 2 * usage["reasoning"]
    assert agg["notional_usd"] == pytest.approx(2 * usage["notional_usd"], abs=1e-6)
    assert set(agg["by_model"]) == {"grok-4.7", "grok-4.7-build-fast"}
    assert usage["cached"] <= usage["input"]            # cached is a SUBSET of input on the wire
    assert agg["local_counters"]["five_hour"]["tokens"] == 2 * (usage["input"] + usage["output"])


async def test_a_captured_limit_error_of_a_real_failed_turn_is_surfaced(env):
    raw = {"code": -32000, "message": "Usage limit reached for SuperGrok. Try again in 3h 12m."}
    env.fake("synthetic_text", prompt_error=json.dumps(raw))
    await env.run()
    [err] = gu.limit_errors(env.data)
    assert "Usage limit reached" in err["text"] and err["source"] == "rpc_error"
    assert err["project"] == "proj" and err["model"] == "grok-4.7" and isinstance(err["ts"], float)
    assert gu.summary(env.data)["last_limit_error"] == {"ts": err["ts"], "text": err["text"]}


def test_limit_error_rows_written_by_the_engine_round_trip(data):
    grok_engine._capture_limit_error(data, source="exit", text="429 Too Many Requests: rate limit",
                                     session_id="s1", project_name="p", model="grok-4.7")
    grok_engine._capture_limit_error(data, source="rpc_error", text="plain failure: file not found",
                                     session_id="s1", project_name="p", model="grok-4.7")  # engine drops it
    [err] = gu.limit_errors(data)
    assert err == {"ts": err["ts"], "text": "429 Too Many Requests: rate limit", "source": "exit",
                   "session_id": "s1", "project": "p", "model": "grok-4.7"}


def test_text_cap_matches_the_engine():
    assert gu.LIMIT_ERROR_TEXT_CHARS == grok_engine.LIMIT_ERROR_MAX_CHARS
    assert gu.LIMIT_ERRORS_FILE == "grok_limit_errors.jsonl"


# ------------------------------------------------------------------------------------------
# the exact block the Usage tab reads
# ------------------------------------------------------------------------------------------

def test_aggregate_shape_is_exact_and_limits_are_never_reported():
    out = gu.aggregate([row(NOW - 10)], now=NOW)
    assert list(out) == ["turns", "input", "output", "cached", "reasoning", "notional_usd", "by_model",
                         "limits", "local_counters", "last_limit_error"]
    assert out["limits"] is None
    assert set(out["local_counters"]) == {"five_hour", "seven_day"}
    assert all(set(v) == {"turns", "tokens"} for v in out["local_counters"].values())
    assert all(set(v) == {"turns", "input", "output"} for v in out["by_model"].values())
    assert out["last_limit_error"] is None
    json.dumps(out)  # serialisable as is


def test_empty_aggregate_is_the_zero_shape():
    out = gu.aggregate([], now=NOW)
    assert out == {"turns": 0, "input": 0, "output": 0, "cached": 0, "reasoning": 0, "notional_usd": None,
                   "by_model": {}, "limits": None,
                   "local_counters": {"five_hour": {"turns": 0, "tokens": 0}, "seven_day": {"turns": 0, "tokens": 0}},
                   "last_limit_error": None}


def test_notional_is_api_equivalent_and_never_named_cost_or_spend():
    out = gu.aggregate([row(NOW, notional_usd=0.1234567), row(NOW, notional_usd=0.2)], now=NOW)
    assert out["notional_usd"] == pytest.approx(0.323457, abs=1e-9)   # rounded to micro-dollars
    assert no_cost_keys(out) == []
    assert no_cost_keys(gu.summary(Path("/nonexistent"))) == []


def test_notional_is_none_when_no_row_is_priced_and_ignores_bad_prices():
    bad = [row(NOW, notional_usd=v) for v in (None, "0.5", -1.0, float("nan"), float("inf"), True, [], {})]
    assert gu.aggregate(bad, now=NOW)["notional_usd"] is None
    assert gu.aggregate(bad + [row(NOW, notional_usd=0.0)], now=NOW)["notional_usd"] == 0.0
    assert gu.aggregate(bad + [row(NOW, notional_usd=0.25)], now=NOW)["notional_usd"] == 0.25
    assert gu.aggregate([{"ts": NOW}], now=NOW)["notional_usd"] is None  # a row without the field


def test_cached_is_a_subset_of_input_and_never_added_on_top():
    out = gu.aggregate([row(NOW, input=100, cached=60, output=10)], now=NOW)
    assert (out["input"], out["cached"], out["output"]) == (100, 60, 10)
    assert out["local_counters"]["five_hour"]["tokens"] == 110          # input + output, not + cached
    assert out["by_model"]["grok-4.7"] == {"turns": 1, "input": 100, "output": 10}


# ------------------------------------------------------------------------------------------
# windows: 5 h / 7 d and the days filter
# ------------------------------------------------------------------------------------------

def test_five_hour_window_edges():
    rows = [row(NOW - 5 * H, input=1, output=0),            # exactly 5 h old: still counts
            row(NOW - 5 * H - 0.001, input=10, output=0),   # a hair older: out of the 5 h window
            row(NOW, input=100, output=0),
            row(NOW + 60, input=1000, output=0)]            # clock step into the future: counts
    out = gu.aggregate(rows, now=NOW)
    assert out["local_counters"]["five_hour"] == {"turns": 3, "tokens": 1101}
    assert out["local_counters"]["seven_day"] == {"turns": 4, "tokens": 1111}


def test_seven_day_window_edges():
    rows = [row(NOW - 7 * D, input=1, output=0), row(NOW - 7 * D - 0.001, input=10, output=0),
            row(NOW - 6 * D, input=100, output=0), row(NOW - 30 * D, input=1000, output=0)]
    out = gu.aggregate(rows, now=NOW)
    assert out["local_counters"]["seven_day"] == {"turns": 2, "tokens": 101}
    assert out["local_counters"]["five_hour"] == {"turns": 0, "tokens": 0}
    assert out["turns"] == 4  # the totals are not windowed unless days= is given


def test_days_narrows_totals_but_never_the_counters():
    rows = [row(NOW - 1 * H, input=1, output=0, model="a"), row(NOW - 3 * D, input=10, output=0, model="b"),
            row(NOW - 20 * D, input=100, output=0, model="c")]
    out = gu.aggregate(rows, days=1, now=NOW)
    assert (out["turns"], out["input"]) == (1, 1) and list(out["by_model"]) == ["a"]
    assert out["local_counters"]["seven_day"] == {"turns": 2, "tokens": 11}   # 3-day-old row still counted
    assert gu.aggregate(rows, days=7, now=NOW)["turns"] == 2
    assert gu.aggregate(rows, days=30, now=NOW)["turns"] == 3
    assert gu.aggregate(rows, days=None, now=NOW)["turns"] == 3


def test_days_edge_is_inclusive_like_codex():
    rows = [row(NOW - 2 * D, input=1), row(NOW - 2 * D - 0.001, input=10)]
    assert gu.aggregate(rows, days=2, now=NOW)["input"] == 1
    assert gu.usage_rows(Path("/nonexistent"), days=2, now=NOW) == []


@pytest.mark.parametrize("days", [None, 0, -3, "7", "x", True, False, float("nan"), float("inf"), [], {}])
def test_invalid_days_mean_all_rows(days):
    rows = [row(NOW - 400 * D, input=1), row(NOW, input=10)]
    assert gu.aggregate(rows, days=days, now=NOW)["turns"] == 2


def test_default_now_is_the_wall_clock():
    out = gu.aggregate([row(time.time() - 60), row(time.time() - 6 * H)])
    assert out["local_counters"]["five_hour"]["turns"] == 1 and out["local_counters"]["seven_day"]["turns"] == 2


def test_usage_rows_days_filter_on_the_file(data):
    put(data, row(NOW - 3 * D, session_id="old"), row(NOW - 2 * D, session_id="edge"),
        row(NOW - 2 * D + 1, session_id="in"), row(NOW - 10, session_id="new"))
    got = gu.usage_rows(data, days=2, now=NOW)
    assert [r["session_id"] for r in got] == ["edge", "in", "new"]
    assert [r["session_id"] for r in gu.usage_rows(data, now=NOW)] == ["old", "edge", "in", "new"]
    assert [r["session_id"] for r in gu.usage_rows(data, days=0, now=NOW)] == ["old", "edge", "in", "new"]


def test_usage_rows_default_clock_is_the_wall_clock(data):
    put(data, row(time.time() - 3 * D, session_id="old"), row(time.time() - 10, session_id="new"))
    assert [r["session_id"] for r in gu.usage_rows(data, days=1)] == ["new"]
    out = gu.summary(data, days=1)
    assert out["turns"] == 1 and out["local_counters"]["seven_day"]["turns"] == 2


def test_summary_reads_a_week_even_for_a_one_day_window(data):
    put(data, row(NOW - 3 * D, input=10, output=0), row(NOW - 2 * H, input=1, output=0),
        row(NOW - 40 * D, input=1000, output=0))
    out = gu.summary(data, days=1, now=NOW)
    assert out["turns"] == 1 and out["input"] == 1                              # totals: the last day only
    assert out["local_counters"]["seven_day"] == {"turns": 2, "tokens": 11}     # counters: a real 7 d
    assert gu.summary(data, days=30, now=NOW)["turns"] == 2
    assert gu.summary(data, days=None, now=NOW)["turns"] == 3
    assert gu.summary(data, days=10, now=NOW)["turns"] == 2


def test_summary_carries_the_newest_limit_error_and_none_without(data):
    put(data, row(NOW - 5))
    assert gu.summary(data, now=NOW)["last_limit_error"] is None
    put(data, {"ts": NOW - 100, "text": "old quota error"}, {"ts": NOW - 10, "text": "new quota error"},
        name=gu.LIMIT_ERRORS_FILE)
    assert gu.summary(data, now=NOW)["last_limit_error"] == {"ts": NOW - 10, "text": "new quota error"}


# ------------------------------------------------------------------------------------------
# by_model
# ------------------------------------------------------------------------------------------

def test_by_model_keys_and_order():
    rows = [row(NOW, model="b"), row(NOW, model="a"), row(NOW, model="a"), row(NOW, model="c"),
            row(NOW, model="c"), row(NOW, model="c")]
    assert list(gu.aggregate(rows, now=NOW)["by_model"]) == ["c", "a", "b"]     # turns desc, then name
    ties = [row(NOW, model="zeta"), row(NOW, model="alpha"), row(NOW, model="mid")]
    assert list(gu.aggregate(ties, now=NOW)["by_model"]) == ["alpha", "mid", "zeta"]   # equal turns: by name


def test_missing_or_hostile_model_names():
    long = "m" * 300
    rows = [row(NOW, model=None), row(NOW, model=""), row(NOW, model="   "), row(NOW, model=5),
            row(NOW, model=["x"]), {k: v for k, v in row(NOW).items() if k != "model"},
            row(NOW, model="  padded  "), row(NOW, model=long)]
    by = gu.aggregate(rows, now=NOW)["by_model"]
    assert by["unknown"]["turns"] == 6 and by["padded"]["turns"] == 1
    assert by["m" * gu.MODEL_NAME_CHARS]["turns"] == 1 and len(max(by, key=len)) == gu.MODEL_NAME_CHARS


def test_distinct_models_are_capped_and_folded_into_other(monkeypatch):
    monkeypatch.setattr(gu, "MAX_MODELS", 3)
    rows = [row(NOW, model=f"m{i}", input=1, output=1) for i in range(6)] + [row(NOW, model="m0", input=1, output=1)]
    by = gu.aggregate(rows, now=NOW)["by_model"]
    assert set(by) == {"m0", "m1", "m2", "other"}
    assert by["m0"]["turns"] == 2                      # a model already tracked keeps counting past the cap
    assert by["other"] == {"turns": 3, "input": 3, "output": 3}
    assert gu.aggregate(rows, now=NOW)["turns"] == 7   # folding never loses a turn from the totals


# ------------------------------------------------------------------------------------------
# hostile values / hostile input shapes
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "5", "x", -5, -0.5, float("nan"), float("inf"), float("-inf"), True, False,
                                 [], {}, [1]])
def test_hostile_token_values_count_as_zero(bad):
    out = gu.aggregate([row(NOW, input=bad, output=bad, cached=bad, reasoning=bad)], now=NOW)
    assert (out["input"], out["output"], out["cached"], out["reasoning"]) == (0, 0, 0, 0)
    assert out["turns"] == 1                                    # the turn itself still happened
    assert out["local_counters"]["five_hour"] == {"turns": 1, "tokens": 0}


def test_float_counts_are_floored_and_huge_ints_pass():
    out = gu.aggregate([row(NOW, input=12.9, output=7.99), row(NOW, input=10 ** 30, output=0)], now=NOW)
    assert out["input"] == 12 + 10 ** 30 and out["output"] == 7


def test_rows_without_a_usable_timestamp_are_counted_nowhere():
    bad_ts = [None, "1800000000", float("nan"), float("inf"), -1, True, [], {}]
    rows = [row(v) for v in bad_ts] + [{k: v for k, v in row(NOW).items() if k != "ts"}]
    out = gu.aggregate(rows, now=NOW)
    assert out["turns"] == 0 and out["local_counters"]["seven_day"]["turns"] == 0
    assert gu.aggregate([row(0)], now=NOW)["turns"] == 1        # the epoch is a time, just an old one


@pytest.mark.parametrize("rows", [None, 5, "text", object()])
def test_non_iterable_rows_are_an_empty_aggregate(rows):
    assert gu.aggregate(rows, now=NOW)["turns"] == 0


def test_rows_may_be_any_iterable_and_non_dict_entries_are_skipped():
    gen = (r for r in [row(NOW), "junk", None, 7, [1], row(NOW)])
    assert gu.aggregate(gen, now=NOW)["turns"] == 2
    assert gu.aggregate((row(NOW),), now=NOW)["turns"] == 1


# ------------------------------------------------------------------------------------------
# last_limit_error argument
# ------------------------------------------------------------------------------------------

def test_last_limit_error_is_sanitised():
    ok = gu.aggregate([], now=NOW, last_limit_error={"ts": 5, "text": "quota hit", "source": "exit"})
    assert ok["last_limit_error"] == {"ts": 5.0, "text": "quota hit"}
    assert isinstance(ok["last_limit_error"]["ts"], float)
    for bad in (None, 5, "x", [], {}, {"ts": 5}, {"text": "t"}, {"ts": "5", "text": "t"}, {"ts": 5, "text": ""},
                {"ts": 5, "text": "   "}, {"ts": 5, "text": 7}, {"ts": float("nan"), "text": "t"},
                {"ts": -1, "text": "t"}, {"ts": True, "text": "t"}):
        assert gu.aggregate([], now=NOW, last_limit_error=bad)["last_limit_error"] is None, bad
    long = gu.aggregate([], now=NOW, last_limit_error={"ts": 1, "text": "q" * 20000})["last_limit_error"]
    assert len(long["text"]) == gu.LIMIT_ERROR_TEXT_CHARS


# ------------------------------------------------------------------------------------------
# reading the ledger: missing / garbage / hostile files
# ------------------------------------------------------------------------------------------

def test_missing_everything_is_empty_never_an_error(tmp_path):
    assert gu.usage_rows(tmp_path) == [] and gu.usage_rows(tmp_path / "nope") == []
    assert gu.usage_rows(None) == [] and gu.limit_errors(None) == []
    assert gu.limit_errors(tmp_path) == [] and gu.limit_errors(tmp_path / "nope") == []
    assert gu.summary(tmp_path / "nope", days=3, now=NOW)["turns"] == 0
    assert gu.summary(None)["turns"] == 0


def test_garbage_lines_are_skipped_good_rows_survive(data):
    raw = (b"not json at all\n" + b"\n" + b"   \n" + b"[1,2,3]\n" + b'"string"\n' + b"42\n" + b"null\n" + b"true\n"
           + b"\xff\xfe\x00\x01 binary \xc3\x28\n"
           + json.dumps(row(NOW - 5, session_id="good-1")).encode() + b"\n"
           + b'{"ts": "yesterday", "input": 5}\n' + b'{"input": 5}\n' + b'{"ts": NaN, "input": 5}\n'
           + b'{"ts": -7, "input": 5}\n' + b'{"ts": true}\n'
           + json.dumps(row(NOW - 4, session_id="good-2")).encode() + b"\n"
           + b'{"ts": 1800000000.5, "provider": "grok", "input": 12')      # half-written final line
    (data / gu.USAGE_FILE).write_bytes(raw)
    assert [r["session_id"] for r in gu.usage_rows(data)] == ["good-1", "good-2"]
    assert gu.summary(data, now=NOW)["turns"] == 2


def test_unknown_extra_fields_are_preserved_in_rows(data):
    put(data, row(NOW, future_field={"a": [1, 2]}))
    assert gu.usage_rows(data)[0]["future_field"] == {"a": [1, 2]}


def test_usage_file_that_is_a_directory_or_symlink_is_empty(tmp_path, data):
    (data / gu.USAGE_FILE).mkdir()
    assert gu.usage_rows(data) == [] and gu.summary(data)["turns"] == 0
    (data / gu.USAGE_FILE).rmdir()
    real = tmp_path / "elsewhere.jsonl"
    real.write_text(json.dumps(row(NOW)) + "\n")
    (data / gu.USAGE_FILE).symlink_to(real)
    assert gu.usage_rows(data) == []                             # a symlinked ledger is not followed
    (data / gu.LIMIT_ERRORS_FILE).symlink_to(real)
    assert gu.limit_errors(data) == []


def test_fifo_ledger_does_not_block(data):
    os.mkfifo(data / gu.USAGE_FILE)
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(v=gu.usage_rows(data)), daemon=True)
    t.start()
    t.join(5)
    stuck = t.is_alive()
    if stuck:
        os.close(os.open(data / gu.USAGE_FILE, os.O_WRONLY | os.O_NONBLOCK))
        t.join(2)
    assert not stuck and result["v"] == []


def test_data_dir_may_be_a_str_or_a_path(data):
    put(data, row(NOW, session_id="x"))
    assert gu.usage_rows(str(data))[0]["session_id"] == "x" and gu.usage_rows(data)[0]["session_id"] == "x"


def test_data_dir_that_is_a_file_is_empty(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    assert gu.usage_rows(f) == [] and gu.limit_errors(f) == []


# ------------------------------------------------------------------------------------------
# bounded reads
# ------------------------------------------------------------------------------------------

class _CountingFile:
    """Wraps the opened ledger and counts the bytes the reader pulls from it."""

    def __init__(self, fh):
        self._fh = fh
        self.bytes_read = 0

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def readline(self, *a):
        line = self._fh.readline(*a)
        self.bytes_read += len(line)
        return line

    def read(self, *a):
        data = self._fh.read(*a)
        self.bytes_read += len(data)
        return data


def test_a_huge_ledger_is_read_from_its_tail_only(data, monkeypatch):
    rows = [row(NOW - 100_000 + i, session_id=f"s{i:05d}") for i in range(4000)]
    path = put(data, *rows)
    size = path.stat().st_size
    monkeypatch.setattr(gu, "MAX_USAGE_BYTES", 30_000)
    opened: list[_CountingFile] = []
    real_open = grok_jsonl.open_regular

    def spy(p):
        fh = real_open(p)
        if fh is None:
            return None
        opened.append(_CountingFile(fh))
        return opened[-1]

    monkeypatch.setattr(grok_jsonl, "open_regular", spy)
    got = gu.usage_rows(data)
    assert size > 300_000 and 0 < len(got) < 300
    assert got[-1]["session_id"] == "s03999" and got[0]["session_id"] != "s00000"   # the NEWEST rows
    ids = [r["session_id"] for r in got]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)                         # whole rows, in order
    assert sum(f.bytes_read for f in opened) <= 30_000 + 1_000                      # never slurped
    assert gu.summary(data, now=NOW)["turns"] == len(got)


def test_tail_window_lands_on_whole_rows_when_the_cut_is_mid_line(data, monkeypatch):
    path = put(data, *[row(NOW - i, session_id=f"r{i}") for i in range(100)])
    line = len(json.dumps(row(NOW, session_id="r99")).encode()) + 1
    monkeypatch.setattr(gu, "MAX_USAGE_BYTES", 5 * line + 17)       # cut lands inside a row
    got = gu.usage_rows(data)
    assert len(got) == 5 and got[-1]["session_id"] == "r99"
    assert path.stat().st_size > 5 * line


def test_giant_line_is_skipped_and_neighbours_survive(data, monkeypatch):
    monkeypatch.setattr(gu, "MAX_USAGE_LINE_BYTES", 2000)
    put(data, row(NOW, session_id="before"), raw=b'{"ts": 1, "pad": "' + b"x" * 10_000 + b'"}\n')
    put(data, row(NOW, session_id="valid-but-huge", model="m" * 3000), row(NOW, session_id="after"))
    assert [r["session_id"] for r in gu.usage_rows(data)] == ["before", "after"]


def test_limit_error_ledger_is_tail_bounded_too(data, monkeypatch):
    rows = [{"ts": NOW - 5000 + i, "text": f"quota error {i:04d} " + "z" * 100} for i in range(500)]
    put(data, *rows, name=gu.LIMIT_ERRORS_FILE)
    monkeypatch.setattr(gu, "MAX_LIMIT_ERROR_BYTES", 6_000)
    got = gu.limit_errors(data, limit=200)
    assert 0 < len(got) < 60 and got[0]["text"].startswith("quota error 0499")
    assert all(r["text"].startswith("quota error 0") for r in got)


# ------------------------------------------------------------------------------------------
# limit_errors
# ------------------------------------------------------------------------------------------

def test_limit_errors_are_newest_first_and_limited(data):
    put(data, *[{"ts": NOW - 100 + i, "text": f"err {i}", "source": "exit", "session_id": f"s{i}",
                 "project": "p", "model": "grok-4.7"} for i in range(30)], name=gu.LIMIT_ERRORS_FILE)
    got = gu.limit_errors(data)
    assert [r["text"] for r in got][:3] == ["err 29", "err 28", "err 27"] and len(got) == 20   # default limit 20
    assert [r["text"] for r in gu.limit_errors(data, limit=2)] == ["err 29", "err 28"]
    assert set(got[0]) == {"ts", "text", "source", "session_id", "project", "model"}


@pytest.mark.parametrize("limit,expected", [(0, 1), (-5, 1), (1, 1), (7, 7), (10 ** 9, 30), ("x", 20), (True, 20),
                                            (None, 20), (2.5, 20)])
def test_limit_errors_limit_clamping(data, monkeypatch, limit, expected):
    monkeypatch.setattr(gu, "MAX_LIMIT_ERRORS", 30)
    put(data, *[{"ts": NOW + i, "text": f"e{i}"} for i in range(40)], name=gu.LIMIT_ERRORS_FILE)
    assert len(gu.limit_errors(data, limit=limit)) == expected


def test_limit_errors_order_follows_timestamps_not_file_order_and_ties_prefer_later_lines(data):
    put(data, {"ts": 300, "text": "late stamp, early line"}, {"ts": 100, "text": "oldest"},
        {"ts": 200, "text": "tie A"}, {"ts": 200, "text": "tie B"}, name=gu.LIMIT_ERRORS_FILE)
    assert [r["text"] for r in gu.limit_errors(data)] == ["late stamp, early line", "tie B", "tie A", "oldest"]


def test_limit_errors_skip_unusable_rows_and_normalise_fields(data):
    put(data, {"ts": 1, "text": "ok", "source": 5, "session_id": ["x"], "project": 12345, "model": {"a": 1}},
        {"ts": 2}, {"text": "no ts"}, {"ts": "3", "text": "string ts"}, {"ts": 4, "text": ""},
        {"ts": 5, "text": 12345}, {"ts": float("nan"), "text": "nan ts"}, {"ts": True, "text": "bool ts"},
        raw=b"garbage\n[1]\n", name=gu.LIMIT_ERRORS_FILE)
    [only] = gu.limit_errors(data)
    assert only == {"ts": 1.0, "text": "ok", "source": None, "session_id": None, "project": None, "model": None}


def test_limit_error_text_is_capped(data, monkeypatch):
    put(data, {"ts": 1, "text": "q" * 20000}, name=gu.LIMIT_ERRORS_FILE)
    assert len(gu.limit_errors(data)[0]["text"]) == gu.LIMIT_ERROR_TEXT_CHARS


def test_limit_errors_survive_a_garbage_file(data):
    (data / gu.LIMIT_ERRORS_FILE).write_bytes(b"\x00\xff\xfe junk\n{broken\n"
                                              + json.dumps({"ts": 9, "text": "real"}).encode() + b"\n{half")
    assert [r["text"] for r in gu.limit_errors(data)] == ["real"]


def test_summary_equals_aggregate_of_usage_rows(data):
    put(data, row(NOW - 1 * H, model="a"), row(NOW - 2 * D, model="b", input=7))
    put(data, {"ts": NOW - 1, "text": "quota"}, name=gu.LIMIT_ERRORS_FILE)
    want = gu.aggregate(gu.usage_rows(data, now=NOW), now=NOW, last_limit_error=gu.limit_errors(data)[0])
    assert gu.summary(data, now=NOW) == want and math.isfinite(want["notional_usd"])


# ------------------------------------------------------------------------------------------
# found by the mutation round
# ------------------------------------------------------------------------------------------

PAST = 1_000_000_000.0   # an injected clock far from the wall clock: any code that reads time.time() shows


def test_an_injected_clock_reaches_every_layer_of_summary(data):
    put(data, row(PAST - 3 * D, input=10, output=0), row(PAST - 2 * H, input=1, output=0))
    out = gu.summary(data, days=1, now=PAST)
    assert out["turns"] == 1 and out["input"] == 1
    assert out["local_counters"]["seven_day"] == {"turns": 2, "tokens": 11}
    assert out["local_counters"]["five_hour"] == {"turns": 1, "tokens": 1}
    assert gu.summary(data, days=None, now=PAST)["turns"] == 2


def test_summary_parses_only_the_window_it_needs(data, monkeypatch):
    put(data, row(NOW - 1 * H))
    seen = []
    real = gu.usage_rows
    monkeypatch.setattr(gu, "usage_rows", lambda d, **kw: (seen.append(kw.get("days")), real(d, **kw))[1])
    gu.summary(data, days=1, now=NOW)
    gu.summary(data, days=30, now=NOW)
    gu.summary(data, days=None, now=NOW)
    gu.summary(data, days=0.25, now=NOW)
    assert seen == [7, 30, None, 7]     # memory stays bounded: never the whole ledger for a short window


class _FailingFile:
    """An opened ledger whose reads die with an I/O error after ``ok_lines`` lines."""

    def __init__(self, fh, ok_lines):
        self._fh, self._left = fh, ok_lines

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def readline(self, *a):
        if self._left <= 0:
            raise OSError(5, "Input/output error")
        self._left -= 1
        return self._fh.readline(*a)


def test_an_io_error_mid_read_keeps_the_rows_already_parsed(data, monkeypatch):
    put(data, *[row(NOW - i, session_id=f"u{i}") for i in range(5)])
    put(data, *[{"ts": NOW - i, "text": f"e{i}"} for i in range(5)], name=gu.LIMIT_ERRORS_FILE)
    real = grok_jsonl.open_regular
    monkeypatch.setattr(grok_jsonl, "open_regular", lambda p: _FailingFile(real(p), 3) if real(p) else None)
    assert [r["session_id"] for r in gu.usage_rows(data)] == ["u0", "u1", "u2"]
    assert len(gu.limit_errors(data, limit=50)) == 3
    assert gu.summary(data, now=NOW)["turns"] == 3


def test_fstat_failure_is_an_empty_read(data, monkeypatch):
    put(data, row(NOW))

    class NoStat:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

        def fileno(self):
            raise OSError(9, "Bad file descriptor")

    real = grok_jsonl.open_regular
    monkeypatch.setattr(grok_jsonl, "open_regular", lambda p: NoStat(real(p)))
    assert gu.usage_rows(data) == []


@pytest.mark.parametrize("bad_dir", ["bad\x00dir", "/tmp/x\x00"])
def test_a_data_dir_with_a_nul_byte_is_empty_not_an_error(bad_dir):
    assert gu.usage_rows(bad_dir) == [] and gu.limit_errors(bad_dir) == []
    assert gu.summary(bad_dir)["turns"] == 0


def test_limit_error_giant_line_is_skipped(data, monkeypatch):
    monkeypatch.setattr(gu, "MAX_LIMIT_ERROR_LINE_BYTES", 500)
    put(data, {"ts": 1, "text": "small before"}, {"ts": 2, "text": "X" * 3000}, {"ts": 3, "text": "small after"},
        name=gu.LIMIT_ERRORS_FILE)
    assert [r["text"] for r in gu.limit_errors(data)] == ["small after", "small before"]
