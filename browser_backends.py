"""spec-066 — Pluggable browser backends + anti-detect (CloakBrowser).

The single swap point under the spec-065 browser pane. ``BrowserSession.start()``
calls :func:`acquire` to obtain a Playwright browser handle; everything downstream
(CDP screencast, input dispatch, agent MCP tools) is backend-agnostic.

Three tiers, all optional and config-driven (``data/modules.json`` → the ``browser``
module's ``config`` block):

* **A builtin** — vanilla headless Chromium via Playwright. DEFAULT, zero extra deps,
  works out of the box on a fresh OSS install. No stealth.
* **B cloakbrowser** — the ``cloakbrowser`` PyPI package (MIT wrapper, free Chromium
  binary). Lazy-imported; absent → the tier reports unavailable, builtin still works.
  ``launch_async()`` returns a standard Playwright ``Browser``, so it is a drop-in.
* **C external-cdp** — ``connect_over_cdp(url)`` to ANY CDP browser: a static endpoint,
  or a Cloak Manager persistent (logged-in) profile resolved through the Manager API.

OSS invariants: nothing proprietary is bundled, no operator infra is hardcoded
(``manager_url`` comes from config/env, the token from the encrypted safe), and a
missing dependency or config degrades gracefully — it never crashes the cockpit.
"""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import modules as _modules

_log = logging.getLogger(__name__)

try:  # the encrypted safe — optional at import time (tests may stub it)
    import secretstore as _secretstore
except Exception:  # pragma: no cover - import guard
    _secretstore = None  # type: ignore[assignment]


# A realistic User-Agent: the Cloak Manager sits behind a WAF that 403s any
# non-browser client missing a real UA (documented in the cloakbrowser vault note).
_MANAGER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_MANAGER_TIMEOUT = 20.0
# Fixed safe key for the Cloak Manager auth token (entered in the UI, never in
# modules.json / tracked code). Same encrypted store as spec-054 credentials.
MANAGER_TOKEN_KEY = "cloak-manager-token"

# Playwright's own connect_over_cdp default is 180s. That is not a wait, it is a
# hang: three minutes per attempt, every session retrying, and the pane says
# nothing. A connect to a healthy profile takes ~1s (measured), so anything past
# a few seconds means something is wrong and the caller deserves to hear it.
CDP_CONNECT_TIMEOUT = float(os.environ.get("CLOAK_CDP_CONNECT_TIMEOUT", "30"))
# Per-target liveness probe before connecting (see _prune_dead_targets).
CDP_PROBE_TIMEOUT = float(os.environ.get("CLOAK_CDP_PROBE_TIMEOUT", "4"))
CDP_PRUNE_DEAD_TARGETS = (os.environ.get("CLOAK_CDP_PRUNE", "1") or "1").lower() not in ("0", "false", "no")

# Stealth knobs passed straight through to cloakbrowser.launch_async (Tier B, Phase C).
_CLOAK_KNOBS = ("proxy", "geoip", "humanize", "timezone", "locale")

VALID_BACKENDS = ("builtin", "cloakbrowser", "external-cdp")
VALID_AGENT_ACTIONS = ("read", "full")


class BackendError(RuntimeError):
    """A backend was selected but could not be acquired (bad config / missing dep)."""


@dataclass
class Acquired:
    """The outcome of :func:`acquire` — a live Playwright browser handle.

    ``owns_browser`` is False for external/connected backends: tearing the session
    down must only *disconnect*, never kill the operator's / Manager's browser.
    """

    pw: Any                       # the async_playwright instance (None for cloakbrowser)
    browser: Any
    context: Any
    page: Any
    owns_browser: bool
    backend: str
    label: str = ""
    # True when THIS session created the page inside a browser it does not own (a shared
    # Manager profile). Such a page must be closed on teardown — nobody else will — while
    # the browser and its cookies stay untouched.
    owns_page: bool = False
    # True when the underlying browser/context is shared with other projects, so page
    # adoption must be scoped to our own tab (see browser_pane).
    shared_context: bool = False
    # Cloak Manager profile id backing this session, "" for every other backend. The
    # session must hand this back on teardown (release_profile) or the profile stays
    # running forever — see the profile-lifecycle block below.
    profile: str = ""


# ───────────────────────────── config resolution ─────────────────────────────


def _browser_config() -> dict:
    """The ``browser`` module's persisted ``config`` block (``{}`` when unset)."""
    try:
        cfg = _modules.get_config("browser")
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def resolve(cwd: str) -> dict:
    """Resolve the effective backend spec for a project ``cwd``.

    Precedence:

    1. **Per-project Manager profile** — if ``per_project_profile[cwd]`` is set, the
       project browses *as* that profile over ``external-cdp``, regardless of the
       global backend. This is how one project (e.g. a grants workspace) uses a
       logged-in Cloak Manager profile while every other project stays on the default
       local browser — the operator watches the SAME profile the Manager shows.
    2. **Global backend** — ``builtin`` / ``cloakbrowser`` / ``external-cdp`` from
       ``modules.json`` → ``browser.config``; an unknown/missing value → ``builtin``.

    Graceful fallback: ``external-cdp`` with neither a static ``cdp_url`` nor a profile
    to connect to for *this* project degrades to ``builtin`` so the pane keeps working
    instead of raising.
    """
    cfg = _browser_config()
    backend = cfg.get("backend") or "builtin"
    if backend not in VALID_BACKENDS:
        backend = "builtin"
    agent = cfg.get("agent_actions") if cfg.get("agent_actions") in VALID_AGENT_ACTIONS else "read"

    # (1) Manager profile — per-project mapping first, then the global default.
    #
    # `default_profile` used to be honoured ONLY when the global backend was already
    # external-cdp, so setting it while on cloakbrowser/builtin was a silent no-op: the UI
    # said "in use" and nothing changed. A configured profile now wins over the backend
    # choice for every project, which is what "use this logged-in profile everywhere" means.
    #
    # An explicit EMPTY mapping is an opt-out: per_project_profile[cwd] = "" pins that one
    # project back to the plain backend even when a default profile exists.
    per = cfg.get("per_project_profile") or {}
    if not isinstance(per, dict):
        per = {}
    if cwd in per:
        profile = str(per.get(cwd) or "")
    else:
        profile = str(cfg.get("default_profile") or "")
    if profile:
        # cdp_url MUST stay empty so _acquire_external resolves the profile via the Manager
        # (a static cdp_url would otherwise take precedence over the profile).
        return {"backend": "external-cdp", "agent_actions": agent, "cdp_url": "",
                "profile": profile, "isolate_page": True}

    out: dict[str, Any] = {"backend": backend, "agent_actions": agent}
    if backend == "external-cdp":
        cdp_url = cfg.get("cdp_url") or os.environ.get("CLOAK_CDP_URL") or ""
        if not cdp_url:
            out["backend"] = "builtin"   # nothing to connect to → don't break the pane
            return out
        out["cdp_url"] = cdp_url
    elif backend == "cloakbrowser":
        for k in _CLOAK_KNOBS:
            if cfg.get(k) not in (None, ""):
                out[k] = cfg[k]
    return out


def agent_actions(cwd: str = "") -> str:
    """Effective agent-action gate ('read' default | 'full'). Read tools are always
    allowed; mutating tools (click/type) are gated by this (spec-066 Phase C)."""
    return resolve(cwd).get("agent_actions", "read")


# ───────────────────────────── tier B: cloakbrowser ──────────────────────────


def _cloak_module():
    try:
        import cloakbrowser  # type: ignore
        return cloakbrowser
    except Exception:
        return None


def cloak_status() -> dict:
    """Availability of the cloakbrowser tier without launching anything.

    ``installed`` = the package imports; ``binary_ready`` = the free Chromium binary
    is downloaded (``binary_info``). Absent bits are reported, not raised.
    """
    cb = _cloak_module()
    if cb is None:
        return {"installed": False, "binary_ready": False, "version": None}
    info: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        bi = cb.binary_info()
        if isinstance(bi, dict):
            info = bi
    binary_ready = bool(
        info.get("installed") or info.get("ready") or info.get("path") or info.get("downloaded")
    )
    return {
        "installed": True,
        "binary_ready": binary_ready,
        "version": getattr(cb, "__version__", None),
    }


async def _acquire_cloak(cfg: dict, viewport: dict) -> Acquired:
    cb = _cloak_module()
    if cb is None:
        raise BackendError(
            "CloakBrowser is not installed. Run: venv/bin/pip install cloakbrowser "
            "&& venv/bin/python -m cloakbrowser install"
        )
    launch_async = getattr(cb, "launch_async", None)
    if launch_async is None:
        raise BackendError("cloakbrowser has no launch_async(); update the package.")
    kwargs: dict[str, Any] = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    for k in _CLOAK_KNOBS:
        if cfg.get(k) not in (None, ""):
            kwargs[k] = cfg[k]
    try:
        browser = await launch_async(**kwargs)
    except Exception as e:
        raise BackendError(f"CloakBrowser failed to launch: {e}") from e
    context = await browser.new_context(viewport=viewport)
    page = await context.new_page()
    return Acquired(pw=None, browser=browser, context=context, page=page,
                    owns_browser=True, backend="cloakbrowser", label="CloakBrowser (stealth)")


# ───────────────────────────── tier C: external CDP ──────────────────────────


async def _prune_dead_targets(cdp_url: str, headers: dict) -> int:
    """Close pages whose renderer no longer answers CDP, BEFORE connecting.

    ``connect_over_cdp`` attaches to EVERY target and waits for each one to
    initialise, so a single wedged page takes the whole connection down with it:
    the browser answers ``Browser.getVersion`` in 60ms while Playwright sits at
    180s and times out. That is not hypothetical — one hung
    ``americastire.com`` tab in the shared ``google`` profile made the browser
    "not work" in every session at once for hours (its renderer: 2.9GB RSS, 203
    minutes of CPU). A protocol trace showed 200 of 210 commands answered and
    the 10 stragglers all on that one sessionId.

    So we do what Playwright cannot: talk raw CDP first, ping each page, and
    close the ones that do not answer within ``CDP_PROBE_TIMEOUT``. Live pages
    are detached untouched — an operator's tab is never closed for being idle,
    only for being dead. Returns how many were closed; any failure here is
    swallowed by the caller, since a probe that cannot run must not block a
    connect that might still work.
    """
    import asyncio
    import json as _json

    import aiohttp

    closed = 0
    timeout = aiohttp.ClientTimeout(total=CDP_PROBE_TIMEOUT * 4, sock_connect=CDP_PROBE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers or None) as sess:
        async with sess.ws_connect(cdp_url, max_msg_size=0) as ws:
            next_id = 0

            async def _call(method: str, params: dict | None = None, session_id: str = "") -> int:
                nonlocal next_id
                next_id += 1
                msg: dict[str, Any] = {"id": next_id, "method": method}
                if params:
                    msg["params"] = params
                if session_id:
                    msg["sessionId"] = session_id
                await ws.send_str(_json.dumps(msg))
                return next_id

            async def _collect(deadline: float, wanted: set) -> dict:
                """Read until every id in ``wanted`` answered or the deadline passes."""
                out: dict[int, dict] = {}
                loop = asyncio.get_running_loop()
                while wanted - set(out) and loop.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=max(0.05, deadline - loop.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.ERROR):
                        break
                    if msg.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    with contextlib.suppress(Exception):
                        data = _json.loads(msg.data)
                        if isinstance(data, dict) and data.get("id") in wanted:
                            out[data["id"]] = data
                return out

            loop = asyncio.get_running_loop()
            list_id = await _call("Target.getTargets")
            got = await _collect(loop.time() + CDP_PROBE_TIMEOUT, {list_id})
            infos = (got.get(list_id, {}).get("result", {}) or {}).get("targetInfos") or []
            pages = [t for t in infos if t.get("type") == "page"]
            if not pages:
                return 0

            # Attach to every page at once, then ping them all in parallel — a serial
            # probe would cost CDP_PROBE_TIMEOUT per dead tab.
            attach_ids = {}
            for t in pages:
                attach_ids[await _call("Target.attachToTarget",
                                       {"targetId": t["targetId"], "flatten": True})] = t
            attached = await _collect(loop.time() + CDP_PROBE_TIMEOUT, set(attach_ids))
            sessions = {}
            for mid, t in attach_ids.items():
                sid = (attached.get(mid, {}).get("result", {}) or {}).get("sessionId")
                if sid:
                    sessions[sid] = t

            ping_ids = {}
            for sid, t in sessions.items():
                ping_ids[await _call("Runtime.evaluate", {"expression": "1", "returnByValue": True},
                                     session_id=sid)] = (sid, t)
            answered = await _collect(loop.time() + CDP_PROBE_TIMEOUT, set(ping_ids))

            for mid, (sid, t) in ping_ids.items():
                if mid in answered:
                    with contextlib.suppress(Exception):
                        await _call("Target.detachFromTarget", {"sessionId": sid})
                    continue
                _log.warning(
                    "closing wedged CDP target %s (%s) — no answer in %.0fs, it would hang connect_over_cdp",
                    t.get("targetId"), (t.get("url") or "")[:120], CDP_PROBE_TIMEOUT,
                )
                with contextlib.suppress(Exception):
                    await _call("Target.closeTarget", {"targetId": t["targetId"]})
                    closed += 1
            if closed:
                # Give Chrome a moment to actually tear the target down, otherwise the
                # connect that follows can still attach to a half-closed page.
                await asyncio.sleep(0.3)
    return closed


async def _acquire_external(cfg: dict, viewport: dict) -> Acquired:
    cdp_url = cfg.get("cdp_url") or ""
    profile = cfg.get("profile") or ""
    label = "External CDP"
    headers: dict[str, str] = {}
    if profile and not cdp_url:
        # Decide ownership BEFORE launching: once it is up we can no longer tell
        # "we started this" from "it was already the operator's".
        await _note_launch_ownership(profile)
        cdp_url = await profile_cdp_url(profile)
        label = f"Cloak Manager · {profile}"
        # The Manager's CDP endpoint is auth-gated (Bearer) and WAF-fronted (rejects a
        # non-browser UA) — connect_over_cdp must carry both, like the REST client.
        headers["User-Agent"] = _MANAGER_UA
        tok = manager_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    if not cdp_url:
        raise BackendError(
            "external-cdp backend selected but no cdp_url or Cloak Manager profile is configured."
        )
    # Health-check the targets we are about to attach to. Never fatal: if the probe
    # itself cannot run, the connect below may still succeed, and it owns the error.
    if CDP_PRUNE_DEAD_TARGETS:
        try:
            closed = await _prune_dead_targets(cdp_url, headers)
            if closed:
                _log.info("pruned %d wedged target(s) before connecting (profile=%s)",
                          closed, profile or "-")
        except Exception as e:
            _log.warning("CDP target health probe failed (profile=%s): %s", profile or "-", e)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(
            cdp_url, headers=headers or None, timeout=CDP_CONNECT_TIMEOUT * 1000)
    except Exception as e:
        with contextlib.suppress(Exception):
            await pw.stop()
        # A dead trail here is exactly what turned a 74-renderer-process profile
        # leak into a multi-minute mystery: the Manager's OWN container logs had
        # "failed to reach Chrome CDP for <profile>", but nothing on our side ever
        # recorded that we saw the SAME failure, at what time, for which profile.
        _log.warning("external-cdp connect_over_cdp failed (profile=%s): %s", profile or "-", e)
        hint = ""
        if profile and "Timeout" in str(e):
            # The transport is almost never the problem here; a wedged page that
            # survived the probe is. Say so, with the one action that fixes it.
            hint = (f" The profile answered the socket but not the protocol — a page is "
                    f"probably wedged. Restart profile {profile} in Cloak Manager "
                    f"(logins survive, they live in the profile's on-disk user-data-dir).")
        raise BackendError(f"connect_over_cdp({cdp_url!r}) failed: {e}{hint}") from e
    # Reuse the connected browser's existing context (that is where the profile's cookies
    # and logins live — sharing it is the whole point).
    context = browser.contexts[0] if browser.contexts else await browser.new_context(viewport=viewport)

    # The PAGE, however, is per project when the profile may be shared. Reusing
    # context.pages[0] meant two projects on one profile drove the SAME tab: navigating in
    # one moved the other's pane. One tab each keeps the shared identity (context) without
    # the collision.
    isolate = bool(cfg.get("isolate_page"))
    owns_page = False
    if isolate or not context.pages:
        page = await context.new_page()
        owns_page = True
    else:
        page = context.pages[0]
    # NO set_viewport_size here, deliberately. It sets an emulation override, and on a
    # connect_over_cdp profile the override does NOT win the layout — the profile keeps
    # laying the page out at its own window size (1920x947 on the Cloak profile) — but it
    # DOES clamp what Chromium captures. Result: screenshots and the operator's screencast
    # showed the top-left 1280x720 CROP of a 1920-wide page, with the rest simply invisible
    # and every click past the crop mapped against a picture that never contained it
    # (verified 2026-08-30: raw Page.captureScreenshot 1280x720 vs cssLayoutViewport
    # 1920x947; clearing the override restored the full 1920x947 capture). An external
    # browser's window belongs to whoever owns it — measure it (BrowserSession._sync_viewport),
    # never impose ours.
    return Acquired(pw=pw, browser=browser, context=context, page=page,
                    owns_browser=False, backend="external-cdp", label=label,
                    owns_page=owns_page, shared_context=isolate, profile=profile)


# ───────────────────────────── tier A: builtin ───────────────────────────────


async def _acquire_builtin(viewport: dict) -> Acquired:
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # pragma: no cover - import guard
        raise BackendError(
            "Playwright is not installed. Run: venv/bin/pip install playwright "
            "&& venv/bin/playwright install chromium"
        ) from e
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(viewport=viewport)
        page = await context.new_page()
    except Exception as e:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise BackendError(f"Chromium failed to launch: {e}") from e
    return Acquired(pw=pw, browser=browser, context=context, page=page,
                    owns_browser=True, backend="builtin", label="Built-in Chromium")


# ───────────────────────────── public entry point ────────────────────────────


async def acquire(cwd: str, viewport: dict) -> Acquired:
    """Acquire a Playwright browser handle per the resolved backend for ``cwd``.

    Raises :class:`BackendError` on a misconfigured/unavailable backend; the caller
    surfaces that to the pane and the builtin default keeps working. Every attempt
    (success or failure) is logged here — the single funnel point that has both
    `cwd` and the resolved `backend`, so `journalctl -u <service>` gives a durable
    timeline of "session for project X started on backend Y at time T" to correlate
    against a later failure, instead of the only trail being on the OTHER side of a
    connection (e.g. the Cloak Manager's own container logs).
    """
    cfg = resolve(cwd)
    backend = cfg["backend"]
    try:
        if backend == "cloakbrowser":
            acq = await _acquire_cloak(cfg, viewport)
        elif backend == "external-cdp":
            acq = await _acquire_external(cfg, viewport)
        else:
            acq = await _acquire_builtin(viewport)
    except BackendError as e:
        _log.warning("browser backend acquire FAILED (cwd=%s backend=%s): %s", cwd, backend, e)
        # A failed acquire may still have LAUNCHED the profile (profile_cdp_url does,
        # before connecting). Without this the profile is orphaned for good: nobody is
        # attached, so release_profile is never called, so the idle-stop is never
        # scheduled, and it renders through swiftshader until someone notices the fans.
        # That is how the profile behind this fix stayed up for 14 hours.
        with contextlib.suppress(Exception):
            await schedule_stop_if_unused(str(cfg.get("profile") or ""))
        raise
    # Refcount the Manager profile only after a SUCCESSFUL acquire — registering a
    # failed attempt would pin a profile open with a session that never existed.
    await attach_profile(acq.profile, cwd)
    _log.info("browser backend acquired (cwd=%s backend=%s label=%s)", cwd, backend, acq.label or acq.backend)
    return acq


# ───────────────────────────── Cloak Manager client ──────────────────────────


def manager_base() -> "str | None":
    """Cloak Manager base URL from config or ``CLOAK_MANAGER_URL`` env (no trailing /)."""
    cfg = _browser_config()
    url = (cfg.get("manager_url") or os.environ.get("CLOAK_MANAGER_URL") or "").strip()
    return url.rstrip("/") or None


def manager_cdp_base() -> "str | None":
    """Base host for ``connect_over_cdp``. The Manager's REST API can sit behind a
    CDN/WAF (fine for JSON), but raw CDP websockets need a directly-reachable host —
    ``CLOAK_MANAGER_CDP_BASE`` overrides it (e.g. an internal LAN/Docker address).
    Falls back to the REST base when unset."""
    url = (os.environ.get("CLOAK_MANAGER_CDP_BASE") or "").strip()
    return url.rstrip("/") or manager_base()


def manager_token() -> "str | None":
    """The Manager auth token from the encrypted safe (never from modules.json)."""
    if _secretstore is None:
        return None
    with contextlib.suppress(Exception):
        return _secretstore.get(MANAGER_TOKEN_KEY)
    return None


def manager_configured() -> bool:
    return manager_base() is not None


async def _manager_request(method: str, path: str) -> Any:
    base = manager_base()
    if not base:
        raise BackendError("Cloak Manager URL is not configured.")
    import aiohttp
    headers = {"User-Agent": _MANAGER_UA, "Accept": "application/json"}
    tok = manager_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    timeout = aiohttp.ClientTimeout(total=_MANAGER_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.request(method, base + path, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise BackendError(f"Cloak Manager {method} {path} → {resp.status}: {body[:200]}")
            import json as _json
            try:
                return _json.loads(body) if body else {}
            except Exception:
                return {"raw": body}


async def list_profiles() -> list[dict]:
    """List Cloak Manager profiles (id, name, status). Empty list if unconfigured."""
    if not manager_configured():
        return []
    data = await _manager_request("GET", "/api/profiles")
    items = data.get("profiles") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for p in items:
        if not isinstance(p, dict):
            continue
        out.append({
            "id": str(p.get("id") or p.get("profile_id") or p.get("name") or ""),
            "name": str(p.get("name") or p.get("id") or "profile"),
            "status": str(p.get("status") or ("running" if p.get("running") else "stopped")),
        })
    return [p for p in out if p["id"]]


async def launch_profile(profile_id: str) -> dict:
    return await _manager_request("POST", f"/api/profiles/{profile_id}/launch")


async def stop_profile(profile_id: str) -> dict:
    return await _manager_request("POST", f"/api/profiles/{profile_id}/stop")


async def profile_cdp_url(profile_id: str) -> str:
    """Ensure a profile is launched and return its absolute CDP URL for connect_over_cdp.

    The Manager returns a *relative* path (``/api/profiles/<id>/cdp``); we prefix it with
    the CDP host (``manager_cdp_base()``) so Playwright gets an absolute endpoint."""
    with contextlib.suppress(BackendError):
        await launch_profile(profile_id)
    data = await _manager_request("GET", f"/api/profiles/{profile_id}/cdp")
    url = ""
    if isinstance(data, dict):
        url = data.get("cdp_url") or data.get("url") or data.get("webSocketDebuggerUrl") or ""
    if not url:
        raise BackendError(f"Cloak Manager returned no CDP URL for profile {profile_id!r}.")
    url = str(url)
    if url.startswith("/"):
        base = manager_cdp_base()
        if not base:
            raise BackendError("Cloak Manager CDP base is not configured (set CLOAK_MANAGER_CDP_BASE or manager_url).")
        url = base + url
    return url


# ─────────────────────── Cloak Manager profile lifecycle ─────────────────────
# We LAUNCH a Manager profile on demand (profile_cdp_url below) but used to never
# stop one: teardown deliberately only disconnects, because killing a shared
# logged-in browser out from under the operator would be worse than leaving it up.
# The cost of "leave it up" turned out to be real, though — two profiles idled ~10
# hours after their last use, 55 Chrome processes between them, and because the VM
# has no GPU passthrough every one of them renders through swiftshader (software
# GL) on the CPU. That pinned the host at 445% CPU / 67-73°C, load 9.8 on 6 vCPU,
# swap full, fans audible across the room — for browsers nobody was looking at.
#
# So: whoever launched it, stops it. We track which profiles WE started (a profile
# already running when we arrived belongs to the operator — never ours to kill),
# refcount the sessions attached to each, and stop one only after the LAST session
# detaches and a grace period passes with nobody coming back. Stopping is safe for
# logins: cookies live in the profile's on-disk user-data-dir, not in the process.
#
# Ownership must SURVIVE a cockpit restart. It is process state, but the thing it
# describes — a running Chrome — outlives the process: restart the service and every
# profile we had launched is still up, so on reconnect it looks "already running",
# i.e. the operator's, and would never be stopped again. That is not hypothetical —
# it is exactly what the first restart after this feature shipped did to profile
# d33e103d. So the set is persisted and reloaded; an entry is dropped as soon as the
# profile is actually stopped, so a stale file cannot make us adopt a profile the
# operator later started by hand.
_PROFILE_USERS: "dict[str, set[str]]" = {}       # profile id → session keys attached now
_PROFILE_OURS: "set[str]" = set()                # profiles WE launched (persisted below)
_PROFILE_STOPPERS: "dict[str, Any]" = {}         # profile id → pending stop task
_PROFILE_LOCK: "Any" = None                      # created lazily, needs a running loop
_OWNED_PATH = Path(__file__).resolve().parent / "data" / "cloak-profiles-owned.json"
_OWNED_LOADED = False


def _load_owned() -> None:
    """Reload the persisted ownership set once per process."""
    global _OWNED_LOADED
    if _OWNED_LOADED:
        return
    _OWNED_LOADED = True
    with contextlib.suppress(Exception):
        import json as _json
        data = _json.loads(_OWNED_PATH.read_text())
        if isinstance(data, list):
            _PROFILE_OURS.update(str(p) for p in data if p)
            if _PROFILE_OURS:
                _log.info("reloaded %d cloak profile(s) we own across restart: %s",
                          len(_PROFILE_OURS), ", ".join(sorted(_PROFILE_OURS)))


def _save_owned() -> None:
    with contextlib.suppress(Exception):
        import json as _json
        _OWNED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OWNED_PATH.write_text(_json.dumps(sorted(_PROFILE_OURS)))

# Seconds a profile we launched may sit with zero attached sessions before being
# stopped. 0 disables the whole mechanism (back to the old leave-it-running
# behaviour). Deliberately much longer than browser_pane's 120s session idle: a
# cold profile start costs seconds, and an operator reading a page in the Manager's
# own noVNC viewer looks exactly like "idle" from here.
PROFILE_IDLE_STOP = float(os.environ.get("CLOAK_PROFILE_IDLE_STOP", "900"))


def _profile_lock() -> Any:
    global _PROFILE_LOCK
    if _PROFILE_LOCK is None:
        import asyncio
        _PROFILE_LOCK = asyncio.Lock()
    return _PROFILE_LOCK


async def attach_profile(profile_id: str, session_key: str) -> None:
    """Register that ``session_key`` is now using ``profile_id`` and cancel any
    pending stop — a project reconnecting within the grace window must not have the
    profile yanked out from under it a moment later."""
    if not profile_id:
        return
    async with _profile_lock():
        task = _PROFILE_STOPPERS.pop(profile_id, None)
        if task is not None:
            task.cancel()
            _log.info("cloak profile %s reused before idle-stop fired, cancelling stop", profile_id)
        _PROFILE_USERS.setdefault(profile_id, set()).add(session_key)


async def release_profile(profile_id: str, session_key: str) -> None:
    """``session_key`` is done with ``profile_id``. When it was the last user AND we
    launched this profile, schedule the stop after the idle grace period."""
    if not profile_id:
        return
    async with _profile_lock():
        users = _PROFILE_USERS.get(profile_id)
        if users is not None:
            users.discard(session_key)
            if not users:
                _PROFILE_USERS.pop(profile_id, None)
        _schedule_stop_locked(profile_id)


def _schedule_stop_locked(profile_id: str) -> None:
    """Arm the idle-stop for a profile nobody is using. Caller holds ``_profile_lock``."""
    import asyncio
    if _PROFILE_USERS.get(profile_id):
        return  # another project is still on this profile
    if profile_id not in _PROFILE_OURS or PROFILE_IDLE_STOP <= 0:
        return  # the operator's own running profile, or the feature is off
    if profile_id in _PROFILE_STOPPERS:
        return
    _PROFILE_STOPPERS[profile_id] = asyncio.create_task(_stop_when_idle(profile_id))


async def schedule_stop_if_unused(profile_id: str) -> None:
    """Arm the idle-stop for a profile we launched that never became a session.

    ``attach_profile`` deliberately runs only after a SUCCESSFUL acquire, so a launch
    followed by a failed connect leaves a running profile with no user and no pending
    stop — forever. This is the missing counterpart: same rules as ``release_profile``
    (ours only, never the operator's), just without a session to release."""
    if not profile_id:
        return
    async with _profile_lock():
        _schedule_stop_locked(profile_id)


async def _stop_when_idle(profile_id: str) -> None:
    import asyncio
    try:
        await asyncio.sleep(PROFILE_IDLE_STOP)
    except asyncio.CancelledError:
        return
    async with _profile_lock():
        _PROFILE_STOPPERS.pop(profile_id, None)
        if _PROFILE_USERS.get(profile_id):
            return  # someone attached while we slept
        _PROFILE_OURS.discard(profile_id)
        _save_owned()
    try:
        await stop_profile(profile_id)
        _log.info("stopped idle cloak profile %s after %.0fs with no sessions",
                  profile_id, PROFILE_IDLE_STOP)
    except Exception as e:
        _log.warning("failed to stop idle cloak profile %s: %s", profile_id, e)


async def _note_launch_ownership(profile_id: str) -> None:
    """Mark the profile as ours to stop IFF it was not already running. Called before
    we launch it; a profile the operator started stays theirs for its whole life."""
    _load_owned()
    if profile_id in _PROFILE_OURS:
        return  # already ours from before a restart — do not re-evaluate as "running"
    running = False
    with contextlib.suppress(Exception):
        for p in await list_profiles():
            if p["id"] == profile_id:
                running = p["status"].lower() in ("running", "started", "active")
                break
    if not running:
        async with _profile_lock():
            _PROFILE_OURS.add(profile_id)
            _save_owned()


def profile_lifecycle_status() -> dict:
    """Snapshot for diagnostics: which profiles we hold open and who is on them."""
    return {
        "idle_stop_seconds": PROFILE_IDLE_STOP,
        "launched_by_us": sorted(_PROFILE_OURS),
        "attached": {pid: sorted(keys) for pid, keys in _PROFILE_USERS.items()},
        "stop_pending": sorted(_PROFILE_STOPPERS),
    }


# ───────────────────────────── availability summary ──────────────────────────


def availability() -> dict:
    """Snapshot for the Extensions → Browser UI: which tiers are usable + selection."""
    cfg = _browser_config()
    return {
        "current": resolve(""),
        "tiers": {
            "builtin": {"available": True},
            "cloakbrowser": cloak_status(),
            "external-cdp": {"available": True},
        },
        "manager": {
            "configured": manager_configured(),
            "url": manager_base(),
            "token_set": manager_token() is not None,
        },
        "config": {
            "backend": cfg.get("backend") or "builtin",
            "cdp_url": cfg.get("cdp_url") or "",
            "manager_url": cfg.get("manager_url") or "",
            "default_profile": cfg.get("default_profile") or "",
            # Needed by the UI to render which project is pinned to which profile (and
            # which is explicitly opted out — an empty string value).
            "per_project_profile": cfg.get("per_project_profile") or {},
            "agent_actions": cfg.get("agent_actions") or "read",
        },
    }
