"""
Root-fix A2: cgroup memory alert + the bundle-grep OOM guard.

The alert loop itself needs a real cgroup, so only its pure helpers are unit-tested
(percentage math, cgroup path resolution fallback, offender formatting is I/O-bound
and guarded by try/except). The PreToolUse guard's classifier is pure and fully tested.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp


# ── _memory_usage_pct ─────────────────────────────────────────────────────────

def test_pct_basic():
    assert webapp._memory_usage_pct(6 * 1024, 12 * 1024) == 50.0


def test_pct_no_ceiling_returns_none():
    assert webapp._memory_usage_pct(1024, None) is None
    assert webapp._memory_usage_pct(1024, 0) is None


def test_pct_above_100_is_possible():
    # memory.current can momentarily exceed memory.max during reclaim
    assert webapp._memory_usage_pct(130, 100) == 130.0


# ── _cgroup_memory_path ───────────────────────────────────────────────────────

def test_cgroup_path_never_raises():
    # On CI/WSL/macOS this may be None; on a systemd host it is a real dir.
    p = webapp._cgroup_memory_path()
    assert p is None or (p / "memory.current").exists()


# ── bundle-grep guard classifier ─────────────────────────────────────────────

FATAL = [
    # the literal shape from the documented gotcha
    "ugrep -o '.{0,500}TOKEN.{0,500}' node_modules/pkg/dist/bundle.min.js",
    "grep -rE '.{0,200}secret.{0,200}' venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude",
    "rg '.{0,100}apiKey' web/dist/assets/index.js",
]

SAFE = [
    "grep -n 'foo' engine.py",                                    # no wide context
    "grep -rn 'task_type' node_modules/",                         # bundle path but no .{N}
    "ugrep -o '.{0,500}TOKEN.{0,500}' notes.txt",                 # wide context but not a bundle
    "grep -E 'x.{0,10}y' node_modules/a.min.js",                  # context window below threshold
    "python -c \"print(open('node_modules/a.js').read()[:500])\"",  # slicing, not grepping
    "sed -n '1,100p' dist/bundle.js",
]


@pytest.mark.parametrize("cmd", FATAL)
def test_guard_denies_fatal_shapes(cmd):
    assert engine._is_wide_bundle_grep(cmd) is True


@pytest.mark.parametrize("cmd", SAFE)
def test_guard_allows_safe_shapes(cmd):
    assert engine._is_wide_bundle_grep(cmd) is False


@pytest.mark.asyncio
async def test_guard_hook_denies_with_reason():
    out = await engine._bundle_grep_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": FATAL[0]}}, None, None)
    spec = out.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "deny"
    assert "Slice the file" in spec.get("permissionDecisionReason", "")


@pytest.mark.asyncio
async def test_guard_hook_allows_normal_commands():
    out = await engine._bundle_grep_guard_hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, None, None)
    assert out == {}


@pytest.mark.asyncio
async def test_guard_hook_never_raises_on_garbage():
    assert await engine._bundle_grep_guard_hook({}, None, None) == {}
    assert await engine._bundle_grep_guard_hook({"tool_input": None}, None, None) == {}
    assert await engine._bundle_grep_guard_hook("not a dict", None, None) == {}
