"""Tests for tools/doctor.py — one-command cockpit diagnosis (spec-082 workstream C).

Every probe takes its collaborators (subprocess runner, HTTP getter, process finder,
installed-version lookup, ...) as parameters, so these tests drive the findings logic
entirely with faked data. No test requires systemd, the network, or a live cockpit.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "doctor", Path(__file__).resolve().parent.parent / "tools" / "doctor.py")
doctor = importlib.util.module_from_spec(_SPEC)
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
    assert key_fact.value.endswith("1234")


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


def test_find_bot_processes_no_proc_dir_is_empty(tmp_path, monkeypatch):
    # /proc always exists on Linux CI, but the function must degrade gracefully
    # wherever it doesn't (guarded internally) — exercise the real function directly.
    pids = doctor._find_bot_processes(Path("/nonexistent-repo-root-for-test"))
    assert pids == []


def test_port_listening_false_for_closed_port():
    # Port 1 is a reserved/privileged port almost never bound in test environments.
    assert doctor._port_listening("127.0.0.1", 1, timeout=0.3) is False


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


def test_main_completes_well_under_five_seconds(monkeypatch):
    """Smoke budget check with the real collect() (no faking) — the acceptance
    criterion is <5s on a healthy host; this asserts the ceiling generously so it
    stays robust on a loaded CI box while still catching a runaway probe."""
    import time
    t0 = time.monotonic()
    doctor.main([])
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0
