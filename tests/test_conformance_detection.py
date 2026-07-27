"""
Tests for _detect_error_handler's self-declaration parsing (webapp.py).

Covers the two systemic defects found by the fleet-wide CLAUDE.md retrofit:
(a) the onboarding template ships the checklist under "## Cardloop Integration
    Status", which the parser did not recognize (only "## Cardloop conformance"
    and legacy "## ClaudeOps conformance").
(b) the negative-value set did not include Russian "нет", so legacy files with
    "error handler: нет" were misread as a positive (truthy) declaration.
"""
from pathlib import Path

from webapp import _detect_error_handler


def _text(heading: str, value: str) -> str:
    return f"{heading}\n- error handler: {value}\n- log_cmd: no\n"


# ─────────────────────────── heading variants ──────────────────────────────

def test_conformance_heading_current(tmp_path: Path):
    text = _text("## Cardloop conformance", "yes: sentry")
    assert _detect_error_handler(tmp_path, text) is True


def test_conformance_heading_legacy(tmp_path: Path):
    text = _text("## ClaudeOps conformance", "yes: sentry")
    assert _detect_error_handler(tmp_path, text) is True


def test_conformance_heading_template_integration_status(tmp_path: Path):
    """The onboarding template's heading must also be recognized."""
    text = _text("## Cardloop Integration Status", "yes: sentry")
    assert _detect_error_handler(tmp_path, text) is True


# ─────────────────────────── negative values ───────────────────────────────

def test_negative_value_english_no(tmp_path: Path):
    text = _text("## Cardloop conformance", "no")
    assert _detect_error_handler(tmp_path, text) is False


def test_negative_value_russian_net(tmp_path: Path):
    """'нет' is legacy Russian negative data — must NOT be a false-green."""
    text = _text("## Cardloop conformance", "нет")
    assert _detect_error_handler(tmp_path, text) is False


def test_negative_value_russian_net_under_template_heading(tmp_path: Path):
    """Both fixes combined: new heading + Russian negative value."""
    text = _text("## Cardloop Integration Status", "нет")
    assert _detect_error_handler(tmp_path, text) is False


def test_positive_value_still_detected(tmp_path: Path):
    text = _text("## Cardloop conformance", "yes: try/except in main.py")
    assert _detect_error_handler(tmp_path, text) is True


def test_no_conformance_section_falls_back_to_code_heuristic(tmp_path: Path):
    """No self-declaration section at all → falls through to the code scan,
    which finds nothing in an empty dir → False."""
    assert _detect_error_handler(tmp_path, "# Just a plain CLAUDE.md\n") is False
