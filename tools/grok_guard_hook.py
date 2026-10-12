#!/usr/bin/env python3
"""PreToolUse command hook for Grok turns in full-access mode (GROK_ACCESS=full).

A Claude turn runs two kinds of guards before every Bash call: the Bash deny list, in-process
(engine._dangerous_command_guard_hook), and the operator's own PreToolUse hooks from the Claude
settings it loads (user, project, local — engine: setting_sources). A Grok turn has neither, so
grok_engine registers this script in the Grok home's config.toml and the CLI runs it before every
shell tool call:

1. the deny list: same verdict (command_guard.deny_verdict + DENY_COMMANDS_EXTRA), same deny text,
   same audit line;
2. then every Claude PreToolUse hook whose matcher matches `Bash`, run the way Claude Code runs it
   (`sh -c <command>`, a Claude-shaped payload on stdin, CLAUDE_PROJECT_DIR set). Exit 2 or a JSON
   `permissionDecision: deny` denies the call; anything else lets it through.

Why not Grok's own Claude-hook import (GROK_CLAUDE_HOOKS_ENABLED): measured 2026-10-12, it never
runs a hook whose command is a quoted path (`"$HOME/.claude/hooks/x.sh"`, the usual spelling) and
says nothing — a guard that silently does not run. Here the shell does the quoting, as in Claude.

Input: the hook JSON on stdin. Grok sends both spellings (`tool_input` and `toolInput`); its shell
tool is `run_terminal_command`, not `Bash`. Output: nothing (allow) or a Claude-format PreToolUse
deny, which Grok honours. Never fails a tool call by crashing: anything unexpected allows, exactly
like the in-process hook.

    grok_guard_hook.py [--audit-dir DIR]
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import command_guard  # noqa: E402  (stdlib only: the hook starts in ~50 ms)

# Claude Code's default per-hook timeout; a declared `timeout` (seconds) lowers it, never raises it,
# so the whole hook stays inside the timeout grok_engine gives this script.
OPERATOR_HOOK_TIMEOUT_SEC = 60.0


def _audit(audit_dir: "str | None", cwd, rule: str, command: str) -> None:
    if not audit_dir:
        return
    try:
        one = " ".join(command.split())
        one = one if len(one) <= 200 else one[:199] + "…"
        project = os.path.basename(str(cwd or "").rstrip("/")) or "unknown"
        path = Path(audit_dir)
        path.mkdir(exist_ok=True)
        with open(path / f"audit-{time.strftime('%Y-%m')}.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{project}] DENY: {rule}: {one} (grok)\n")
    except Exception:
        pass


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _tool_input(payload) -> "dict | None":
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = payload.get("toolInput")
    return tool_input if isinstance(tool_input, dict) else None


def decide(payload, extra_raw: str) -> "tuple[dict, str, str] | None":
    """(hook output, rule, command) for a command on the deny list, else None."""
    tool_input = _tool_input(payload)
    command = tool_input.get("command") if tool_input else None
    if not isinstance(command, str) or not command:
        return None
    hit = command_guard.deny_verdict(command, command_guard.compile_extra_patterns(extra_raw))
    if not hit:
        return None
    rule, reason = hit
    return (_deny(f"Blocked [{rule}]: {reason}. This shape is on the Bash deny list "
                  "and cannot run even in full-auto mode; retrying the same command "
                  "will be denied again."), rule, command)


# ------------------------------------------------------------------------------------------
# the operator's Claude PreToolUse hooks
# ------------------------------------------------------------------------------------------

def _project_dir(payload: dict) -> "str | None":
    for key in ("workspaceRoot", "cwd"):
        value = payload.get(key)
        if isinstance(value, str) and os.path.isabs(value):
            return value
    return None


def settings_files(project_dir: "str | None") -> "list[Path]":
    """Where a Claude turn reads hooks from: the user settings (CLAUDE_CONFIG_DIR, else ~/.claude) and
    the project's `.claude/settings.json` + `.claude/settings.local.json`."""
    user = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    out = [Path(user) / "settings.json"]
    if project_dir:
        out += [Path(project_dir) / ".claude" / "settings.json", Path(project_dir) / ".claude" / "settings.local.json"]
    return out


def _matches_bash(matcher) -> bool:
    """Claude's matcher rule for the tool `Bash`: omitted, empty or `*` match every tool; otherwise
    the matcher is a pattern the whole tool name must match (`Bash`, `Bash|Edit`)."""
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return matcher == "Bash"


def operator_hooks(project_dir: "str | None") -> "list[tuple[str, float]]":
    """(command, timeout) of every command hook a Claude Bash call would run, in settings order."""
    hooks: "list[tuple[str, float]]" = []
    for path in settings_files(project_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = (data.get("hooks") or {}).get("PreToolUse") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict) or not _matches_bash(entry.get("matcher")):
                continue
            for hook in entry.get("hooks") or ():
                if (isinstance(hook, dict) and hook.get("type") == "command"
                        and isinstance(hook.get("command"), str) and hook["command"].strip()):
                    declared = hook.get("timeout")
                    timeout = OPERATOR_HOOK_TIMEOUT_SEC
                    if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0:
                        timeout = min(float(declared), OPERATOR_HOOK_TIMEOUT_SEC)
                    hooks.append((hook["command"], timeout))
    return hooks


def claude_payload(payload: dict) -> dict:
    """What Claude Code hands a PreToolUse hook for a Bash call (the tool is `Bash` here, whatever Grok
    calls it, so a hook that checks `tool_name` behaves as it does for Claude)."""
    out = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": dict(_tool_input(payload) or {})}
    for theirs, ours in (("session_id", "session_id"), ("sessionId", "session_id"), ("transcript_path", "transcript_path"),
                         ("transcriptPath", "transcript_path"), ("cwd", "cwd"), ("permission_mode", "permission_mode"),
                         ("permissionMode", "permission_mode")):
        if ours not in out and payload.get(theirs) is not None:
            out[ours] = payload[theirs]
    return out


def run_operator_hook(command: str, timeout: float, stdin: str, project_dir: "str | None") -> "str | None":
    """The deny reason, or None when the hook lets the call through (also on a crash or a timeout —
    a broken hook is a non-blocking error in Claude Code too)."""
    env = dict(os.environ)
    if project_dir:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env,
                                cwd=project_dir if project_dir and os.path.isdir(project_dir) else None,
                                start_new_session=True)
    except OSError:
        return None
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return None
    if proc.returncode == 2:
        return err.strip() or "Blocked by a PreToolUse hook."
    if proc.returncode != 0 or not out.strip():
        return None
    try:
        result = json.loads(out)
    except ValueError:
        return None
    if not isinstance(result, dict):
        return None
    specific = result.get("hookSpecificOutput")
    if isinstance(specific, dict) and specific.get("permissionDecision") == "deny":
        return str(specific.get("permissionDecisionReason") or "Denied by a PreToolUse hook.")
    if result.get("decision") == "block":
        return str(result.get("reason") or "Blocked by a PreToolUse hook.")
    return None


def operator_verdict(payload) -> "dict | None":
    """The first deny of the operator's Claude PreToolUse hooks for this shell call, as hook output."""
    tool_input = _tool_input(payload)
    command = tool_input.get("command") if tool_input else None
    if not isinstance(command, str) or not command:
        return None
    project_dir = _project_dir(payload)
    stdin = json.dumps(claude_payload(payload))
    for hook_command, timeout in operator_hooks(project_dir):
        reason = run_operator_hook(hook_command, timeout, stdin, project_dir)
        if reason:
            return _deny(reason)
    return None


def main(argv: "list[str]") -> int:
    audit_dir = None
    if len(argv) >= 2 and argv[0] == "--audit-dir":
        audit_dir = argv[1]
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except Exception:
        return 0
    try:
        found = decide(payload, os.environ.get("DENY_COMMANDS_EXTRA", ""))
        if found:
            out, rule, command = found
            _audit(audit_dir, payload.get("cwd"), rule, command)
            sys.stdout.write(json.dumps(out))
            return 0
        denied = operator_verdict(payload)
    except Exception:
        return 0
    if denied:
        sys.stdout.write(json.dumps(denied))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
