"""tools/grok_guard_hook.py: the guards of a Claude Bash call as the command hook a full-access Grok turn
runs before every shell call — the Bash deny list (same verdict, deny text and audit line as the
in-process hook) and then the operator's Claude PreToolUse hooks, run the way Claude Code runs them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import engine

HOOK = Path(__file__).resolve().parent.parent / "tools" / "grok_guard_hook.py"
sys.path.insert(0, str(HOOK.parent))
import grok_guard_hook  # noqa: E402


@pytest.fixture
def home(tmp_path):
    """A $HOME with no Claude settings, so no test depends on the operator's real hooks."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


def hermetic_env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "DENY_COMMANDS_EXTRA")}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def run_hook(payload, home: Path, *args, **env_extra):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK), *args], input=raw, capture_output=True, text=True,
                          timeout=30, env=hermetic_env(home, **env_extra))


def grok_payload(command, *, camel=False, cwd="/srv/projects/vk"):
    """The shape grok 1.0.x sends (measured 2026-10-12): both spellings, shell tool `run_terminal_command`."""
    key = "toolInput" if camel else "tool_input"
    return {"hookEventName": "pre_tool_use", "tool_name": "run_terminal_command", "cwd": cwd,
            "sessionId": "S-1", "permissionMode": "bypassPermissions",
            key: {"command": command, "description": "x"}}


def write_settings(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": entries}}))


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def denial(out) -> "str | None":
    assert out.returncode == 0, out.stderr
    if not out.stdout:
        return None
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny"
    return decision["permissionDecisionReason"]


# ---- the Bash deny list ------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git push --force origin master", "rm -rf ~", "git commit --no-verify -m x", "mkfs.ext4 /dev/sda1",
])
def test_a_denied_command_gets_the_claude_engines_verdict(home, command):
    rule, _reason = engine._classify_dangerous_command_rule(command)
    assert denial(run_hook(grok_payload(command), home)).startswith(f"Blocked [{rule}]")


def test_the_camel_case_spelling_is_read_too(home):
    assert denial(run_hook(grok_payload("git push -f origin main", camel=True), home))


@pytest.mark.parametrize("payload", [grok_payload("ls -la"), grok_payload("git push origin master"),
                                     {"tool_name": "read_file", "tool_input": {"path": "/etc/hosts"}},
                                     {"tool_input": {"command": ""}}, {"tool_input": "rm -rf /"}, [], None])
def test_anything_else_is_allowed_silently(home, payload):
    assert denial(run_hook(payload, home)) is None


@pytest.mark.parametrize("raw", ["", "not json", "{\"tool_input\": "])
def test_garbage_on_stdin_allows_and_never_crashes(home, raw):
    assert denial(run_hook(raw, home)) is None


def test_a_deny_is_audited_with_the_project_dir_name(home, tmp_path):
    audit = tmp_path / "audit"
    run_hook(grok_payload("git push --force origin master", cwd="/srv/projects/vkopp/"), home, "--audit-dir", str(audit))
    (log,) = audit.glob("audit-*.log")
    assert "[vkopp] DENY: git-force-push-protected: git push --force origin master (grok)" in log.read_text()


def test_an_allowed_command_writes_no_audit_line(home, tmp_path):
    run_hook(grok_payload("ls"), home, "--audit-dir", str(tmp_path / "audit"))
    assert not (tmp_path / "audit").exists()


def test_the_operators_extra_patterns_apply(home):
    found = grok_guard_hook.decide(grok_payload("kubectl delete ns prod"), r"kubectl\s+delete\s+ns\s+prod")
    assert found and found[1] == "extra-pattern"
    assert grok_guard_hook.decide(grok_payload("kubectl get ns"), r"kubectl\s+delete\s+ns\s+prod") is None
    assert denial(run_hook(grok_payload("kubectl delete ns prod"), home,
                           DENY_COMMANDS_EXTRA=r"kubectl\s+delete\s+ns\s+prod"))


# ---- the operator's Claude PreToolUse hooks ----------------------------------------------

def test_an_exit_2_hook_written_as_a_quoted_home_path_denies_with_its_stderr(home):
    """The spelling ~/.claude/settings.json really uses — the one Grok's own import silently skips."""
    script(home / ".claude" / "hooks" / "guard.sh", 'cat >/dev/null\necho "no self-restart" >&2\nexit 2\n')
    write_settings(home / ".claude" / "settings.json",
                   [{"matcher": "Bash", "hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/guard.sh"'}]}])
    assert denial(run_hook(grok_payload("echo hi"), home)) == "no self-restart"


def test_a_json_deny_and_a_legacy_block_both_deny(home):
    deny = script(home / "h" / "deny.sh", 'cat >/dev/null\n'
                  'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",'
                  '"permissionDecisionReason":"json says no"}}\'\n')
    write_settings(home / ".claude" / "settings.json", [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                                      "command": str(deny)}]}])
    assert denial(run_hook(grok_payload("echo hi"), home)) == "json says no"
    block = script(home / "h" / "block.sh", 'cat >/dev/null\necho \'{"decision":"block","reason":"legacy no"}\'\n')
    write_settings(home / ".claude" / "settings.json", [{"hooks": [{"type": "command", "command": str(block)}]}])
    assert denial(run_hook(grok_payload("echo hi"), home)) == "legacy no"


@pytest.mark.parametrize("body", ["cat >/dev/null\nexit 0\n", "cat >/dev/null\nexit 1\n",
                                  "cat >/dev/null\necho not-json\n",
                                  'cat >/dev/null\necho \'{"hookSpecificOutput":{"permissionDecision":"allow"}}\'\n'])
def test_a_hook_that_allows_or_fails_lets_the_call_through(home, body):
    hook = script(home / "h" / "x.sh", body)
    write_settings(home / ".claude" / "settings.json", [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                                      "command": str(hook)}]}])
    assert denial(run_hook(grok_payload("echo hi"), home)) is None


def test_the_hook_gets_the_payload_claude_would_send(home, tmp_path):
    seen = tmp_path / "seen.json"
    hook = script(home / "h" / "dump.sh", f'cat > {seen}\necho "$CLAUDE_PROJECT_DIR" > {seen}.dir\n')
    write_settings(home / ".claude" / "settings.json", [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                                      "command": str(hook)}]}])
    proj = tmp_path / "proj"
    proj.mkdir()
    run_hook(grok_payload("echo hi", cwd=str(proj)), home)
    got = json.loads(seen.read_text())
    assert got["tool_name"] == "Bash" and got["hook_event_name"] == "PreToolUse"
    assert got["tool_input"]["command"] == "echo hi"
    assert got["cwd"] == str(proj) and got["session_id"] == "S-1" and got["permission_mode"] == "bypassPermissions"
    assert (tmp_path / "seen.json.dir").read_text().strip() == str(proj)


@pytest.mark.parametrize("matcher,runs", [("Bash", True), ("Bash|Edit", True), ("*", True), ("", True),
                                          (None, True), ("Edit|Write", False), ("Bas", False), ("mcp__.*", False),
                                          ("[bad", False)])
def test_matchers_follow_claudes_rules(home, tmp_path, matcher, runs):
    marker = tmp_path / "ran"
    hook = script(home / "h" / "m.sh", f"cat >/dev/null\ntouch {marker}\n")
    entry = {"hooks": [{"type": "command", "command": str(hook)}]}
    if matcher is not None:
        entry["matcher"] = matcher
    write_settings(home / ".claude" / "settings.json", [entry])
    run_hook(grok_payload("echo hi"), home)
    assert marker.exists() is runs


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_the_projects_own_settings_are_read(home, tmp_path, name):
    proj = tmp_path / "proj"
    hook = script(tmp_path / "h" / "p.sh", f'cat >/dev/null\necho "from {name}" >&2\nexit 2\n')
    write_settings(proj / ".claude" / name, [{"matcher": "Bash", "hooks": [{"type": "command", "command": str(hook)}]}])
    assert denial(run_hook(grok_payload("echo hi", cwd=str(proj)), home)) == f"from {name}"
    assert denial(run_hook(grok_payload("echo hi", cwd=str(tmp_path / "other")), home)) is None


def test_claude_config_dir_replaces_the_home_settings(home, tmp_path):
    alt = tmp_path / "acct"
    script(home / ".claude" / "hooks" / "g.sh", 'cat >/dev/null\necho "from home" >&2\nexit 2\n')
    write_settings(home / ".claude" / "settings.json",
                   [{"matcher": "Bash", "hooks": [{"type": "command", "command": '"$HOME/.claude/hooks/g.sh"'}]}])
    hook = script(tmp_path / "h" / "acct.sh", 'cat >/dev/null\necho "from the account" >&2\nexit 2\n')
    write_settings(alt / "settings.json", [{"matcher": "Bash", "hooks": [{"type": "command", "command": str(hook)}]}])
    assert denial(run_hook(grok_payload("echo hi"), home, CLAUDE_CONFIG_DIR=str(alt))) == "from the account"


def test_the_deny_list_answers_first_and_no_operator_hook_runs(home, tmp_path):
    marker = tmp_path / "ran"
    hook = script(home / "h" / "m.sh", f"cat >/dev/null\ntouch {marker}\n")
    write_settings(home / ".claude" / "settings.json", [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                                      "command": str(hook)}]}])
    assert denial(run_hook(grok_payload("git push --force origin master"), home)).startswith("Blocked [")
    assert not marker.exists()


def test_a_hanging_hook_is_killed_at_its_timeout_and_allows(home, tmp_path):
    hook = script(home / "h" / "hang.sh", "cat >/dev/null\nsleep 30\n")
    write_settings(home / ".claude" / "settings.json",
                   [{"matcher": "Bash", "hooks": [{"type": "command", "command": str(hook), "timeout": 1}]}])
    t0 = time.monotonic()
    assert denial(run_hook(grok_payload("echo hi"), home)) is None
    assert time.monotonic() - t0 < 10


@pytest.mark.parametrize("body", ["not json", "[]", '{"hooks": "x"}', '{"hooks": {"PreToolUse": {"a": 1}}}',
                                  '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": 7}]}]}}'])
def test_malformed_settings_allow(home, body):
    (home / ".claude" / "settings.json").write_text(body)
    assert denial(run_hook(grok_payload("echo hi"), home)) is None


@pytest.mark.parametrize("declared,want", [(5, 5.0), (0.5, 0.5), (600, 60.0), (None, 60.0), (True, 60.0),
                                            (-1, 60.0), ("9", 60.0)])
def test_a_declared_timeout_lowers_the_cap_but_never_raises_it(home, monkeypatch, declared, want):
    hook = {"type": "command", "command": "true"}
    if declared is not None:
        hook["timeout"] = declared
    write_settings(home / ".claude" / "settings.json", [{"matcher": "Bash", "hooks": [hook]}])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert grok_guard_hook.operator_hooks(None) == [("true", want)]
