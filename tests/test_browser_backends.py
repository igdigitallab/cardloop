"""Unit tests for spec-066 — pluggable browser backends.

Covers the config layer (modules.get_config/set_config + the browser default_config),
backend resolution (builtin / cloakbrowser / external-cdp), graceful degradation when
cloakbrowser is absent, the agent_actions safety gate, and the Cloak Manager config
plumbing (URL from config/env, token from the safe — never modules.json).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _use_tmp_data(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    os.environ["_CARDLOOP_DATA_DIR"] = str(data_dir)
    return data_dir


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    _use_tmp_data(tmp_path)
    # Clear backend-affecting env so a host value can't leak into assertions.
    for k in ("CLOAK_MANAGER_URL", "CLOAK_CDP_URL"):
        os.environ.pop(k, None)
    yield
    os.environ.pop("_CARDLOOP_DATA_DIR", None)
    for k in ("CLOAK_MANAGER_URL", "CLOAK_CDP_URL"):
        os.environ.pop(k, None)


import modules as _mod  # noqa: E402
import browser_backends as _backends  # noqa: E402


# ───────────────────────────── config layer ──────────────────────────────────


def test_browser_default_config_present():
    cfg = _mod.get_config("browser")
    assert cfg["backend"] == "builtin"
    assert cfg["agent_actions"] == "read"
    assert cfg["per_project_profile"] == {}


def test_list_modules_includes_config():
    browser = next(m for m in _mod.list_modules() if m["id"] == "browser")
    assert "config" in browser
    assert browser["config"]["backend"] == "builtin"


def test_set_config_roundtrip():
    _mod.set_config("browser", {"backend": "cloakbrowser", "agent_actions": "full"})
    cfg = _mod.get_config("browser")
    assert cfg["backend"] == "cloakbrowser"
    assert cfg["agent_actions"] == "full"
    # Untouched defaults survive the shallow merge.
    assert cfg["cdp_url"] == ""


def test_set_config_drops_unknown_keys():
    """Unknown keys (e.g. a smuggled secret) must not be persisted."""
    _mod.set_config("browser", {"backend": "external-cdp", "manager_token": "SECRET", "evil": 1})
    cfg = _mod.get_config("browser")
    assert "manager_token" not in cfg
    assert "evil" not in cfg
    assert cfg["backend"] == "external-cdp"


def test_set_config_rejects_non_dict():
    with pytest.raises(TypeError):
        _mod.set_config("browser", ["not", "a", "dict"])


def test_set_config_merges_partial_updates():
    """A later partial update must not reset previously-set fields."""
    _mod.set_config("browser", {"backend": "external-cdp", "cdp_url": "http://h:9222"})
    _mod.set_config("browser", {"agent_actions": "full"})  # partial — only agent_actions
    cfg = _mod.get_config("browser")
    assert cfg["backend"] == "external-cdp"      # survived
    assert cfg["cdp_url"] == "http://h:9222"     # survived
    assert cfg["agent_actions"] == "full"        # applied


def test_set_config_unknown_module():
    with pytest.raises(KeyError):
        _mod.set_config("nope", {})


def test_get_config_unknown_module():
    with pytest.raises(KeyError):
        _mod.get_config("nope")


# ───────────────────────────── backend resolution ────────────────────────────


def test_resolve_default_builtin():
    r = _backends.resolve("/some/cwd")
    assert r["backend"] == "builtin"
    assert r["agent_actions"] == "read"


def test_resolve_unknown_backend_falls_back_to_builtin():
    _mod.set_config("browser", {"backend": "wat"})
    assert _backends.resolve("/x")["backend"] == "builtin"


def test_resolve_invalid_agent_actions_falls_back_to_read():
    _mod.set_config("browser", {"agent_actions": "destroy-everything"})
    assert _backends.resolve("/x")["agent_actions"] == "read"


def test_resolve_external_cdp_static_url():
    _mod.set_config("browser", {"backend": "external-cdp", "cdp_url": "http://h:9222"})
    r = _backends.resolve("/x")
    assert r["backend"] == "external-cdp"
    assert r["cdp_url"] == "http://h:9222"


def test_resolve_external_cdp_url_from_env():
    _mod.set_config("browser", {"backend": "external-cdp"})
    os.environ["CLOAK_CDP_URL"] = "http://env:9222"
    assert _backends.resolve("/x")["cdp_url"] == "http://env:9222"


def test_resolve_per_project_profile_overrides_default():
    _mod.set_config("browser", {
        "backend": "external-cdp",
        "default_profile": "global-prof",
        "per_project_profile": {"/proj/a": "prof-a"},
    })
    assert _backends.resolve("/proj/a")["profile"] == "prof-a"
    assert _backends.resolve("/proj/b")["profile"] == "global-prof"


def test_resolve_cloak_knobs_passthrough():
    _mod.set_config("browser", {"backend": "cloakbrowser", "proxy": "http://p:8080", "humanize": True})
    r = _backends.resolve("/x")
    assert r["proxy"] == "http://p:8080"
    assert r["humanize"] is True


def test_agent_actions_helper():
    assert _backends.agent_actions("/x") == "read"
    _mod.set_config("browser", {"agent_actions": "full"})
    assert _backends.agent_actions("/x") == "full"


# ───────────────────────────── availability ──────────────────────────────────


def test_cloak_status_absent():
    """cloakbrowser is not a hard dependency — absence is reported, not raised."""
    st = _backends.cloak_status()
    assert st["installed"] is False
    assert st["binary_ready"] is False


def test_availability_shape():
    av = _backends.availability()
    assert av["tiers"]["builtin"]["available"] is True
    assert "cloakbrowser" in av["tiers"]
    assert av["tiers"]["external-cdp"]["available"] is True
    assert "manager" in av
    assert av["config"]["backend"] == "builtin"


# ───────────────────────────── Cloak Manager plumbing ────────────────────────


def test_manager_base_unconfigured():
    assert _backends.manager_base() is None
    assert _backends.manager_configured() is False


def test_manager_base_from_config_strips_slash():
    _mod.set_config("browser", {"manager_url": "https://cloak.example.com/"})
    assert _backends.manager_base() == "https://cloak.example.com"
    assert _backends.manager_configured() is True


def test_manager_base_from_env():
    os.environ["CLOAK_MANAGER_URL"] = "https://env.example.com"
    assert _backends.manager_base() == "https://env.example.com"


async def test_list_profiles_empty_when_unconfigured():
    """No Manager URL → no profiles, no network call, no raise."""
    assert await _backends.list_profiles() == []
