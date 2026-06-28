"""spec-065 Phase B/C — live agent-driven browser pane.

One headless Chromium per project (keyed by cwd), streamed to the cockpit as JPEG
frames over a WebSocket via the Chrome DevTools Protocol (Page.startScreencast).
The SAME session is driven by the operator (pane input) and by the agent (MCP
tools in browser_tools.py) — they share one CDP session, so the operator watches
what the agent does, live.

Design notes:
- Playwright is imported LAZILY (inside .start()): the cockpit must boot fine on
  an instance that never enables the browser module / never ran `playwright
  install chromium`. A clear error is surfaced to the pane instead of crashing.
- On-demand + idle-killed: a session is created on first use (pane connect OR an
  agent tool) and closed once it has no subscribers and has been idle past the
  grace window — respects the service memory cap (the browser is ~0.5-1 GB).
- Screencast is lossy by design: if a WebSocket subscriber lags, frames are
  dropped for that subscriber (unlike a PTY, a stale video frame is worthless).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from typing import Any

# Frame / viewport geometry — the frontend maps pointer coordinates into this
# exact space, so it MUST match the client contract (BrowserTab.tsx).
VIEWPORT = {"width": 1280, "height": 720}
_IDLE_GRACE = 120.0          # close the browser this long after the last activity with no subscribers
_WATCH_INTERVAL = 15.0       # idle-watchdog tick

# Registry: cwd -> BrowserSession. Shared by the WS handler (webapp) and the
# agent MCP tools (browser_tools) so both drive the same browser.
_SESSIONS: "dict[str, BrowserSession]" = {}
_REGISTRY_LOCK = asyncio.Lock()


class BrowserUnavailable(RuntimeError):
    """Raised when Playwright / Chromium is not installed on this instance."""


class BrowserSession:
    """A single Chromium instance + CDP screencast for one project (cwd)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._subs: "set[Any]" = set()       # subscriber WebSocketResponse objects
        self._busy: "set[Any]" = set()       # subscribers with an in-flight send (frame-drop gate)
        self._last_activity = time.monotonic()
        self._watchdog: "asyncio.Task | None" = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
            except Exception as e:  # pragma: no cover - import guard
                raise BrowserUnavailable(
                    "Playwright is not installed. Run: venv/bin/pip install playwright "
                    "&& venv/bin/playwright install chromium"
                ) from e
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                self._ctx = await self._browser.new_context(viewport=VIEWPORT)
                self._page = await self._ctx.new_page()
                self._cdp = await self._ctx.new_cdp_session(self._page)
                self._cdp.on("Page.screencastFrame", self._on_frame)
                self._page.on("framenavigated", self._on_navigated)
                await self._page.goto("about:blank")
                await self._cdp.send("Page.startScreencast", {
                    "format": "jpeg", "quality": 55,
                    "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
                    "everyNthFrame": 1,
                })
            except BrowserUnavailable:
                raise
            except Exception as e:
                with contextlib.suppress(Exception):
                    await self._teardown()
                raise BrowserUnavailable(f"Chromium failed to launch: {e}") from e
            self._started = True
            self._touch()
            self._watchdog = asyncio.create_task(self._idle_watch())

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    async def _teardown(self) -> None:
        for obj, meth in ((self._cdp, None), (self._ctx, "close"),
                          (self._browser, "close"), (self._pw, "stop")):
            if obj is None or meth is None:
                continue
            with contextlib.suppress(Exception):
                await getattr(obj, meth)()
        self._pw = self._browser = self._ctx = self._page = self._cdp = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._watchdog:
            self._watchdog.cancel()
        with contextlib.suppress(Exception):
            if self._cdp:
                await self._cdp.send("Page.stopScreencast")
        await self._teardown()
        self._started = False

    # ── screencast → subscribers ─────────────────────────────────────────────
    def _on_frame(self, params: dict) -> None:
        sid = params.get("sessionId")
        if self._cdp is not None and sid is not None:
            asyncio.create_task(self._ack(sid))
        data = params.get("data")
        if not data or not self._subs:
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            return
        for ws in list(self._subs):
            if ws in self._busy:
                continue  # drop this frame for a lagging subscriber
            self._busy.add(ws)
            asyncio.create_task(self._send_frame(ws, raw))

    async def _ack(self, sid: str) -> None:
        with contextlib.suppress(Exception):
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": sid})

    async def _send_frame(self, ws: Any, raw: bytes) -> None:
        try:
            await ws.send_bytes(raw)
        except Exception:
            self._subs.discard(ws)
        finally:
            self._busy.discard(ws)

    def _on_navigated(self, frame: Any) -> None:
        # Only the main frame matters for the URL bar.
        try:
            if self._page is not None and frame == self._page.main_frame:
                asyncio.create_task(self._broadcast_nav())
        except Exception:
            pass

    async def _broadcast_nav(self) -> None:
        if self._page is None:
            return
        with contextlib.suppress(Exception):
            url = self._page.url
            title = await self._page.title()
            await self.broadcast_json({"type": "nav", "url": url, "title": title})

    async def broadcast_json(self, obj: dict) -> None:
        for ws in list(self._subs):
            with contextlib.suppress(Exception):
                await ws.send_json(obj)

    # ── subscribers (pane WebSockets) ────────────────────────────────────────
    async def add_subscriber(self, ws: Any) -> None:
        await self.start()
        self._subs.add(ws)
        self._touch()
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "ready", "width": VIEWPORT["width"], "height": VIEWPORT["height"]})
            await ws.send_json({"type": "nav", "url": self._page.url, "title": await self._page.title()})

    def remove_subscriber(self, ws: Any) -> None:
        self._subs.discard(ws)
        self._busy.discard(ws)
        self._touch()

    # ── input (from the pane) ────────────────────────────────────────────────
    async def handle_input(self, msg: dict) -> None:
        self._touch()
        if self._cdp is None:
            return
        t = msg.get("t")
        try:
            if t == "mouse":
                await self._mouse(msg)
            elif t == "wheel":
                await self._wheel(msg)
            elif t == "key":
                await self._key(msg)
            elif t == "navigate":
                await self.navigate(str(msg.get("url") or ""))
        except Exception:
            pass

    @staticmethod
    def _clamp(v: Any, hi: int) -> float:
        try:
            return float(max(0, min(hi, int(v))))
        except Exception:
            return 0.0

    async def _mouse(self, msg: dict) -> None:
        x = self._clamp(msg.get("x"), VIEWPORT["width"])
        y = self._clamp(msg.get("y"), VIEWPORT["height"])
        action = msg.get("action")
        cdp_type = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}.get(action)
        if not cdp_type:
            return
        params: dict[str, Any] = {"type": cdp_type, "x": x, "y": y}
        if action in ("down", "up"):
            params["button"] = msg.get("button", "left")
            params["clickCount"] = 1
        await self._cdp.send("Input.dispatchMouseEvent", params)

    async def _wheel(self, msg: dict) -> None:
        await self._cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": self._clamp(msg.get("x"), VIEWPORT["width"]),
            "y": self._clamp(msg.get("y"), VIEWPORT["height"]),
            "deltaX": float(msg.get("dx") or 0),
            "deltaY": float(msg.get("dy") or 0),
        })

    async def _key(self, msg: dict) -> None:
        action = msg.get("action")
        cdp_type = {"down": "keyDown", "up": "keyUp", "char": "char"}.get(action)
        if not cdp_type:
            return
        params: dict[str, Any] = {"type": cdp_type}
        key = msg.get("key")
        text = msg.get("text") or ""
        if key:
            params["key"] = key
        if text:
            params["text"] = text
            if cdp_type == "keyDown":
                params["type"] = "keyDown"
        await self._cdp.send("Input.dispatchKeyEvent", params)

    # ── high-level actions (used by agent MCP tools) ─────────────────────────
    async def navigate(self, url: str) -> None:
        await self.start()
        self._touch()
        if not url:
            return
        if not url.startswith(("http://", "https://", "about:", "data:", "file:")):
            url = "https://" + url
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._broadcast_nav()

    async def click(self, selector: str) -> None:
        await self.start()
        self._touch()
        await self._page.click(selector, timeout=10000)

    async def type_text(self, text: str, selector: str | None = None) -> None:
        await self.start()
        self._touch()
        if selector:
            await self._page.fill(selector, text, timeout=10000)
        else:
            await self._page.keyboard.type(text)

    async def snapshot(self, max_chars: int = 4000) -> dict:
        await self.start()
        self._touch()
        url = self._page.url
        title = await self._page.title()
        try:
            text = await self._page.inner_text("body", timeout=5000)
        except Exception:
            text = ""
        return {"url": url, "title": title, "text": (text or "")[:max_chars]}

    # ── idle watchdog ────────────────────────────────────────────────────────
    async def _idle_watch(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(_WATCH_INTERVAL)
                idle = time.monotonic() - self._last_activity
                if not self._subs and idle > _IDLE_GRACE:
                    break
        except asyncio.CancelledError:
            return
        await close_session(self.key)


# ── registry ────────────────────────────────────────────────────────────────
async def get_or_create(key: str) -> BrowserSession:
    """Return the live session for `key` (a project cwd), creating it if needed."""
    async with _REGISTRY_LOCK:
        sess = _SESSIONS.get(key)
        if sess is None or sess._closed:
            sess = BrowserSession(key)
            _SESSIONS[key] = sess
    await sess.start()
    return sess


async def close_session(key: str) -> None:
    async with _REGISTRY_LOCK:
        sess = _SESSIONS.pop(key, None)
    if sess is not None:
        await sess.close()


async def close_all() -> None:
    """Close every live browser session (graceful shutdown)."""
    async with _REGISTRY_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for sess in sessions:
        with contextlib.suppress(Exception):
            await sess.close()
