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
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)

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


# ── agent snapshot: interactive elements ───────────────────────────────────────
# browser_snapshot used to return page TEXT only — an agent aiming a CSS selector at
# a form had nothing but that text to go on, and blind-guessed. That's exactly what
# breaks on a split-digit code field (N unlabeled <input maxlength=1>) and an
# icon-only submit button: there's no visible text to guess a selector from. This
# collects id/name/class/placeholder/role/aria-label for every interactive element so
# the agent can read them off instead of guessing.
#
# The ARIA roles (option/listbox/combobox/menuitem) exist for one specific gap: a
# typeahead/autocomplete widget (Fluent UI TagPicker, MUI Combobox, ...) renders its
# suggestion popup as plain <div>/<li> with role="option" inside role="listbox" — not
# an <input> or <button> — so without these the popup only showed up as loose text at
# the bottom of the snapshot with no selector to click. aria-selected/aria-expanded
# are collected alongside so the agent can tell which suggestion is highlighted and
# whether the popup is even open, without a second round-trip.
#
# input[type=hidden] is EXCLUDED outright (never rendered, never interactable — an
# enterprise app like PeopleSoft can stuff dozens of them, ICType/ICSID/SpMfuMax/...,
# ahead of any real control in DOM order). Everything else still gets collected up to
# a generous pool (400) and then SORTED visible-first before the final 60-item cap, so
# an off-screen/collapsed element never crowds out a real, clickable one just by
# happening to sit earlier in the document.
_INTERACTIVE_ELEMENTS_JS = """
() => {
    const all = Array.from(document.querySelectorAll(
        'input:not([type="hidden"]), button, select, textarea, a[href], [role="button"], [role="link"], ' +
        '[role="option"], [role="listbox"], [role="combobox"], [role="menuitem"], ' +
        '[role="checkbox"], [role="radio"], [contenteditable="true"]'
    )).slice(0, 400).map(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const out = { tag: el.tagName.toLowerCase() };
        for (const attr of ['type', 'id', 'name', 'placeholder', 'maxlength', 'href', 'role',
                             'aria-label', 'aria-selected', 'aria-expanded', 'title']) {
            const v = el.getAttribute(attr);
            if (v) out[attr] = v.length > 50 ? v.slice(0, 50) + '…' : v;
        }
        if (el.className && typeof el.className === 'string' && el.className.trim()) {
            out['class'] = el.className.length > 50 ? el.className.slice(0, 50) + '…' : el.className;
        }
        const text = (el.innerText || el.value || '').trim();
        if (text) out.text = text.length > 40 ? text.slice(0, 40) + '…' : text;
        out.visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        // A field the browser itself has marked invalid (failed :invalid CSS
        // validation, or aria-invalid) after a rejected submit — this is the "which
        // field is highlighted" the plain-text snapshot alone can't answer.
        try {
            if (el.matches(':invalid') || el.getAttribute('aria-invalid') === 'true') out.invalid = true;
        } catch (e) {}
        // A <select>'s own value/options are NOT text content the innerText grab above
        // would ever see — without this an agent has to guess an <option>'s value
        // attribute blind, which is exactly why select_option() calls kept missing.
        if (el.tagName === 'SELECT') {
            out.selected = el.value;
            out.options = Array.from(el.options).slice(0, 20).map(o => ({
                value: o.value,
                label: (o.label || o.textContent || '').trim().slice(0, 40),
            }));
        }
        // Checked state — mirrors the <select> marker above. Without this a
        // checkbox toggled via its <label> (not the input itself) has no visible
        // confirmation anywhere short of a screenshot; a real native checkbox/radio
        // reports el.checked, a custom ARIA widget reports aria-checked instead.
        if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
            out.checked = el.checked;
        } else {
            const role = el.getAttribute('role');
            if (role === 'checkbox' || role === 'radio') {
                const ac = el.getAttribute('aria-checked');
                if (ac !== null) out.checked = ac === 'true' ? true : (ac === 'false' ? false : ac);
            }
        }
        return out;
    });
    all.sort((a, b) => (b.visible === true) - (a.visible === true));
    return all.slice(0, 60);
}
"""


def _format_interactive_elements(elements: "list[dict]") -> str:
    """Render the JS collector's output as one line per element, e.g.:
    ``[3] [x] input type="checkbox" id="agree"`` — enough to build a
    ``[id="..."]`` selector and read current state without guessing or a screenshot.
    """
    lines: "list[str]" = []
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            continue
        bits = [el.get("tag") or "?"]
        for k in ("type", "id", "name", "class", "placeholder", "maxlength", "role",
                  "aria-label", "aria-selected", "aria-expanded", "title", "href"):
            v = el.get(k)
            if v:
                bits.append(f'{k}="{v}"')
        if el.get("invalid"):
            bits.append("⚠INVALID")
        if el.get("visible") is False:
            bits.append("(hidden)")
        checked = el.get("checked")
        prefix = ""
        if checked is True:
            prefix = "[x] "
        elif checked is False:
            prefix = "[ ] "
        elif checked is not None:
            prefix = f"[{checked}] "
        line = f"[{i}] " + prefix + " ".join(bits)
        text = el.get("text")
        if text:
            line += f' — "{text}"'
        opts = el.get("options")
        if isinstance(opts, list) and opts:
            selected = el.get("selected")
            opt_strs = []
            for o in opts:
                if not isinstance(o, dict):
                    continue
                val, label = o.get("value", ""), o.get("label", "")
                mark = "*" if val == selected else " "
                opt_strs.append(f'{mark}{val}="{label}"' if label and label != val else f"{mark}{val}")
            line += "\n    options: " + ", ".join(opt_strs)
        lines.append(line)
    return "\n".join(lines)


# ── dead-connection classifier (agent tools + operator input share this) ──────
# "Connection closed while reading from the driver" is Playwright's own message
# when the LOCAL Node driver subprocess pipe has died — nothing to do with a bad
# selector. The other markers cover the equivalent CDP-level phrasing (a detached
# target, a closed browser/session). Deliberately narrow: a plain Playwright
# TimeoutError ("waiting for selector ... exceeded") must NOT match here, or a
# simple bad selector would retire and rebuild a perfectly healthy session for
# nothing.
_DEAD_CONNECTION_MARKERS = (
    "connection closed",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "session closed",
    "websocket connection closed",
    "browser has disconnected",
)


def looks_like_dead_connection(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _DEAD_CONNECTION_MARKERS)


def _is_strict_mode_violation(exc: Exception) -> bool:
    """True for Playwright's own "locator(...) resolved to N elements" error — the
    Locator API's built-in ambiguity guard (verified empirically: Page.click() does
    NOT have this guard and silently acts on the first match, which is why every
    action method here uses .locator(selector).click()/... instead). This error
    must propagate immediately rather than being swallowed by the iframe fallback
    or the retry wrapper — the selector itself is the problem, not the connection."""
    return "strict mode violation" in str(exc).lower()


# ── iframes ─────────────────────────────────────────────────────────────────────
# A Google Sign-In button, a reCAPTCHA/hCaptcha checkbox, an embedded payment
# widget — all commonly render inside a same- OR cross-origin <iframe>. page-level
# inner_text()/evaluate() only ever look at the MAIN document; content inside an
# iframe is a genuinely separate document they never traverse — an element that is
# right there on screen reads as "not on the page at all". Playwright talks to
# every frame over CDP directly, which is NOT bound by the browser's own
# same-origin policy the way the page's own JS would be — so this is fixable
# without touching (or defeating) whatever anti-bot check the iframe itself runs.
_MAX_SCANNED_FRAMES = 12    # snapshot: cheap read-only evaluate() calls
_MAX_FALLBACK_FRAMES = 8    # click/type_text: each attempt auto-waits, keep it bounded


def _snippet(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _truncate_text(text: str, max_chars: int) -> str:
    """Cut at the last whitespace at/before max_chars instead of mid-word, and say
    so explicitly. A hard [:max_chars] slice ending mid-word ("...FLC Enrol…") reads
    as if the page just ends there — an agent has no way to tell "that's everything"
    from "that's where I stopped looking" without an explicit notice, and had to
    re-snapshot with a narrower query multiple times to read past it blind.
    """
    if len(text) <= max_chars:
        return text
    cut = max(text.rfind(" ", 0, max_chars), text.rfind("\n", 0, max_chars))
    if cut < max_chars - 200:  # no reasonable boundary nearby — just hard-cut
        cut = max_chars
    shown = text[:cut].rstrip()
    return (
        f"{shown}\n…[truncated: {len(shown)} of {len(text)} chars shown — "
        "call browser_snapshot again with a larger max_chars to see more]"
    )


# ── captcha detection / token injection ────────────────────────────────────────
# Two independent detection passes, because either one alone has a blind spot: the
# widget <div data-sitekey> carries the richest info (sitekey + data-callback +
# action) but some sites render the widget into a shadow root or remove the div
# after load, while the <iframe> the vendor injects is always present but only
# carries the sitekey. Running both and de-duplicating by (kind, sitekey) is what
# makes this work on a form that was built either way.
_DETECT_CAPTCHA_JS = """
() => {
  const found = [], seen = new Set();
  const push = (kind, sitekey, extra) => {
    if (!kind || !sitekey) return;
    const id = kind + '|' + sitekey;
    if (seen.has(id)) return;
    seen.add(id);
    found.push(Object.assign({ kind: kind, sitekey: sitekey }, extra || {}));
  };

  document.querySelectorAll('[data-sitekey]').forEach(el => {
    const cls = String(el.className || '') + ' ' + String(el.id || '');
    let kind = 'recaptcha_v2';
    if (el.classList.contains('h-captcha') || /hcaptcha/i.test(cls)) kind = 'hcaptcha';
    else if (el.classList.contains('cf-turnstile') || /turnstile/i.test(cls)) kind = 'turnstile';
    push(kind, el.getAttribute('data-sitekey'), {
      callback: el.getAttribute('data-callback') || null,
      action: el.getAttribute('data-action') || null,
      size: el.getAttribute('data-size') || null,
      source: 'widget'
    });
  });

  document.querySelectorAll('iframe[src]').forEach(f => {
    const src = String(f.src || '');
    let k = null;
    try {
      const u = new URL(src, location.href);
      k = u.searchParams.get('k') || u.searchParams.get('sitekey');
    } catch (e) {}
    if (/google\\.com\\/recaptcha|recaptcha\\.net/.test(src)) push('recaptcha_v2', k, { source: 'iframe' });
    else if (/hcaptcha\\.com/.test(src)) push('hcaptcha', k, { source: 'iframe' });
    else if (/challenges\\.cloudflare\\.com/.test(src)) {
      const m = src.match(/\\/(0x[A-Za-z0-9_-]{10,})\\//) || src.match(/\\/([0-9]x[A-Za-z0-9]{15,})\\//);
      push('turnstile', k || (m ? m[1] : null), { source: 'iframe' });
    }
  });

  return {
    url: location.href,
    // A full-page Cloudflare interstitial, NOT a widget in a form. Different task
    // shape (needs action/cData/chlPageData) and the token is IP-bound — see the
    // refusal in solve_captcha rather than paying for a token that cannot work.
    challengePage: !!(window._cf_chl_opt || document.getElementById('cf-challenge-running')
                      || document.getElementById('challenge-running')),
    hasResponseField: !!document.querySelector(
      '#g-recaptcha-response, [name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'),
    widgets: found
  };
}
"""

# Writing the token into the hidden response field is necessary but rarely
# sufficient: most sites only learn the captcha passed when the widget's own
# callback fires (that's what enables the submit button / posts the form). We set
# the field, dispatch input+change so any framework binding notices, then hunt for
# the callback three ways — data-callback attribute, reCAPTCHA's internal
# ___grecaptcha_cfg client registry, and hCaptcha/Turnstile's global. Every step is
# reported back so a failure says WHICH half worked instead of just "didn't work".
_INJECT_TOKEN_JS = """
([token, kind, callbackName]) => {
  const report = { fields: [], callbacks: [], notes: [] };
  const selectors = {
    recaptcha_v2: ['#g-recaptcha-response', 'textarea[name="g-recaptcha-response"]', '[name="g-recaptcha-response"]'],
    recaptcha_v3: ['#g-recaptcha-response', '[name="g-recaptcha-response"]'],
    hcaptcha: ['[name="h-captcha-response"]', '[name="g-recaptcha-response"]', '#h-captcha-response'],
    turnstile: ['[name="cf-turnstile-response"]', '#cf-chl-widget-response', '[name="cf_challenge_response"]']
  };

  (selectors[kind] || []).forEach(sel => {
    document.querySelectorAll(sel).forEach(el => {
      el.value = token;
      if (el.style && el.style.display === 'none') el.removeAttribute('aria-hidden');
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      report.fields.push(sel);
    });
  });

  const fire = (fn, label) => {
    if (typeof fn !== 'function') return false;
    try { fn(token); report.callbacks.push(label); return true; }
    catch (e) { report.notes.push(label + ' threw: ' + e.message); return false; }
  };

  if (callbackName) {
    const parts = String(callbackName).split('.');
    let fn = window;
    for (const p of parts) { fn = fn ? fn[p] : null; }
    if (!fire(fn, 'data-callback:' + callbackName)) {
      report.notes.push('data-callback "' + callbackName + '" is not a reachable function');
    }
  }

  // reCAPTCHA keeps every rendered client (and its callback) here. The shape is
  // obfuscated and changes between releases, so walk it structurally: find any
  // object holding a 'callback' function, rather than relying on key names.
  if (kind === 'recaptcha_v2' || kind === 'recaptcha_v3') {
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      const walk = (obj, depth) => {
        if (!obj || depth > 4 || typeof obj !== 'object') return;
        for (const k of Object.keys(obj)) {
          let v;
          try { v = obj[k]; } catch (e) { continue; }
          if (k === 'callback' && typeof v === 'function') fire(v, 'grecaptcha.callback');
          else if (v && typeof v === 'object') walk(v, depth + 1);
        }
      };
      Object.keys(cfg.clients).forEach(id => walk(cfg.clients[id], 0));
    } else {
      report.notes.push('___grecaptcha_cfg.clients absent — widget may render in a shadow root');
    }
  }

  if (report.callbacks.length === 0 && report.fields.length === 0) {
    report.notes.push('no response field and no callback found — is the widget actually on this page?');
  }
  return report;
}
"""


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
        self._profile = ""          # Cloak Manager profile id, handed back on teardown
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
                self._profile = acq.profile
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
            # We own EVERY tab tracked in self._tabs, not just the active self._page —
            # _handle_new_page only ever adopts a page opened FROM one of our own
            # (window.open/target=_blank; a shared-profile popup with a foreign opener
            # is explicitly rejected there). Closing only the active tab here used to
            # leak every OTHER open tab as a permanent zombie renderer process in the
            # shared Cloak Manager profile on every teardown (restart, module
            # disable, ...) — a live incident found profile d33e103d had accumulated
            # 74 renderer processes / 4.4GB RSS over 6 days this way, eventually
            # making its CDP endpoint intermittently unreachable (502s from the
            # Manager) for every project sharing that profile.
            close_steps = tuple((page, "close") for page in self._tabs.values()) if self._owns_page else ()
            steps = close_steps + ((self._pw, "stop"),)
        for obj, meth in steps:
            if obj is None:
                continue
            with contextlib.suppress(Exception):
                await getattr(obj, meth)()
        self._pw = self._browser = self._ctx = self._page = self._cdp = None
        self._tabs.clear()
        self._active_id = None
        # Hand the Manager profile back LAST, once our tabs are actually closed. If we
        # were its only user and we launched it, this starts the idle-stop countdown —
        # without it the profile's Chrome (55 processes across two profiles, software
        # -rendered on a GPU-less VM) simply ran until someone noticed the fans.
        if self._profile:
            profile, self._profile = self._profile, ""
            with contextlib.suppress(Exception):
                await _backends.release_profile(profile, self.key)

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
        _log.warning("browser disconnected (cwd=%s backend=%s) — retiring session", self.key, self.backend)
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
                # Confirm a real click landed — sent only AFTER the CDP dispatch
                # above actually succeeded, never optimistically on receipt. The
                # operator has no other way to tell "the browser is live and just
                # processed my click" from "the pane is frozen and my click went
                # nowhere" — the pane can look identical either way until the next
                # frame happens to change. Only 'down' acks (not every 'move'),
                # matching the moment of an actual click/tap.
                if msg.get("action") == "down" and ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.send_json({"type": "click_ack", "x": msg.get("x"), "y": msg.get("y")})
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
        except Exception as e:
            # Every command here is a raw CDP call on an already-established session —
            # unlike the agent's selector-based click()/type_text(), there is no
            # "element not found"-style recoverable failure at this level. An exception
            # here means the CDP session itself is broken (a dead local Playwright
            # driver pipe, a detached target). Swallowing it silently used to leave the
            # pane showing its last cached frame forever — looking alive, deaf to every
            # click. Surface it (so the operator gets the Reconnect button already
            # wired in the frontend) and retire the session so the next connect rebuilds
            # cleanly instead of reusing the broken one.
            if not self._closed:
                _log.warning(
                    "browser input dispatch failed (cwd=%s backend=%s t=%s), retiring session: %s",
                    self.key, self.backend, t, e,
                )
                with contextlib.suppress(Exception):
                    await self.broadcast_json({
                        "type": "error",
                        "message": f"Browser session lost ({e}). Click Reconnect to rebuild it.",
                    })
                asyncio.create_task(close_session(self.key, self))

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
    async def _retire_if_dead(self, exc: Exception) -> None:
        """A failure from these agent-facing calls means one of two very different
        things: a bad selector / an element that never showed up (a normal usage
        error — retrying would just waste another 10s timeout), or the CDP
        connection itself has died (a dead local Playwright driver pipe, a detached
        target — retrying against the SAME session would fail identically forever,
        which is exactly what made browser_navigate/browser_snapshot loop on
        "Connection closed while reading from the driver" with no recovery). Only
        the second case retires the session, so the next get_or_create() — and
        browser_tools._run_with_retry's single retry — gets a fresh one instead of
        the same corpse. Mirrors handle_input's self-healing for the operator's raw
        input path.
        """
        if not self._closed and looks_like_dead_connection(exc):
            _log.warning("browser session retiring (cwd=%s backend=%s), dead connection: %s", self.key, self.backend, exc)
            with contextlib.suppress(Exception):
                await close_session(self.key, self)

    async def navigate(self, url: str) -> None:
        await self.start()
        self._touch()
        if not url:
            return
        if not url.startswith(("http://", "https://", "about:", "data:", "file:")):
            url = "https://" + url
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._broadcast_nav()
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def _click_one(self, target: Any, selector: str, click_count: int, nth: "int | None") -> None:
        loc = target.locator(selector)
        if nth is not None:
            loc = loc.nth(nth)
        await loc.click(timeout=10000 if target is self._page else 3000, click_count=click_count)

    async def _click_anywhere(self, selector: str, *, click_count: int = 1, nth: "int | None" = None) -> None:
        """page.click() only ever searches the MAIN frame. A same- or cross-origin
        iframe (a Google Sign-In button, a CAPTCHA checkbox, an embedded payment
        widget) is invisible to it even though Playwright itself can reach it. Try
        the main frame first — the common, fast case, at the original generous
        timeout — and only on failure fall back to searching every other frame for
        the same selector, at a shorter timeout each so a genuinely-missing element
        still fails in bounded time.

        Uses Locator.click(), NOT the deprecated page.click(selector) convenience
        method — the latter silently acts on the FIRST of several matches with no
        error, which is exactly how a selector like "a.ps-button:visible,
        button:visible" once clicked "Exit" instead of the intended button and reset
        an in-progress wizard. Locator.click() is strict by default: it raises a
        clear "strict mode violation: locator(...) resolved to N elements" instead.
        That error is deliberately NOT swallowed by the iframe fallback below (an
        ambiguous selector in one frame isn't fixed by trying a different frame,
        and silently landing on an unrelated single match elsewhere would be worse
        than just refusing) — pass `nth` to disambiguate once you know which one.
        """
        try:
            await self._click_one(self._page, selector, click_count, nth)
            return
        except Exception as first_err:
            if looks_like_dead_connection(first_err) or _is_strict_mode_violation(first_err):
                raise
            for frame in list(self._page.frames)[: _MAX_FALLBACK_FRAMES + 1]:
                if frame == self._page.main_frame:
                    continue
                try:
                    await self._click_one(frame, selector, click_count, nth)
                    return
                except Exception as e:
                    if _is_strict_mode_violation(e):
                        raise
                    continue
            raise first_err

    async def _upload_one(self, target: Any, selector: str, path: str, nth: "int | None") -> None:
        loc = target.locator(selector)
        if nth is not None:
            loc = loc.nth(nth)
        await loc.set_input_files(path, timeout=10000 if target is self._page else 3000)

    async def _upload_anywhere(self, selector: str, path: str, nth: "int | None" = None) -> None:
        """Same main-frame-then-iframe fallback and strict-mode short-circuit as
        _click_anywhere. A file input inside a Google/Cloudflare-style embed lives
        in an iframe just as often as a button does, and an ambiguous selector
        here would upload into the WRONG field just as silently as a wrong click."""
        try:
            await self._upload_one(self._page, selector, path, nth)
            return
        except Exception as first_err:
            if looks_like_dead_connection(first_err) or _is_strict_mode_violation(first_err):
                raise
            for frame in list(self._page.frames)[: _MAX_FALLBACK_FRAMES + 1]:
                if frame == self._page.main_frame:
                    continue
                try:
                    await self._upload_one(frame, selector, path, nth)
                    return
                except Exception as e:
                    if _is_strict_mode_violation(e):
                        raise
                    continue
            raise first_err

    async def upload_file(self, selector: str, path: str, nth: "int | None" = None) -> None:
        """Set a file on a ``<input type="file">`` directly, via CDP
        (Playwright's set_input_files → DOM.setFileInputFiles) — the ONLY way to
        upload through this pane. Clicking an "Upload" button opens the OS's own
        native file picker, which lives completely outside the page and is
        invisible to click()/type() alike; typing a path into the input does
        nothing either — browsers refuse programmatic/keyboard text entry into a
        file input as a security measure. This bypasses that dialog entirely and
        works even when the input is visually hidden behind a styled button (the
        common pattern) — set_input_files never depends on the element being
        visible or clickable. Verified to transfer real file bytes over
        connect_over_cdp too (the external-cdp/Cloak Manager backend), not just a
        path reference the remote browser would need shared storage to resolve.
        """
        await self.start()
        self._touch()
        if not os.path.isfile(path):
            raise FileNotFoundError(f"No such file on this server: {path}")
        try:
            await self._upload_anywhere(selector, path, nth)
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def _select_one(self, target: Any, selector: str, value: str, nth: "int | None") -> None:
        loc = target.locator(selector)
        if nth is not None:
            loc = loc.nth(nth)
        timeout = 10000 if target is self._page else 3000
        try:
            await loc.select_option(value=value, timeout=timeout)
        except Exception as e:
            if _is_strict_mode_violation(e):
                raise
            await loc.select_option(label=value, timeout=timeout)

    async def _select_anywhere(self, selector: str, value: str, nth: "int | None" = None) -> None:
        """Same main-frame-then-iframe fallback and strict-mode short-circuit as
        _click_anywhere. Tries `value` as the <option>'s value attribute first,
        then as its visible label — an agent reading the label off a snapshot
        ("Freelancer") won't always know the underlying value attribute the page
        actually uses."""
        try:
            await self._select_one(self._page, selector, value, nth)
            return
        except Exception as first_err:
            if looks_like_dead_connection(first_err) or _is_strict_mode_violation(first_err):
                raise
            for frame in list(self._page.frames)[: _MAX_FALLBACK_FRAMES + 1]:
                if frame == self._page.main_frame:
                    continue
                try:
                    await self._select_one(frame, selector, value, nth)
                    return
                except Exception as e:
                    if _is_strict_mode_violation(e):
                        raise
                    continue
            raise first_err

    async def select_option(self, selector: str, value: str, nth: "int | None" = None) -> None:
        """Choose an <option> in a native <select> directly via CDP
        (Page.selectOption under Playwright's select_option()). Clicking a
        <select> pops OS/browser-native list UI — outside the page, the exact
        same class of problem as a file input's OS picker — so click()+click()
        on an <option> either does nothing or silently misses, which is why a
        required <select> reads as "filled" visually but the form still rejects
        it on submit ("please fix the highlighted fields") with no visible cause.
        """
        await self.start()
        self._touch()
        try:
            await self._select_anywhere(selector, value, nth)
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def click(self, selector: str, nth: "int | None" = None) -> None:
        await self.start()
        self._touch()
        try:
            await self._click_anywhere(selector, nth=nth)
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def type_text(self, text: str, selector: str | None = None) -> None:
        """Type via REAL per-character keystrokes (page.keyboard.type), never a bulk
        value-set. A split-digit code field (N separate maxlength=1 boxes with a JS
        keydown listener that auto-advances focus — the common Microsoft/Google OTP
        pattern) only reacts to genuine keydown events: Page.fill() writes .value
        directly and fires oninput but no keydown, so the page's own JS never moves
        focus and the whole string lands truncated in box one. Passing the FIRST
        box's selector is enough — the page's auto-advance carries the rest of the
        keystrokes to the next boxes on its own, the same way a real user typing does.
        """
        await self.start()
        self._touch()
        try:
            if selector:
                # Triple-click = select-the-line (Chromium's clickCount 3), so typing
                # REPLACES any existing value like fill() did, while still landing as
                # real keystrokes. Same main-frame-then-iframe fallback as click().
                await self._click_anywhere(selector, click_count=3)
            # A small inter-key delay: many auto-advance handlers move focus to the
            # next box asynchronously (setTimeout/rAF, not synchronously inside the
            # keydown handler) — a zero-delay burst can outrun that and land two
            # keystrokes on the same box before focus moves. It also reads as more
            # human to anti-bot heuristics than an instantaneous paste-speed burst.
            # keyboard.type() dispatches at the CDP/OS level to whatever currently
            # has focus, so it reaches an iframe field just as well as a main-frame
            # one once the click above has focused it — no frame-awareness needed here.
            await self._page.keyboard.type(text, delay=25)
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def _scan_frame(self, frame: Any) -> "tuple[list[dict], str]":
        """(elements, visible text) for one frame — same collector as the main
        page, just run in that frame's own JS context via Playwright/CDP (which
        reaches cross-origin iframes a page-level document.querySelectorAll never
        could)."""
        elements: "list[dict]" = []
        with contextlib.suppress(Exception):
            elements = await frame.evaluate(_INTERACTIVE_ELEMENTS_JS)
        text = ""
        with contextlib.suppress(Exception):
            text = await frame.inner_text("body", timeout=2000)
        return elements, (text or "").strip()

    async def snapshot(self, max_chars: int = 4000) -> dict:
        await self.start()
        self._touch()
        try:
            url = self._page.url
            title = await self._page.title()
        except Exception as e:
            await self._retire_if_dead(e)
            raise
        try:
            text = await self._page.inner_text("body", timeout=5000)
        except Exception:
            text = ""
        elements: "list[dict]" = []
        with contextlib.suppress(Exception):
            elements = await self._page.evaluate(_INTERACTIVE_ELEMENTS_JS)
        sections: "list[str]" = [_format_interactive_elements(elements)] if elements else []

        # Same-/cross-origin iframes: a Google Sign-In button or a CAPTCHA checkbox
        # otherwise reads as "not on the page at all" even though it's right there
        # on screen — see the module-level note above _MAX_SCANNED_FRAMES.
        iframe_texts: "list[str]" = []
        for frame in list(self._page.frames)[:_MAX_SCANNED_FRAMES]:
            if frame == self._page.main_frame:
                continue
            try:
                f_url = frame.url
            except Exception:
                continue
            if not f_url or f_url == "about:blank":
                continue
            frame_els, frame_text = await self._scan_frame(frame)
            if not frame_els and not frame_text:
                continue
            header = f"[iframe {_snippet(f_url, 80)}]"
            if frame_els:
                sections.append(header + "\n" + _format_interactive_elements(frame_els))
            if frame_text:
                iframe_texts.append(f"{header}\n{_snippet(frame_text, 500)}")

        if iframe_texts:
            text = (text or "") + "\n\n" + "\n\n".join(iframe_texts)

        return {
            "url": url,
            "title": title,
            "text": _truncate_text(text or "", max_chars),
            "elements": "\n\n".join(sections),
        }

    async def screenshot(self) -> bytes:
        """Full-resolution capture for the agent's browser_screenshot tool — when
        the text/DOM snapshot is ambiguous (or the operator reports seeing
        something the snapshot doesn't explain), there was no way to actually look
        at the page short of asking the operator. Deliberately NOT _capture_frame()
        (that one exists for priming the live pane and uses STREAM's downscaled
        960x540 q45 — fine for a video feed, useless for reading small text
        precisely) — this captures at the full VIEWPORT resolution and a much
        higher JPEG quality instead.
        """
        await self.start()
        self._touch()
        if self._cdp is None:
            raise RuntimeError("No active CDP session for this browser tab.")
        try:
            res = await self._cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 85})
        except Exception as e:
            await self._retire_if_dead(e)
            raise
        return base64.b64decode(res["data"])

    # ── captcha ──────────────────────────────────────────────────────────────
    async def detect_captcha(self) -> dict:
        """What captcha, if any, is on the current page. Read-only and free — no
        API call, no balance spent. Scans the main document first, then iframes:
        a widget hosted inside a same-origin wrapper frame is invisible to a
        main-document-only scan (same blind spot snapshot/click already handle)."""
        await self.start()
        self._touch()
        try:
            info = await self._page.evaluate(_DETECT_CAPTCHA_JS)
            if not info.get("widgets"):
                for frame in list(self._page.frames)[:_MAX_SCANNED_FRAMES]:
                    if frame == self._page.main_frame:
                        continue
                    with contextlib.suppress(Exception):
                        sub = await frame.evaluate(_DETECT_CAPTCHA_JS)
                        if sub.get("widgets"):
                            info["widgets"] = sub["widgets"]
                            info["foundInFrame"] = frame.url
                            break
            return info
        except Exception as e:
            await self._retire_if_dead(e)
            raise

    async def solve_captcha(self, budget: float = 180.0, image_selector: "str | None" = None) -> dict:
        """Solve the captcha on the current page via 2captcha and hand the token to
        the page. Returns a report dict; raises on anything that stops us.

        Two distinct modes. With ``image_selector`` this is an OCR job: the element
        is screenshotted and sent as ImageToTextTask, and the answer comes back as
        TEXT for the agent to type — nothing is injected, because a distorted-text
        captcha has no response field to inject into. Without it, this is a token
        captcha (reCAPTCHA / hCaptcha / Turnstile): 2captcha solves it on their
        side and we inject the resulting token, never touching the widget itself.
        """
        import captcha_solver as _cs

        await self.start()
        self._touch()

        if image_selector:
            loc = self._page.locator(image_selector)
            try:
                shot = await loc.screenshot(timeout=10000)
            except Exception as e:
                await self._retire_if_dead(e)
                raise
            solution = await _cs.solve(
                _cs.build_task("image", self._page.url, "", {"body": base64.b64encode(shot).decode("ascii")}),
                budget=budget,
            )
            return {
                "mode": "image",
                "text": _cs.token_of(solution),
                "cost": solution.get("_cost"),
                "seconds": solution.get("_seconds"),
                "injected": False,
            }

        info = await self.detect_captcha()
        if info.get("challengePage") and not info.get("widgets"):
            raise RuntimeError(
                "This is a full-page Cloudflare challenge ('Verify you are human'), not a "
                "captcha widget in a form. It cannot be solved this way: it needs "
                "action/cData/chlPageData scraped from the page's turnstile.render call, and "
                "the token is bound to the solving IP — a proxyless token is rejected when "
                "replayed from here. Ask the operator to pass it once by hand in the pane "
                "(the session then persists in the browser profile)."
            )
        widgets = info.get("widgets") or []
        if not widgets:
            raise RuntimeError(
                f"No captcha widget found on {info.get('url')}. Either the page has none, or "
                "it hasn't rendered yet — wait a moment and retry, or call browser_screenshot "
                "to look at the page."
            )

        w = widgets[0]
        kind, sitekey = w.get("kind"), w.get("sitekey")
        extra: dict = {}
        if kind == "recaptcha_v2" and (w.get("size") or "").lower() == "invisible":
            extra["isInvisible"] = True
        if kind == "recaptcha_v3" and w.get("action"):
            extra["pageAction"] = w["action"]

        _log.info("solving captcha kind=%s sitekey=%s… on %s", kind, str(sitekey)[:12], info.get("url"))
        solution = await _cs.solve(
            _cs.build_task(kind, info.get("url") or self._page.url, sitekey, extra), budget=budget
        )
        token = _cs.token_of(solution)

        try:
            report = await self._page.evaluate(_INJECT_TOKEN_JS, [token, kind, w.get("callback")])
        except Exception as e:
            await self._retire_if_dead(e)
            raise

        return {
            "mode": "token",
            "kind": kind,
            "sitekey": sitekey,
            "url": info.get("url"),
            "cost": solution.get("_cost"),
            "seconds": solution.get("_seconds"),
            "injected": bool(report.get("fields")),
            "fields": report.get("fields") or [],
            "callbacks": report.get("callbacks") or [],
            "notes": report.get("notes") or [],
            "extra_widgets": len(widgets) - 1,
        }

    def status(self) -> dict:
        """Session health for the agent's browser_status tool — lets it tell "the
        connection died" apart from "my selector is wrong" instead of guessing from
        repeated identical failures."""
        return {
            "backend": self.backend,
            "profile": self._profile or None,
            "started": self._started,
            "closed": self._closed,
            "alive": self._is_alive(),
            "subscribers": len(self._subs),
            "idle_seconds": round(time.monotonic() - self._last_activity, 1),
            "url": (self._page.url if self._page is not None else None),
        }

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
    The dead session is explicitly closed before being replaced: discarding the
    Python object alone does NOT stop its local Playwright driver subprocess or
    its CDP connection, so every rebuild without this leaked one more orphaned
    driver process — the actual cause behind a storm of "Connection closed while
    reading from the driver" errors after a service restart.
    """
    stale: "BrowserSession | None" = None
    async with _REGISTRY_LOCK:
        sess = _SESSIONS.get(key)
        if sess is not None and not sess._is_alive():
            stale, sess = sess, None
        if sess is None:
            sess = BrowserSession(key)
            _SESSIONS[key] = sess
    if stale is not None:
        _log.info("browser session for %s was stale, closing before rebuild", key)
        with contextlib.suppress(Exception):
            await stale.close()
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
