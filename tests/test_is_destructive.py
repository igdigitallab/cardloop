"""
Tests for bot._is_destructive — detector for "irreversible" commands.

Signals in the TG footer ⚠️ and audit log.
From CLAUDE.md: "Do NOT use -f / rm / kill (catches tail -f, perform, etc.).
Only rm -rf/rm -f/git push/--force and similar."
"""
import pytest

from engine import _is_destructive


# ─────────────────────────── positive cases (should return True) ───────────────────────────

@pytest.mark.parametrize("cmd", [
    "git push",
    "git push origin master",
    "git push --force",
    "git reset --hard HEAD~5",
    "git rebase main",
    "git clean -fdx",
    "rm -rf /tmp/foo",
    "rm -rf node_modules",
    "rm -r /var/cache",
    "rm -f some_file",
    "DROP TABLE users",
    "drop table users",
    "drop database mydb",
    "delete from users where id=1",
    "TRUNCATE products",
    "docker rm -f mycontainer",
    "docker stop mycontainer",
    "docker compose down",
    "docker-compose down -v",
    "systemctl restart nginx",
    "systemctl stop my-service",
    "curl coolify.example.com/api/v1/deploy",
    "cmd --force",
])
def test_destructive_commands(cmd):
    assert _is_destructive(cmd), f"Should be detected as destructive: {cmd!r}"


# ─────────────────────────── negative cases (should NOT trigger) ───────────────────────────

@pytest.mark.parametrize("cmd", [
    "ls -la",
    "pwd",
    "git status",
    "git log",
    "git diff",
    "git pull",
    "git commit -m 'fix'",
    "git checkout main",
    "tail -f /var/log/syslog",       # from CLAUDE.md: -f must not be caught
    "perform action",                  # from CLAUDE.md: "perform" does not contain 'rm '
    "cat file.txt",
    "echo hello",
    "python script.py",
    "npm install",
    "npm run build",
    "make test",
    "journalctl -u my-service",
    "systemctl status nginx",          # status — not destructive
    "systemctl is-active nginx",
    "ps aux | grep python",
    "kill 12345",                      # kill without surrounding spaces is not in the list
    "sudo apt update",
])
def test_safe_commands(cmd):
    assert not _is_destructive(cmd), f"Should NOT be detected as destructive: {cmd!r}"


# ─────────────────────────── case-insensitive ───────────────────────────

def test_case_insensitive_uppercase():
    assert _is_destructive("RM -RF /tmp")


def test_case_insensitive_mixed():
    assert _is_destructive("Git Push origin")


def test_case_insensitive_drop():
    assert _is_destructive("Drop Table foo")


# ─────────────────────────── edge cases ───────────────────────────

def test_empty_string_not_destructive():
    assert not _is_destructive("")


def test_destructive_substring_in_arg():
    """Destructive substring inside a quoted argument also triggers (intentional over-match — safer)."""
    # 'rm -rf' appearing inside a string argument — will be flagged
    assert _is_destructive("echo 'rm -rf /tmp'")


def test_tail_dash_f_not_caught_regression():
    """REGRESSION: 'tail -f' contains '-f ' but must NOT trigger (historical footgun from CLAUDE.md)."""
    # Verify the detector does not use an overly broad '-f ' pattern
    assert not _is_destructive("tail -f /var/log/syslog")
    assert not _is_destructive("watch -f")
