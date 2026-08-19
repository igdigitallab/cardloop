"""
tunnel.py — spec-082 workstream B: zero-config remote access.

Wraps a `cloudflared` quick tunnel (`cloudflared tunnel --url http://127.0.0.1:<port>
--no-autoupdate`) so `python bot.py --tunnel` can put the cockpit on a public
`https://*.trycloudflare.com` URL with no Cloudflare account, no config file, and no
DNS setup. Also renders a terminal QR code (Unicode half-block rows, two matrix rows
per printed line so it fits a normal terminal window) for that URL — or for the
cockpit's LAN URL when WEB_HOST binds a non-localhost interface without --tunnel.

Lifecycle, owned by bot.py:

    url = await start_tunnel(port)   # spawns cloudflared, waits for the URL, then starts
                                      # a watchdog that restarts it (bounded backoff) if it
                                      # exits unexpectedly. Never raises: returns None if
                                      # the binary is missing or no URL showed up in time —
                                      # the cockpit must keep starting on localhost regardless.
    await stop_tunnel()               # terminates cloudflared + the watchdog. Call this from
                                      # bot.py's shutdown sequence so nothing is ever orphaned.

Independent implementation informed by a public README read (see spec-082's legal
guardrail) — no code from any other project.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import time

log = logging.getLogger("cardloop.tunnel")

CLOUDFLARED_BIN = "cloudflared"

# cloudflared prints its assigned hostname inside a decorative box on stderr, e.g.:
#   2026-08-19T00:00:00Z INF |  https://random-words-1234.trycloudflare.com  |
_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_STARTUP_TIMEOUT_SEC = 20.0    # how long to wait for the URL on (re)start
_INITIAL_BACKOFF_SEC = 2.0
_MAX_BACKOFF_SEC = 60.0
_TERMINATE_TIMEOUT_SEC = 5.0    # grace period for SIGTERM before stop() escalates to kill()

INSTALL_HINT = (
    "cloudflared not found on PATH — --tunnel needs it to create a quick tunnel.\n"
    "  Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
    "  Debian/Ubuntu: curl -fsSL https://pkg.cloudflare.com/cloudflared-install.sh | sudo bash\n"
    "  macOS: brew install cloudflared\n"
    "Continuing without a tunnel — the cockpit is still reachable on localhost."
)


def find_cloudflared(binary: str = CLOUDFLARED_BIN) -> "str | None":
    """Resolve the cloudflared binary on PATH, or None if it isn't installed."""
    return shutil.which(binary)


class QuickTunnel:
    """Owns one cloudflared subprocess (plus its restart watchdog) for the tunnel's lifetime."""

    def __init__(self, port: int, binary: str = CLOUDFLARED_BIN):
        self.port = port
        self.binary = binary
        self.url: "str | None" = None
        self._proc: "asyncio.subprocess.Process | None" = None
        self._watchdog: "asyncio.Task | None" = None
        self._stopping = False

    async def start(self) -> "str | None":
        """Spawn cloudflared and wait for its URL. Never raises — returns None on any failure."""
        if not find_cloudflared(self.binary):
            log.warning(INSTALL_HINT)
            return None
        try:
            url = await self._spawn_and_wait_for_url()
        except FileNotFoundError:
            log.warning(INSTALL_HINT)
            return None
        except Exception as e:
            log.warning("[tunnel] failed to start cloudflared: %s", e)
            return None
        self.url = url
        if url:
            self._stopping = False
            self._watchdog = asyncio.create_task(self._watch())
        return url

    async def _spawn_and_wait_for_url(self) -> "str | None":
        self._proc = await asyncio.create_subprocess_exec(
            self.binary, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return await self._read_url_from_stderr(_STARTUP_TIMEOUT_SEC)

    async def _read_url_from_stderr(self, timeout: float) -> "str | None":
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = await asyncio.wait_for(proc.stderr.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if not line:
                return None  # stderr closed before a URL appeared (process likely exited)
            match = _URL_RE.search(line.decode("utf-8", "replace"))
            if match:
                # Keep draining stderr in the background so the pipe never fills up and
                # blocks cloudflared once we stop reading it synchronously.
                asyncio.create_task(self._drain_stderr())
                return match.group(0)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
        except Exception:
            return

    async def _watch(self) -> None:
        """Restart cloudflared with bounded exponential backoff if it exits unexpectedly."""
        backoff = _INITIAL_BACKOFF_SEC
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            returncode = await proc.wait()
            if self._stopping:
                return
            log.warning(
                "[tunnel] cloudflared exited unexpectedly (code %s) — restarting in %.0fs",
                returncode, backoff,
            )
            await asyncio.sleep(backoff)
            if self._stopping:
                return
            backoff = min(backoff * 2, _MAX_BACKOFF_SEC)
            try:
                url = await self._spawn_and_wait_for_url()
            except Exception as e:
                log.warning("[tunnel] restart attempt failed: %s", e)
                continue
            if url:
                self.url = url
                backoff = _INITIAL_BACKOFF_SEC
                log.info("[tunnel] cloudflared restarted — new URL: %s", url)

    async def stop(self) -> None:
        """Terminate cloudflared and the watchdog. Idempotent; safe even if start() never ran."""
        self._stopping = True
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


# ── Process-wide singleton — bot.py never juggles a QuickTunnel instance directly ──

_active: "QuickTunnel | None" = None


async def start_tunnel(port: int, binary: str = CLOUDFLARED_BIN) -> "str | None":
    """Start (or replace) the process-wide quick tunnel. See QuickTunnel for the details."""
    global _active
    if _active is not None:
        await _active.stop()
    t = QuickTunnel(port, binary=binary)
    _active = t
    return await t.start()


async def stop_tunnel() -> None:
    """Tear down the process-wide tunnel, if any. Safe to call even if none was started."""
    global _active
    t, _active = _active, None
    if t is not None:
        await t.stop()


def current_url() -> "str | None":
    return _active.url if _active is not None else None


# ── Terminal QR rendering ───────────────────────────────────────────────────────

def render_qr(data: str) -> str:
    """Render `data` as a Unicode half-block QR code — two matrix rows per printed
    text line, so a normal-size terminal window can fit the whole code."""
    import qrcode  # deferred: keeps `import tunnel` cheap when no QR is ever printed

    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()  # list[list[bool]]; True = dark module
    width = len(matrix[0]) if matrix else 0
    lines = []
    for y in range(0, len(matrix), 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < len(matrix) else [False] * width
        chars = []
        for x in range(width):
            t, b = top[x], bottom[x]
            if t and b:
                chars.append("█")   # █ both halves dark
            elif t:
                chars.append("▀")   # ▀ top half dark
            elif b:
                chars.append("▄")   # ▄ bottom half dark
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


def print_qr(url: str, label: str = "") -> None:
    """Print `url` plus a scannable terminal QR code. Never raises — best-effort UX only."""
    try:
        art = render_qr(url)
    except Exception as e:
        log.warning("[tunnel] could not render QR code (%s) — URL above still works", e)
        return
    if label:
        print(label)
    print(art)


# ── LAN URL for the non-tunnel case ─────────────────────────────────────────────

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_ANY_HOSTS = {"0.0.0.0", "::"}


def _detect_outbound_ip() -> "str | None":
    """Best-effort local IP the host would use to reach the internet. Opens a UDP socket
    and calls connect() — for UDP that only resolves a route via the kernel routing
    table, no packet is actually sent. Used to turn WEB_HOST=0.0.0.0 into a concrete,
    dialable LAN address for the QR code."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def lan_url(host: str, port: int, scheme: str = "http") -> "str | None":
    """Best-effort LAN URL for `host:port`, or None if there's nothing worth printing
    (the cockpit is still bound to loopback only)."""
    if host in _LOOPBACK_HOSTS:
        return None
    ip = host
    if host in _ANY_HOSTS:
        ip = _detect_outbound_ip()
        if not ip:
            return None
    return f"{scheme}://{ip}:{port}"
