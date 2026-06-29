"""Unit tests for modules.py — spec-065 Phase A module registry.

Covers:
- defaults when data/modules.json is absent
- toggle persists across reload
- unknown id raises KeyError
- file round-trip and atomic write (tmp + replace)
- list_modules() shape (no default_enabled in output)
- is_enabled() fallback to builtin default
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _use_tmp_data(tmp_path: Path):
    """Point modules._modules_path() at a temp dir for test isolation."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    os.environ["_CARDLOOP_DATA_DIR"] = str(data_dir)
    return data_dir


def _clear_tmp_data():
    os.environ.pop("_CARDLOOP_DATA_DIR", None)


# Re-import modules after each test to clear any in-process state.
# (The module has no module-level mutable cache, but we re-import for
# test isolation of the env var.)
@pytest.fixture(autouse=True)
def isolated_data(tmp_path):
    _use_tmp_data(tmp_path)
    yield
    _clear_tmp_data()


import modules as _mod  # noqa: E402 (after sys.path insert)


# ---------------------------------------------------------------------------
# list_modules()
# ---------------------------------------------------------------------------

def test_list_modules_returns_builtins():
    result = _mod.list_modules()
    assert len(result) == 3
    ids = {m["id"] for m in result}
    assert ids == {"github", "browser", "autopilot"}


def test_autopilot_default_disabled():
    """Autopilot is opt-in: off by default (spec-068 kill-switch)."""
    mods = {m["id"]: m for m in _mod.list_modules()}
    assert mods["autopilot"]["enabled"] is False


def test_list_modules_shape_no_default_enabled():
    """The returned dicts must NOT expose default_enabled."""
    for m in _mod.list_modules():
        assert "default_enabled" not in m
        # Required contract fields present
        for field in ("id", "name", "description", "version", "provides", "enabled"):
            assert field in m, f"Missing field {field!r} in module {m['id']!r}"
        assert isinstance(m["provides"], list)
        assert isinstance(m["enabled"], bool)


def test_github_default_enabled():
    mods = {m["id"]: m for m in _mod.list_modules()}
    assert mods["github"]["enabled"] is True


def test_browser_default_disabled():
    mods = {m["id"]: m for m in _mod.list_modules()}
    assert mods["browser"]["enabled"] is False


def test_list_modules_missing_file_uses_defaults(tmp_path):
    """No data/modules.json → defaults apply; no file is created."""
    result = _mod.list_modules()
    assert not (tmp_path / "data" / "modules.json").exists()
    mods = {m["id"]: m for m in result}
    assert mods["github"]["enabled"] is True
    assert mods["browser"]["enabled"] is False


# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------

def test_is_enabled_github_default_true():
    assert _mod.is_enabled("github") is True


def test_is_enabled_browser_default_false():
    assert _mod.is_enabled("browser") is False


def test_is_enabled_unknown_raises_key_error():
    with pytest.raises(KeyError):
        _mod.is_enabled("nonexistent-module")


# ---------------------------------------------------------------------------
# set_enabled()
# ---------------------------------------------------------------------------

def test_set_enabled_returns_updated_dict():
    updated = _mod.set_enabled("browser", True)
    assert updated["id"] == "browser"
    assert updated["enabled"] is True
    for field in ("name", "description", "version", "provides"):
        assert field in updated
    assert "default_enabled" not in updated


def test_set_enabled_toggle_persists_across_reload(tmp_path):
    """enable browser → is_enabled() returns True; disable → False."""
    _mod.set_enabled("browser", True)
    assert _mod.is_enabled("browser") is True

    _mod.set_enabled("browser", False)
    assert _mod.is_enabled("browser") is False


def test_set_enabled_persists_to_file(tmp_path):
    data_dir = tmp_path / "data"
    _mod.set_enabled("browser", True)
    p = data_dir / "modules.json"
    assert p.exists(), "modules.json should be created on first set_enabled()"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["browser"]["enabled"] is True


def test_set_enabled_unknown_raises_key_error():
    with pytest.raises(KeyError):
        _mod.set_enabled("bogus-id", True)


def test_set_enabled_atomic_write_no_tmp_file_left(tmp_path):
    """The .tmp file must not remain after a successful write."""
    data_dir = tmp_path / "data"
    _mod.set_enabled("github", False)
    tmp_files = list(data_dir.glob("*.tmp"))
    assert tmp_files == [], f"Stale .tmp files after set_enabled: {tmp_files}"


# ---------------------------------------------------------------------------
# File round-trip
# ---------------------------------------------------------------------------

def test_file_round_trip_multiple_toggles(tmp_path):
    data_dir = tmp_path / "data"
    _mod.set_enabled("github", False)
    _mod.set_enabled("browser", True)

    raw = json.loads((data_dir / "modules.json").read_text(encoding="utf-8"))
    assert raw["github"]["enabled"] is False
    assert raw["browser"]["enabled"] is True

    # Reload from disk matches
    assert _mod.is_enabled("github") is False
    assert _mod.is_enabled("browser") is True


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    """A corrupt modules.json → defaults apply, no crash."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "modules.json").write_text("{not valid json!", encoding="utf-8")

    assert _mod.is_enabled("github") is True
    assert _mod.is_enabled("browser") is False


def test_list_modules_after_partial_override(tmp_path):
    """Only one module overridden — the other still shows its default."""
    _mod.set_enabled("browser", True)
    mods = {m["id"]: m for m in _mod.list_modules()}
    assert mods["github"]["enabled"] is True   # default, not in file
    assert mods["browser"]["enabled"] is True  # from file
