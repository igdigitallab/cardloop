"""
Codex subscription limits behind the usage badge.

Codex never answers a "what are my limits" question — it PUSHES
`account/rateLimits/updated` mid-turn and that is the only source. The adapter already
normalised the notification into a `rate_limit` event and then dropped it on the floor
(webapp only stored events carrying `rate_limit_type`, which is Claude-shaped), so the
badge had nothing to show. These tests pin the persistence + shaping.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import codex_engine as _codex


def _snapshot(**over):
    snap = {
        "planType": "team",
        "limitName": "gpt-5.6",
        "primary": {"usedPercent": 42, "resetsAt": int(time.time()) + 7200,
                    "windowDurationMins": 300},
        "secondary": {"usedPercent": 71, "resetsAt": int(time.time()) + 400000,
                      "windowDurationMins": 10080},
        "credits": {"hasCredits": True, "unlimited": False, "balance": "12.00"},
    }
    snap.update(over)
    return snap


def test_snapshot_round_trips_to_ui_rows(tmp_path):
    _codex._save_rate_limits(tmp_path, _snapshot())
    ui = _codex.rate_limits_for_ui(tmp_path)
    assert ui["plan_type"] == "team"
    assert ui["ts"] > 0
    assert ui["limits"]["primary"]["utilization"] == 0.42
    assert ui["limits"]["primary"]["label"] == "5-hour window"
    assert ui["limits"]["primary"]["status"] == "allowed"
    assert ui["limits"]["secondary"]["utilization"] == 0.71
    assert ui["limits"]["secondary"]["label"] == "Week"
    # Credits are present, so no scary empty-wallet row.
    assert "credits" not in ui["limits"]


def test_empty_credits_surface_as_a_row(tmp_path):
    """The failure actually hit in production: auth fine, plan fine, wallet empty."""
    _codex._save_rate_limits(tmp_path, _snapshot(
        credits={"hasCredits": False, "unlimited": False, "balance": "0.00"}))
    ui = _codex.rate_limits_for_ui(tmp_path)
    assert ui["limits"]["credits"]["status"] == "rejected"
    assert "empty" in ui["limits"]["credits"]["label"].lower()


def test_unlimited_credits_do_not_add_a_row(tmp_path):
    _codex._save_rate_limits(tmp_path, _snapshot(
        credits={"hasCredits": False, "unlimited": True, "balance": None}))
    assert "credits" not in _codex.rate_limits_for_ui(tmp_path)["limits"]


def test_rate_limit_reached_marks_windows_rejected(tmp_path):
    _codex._save_rate_limits(tmp_path, _snapshot(rateLimitReachedType="primary"))
    ui = _codex.rate_limits_for_ui(tmp_path)
    assert ui["limits"]["primary"]["status"] == "rejected"
    assert ui["limits"]["secondary"]["status"] == "rejected"


def test_relative_resets_at_is_converted_to_absolute(tmp_path):
    """A small resetsAt cannot be a unix timestamp; treating it as one renders 1970."""
    _codex._save_rate_limits(tmp_path, _snapshot(
        primary={"usedPercent": 10, "resetsAt": 600, "windowDurationMins": 300}))
    row = _codex.rate_limits_for_ui(tmp_path)["limits"]["primary"]
    assert row["resets_at"] > time.time() + 500


def test_window_labels_cover_odd_durations(tmp_path):
    _codex._save_rate_limits(tmp_path, _snapshot(
        primary={"usedPercent": 5, "resetsAt": None, "windowDurationMins": 90},
        secondary={"usedPercent": 5, "resetsAt": None, "windowDurationMins": 20160}))
    limits = _codex.rate_limits_for_ui(tmp_path)["limits"]
    assert limits["primary"]["label"] == "90-minute window"
    assert limits["secondary"]["label"] == "2 weeks"
    assert limits["primary"]["resets_at"] is None


def test_missing_or_unreadable_file_is_none(tmp_path):
    assert _codex.rate_limits_for_ui(tmp_path) is None
    (tmp_path / "codex_rate_limits.json").write_text("{not json")
    assert _codex.rate_limits_for_ui(tmp_path) is None
    assert _codex.rate_limits_for_ui(None) is None


def test_snapshot_without_usable_windows_is_none(tmp_path):
    _codex._save_rate_limits(tmp_path, {"planType": "team"})
    assert _codex.rate_limits_for_ui(tmp_path) is None


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    _codex._save_rate_limits(tmp_path, _snapshot())
    _codex._save_rate_limits(tmp_path, _snapshot(primary={"usedPercent": 99, "resetsAt": None,
                                                          "windowDurationMins": 300}))
    assert not list(tmp_path.glob("*.tmp"))
    stored = json.loads((tmp_path / "codex_rate_limits.json").read_text())
    assert stored["snapshot"]["primary"]["usedPercent"] == 99


def test_save_tolerates_a_bad_dir_and_empty_snapshot(tmp_path):
    _codex._save_rate_limits(None, _snapshot())          # must not raise
    _codex._save_rate_limits(tmp_path, {})               # nothing to store
    assert not (tmp_path / "codex_rate_limits.json").exists()
