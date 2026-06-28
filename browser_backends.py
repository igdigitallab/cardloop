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
import os
from dataclasses import dataclass, field
from typing import Any

import modules as _modules

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

    Reads ``modules.json`` → ``browser.config`` and normalises it. An unknown or
    missing backend falls back to ``builtin`` so the pane always works.
    """
    cfg = _browser_config()
    backend = cfg.get("backend") or "builtin"
    if backend not in VALID_BACKENDS:
        backend = "builtin"
    out: dict[str, Any] = {
        "backend": backend,
        "agent_actions": cfg.get("agent_actions") if cfg.get("agent_actions") in VALID_AGENT_ACTIONS else "read",
    }
    if backend == "external-cdp":
        out["cdp_url"] = cfg.get("cdp_url") or os.environ.get("CLOAK_CDP_URL") or ""
        per = cfg.get("per_project_profile") or {}
        out["profile"] = (per.get(cwd) if isinstance(per, dict) else None) or cfg.get("default_profile") or ""
    if backend == "cloakbrowser":
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


async def _acquire_external(cfg: dict, viewport: dict) -> Acquired:
    cdp_url = cfg.get("cdp_url") or ""
    profile = cfg.get("profile") or ""
    label = "External CDP"
    if profile and not cdp_url:
        cdp_url = await profile_cdp_url(profile)
        label = f"Cloak Manager · {profile}"
    if not cdp_url:
        raise BackendError(
            "external-cdp backend selected but no cdp_url or Cloak Manager profile is configured."
        )
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise BackendError(f"connect_over_cdp({cdp_url!r}) failed: {e}") from e
    # Reuse the connected browser's existing context/page when present (a persistent
    # logged-in profile already has them); only create when the browser is bare.
    context = browser.contexts[0] if browser.contexts else await browser.new_context(viewport=viewport)
    page = context.pages[0] if context.pages else await context.new_page()
    with contextlib.suppress(Exception):
        await page.set_viewport_size(viewport)
    return Acquired(pw=pw, browser=browser, context=context, page=page,
                    owns_browser=False, backend="external-cdp", label=label)


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
    surfaces that to the pane and the builtin default keeps working.
    """
    cfg = resolve(cwd)
    backend = cfg["backend"]
    if backend == "cloakbrowser":
        return await _acquire_cloak(cfg, viewport)
    if backend == "external-cdp":
        return await _acquire_external(cfg, viewport)
    return await _acquire_builtin(viewport)


# ───────────────────────────── Cloak Manager client ──────────────────────────


def manager_base() -> "str | None":
    """Cloak Manager base URL from config or ``CLOAK_MANAGER_URL`` env (no trailing /)."""
    cfg = _browser_config()
    url = (cfg.get("manager_url") or os.environ.get("CLOAK_MANAGER_URL") or "").strip()
    return url.rstrip("/") or None


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
    """Ensure a profile is launched and return its live CDP URL for connect_over_cdp."""
    with contextlib.suppress(BackendError):
        await launch_profile(profile_id)
    data = await _manager_request("GET", f"/api/profiles/{profile_id}/cdp")
    if isinstance(data, dict):
        url = data.get("cdp_url") or data.get("url") or data.get("webSocketDebuggerUrl") or ""
        if url:
            return str(url)
    raise BackendError(f"Cloak Manager returned no CDP URL for profile {profile_id!r}.")


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
            "agent_actions": cfg.get("agent_actions") or "read",
        },
    }
