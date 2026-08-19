"""Tests for tunnel.py (spec-082 workstream B: --tunnel + QR zero-config remote access)
and the small bot.py glue around it (_parse_args / _tunnel_requested / _maybe_start_tunnel).

asyncio_mode=auto (pytest.ini) so async tests are plain `async def test_...`.
NEVER invokes a real cloudflared binary or touches the network — all subprocess
interaction is monkeypatched, mirroring the pattern in test_second_opinion.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tunnel  # module under test


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _reset_active_tunnel():
    """Every test starts with a clean process-wide singleton and cleans up after
    itself even if a test forgets to call stop_tunnel() explicitly."""
    tunnel._active = None
    yield
    if tunnel._active is not None:
        await tunnel._active.stop()
    tunnel._active = None


class FakeStream:
    """Mimics the async line-reader interface of a subprocess.Process.stderr pipe."""

    def __init__(self, lines: "list[bytes]", block_after: bool = False):
        self._lines = list(lines)
        self._block_after = block_after
        self._never = asyncio.Event()  # never set — used to simulate a still-open pipe

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._block_after:
            await self._never.wait()
        return b""  # EOF


class FakeProc:
    """Mimics asyncio.subprocess.Process enough for QuickTunnel's needs.

    Mirrors real asyncio.subprocess.Process semantics: each wait() call gets its OWN
    future (real Process keeps a list of per-call waiters), so cancelling one caller's
    wait() (e.g. the watchdog task, cancelled by stop()) does not poison a LATER,
    independent wait() call made by that same stop() right after — using one shared
    future for the whole process would incorrectly propagate that cancellation.
    """

    def __init__(self, stderr_lines: "list[bytes]", block_after: bool = False):
        self.stderr = FakeStream(stderr_lines, block_after=block_after)
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self._waiters: "list[asyncio.Future]" = []

    async def wait(self):
        if self.returncode is not None:
            return self.returncode
        fut = asyncio.get_event_loop().create_future()
        self._waiters.append(fut)
        try:
            return await fut
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    def set_exit(self, code: int) -> None:
        self.returncode = code
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(code)

    def terminate(self) -> None:
        self.terminate_called = True
        self.set_exit(-15)

    def kill(self) -> None:
        self.kill_called = True
        self.set_exit(-9)


class FakeProcThatIgnoresTerminate(FakeProc):
    """terminate() does NOT resolve the process — used to exercise the kill() escalation."""

    def terminate(self) -> None:
        self.terminate_called = True  # do NOT resolve _wait_future


def _patch_binary_found(monkeypatch, path: str = "/usr/bin/cloudflared") -> None:
    monkeypatch.setattr(tunnel, "find_cloudflared", lambda binary=tunnel.CLOUDFLARED_BIN: path)


def _patch_exec(monkeypatch, *procs: FakeProc):
    """Make asyncio.create_subprocess_exec return each proc in `procs` in turn."""
    queue = list(procs)
    captured = {"argv": []}

    async def fake_exec(*argv, **kwargs):
        captured["argv"].append(argv)
        return queue.pop(0)

    monkeypatch.setattr(tunnel.asyncio, "create_subprocess_exec", fake_exec)
    return captured


# ---------------------------------------------------------------------------
# 1. find_cloudflared / missing-binary path
# ---------------------------------------------------------------------------

def test_find_cloudflared_uses_shutil_which(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: "/usr/bin/cloudflared" if name == "cloudflared" else None)
    assert tunnel.find_cloudflared() == "/usr/bin/cloudflared"


def test_find_cloudflared_none_when_absent(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    assert tunnel.find_cloudflared() is None


async def test_start_returns_none_and_never_spawns_when_binary_missing(monkeypatch):
    monkeypatch.setattr(tunnel, "find_cloudflared", lambda binary=tunnel.CLOUDFLARED_BIN: None)
    spawned = {"n": 0}

    async def fake_exec(*a, **k):
        spawned["n"] += 1
        raise AssertionError("must not spawn cloudflared when the binary is missing")

    monkeypatch.setattr(tunnel.asyncio, "create_subprocess_exec", fake_exec)

    qt = tunnel.QuickTunnel(port=8787)
    url = await qt.start()

    assert url is None
    assert spawned["n"] == 0
    assert qt._watchdog is None


# ---------------------------------------------------------------------------
# 2. URL parsing from fixture stderr
# ---------------------------------------------------------------------------

def test_url_regex_matches_boxed_cloudflared_output():
    line = "2026-08-19T00:00:00Z INF |  https://random-words-1234.trycloudflare.com  |"
    m = tunnel._URL_RE.search(line)
    assert m and m.group(0) == "https://random-words-1234.trycloudflare.com"


def test_url_regex_ignores_non_trycloudflare_urls():
    line = "2026-08-19T00:00:00Z INF visit https://cloudflare.com for docs"
    assert tunnel._URL_RE.search(line) is None


async def test_start_happy_path_parses_url_and_arms_watchdog(monkeypatch):
    _patch_binary_found(monkeypatch)
    proc = FakeProc([
        b"2026-08-19T00:00:00Z INF Thank you for trying Cloudflare Tunnel\n",
        b"2026-08-19T00:00:00Z INF +----------------------------------------------+\n",
        b"2026-08-19T00:00:00Z INF |  https://foo-bar-baz.trycloudflare.com        |\n",
        b"2026-08-19T00:00:00Z INF +----------------------------------------------+\n",
    ])
    captured = _patch_exec(monkeypatch, proc)

    qt = tunnel.QuickTunnel(port=8787)
    url = await qt.start()

    assert url == "https://foo-bar-baz.trycloudflare.com"
    assert qt.url == url
    assert qt._watchdog is not None
    # sanity: it invoked cloudflared with the right shape of argv
    argv = captured["argv"][0]
    assert argv[0] == tunnel.CLOUDFLARED_BIN
    assert "--url" in argv
    assert "http://127.0.0.1:8787" in argv
    assert "--no-autoupdate" in argv

    await qt.stop()


async def test_read_url_from_stderr_times_out_when_none_appears(monkeypatch):
    monkeypatch.setattr(tunnel, "_STARTUP_TIMEOUT_SEC", 0.05)
    _patch_binary_found(monkeypatch)
    proc = FakeProc([], block_after=True)  # pipe stays open, no URL ever printed
    _patch_exec(monkeypatch, proc)

    qt = tunnel.QuickTunnel(port=8787)
    url = await qt.start()

    assert url is None
    assert qt._watchdog is None  # never armed — nothing to restart


async def test_stderr_eof_before_url_returns_none(monkeypatch):
    _patch_binary_found(monkeypatch)
    proc = FakeProc([b"cloudflared: some early error, exiting\n"])  # then EOF, no URL line
    _patch_exec(monkeypatch, proc)

    qt = tunnel.QuickTunnel(port=8787)
    url = await qt.start()

    assert url is None
    assert qt._watchdog is None


# ---------------------------------------------------------------------------
# 3. Restart with bounded backoff on unexpected exit
# ---------------------------------------------------------------------------

async def test_watchdog_restarts_with_backoff_and_logs_new_url(monkeypatch):
    monkeypatch.setattr(tunnel, "_INITIAL_BACKOFF_SEC", 0.01)
    monkeypatch.setattr(tunnel, "_MAX_BACKOFF_SEC", 0.02)
    _patch_binary_found(monkeypatch)

    proc1 = FakeProc([b"https://one.trycloudflare.com\n"])
    proc2 = FakeProc([b"https://two.trycloudflare.com\n"])
    _patch_exec(monkeypatch, proc1, proc2)

    qt = tunnel.QuickTunnel(port=8787)
    url = await qt.start()
    assert url == "https://one.trycloudflare.com"

    # Simulate cloudflared dying unexpectedly.
    proc1.set_exit(1)

    for _ in range(100):
        if qt.url == "https://two.trycloudflare.com":
            break
        await asyncio.sleep(0.01)

    assert qt.url == "https://two.trycloudflare.com", "watchdog must respawn with a fresh URL"
    await qt.stop()


# ---------------------------------------------------------------------------
# 4. stop() — clean shutdown, escalates to kill() if terminate() doesn't land
# ---------------------------------------------------------------------------

async def test_stop_terminates_process_and_cancels_watchdog(monkeypatch):
    _patch_binary_found(monkeypatch)
    proc = FakeProc([b"https://foo.trycloudflare.com\n"])
    _patch_exec(monkeypatch, proc)

    qt = tunnel.QuickTunnel(port=8787)
    await qt.start()
    watchdog = qt._watchdog

    await qt.stop()

    assert proc.terminate_called
    assert not proc.kill_called
    assert qt._proc is None
    assert qt._watchdog is None
    assert watchdog.done()


async def test_stop_is_idempotent_and_safe_before_start(monkeypatch):
    qt = tunnel.QuickTunnel(port=8787)
    await qt.stop()  # never started — must not raise
    await qt.stop()  # calling twice must not raise either


async def test_stop_escalates_to_kill_when_terminate_does_not_land(monkeypatch):
    monkeypatch.setattr(tunnel, "_TERMINATE_TIMEOUT_SEC", 0.02)
    _patch_binary_found(monkeypatch)
    proc = FakeProcThatIgnoresTerminate([b"https://stuck.trycloudflare.com\n"])
    _patch_exec(monkeypatch, proc)

    qt = tunnel.QuickTunnel(port=8787)
    await qt.start()
    await qt.stop()

    assert proc.terminate_called
    assert proc.kill_called


# ---------------------------------------------------------------------------
# 5. Module-level singleton (start_tunnel / stop_tunnel / current_url)
# ---------------------------------------------------------------------------

async def test_start_tunnel_stop_tunnel_singleton(monkeypatch):
    _patch_binary_found(monkeypatch)
    proc = FakeProc([b"https://sing.trycloudflare.com\n"])
    _patch_exec(monkeypatch, proc)

    url = await tunnel.start_tunnel(9999)

    assert url == "https://sing.trycloudflare.com"
    assert tunnel.current_url() == url

    await tunnel.stop_tunnel()

    assert tunnel.current_url() is None
    assert proc.terminate_called


async def test_stop_tunnel_when_never_started_is_a_noop():
    await tunnel.stop_tunnel()  # must not raise
    assert tunnel.current_url() is None


# ---------------------------------------------------------------------------
# 6. QR rendering (half-block Unicode) and printing
# ---------------------------------------------------------------------------

def test_render_qr_uses_only_half_block_glyphs_and_is_rectangular():
    art = tunnel.render_qr("https://example.trycloudflare.com")
    lines = art.splitlines()
    assert len(lines) > 5
    allowed = set(" █▀▄")
    assert all(set(line) <= allowed for line in lines)
    assert len({len(line) for line in lines}) == 1, "every row must be the same width"


def test_render_qr_differs_for_different_payloads():
    a = tunnel.render_qr("https://aaaaaaaa.trycloudflare.com")
    b = tunnel.render_qr("https://bbbbbbbb.trycloudflare.com")
    assert a != b


def test_print_qr_prints_label_and_art(capsys):
    tunnel.print_qr("https://foo.trycloudflare.com", label="scan me")
    out = capsys.readouterr().out
    assert "scan me" in out
    assert any(ch in out for ch in "█▀▄")


def test_print_qr_never_raises_when_rendering_fails(monkeypatch, capsys):
    def boom(data):
        raise RuntimeError("no qrcode lib available")

    monkeypatch.setattr(tunnel, "render_qr", boom)
    tunnel.print_qr("https://foo.trycloudflare.com", label="scan me")  # must not raise

    out = capsys.readouterr().out
    assert "scan me" not in out  # nothing printed once rendering fails


# ---------------------------------------------------------------------------
# 7. LAN URL resolution (used when --tunnel is off but WEB_HOST is non-localhost)
# ---------------------------------------------------------------------------

def test_lan_url_none_for_loopback_hosts():
    assert tunnel.lan_url("127.0.0.1", 8787) is None
    assert tunnel.lan_url("localhost", 8787) is None
    assert tunnel.lan_url("::1", 8787) is None


def test_lan_url_uses_concrete_host_directly():
    assert tunnel.lan_url("192.168.1.50", 8787) == "http://192.168.1.50:8787"


def test_lan_url_resolves_bind_all_via_outbound_ip(monkeypatch):
    monkeypatch.setattr(tunnel, "_detect_outbound_ip", lambda: "10.0.0.5")
    assert tunnel.lan_url("0.0.0.0", 8787) == "http://10.0.0.5:8787"


def test_lan_url_none_when_bind_all_and_no_route(monkeypatch):
    monkeypatch.setattr(tunnel, "_detect_outbound_ip", lambda: None)
    assert tunnel.lan_url("0.0.0.0", 8787) is None


def test_detect_outbound_ip_handles_no_network(monkeypatch):
    class FailingSocket:
        def connect(self, addr):
            raise OSError("network unreachable")

        def getsockname(self):
            return ("0.0.0.0", 0)

        def close(self):
            pass

    monkeypatch.setattr(tunnel.socket, "socket", lambda *a, **k: FailingSocket())
    assert tunnel._detect_outbound_ip() is None


# ---------------------------------------------------------------------------
# 8. bot.py glue: --tunnel flag / CARDLOOP_TUNNEL env / _maybe_start_tunnel
# ---------------------------------------------------------------------------

import bot  # noqa: E402  (after tunnel import; mirrors other test files' import order)


def test_parse_args_default_no_tunnel():
    args = bot._parse_args([])
    assert args.tunnel is False


def test_parse_args_tunnel_flag():
    args = bot._parse_args(["--tunnel"])
    assert args.tunnel is True


def test_tunnel_requested_by_cli_flag(monkeypatch):
    monkeypatch.delenv("CARDLOOP_TUNNEL", raising=False)
    args = bot._parse_args(["--tunnel"])
    assert bot._tunnel_requested(args) is True


def test_tunnel_requested_by_env(monkeypatch):
    monkeypatch.setenv("CARDLOOP_TUNNEL", "1")
    args = bot._parse_args([])
    assert bot._tunnel_requested(args) is True


def test_tunnel_requested_env_true_variants(monkeypatch):
    args = bot._parse_args([])
    for value in ("1", "true", "True", "yes", "YES"):
        monkeypatch.setenv("CARDLOOP_TUNNEL", value)
        assert bot._tunnel_requested(args) is True, value


def test_tunnel_requested_false_by_default(monkeypatch):
    monkeypatch.delenv("CARDLOOP_TUNNEL", raising=False)
    args = bot._parse_args([])
    assert bot._tunnel_requested(args) is False


def test_tunnel_requested_env_off_variants(monkeypatch):
    args = bot._parse_args([])
    for value in ("", "0", "false", "no"):
        monkeypatch.setenv("CARDLOOP_TUNNEL", value)
        assert bot._tunnel_requested(args) is False, value


async def test_maybe_start_tunnel_noop_when_disabled_and_localhost(monkeypatch):
    monkeypatch.delenv("WEB_HOST", raising=False)
    called = {"n": 0}

    async def fake_start_tunnel(port, binary=tunnel.CLOUDFLARED_BIN):
        called["n"] += 1
        return "https://should-not-be-called.trycloudflare.com"

    monkeypatch.setattr(tunnel, "start_tunnel", fake_start_tunnel)

    result = await bot._maybe_start_tunnel(False)

    assert result is None
    assert called["n"] == 0


async def test_maybe_start_tunnel_refuses_on_bad_password(monkeypatch, capsys):
    monkeypatch.setattr(bot, "WEB_PASSWORD", "")  # blank → _check_web_password raises
    called = {"n": 0}

    async def fake_start_tunnel(port, binary=tunnel.CLOUDFLARED_BIN):
        called["n"] += 1
        return "https://should-not-be-called.trycloudflare.com"

    monkeypatch.setattr(tunnel, "start_tunnel", fake_start_tunnel)

    result = await bot._maybe_start_tunnel(True)

    assert result is None
    assert called["n"] == 0, "a public tunnel must never start when the password check would fail"
    out = capsys.readouterr().out
    assert "refusing to start the tunnel" in out


async def test_maybe_start_tunnel_happy_path_prints_banner_and_qr(monkeypatch, capsys):
    monkeypatch.setattr(bot, "WEB_PASSWORD", "a-strong-password")

    async def fake_start_tunnel(port, binary=tunnel.CLOUDFLARED_BIN):
        assert port == bot.WEB_PORT
        return "https://happy.trycloudflare.com"

    monkeypatch.setattr(tunnel, "start_tunnel", fake_start_tunnel)

    result = await bot._maybe_start_tunnel(True)

    assert result == "https://happy.trycloudflare.com"
    out = capsys.readouterr().out
    assert "https://happy.trycloudflare.com" in out
    assert "WEB_PASSWORD" in out
    assert any(ch in out for ch in "█▀▄")  # the QR code got printed


async def test_maybe_start_tunnel_prints_lan_qr_when_non_localhost_and_no_tunnel(monkeypatch, capsys):
    monkeypatch.setenv("WEB_HOST", "192.168.1.20")

    async def fail_start_tunnel(*a, **k):
        raise AssertionError("must not attempt a tunnel when tunnel_enabled is False")

    monkeypatch.setattr(tunnel, "start_tunnel", fail_start_tunnel)

    result = await bot._maybe_start_tunnel(False)

    assert result is None
    out = capsys.readouterr().out
    assert "192.168.1.20" in out
    assert any(ch in out for ch in "█▀▄")
