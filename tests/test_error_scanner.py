"""
Tests for the incident scanner: log/pytest parsers, deduplication, and board ingest.

CRITICAL: regression = either lost errors (not landing in Failed),
or a flood of duplicates (one traceback → 1000 cards overnight).
"""
import pytest

from webapp import (
    _parse_log_errors,
    _parse_pytest_failures,
    _hash6,
    _norm_msg,
    _parse_incident_desc,
    _format_incident_desc,
    _is_incident_card,
    _incident_title,
)


# ─────────────────────────── log parser: Python tracebacks ───────────────────────────

PYTHON_TRACEBACK = """
2026-05-31 INFO Server starting
Traceback (most recent call last):
  File "/app/main.py", line 42, in handler
    result = compute(x)
  File "/app/lib.py", line 17, in compute
    return 1 / x
ZeroDivisionError: division by zero
2026-05-31 INFO Recovered
"""

def test_parse_python_traceback():
    errors = _parse_log_errors(PYTHON_TRACEBACK)
    assert len(errors) == 1
    assert errors[0]["type"] == "ZeroDivisionError"
    assert errors[0]["message"] == "division by zero"
    assert errors[0]["source"] == "log"
    assert errors[0]["hash"]
    assert len(errors[0]["hash"]) == 6


def test_parse_python_traceback_excerpt_has_exception():
    errors = _parse_log_errors(PYTHON_TRACEBACK)
    assert "ZeroDivisionError: division by zero" in errors[0]["excerpt"]


def test_parse_multiple_distinct_tracebacks():
    log = PYTHON_TRACEBACK + """
Traceback (most recent call last):
  File "/x.py", line 1, in <module>
    open("/nope")
FileNotFoundError: [Errno 2] No such file or directory: '/nope'
"""
    errors = _parse_log_errors(log)
    assert len(errors) == 2
    types = {e["type"] for e in errors}
    assert types == {"ZeroDivisionError", "FileNotFoundError"}


def test_dedup_identical_traceback_in_same_log():
    """The same traceback appearing twice in a log → single entry (dedup by hash within one run)."""
    log = PYTHON_TRACEBACK + "\n" + PYTHON_TRACEBACK
    errors = _parse_log_errors(log)
    assert len(errors) == 1, "Duplicate traceback should be collapsed into one entry"


# ─────────────────────────── log parser: Generic ERROR/CRITICAL ───────────────────────────

def test_parse_generic_error():
    log = "2026-05-31 12:00 ERROR: database connection lost"
    errors = _parse_log_errors(log)
    assert len(errors) == 1
    assert errors[0]["type"] == "ERROR"
    assert "database connection lost" in errors[0]["message"]


def test_parse_critical():
    log = "[2026-05-31] CRITICAL disk full /var/log"
    errors = _parse_log_errors(log)
    assert len(errors) == 1
    assert errors[0]["type"] == "CRITICAL"


def test_skip_deprecation_warnings():
    """DeprecationWarning should not appear in incidents — it is noise."""
    log = "ERROR: DeprecationWarning: foo() is deprecated, use bar()"
    errors = _parse_log_errors(log)
    assert len(errors) == 0, "Deprecation warnings should be filtered out"


def test_skip_health_check_noise():
    log = "INFO: GET /api/health 200 OK"
    errors = _parse_log_errors(log)
    assert len(errors) == 0


def test_traceback_not_double_counted_with_generic():
    """If an ERROR line contains Traceback it should not be double-counted (traceback parser takes priority)."""
    log = """
ERROR: Traceback (most recent call last):
  File "x.py", line 1, in <module>
    1/0
ZeroDivisionError: division by zero
"""
    errors = _parse_log_errors(log)
    # One entry from the python-traceback parser; the ERROR line containing "Traceback" should be skipped
    assert len(errors) == 1
    assert errors[0]["type"] == "ZeroDivisionError"


# ─────────────────────────── pytest parser ───────────────────────────

PYTEST_OUTPUT = """
============================= test session starts =============================
collected 5 items

tests/test_foo.py::test_one PASSED                                       [ 20%]
tests/test_foo.py::test_two FAILED                                       [ 40%]
tests/test_bar.py::test_three FAILED                                     [ 60%]

============================= short test summary info ==========================
FAILED tests/test_foo.py::test_two - AssertionError: 1 != 2
FAILED tests/test_bar.py::test_three - KeyError: 'missing'
====================== 2 failed, 3 passed in 0.42s ============================
"""

def test_parse_pytest_failures():
    fails = _parse_pytest_failures(PYTEST_OUTPUT)
    assert len(fails) == 2
    tests = {f["test"] for f in fails}
    assert tests == {"test_two", "test_three"}


def test_parse_pytest_failure_includes_reason():
    fails = _parse_pytest_failures(PYTEST_OUTPUT)
    msgs = [f["message"] for f in fails]
    assert any("AssertionError" in m for m in msgs)
    assert any("KeyError" in m for m in msgs)


def test_pytest_failures_have_source_test():
    fails = _parse_pytest_failures(PYTEST_OUTPUT)
    for f in fails:
        assert f["source"] == "test"
        assert f["type"] == "FAILED"


def test_pytest_dedup_same_failure():
    """The same FAILED entry appearing twice → single entry."""
    out = PYTEST_OUTPUT + "\n" + PYTEST_OUTPUT
    fails = _parse_pytest_failures(out)
    assert len(fails) == 2  # not 4


def test_pytest_failure_with_no_reason():
    """FAILED with no reason — should still appear in the list."""
    out = "FAILED tests/test_x.py::test_y"
    fails = _parse_pytest_failures(out)
    assert len(fails) == 1
    assert fails[0]["test"] == "test_y"


def test_pytest_parametrized_test_name():
    """Parametrized tests test_x[case-1] — name contains brackets."""
    out = "FAILED tests/test_x.py::test_param[case-1] - AssertionError"
    fails = _parse_pytest_failures(out)
    assert len(fails) == 1
    assert "test_param" in fails[0]["test"]


# ─────────────────────────── _norm_msg / _hash6 ───────────────────────────

def test_norm_msg_strips_numbers():
    """Numbers (PID/timestamp) should be normalised → duplicates with different numbers share one hash."""
    h1 = _hash6(_norm_msg("connection lost at 1234567890"))
    h2 = _hash6(_norm_msg("connection lost at 9876543210"))
    assert h1 == h2, "Hashes should match after number normalisation"


def test_norm_msg_strips_paths():
    """Paths in the message are normalised — the same bug in different files collapses to one entry."""
    h1 = _hash6(_norm_msg("error in /tmp/foo/bar.py"))
    h2 = _hash6(_norm_msg("error in /var/log/baz.py"))
    assert h1 == h2


def test_norm_msg_preserves_exception_type_difference():
    """Different exception types produce different hashes."""
    h1 = _hash6(_norm_msg("KeyError: missing key"))
    h2 = _hash6(_norm_msg("ValueError: missing key"))
    assert h1 != h2


def test_hash6_length():
    assert len(_hash6("anything")) == 6
    assert all(c in "0123456789abcdef" for c in _hash6("x"))


# ─────────────────────────── incident description format ───────────────────────────

def test_format_and_parse_desc_round_trip():
    """format → parse returns the original keys."""
    meta = {
        "source": "log",
        "seen": "3",
        "first": "2026-05-31T10:00",
        "last": "2026-05-31T15:30",
        "excerpt": "ZeroDivisionError: division by zero",
    }
    desc = _format_incident_desc(meta)
    parsed = _parse_incident_desc(desc)
    assert parsed["source"] == "log"
    assert parsed["seen"] == "3"
    assert parsed["first"] == "2026-05-31T10:00"
    assert parsed["last"] == "2026-05-31T15:30"
    assert "ZeroDivisionError" in parsed["excerpt"]


def test_format_desc_keeps_excerpt_compact():
    """Multi-line excerpt → single line with separator (description in '  > line' format)."""
    meta = {"source": "log", "seen": "1", "excerpt": "line1\nline2\nline3"}
    desc = _format_incident_desc(meta)
    # Excerpt must become a single line in the file
    excerpt_lines = [ln for ln in desc.splitlines() if ln.startswith("excerpt=")]
    assert len(excerpt_lines) == 1
    assert "line1" in excerpt_lines[0]
    assert "line2" in excerpt_lines[0]


def test_parse_empty_desc():
    assert _parse_incident_desc(None) == {}
    assert _parse_incident_desc("") == {}


def test_parse_desc_ignores_unknown_lines():
    """Unknown lines in description (e.g. the agent appended something) are ignored."""
    desc = "source=log\nseen=2\nrandom note from human\nfirst=2026-05-31"
    parsed = _parse_incident_desc(desc)
    assert parsed == {"source": "log", "seen": "2", "first": "2026-05-31"}


# ─────────────────────────── card classification ───────────────────────────

def test_is_incident_card_true():
    assert _is_incident_card({"id": "err-abc123", "text": "[ERR] Foo"})


def test_is_incident_card_false():
    assert not _is_incident_card({"id": "abc123", "text": "regular task"})
    assert not _is_incident_card({"id": "", "text": "no id"})


def test_incident_title_python_error():
    err = {"source": "log", "type": "KeyError", "message": "missing 'foo'"}
    title = _incident_title(err)
    assert title.startswith("[ERR]")
    assert "KeyError" in title
    assert "missing 'foo'" in title


def test_incident_title_test_failure():
    err = {"source": "test", "type": "FAILED", "message": "test_bar — AssertionError"}
    title = _incident_title(err)
    assert title.startswith("[TEST]")
    assert "test_bar" in title


def test_incident_title_truncates_long_message():
    err = {"source": "log", "type": "Error", "message": "x" * 200}
    title = _incident_title(err)
    assert len(title) <= 120, f"Title is too long: {len(title)}"
