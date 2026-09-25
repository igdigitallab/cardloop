"""OOM shield: children of the service cgroup must outrank the cockpit in the OOM killer's
order (2026-09-24: the kernel killed bot.py itself and every live chat died with it)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import webapp

pytestmark = pytest.mark.skipif(not Path("/proc/self/oom_score_adj").exists(),
                                reason="needs Linux /proc")


def _adj(pid: int) -> int:
    return int(Path(f"/proc/{pid}/oom_score_adj").read_text())


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield p
    p.kill()
    p.wait()


def test_raises_children_and_spares_self(tmp_path, child):
    me = os.getpid()
    before_self = _adj(me)
    # Run inside the live cockpit, this process already inherits the shield's raised score,
    # so the target must sit above whatever the child started with or nothing changes.
    inherited = _adj(child.pid)
    if inherited >= 1000:
        pytest.skip("child already inherits the maximum oom_score_adj; nothing left to raise")
    target = min(1000, inherited + 100)
    (tmp_path / "cgroup.procs").write_text(f"{me}\n{child.pid}\n999999999\n")
    changed = webapp._oom_raise_children(tmp_path, me, target)
    assert changed == 1                       # the dead pid is skipped, not an error
    assert _adj(child.pid) == target
    assert _adj(me) == before_self            # the cockpit keeps its own score


def test_never_lowers_a_higher_value(tmp_path, child):
    Path(f"/proc/{child.pid}/oom_score_adj").write_text("800")
    (tmp_path / "cgroup.procs").write_text(f"{child.pid}\n")
    assert webapp._oom_raise_children(tmp_path, os.getpid(), 500) == 0
    assert _adj(child.pid) == 800


def test_missing_cgroup_is_harmless(tmp_path):
    assert webapp._oom_raise_children(tmp_path / "nope", os.getpid(), 500) == 0
