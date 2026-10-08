"""
spec-096 P3b — a REAL cockpit process started as the program (bot.py) keeps its secrets out of the
shell it hands the operator and out of reach of other processes of the same user.

  - the terminal PTY (a normal spawn path: a child of the cockpit with the cockpit's environment)
    sees neither the web password nor a secret-shaped sentinel, yet the cockpit itself still logs in
    with that password (in-process readers use the startup snapshot);
  - /proc/<cockpit pid>/environ is not readable by this (same-user) test process, while the
    world-readable entries are.

Run with:  venv/bin/python -m pytest tests/e2e -m e2e
"""
import asyncio
import concurrent.futures
import os
import re
from pathlib import Path

import aiohttp
import pytest

pytestmark = pytest.mark.e2e


def _cockpit_pid(app_dir: Path) -> int:
    target = str(app_dir / "bot.py").encode()
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if target in Path(f"/proc/{entry}/cmdline").read_bytes():
                    return int(entry)
            except OSError:
                continue
    raise AssertionError(f"no cockpit process found for {target!r}")


async def _terminal_output(server: dict, command: str) -> str:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:   # unsafe: host is an IP
        r = await s.post(f"{server['base_url']}/api/login", json={"password": server["password"]})
        assert r.status == 200, "the cockpit refused its own password after the secrets were scrubbed"
        async with s.ws_connect(f"{server['base_url']}/api/terminal/ws") as ws:
            await ws.send_bytes((command + "\n").encode())
            seen = b""
            deadline = asyncio.get_running_loop().time() + 15
            while b"END-OF-OUTPUT" not in seen:      # the typed command spells it split, only the output joins it
                timeout = deadline - asyncio.get_running_loop().time()
                assert timeout > 0, f"terminal never answered: {seen[-400:]!r}"
                msg = await ws.receive(timeout=timeout)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    seen += msg.data
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
            return seen.decode(errors="replace")


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs Linux procfs")
def test_the_cockpit_terminal_does_not_inherit_the_secrets(e2e_server):
    # Playwright's sync fixtures keep an event loop running on this thread: use a private one.
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        out = pool.submit(asyncio.run, _terminal_output(
            e2e_server,
            'echo "PW=[${WEB_PASSWORD}] SENT=[${E2E_SENTINEL_TOKEN}]"; echo "END-OF""-OUTPUT"')).result(timeout=60)
    assert e2e_server["password"] not in out
    assert e2e_server["sentinel"] not in out
    assert re.search(r"PW=\[\] SENT=\[\]", out), f"the shell did not run the probe:\n{out[-600:]}"


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs Linux procfs")
def test_a_same_user_process_cannot_read_the_cockpit_environment(e2e_server):
    pid = _cockpit_pid(e2e_server["app_dir"])
    p = Path(f"/proc/{pid}")
    for entry in ("environ", "maps", "mem"):
        with pytest.raises(PermissionError):
            open(p / entry, "rb").read(1)
    with pytest.raises(PermissionError):
        os.listdir(p / "fd")
    assert "VmRSS" in (p / "status").read_text()         # what doctor and the load meter read
    assert b"bot.py" in (p / "cmdline").read_bytes()


def test_the_startup_line_names_what_was_removed_and_never_a_value(e2e_server):
    log = (e2e_server["app_dir"] / "server.log").read_text(errors="replace")
    line = next((ln for ln in log.splitlines() if "[security]" in ln and "removed from the process" in ln), "")
    assert "WEB_PASSWORD" in line and "E2E_SENTINEL_TOKEN" in line, log[:1500]
    assert e2e_server["password"] not in log and e2e_server["sentinel"] not in log
