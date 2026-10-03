"""spec-095 P7: the kernel facts the data-dir deny design rests on, measured with plain bubblewrap.

Grok hides a denied path with a bind mount over it, set up when the sandbox starts. The cockpit rewrites
its state files by writing a temp file and renaming it over the original (`chats.json`,
`crash-recovery-state.json`, ...), from OUTSIDE the sandbox, while a turn runs:

  * a mount over a FILE is detached by that rename — the sandbox then reads the new contents;
  * a mount over a DIRECTORY is not: the host's writes underneath it stay invisible, nothing can be
    created in it and it cannot be renamed away.

That is why `grok_engine._data_deny_entry` hides the data dir as ONE directory entry (and refuses a
GROK_HOME inside it) instead of masking its files. These tests do not exercise our code: they pin the
premise, so a kernel that behaves differently turns this red instead of the design going silently wrong.
Skipped where unprivileged bubblewrap does not work.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BWRAP = shutil.which("bwrap")


def _bwrap_works() -> bool:
    if not BWRAP:
        return False
    try:
        return subprocess.run([BWRAP, "--dev-bind", "/", "/", "true"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _bwrap_works(), reason="unprivileged bubblewrap is not usable here")


def _run_inside(mask_args: list[str], script: str, host_action, go: Path, timeout: float = 20.0) -> str:
    """Start `sh -c script` under bwrap, run `host_action()` on the host once the script waits, and
    return the script's output. The script blocks on `go` (created after the host action)."""
    proc = subprocess.Popen([BWRAP, "--dev-bind", "/", "/", *mask_args, "sh", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(0.8)                                   # the sandbox is up and the script is polling
        host_action()
        go.write_text("go")
        out, _ = proc.communicate(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return out


def test_a_mount_over_a_file_is_detached_when_the_host_renames_a_new_file_over_it(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "chats.json").write_text("ORIGINAL")
    go = tmp_path / "go"

    def host():
        (data / "chats.json.tmp").write_text("NEW-SECRET")
        os.replace(data / "chats.json.tmp", data / "chats.json")

    script = (f'echo "before=$(cat {data}/chats.json 2>&1)"; while [ ! -e {go} ]; do sleep 0.1; done; '
              f'echo "after=$(cat {data}/chats.json 2>&1)"')
    out = _run_inside(["--ro-bind", "/dev/null", str(data / "chats.json")], script, host, go)
    assert "before=cat:" in out and "Permission denied" in out, out        # masked while nothing happens
    assert "after=NEW-SECRET" in out, out                                  # ... and open after the rename


def test_a_mount_over_a_directory_survives_every_write_the_cockpit_makes_underneath(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "chats.json").write_text("ORIGINAL")
    empty = tmp_path / "empty"
    empty.mkdir()
    go = tmp_path / "go"

    def host():
        (data / "chats.json.tmp").write_text("NEW-SECRET")
        os.replace(data / "chats.json.tmp", data / "chats.json")
        (data / "brand-new.json").write_text("NEW-SECRET")

    script = (f'echo "before=[$(ls -A {data})]"; while [ ! -e {go} ]; do sleep 0.1; done; '
              f'echo "after=[$(ls -A {data})] $(cat {data}/chats.json 2>&1)"; '
              f'mkdir {data}/planted 2>&1; echo "mkdir=$?"; echo x > {data}/planted.json 2>&1; echo "write=$?"; '
              f'mv {data} {data}-moved 2>&1; echo "mv=$?"')
    out = _run_inside(["--ro-bind", str(empty), str(data)], script, host, go)
    assert "before=[]" in out and "after=[]" in out and "NEW-SECRET" not in out, out
    assert "mkdir=0" not in out and "write=0" not in out and "mv=0" not in out, out
    assert not (data.parent / "data-moved").exists() and (data / "chats.json").read_text() == "NEW-SECRET"
