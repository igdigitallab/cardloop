"""spec-096 P3.5 — the diagnostic-command allowlist must test what is EXECUTED.

`_validate_diag_cmd` used to check os.path.basename(token), but the command is exec'd with the
full token: `/tmp/tail -f x` passed on the strength of its file name.
"""
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from webapp import _validate_diag_cmd  # noqa: E402


def _fake_program(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("#!/bin/sh\necho planted\n")
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


@pytest.mark.parametrize("cmd", [
    "/tmp/tail -f x",
    "/tmp/python3 /tmp/x.py",
    "/tmp/docker run --privileged alpine",
    "/var/tmp/journalctl -u x",
    "/usr/bin/../../tmp/tail -f x",         # lexically under /usr/bin, really /tmp/tail
    "/home/someone/venv/bin/python -m pytest",   # absolute, not what a bare `python` resolves to
    "../elsewhere/tail -f x",
    "venv/../../tmp/tail -f x",
    "bin/../../x/python -m pytest",
    "tail/ -f x",
])
def test_a_path_that_is_not_the_allowlisted_program_is_rejected(cmd):
    assert _validate_diag_cmd(cmd) is False


def test_planted_file_by_an_allowlisted_name_is_rejected_by_absolute_path(tmp_path, monkeypatch):
    planted = _fake_program(tmp_path / "evil", "tail")
    assert _validate_diag_cmd(f"{planted} -f x") is False
    assert _validate_diag_cmd(f"{planted}") is False


def test_absolute_path_equal_to_what_the_bare_name_resolves_to_is_accepted(tmp_path, monkeypatch):
    real = shutil.which("tail")
    assert real, "tail is required on the test host"
    assert _validate_diag_cmd(f"{real} -n 5 /tmp/x") is True
    # a usr-merged host reaches the same file through /bin and /usr/bin
    for alias in ("/bin/tail", "/usr/bin/tail"):
        if os.path.exists(alias) and os.path.realpath(alias) == os.path.realpath(real):
            assert _validate_diag_cmd(f"{alias} -n 5 /tmp/x") is True
    # ... and it follows PATH, exactly like the exec does for the bare name
    ours = _fake_program(tmp_path / "bin", "tail")
    monkeypatch.setenv("PATH", f"{ours.parent}{os.pathsep}{os.environ['PATH']}")
    assert _validate_diag_cmd(f"{ours} -f x") is True
    monkeypatch.setenv("PATH", os.environ["PATH"].split(os.pathsep, 1)[1])
    assert _validate_diag_cmd(f"{ours} -f x") is False


def test_absolute_path_is_rejected_when_the_name_is_not_on_path(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    assert _validate_diag_cmd("/usr/bin/tail -f x") is False
    assert _validate_diag_cmd("tail -f x") is True            # bare name: nothing to compare, exec decides


@pytest.mark.parametrize("cmd", [
    "tail -f /var/log/x.log",
    "journalctl -u cardloop -n 300 --no-pager",
    "docker logs --tail 300 myapp",
    "venv/bin/python -m pytest -q --no-header tests/",
    ".venv/bin/pytest -q",
    "./venv/bin/python -m pytest tests -q",
    "python3 -m py_compile main.py",
    "tail -n 300 /tmp/x.log 2>/dev/null",
])
def test_real_configurations_still_validate(cmd):
    assert _validate_diag_cmd(cmd) is True


def test_wrapper_script_dir_rule_is_unchanged(tmp_path, monkeypatch):
    script = _fake_program(tmp_path, "coolify-logs.sh")
    monkeypatch.delenv("DIAG_CMD_ALLOW_DIRS", raising=False)
    assert _validate_diag_cmd(f"{script} app") is False
    monkeypatch.setenv("DIAG_CMD_ALLOW_DIRS", str(tmp_path))
    assert _validate_diag_cmd(f"{script} app") is True
    # the dir rule is for scripts: an allowlisted NAME planted in that dir is just another script there
    planted = _fake_program(tmp_path, "tail")
    assert _validate_diag_cmd(f"{planted} -f x") is True
