"""2captcha bridge — the network half of ``browser_solve_captcha``.

Deliberately knows NOTHING about Playwright or the browser pane: it takes a task
description, talks to the 2captcha API, and returns a token. The page-side half
(detecting the widget, injecting the token, firing the site's callback) lives in
``browser_pane.BrowserSession.solve_captcha``.

Two things worth knowing before touching this:

* **Token captchas are not clicked.** For reCAPTCHA v2 / hCaptcha / Turnstile the
  solver never touches the widget — 2captcha solves it on their side and returns a
  response token, which we write into the page's hidden response field and hand to
  the site's callback. The "pick every fire hydrant" image grid is therefore
  irrelevant: it is reCAPTCHA v2, the same task type as the plain checkbox, and we
  never see the images at all.
* **A Cloudflare full-page interstitial is a different animal** from a Turnstile
  widget embedded in a form. It needs ``action``/``data``/``pagedata`` scraped out
  of a ``turnstile.render`` call, AND the token is bound to the solver's IP — so a
  proxyless solve is rejected when replayed from our address. We detect that case
  and refuse with an explanation rather than burning balance on a token that cannot
  work. See the memory note ``twocaptcha-key-and-turnstile-limits``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

try:  # optional — the safe is present in this deployment, not necessarily in a fork
    import secretstore as _secretstore
except Exception:  # pragma: no cover - import guard
    _secretstore = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

API_BASE = "https://api.2captcha.com"
API_KEY_SECRET = "twocaptcha_api_key"
API_KEY_ENV = "TWOCAPTCHA_API_KEY"

_HTTP_TIMEOUT = 30          # per API call
_DEFAULT_BUDGET = 180.0     # seconds to wait for a solve before giving up
_POLL_EVERY = 5.0           # 2captcha asks for >=5s between getTaskResult polls
_FIRST_POLL_AFTER = 8.0     # nothing is ever ready sooner; don't waste a poll

# Widget kind → 2captcha task type. Proxyless everywhere: these tokens are not
# IP-bound the way a Cloudflare challenge-page token is.
TASK_TYPES = {
    "recaptcha_v2": "RecaptchaV2TaskProxyless",
    "recaptcha_v3": "RecaptchaV3TaskProxyless",
    "hcaptcha": "HCaptchaTaskProxyless",
    "turnstile": "TurnstileTaskProxyless",
    "image": "ImageToTextTask",
}


class CaptchaError(RuntimeError):
    """Anything that stops us returning a token, with an operator-readable reason."""


def api_key() -> "str | None":
    """The 2captcha key: ``TWOCAPTCHA_API_KEY`` env first, then the encrypted safe.

    Env-first keeps a fork/OSS install working with nothing but ``.env`` (see
    ``.env.example``), while this deployment keeps the real value in the safe only.
    """
    env = (os.environ.get(API_KEY_ENV) or "").strip()
    if env:
        return env
    if _secretstore is not None:
        with contextlib.suppress(Exception):
            key = (_secretstore.get(API_KEY_SECRET) or "").strip()
            if key:
                return key
    return None


def configured() -> bool:
    return api_key() is not None


async def _post(path: str, payload: dict) -> dict:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(API_BASE + path, json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise CaptchaError(f"2captcha {path} → HTTP {resp.status}: {body[:200]}")
            import json as _json
            try:
                return _json.loads(body)
            except Exception:
                raise CaptchaError(f"2captcha {path} returned non-JSON: {body[:200]}")


def _require_key() -> str:
    key = api_key()
    if not key:
        raise CaptchaError(
            "No 2captcha API key. Set TWOCAPTCHA_API_KEY in .env, or store it in the "
            f"encrypted safe as '{API_KEY_SECRET}' (secret set {API_KEY_SECRET} <key>)."
        )
    return key


async def balance() -> float:
    data = await _post("/getBalance", {"clientKey": _require_key()})
    if data.get("errorId"):
        raise CaptchaError(f"2captcha getBalance: {data.get('errorDescription') or data}")
    return float(data.get("balance") or 0.0)


async def solve(task: dict, budget: float = _DEFAULT_BUDGET) -> dict:
    """Submit ``task`` and poll until solved. Returns 2captcha's ``solution`` dict
    plus ``_cost``/``_seconds``. Raises CaptchaError on refusal, timeout or an
    unsolvable captcha — never returns a partial/placeholder token silently.
    """
    key = _require_key()
    t0 = time.monotonic()
    created = await _post("/createTask", {"clientKey": key, "task": task})
    if created.get("errorId"):
        raise CaptchaError(
            f"2captcha rejected the task ({created.get('errorCode')}): "
            f"{created.get('errorDescription') or created}"
        )
    task_id = created.get("taskId")
    if not task_id:
        raise CaptchaError(f"2captcha createTask returned no taskId: {created}")
    _log.info("2captcha task %s created (type=%s)", task_id, task.get("type"))

    await asyncio.sleep(min(_FIRST_POLL_AFTER, budget))
    while True:
        res = await _post("/getTaskResult", {"clientKey": key, "taskId": task_id})
        if res.get("errorId"):
            raise CaptchaError(
                f"2captcha could not solve it ({res.get('errorCode')}): "
                f"{res.get('errorDescription') or res}"
            )
        if res.get("status") == "ready":
            solution = dict(res.get("solution") or {})
            solution["_cost"] = res.get("cost")
            solution["_seconds"] = round(time.monotonic() - t0, 1)
            _log.info("2captcha task %s solved in %ss (cost=%s)",
                      task_id, solution["_seconds"], solution["_cost"])
            return solution
        if time.monotonic() - t0 >= budget:
            raise CaptchaError(
                f"2captcha did not solve it within {budget:.0f}s (task {task_id} still "
                "processing). Their workers are backed up, or the captcha is one they "
                "cannot do; try again or solve it by hand in the pane."
            )
        await asyncio.sleep(_POLL_EVERY)


def token_of(solution: dict) -> str:
    """The response token, whatever key this captcha type happens to use."""
    for field in ("gRecaptchaResponse", "token", "text"):
        val = solution.get(field)
        if val:
            return str(val)
    raise CaptchaError(f"2captcha solution had no token field: {list(solution.keys())}")


def build_task(kind: str, page_url: str, sitekey: str, extra: "dict | None" = None) -> dict:
    """Task payload for a detected widget. ``extra`` carries per-kind bits
    (reCAPTCHA v3 action/minScore, an invisible-recaptcha flag, image body)."""
    task_type = TASK_TYPES.get(kind)
    if not task_type:
        raise CaptchaError(f"Unsupported captcha kind {kind!r}; known: {sorted(TASK_TYPES)}")
    task: dict[str, Any] = {"type": task_type}
    if kind == "image":
        task["body"] = (extra or {}).get("body") or ""
        if not task["body"]:
            raise CaptchaError("image captcha task needs a base64 body")
    else:
        task["websiteURL"] = page_url
        task["websiteKey"] = sitekey
    for k, v in (extra or {}).items():
        if k != "body" and v is not None:
            task[k] = v
    return task
