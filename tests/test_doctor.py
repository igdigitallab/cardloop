"""Tests for tools/doctor.py — one-command cockpit diagnosis (spec-082 workstream C).

Every probe takes its collaborators (subprocess runner, HTTP getter, process finder,
installed-version lookup, ...) as parameters, so these tests drive the findings logic
entirely with faked data. No test requires systemd, the network, or a live cockpit.
"""
from __future__ import annotations

import importlib.util
import json
import tomllib
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "doctor", Path(__file__).resolve().parent.parent / "tools" / "doctor.py")
doctor = importlib.util.module_from_spec(_SPEC)
# Register in sys.modules BEFORE exec: doctor.py's @dataclass Fact needs its module
# resolvable via sys.modules[cls.__module__] during class creation, or dataclasses
# crashes with "'NoneType' object has no attribute '__dict__'".
sys.modules["doctor"] = doctor
_SPEC.loader.exec_module(doctor)  # type: ignore[union-attr]


# ─────────────────────────── fake collaborators ──────────────────────────────────

def _fake_run(table: dict):
    """Build a `run(cmd, timeout=...)` fake: table maps a cmd-prefix tuple to a
    canned (returncode, stdout, stderr). Falls back to None (command "not found")."""
    def run(cmd, timeout=3.0):
        for prefix, result in table.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return result
        return None
    return run


# ─────────────────────────── Fact / redaction ─────────────────────────────────────

def test_redact_keeps_prefix_and_suffix_only():
    r = doctor._redact("sk-ant-api03-ABCDEFGHIJKLMNOP1234")
    assert r.startswith("sk-ant-")
    assert r.endswith("1234")
    assert "ABCDEFGHIJKLMNOP" not in r


def test_redact_short_value_fully_masked():
    assert doctor._redact("abc") == "…"


def test_scrub_removes_known_secret_pattern():
    text = "auth failed for sk-ant-api03-SUPERSECRETVALUE1234 while connecting"
    out = doctor._scrub(text, [])
    assert "SUPERSECRETVALUE1234" not in out
    assert "sk-ant-" in out  # prefix survives, only the middle is redacted


def test_scrub_removes_exact_known_secret_value():
    secret = "hunter2-not-a-real-password"
    text = f"login failed, tried password {secret} three times"
    out = doctor._scrub(text, [secret])
    assert secret not in out


def test_scrub_noop_on_empty():
    assert doctor._scrub("", ["x"]) == ""
    assert doctor._scrub(None, ["x"]) is None


def test_scrub_jwt_shaped_token():
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = doctor._scrub(f"oauth token: {fake_jwt}", [])
    assert fake_jwt not in out


# ─────────────────────────── redaction end-to-end (the required test) ────────────

def _sections_with_leaked_secret(secret_value: str) -> dict:
    leaking_fact = doctor.Fact("Recent warnings",
                                f"Traceback: request failed, key={secret_value} rejected",
                                level="warn",
                                remedy=f"rotate the key (was {secret_value})")
    return {name: [] for name in doctor.SECTIONS} | {"Service": [leaking_fact]}


def test_fake_secrets_never_appear_in_text_output():
    api_key = "sk-ant-api03-THISISAFAKESECRETVALUE7890"
    password = "correct-horse-battery-staple-FAKE"
    oauth_token = "atk_FAKEOAUTHTOKENVALUE1234567890abcdef"
    sections = _sections_with_leaked_secret(api_key)
    text = doctor.render_text(sections, [api_key, password, oauth_token], elapsed=0.1)
    assert api_key not in text
    assert password not in text
    assert oauth_token not in text


def test_fake_secrets_never_appear_in_json_output():
    api_key = "sk-ant-api03-THISISAFAKESECRETVALUE7890"
    password = "correct-horse-battery-staple-FAKE"
    sections = _sections_with_leaked_secret(api_key)
    raw = doctor.render_json(sections, [api_key, password], elapsed=0.1, exit_code=0)
    assert api_key not in raw
    assert password not in raw
    # must still be valid JSON after scrubbing
    parsed = json.loads(raw)
    assert parsed["verdict"]["ok"] is False


# ─────────────────────────── Versions ────────────────────────────────────────────

def test_versions_flags_stale_sdk_below_requirements_floor(tmp_path):
    (tmp_path / "requirements.txt").write_text("claude-agent-sdk>=0.2.129\n")
    run = _fake_run({
        ("git",): (0, "v1.0.0", ""),
        ("node",): (0, "v20.0.0", ""),
        ("claude",): (0, "2.1.221 (Claude Code)", ""),
    })
    facts = doctor.probe_versions(repo_root=tmp_path, run=run,
                                   installed_version=lambda name: "0.2.90" if name == "claude-agent-sdk" else None)
    sdk_fact = next(f for f in facts if f.label == "claude-agent-sdk")
    assert sdk_fact.level == "fail"
    assert "0.2.129" in sdk_fact.remedy


def test_versions_ok_sdk_meets_floor(tmp_path):
    (tmp_path / "requirements.txt").write_text("claude-agent-sdk>=0.2.129\n")
    run = _fake_run({("git",): (0, "v1.0.0", ""), ("node",): (0, "v20.0.0", "")})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run,
                                   installed_version=lambda name: "0.2.129" if name == "claude-agent-sdk" else None)
    sdk_fact = next(f for f in facts if f.label == "claude-agent-sdk")
    assert sdk_fact.level == "ok"


def test_versions_flags_sdk_behind_pypi_even_when_floor_is_met(tmp_path):
    """Meeting requirements.txt's floor is OUR number, not Anthropic's — doctor must
    still say something when the cockpit's cached PyPI answer is newer."""
    (tmp_path / "requirements.txt").write_text("claude-agent-sdk>=0.2.129\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sdk-version.json").write_text(json.dumps({"latest": "0.2.144"}))
    run = _fake_run({("git",): (0, "v1.0.0", ""), ("node",): (0, "v20.0.0", "")})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run,
                                   installed_version=lambda name: "0.2.129" if name == "claude-agent-sdk" else None)
    assert next(f for f in facts if f.label == "claude-agent-sdk").level == "ok"
    pypi = next(f for f in facts if f.label == "claude-agent-sdk (PyPI)")
    assert pypi.level == "warn"
    assert "0.2.144" in pypi.value and "0.2.144" in pypi.remedy


def test_versions_no_pypi_fact_when_current_or_uncached(tmp_path):
    """Silent in both quiet cases: cache says we're current, and no cache at all."""
    (tmp_path / "requirements.txt").write_text("claude-agent-sdk>=0.2.129\n")
    run = _fake_run({("git",): (0, "v1.0.0", ""), ("node",): (0, "v20.0.0", "")})
    installed = lambda name: "0.2.144" if name == "claude-agent-sdk" else None

    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=installed)
    assert not [f for f in facts if f.label == "claude-agent-sdk (PyPI)"]

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sdk-version.json").write_text(json.dumps({"latest": "0.2.144"}))
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=installed)
    assert not [f for f in facts if f.label == "claude-agent-sdk (PyPI)"]


def test_versions_sdk_not_importable_is_fail(tmp_path):
    (tmp_path / "requirements.txt").write_text("claude-agent-sdk>=0.2.129\n")
    run = _fake_run({})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=lambda name: None)
    sdk_fact = next(f for f in facts if f.label == "claude-agent-sdk")
    assert sdk_fact.level == "fail"
    assert "venv/bin/python" in sdk_fact.remedy


def test_versions_missing_node_is_fail(tmp_path):
    run = _fake_run({("git",): (0, "v1.0.0", "")})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=lambda name: None)
    node_fact = next(f for f in facts if f.label == "Node")
    assert node_fact.level == "fail"


def test_versions_dirty_git_tree_is_warn(tmp_path):
    run = _fake_run({
        ("git", "-C", str(tmp_path), "describe"): (0, "v1.0.0-dirty", ""),
        ("git", "-C", str(tmp_path), "rev-parse"): (0, "master", ""),
        ("node",): (0, "v20.0.0", ""),
    })
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=lambda name: None)
    cardloop_fact = next(f for f in facts if f.label == "Cardloop")
    assert cardloop_fact.level == "warn"
    assert "uncommitted" in cardloop_fact.remedy


def test_versions_codex_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_ENABLED", raising=False)
    run = _fake_run({})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=lambda name: None)
    codex_fact = next(f for f in facts if f.label == "Codex SDK")
    assert codex_fact.level == "info"
    assert "disabled" in codex_fact.value


def test_versions_codex_enabled_but_missing_is_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ENABLED", "true")
    run = _fake_run({})
    facts = doctor.probe_versions(repo_root=tmp_path, run=run, installed_version=lambda name: None)
    codex_fact = next(f for f in facts if f.label == "Codex SDK")
    assert codex_fact.level == "fail"


# ─────────────────────────── Auth ────────────────────────────────────────────────

def test_auth_api_key_set_in_subscription_mode_is_fail(tmp_path):
    env = {"CLAUDE_AUTH_MODE": "subscription", "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefgh1234"}
    cred = tmp_path / "missing-creds.json"
    facts = doctor.probe_auth(env, cred_path=cred)
    key_fact = next(f for f in facts if f.label == "ANTHROPIC_API_KEY")
    assert key_fact.level == "fail"
    assert "abcdefgh1234" not in key_fact.value  # redacted, per spec: sk-ant-…4chars
    assert "sk-ant-…1234" in key_fact.value


def test_auth_api_key_missing_in_api_key_mode_is_fail(tmp_path):
    env = {"CLAUDE_AUTH_MODE": "api_key"}
    facts = doctor.probe_auth(env, cred_path=tmp_path / "nope.json")
    key_fact = next(f for f in facts if f.label == "ANTHROPIC_API_KEY")
    assert key_fact.level == "fail"


def test_auth_api_key_set_in_api_key_mode_is_warn_not_fail(tmp_path):
    env = {"CLAUDE_AUTH_MODE": "api_key", "ANTHROPIC_API_KEY": "sk-ant-api03-abcdefgh1234"}
    facts = doctor.probe_auth(env, cred_path=tmp_path / "nope.json")
    key_fact = next(f for f in facts if f.label == "ANTHROPIC_API_KEY")
    assert key_fact.level == "warn"


def test_auth_no_api_key_subscription_mode_is_ok(tmp_path):
    env = {"CLAUDE_AUTH_MODE": "subscription"}
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 9999999999999}}))
    facts = doctor.probe_auth(env, cred_path=cred)
    key_fact = next(f for f in facts if f.label == "ANTHROPIC_API_KEY")
    assert key_fact.level == "ok"


def test_auth_credentials_missing_is_fail_in_subscription_mode(tmp_path):
    env = {"CLAUDE_AUTH_MODE": "subscription"}
    facts = doctor.probe_auth(env, cred_path=tmp_path / "absent.json")
    cred_fact = next(f for f in facts if f.label == "OAuth credentials")
    assert cred_fact.level == "fail"
    assert "claude login" in cred_fact.remedy


def test_auth_credentials_expired_is_fail(tmp_path):
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"expiresAt": 1}}))  # 1970, long expired
    env = {"CLAUDE_AUTH_MODE": "subscription"}
    facts = doctor.probe_auth(env, cred_path=cred)
    cred_fact = next(f for f in facts if f.label == "OAuth credentials")
    assert cred_fact.level == "fail"
    assert "EXPIRED" in cred_fact.value


def test_auth_credentials_valid_is_ok(tmp_path):
    future_ms = 99999999999999
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"expiresAt": future_ms, "subscriptionType": "max"}}))
    env = {"CLAUDE_AUTH_MODE": "subscription"}
    facts = doctor.probe_auth(env, cred_path=cred)
    cred_fact = next(f for f in facts if f.label == "OAuth credentials")
    assert cred_fact.level == "ok"
    assert "max" in cred_fact.value


# ─────────────────────────── Config ──────────────────────────────────────────────

def test_config_web_password_placeholder_is_fail(tmp_path):
    env = {"WEB_PASSWORD": "CHANGE_ME"}
    facts = doctor.probe_config(env, tmp_path / ".env", True, totp_status=lambda repo_root: (None, ""))
    pw_fact = next(f for f in facts if f.label == "WEB_PASSWORD")
    assert pw_fact.level == "fail"


def test_config_web_password_blank_is_fail(tmp_path):
    env = {"WEB_PASSWORD": ""}
    facts = doctor.probe_config(env, tmp_path / ".env", True, totp_status=lambda repo_root: (None, ""))
    pw_fact = next(f for f in facts if f.label == "WEB_PASSWORD")
    assert pw_fact.level == "fail"


def test_config_web_password_set_never_shows_value(tmp_path):
    env = {"WEB_PASSWORD": "s3cr3t-actual-value"}
    facts = doctor.probe_config(env, tmp_path / ".env", True, totp_status=lambda repo_root: (None, ""))
    pw_fact = next(f for f in facts if f.label == "WEB_PASSWORD")
    assert pw_fact.level == "ok"
    assert "s3cr3t-actual-value" not in pw_fact.value
    assert pw_fact.value == "set"


def test_config_env_missing_is_fail(tmp_path):
    facts = doctor.probe_config({}, tmp_path / ".env", False, totp_status=lambda repo_root: (None, ""))
    env_fact = next(f for f in facts if f.label == ".env")
    assert env_fact.level == "fail"


def test_config_totp_on(tmp_path):
    env = {"WEB_PASSWORD": "real-password"}
    facts = doctor.probe_config(env, tmp_path / ".env", True, totp_status=lambda repo_root: (True, ""))
    totp_fact = next(f for f in facts if f.label == "TOTP")
    assert totp_fact.value == "on"
    assert totp_fact.level == "ok"


def test_config_totp_unknown_is_info_not_a_problem(tmp_path):
    env = {"WEB_PASSWORD": "real-password"}
    facts = doctor.probe_config(env, tmp_path / ".env", True,
                                 totp_status=lambda repo_root: (None, "no vault yet"))
    totp_fact = next(f for f in facts if f.label == "TOTP")
    assert totp_fact.level == "info"


# ─────────────────────────── Service ─────────────────────────────────────────────

def test_service_memory_high_below_max_is_livelock_fail():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=4294967296\nMemoryMax=8589934592\n"
                                    "MemoryCurrent=1000000000\nMainPID=123", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    mem_fact = next(f for f in facts if f.label == "MemoryHigh/MemoryMax")
    assert mem_fact.level == "fail"
    assert "MemoryHigh=infinity" in mem_fact.remedy


def test_service_memory_high_infinity_is_ok():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=8589934592\n"
                                    "MemoryCurrent=1000000000\nMainPID=123", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    mem_fact = next(f for f in facts if f.label == "MemoryHigh/MemoryMax")
    assert mem_fact.level == "ok"


def test_service_oom_policy_stop_fails_and_continue_passes():
    """OOMPolicy=stop (the systemd default) turned one OOM-killed agent job into a full cockpit
    restart twice on 2026-09-23; doctor must flag it with the exact fix."""
    def facts_for(policy):
        run = _fake_run({
            ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                        "MemoryHigh=infinity\nMemoryMax=8589934592\n"
                                        f"MemoryCurrent=1000000000\nMainPID=123\nOOMPolicy={policy}", ""),
            ("journalctl",): (0, "-- No entries --", ""),
        })
        return next(f for f in doctor.probe_service("cardloop", run=run) if f.label == "OOMPolicy")

    stop = facts_for("stop")
    assert stop.level == "fail" and "OOMPolicy=continue" in stop.remedy
    assert facts_for("continue").level == "ok"


def test_service_memory_current_warns_when_headroom_is_thin():
    """83% of MemoryMax is the state that preceded both real OOM kills on ops (2026-08-26/27):
    the unit still reports active/running, so nothing else in doctor would flag it."""
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=10737418240\n"
                                    "MemoryCurrent=8900000000\nMainPID=123", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    mem_fact = next(f for f in facts if f.label == "MemoryCurrent")
    assert mem_fact.level == "warn"
    assert "LIVE_CLIENT_MAX" in mem_fact.remedy


def test_service_memory_current_fails_at_ninety_percent():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=10737418240\n"
                                    "MemoryCurrent=10200000000\nMainPID=123", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    mem_fact = next(f for f in facts if f.label == "MemoryCurrent")
    assert mem_fact.level == "fail"


def _cgroup_with(tmp_path, current, inactive, slab=0, stat=True):
    cg = tmp_path / "system.slice" / "cardloop.service"
    cg.mkdir(parents=True)
    (cg / "memory.current").write_text(str(current))
    if stat:
        (cg / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nslab_reclaimable {slab}\n")
    return tmp_path


def _mem_run():
    return _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=10737418240\n"
                                    "MemoryCurrent=10200000000\nMainPID=123\n"
                                    "ControlGroup=/system.slice/cardloop.service", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })


def test_service_memory_judges_the_working_set_not_the_page_cache(tmp_path):
    """10.2 GB of memory.current is 95% of MemoryMax, but 7 GB of it is inactive file cache and 1 GB
    reclaimable slab: the host has plenty of headroom and doctor must not cry OOM."""
    root = _cgroup_with(tmp_path, current=10_200_000_000, inactive=7_000_000_000, slab=1_000_000_000)
    facts = doctor.probe_service("cardloop", run=_mem_run(), cgroup_root=root)
    mem_fact = next(f for f in facts if f.label == "MemoryCurrent")
    assert mem_fact.level == "ok"
    assert "working set" in mem_fact.value and "(20% of MemoryMax)" in mem_fact.value and "raw 9727 MiB" in mem_fact.value


def test_service_memory_still_fails_when_the_working_set_really_is_high(tmp_path):
    root = _cgroup_with(tmp_path, current=10_200_000_000, inactive=100_000_000)
    mem_fact = next(f for f in doctor.probe_service("cardloop", run=_mem_run(), cgroup_root=root)
                    if f.label == "MemoryCurrent")
    assert mem_fact.level == "fail"


def test_service_memory_falls_back_to_the_raw_figure_when_the_cgroup_is_unreadable(tmp_path):
    root = _cgroup_with(tmp_path, current=10_200_000_000, inactive=0, stat=False)
    mem_fact = next(f for f in doctor.probe_service("cardloop", run=_mem_run(), cgroup_root=root)
                    if f.label == "MemoryCurrent")
    assert mem_fact.level == "fail" and "working set" not in mem_fact.value


def test_service_restart_count_is_surfaced():
    """systemd silently restarting the unit is invisible in the cockpit — the operator only sees
    a chat that froze. Surface it."""
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=infinity\n"
                                    "MemoryCurrent=1000000000\nNRestarts=2\nMainPID=123", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    restart_fact = next(f for f in facts if f.label == "Restarts")
    assert restart_fact.level == "warn"
    assert "2 since boot" in restart_fact.value


def test_service_inactive_is_fail():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=inactive\nSubState=dead\n"
                                    "MemoryHigh=infinity\nMemoryMax=infinity\n", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    unit_fact = next(f for f in facts if f.label == "systemd unit")
    assert unit_fact.level == "fail"


def test_service_active_is_ok():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=infinity\n", ""),
        ("journalctl",): (0, "-- No entries --", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    unit_fact = next(f for f in facts if f.label == "systemd unit")
    assert unit_fact.level == "ok"


def test_service_systemctl_unavailable_does_not_crash():
    run = _fake_run({})  # every command "not found"
    facts = doctor.probe_service("cardloop", run=run)
    assert facts  # produced at least the "could not query" info fact
    assert facts[0].level == "info"


def test_service_recent_warnings_flagged():
    run = _fake_run({
        ("systemctl", "show"): (0, "ActiveState=active\nSubState=running\n"
                                    "MemoryHigh=infinity\nMemoryMax=infinity\n", ""),
        ("journalctl",): (0, "Aug 19 10:00:00 host bot.py[1]: WARNING something broke", ""),
    })
    facts = doctor.probe_service("cardloop", run=run)
    warn_fact = next(f for f in facts if f.label == "Recent warnings")
    assert warn_fact.level == "warn"


# ─────────────────────────── Runtime ─────────────────────────────────────────────

def test_runtime_health_unreachable_is_fail(tmp_path):
    def boom(url, timeout=3.0):
        raise OSError("Connection refused")
    facts = doctor.probe_runtime("8787", repo_root=tmp_path, http_get=boom,
                                  find_procs=lambda root: [], port_listening=lambda h, p: False)
    health_fact = next(f for f in facts if f.label == "GET /api/health?deep=1")
    assert health_fact.level == "fail"
    port_fact = next(f for f in facts if f.label.startswith("port"))
    assert port_fact.level == "fail"


def test_runtime_health_ok(tmp_path):
    (tmp_path / "web" / "dist").mkdir(parents=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("<html></html>")
    facts = doctor.probe_runtime(
        "8787", repo_root=tmp_path,
        http_get=lambda url, timeout=3.0: {"ok": True, "running": 0, "agents": 0, "plan_pending": 0},
        find_procs=lambda root: [1234], port_listening=lambda h, p: True)
    health_fact = next(f for f in facts if f.label == "GET /api/health?deep=1")
    assert health_fact.level == "ok"


def test_runtime_multiple_bot_processes_is_warn(tmp_path):
    facts = doctor.probe_runtime(
        "8787", repo_root=tmp_path,
        http_get=lambda url, timeout=3.0: {"ok": True, "running": 0},
        find_procs=lambda root: [111, 222], port_listening=lambda h, p: True)
    proc_fact = next(f for f in facts if f.label == "bot.py processes")
    assert proc_fact.level == "warn"


def test_runtime_web_dist_missing_is_fail(tmp_path):
    facts = doctor.probe_runtime(
        "8787", repo_root=tmp_path,
        http_get=lambda url, timeout=3.0: {"ok": True},
        find_procs=lambda root: [], port_listening=lambda h, p: True)
    dist_fact = next(f for f in facts if f.label == "web/dist")
    assert dist_fact.level == "fail"
    assert "MISSING" in dist_fact.value


def test_runtime_web_dist_stale_is_fail(tmp_path):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<html></html>")
    src = tmp_path / "web" / "src"
    src.mkdir(parents=True)
    # Make the src file's mtime clearly newer than the dist file's.
    import os
    import time
    old = time.time() - 100
    os.utime(index, (old, old))
    (src / "App.tsx").write_text("export default 1;")

    facts = doctor.probe_runtime(
        "8787", repo_root=tmp_path,
        http_get=lambda url, timeout=3.0: {"ok": True},
        find_procs=lambda root: [], port_listening=lambda h, p: True)
    dist_fact = next(f for f in facts if f.label == "web/dist")
    assert dist_fact.level == "fail"
    assert "STALE" in dist_fact.value


def test_runtime_web_dist_fresh_is_ok(tmp_path):
    src = tmp_path / "web" / "src"
    src.mkdir(parents=True)
    (src / "App.tsx").write_text("export default 1;")
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    import os
    import time
    (dist / "index.html").write_text("<html></html>")
    future = time.time() + 100
    os.utime(dist / "index.html", (future, future))

    facts = doctor.probe_runtime(
        "8787", repo_root=tmp_path,
        http_get=lambda url, timeout=3.0: {"ok": True},
        find_procs=lambda root: [], port_listening=lambda h, p: True)
    dist_fact = next(f for f in facts if f.label == "web/dist")
    assert dist_fact.level == "ok"


# ─────────────────────────── Data ────────────────────────────────────────────────

def test_data_counts_topics_and_sessions(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "topics.json").write_text(json.dumps({"a": 1, "b": 2, "c": 3}))
    (data / "sessions.json").write_text(json.dumps({"a": 1}))
    facts = doctor.probe_data(repo_root=tmp_path)
    topics_fact = next(f for f in facts if f.label == "topics.json")
    sessions_fact = next(f for f in facts if f.label == "sessions.json")
    assert "3 entries" in topics_fact.value
    assert "1 entries" in sessions_fact.value


def test_data_missing_dir_is_info_not_failure(tmp_path):
    facts = doctor.probe_data(repo_root=tmp_path)
    assert facts[0].level == "info"


def test_data_board_counts_never_leak_card_text(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    tasks = tmp_path / "TASKS.md"
    tasks.write_text(
        "# Tasks\n\n"
        "## Backlog\n"
        "- [ ] TOP SECRET card text nobody should see <!--ops:abc123-->\n"
        "## In Progress\n"
        "## Review\n"
        "## Failed\n"
    )
    facts = doctor.probe_data(repo_root=tmp_path)
    board_fact = next(f for f in facts if f.label == "board (TASKS.md)")
    assert "TOP SECRET" not in board_fact.value
    assert "Backlog=1" in board_fact.value


def test_data_registry_optional_and_absent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    facts = doctor.probe_data(repo_root=tmp_path)
    reg_fact = next(f for f in facts if f.label == "registry.json")
    assert "absent" in reg_fact.value
    assert reg_fact.level == "ok"  # optional file — absence is not a problem


# ─────────────────────────── small helpers ───────────────────────────────────────

def test_parse_mem_value_infinity_is_none():
    assert doctor._parse_mem_value("infinity") is None
    assert doctor._parse_mem_value("[not set]") is None
    assert doctor._parse_mem_value("") is None
    assert doctor._parse_mem_value(None) is None


def test_parse_mem_value_numeric():
    assert doctor._parse_mem_value("1048576") == 1048576


def test_parse_version_orders_correctly():
    assert doctor._parse_version("0.2.90") < doctor._parse_version("0.2.129")
    assert doctor._parse_version("0.2.129") >= doctor._parse_version("0.2.129")


def test_human_bytes_reasonable():
    assert doctor._human_bytes(500) == "500B"
    assert "KB" in doctor._human_bytes(2048)
    assert "MB" in doctor._human_bytes(5 * 1024 * 1024)


def test_count_json_entries_dict_and_list(tmp_path):
    d = tmp_path / "d.json"
    d.write_text(json.dumps({"a": 1, "b": 2}))
    lst = tmp_path / "l.json"
    lst.write_text(json.dumps([1, 2, 3]))
    absent = tmp_path / "nope.json"
    assert doctor._count_json_entries(d) == 2
    assert doctor._count_json_entries(lst) == 3
    assert doctor._count_json_entries(absent) is None


def test_dir_size_counts_files(tmp_path):
    (tmp_path / "a.txt").write_text("x" * 100)
    (tmp_path / "b.txt").write_text("y" * 200)
    result = doctor._dir_size(tmp_path, budget_sec=2.0)
    assert "B" in result or "KB" in result


def test_find_bot_processes_returns_empty_for_nonmatching_root():
    # Reads the real local process table (not network/systemd/a live cockpit) but a
    # bot.py under this bogus root can never match a real PID's cmdline — deterministic.
    pids = doctor._find_bot_processes(Path("/nonexistent-repo-root-for-test"))
    assert pids == []


# ─────────────────────────── env loading ─────────────────────────────────────────

def test_load_dotenv_merged_fills_gaps_not_overrides(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("WEB_PORT=9999\nCARDLOOP_SERVICE=fromdotenv\n")
    monkeypatch.setenv("WEB_PORT", "1111")  # real env wins over .env
    monkeypatch.delenv("CARDLOOP_SERVICE", raising=False)
    monkeypatch.delenv("COPS_NO_DOTENV", raising=False)
    merged, env_path, exists = doctor._load_dotenv_merged(repo_root=tmp_path)
    assert exists is True
    assert merged["WEB_PORT"] == "1111"          # real env takes precedence
    assert merged["CARDLOOP_SERVICE"] == "fromdotenv"  # .env fills the gap


def test_load_dotenv_merged_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COPS_NO_DOTENV", raising=False)
    merged, env_path, exists = doctor._load_dotenv_merged(repo_root=tmp_path)
    assert exists is False


# ─────────────────────────── verdict / rendering / exit code ─────────────────────

def test_verdict_empty_prints_no_problems_found():
    sections = {name: [doctor.Fact("x", "ok value", level="ok")] for name in doctor.SECTIONS}
    text = doctor.render_text(sections, [], elapsed=0.05)
    assert "no problems found" in text


def test_verdict_nonempty_lists_findings_with_remedy():
    sections = {name: [] for name in doctor.SECTIONS}
    sections["Runtime"] = [doctor.Fact("web/dist", "MISSING", level="fail", remedy="cd web && npm run build")]
    text = doctor.render_text(sections, [], elapsed=0.05)
    assert "web/dist" in text
    assert "npm run build" in text


def test_exit_code_zero_when_only_warnings():
    sections = {name: [] for name in doctor.SECTIONS}
    sections["Service"] = [doctor.Fact("Recent warnings", "1 line", level="warn")]
    has_fail = any(f.level == "fail" for facts in sections.values() for f in facts)
    assert has_fail is False  # exit_code computed the same way in main()


def test_exit_code_one_when_any_fail():
    sections = {name: [] for name in doctor.SECTIONS}
    sections["Runtime"] = [doctor.Fact("web/dist", "MISSING", level="fail")]
    has_fail = any(f.level == "fail" for facts in sections.values() for f in facts)
    assert has_fail is True


def test_render_json_is_valid_and_matches_exit_code():
    sections = {name: [] for name in doctor.SECTIONS}
    sections["Auth"] = [doctor.Fact("ANTHROPIC_API_KEY", "SET while subscription", level="fail")]
    raw = doctor.render_json(sections, [], elapsed=0.2, exit_code=1)
    parsed = json.loads(raw)
    assert parsed["verdict"]["exit_code"] == 1
    assert parsed["verdict"]["ok"] is False
    assert len(parsed["verdict"]["findings"]) == 1
    assert parsed["elapsed_sec"] == pytest.approx(0.2)


# ─────────────────────────── CLI wiring (main) ───────────────────────────────────

def test_main_json_flag_prints_valid_json(monkeypatch, capsys):
    fake_sections = {name: [doctor.Fact("x", "ok", level="ok")] for name in doctor.SECTIONS}
    monkeypatch.setattr(doctor, "collect", lambda repo_root: (fake_sections, []))
    code = doctor.main(["--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert code == 0
    assert parsed["verdict"]["ok"] is True


def test_main_text_mode_exit_code_reflects_failures(monkeypatch, capsys):
    fake_sections = {name: [] for name in doctor.SECTIONS}
    fake_sections["Runtime"] = [doctor.Fact("web/dist", "MISSING", level="fail")]
    monkeypatch.setattr(doctor, "collect", lambda repo_root: (fake_sections, []))
    code = doctor.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "web/dist" in out
    assert "no problems found" not in out


# ═══════════════════════════ Grok (spec-095 §5.9) ════════════════════════════════
#
# Every test drives probe_grok against a throwaway install: a tiny fake `grok` script (own
# `--version` / `inspect --json` switches, tests/fake_grok_acp.py for the engine's own fake), a
# fake bwrap, a fake /proc tree, temp dirs. Never the real binary, never the network, never a
# model turn.

import os
import shutil
import subprocess
import time

import grok_engine

FAKE_ACP = Path(__file__).resolve().parent / "fake_grok_acp.py"
SLEEP = shutil.which("sleep") or "/bin/sleep"
TOKEN_ACCESS = "ACCESS-TOKEN-VALUE-abcdef123456"
TOKEN_REFRESH = "REFRESH-TOKEN-VALUE-abcdef123456"
PLANTED_EMAIL = "planted.person@example.invalid"
CLK = os.sysconf("SC_CLK_TCK")


def inspect_doc(home: str, **over) -> dict:
    """The shape `grok inspect --json` has on grok 1.0.46 (captured from the real CLI), minimal."""
    doc = {
        "grokVersion": "1.0.46", "channel": "unknown", "cwd": "/x", "projectRoot": None,
        "projectTrusted": False, "projectInstructions": [], "plugins": [],
        "hooks": [{"event": "pre_tool_use", "hookType": "command", "target": f"\"{home}/.claude/hooks/g.sh\"",
                   "source": {"type": "user", "path": f"{home}/.claude"}, "vendor": "claude",
                   "disabled": True, "compatibilityStatus": "disabled"}],
        "skills": [{"name": "agents-skill", "source": {"type": "user",
                                                       "path": f"{home}/.agents/skills/a/SKILL.md"},
                    "userInvocable": True}],
        "agents": [{"name": "general-purpose", "source": {"type": "builtin"}}],
        "mcpServers": [{"name": "mail", "transport": "stdio", "target": "/x",
                        "source": {"type": "claudeJson", "path": f"{home}/.claude.json"},
                        "disabled": True, "compatibilityStatus": "disabled", "vendor": "claude"}],
        "externalCompat": {"remoteSettingsLoaded": False, "cells": [
            {"vendor": "claude", "surface": "mcps", "enabled": False, "source": "env"},
            {"vendor": "claude", "surface": "skills", "enabled": False, "source": "env"},
            {"vendor": "cursor", "surface": "hooks", "enabled": False, "source": "env"},
            {"vendor": "claude", "surface": "sessions", "enabled": True, "source": "default"},
            {"vendor": "codex", "surface": "sessions", "enabled": True, "source": "default"}]},
    }
    doc.update(over)
    return doc


def pid_gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state == "Z"


class GrokBox:
    """One isolated Grok install + the env dict probe_grok is handed."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        bwrap = self.bindir / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n")
        bwrap.chmod(0o755)
        self.fake_home = tmp_path / "fakehome"
        self.fake_home.mkdir()
        self.home = tmp_path / "grok-home"          # GROK_HOME — NOT created until a test asks
        self.data = tmp_path / "cockpit-data"       # deliberately NOT <repo_root>/data
        self.data.mkdir()
        # the engine always denies the cockpit repo's own `.env`: a scratch repo, so the counts below
        # do not depend on whether the checkout running the suite happens to have one
        self.engine_repo = tmp_path / "cockpit-repo"
        self.engine_repo.mkdir()
        monkeypatch.setattr(grok_engine, "_REPO", self.engine_repo)
        self.secret_dir = tmp_path / "secrets"
        self.secret_dir.mkdir()
        self.dump = tmp_path / "dump.jsonl"
        self.pidfile = tmp_path / "grandchild.pid"
        self.bin = tmp_path / "grok"
        self.env = {"GROK_ENABLED": "true", "GROK_BIN": str(self.bin), "GROK_HOME": str(self.home),
                    "_CARDLOOP_DATA_DIR": str(self.data), "HOME": str(self.fake_home),
                    "PATH": str(self.bindir), "GROK_SANDBOX_DENY": str(self.secret_dir)}
        for k in list(os.environ):
            if k.startswith(("GROK_", "FAKE_")):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("_CARDLOOP_DATA_DIR", raising=False)   # doctor must hand the engine its OWN ctx
        for k, v in self.env.items():                # engine helpers read os.environ directly
            if k != "_CARDLOOP_DATA_DIR":
                monkeypatch.setenv(k, v)
        self.fake()
        self.write_auth()

    # --- fake binary -----------------------------------------------------------------------
    def fake(self, version: str = "grok 1.0.46 (fake) [stable]", inspect_mode: str = "ok",
             doc: "dict | None" = None, stderr: str = "", version_mode: str = "ok", version_exit: int = 0,
             docs_by_cwd: "dict | None" = None, per_cwd_mode: "dict | None" = None) -> None:
        cfg = {"version": version, "version_mode": version_mode, "version_exit": version_exit,
               "inspect_mode": inspect_mode, "stderr": stderr, "sleep": SLEEP,
               "doc": doc if doc is not None else inspect_doc(str(self.fake_home)),
               "docs_by_cwd": docs_by_cwd or {}, "per_cwd_mode": per_cwd_mode or {},
               "dump": str(self.dump), "pidfile": str(self.pidfile)}
        self.bin.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
cfg = json.loads({json.dumps(json.dumps(cfg))})
args = sys.argv[1:]
gh = os.environ.get("GROK_HOME", "")
cfg_toml = os.path.join(gh, "config.toml")
def slurp(name):
    path = os.path.join(gh, name)
    return open(path).read() if os.path.exists(path) else None
with open(cfg["dump"], "a") as fh:
    fh.write(json.dumps({{"args": args, "home": os.environ.get("GROK_HOME"), "cwd": os.getcwd(),
                         "env": dict(os.environ),
                         "config": open(cfg_toml).read() if os.path.exists(cfg_toml) else None,
                         "sandbox_toml": slurp("sandbox.toml"), "trust_store": slurp("trusted_folders.toml")}}) + "\\n")
def hang():
    gc = subprocess.Popen([cfg["sleep"], "300"])
    open(cfg["pidfile"], "w").write(str(gc.pid))
    time.sleep(300)
if args == ["--version"]:
    if cfg["version_mode"] == "hang":
        hang()
    print(cfg["version"])
    sys.exit(cfg["version_exit"])
if args == ["inspect", "--json"]:
    here = os.path.basename(os.getcwd())
    m = cfg["per_cwd_mode"].get(here, cfg["inspect_mode"])
    if m == "hang":
        hang()
    if m == "exit":
        sys.stderr.write(cfg["stderr"])
        sys.exit(3)
    if m == "garbage":
        print("this is not json")
        sys.exit(0)
    print(json.dumps(cfg["docs_by_cwd"].get(here, cfg["doc"])))
    sys.exit(0)
if args[:1] == ["agent"]:
    open(cfg["pidfile"], "w").write(str(os.getpid()))
    time.sleep(300)
sys.exit(2)
""")
        self.bin.chmod(0o755)

    def make_import_surface(self) -> None:
        """The engine ALWAYS denies these (FLOOR_DENY); existing ones are not counted as 'missing'."""
        for d in (".claude", ".claude-accounts", ".cursor"):
            (self.fake_home / d).mkdir(exist_ok=True)
        (self.fake_home / ".claude.json").write_text("{}")
        (self.engine_repo / ".env").write_text("WEB_PASSWORD=x\n")
        # the other always-on entries: the operator's own login and the secret safe's directory
        (self.fake_home / ".grok").mkdir(exist_ok=True)
        (self.fake_home / ".grok" / "auth.json").write_text("{}")
        (self.fake_home / ".config" / "claude-ops").mkdir(parents=True, exist_ok=True)

    def use_acp_fake(self, **switches) -> None:
        """The engine's own fake (tests/fake_grok_acp.py): `--version` and `models` only."""
        env = {f"FAKE_GROK_{k.upper()}": str(v) for k, v in switches.items()}
        self.bin.write_text(f"#!{sys.executable}\nimport os, sys\nos.environ.update({env!r})\n"
                            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE_ACP)!r}, *sys.argv[1:]])\n")
        self.bin.chmod(0o755)

    def calls(self) -> "list[dict]":
        if not self.dump.exists():
            return []
        return [json.loads(line) for line in self.dump.read_text().splitlines() if line.strip()]

    # --- state the probes read -------------------------------------------------------------
    def write_auth(self, **entry) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        body = {"auth_mode": "oidc", "coding_data_retention_opt_out": True, "email": PLANTED_EMAIL,
                "key": TOKEN_ACCESS, "refresh_token": TOKEN_REFRESH, "expires_at": "2099-01-01T00:00:00Z"}
        body.update(entry)
        body = {k: v for k, v in body.items() if v is not None}
        (self.home / "auth.json").write_text(json.dumps({"https://auth.x.ai::client-id": body}))

    def ensure_home(self) -> dict:
        return grok_engine.ensure_home({"DATA": self.data}, bin_path=str(self.bin))

    def write_probe(self, state: str, *, age: float = 60.0, detail: str = "canary unreadable, control readable",
                    fingerprint: str = "auto", version: str = "1.0.46") -> None:
        if fingerprint == "auto":
            deny, _ = grok_engine.build_deny(self.home, {"DATA": self.data}, bin_path=str(self.bin))
            fingerprint = grok_engine._probe_fingerprint(version, {"deny": deny, "home": self.home})
        (self.data / "grok_sandbox_probe.json").write_text(json.dumps(
            {"fingerprint": fingerprint, "state": state, "detail": detail, "ts": time.time() - age}))

    def probe(self, **kw) -> "dict[str, doctor.Fact]":
        secrets = kw.setdefault("secrets_out", [])
        facts = doctor.probe_grok(self.env, repo_root=self.tmp, **kw)
        by = {f.label: f for f in facts}
        assert len(by) == len(facts), "duplicate Grok fact labels"
        self.secrets = secrets
        return by


@pytest.fixture
def box(tmp_path, monkeypatch):
    return GrokBox(tmp_path, monkeypatch)


def snapshot(*roots: Path) -> dict:
    """name -> (size, mtime_ns) of everything under the roots: proves doctor wrote nothing."""
    out = {}
    for root in roots:
        for p in [root, *root.rglob("*")]:
            try:
                st = p.lstat()
            except OSError:
                continue
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


def fake_proc(root: Path, procs: "list[dict]", uptime: float = 100000.0) -> Path:
    """A /proc tree: each proc = {pid, argv, environ (dict|None=unreadable), age, state, pgid}."""
    root.mkdir(exist_ok=True)
    (root / "uptime").write_text(f"{uptime} 0.00\n")
    for p in procs:
        d = root / str(p["pid"])
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in p["argv"]) + b"\0")
        if p.get("environ") is not None:
            (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in p["environ"].items()) + b"\0")
        pgid = p.get("pgid", p["pid"])
        start = int((uptime - p["age"]) * CLK)
        (d / "stat").write_text(f"{p['pid']} (grok) {p.get('state', 'S')} 1 {pgid} {pgid} 0 -1 4194560 "
                                f"0 0 0 0 0 0 0 0 20 0 1 0 {start} 0 0\n")
    return root


AGENT_ARGV = ["/home/u/.grok/bin/grok", "agent", "--no-leader", "stdio"]


# ─────────────────────────── silent unless enabled ───────────────────────────────

@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off", "garbage"])
def test_grok_is_silent_when_disabled(box, value):
    env = dict(box.env)
    if value is None:
        env.pop("GROK_ENABLED")
    else:
        env["GROK_ENABLED"] = value

    def boom(*a, **k):
        raise AssertionError("a disabled Grok must not run anything")
    assert doctor.probe_grok(env, repo_root=box.tmp, run=boom, proc_root=box.tmp / "nope") == []
    assert box.calls() == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_grok_runs_for_every_truthy_spelling(box, value):
    box.env["GROK_ENABLED"] = value
    assert "Grok CLI" in box.probe(proc_root=box.tmp / "noproc")


def test_grok_enabled_only_through_dotenv_is_honoured_by_collect(box, monkeypatch):
    """Enabled in .env only (not exported): the cockpit loads .env itself, so doctor must too —
    otherwise it would call a Grok-enabled cockpit clean without looking."""
    monkeypatch.delenv("GROK_ENABLED")
    (box.tmp / ".env").write_text("GROK_ENABLED=true\n")
    monkeypatch.delenv("COPS_NO_DOTENV", raising=False)
    for name in ("probe_versions", "probe_auth", "probe_config", "probe_service", "probe_runtime",
                 "probe_data", "probe_load"):
        monkeypatch.setattr(doctor, name, lambda *a, **k: [])
    real = doctor.probe_grok
    monkeypatch.setattr(doctor, "probe_grok", lambda env, repo_root, **kw: real(
        env, repo_root, proc_root=box.tmp / "noproc", **kw))
    sections, secrets = doctor.collect(box.tmp)
    assert any(f.label == "Grok CLI" for f in sections["Grok"])
    assert os.environ.get("GROK_ENABLED") is None          # the overlay put it back
    assert PLANTED_EMAIL in secrets                         # the account email is scrubbed everywhere


def test_collect_reports_an_empty_grok_section_when_disabled(box, monkeypatch):
    monkeypatch.setenv("GROK_ENABLED", "false")
    for name in ("probe_versions", "probe_auth", "probe_config", "probe_service", "probe_runtime",
                 "probe_data", "probe_load"):
        monkeypatch.setattr(doctor, name, lambda *a, **k: [])
    sections, secrets = doctor.collect(box.tmp)
    assert sections["Grok"] == []
    assert PLANTED_EMAIL not in secrets


# ─────────────────────────── Grok CLI ────────────────────────────────────────────

def test_cli_known_good_version_is_ok(box):
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "ok" and "1.0.46" in f.value and "known-good" in f.value and str(box.bin) in f.value


def test_cli_unknown_version_warns_never_fails(box):
    box.fake(version="grok 1.0.99 (abc123)")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "warn"
    assert "1.0.99" in f.value and "1.0.46" in f.value
    assert "KNOWN_GOOD_VERSIONS" in f.remedy and "grok_live" in f.remedy


def test_cli_with_the_engines_own_fake_binary(box):
    box.use_acp_fake()
    assert box.probe(proc_root=box.tmp / "noproc")["Grok CLI"].level == "ok"
    box.use_acp_fake(version="grok 2.0.0 (fake)")
    assert box.probe(proc_root=box.tmp / "noproc")["Grok CLI"].level == "warn"


def test_cli_missing_binary_with_grok_bin_set_is_fail(box):
    box.env["GROK_BIN"] = str(box.tmp / "nope")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "fail" and "GROK_BIN" in f.value and "install" in f.remedy


def test_cli_missing_binary_without_grok_bin_is_fail(box):
    box.env.pop("GROK_BIN")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "fail" and "not found" in f.value and "GROK_BIN" in f.value


def test_cli_garbage_version_output_is_fail(box):
    box.fake(version="hello world")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "fail" and "unrecognised" in f.value


def test_cli_nonzero_exit_is_fail(box):
    box.fake(version_exit=2)
    assert box.probe(proc_root=box.tmp / "noproc")["Grok CLI"].level == "fail"


def test_cli_that_cannot_answer_is_a_warning_not_a_hang(box):
    seen = []

    def run(cmd, timeout=3.0, env=None, cwd=None):
        seen.append((cmd, timeout))
        return None
    f = box.probe(run=run, proc_root=box.tmp / "noproc")["Grok CLI"]
    assert f.level == "warn" and "did not finish" in f.value
    assert seen[0][0] == [str(box.bin), "--version"] and seen[0][1] == doctor.GROK_CMD_TIMEOUT_SEC


def test_cli_hang_is_bounded_and_leaves_no_process(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_CMD_TIMEOUT_SEC", 0.6)
    box.fake(version_mode="hang")
    t0 = time.monotonic()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok CLI"]
    assert time.monotonic() - t0 < 5
    assert f.level == "warn"
    gc = int(box.pidfile.read_text())
    deadline = time.monotonic() + 3
    while not pid_gone(gc) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_gone(gc), "the grandchild of a timed-out probe must be killed with its group"


def test_cli_and_inspect_run_in_a_throwaway_home_and_write_nothing_real(box):
    """`grok --version` creates a missing GROK_HOME and `grok inspect` rewrites <home>/docs: neither
    may touch the cockpit's real home (doctor is read-only)."""
    box.home.rmdir() if not any(box.home.iterdir()) else None
    shutil.rmtree(box.home)                      # the real home does not exist at all
    before = snapshot(box.data, box.fake_home, box.secret_dir)
    box.probe(proc_root=box.tmp / "noproc")
    assert not box.home.exists()
    assert snapshot(box.data, box.fake_home, box.secret_dir) == before
    runs = box.calls()
    assert [c["args"] for c in runs] == [["--version"], ["inspect", "--json"]]
    for c in runs:
        assert c["home"] != str(box.home) and "doctor-grok-" in c["home"]
        assert "doctor-grok-" in c["cwd"] and c["cwd"].endswith("cwd")
        assert not Path(c["home"]).parent.exists(), "the throwaway dir must be gone afterwards"


def test_commands_run_under_the_engines_child_env(box):
    box.env["WEB_PASSWORD"] = "pw-s3cret-web-value"       # must not reach the child (allowlist)
    box.env["ANTHROPIC_API_KEY"] = "sk-ant-s3cret-value"
    box.probe(proc_root=box.tmp / "noproc")
    for c in box.calls():
        for k, v in grok_engine.D3_ENV.items():
            assert c["env"].get(k) == v, k
        assert "WEB_PASSWORD" not in c["env"] and "ANTHROPIC_API_KEY" not in c["env"]
        assert "GROK_SANDBOX" not in c["env"]            # the engine's probes run unsandboxed too


# ─────────────────────────── Grok auto-update ────────────────────────────────────

def test_autoupdate_off_with_generated_config_is_ok(box):
    box.ensure_home()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auto-update"]
    assert f.level == "ok" and "auto_update = false" in f.value


def test_autoupdate_without_a_generated_config_is_ok_but_says_so(box):
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auto-update"]
    assert f.level == "ok" and "not generated yet" in f.value


def test_autoupdate_unreadable_config_says_so(box):
    (box.home / "config.toml").write_text("this is [not toml")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auto-update"]
    assert f.level == "ok" and "unreadable" in f.value


def test_autoupdate_enabled_in_config_warns(box):
    box.ensure_home()
    (box.home / "config.toml").write_text("[cli]\nauto_update = true\n")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auto-update"]
    assert f.level == "warn" and "auto_update = true" in f.value and "grok-acct" in f.remedy


def test_autoupdate_switch_missing_from_the_child_env_warns(box, monkeypatch):
    monkeypatch.delitem(grok_engine.D3_ENV, "GROK_DISABLE_AUTOUPDATER")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auto-update"]
    assert f.level == "warn" and "NOT disabled" in f.value and "D3" in f.remedy


# ─────────────────────────── Grok auth ───────────────────────────────────────────

def test_auth_signed_in_is_ok_and_redacts_the_email(box):
    by = box.probe(proc_root=box.tmp / "noproc")
    f = by["Grok auth"]
    assert f.level == "ok" and "oidc" in f.value and "opt-out: yes" in f.value
    assert PLANTED_EMAIL not in f.value
    assert f.value.count("…") == 1 and "as " in f.value              # the doctor `_redact` shape
    assert box.secrets == [PLANTED_EMAIL]


def test_auth_without_an_email_has_no_as_clause(box):
    box.write_auth(email=None)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auth"]
    assert f.level == "ok" and " as " not in f.value and box.secrets == []


def test_auth_missing_login_is_fail_with_the_login_command(box):
    (box.home / "auth.json").unlink()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auth"]
    assert f.level == "fail" and f.remedy == "tools/grok-acct login" and str(box.home) in f.value


def test_auth_unparsable_login_file_is_fail(box):
    (box.home / "auth.json").write_text("{not json")
    assert box.probe(proc_root=box.tmp / "noproc")["Grok auth"].level == "fail"


def test_auth_non_oidc_login_is_fail(box):
    box.write_auth(auth_mode="api_key")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auth"]
    assert f.level == "fail" and "OIDC" in f.value and f.remedy == "tools/grok-acct login"


@pytest.mark.parametrize("opt_out", [False, None, "true", 1])
def test_auth_retention_opt_out_not_strictly_true_is_fail(box, opt_out):
    box.write_auth(coding_data_retention_opt_out=opt_out)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok auth"]
    assert f.level == "fail" and "retention" in f.value and "grok-acct login" in f.remedy


def test_auth_never_prints_tokens_or_the_email_in_any_render(box):
    box.fake(inspect_mode="exit", stderr=f"denied for {PLANTED_EMAIL} token {TOKEN_ACCESS}")
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc",
                              secrets_out=(secrets := []))
    sections = {n: [] for n in doctor.SECTIONS} | {"Grok": facts}
    secrets += [TOKEN_ACCESS, TOKEN_REFRESH]                           # what a render would also know
    text = doctor.render_text(sections, secrets, elapsed=0.1)
    raw = doctor.render_json(sections, secrets, elapsed=0.1, exit_code=0)
    for blob in (text, raw):
        assert PLANTED_EMAIL not in blob
        assert TOKEN_ACCESS not in blob and TOKEN_REFRESH not in blob
    assert "could not inspect" in text                                  # the leaking fact IS rendered


def test_auth_secret_email_scrubs_even_when_the_probe_text_carries_it(box):
    box.fake(inspect_mode="exit", stderr=f"user {PLANTED_EMAIL} rejected")
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc",
                              secrets_out=(secrets := []))
    compat = next(f for f in facts if f.label == "Grok compat")
    assert PLANTED_EMAIL in compat.value                                # the raw fact leaks ...
    assert PLANTED_EMAIL not in doctor._scrub(compat.value, secrets)    # ... and the plumbing scrubs it


# ─────────────────────────── Grok sandbox: bwrap / GROK_HOME ─────────────────────

def test_bwrap_present_is_ok(box):
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (bwrap)"]
    assert f.level == "ok" and f.value == str(box.bindir / "bwrap")


def test_bwrap_missing_is_fail(box):
    (box.bindir / "bwrap").unlink()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (bwrap)"]
    assert f.level == "fail" and "bubblewrap" in f.value and "bubblewrap" in f.remedy


def test_home_plain_directory_is_ok(box):
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (GROK_HOME)"]
    assert f.level == "ok" and f.value == str(box.home)


def test_home_symlink_is_fail(box):
    real = box.tmp / "real-home"
    real.mkdir()
    shutil.rmtree(box.home)
    box.home.symlink_to(real)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (GROK_HOME)"]
    assert f.level == "fail" and "symlink" in f.value


def test_home_that_is_a_file_is_fail(box):
    shutil.rmtree(box.home)
    box.home.write_text("x")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (GROK_HOME)"]
    assert f.level == "fail" and "not a directory" in f.value


def test_home_missing_is_info_not_a_verdict(box):
    shutil.rmtree(box.home)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (GROK_HOME)"]
    assert f.level == "info" and "does not exist" in f.value


# ─────────────────────────── Grok sandbox: profile ───────────────────────────────

def test_profile_not_generated_yet_warns(box):
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "warn" and "not generated" in f.value and "ensure_home" in f.remedy


def test_profile_generated_by_the_engine_is_ok_with_the_deny_count(box):
    info = box.ensure_home()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "ok"
    assert f"{len(info['deny'])} deny entries" in f.value and "'cardloop'" in f.value
    assert str(box.secret_dir) in (box.home / "sandbox.toml").read_text()


def test_profile_value_names_the_listed_paths_missing_on_this_host(box, monkeypatch):
    monkeypatch.setenv("GROK_SANDBOX_DENY", f"{box.secret_dir},{box.tmp / 'does-not-exist'}")
    box.env["GROK_SANDBOX_DENY"] = os.environ["GROK_SANDBOX_DENY"]
    box.make_import_surface()                  # the always-on floor exists here: only ONE path is missing
    box.ensure_home()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "ok" and "1 listed path(s) missing" in f.value


def test_profile_that_differs_from_what_the_engine_would_write_warns(box):
    box.ensure_home()
    other = box.tmp / "other-secret"
    other.mkdir()
    box.env["GROK_SANDBOX_DENY"] = f"{box.secret_dir},{other}"      # env changed since the last turn
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "warn" and "would write" in f.value and "re-run" in f.remedy


def test_profile_that_is_not_toml_warns(box):
    box.ensure_home()
    (box.home / "sandbox.toml").write_text("not [valid toml")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "warn" and "unreadable" in f.value


def test_profile_without_the_cardloop_table_warns(box):
    box.ensure_home()
    (box.home / "sandbox.toml").write_text('[profiles.other]\nextends = "workspace"\ndeny = []\n')
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "warn" and "no [profiles.cardloop]" in f.value


def test_invalid_deny_list_is_fail(box):
    box.env["GROK_SANDBOX_DENY"] = "**/{x}.pem"           # brace alternation: Grok refuses to start on it
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "fail" and "deny list invalid" in f.value and "GROK_SANDBOX_DENY" in f.remedy


def test_deny_list_that_hides_the_home_is_fail(box):
    box.env["GROK_SANDBOX_DENY"] = str(box.fake_home)               # $HOME itself: Grok could not start
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (profile)"]
    assert f.level == "fail"


# ─────────────────────────── Grok sandbox: cached probe verdict ──────────────────

def probe_fact(box, **kw):
    return box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (probe)"]


def test_probe_absent_warns_and_says_doctor_will_not_run_one(box):
    box.ensure_home()
    f = probe_fact(box)
    assert f.level == "warn" and "no verdict" in f.value and "never runs a model turn" in f.remedy


def test_probe_ok_and_fresh_is_ok(box):
    box.ensure_home()
    box.write_probe("ok")
    f = probe_fact(box)
    assert f.level == "ok" and f.value.startswith("ok ") and "canary unreadable" in f.value


def test_probe_ok_older_than_the_cache_ttl_is_stale_warn(box):
    box.ensure_home()
    box.write_probe("ok", age=grok_engine.SANDBOX_PROBE_OK_TTL_SEC + 60)
    f = probe_fact(box)
    assert f.level == "warn" and "stale" in f.value and "TTL" in f.value


def test_probe_ok_just_inside_the_ttl_is_ok(box):
    box.ensure_home()
    box.write_probe("ok", age=grok_engine.SANDBOX_PROBE_OK_TTL_SEC - 3600)
    assert probe_fact(box).level == "ok"


def test_probe_ok_for_another_fingerprint_is_stale_warn(box):
    box.ensure_home()
    box.write_probe("ok", fingerprint="0" * 24)
    f = probe_fact(box)
    assert f.level == "warn" and "different CLI version" in f.value


def test_probe_ok_for_another_cli_version_is_stale_warn(box):
    box.ensure_home()
    box.write_probe("ok", version="1.0.45")
    assert probe_fact(box).level == "warn"


def test_probe_ok_when_the_cli_version_is_unknown_cannot_be_trusted_fresh(box):
    box.ensure_home()
    box.write_probe("ok")
    box.fake(version="garbage")                                      # no version -> no fingerprint to compare
    f = probe_fact(box)
    assert f.level == "warn" and "cannot be checked" in f.value


def test_probe_failed_is_fail(box):
    box.ensure_home()
    box.write_probe("failed", detail="the sandbox deny list did NOT hide the canary file")
    f = probe_fact(box)
    assert f.level == "fail" and "FAILED" in f.value and "did NOT hide" in f.value
    assert str(box.data / "grok_sandbox_probe.json") in f.remedy


def test_probe_failed_stays_fail_however_old(box):
    box.ensure_home()
    box.write_probe("failed", age=30 * 86400)
    assert probe_fact(box).level == "fail"


def test_probe_failed_for_another_fingerprint_is_only_stale(box):
    box.ensure_home()
    box.write_probe("failed", fingerprint="f" * 24)
    f = probe_fact(box)
    assert f.level == "warn" and "stale FAILED" in f.value


def test_probe_failed_when_freshness_cannot_be_judged_is_still_fail(box):
    box.ensure_home()
    box.write_probe("failed")
    box.fake(version="garbage")
    assert probe_fact(box).level == "fail"


def test_probe_inconclusive_warns_fail_closed(box):
    box.ensure_home()
    box.write_probe("inconclusive", detail="the probe turn never read its control file")
    f = probe_fact(box)
    assert f.level == "warn" and "inconclusive" in f.value and "fails closed" in f.remedy


def test_probe_unreadable_file_warns(box):
    box.ensure_home()
    (box.data / "grok_sandbox_probe.json").write_text("{nope")
    f = probe_fact(box)
    assert f.level == "warn" and "unreadable" in f.value


@pytest.mark.parametrize("payload", [{"fingerprint": "x"}, {"state": 7, "ts": 1}, [], "str"])
def test_probe_file_without_a_usable_state_warns(box, payload):
    box.ensure_home()
    (box.data / "grok_sandbox_probe.json").write_text(json.dumps(payload))
    f = probe_fact(box)
    assert f.level == "warn" and "unreadable" in f.value


def test_probe_file_without_a_timestamp_reads_as_ancient_not_unreadable(box):
    box.ensure_home()
    (box.data / "grok_sandbox_probe.json").write_text(json.dumps({"state": "ok", "detail": "x"}))
    f = probe_fact(box)
    assert f.level == "warn" and "stale ok verdict" in f.value


def test_probe_unknown_state_warns(box):
    box.ensure_home()
    box.write_probe("maybe")
    f = probe_fact(box)
    assert f.level == "warn" and "unrecognised" in f.value


def test_probe_detail_is_truncated(box):
    box.ensure_home()
    box.write_probe("inconclusive", detail="x" * 5000)
    assert len(probe_fact(box).value) < 400


def test_doctor_never_runs_a_model_turn_or_the_engine_probe(box, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("doctor must not call the engine's turn/probe machinery")
    for name in ("run_grok_engine", "_run_turn", "_probe_sandbox_denial", "_ensure_sandbox_probe",
                 "provider_info", "_probe_provider", "ensure_home", "reap_litter", "reset_sandbox_probe"):
        monkeypatch.setattr(grok_engine, name, boom)
    facts = box.probe(proc_root=box.tmp / "noproc")
    assert "Grok sandbox (probe)" in facts and not [f for f in facts.values() if "crashed" in f.value]


def test_doctor_is_read_only_against_a_fully_prepared_install(box):
    box.ensure_home()
    box.write_probe("ok")
    (box.home / "sandbox-blocked.99999999").write_text("")
    (box.data / "grok_usage.jsonl").write_text("{}\n")
    before = snapshot(box.home, box.data, box.fake_home, box.secret_dir)
    box.probe(proc_root=box.tmp / "noproc")
    assert snapshot(box.home, box.data, box.fake_home, box.secret_dir) == before


# ─────────────────────────── Grok compat ─────────────────────────────────────────

def compat(box, **fake) -> "doctor.Fact":
    box.fake(**fake)
    return box.probe(proc_root=box.tmp / "noproc")["Grok compat"]


def doc_with(box, **over):
    return inspect_doc(str(box.fake_home), **over)


def test_compat_isolated_is_ok(box):
    f = compat(box)
    assert f.level == "ok" and "isolated" in f.value and "0 active MCP servers" in f.value


def test_compat_runs_inspect_under_the_neutral_dir_with_the_homes_config_only(box):
    box.ensure_home()
    (box.home / "config.toml").write_text("[cli]\nauto_update = false\n# marker\n")
    (box.home / "auth.json").write_text("SHOULD-NOT-BE-COPIED")
    compat(box)
    run = [c for c in box.calls() if c["args"] == ["inspect", "--json"]][0]
    assert run["config"].endswith("# marker\n")                   # the cockpit's config rides along
    assert "doctor-grok-" in run["home"] and str(box.home) != run["home"]


def test_compat_active_mcp_server_is_fail_with_its_name(box):
    mcp = [{"name": "mail", "disabled": True}, {"name": "tablet", "transport": "stdio",
                                                "source": {"type": "mcpJson", "path": "/x/.mcp.json"}}]
    f = compat(box, doc=doc_with(box, mcpServers=mcp))
    assert f.level == "fail" and "1 active MCP server(s): tablet" in f.value and "mail" not in f.value
    assert "GROK_ENABLED=false" in f.remedy


def test_compat_many_active_mcp_servers_are_summarised(box):
    mcp = [{"name": f"srv{i}"} for i in range(8)]
    f = compat(box, doc=doc_with(box, mcpServers=mcp))
    assert f.level == "fail" and "8 active" in f.value and "+3 more" in f.value and "srv7" not in f.value


def test_compat_all_servers_disabled_is_ok(box):
    mcp = [{"name": "a", "disabled": True}, {"name": "b", "disabled": True}]
    assert compat(box, doc=doc_with(box, mcpServers=mcp)).level == "ok"


def test_compat_mcp_beats_hooks_in_the_verdict(box):
    f = compat(box, doc=doc_with(box, mcpServers=[{"name": "x"}],
                                 hooks=[{"event": "e", "vendor": "claude"}]))
    assert f.level == "fail"


def test_compat_active_claude_hook_is_warn(box):
    f = compat(box, doc=doc_with(box, hooks=[{"event": "session_start", "vendor": "claude"}]))
    assert f.level == "warn" and "hook session_start" in f.value and "PLUGINS" in f.remedy


def test_compat_disabled_claude_hook_is_ok(box):
    assert compat(box, doc=doc_with(box, hooks=[{"event": "e", "vendor": "claude", "disabled": True}])).level == "ok"


def test_compat_plugin_hook_from_claudes_tree_is_warn_even_without_a_vendor_tag(box):
    hook = {"event": "(plugin)", "hookType": "file", "matcher": None,
            "target": f"{box.fake_home}/.claude-accounts/work/plugins/cache/p/hooks/hooks.json",
            "source": {"type": "plugin", "plugin_name": "p", "path": f"{box.fake_home}/.claude-accounts/work"}}
    f = compat(box, doc=doc_with(box, hooks=[hook]))
    assert f.level == "warn" and "hook (plugin)" in f.value


def test_compat_plugin_skill_loaded_from_dot_claude_is_warn(box):
    skill = {"name": "brainstorming", "source": {"type": "plugin",
                                                  "path": f"{box.fake_home}/.claude/plugins/x/SKILL.md"}}
    f = compat(box, doc=doc_with(box, skills=[skill]))
    assert f.level == "warn" and "skill brainstorming" in f.value


def test_compat_cursor_origin_by_path_is_warn(box):
    skill = {"name": "cs", "source": {"type": "user", "path": f"{box.fake_home}/.cursor/skills/cs/SKILL.md"}}
    assert compat(box, doc=doc_with(box, skills=[skill])).level == "warn"


def test_compat_the_agents_dir_skills_that_stay_on_by_design_are_not_flagged(box):
    """§L10: ~/.agents/skills has no off switch; they are neither Claude's nor Cursor's."""
    skills = [{"name": f"s{i}", "source": {"type": "user", "path": f"{box.fake_home}/.agents/skills/s{i}/SKILL.md"}}
              for i in range(15)]
    assert compat(box, doc=doc_with(box, skills=skills)).level == "ok"


def test_compat_a_sibling_directory_named_like_dot_claude_is_not_foreign(box):
    skill = {"name": "x", "source": {"type": "user", "path": f"{box.fake_home}/.claude-notes/x/SKILL.md"}}
    assert compat(box, doc=doc_with(box, skills=[skill])).level == "ok"


def test_compat_claude_vendor_skill_and_agent_are_warn(box):
    f = compat(box, doc=doc_with(box, skills=[{"name": "s", "vendor": "claude"}],
                                 agents=[{"name": "a", "vendor": "cursor"}]))
    assert f.level == "warn" and "skill s" in f.value and "agent a" in f.value


def test_compat_claude_json_sourced_agent_is_warn(box):
    f = compat(box, doc=doc_with(box, agents=[{"name": "a", "source": {"type": "claudeJson"}}]))
    assert f.level == "warn" and "agent a" in f.value


def test_compat_disabled_claude_skill_is_ok(box):
    assert compat(box, doc=doc_with(box, skills=[{"name": "s", "vendor": "claude", "disabled": True}])).level == "ok"


@pytest.mark.parametrize("surface", ["skills", "rules", "hooks", "mcps", "mcp", "agents"])
def test_compat_a_claude_compat_switch_left_on_is_warn(box, surface):
    cells = [{"vendor": "claude", "surface": surface, "enabled": True, "source": "default"}]
    f = compat(box, doc=doc_with(box, externalCompat={"cells": cells}))
    assert f.level == "warn" and f"claude/{surface}" in f.value


def test_compat_session_import_cells_and_other_vendors_are_not_flagged(box):
    cells = [{"vendor": "claude", "surface": "sessions", "enabled": True},
             {"vendor": "codex", "surface": "skills", "enabled": True},
             {"vendor": "cursor", "surface": "skills", "enabled": False}]
    assert compat(box, doc=doc_with(box, externalCompat={"cells": cells})).level == "ok"


def test_compat_leaked_items_are_summarised(box):
    skills = [{"name": f"s{i}", "vendor": "claude"} for i in range(8)]
    f = compat(box, doc=doc_with(box, skills=skills))
    assert "+3 more" in f.value and "s7" not in f.value


@pytest.mark.parametrize("drop", ["mcpServers", "hooks", "skills", "agents", "externalCompat"])
def test_compat_missing_key_is_unrecognised_never_zero_found(box, drop):
    doc = doc_with(box)
    doc.pop(drop)
    f = compat(box, doc=doc)
    assert f.level == "warn" and "unrecognised" in f.value


@pytest.mark.parametrize("bad", [{"mcpServers": {"a": 1}}, {"hooks": "none"}, {"skills": ["str"]},
                                 {"mcpServers": {}}, {"hooks": ""},
                                 {"agents": None}, {"externalCompat": []}, {"externalCompat": {"cells": {}}},
                                 {"externalCompat": {"cells": ["x"]}}, {"externalCompat": {}}])
def test_compat_wrong_shapes_are_unrecognised(box, bad):
    f = compat(box, doc=doc_with(box, **bad))
    assert f.level == "warn" and "unrecognised" in f.value


def test_compat_non_dict_document_is_unrecognised(box):
    f = compat(box, doc=["a", "list"])
    assert f.level == "warn" and "unrecognised" in f.value


def test_compat_garbage_output_is_unrecognised(box):
    f = compat(box, inspect_mode="garbage")
    assert f.level == "warn" and "unrecognised" in f.value


def test_compat_nonzero_exit_warns_with_a_stderr_snippet(box):
    f = compat(box, inspect_mode="exit", stderr="boom: something broke")
    assert f.level == "warn" and "exit 3" in f.value and "boom: something broke" in f.value


def test_compat_hang_degrades_to_a_bounded_warning_and_kills_the_group(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_CMD_TIMEOUT_SEC", 0.6)
    t0 = time.monotonic()
    f = compat(box, inspect_mode="hang")
    assert time.monotonic() - t0 < 5
    assert f.level == "warn" and "could not inspect" in f.value
    gc = int(box.pidfile.read_text())
    deadline = time.monotonic() + 3
    while not pid_gone(gc) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_gone(gc)


def test_compat_with_the_engines_own_fake_degrades_to_warn(box):
    """tests/fake_grok_acp.py has no `inspect`: exit 2 must read as 'could not inspect'."""
    box.use_acp_fake()
    f = box.probe(proc_root=box.tmp / "noproc")["Grok compat"]
    assert f.level == "warn" and "could not inspect (exit 2)" in f.value


def test_compat_is_not_checked_without_a_usable_cli(box):
    box.fake(version="garbage")
    f = box.probe(proc_root=box.tmp / "noproc")["Grok compat"]
    assert f.level == "info" and "no usable Grok CLI" in f.value
    assert [c["args"] for c in box.calls()] == [["--version"]]


def test_compat_does_not_need_a_real_home(box):
    shutil.rmtree(box.home)
    assert box.probe(proc_root=box.tmp / "noproc")["Grok compat"].level == "ok"


# ─────────────────────────── Grok processes ──────────────────────────────────────

def agent(pid, age, home, **kw):
    return {"pid": pid, "argv": kw.pop("argv", AGENT_ARGV), "age": age,
            "environ": kw.pop("environ", {"GROK_HOME": str(home), "PATH": "/bin"}), **kw}


def procs_fact(box, procs, **kw):
    root = fake_proc(box.tmp / f"proc{len(list(box.tmp.glob('proc*')))}", procs, **kw)
    return box.probe(proc_root=root)["Grok processes"]


def test_processes_none_is_ok(box):
    f = procs_fact(box, [])
    assert f.level == "ok" and "no `grok agent`" in f.value


def test_processes_young_turns_are_in_flight_not_a_problem(box):
    f = procs_fact(box, [agent(500, 30, box.home), agent(501, 14 * 60, box.home)])
    assert f.level == "ok" and "2 turn(s) in flight" in f.value and "oldest 14m" in f.value


def test_processes_leftover_older_than_15_minutes_is_fail(box):
    f = procs_fact(box, [agent(500, 3 * 3600 + 120, box.home, pgid=4242)])
    assert f.level == "fail" and "pid 500" in f.value and "3h 2m" in f.value
    assert "kill -TERM -- -4242" in f.remedy and "journalctl" in f.remedy


def test_processes_boundary_is_exactly_fifteen_minutes(box):
    assert procs_fact(box, [agent(500, doctor.GROK_AGENT_MAX_AGE_SEC - 1, box.home)]).level == "ok"
    assert procs_fact(box, [agent(500, doctor.GROK_AGENT_MAX_AGE_SEC, box.home)]).level == "fail"


def test_processes_one_old_among_young_is_fail_and_names_only_the_old(box):
    f = procs_fact(box, [agent(500, 40, box.home), agent(501, 7200, box.home)])
    assert f.level == "fail" and "1 leftover" in f.value and "pid 501" in f.value and "pid 500" not in f.value


def test_processes_of_another_grok_home_are_not_ours(box):
    f = procs_fact(box, [agent(500, 7200, box.tmp / "operator-home")])
    assert f.level == "ok"


def test_processes_without_a_grok_home_in_their_environ_are_not_ours(box):
    f = procs_fact(box, [agent(500, 7200, box.home, environ={"PATH": "/bin"})])
    assert f.level == "ok"


def test_processes_home_is_compared_after_resolving_symlinks(box):
    link = box.tmp / "home-link"
    link.symlink_to(box.home)
    assert procs_fact(box, [agent(500, 7200, link)]).level == "fail"


def test_processes_with_an_unreadable_environ_are_counted(box):
    f = procs_fact(box, [agent(500, 7200, box.home, environ=None)])
    assert f.level == "fail"


def test_processes_that_are_not_the_engines_argv_are_ignored(box):
    for argv in (["grok", "agent", "stdio"], ["grok", "--no-leader", "stdio"], ["grok", "agent", "--no-leader"],
                 ["grok"], ["vim", "notes.txt"]):
        assert procs_fact(box, [agent(500, 7200, box.home, argv=argv)]).level == "ok", argv


def test_processes_zombies_are_ignored(box):
    assert procs_fact(box, [agent(500, 7200, box.home, state="Z")]).level == "ok"


def test_processes_age_unreadable_warns(box):
    root = fake_proc(box.tmp / "procx", [agent(500, 30, box.home)])
    (root / "uptime").unlink()
    f = box.probe(proc_root=root)["Grok processes"]
    assert f.level == "warn" and "age unreadable" in f.value


def test_processes_non_numeric_entries_and_broken_stat_are_skipped(box):
    root = fake_proc(box.tmp / "procy", [agent(500, 30, box.home), agent(501, 30, box.home)])
    (root / "self").mkdir()
    (root / "501" / "stat").write_text("garbage")
    f = box.probe(proc_root=root)["Grok processes"]
    assert f.level == "ok" and "1 turn(s)" in f.value


def test_processes_real_proc_real_child_age_arithmetic(box, monkeypatch):
    """The /proc parsing against a real process: start-time ticks vs uptime."""
    env = {**os.environ, "GROK_HOME": str(box.home)}
    child = subprocess.Popen([str(box.bin), "agent", "--no-leader", "stdio"], env=env,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not box.pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(1.2)
        found = doctor._list_grok_agents(box.home)
        mine = [p for p in found if p["pid"] == child.pid]
        assert mine and 1.0 <= mine[0]["age"] < 30 and mine[0]["pgid"] == child.pid
        assert doctor._list_grok_agents(box.tmp / "another-home") == []
        f = box.probe(proc_root=Path("/proc"))["Grok processes"]
        assert f.level == "ok" and "1 turn(s) in flight" in f.value
        monkeypatch.setattr(doctor, "GROK_AGENT_MAX_AGE_SEC", 1.0)
        assert box.probe(proc_root=Path("/proc"))["Grok processes"].level == "fail"
    finally:
        os.killpg(child.pid, 9)
        child.wait()


def test_processes_missing_proc_root_means_none(box):
    assert box.probe(proc_root=box.tmp / "no-such-proc")["Grok processes"].level == "ok"


# ─────────────────────────── Grok litter ─────────────────────────────────────────

def litter_fact(box, n, *, prefix="sandbox-blocked"):
    box.home.mkdir(exist_ok=True)
    for i in range(n):
        (box.home / f"{prefix}.{9000 + i}").write_text("")
    return box.probe(proc_root=box.tmp / "noproc")["Grok litter"]


def test_litter_none_is_ok(box):
    f = litter_fact(box, 0)
    assert f.level == "ok" and f.value.startswith("0 ")


def test_litter_at_the_limit_is_ok(box):
    assert litter_fact(box, doctor.GROK_LITTER_WARN).level == "ok"


def test_litter_over_the_limit_warns_with_the_cleanup_command(box):
    f = litter_fact(box, doctor.GROK_LITTER_WARN + 1)
    assert f.level == "warn" and f"{doctor.GROK_LITTER_WARN + 1} sandbox-blocked*" in f.value
    assert "reap_litter" in f.remedy and repr(str(box.home)) in f.remedy


def test_litter_counts_the_dir_variant_too_and_ignores_other_files(box):
    box.home.mkdir(exist_ok=True)
    for i in range(doctor.GROK_LITTER_WARN + 1):
        (box.home / f"sandbox-blocked-dir.{i}").mkdir()
    (box.home / "sessions").mkdir()
    (box.home / "auth.json").write_text("{}")
    assert box.probe(proc_root=box.tmp / "noproc")["Grok litter"].level == "warn"


def test_litter_unrelated_files_do_not_count(box):
    box.home.mkdir(exist_ok=True)
    for i in range(doctor.GROK_LITTER_WARN + 5):
        (box.home / f"other.{i}").write_text("")
    assert box.probe(proc_root=box.tmp / "noproc")["Grok litter"].level == "ok"


def test_litter_in_a_missing_home_is_zero(box):
    shutil.rmtree(box.home)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok litter"]
    assert f.level == "ok" and f.value.startswith("0 ")


# ─────────────────────────── Grok usage files ────────────────────────────────────

def test_usage_files_absent_is_silent(box):
    assert "Grok usage files" not in box.probe(proc_root=box.tmp / "noproc")


def test_usage_files_small_are_informational_ok(box):
    (box.data / "grok_usage.jsonl").write_text("x" * 2048)
    (box.data / "grok_limit_errors.jsonl").write_text("y" * 100)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok usage files"]
    assert f.level == "ok" and "grok_usage.jsonl 2.0KB" in f.value and "grok_limit_errors.jsonl 100B" in f.value


def test_usage_files_only_one_present(box):
    (box.data / "grok_limit_errors.jsonl").write_text("y" * 100)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok usage files"]
    assert f.level == "ok" and "grok_usage" not in f.value


def test_usage_file_over_its_limit_warns(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_USAGE_WARN_BYTES", 1000)
    (box.data / "grok_usage.jsonl").write_text("x" * 1001)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok usage files"]
    assert f.level == "warn" and "too large: grok_usage.jsonl" in f.value and "archive" in f.remedy


def test_usage_file_exactly_at_its_limit_is_ok(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_USAGE_WARN_BYTES", 1000)
    (box.data / "grok_usage.jsonl").write_text("x" * 1000)
    assert box.probe(proc_root=box.tmp / "noproc")["Grok usage files"].level == "ok"


def test_limit_errors_file_has_its_own_smaller_limit(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_LIMIT_ERRORS_WARN_BYTES", 500)
    (box.data / "grok_usage.jsonl").write_text("x" * 1001)           # fine for the usage limit
    (box.data / "grok_limit_errors.jsonl").write_text("y" * 501)
    f = box.probe(proc_root=box.tmp / "noproc")["Grok usage files"]
    assert f.level == "warn" and "grok_limit_errors.jsonl" in f.value.split("too large:")[1]
    assert "grok_usage.jsonl" not in f.value.split("too large:")[1]


def test_default_usage_limits_are_sane():
    assert doctor.GROK_USAGE_WARN_BYTES >= 1024 * 1024 and doctor.GROK_LIMIT_ERRORS_WARN_BYTES >= 256 * 1024


# ─────────────────────────── plumbing: overlay, isolation, import failure, render ─

def test_env_overlay_applies_and_restores_exactly(monkeypatch):
    monkeypatch.setenv("GROK_HOME", "/orig/home")
    monkeypatch.setenv("GROK_ONLY_IN_PROCESS", "leftover")
    monkeypatch.delenv("GROK_ONLY_IN_ENV", raising=False)
    monkeypatch.setenv("UNRELATED_KEY", "keep")
    monkeypatch.setenv("PATH", "/orig/path")
    monkeypatch.setenv("HOME", "/orig/home-dir")
    env = {"GROK_HOME": "/dotenv/home", "GROK_ONLY_IN_ENV": "yes", "UNRELATED_KEY": "changed",
           "PATH": "/p", "HOME": "/h"}
    with doctor._env_overlay(env):
        assert os.environ["GROK_HOME"] == "/dotenv/home"
        assert os.environ["GROK_ONLY_IN_ENV"] == "yes"
        assert "GROK_ONLY_IN_PROCESS" not in os.environ              # env is authoritative for GROK_*
        assert os.environ["UNRELATED_KEY"] == "keep"                 # only the keys the engine reads
        assert os.environ["PATH"] == "/p" and os.environ["HOME"] == "/h"
    assert os.environ["GROK_HOME"] == "/orig/home"
    assert os.environ["GROK_ONLY_IN_PROCESS"] == "leftover"
    assert "GROK_ONLY_IN_ENV" not in os.environ
    assert os.environ["UNRELATED_KEY"] == "keep"
    assert os.environ["PATH"] == "/orig/path" and os.environ["HOME"] == "/orig/home-dir"


def test_env_overlay_never_unsets_path_or_home(monkeypatch):
    monkeypatch.setenv("PATH", "/orig/path")
    monkeypatch.setenv("HOME", "/orig/home-dir")
    with doctor._env_overlay({"GROK_HOME": "/x"}):
        assert os.environ["PATH"] == "/orig/path" and os.environ["HOME"] == "/orig/home-dir"


def test_env_overlay_restores_after_an_exception(monkeypatch):
    monkeypatch.setenv("GROK_HOME", "/orig/home")
    with pytest.raises(RuntimeError):
        with doctor._env_overlay({"GROK_HOME": "/other"}):
            raise RuntimeError("boom")
    assert os.environ["GROK_HOME"] == "/orig/home"


def test_probe_uses_the_env_it_is_given_not_the_process_env(box, monkeypatch):
    other = box.tmp / "elsewhere-home"
    other.mkdir()
    monkeypatch.setenv("GROK_HOME", str(other))                      # the process says "elsewhere" ...
    f = box.probe(proc_root=box.tmp / "noproc")["Grok sandbox (GROK_HOME)"]
    assert f.value == str(box.home)                                  # ... the merged env wins
    assert os.environ["GROK_HOME"] == str(other)


def test_data_dir_defaults_to_the_repo_data_dir(box):
    box.env.pop("_CARDLOOP_DATA_DIR")
    (box.tmp / "data").mkdir(exist_ok=True)
    (box.tmp / "data" / "grok_usage.jsonl").write_text("x")
    assert "Grok usage files" in box.probe(proc_root=box.tmp / "noproc")


def test_one_crashing_step_does_not_hide_the_others(box, monkeypatch):
    def boom(g):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(doctor, "_grok_usage_files", boom)
    by = box.probe(proc_root=box.tmp / "noproc")
    assert by["Grok usage files"].level == "warn" and "kaboom" in by["Grok usage files"].value
    assert "Grok CLI" in by and "Grok auth" in by and "Grok processes" in by


def test_unimportable_engine_is_a_single_warning(box, monkeypatch):
    monkeypatch.setitem(sys.modules, "grok_engine", None)            # `import grok_engine` -> ImportError
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc")
    assert [f.label for f in facts] == ["Grok"] and facts[0].level == "warn"
    assert "cannot be imported" in facts[0].value and os.environ["GROK_HOME"] == str(box.home)


def test_run_group_returns_output_and_exit_code(tmp_path):
    out = doctor._run_group([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('e\\n'); sys.exit(4)"])
    assert out == (4, "hi", "e")


def test_run_group_missing_binary_is_none():
    assert doctor._run_group(["/nonexistent/definitely-not-here"]) is None


def test_run_group_timeout_is_none_and_the_whole_group_dies(tmp_path):
    pidfile = tmp_path / "gc.pid"
    code = ("import subprocess, sys, time\n"
            f"p = subprocess.Popen([{SLEEP!r}, '300'])\nopen({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(300)\n")
    t0 = time.monotonic()
    assert doctor._run_group([sys.executable, "-c", code], timeout=0.8) is None
    assert time.monotonic() - t0 < 5
    gc = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while not pid_gone(gc) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_gone(gc)


def test_run_group_kills_a_straggler_even_after_a_clean_exit(tmp_path):
    pidfile = tmp_path / "gc.pid"
    code = ("import subprocess, sys\n"
            f"p = subprocess.Popen([{SLEEP!r}, '300'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\nprint('done')\n")
    assert doctor._run_group([sys.executable, "-c", code], timeout=5) == (0, "done", "")
    gc = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while not pid_gone(gc) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_gone(gc)


@pytest.mark.parametrize("sec,text", [(5, "5s"), (89, "89s"), (90, "1m"), (600, "10m"), (5399, "89m"),
                                      (5400, "1h 30m"), (3 * 3600 + 120, "3h 2m"), (-5, "0s")])
def test_fmt_age(sec, text):
    assert doctor._fmt_age(sec) == text


def test_the_grok_section_is_hidden_when_empty_and_the_output_is_unchanged():
    sections = {name: [doctor.Fact("x", "v")] for name in doctor.CORE_SECTIONS} | {"Grok": []}
    text = doctor.render_text(sections, [], elapsed=0.1)
    assert "== Grok ==" not in text
    assert [l for l in text.splitlines() if l.startswith("== ")] == [f"== {n} ==" for n in doctor.CORE_SECTIONS] + ["== Verdict =="]
    parsed = json.loads(doctor.render_json(sections, [], elapsed=0.1, exit_code=0))
    assert list(parsed["sections"]) == list(doctor.CORE_SECTIONS)


def test_the_grok_section_shows_between_load_and_the_verdict_when_it_has_facts():
    sections = {name: [doctor.Fact("x", "v")] for name in doctor.CORE_SECTIONS}
    sections["Grok"] = [doctor.Fact("Grok CLI", "1.0.46"), doctor.Fact("Grok auth", "no login", level="fail",
                                                                     remedy="tools/grok-acct login")]
    text = doctor.render_text(sections, [], elapsed=0.1)
    assert text.index("== Load ==") < text.index("== Grok ==") < text.index("== Verdict ==")
    assert "✗ [Grok] Grok auth: no login" in text and "-> tools/grok-acct login" in text
    parsed = json.loads(doctor.render_json(sections, [], elapsed=0.1, exit_code=1))
    assert list(parsed["sections"]) == list(doctor.CORE_SECTIONS) + ["Grok"]
    assert parsed["sections"]["Grok"][1] == {"label": "Grok auth", "value": "no login", "level": "fail",
                                              "remedy": "tools/grok-acct login"}
    finding = parsed["verdict"]["findings"][0]
    assert finding["section"] == "Grok" and finding["level"] == "fail"


def test_a_grok_failure_drives_the_exit_code(monkeypatch, capsys):
    sections = {name: [] for name in doctor.SECTIONS}
    sections["Grok"] = [doctor.Fact("Grok sandbox (probe)", "FAILED", level="fail")]
    monkeypatch.setattr(doctor, "collect", lambda repo_root: (sections, []))
    assert doctor.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"]["exit_code"] == 1


def test_a_real_run_of_every_step_is_fast(box):
    t0 = time.monotonic()
    box.ensure_home()
    box.write_probe("ok")
    facts = box.probe(proc_root=box.tmp / "noproc")
    assert time.monotonic() - t0 < 3
    assert {f.level for f in facts.values()} <= {"ok", "info"}, {k: v.level for k, v in facts.items()}


# ─────────────────────────── second round: gaps found by mutation testing ─────────

def test_run_group_leaves_no_zombie_behind_after_a_timeout():
    def zombie_children():
        out = []
        for e in os.listdir("/proc"):
            if e.isdigit():
                try:
                    rest = Path(f"/proc/{e}/stat").read_text().rsplit(")", 1)[1].split()
                except OSError:
                    continue
                if rest[0] == "Z" and int(rest[1]) == os.getpid():
                    out.append(int(e))
        return out
    before = set(zombie_children())
    assert doctor._run_group([sys.executable, "-c", "import time; time.sleep(300)"], timeout=0.5) is None
    assert set(zombie_children()) <= before, "a timed-out probe must be reaped, not left as a zombie"


def test_agents_listing_resolves_a_symlinked_home_argument(box):
    link = box.tmp / "home-link"
    link.symlink_to(box.home)
    root = fake_proc(box.tmp / "procz", [agent(500, 30, box.home)])      # environ carries the REAL path
    assert [p["pid"] for p in doctor._list_grok_agents(link, root)] == [500]


def test_agents_listing_survives_a_missing_clock_tick_rate(box, monkeypatch):
    def broken(name):
        raise ValueError(name)
    monkeypatch.setattr(os, "sysconf", broken)
    root = fake_proc(box.tmp / "procw", [agent(500, 100, box.home)])
    # fake_proc wrote start ticks with the host's rate; the fallback assumes the usual 100
    (root / "500" / "stat").write_text(
        f"500 (grok) S 1 500 500 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 {int((100000.0 - 100) * 100)} 0 0\n")
    [p] = doctor._list_grok_agents(box.home, root)
    assert 99 <= p["age"] <= 101


def test_compat_hook_matched_by_its_target_alone(box):
    hook = {"event": "pre_tool_use", "target": f"\"{box.fake_home}/.claude/hooks/x.sh\"",
            "source": {"type": "user", "path": "/somewhere/else"}}
    f = compat(box, doc=doc_with(box, hooks=[hook]))
    assert f.level == "warn" and "hook pre_tool_use" in f.value


def test_compat_skill_matched_by_its_source_path_alone(box):
    skill = {"name": "p", "target": "/elsewhere", "source": {"type": "plugin",
                                                              "path": f"{box.fake_home}/.claude/plugins/p"}}
    assert compat(box, doc=doc_with(box, skills=[skill])).level == "warn"


def test_compat_active_server_without_a_name_is_still_counted(box):
    f = compat(box, doc=doc_with(box, mcpServers=[{"transport": "stdio"}]))
    assert f.level == "fail" and "1 active MCP server(s): ?" in f.value


def test_compat_reports_leaked_items_and_switches_together(box):
    cells = [{"vendor": "cursor", "surface": "rules", "enabled": True}]
    f = compat(box, doc=doc_with(box, skills=[{"name": "s", "vendor": "claude"}],
                                 externalCompat={"cells": cells}))
    assert f.level == "warn" and "skill s" in f.value and "cursor/rules" in f.value and "; " in f.value


def test_processes_many_leftovers_name_only_the_first_five(box):
    f = procs_fact(box, [agent(500 + i, 7200 + i, box.home) for i in range(7)])
    assert f.level == "fail" and "7 leftover" in f.value and f.value.count("pid ") == 5


def test_processes_oldest_in_flight_is_the_maximum(box):
    f = procs_fact(box, [agent(500, 600, box.home), agent(501, 60, box.home)])
    assert "oldest 10m" in f.value


def test_auth_without_a_secrets_sink_still_works(box):
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc")
    assert next(f for f in facts if f.label == "Grok auth").level == "ok"


def test_probe_age_is_measured_from_the_recorded_timestamp(box):
    box.ensure_home()
    box.write_probe("ok", age=7200)
    f = probe_fact(box)
    assert f.level == "ok" and "2h 0m ago" in f.value
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc",
                              now=lambda: time.time() + 3 * 3600)
    assert next(f for f in facts if f.label == "Grok sandbox (probe)").value.startswith("ok 5h 0m ago")


def test_run_group_never_waits_on_the_terminal(tmp_path):
    """A probe that reads stdin must see EOF at once, not block on whatever doctor inherited."""
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)                                  # fd 0 is now a pipe that never delivers anything
    try:
        t0 = time.monotonic()
        out = doctor._run_group([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=4)
    finally:
        os.dup2(saved, 0)
        for fd in (r, w, saved):
            os.close(fd)
    assert out == (0, "''", "") and time.monotonic() - t0 < 3


def test_processes_truncated_stat_line_is_skipped(box):
    root = fake_proc(box.tmp / "procv", [agent(500, 30, box.home), agent(501, 30, box.home)])
    (root / "501" / "stat").write_text("501 (grok) S 1 501")
    f = box.probe(proc_root=root)["Grok processes"]
    assert f.level == "ok" and "1 turn(s)" in f.value


def test_compat_long_lists_show_exactly_five_names(box):
    f = compat(box, doc=doc_with(box, mcpServers=[{"name": f"srv{i}"} for i in range(8)]))
    assert all(f"srv{i}" in f.value for i in range(5)) and "srv5" not in f.value and "+3 more" in f.value


def test_compat_exactly_five_names_have_no_more_suffix(box):
    f = compat(box, doc=doc_with(box, mcpServers=[{"name": f"srv{i}"} for i in range(5)]))
    assert "srv4" in f.value and "more" not in f.value


def test_core_sections_always_print_even_when_empty():
    sections = {name: [] for name in doctor.SECTIONS}
    text = doctor.render_text(sections, [], elapsed=0.1)
    for name in doctor.CORE_SECTIONS:
        assert f"== {name} ==" in text
    assert "== Grok ==" not in text
    parsed = json.loads(doctor.render_json(sections, [], elapsed=0.1, exit_code=0))
    assert list(parsed["sections"]) == list(doctor.CORE_SECTIONS)


# ═══════════════ spec-095 P1b: folder trust + per-project compat (the .mcp.json hole) ═══════════════
#
# `grok inspect --json` lists a project's own `.mcp.json` servers as ACTIVE, yet a turn started none
# of them (measured live with marker files): folder trust gates them. So the per-project fact judges
# `projectTrusted` + what is listed, run IN the project under the engine's sandbox profile.

def _topics(box, records: "dict | list") -> None:
    (box.data / "topics.json").write_text(json.dumps(records))


def _project(box, name: str) -> Path:
    d = box.tmp / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _opt_in(box, *names: str, **flags) -> "dict[str, Path]":
    """Projects that USE Grok: their board default is Grok (the doctor checks where Grok actually runs)."""
    dirs = {n: _project(box, n) for n in names}
    _topics(box, {f"k{i}": {"project": n, "cwd": str(d), "board_provider": "grok", **flags}
                  for i, (n, d) in enumerate(dirs.items())})
    return dirs


def _doc(box, **over) -> dict:
    """What a sandboxed inspect shows once the engine's config.toml hid ~/.agents: no user skills."""
    over.setdefault("skills", [])
    return inspect_doc(str(box.fake_home), **over)


def _proj_calls(box) -> "list[dict]":
    return [c for c in box.calls() if c["args"] == ["inspect", "--json"] and "projects" in c["cwd"]]


def _mcp(name: str, *, disabled: bool = False, kind: str = "mcpJson", path: str = "/p/.mcp.json") -> dict:
    rec = {"name": name, "transport": "stdio", "target": "/opt/x", "source": {"type": kind, "path": path}}
    if disabled:
        rec.update(disabled=True, compatibilityStatus="disabled")
    return rec


LABEL = "Grok compat (projects)"


def _fake(box, **kw) -> None:
    """The fake grok, with the isolated document as the default for every project."""
    kw.setdefault("doc", _doc(box))
    box.fake(**kw)


def test_projects_fact_is_silent_without_a_readable_registry(box):
    for setup in (lambda: None, lambda: (box.data / "topics.json").write_text("{not json"),
                  lambda: _topics(box, ["a", "list"]), lambda: _topics(box, "text")):
        setup()
        facts = box.probe(proc_root=box.tmp / "noproc")
        assert LABEL not in facts
    assert _proj_calls(box) == []


def test_projects_fact_says_so_when_no_project_uses_grok(box):
    _topics(box, {"k": {"project": "P", "cwd": str(_project(box, "p")), "board_provider": "claude"}})
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "info" and "no project uses Grok yet" in f.value
    assert _proj_calls(box) == []


@pytest.mark.parametrize("rec", [{"board_provider": "grok"}, {"grok_model": "grok-4.7"}])
def test_a_project_counts_as_using_grok_by_its_board_default_or_its_model(box, rec):
    _fake(box)
    _topics(box, {"k": {"project": "P", "cwd": str(_project(box, "p")), **rec}})
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "1 project(s) that use Grok" in f.value and len(_proj_calls(box)) == 1


@pytest.mark.parametrize("rec", [{}, {"board_provider": "codex"}, {"grok_model": ""}, {"grok_model": None}])
def test_a_project_without_any_sign_of_grok_is_not_inspected(box, rec):
    _fake(box)
    _topics(box, {"k": {"project": "P", "cwd": str(_project(box, "p")), **rec}})
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "info" and _proj_calls(box) == []


def test_a_project_with_a_grok_chat_counts_as_using_grok(box):
    _fake(box)
    _topics(box, {"a": {"project": "WithChat", "cwd": str(_project(box, "wc"))},
                  "b": {"project": "Other", "cwd": str(_project(box, "ot"))}})
    (box.data / "chats.json").write_text(json.dumps({
        "WithChat": {"active": "c1", "chats": [{"id": "c0", "provider": "claude"}, {"id": "c1", "provider": "grok"}]},
        "Other": {"active": "c2", "chats": [{"id": "c2", "provider": "codex"}]}}))
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "1 project(s)" in f.value
    assert [Path(c["cwd"]).name for c in _proj_calls(box)] == ["wc"]


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"P": "x"}', '{"P": {"chats": "no"}}', '{"P": {"chats": ["x", 3]}}'])
def test_an_unreadable_or_odd_chat_store_never_breaks_the_check(box, raw):
    _fake(box)
    _topics(box, {"k": {"project": "P", "cwd": str(_project(box, "p"))}})
    (box.data / "chats.json").write_text(raw)
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "info" and _proj_calls(box) == []


def test_an_isolated_project_is_ok_and_inspected_in_its_own_dir_under_the_sandbox(box):
    _fake(box)
    box.make_import_surface()
    box.ensure_home()
    dirs = _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "1 project(s)" in f.value and "isolated" in f.value
    (call,) = _proj_calls(box)
    assert call["cwd"] == str(dirs["alpha"].resolve())
    assert call["env"]["GROK_SANDBOX"] == "cardloop"                  # the production view, not the bare one
    assert call["env"]["GROK_FOLDER_TRUST"] == "1"
    assert call["home"] != str(box.home) and "doctor-grok-" in call["home"]
    toml = tomllib.loads(call["sandbox_toml"])
    assert str(box.fake_home / ".claude") in toml["profiles"]["cardloop"]["deny"]
    assert tomllib.loads(call["config"])["skills"]["ignore"] == [str(box.fake_home / ".agents")]
    assert not Path(call["home"]).exists(), "the throwaway home must be gone afterwards"


def test_the_project_view_uses_the_engines_config_even_before_it_was_generated(box):
    _fake(box)
    assert not (box.home / "config.toml").exists()
    _opt_in(box, "alpha")
    box.probe(proc_root=box.tmp / "noproc")
    (call,) = _proj_calls(box)
    assert tomllib.loads(call["config"])["skills"]["disabled"] == ["resume-claude", "resume-codex", "resume-cursor"]


def test_the_real_trust_store_is_what_the_project_inspect_runs_against(box):
    _fake(box)
    box.ensure_home()
    (box.home / "trusted_folders.toml").write_text("# empty\n")
    _opt_in(box, "alpha")
    box.probe(proc_root=box.tmp / "noproc")
    (call,) = _proj_calls(box)
    assert call["trust_store"] == "# empty\n"


def test_project_mcp_listed_while_the_folder_is_untrusted_is_gated_not_a_problem(box):
    doc = _doc(box, mcpServers=[_mcp("tablet")], projectTrusted=False)
    _fake(box, docs_by_cwd={"alpha": doc})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "tablet" in f.value and "gated by folder trust" in f.value


def test_a_trusted_project_folder_is_fail_and_names_what_would_start(box):
    doc = _doc(box, projectTrusted=True, mcpServers=[_mcp("tablet"), _mcp("off", disabled=True)],
               hooks=[{"event": "session_start", "source": {"type": "project", "path": "/p/.grok/hooks"}}])
    _fake(box, docs_by_cwd={"alpha": doc})
    _opt_in(box, "alpha", "beta")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "fail" and "alpha" in f.value and "TRUSTED" in f.value
    assert "tablet" in f.value and "off" not in f.value.replace("tablet", "")
    assert "session_start (project)" in f.value
    assert "beta" not in f.value.split("other project")[0] and "1 other project(s) isolated" in f.value
    assert "trusted_folders.toml" in f.remedy


def test_a_trusted_folder_with_nothing_to_start_is_ok_not_fail(box):
    # measured live: a project with no config of its own reports projectTrusted=true whatever the trust
    # store says; failing it made `doctor` exit 1 for every plain opted-in project
    _fake(box, docs_by_cwd={"alpha": _doc(box, projectTrusted=True)})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "isolated" in f.value and "TRUSTED" not in f.value


def test_a_trusted_folder_whose_only_items_are_disabled_is_ok_too(box):
    doc = _doc(box, projectTrusted=True, mcpServers=[_mcp("off", disabled=True)],
               hooks=[{"event": "session_start", "source": {"type": "project"}, "disabled": True}])
    _fake(box, docs_by_cwd={"alpha": doc})
    _opt_in(box, "alpha")
    assert box.probe(proc_root=box.tmp / "noproc")[LABEL].level == "ok"


def test_a_trusted_folder_with_one_active_skill_is_still_fail(box):
    skill = {"name": "deploy", "source": {"type": "project"}}
    _fake(box, docs_by_cwd={"alpha": _doc(box, projectTrusted=True, skills=[skill])})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "fail" and "TRUSTED" in f.value and "skills deploy (project)" in f.value


def test_an_active_hook_under_the_sandbox_view_is_warn(box):
    hook = {"event": "session_start", "source": {"type": "plugin", "plugin_name": "x"}, "disabled": None}
    _fake(box, docs_by_cwd={"alpha": _doc(box, hooks=[hook])})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "hook(s) active" in f.value and "session_start (plugin)" in f.value


def test_a_disabled_hook_is_ignored(box):
    hook = {"event": "session_start", "source": {"type": "user"}, "vendor": "claude", "disabled": True}
    _fake(box, docs_by_cwd={"alpha": _doc(box, hooks=[hook])})
    _opt_in(box, "alpha")
    assert box.probe(proc_root=box.tmp / "noproc")[LABEL].level == "ok"


def test_a_non_bundled_active_skill_is_warn_and_bundled_or_disabled_ones_are_not(box):
    skills = [{"name": "mine", "source": {"type": "user", "path": "/h/.agents/skills/m"}},
              {"name": "off", "source": {"type": "user"}, "disabled": True},
              {"name": "review", "source": {"type": "bundled"}}]
    _fake(box, docs_by_cwd={"alpha": _doc(box, skills=skills)})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "mine (user)" in f.value and "off" not in f.value and "review" not in f.value


def test_fail_beats_warn_across_projects(box):
    hook = {"event": "session_start", "source": {"type": "plugin"}}
    _fake(box, docs_by_cwd={"alpha": _doc(box, hooks=[hook]),
                            "beta": _doc(box, projectTrusted=True, mcpServers=[_mcp("tablet")])})
    _opt_in(box, "alpha", "beta")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "fail" and "alpha" in f.value and "beta" in f.value


@pytest.mark.parametrize("drop", ["projectTrusted", "mcpServers", "hooks", "skills"])
def test_project_inspect_missing_key_is_unrecognised_never_zero_found(box, drop):
    doc = _doc(box)
    del doc[drop]
    _fake(box, docs_by_cwd={"alpha": doc})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "unrecognised" in f.value


@pytest.mark.parametrize("bad", ["yes", 1, None, "false"])
def test_project_trusted_must_be_a_real_boolean(box, bad):
    _fake(box, docs_by_cwd={"alpha": _doc(box, projectTrusted=bad)})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "unrecognised" in f.value


@pytest.mark.parametrize("mode,needle", [("garbage", "unrecognised"), ("exit", "exit 3")])
def test_project_inspect_failures_degrade_to_a_warning(box, mode, needle):
    _fake(box, per_cwd_mode={"alpha": mode}, stderr="boom from grok")
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and needle in f.value


def test_a_hung_project_inspect_is_bounded_and_its_group_killed(box, monkeypatch):
    monkeypatch.setattr(doctor, "GROK_PROJECT_INSPECT_TIMEOUT_SEC", 0.5)
    _fake(box, per_cwd_mode={"alpha": "hang"})
    _opt_in(box, "alpha", "beta")
    t0 = time.monotonic()
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert time.monotonic() - t0 < 6
    assert f.level == "warn" and "could not inspect" in f.value and "alpha" in f.value
    assert "1 other project(s) isolated" in f.value                       # beta was still judged
    gc = int(box.pidfile.read_text())
    deadline = time.monotonic() + 3
    while not pid_gone(gc) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pid_gone(gc)


def test_project_dirs_are_deduplicated_and_unusable_ones_skipped(box):
    _fake(box)
    real = _project(box, "real")
    link = box.tmp / "projects" / "link"
    link.symlink_to(real)
    G = {"board_provider": "grok"}
    _topics(box, {
        "a": {"project": "Real", "cwd": str(real), **G},
        "b": {"project": "Alias", "cwd": str(link), **G},                            # same real path
        "c": {"project": "Gone", "cwd": str(box.tmp / "nope"), **G},
        "d": {"project": "Rel", "cwd": "relative/dir", **G},
        "e": {"project": "NoCwd", **G},
        "f": "not a record",
        "i": {"project": "Off", "cwd": str(_project(box, "off"))},
    })
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "ok" and "1 project(s)" in f.value
    assert [c["cwd"] for c in _proj_calls(box)] == [str(real.resolve())]


def test_the_number_of_inspect_calls_is_capped_and_the_rest_reported(box, monkeypatch):
    _fake(box)
    monkeypatch.setattr(doctor, "GROK_PROJECTS_MAX", 3)
    _opt_in(box, *[f"p{i:02d}" for i in range(7)])
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert len(_proj_calls(box)) == 3 and "+4 more project(s) not checked, limit 3" in f.value


def test_projects_fact_writes_nothing_real(box):
    _fake(box)
    box.ensure_home()
    _opt_in(box, "alpha")
    before = snapshot(box.home, box.data, box.fake_home, box.secret_dir, box.tmp / "projects")
    box.probe(proc_root=box.tmp / "noproc")
    assert snapshot(box.home, box.data, box.fake_home, box.secret_dir, box.tmp / "projects") == before


def test_projects_fact_is_not_checked_without_a_usable_cli(box):
    _fake(box)
    box.bin.unlink()
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "info" and "no usable Grok CLI" in f.value


def test_projects_fact_with_an_invalid_deny_list_is_not_checked(box):
    _fake(box)
    box.env["GROK_SANDBOX_DENY"] = "**/{x}.pem"
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "not checked" in f.value and _proj_calls(box) == []


def test_projects_fact_crash_is_contained(box, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(doctor, "_grok_project_dirs", boom)
    facts = box.probe(proc_root=box.tmp / "noproc")
    assert facts[LABEL].level == "warn" and "probe crashed" in facts[LABEL].value
    assert "Grok CLI" in facts                                            # the other facts survived


def test_project_names_never_leak_environment_secrets(box):
    box.env["WEB_PASSWORD"] = "pw-s3cret-web-value"
    _fake(box, docs_by_cwd={"alpha": _doc(box, projectTrusted=True, mcpServers=[_mcp("tablet")])})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert "pw-s3cret-web-value" not in f.value + (f.remedy or "")
    assert "/opt/x" not in f.value                                         # the server target is never printed


# ---- Grok folder trust ----------------------------------------------------------------------

FT = "Grok folder trust"


def test_folder_trust_ok_when_pinned_and_the_store_is_empty(box):
    box.ensure_home()
    f = box.probe(proc_root=box.tmp / "noproc")[FT]
    assert f.level == "ok" and "no folder is trusted" in f.value and "GROK_FOLDER_TRUST=1" in f.value


@pytest.mark.parametrize("body", ["", "\n", "# nothing\n"])
def test_folder_trust_ignores_an_empty_or_comment_only_store(box, body):
    box.ensure_home()
    (box.home / "trusted_folders.toml").write_text(body)
    assert box.probe(proc_root=box.tmp / "noproc")[FT].level == "ok"


def test_folder_trust_fails_when_the_store_has_an_entry(box):
    box.ensure_home()
    (box.home / "trusted_folders.toml").write_text('[[folders]]\npath = "/p"\n')
    f = box.probe(proc_root=box.tmp / "noproc")[FT]
    assert f.level == "fail" and "folder trust is granted" in f.value
    assert "refuses every Grok turn" in f.remedy


def test_folder_trust_fails_when_the_pin_is_missing_from_the_child_env(box, monkeypatch):
    monkeypatch.delitem(grok_engine.D3_ENV, "GROK_FOLDER_TRUST")
    f = box.probe(proc_root=box.tmp / "noproc")[FT]
    assert f.level == "fail" and "not pinned on" in f.value and "D3_ENV" in f.remedy


def test_folder_trust_fails_when_the_pin_is_wrong(box, monkeypatch):
    monkeypatch.setitem(grok_engine.D3_ENV, "GROK_FOLDER_TRUST", "0")
    assert box.probe(proc_root=box.tmp / "noproc")[FT].level == "fail"


def test_folder_trust_unreadable_store_fails_closed(box):
    box.ensure_home()
    path = box.home / "trusted_folders.toml"
    path.write_text("")
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        f = box.probe(proc_root=box.tmp / "noproc")[FT]
        assert f.level == "fail" and "unknown" in f.value
    finally:
        os.chmod(path, 0o600)


def test_the_new_facts_reach_the_rendered_report_and_the_json(box, monkeypatch):
    _fake(box)
    _opt_in(box, "alpha")
    facts = doctor.probe_grok(box.env, repo_root=box.tmp, proc_root=box.tmp / "noproc")
    labels = [f.label for f in facts]
    assert FT in labels and LABEL in labels
    assert labels.index(FT) < labels.index(LABEL) < labels.index("Grok processes")


def test_a_relative_cwd_is_never_resolved_against_doctors_own_directory(box, monkeypatch):
    _fake(box)
    (box.tmp / "rel" / "dir").mkdir(parents=True)
    monkeypatch.chdir(box.tmp)                         # "rel/dir" EXISTS from here, but whose cwd is it?
    _topics(box, {"a": {"project": "Rel", "cwd": "rel/dir", "board_provider": "grok"}})
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "info" and _proj_calls(box) == []


@pytest.mark.parametrize("key,bad", [("mcpServers", "tablet"), ("mcpServers", {"tablet": {}}),
                                     ("hooks", {"a": 1}), ("skills", ["just-a-name"]),
                                     ("mcpServers", [1, 2]), ("hooks", [None])])
def test_project_inspect_lists_of_the_wrong_shape_are_unrecognised(box, key, bad):
    _fake(box, docs_by_cwd={"alpha": _doc(box, **{key: bad})})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert f.level == "warn" and "unrecognised" in f.value


def test_many_active_items_are_summarised(box):
    servers = [_mcp(f"srv{i}") for i in range(6)]
    _fake(box, docs_by_cwd={"alpha": _doc(box, projectTrusted=True, mcpServers=servers)})
    _opt_in(box, "alpha")
    f = box.probe(proc_root=box.tmp / "noproc")[LABEL]
    assert "srv0" in f.value and "srv3" in f.value and "srv4" not in f.value and "(+2 more)" in f.value
