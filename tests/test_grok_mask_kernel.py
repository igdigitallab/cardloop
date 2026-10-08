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


def test_a_mount_over_a_unix_socket_stops_a_connect_through_either_spelling_of_its_path(tmp_path):
    # spec-096 P8: /var/run/docker.sock (root-equivalent for a member of the docker group) joins the default
    # deny list. /var/run is a symlink to /run, so the entry and the real path are two spellings of one socket
    # and one mount must hide both. Measured with a throwaway socket, never the real daemon's.
    import socket
    real_dir = tmp_path / "run"
    real_dir.mkdir()
    (tmp_path / "var-run").symlink_to(real_dir)
    sock_path = real_dir / "d.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(8)
    probe = ('import socket,sys\ns=socket.socket(socket.AF_UNIX)\n'
             'try:\n s.connect(sys.argv[1]); print("CONNECTED")\nexcept OSError as e:\n print("DENIED", e.errno)\n')
    prog = tmp_path / "probe.py"
    prog.write_text(probe)
    python = shutil.which("python3") or "python3"

    def connect(*mask):
        out = subprocess.run(
            [BWRAP, "--dev-bind", "/", "/", *mask, python, str(prog), str(sock_path)],
            capture_output=True, text=True, timeout=20).stdout
        out2 = subprocess.run(
            [BWRAP, "--dev-bind", "/", "/", *mask, python, str(prog), str(tmp_path / "var-run" / "d.sock")],
            capture_output=True, text=True, timeout=20).stdout
        return out.strip(), out2.strip()

    try:
        assert connect() == ("CONNECTED", "CONNECTED")                               # positive control
        # masked through the SYMLINKED spelling, probed through both
        assert connect("--ro-bind", "/dev/null", str(tmp_path / "var-run" / "d.sock"))[0].startswith("DENIED")
        assert connect("--ro-bind", "/dev/null", str(tmp_path / "var-run" / "d.sock"))[1].startswith("DENIED")
        assert connect("--ro-bind", "/dev/null", str(sock_path))[1].startswith("DENIED")
    finally:
        srv.close()
