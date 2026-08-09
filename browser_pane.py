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

import browser_backends as _backends

# Frame / viewport geometry — the frontend maps pointer coordinates into this
# exact space, so it MUST match the client contract (BrowserTab.tsx).
VIEWPORT = {"width": 1280, "height": 720}
# The page RENDERS at VIEWPORT, but the screencast is downscaled to STREAM before JPEG
# encoding. Per-frame bytes on the operator's connection (cockpit WS → proxy → device)
# are the dominant lag source — worst on mobile and remote (Cloak Manager) profiles.
# 960×540 q45 is ~3× lighter than native 1280×720 q55 while staying readable for forms.
# STREAM is 16:9 like VIEWPORT, so the displayed frame maps 1:1 to pointer coordinates —
# the downscale never affects clicks (input is dispatched in VIEWPORT space, not pixels).
STREAM = {"width": 960, "height": 540, "quality": 45}
_IDLE_GRACE = 120.0          # close the browser this long after the last activity with no subscribers
_WATCH_INTERVAL = 15.0       # idle-watchdog tick

# ── keyboard: DOM key name → (Windows virtual key code, DOM code) ─────────────
# Chromium derives an *editing command* (delete a char, move the caret, submit a
# form) from the event's virtual key code — NOT from `key`. A dispatchKeyEvent
# carrying only {"key": "Backspace"} fires a JS keydown and does nothing else, so
# the pane looked like it "typed but could not erase". Everything below exists so
# the non-printable keys actually act (verified against real Chromium).
_VKEYS: "dict[str, tuple[int, str]]" = {
    "Backspace": (8, "Backspace"), "Tab": (9, "Tab"), "Enter": (13, "Enter"),
    "Shift": (16, "ShiftLeft"), "Control": (17, "ControlLeft"), "Alt": (18, "AltLeft"),
    "CapsLock": (20, "CapsLock"), "Escape": (27, "Escape"), " ": (32, "Space"),
    "PageUp": (33, "PageUp"), "PageDown": (34, "PageDown"),
    "End": (35, "End"), "Home": (36, "Home"),
    "ArrowLeft": (37, "ArrowLeft"), "ArrowUp": (38, "ArrowUp"),
    "ArrowRight": (39, "ArrowRight"), "ArrowDown": (40, "ArrowDown"),
    "Insert": (45, "Insert"), "Delete": (46, "Delete"),
    "Meta": (91, "MetaLeft"), "ContextMenu": (93, "ContextMenu"),
}
for _i in range(1, 13):  # F1–F12
    _VKEYS[f"F{_i}"] = (111 + _i, f"F{_i}")

# Modifier bitmask shared with the client (BrowserTab.tsx): CDP's own encoding.
_MOD_ALT, _MOD_CTRL, _MOD_META, _MOD_SHIFT = 1, 2, 4, 8


def _key_info(key: str) -> "tuple[int, str]":
    """(windowsVirtualKeyCode, code) for a DOM key name — (0, "") when unknown.

    Punctuation is deliberately left unmapped: its virtual key code is keyboard-layout
    specific, and printable keys insert through the event's `text` anyway.
    """
    if key in _VKEYS:
        return _VKEYS[key]
    if len(key) == 1:
        ch = key.upper()
        if "A" <= ch <= "Z":
            return ord(ch), f"Key{ch}"
        if "0" <= ch <= "9":
            return ord(ch), f"Digit{ch}"
    return 0, ""

# Branded start page — shown instead of a bare white about:blank so a freshly
# opened pane reads as "ready, type a URL" rather than blank/broken. Encoded as a
# base64 data URL so Chromium renders it (and thus emits a screencast frame).
_START_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'></head>"
    "<body style=\"margin:0;height:100vh;display:flex;align-items:center;"
    "justify-content:center;background:#0d0d0d;color:#6e7681;"
    "font-family:system-ui,-apple-system,Segoe UI,sans-serif\">"
    "<div style='text-align:center'><div style='font-size:34px;margin-bottom:10px'>&#127760;</div>"
    "<div style='font-size:14px'>Type a URL above to start browsing</div></div></body></html>"
)
_START_URL = "data:text/html;base64," + base64.b64encode(_START_HTML.encode("utf-8")).decode("ascii")

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
        # spec-065 tabs: a session holds MANY pages; _page/_cdp are always the ACTIVE
        # tab's, so every existing single-page path (input, navigate, screencast, prime)
        # is unchanged. _tabs maps a stable string id → Playwright Page; _active_id is the
        # shown tab. New pages (agent window.open / target=_blank / operator "+") are
        # adopted automatically and foregrounded.
        self._tabs: "dict[str, Any]" = {}
        self._active_id: "str | None" = None
        self._tab_seq = 0
        self._switch_lock = asyncio.Lock()
        self._owns_browser = True   # False for connected/external backends — disconnect, don't kill
        self._owns_page = False     # True when we created our own tab inside a shared profile
        self._shared_ctx = False    # True when the context is shared with other projects
        self.backend = "builtin"    # spec-066: which backend acquired this session (for the pane header)
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._subs: "set[Any]" = set()       # subscriber WebSocketResponse objects
        self._busy: "set[Any]" = set()       # subscribers with an in-flight send (frame-drop gate)
        self._last_frame: "bytes | None" = None  # most recent JPEG — replayed to late subscribers
        self._last_activity = time.monotonic()
        self._watchdog: "asyncio.Task | None" = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            # spec-066: the browser handle is acquired via the pluggable backend
            # (builtin / cloakbrowser / external-cdp). Everything below — screencast,
            # input, agent tools — is backend-agnostic CDP.
            try:
                acq = await _backends.acquire(self.key, VIEWPORT)
            except _backends.BackendError as e:
                raise BrowserUnavailable(str(e)) from e
            except Exception as e:  # pragma: no cover - defensive
                raise BrowserUnavailable(f"Browser backend failed: {e}") from e
            try:
                self._pw = acq.pw
                self._browser = acq.browser
                self._ctx = acq.context
                self._owns_browser = acq.owns_browser
                self._owns_page = acq.owns_page
                self._shared_ctx = acq.shared_context
                self.backend = acq.backend
                # Browser death (process killed / OOM) — recover instead of a frozen pane.
                self._browser.on("disconnected", self._on_disconnected)
                # Adopt every page the context already has (a logged-in external profile
                # may arrive with several tabs) and watch for pages opened later.
                # NOT when the context is shared with other projects: those pre-existing
                # tabs belong to THEM, and adopting them would let one project's pane
                # stream — and navigate away — another project's page.
                if not self._shared_ctx:
                    for p in list(self._ctx.pages):
                        self._adopt_page(p)
                active = acq.page
                self._adopt_page(active)
                # An external/connected browser keeps its own (possibly logged-in) page;
                # only push the branded start page when we launched a fresh one ourselves.
                if self._owns_browser:
                    with contextlib.suppress(Exception):
                        await active.goto(_START_URL)
                self._ctx.on("page", self._on_new_page)
                # Bind the active tab: creates its CDP session, wires the screencast +
                # crash/nav listeners, and starts streaming. (No subscribers yet → no prime.)
                await self._bind_active(active, prime=False)
            except Exception as e:
                with contextlib.suppress(Exception):
                    await self._teardown()
                raise BrowserUnavailable(f"Browser session failed to initialise: {e}") from e
            self._started = True
            self._touch()
            self._watchdog = asyncio.create_task(self._idle_watch())

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    async def _teardown(self) -> None:
        # An external/connected browser (Cloak Manager profile, remote CDP) must NOT
        # be closed — that would kill the operator's persistent, logged-in session.
        # We only disconnect (pw.stop) and leave its context/pages intact.
        if self._owns_browser:
            steps = ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop"))
        else:
            # We do own the tab we opened inside someone else's browser — close it, or a
            # shared profile accumulates a dead tab on every session restart.
            steps = ((self._page, "close"),) if self._owns_page else ()
            steps = steps + ((self._pw, "stop"),)
        for obj, meth in steps:
            if obj is None:
                continue
            with contextlib.suppress(Exception):
                await getattr(obj, meth)()
        self._pw = self._browser = self._ctx = self._page = self._cdp = None
        self._tabs.clear()
        self._active_id = None

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

    def _is_alive(self) -> bool:
        """True if the session can still serve frames (or has not started yet).

        Death is signalled either by the ``disconnected`` event (sets ``_closed``)
        or, as a race-safety net, by a present browser reporting not-connected.
        """
        if self._closed:
            return False
        if not self._started or self._browser is None:
            return True  # not started / pre-browser — start() will (re)build it
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    def _on_crash(self, _page: Any) -> None:
        """Renderer crashed (commonly OOM). Tell the pane; the browser process
        may survive, so a reload/navigate can recover."""
        asyncio.create_task(self.broadcast_json({
            "type": "error",
            "message": "The page crashed (likely out of memory). Reconnect or navigate to retry.",
        }))

    def _on_disconnected(self, _browser: Any) -> None:
        """Browser process died (OOM-killed / crashed). Retire this session so the
        next get_or_create() builds a fresh one instead of reusing a dead handle."""
        self._closed = True
        self._started = False
        self._last_frame = None
        asyncio.create_task(close_session(self.key, self))

    # ── tabs ─────────────────────────────────────────────────────────────────
    def _adopt_page(self, page: Any) -> str:
        """Track a page under a stable string id (idempotent) and watch for its close."""
        for tid, p in self._tabs.items():
            if p is page:
                return tid
        self._tab_seq += 1
        tid = f"t{self._tab_seq}"
        self._tabs[tid] = page
        with contextlib.suppress(Exception):
            page.on("close", lambda _p=None, _tid=tid: self._on_tab_closed(_tid))
        return tid

    def _id_of(self, page: Any) -> "str | None":
        return next((tid for tid, p in self._tabs.items() if p is page), None)

    async def _bind_active(self, page: Any, *, prime: bool = True) -> None:
        """Make `page` the active tab: move the CDP screencast + nav/crash listeners onto
        it. The previous active tab's screencast is stopped and its CDP detached (only ONE
        tab streams at a time → minimal memory/bandwidth). `prime` pushes a fresh frame +
        nav + tab list to current subscribers (skip at startup when there are none)."""
        if page is self._page and self._cdp is not None:
            if prime:
                await self._broadcast_nav()
                await self._reprime_subs()
            return
        async with self._switch_lock:
            old = self._page
            if self._cdp is not None:
                with contextlib.suppress(Exception):
                    await self._cdp.send("Page.stopScreencast")
                with contextlib.suppress(Exception):
                    await self._cdp.detach()
                self._cdp = None
            if old is not None and old is not page:
                with contextlib.suppress(Exception):
                    old.remove_listener("framenavigated", self._on_navigated)
                with contextlib.suppress(Exception):
                    old.remove_listener("crash", self._on_crash)
            self._page = page
            self._active_id = self._id_of(page)
            self._last_frame = None   # the cached frame belonged to the old tab
            self._cdp = await self._ctx.new_cdp_session(page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            page.on("framenavigated", self._on_navigated)
            page.on("crash", self._on_crash)
            await self._cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": STREAM["quality"],
                "maxWidth": STREAM["width"], "maxHeight": STREAM["height"],
                "everyNthFrame": 1,
            })
        if prime:
            await self._broadcast_nav()
            await self._reprime_subs()

    def _on_new_page(self, page: Any) -> None:
        asyncio.create_task(self._handle_new_page(page))

    async def _handle_new_page(self, page: Any) -> None:
        # In a shared profile context this fires for EVERY project's new tab. Adopt only
        # pages opened from one of our own (OAuth/login popups, target=_blank) — otherwise
        # this pane would hijack a tab that belongs to another project.
        if self._shared_ctx:
            opener = None
            with contextlib.suppress(Exception):
                opener = await page.opener()
            if opener is None or self._id_of(opener) is None:
                return
        self._adopt_page(page)
        # Foreground the new tab — mirror a real browser opening target=_blank / window.open,
        # so the operator follows the agent into the page it just spawned.
        with contextlib.suppress(Exception):
            await self._bind_active(page)

    def _on_tab_closed(self, tid: str) -> None:
        asyncio.create_task(self._handle_tab_closed(tid))

    async def _handle_tab_closed(self, tid: str) -> None:
        self._tabs.pop(tid, None)
        if self._active_id == tid:
            remaining = list(self._tabs.values())
            if remaining:
                await self._bind_active(remaining[-1])
            else:
                self._active_id = self._page = self._cdp = None
        await self._broadcast_tabs()

    async def activate_tab(self, tid: str) -> None:
        page = self._tabs.get(tid)
        if page is not None and tid != self._active_id:
            await self._bind_active(page)

    async def new_tab(self, url: str = "") -> None:
        await self.start()
        if self._ctx is None:
            return
        page = await self._ctx.new_page()    # also fires _on_new_page (adopt + foreground)
        self._adopt_page(page)               # idempotent — guarantee it's tracked now
        await self._bind_active(page)
        if url:
            await self.navigate(url)
        elif self._owns_browser:
            with contextlib.suppress(Exception):
                await page.goto(_START_URL)
            await self._broadcast_nav()
        await self._broadcast_tabs()

    async def close_tab(self, tid: str) -> None:
        if len(self._tabs) <= 1:
            return   # always keep at least one tab
        page = self._tabs.get(tid)
        if page is None:
            return
        with contextlib.suppress(Exception):
            await page.close()   # fires _on_tab_closed → switch active + broadcast

    async def _tabs_payload(self) -> dict:
        tabs: "list[dict]" = []
        for tid, page in list(self._tabs.items()):
            try:
                url = page.url
            except Exception:
                continue
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append({"id": tid, "url": url, "title": title or url, "active": tid == self._active_id})
        return {"type": "tabs", "tabs": tabs, "activeId": self._active_id}

    async def _broadcast_tabs(self) -> None:
        with contextlib.suppress(Exception):
            await self.broadcast_json(await self._tabs_payload())

    async def _reprime_subs(self) -> None:
        """Capture one frame of the active tab and push it to all subscribers, so a tab
        switch updates the view immediately instead of waiting for the next page change."""
        frame = await self._capture_frame()
        if not frame:
            return
        self._last_frame = frame
        for ws in list(self._subs):
            if ws in self._busy:
                continue
            self._busy.add(ws)
            asyncio.create_task(self._send_frame(ws, frame))

    # ── screencast → subscribers ─────────────────────────────────────────────
    def _on_frame(self, params: dict) -> None:
        sid = params.get("sessionId")
        if self._cdp is not None and sid is not None:
            asyncio.create_task(self._ack(sid))
        data = params.get("data")
        if not data:
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            return
        # Cache the latest frame even when nobody is watching: the screencast only
        # emits on CHANGE, so a subscriber that joins a static page would otherwise
        # never receive a frame (the "Browser stream not yet ready" blank pane).
        self._last_frame = raw
        if not self._subs:
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
        # The active tab's title/url just changed — refresh the strip too.
        await self._broadcast_tabs()

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
            await ws.send_json({"type": "ready", "width": VIEWPORT["width"], "height": VIEWPORT["height"], "backend": self.backend})
            await ws.send_json({"type": "nav", "url": self._page.url, "title": await self._page.title()})
            await ws.send_json(await self._tabs_payload())
        # Prime the new subscriber with the current page so a static page renders
        # immediately instead of a blank "stream not ready" pane.
        await self._prime(ws)

    async def _prime(self, ws: Any) -> None:
        """Send the current page state to a freshly-joined subscriber.

        Replays the cached frame if there is one; otherwise forces a one-off
        screenshot (covers a static start page that has not changed since the
        screencast began).
        """
        frame = self._last_frame
        if frame is None:
            frame = await self._capture_frame()
            if frame is not None:
                self._last_frame = frame
        if frame:
            self._busy.add(ws)
            await self._send_frame(ws, frame)

    async def _capture_frame(self) -> "bytes | None":
        if self._cdp is None:
            return None
        try:
            res = await self._cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": STREAM["quality"]})
            return base64.b64decode(res["data"])
        except Exception:
            return None

    def remove_subscriber(self, ws: Any) -> None:
        self._subs.discard(ws)
        self._busy.discard(ws)
        self._touch()

    # ── input (from the pane) ────────────────────────────────────────────────
    async def handle_input(self, msg: dict, ws: Any = None) -> None:
        self._touch()
        t = msg.get("t")
        # Tab controls act on the context (not the active page's CDP), so they run before
        # the _cdp guard — they must work even between tab switches.
        if t in ("tab.activate", "tab.new", "tab.close"):
            try:
                if t == "tab.activate":
                    await self.activate_tab(str(msg.get("id") or ""))
                elif t == "tab.new":
                    await self.new_tab(str(msg.get("url") or ""))
                else:
                    await self.close_tab(str(msg.get("id") or ""))
            except Exception:
                pass
            return
        if self._cdp is None:
            return
        try:
            if t == "mouse":
                await self._mouse(msg)
            elif t == "wheel":
                await self._wheel(msg)
            elif t == "key":
                await self._key(msg)
            elif t == "paste":
                await self._paste(msg)
            elif t == "copy":
                await self._copy(ws)
            elif t == "navigate":
                await self.navigate(str(msg.get("url") or ""))
            elif t in ("back", "forward", "reload"):
                await self._history(t)
        except Exception:
            pass

    async def _history(self, what: str) -> None:
        """Back / forward / reload for the active page (the pane's nav buttons)."""
        page = self._page
        if page is None:
            return
        if what == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif what == "forward":
            await page.go_forward(wait_until="domcontentloaded")
        else:
            await page.reload(wait_until="domcontentloaded")
        await self._broadcast_nav()

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
        try:
            mods = int(msg.get("mods") or 0) & 0xF
            buttons = int(msg.get("buttons") or 0) & 0x1F
        except Exception:
            mods, buttons = 0, 0
        params: dict[str, Any] = {"type": cdp_type, "x": x, "y": y,
                                  "modifiers": mods, "buttons": buttons}
        if action in ("down", "up"):
            params["button"] = msg.get("button", "left")
            # Chromium turns clickCount 2/3 into select-word / select-line. Always
            # sending 1 is why double-click never selected a word in the pane.
            try:
                params["clickCount"] = max(1, min(3, int(msg.get("clickCount") or 1)))
            except Exception:
                params["clickCount"] = 1
        else:
            # A move with a held button is a DRAG (text selection). Without `button`
            # + `buttons` set, Chromium reads it as a plain hover and selects nothing.
            params["button"] = "left" if buttons & 1 else ("right" if buttons & 2 else "none")
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
        text = str(msg.get("text") or "")
        try:
            mods = int(msg.get("mods") or 0) & 0xF
        except Exception:
            mods = 0

        # A bare `char` event only inserts text (the mobile soft keyboard path).
        if cdp_type == "char":
            if text:
                await self._cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": text})
            return

        key = str(msg.get("key") or "")
        vk, code = _key_info(key)
        params: dict[str, Any] = {"type": cdp_type, "modifiers": mods}
        if key:
            params["key"] = key
        if code:
            params["code"] = code
        if vk:
            params["windowsVirtualKeyCode"] = vk
            params["nativeVirtualKeyCode"] = vk
        if cdp_type == "keyDown":
            if text and not (mods & (_MOD_CTRL | _MOD_META)):
                # Printable: `text` is what actually inserts the character.
                params["text"] = text
                params["unmodifiedText"] = text.lower() if mods & _MOD_SHIFT else text
            else:
                # A shortcut (Ctrl/⌘ held) or a non-printable key must NOT carry text,
                # or Chromium would insert a stray character alongside the command.
                params["type"] = "rawKeyDown"
            if msg.get("repeat"):
                params["autoRepeat"] = True
        await self._cdp.send("Input.dispatchKeyEvent", params)

    async def _paste(self, msg: dict) -> None:
        """Insert operator-supplied text at the caret (Input.insertText).

        Ctrl+V cannot work by itself: the remote Chromium has its own, empty clipboard.
        The pane reads the operator's clipboard and ships the text here instead — this is
        how a password manager entry gets into a login form.
        """
        text = str(msg.get("text") or "")
        if text:
            await self._cdp.send("Input.insertText", {"text": text})

    async def _copy(self, ws: Any) -> None:
        """Read the remote page's current text selection and hand it back to the
        requesting pane, which writes it into the OPERATOR's clipboard.

        A forwarded Ctrl+C would only copy inside the remote Chromium's own
        (server-side, invisible) clipboard — never the operator's — so the
        selection has to be pulled out explicitly and shipped to the client.
        """
        if ws is None:
            return
        text = ""
        with contextlib.suppress(Exception):
            res = await self._cdp.send("Runtime.evaluate", {
                "expression": "window.getSelection().toString()",
                "returnByValue": True,
            })
            text = res.get("result", {}).get("value") or ""
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "clipboard", "text": text})

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
        await close_session(self.key, self)


# ── registry ────────────────────────────────────────────────────────────────
async def get_or_create(key: str) -> BrowserSession:
    """Return the live session for `key` (a project cwd), creating it if needed.

    A session whose browser has died (crash/OOM/disconnect) is treated as absent
    and rebuilt — otherwise a dead handle would serve a frozen "Chrome Error" pane.
    """
    async with _REGISTRY_LOCK:
        sess = _SESSIONS.get(key)
        if sess is None or not sess._is_alive():
            sess = BrowserSession(key)
            _SESSIONS[key] = sess
    await sess.start()
    return sess


async def close_session(key: str, sess: "BrowserSession | None" = None) -> None:
    """Close and deregister a session. When `sess` is given, only act if it is
    still the registered session for `key` (identity guard) — prevents a dying
    session's watchdog from evicting a fresh replacement that reused the key."""
    async with _REGISTRY_LOCK:
        cur = _SESSIONS.get(key)
        if sess is not None and cur is not sess:
            return
        _SESSIONS.pop(key, None)
    target = sess if sess is not None else cur
    if target is not None:
        await target.close()


async def close_all() -> None:
    """Close every live browser session (graceful shutdown)."""
    async with _REGISTRY_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for sess in sessions:
        with contextlib.suppress(Exception):
            await sess.close()
