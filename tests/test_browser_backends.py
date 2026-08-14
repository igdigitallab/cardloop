"""Unit tests for spec-066 — pluggable browser backends.

Covers the config layer (modules.get_config/set_config + the browser default_config),
backend resolution (builtin / cloakbrowser / external-cdp), graceful degradation when
cloakbrowser is absent, the agent_actions safety gate, and the Cloak Manager config
plumbing (URL from config/env, token from the safe — never modules.json).
"""
import asyncio
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
    for k in ("CLOAK_MANAGER_URL", "CLOAK_CDP_URL", "CLOAK_MANAGER_CDP_BASE"):
        os.environ.pop(k, None)
    yield
    os.environ.pop("_CARDLOOP_DATA_DIR", None)
    for k in ("CLOAK_MANAGER_URL", "CLOAK_CDP_URL", "CLOAK_MANAGER_CDP_BASE"):
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


def test_resolve_per_project_profile_overrides_global_local_backend():
    """A mapped profile makes that ONE project browse as a Manager profile over
    external-cdp even though the global backend is a local one (cloakbrowser);
    unmapped projects keep the global backend."""
    _mod.set_config("browser", {
        "backend": "cloakbrowser",
        "per_project_profile": {"/proj/grants": "grants-id"},
    })
    a = _backends.resolve("/proj/grants")
    assert a["backend"] == "external-cdp"
    assert a["profile"] == "grants-id"
    assert a["cdp_url"] == ""   # empty so _acquire_external resolves via the Manager
    assert _backends.resolve("/proj/other")["backend"] == "cloakbrowser"


def test_resolve_external_cdp_without_target_falls_back_to_builtin():
    """external-cdp selected globally but no static url and no profile for this cwd →
    degrade to builtin instead of raising, so the pane still works."""
    _mod.set_config("browser", {"backend": "external-cdp"})
    assert _backends.resolve("/x")["backend"] == "builtin"


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


def test_cloak_status_absent(monkeypatch):
    """cloakbrowser is not a hard dependency — absence is reported, not raised.

    Force the absent path via monkeypatch so the assertion holds whether or not the
    package happens to be installed in the test environment (it is, on instances that
    ran spec-066's `pip install cloakbrowser`)."""
    monkeypatch.setattr(_backends, "_cloak_module", lambda: None)
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


def test_manager_cdp_base_override_and_fallback():
    """CDP base falls back to the REST base, but CLOAK_MANAGER_CDP_BASE overrides it
    (so raw CDP can target a directly-reachable host while REST stays behind a CDN)."""
    _mod.set_config("browser", {"manager_url": "https://cloak.example.com"})
    assert _backends.manager_cdp_base() == "https://cloak.example.com"  # fallback
    os.environ["CLOAK_MANAGER_CDP_BASE"] = "http://10.0.0.5:8080/"
    assert _backends.manager_cdp_base() == "http://10.0.0.5:8080"  # override, slash stripped


async def test_profile_cdp_url_absolutizes_relative(monkeypatch):
    """The Manager returns a relative '/api/profiles/<id>/cdp' — profile_cdp_url must
    prefix it with the CDP host so Playwright gets an absolute endpoint."""
    _mod.set_config("browser", {"manager_url": "https://cloak.example.com"})
    os.environ["CLOAK_MANAGER_CDP_BASE"] = "http://10.0.0.5:8080"

    async def fake_req(method, path):
        return {"cdp_url": "/api/profiles/abc/cdp"} if path.endswith("/cdp") else {}
    monkeypatch.setattr(_backends, "_manager_request", fake_req)

    assert await _backends.profile_cdp_url("abc") == "http://10.0.0.5:8080/api/profiles/abc/cdp"


async def test_profile_cdp_url_keeps_absolute(monkeypatch):
    """An already-absolute CDP URL from the Manager is returned untouched."""
    _mod.set_config("browser", {"manager_url": "https://cloak.example.com"})

    async def fake_req(method, path):
        return {"cdp_url": "ws://host:9222/devtools/browser/xyz"} if path.endswith("/cdp") else {}
    monkeypatch.setattr(_backends, "_manager_request", fake_req)

    assert await _backends.profile_cdp_url("abc") == "ws://host:9222/devtools/browser/xyz"


# ─────────────── spec-079/browser: shared profile across projects ───────────────
#
# Before this, `default_profile` was read ONLY inside the external-cdp branch, so
# setting it while the global backend was cloakbrowser/builtin was a silent no-op:
# the UI showed the profile as "in use" and every project still browsed anonymously.

def test_default_profile_applies_under_a_local_backend():
    """The 'use this logged-in profile everywhere' case."""
    _mod.set_config("browser", {"backend": "cloakbrowser", "default_profile": "google-id"})
    r = _backends.resolve("/proj/anything")
    assert r["backend"] == "external-cdp"
    assert r["profile"] == "google-id"
    assert r["cdp_url"] == ""          # resolved via the Manager, not a static endpoint
    assert r["isolate_page"] is True   # own tab per project — see below


def test_default_profile_applies_under_builtin_backend():
    _mod.set_config("browser", {"backend": "builtin", "default_profile": "google-id"})
    assert _backends.resolve("/proj/x")["profile"] == "google-id"


def test_per_project_profile_still_wins_over_the_default():
    _mod.set_config("browser", {
        "backend": "cloakbrowser",
        "default_profile": "google-id",
        "per_project_profile": {"/proj/grants": "grants-id"},
    })
    assert _backends.resolve("/proj/grants")["profile"] == "grants-id"
    assert _backends.resolve("/proj/other")["profile"] == "google-id"


def test_empty_mapping_is_an_explicit_opt_out():
    """A project pinned to '' must fall back to the plain backend even though a default
    profile exists — otherwise 'use everywhere' would be impossible to escape."""
    _mod.set_config("browser", {
        "backend": "cloakbrowser",
        "default_profile": "google-id",
        "per_project_profile": {"/proj/private": ""},
    })
    r = _backends.resolve("/proj/private")
    assert r["backend"] == "cloakbrowser"
    assert "profile" not in r
    assert _backends.resolve("/proj/other")["profile"] == "google-id"


def test_no_profile_configured_leaves_the_backend_alone():
    _mod.set_config("browser", {"backend": "cloakbrowser"})
    r = _backends.resolve("/proj/x")
    assert r["backend"] == "cloakbrowser"
    assert "isolate_page" not in r


def test_static_cdp_url_is_not_page_isolated():
    """Isolation is scoped to shared Manager profiles; a plain remote CDP endpoint keeps
    its previous adopt-the-existing-page behaviour."""
    _mod.set_config("browser", {"backend": "external-cdp", "cdp_url": "http://h:9222"})
    r = _backends.resolve("/x")
    assert "isolate_page" not in r


def test_availability_exposes_per_project_map():
    """The UI cannot render existing assignments without it."""
    _mod.set_config("browser", {
        "backend": "cloakbrowser",
        "per_project_profile": {"/proj/a": "prof-a"},
    })
    assert _backends.availability()["config"]["per_project_profile"] == {"/proj/a": "prof-a"}


def test_malformed_per_project_map_is_ignored():
    _mod.set_config("browser", {"backend": "cloakbrowser", "per_project_profile": "nope"})
    assert _backends.resolve("/proj/x")["backend"] == "cloakbrowser"


# ── Cloak Manager profile lifecycle ────────────────────────────────────────────
# Regression cover for a live incident: two Manager profiles Cardloop had launched
# sat idle ~10 hours with 55 Chrome processes between them, software-rendering
# through swiftshader on a GPU-less VM — host at 445% CPU / 67-73°C, load 9.8 on 6
# vCPU, swap full. We launched them and never stopped them. These pin down the
# contract that fixes it: whoever launched it stops it, and nothing else is touched.

@pytest.fixture(autouse=True)
def _clean_profile_registry(tmp_path, monkeypatch):
    # Point the ownership file at a tmp path for EVERY test in this module: without
    # this, any test that claims ownership persists a fake profile id into the real
    # data/cloak-profiles-owned.json and the running cockpit would later believe it
    # owns a profile named "STOPPED".
    monkeypatch.setattr(_backends, "_OWNED_PATH", tmp_path / "owned.json")
    monkeypatch.setattr(_backends, "_OWNED_LOADED", True)
    _backends._PROFILE_USERS.clear()
    _backends._PROFILE_OURS.clear()
    for t in list(_backends._PROFILE_STOPPERS.values()):
        t.cancel()
    _backends._PROFILE_STOPPERS.clear()
    yield
    _backends._PROFILE_USERS.clear()
    _backends._PROFILE_OURS.clear()
    _backends._PROFILE_STOPPERS.clear()
    _backends._OWNED_LOADED = False


def _stopped_calls(monkeypatch):
    """Record stop_profile() calls instead of hitting the Manager."""
    calls = []

    async def _stop(pid):
        calls.append(pid)
        return {}
    monkeypatch.setattr(_backends, "stop_profile", _stop)
    return calls


def test_idle_profile_we_launched_is_stopped_after_the_last_session_leaves(monkeypatch):
    calls = _stopped_calls(monkeypatch)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0.01)

    async def go():
        _backends._PROFILE_OURS.add("P")
        await _backends.attach_profile("P", "/proj/a")
        await _backends.release_profile("P", "/proj/a")
        task = _backends._PROFILE_STOPPERS.get("P")
        assert task is not None, "the last release must schedule a stop"
        await asyncio.sleep(0.05)
        assert calls == ["P"]
    asyncio.run(go())


def test_a_profile_the_operator_started_is_never_stopped_by_us(monkeypatch):
    """A profile already running when we arrived is the operator's — they may be
    working in the Manager's own noVNC viewer, which looks exactly like idle here."""
    calls = _stopped_calls(monkeypatch)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0.01)

    async def go():
        # NOT added to _PROFILE_OURS — it was already running
        await _backends.attach_profile("P", "/proj/a")
        await _backends.release_profile("P", "/proj/a")
        await asyncio.sleep(0.05)
        assert calls == []
        assert "P" not in _backends._PROFILE_STOPPERS
    asyncio.run(go())


def test_a_profile_still_used_by_another_project_is_not_stopped(monkeypatch):
    """default_profile is shared across every project without its own — one project
    finishing must not kill the browser another one is actively driving."""
    calls = _stopped_calls(monkeypatch)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0.01)

    async def go():
        _backends._PROFILE_OURS.add("P")
        await _backends.attach_profile("P", "/proj/a")
        await _backends.attach_profile("P", "/proj/b")
        await _backends.release_profile("P", "/proj/a")
        await asyncio.sleep(0.05)
        assert calls == []
        # …and only once the SECOND project leaves does it stop
        await _backends.release_profile("P", "/proj/b")
        await asyncio.sleep(0.05)
        assert calls == ["P"]
    asyncio.run(go())


def test_reattaching_within_the_grace_window_cancels_the_stop(monkeypatch):
    """A restart or a project switch reconnects seconds later — the profile must not
    be pulled out from under the returning session."""
    calls = _stopped_calls(monkeypatch)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0.2)

    async def go():
        _backends._PROFILE_OURS.add("P")
        await _backends.attach_profile("P", "/proj/a")
        await _backends.release_profile("P", "/proj/a")
        assert "P" in _backends._PROFILE_STOPPERS
        await _backends.attach_profile("P", "/proj/a")   # came back
        assert "P" not in _backends._PROFILE_STOPPERS
        await asyncio.sleep(0.3)
        assert calls == []
    asyncio.run(go())


def test_idle_stop_can_be_disabled_entirely(monkeypatch):
    calls = _stopped_calls(monkeypatch)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0)

    async def go():
        _backends._PROFILE_OURS.add("P")
        await _backends.attach_profile("P", "/proj/a")
        await _backends.release_profile("P", "/proj/a")
        await asyncio.sleep(0.05)
        assert calls == []
    asyncio.run(go())


def test_ownership_is_claimed_only_for_a_profile_that_was_not_running(monkeypatch):
    async def go():
        async def _listing():
            return [{"id": "RUNNING", "name": "r", "status": "running"},
                    {"id": "STOPPED", "name": "s", "status": "stopped"}]
        monkeypatch.setattr(_backends, "list_profiles", _listing)

        await _backends._note_launch_ownership("STOPPED")
        assert "STOPPED" in _backends._PROFILE_OURS
        await _backends._note_launch_ownership("RUNNING")
        assert "RUNNING" not in _backends._PROFILE_OURS
    asyncio.run(go())


def test_release_of_an_unknown_or_empty_profile_is_a_no_op():
    async def go():
        await _backends.release_profile("", "/proj/a")     # non-Manager backend
        await _backends.release_profile("ghost", "/proj/a")
        assert _backends._PROFILE_STOPPERS == {}
    asyncio.run(go())


def test_lifecycle_status_reports_what_we_hold_open():
    """Point 3 of the incident report: see the open profiles from outside, without
    having to ssh in and read `ps aux` on the VM."""
    async def go():
        _backends._PROFILE_OURS.add("P")
        await _backends.attach_profile("P", "/proj/a")
        st = _backends.profile_lifecycle_status()
        assert st["launched_by_us"] == ["P"]
        assert st["attached"] == {"P": ["/proj/a"]}
        assert st["stop_pending"] == []
    asyncio.run(go())


# ── ownership must survive a cockpit restart ───────────────────────────────────
# Found by verifying the fix above on the live Manager: after the first restart,
# profile d33e103d showed launched_by_us=[] even though we HAD launched it. The
# Chrome outlives the Python process, so on reconnect it reads as "already running"
# — i.e. the operator's — and would never be stopped again. Every restart would
# quietly launder one more profile into the never-stop category.

def test_ownership_survives_a_restart_via_the_persisted_set(monkeypatch, tmp_path):
    async def go():
        path = tmp_path / "owned.json"
        monkeypatch.setattr(_backends, "_OWNED_PATH", path)

        async def _listing():
            return [{"id": "P", "name": "p", "status": "stopped"}]
        monkeypatch.setattr(_backends, "list_profiles", _listing)

        # First run: profile was stopped, so we claim and persist it.
        await _backends._note_launch_ownership("P")
        assert "P" in _backends._PROFILE_OURS
        assert path.exists()

        # Simulate a restart: fresh in-memory state, the profile is now RUNNING
        # (we left it up), and the listing would therefore call it the operator's.
        _backends._PROFILE_OURS.clear()
        _backends._OWNED_LOADED = False

        async def _listing_running():
            return [{"id": "P", "name": "p", "status": "running"}]
        monkeypatch.setattr(_backends, "list_profiles", _listing_running)

        await _backends._note_launch_ownership("P")
        assert "P" in _backends._PROFILE_OURS, "restart laundered our profile into 'operator's'"
    asyncio.run(go())


def test_stopping_a_profile_drops_it_from_the_persisted_set(monkeypatch, tmp_path):
    """Otherwise a stale file would let us adopt — and kill — a profile the operator
    started by hand later on."""
    calls = _stopped_calls(monkeypatch)
    path = tmp_path / "owned.json"
    monkeypatch.setattr(_backends, "_OWNED_PATH", path)
    monkeypatch.setattr(_backends, "PROFILE_IDLE_STOP", 0.01)

    async def go():
        _backends._PROFILE_OURS.add("P")
        _backends._save_owned()
        await _backends.attach_profile("P", "/proj/a")
        await _backends.release_profile("P", "/proj/a")
        await asyncio.sleep(0.05)
        assert calls == ["P"]
        assert "P" not in _backends._PROFILE_OURS
        import json
        assert json.loads(path.read_text()) == []
    asyncio.run(go())


def test_a_corrupt_or_missing_ownership_file_is_survivable(monkeypatch, tmp_path):
    path = tmp_path / "owned.json"
    monkeypatch.setattr(_backends, "_OWNED_PATH", path)
    _backends._OWNED_LOADED = False
    _backends._load_owned()          # missing file
    assert _backends._PROFILE_OURS == set()
    path.write_text("{not json")
    _backends._OWNED_LOADED = False
    _backends._load_owned()          # garbage file
    assert _backends._PROFILE_OURS == set()
