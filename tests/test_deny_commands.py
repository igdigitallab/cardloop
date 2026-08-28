"""
Irreversible-command guard (PreToolUse) — see engine.py "Irreversible-command guard" section.

Every engine connection runs permission_mode="bypassPermissions" (full-auto: no chat/card
confirmation). A PreToolUse hook that returns "deny" still blocks the call in that mode
(https://code.claude.com/docs/en/permissions#extend-permissions-with-hooks), which is what the
classifier below is wired into (_dangerous_command_guard_hook). The classifier itself is pure
and fully unit-tested here; the async hook wrapper is covered by a handful of smoke tests.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine


# ── commands that MUST be blocked ───────────────────────────────────────────────────────────
FATAL = [
    # rm -rf on the filesystem root or $HOME, in the flag/spelling variations people actually
    # type (and the ones a model might generate).
    "rm -rf /",
    "rm -rf / ",
    "rm -rf /*",
    "rm -Rf /",
    "rm -fr /",
    "rm -r -f /",
    "rm --recursive --force /",
    "sudo rm -rf /",
    "rm -rf --no-preserve-root /",
    "rm -rf ~",
    "rm -rf ~/*",
    "rm -rf $HOME",
    "rm -rf $HOME/*",
    "rm -rf ${HOME}",
    "echo building && rm -rf ~",                       # hidden after a harmless prefix
    # git push --force / --force-with-lease into master or main, or with no ref at all
    # (this project's own workflow keeps everything on master, so an unqualified force-push
    # targets it too).
    "git push --force",
    "git push -f",
    "git push --force origin master",
    "git push --force-with-lease origin main",
    "git push -f origin main",
    "git push origin --force",
    # git reset --hard, any target
    "git reset --hard",
    "git reset --hard HEAD~3",
    "git reset --hard origin/main",
    # ~/.ssh mutation
    "rm -f ~/.ssh/id_rsa",
    "rm -rf ~/.ssh",
    "> ~/.ssh/authorized_keys",
    "mv ~/.ssh /tmp/x",
    "chmod 777 ~/.ssh/id_rsa",      # opens the key to group/other
    "chmod go+r ~/.ssh/id_rsa",     # same, symbolic form
    "cp evil.pub >> ~/.ssh/authorized_keys",
    # docker system prune
    "docker system prune",
    "docker system prune -af",
    # mkfs
    "mkfs.ext4 /dev/sda1",
    "mkfs -t ext4 /dev/sdb",
    # dd onto a raw block device
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "dd of=/dev/sdb if=image.img",
    # chmod -R 777 on the root
    "chmod -R 777 /",
    "chmod -R 000 /",
    "chmod -R a+rwx /",
    "chmod 777 -R /",
    "chmod -R 777 /*",
    # fork bomb
    ":(){ :|:& };:",
    ": () { : | : & } ; :",
    ":(){:|:&};:",
]

# ── everyday commands that MUST keep working un-gated ───────────────────────────────────────
SAFE = [
    # rm -rf inside the project's own working directory, relative or absolute
    "rm -rf ./build",
    "rm -rf /home/alice/myproject/web/dist",
    "rm -rf /home/alice/myproject/tmp/",
    "rm -rf node_modules",
    "rm -f report.txt",
    "rm -rf .worktrees/foo",
    "rm -rf $HOME/cardloop/tmp",
    "rm -rf ~/Downloads",
    "rm -rf /home/alice/myproject/*",
    "rm -rf ~/cardloop/*",
    # plain git push (no force) and pushes to a feature branch
    "git push",
    "git push origin feature-x",
    "git push --force-with-lease origin feature-branch",
    "git push -f origin my-scratch-branch",
    # non-destructive git reset
    "git reset HEAD~1",
    "git reset --soft HEAD~1",
    "git reset --mixed",
    # ~/.ssh reads
    "cat ~/.ssh/config",
    "ls -la ~/.ssh",
    "ssh-add ~/.ssh/id_rsa",
    "chmod 600 ~/.ssh/id_ed25519",  # locking a key DOWN is the documented routine op
    "chmod 000 $HOME/.ssh/id_rsa",  # more restrictive, not an exposure
    # docker commands that must keep working
    "docker compose down",
    "docker container prune",
    "docker system df",
    # mkfs-lookalike / unrelated
    "echo mkfsomething",
    "make -j4",
    # dd writing to a regular file or to /dev/null, and reading from a device (backups)
    "dd if=/dev/zero of=testfile bs=1M count=10",
    "dd if=/dev/sda of=backup.img",
    "dd if=/dev/zero of=/dev/null",
    # chmod inside the project, or a non-recursive chmod anywhere
    "chmod -R 777 ./dist",
    "chmod +x script.sh",
    "chmod -R 755 /home/alice/myproject",
    # ordinary read-only / build commands
    "ls -la",
    "git status",
    "git commit -m 'fix: something'",
    "npm run build",
]


@pytest.mark.parametrize("cmd", FATAL)
def test_classifier_blocks_fatal_shapes(cmd):
    assert engine._classify_dangerous_command(cmd) is not None, f"should block: {cmd!r}"


@pytest.mark.parametrize("cmd", SAFE)
def test_classifier_allows_safe_shapes(cmd):
    assert engine._classify_dangerous_command(cmd) is None, f"should allow: {cmd!r}"


def test_classifier_never_raises_on_garbage():
    for garbage in ("", None, 12345, "\x00\x01 rm -rf /", "a" * 5000):
        try:
            engine._classify_dangerous_command(garbage)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover - the assertion below is what matters
            pytest.fail(f"classifier raised on {garbage!r}: {exc}")


# ── the async PreToolUse hook wrapper ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guard_hook_denies_with_reason():
    out = await engine._dangerous_command_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, None, None)
    spec = out.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "deny"
    assert "rm -rf" in spec.get("permissionDecisionReason", "")


@pytest.mark.asyncio
async def test_guard_hook_allows_normal_commands():
    out = await engine._dangerous_command_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git push origin feature-x"}}, None, None)
    assert out == {}


@pytest.mark.asyncio
async def test_guard_hook_never_raises_on_garbage():
    assert await engine._dangerous_command_guard_hook({}, None, None) == {}
    assert await engine._dangerous_command_guard_hook({"tool_input": None}, None, None) == {}
    assert await engine._dangerous_command_guard_hook("not a dict", None, None) == {}


# ── DENY_COMMANDS_EXTRA operator extensibility ──────────────────────────────────────────────

def test_extra_deny_pattern_from_env(monkeypatch):
    monkeypatch.setenv("DENY_COMMANDS_EXTRA", r"\bkubectl\s+delete\s+namespace\s+prod\b, ^another$")
    patterns = engine._compile_extra_deny_patterns()
    assert len(patterns) == 2
    assert any(p.search("kubectl delete namespace prod --force") for p in patterns)
    assert not any(p.search("kubectl get pods") for p in patterns)


def test_extra_deny_pattern_ignores_invalid_regex(monkeypatch):
    monkeypatch.setenv("DENY_COMMANDS_EXTRA", "(unclosed[")
    # must not raise — an operator typo should degrade to "no extra rule", not crash startup
    patterns = engine._compile_extra_deny_patterns()
    assert patterns == []
