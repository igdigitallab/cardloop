"""spec-065 Phase C — agent MCP tools that drive the live browser pane.

These tools act on the SAME browser session the operator watches in the cockpit
pane (keyed by the project cwd in browser_pane._SESSIONS), so the agent's actions
are visible live. The server is built per-run with `cwd` bound, and is only wired
into the engine when the `browser` module is enabled (see engine.py).
"""
from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable

import browser_pane as _browser_pane

# Shared across every selector-taking tool below. The prior wording ("CSS selector")
# undersold this and made agents needlessly conservative — the FULL Playwright
# selector engine is supported: :has-text(...), :text-is(...), :visible, and
# combinators like 'tr:has-text("ASTR") input[type="checkbox"]' all work, and several
# of them already reach into iframes on their own (on top of that, browser_click/
# browser_select/browser_upload also fall back to searching other frames automatically
# if the main frame doesn't match). [id="..."] is always safe for building a selector
# from an id shown in the snapshot; #id is NOT — it breaks on an id containing special
# characters (seen on PeopleSoft: id="#ICYes" needs the selector [id="#ICYes"], since
# #ICYes is parsed as id "" followed by a bogus "ICYes" ID selector).
_SELECTOR_HELP = (
    "Full Playwright selector syntax works here, not just plain CSS — :has-text(...), "
    ":text-is(...), :visible, and combinators like 'tr:has-text(\"X\") input[type=\"checkbox\"]' "
    "are all valid, and several reach into iframes without needing selector/click "
    "fallback. Prefer [id=\"...\"] over #id when building a selector from an id — #id "
    "breaks if the id contains special characters. If the selector matches MORE THAN "
    "ONE element this is refused with a 'strict mode violation: N elements' error — it "
    "never silently acts on the first match — pass nth to pick one, or narrow the "
    "selector (e.g. add :has-text(...) or scope it to a containing row/section)."
)
_NTH_PROP = {
    "type": "integer",
    "description": "0-based index to pick when the selector matches more than one element (see the selector field).",
}

_NAV_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "URL to open (scheme optional — https:// is assumed)."}},
    "required": ["url"],
}
_CLICK_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": f"Selector of the element to click. {_SELECTOR_HELP}"},
        "nth": _NTH_PROP,
    },
    "required": ["selector"],
}
_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Text to type."},
        "selector": {
            "type": "string",
            "description": (
                "Optional selector to click-then-type into; omit to type into the "
                "already-focused element. Typing is real per-character keystrokes (not a "
                "bulk value-set), so for a SPLIT code/OTP field (several 1-digit boxes "
                "with auto-advance JS) pass the FIRST box's selector and the full code as "
                "text — the page's own auto-advance carries the rest to the next boxes. "
                f"If the field already has a value, this REPLACES it, it does not append. {_SELECTOR_HELP}"
            ),
        },
    },
    "required": ["text"],
}
_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "max_chars": {
            "type": "integer",
            "description": (
                "Max characters of visible page text to return (default 4000). Truncation "
                "now cuts at a clean word boundary and says explicitly how much was cut — "
                "if you see '...[truncated: N of M chars shown]', call this again with a "
                "larger max_chars instead of narrowing the page/query and re-snapshotting "
                "repeatedly."
            ),
        },
    },
}
_STATUS_SCHEMA = {"type": "object", "properties": {}}
_SCREENSHOT_SCHEMA = {"type": "object", "properties": {}}
_SOLVE_CAPTCHA_SCHEMA = {
    "type": "object",
    "properties": {
        "image_selector": {
            "type": "string",
            "description": (
                "ONLY for an old-style distorted-text image captcha: the selector of the "
                "<img> holding it. The image is sent for OCR and the answer is returned as "
                "text for you to enter with browser_type — nothing is filled in for you. "
                "Leave this out for reCAPTCHA / hCaptcha / Turnstile (the normal case), "
                f"which are detected and injected automatically. {_SELECTOR_HELP}"
            ),
        },
        "budget_seconds": {
            "type": "integer",
            "description": "How long to wait for the solve before giving up (default 180).",
        },
    },
}
_UPLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {
            "type": "string",
            "description": (
                "Selector of the <input type=\"file\"> ITSELF (check the snapshot's "
                "element list — it's often visible:false, styled behind a button; that's "
                "fine, this does not need the element to be visible or clickable). Not the "
                "visible 'Upload' button — clicking that only opens the OS's native file "
                f"picker, which this tool bypasses entirely. {_SELECTOR_HELP}"
            ),
        },
        "path": {
            "type": "string",
            "description": "Absolute path to the file on THIS server's filesystem (not the operator's machine).",
        },
        "nth": _NTH_PROP,
    },
    "required": ["selector", "path"],
}
_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": f"Selector of the <select> element. {_SELECTOR_HELP}"},
        "nth": _NTH_PROP,
        "value": {
            "type": "string",
            "description": (
                "The option to choose — tried first as the <option>'s value attribute, "
                "then as its visible label text if that doesn't match. Check the "
                "snapshot's element list for a <select>'s available options and which "
                "one is currently selected (marked with *)."
            ),
        },
    },
    "required": ["selector", "value"],
}


def _nth_arg(args: dict) -> "int | None":
    nth = args.get("nth")
    if nth is None:
        return None
    try:
        return int(nth)
    except (TypeError, ValueError):
        return None


async def _run_with_retry(cwd: str, op: "Callable[[Any], Awaitable[Any]]") -> Any:
    """Run ``op(session)`` against the live session for ``cwd``; if it fails because
    the CDP connection itself died — not e.g. a bad selector timing out — the
    session has already self-retired (BrowserSession._retire_if_dead), so getting a
    fresh one and trying exactly once more is what a plain retry SHOULD do here.
    Without this, browser_navigate/browser_snapshot kept re-hitting the same dead
    session and failing identically ("Connection closed while reading from the
    driver") for the rest of the run, with no way to recover short of a service
    restart.
    """
    sess = await _browser_pane.get_or_create(cwd)
    try:
        return await op(sess)
    except Exception as e:
        if not _browser_pane.looks_like_dead_connection(e):
            raise
        fresh = await _browser_pane.get_or_create(cwd)
        if fresh is sess:
            raise  # didn't actually get a new session — retrying would just repeat
        return await op(fresh)


def build_browser_server(cwd: str, agent_actions: str = "read") -> dict:
    """Return {"browser": <sdk-mcp-server>} bound to `cwd`, or {} if unavailable.

    spec-066 safety gate: ``agent_actions`` ∈ {"read", "full"}. Read tools
    (navigate, snapshot, status) are always allowed; mutating tools (click, type —
    they can submit/post as the operator's logged-in identity on a stealth profile)
    are refused with a note when ``agent_actions != "full"``. The operator flips
    this in Extensions → Browser; the default ("read") never silently acts as the
    operator.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except Exception:
        return {}

    _can_mutate = agent_actions == "full"
    _GATE_MSG = (
        "⚠️ Refused: mutating browser actions are disabled (agent_actions=read). "
        "The operator can enable them in Extensions → Browser (agent actions: full), "
        "or perform this click/type themselves in the pane."
    )

    @tool(
        "browser_navigate",
        "Open a URL in the live browser pane (visible to the operator in the cockpit). "
        "Use this to drive a real browser the operator can watch.",
        _NAV_SCHEMA,
    )
    async def browser_navigate(args: dict) -> dict:
        try:
            async def _op(sess):
                await sess.navigate(str(args.get("url") or ""))
                return await sess.snapshot()
            snap = await _run_with_retry(cwd, _op)
            return {"content": [{"type": "text", "text": f"Navigated to {snap['url']} — {snap['title']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_navigate failed: {e}"}]}

    @tool("browser_click", "Click an element in the live browser by CSS selector.", _CLICK_SCHEMA)
    async def browser_click(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            await _run_with_retry(cwd, lambda sess: sess.click(str(args.get("selector") or ""), _nth_arg(args)))
            return {"content": [{"type": "text", "text": f"Clicked {args.get('selector')!r}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_click failed: {e}"}]}

    @tool("browser_type", "Type text in the live browser (optionally into a field given by CSS selector).", _TYPE_SCHEMA)
    async def browser_type(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            await _run_with_retry(
                cwd, lambda sess: sess.type_text(str(args.get("text") or ""), args.get("selector") or None)
            )
            return {"content": [{"type": "text", "text": "Typed."}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_type failed: {e}"}]}

    @tool(
        "browser_select",
        "Choose an option in a native <select> dropdown. Clicking a <select> (or an "
        "<option> inside it) pops OS/browser-native list UI that lives outside the page "
        "and is invisible to browser_click — a required <select> silently stays unset "
        "that way, which is why a form can look fully filled in a snapshot yet still "
        "reject submit with a generic 'please fix the highlighted fields'. This sets the "
        "value directly, no popup involved.",
        _SELECT_SCHEMA,
    )
    async def browser_select(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            selector = str(args.get("selector") or "")
            value = str(args.get("value") or "")
            nth = _nth_arg(args)
            await _run_with_retry(cwd, lambda sess: sess.select_option(selector, value, nth))
            return {"content": [{"type": "text", "text": f"Selected {value!r} in {selector!r}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_select failed: {e}"}]}

    @tool(
        "browser_snapshot",
        "Read the current page in the live browser: url, title, interactive elements "
        "(id/name/class/role, <select> options, invalid-field markers — use these to build "
        "a selector or pick a value instead of guessing), and visible text.",
        _SNAPSHOT_SCHEMA,
    )
    async def browser_snapshot(args: dict) -> dict:
        try:
            max_chars = args.get("max_chars")
            try:
                max_chars = int(max_chars) if max_chars is not None else 4000
            except (TypeError, ValueError):
                max_chars = 4000
            snap = await _run_with_retry(cwd, lambda sess: sess.snapshot(max_chars=max_chars))
            body = f"URL: {snap['url']}\nTitle: {snap['title']}\n"
            if snap.get("elements"):
                body += (
                    "\nInteractive elements (index — tag + attrs — visible text; "
                    "build a selector from id/name/class, e.g. [id=\"x\"] or [name=\"x\"]):\n"
                    f"{snap['elements']}\n"
                )
            body += f"\n{snap['text']}"
            return {"content": [{"type": "text", "text": body}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_snapshot failed: {e}"}]}

    @tool(
        "browser_upload",
        "Upload a local file into a <input type=\"file\"> in the live browser — the ONLY "
        "way to fill a file input through this pane. Clicking an 'Upload'/'Choose file' "
        "button opens the OS's native file picker, which is completely outside the page "
        "and invisible to browser_click/browser_type; typing a path does nothing either "
        "(browsers block programmatic text entry into file inputs). This sets the file "
        "directly via CDP instead, bypassing that dialog.",
        _UPLOAD_SCHEMA,
    )
    async def browser_upload(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            selector = str(args.get("selector") or "")
            path = str(args.get("path") or "")
            nth = _nth_arg(args)
            await _run_with_retry(cwd, lambda sess: sess.upload_file(selector, path, nth))
            return {"content": [{"type": "text", "text": f"Uploaded {path!r} into {selector!r}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_upload failed: {e}"}]}

    @tool(
        "browser_status",
        "Check whether the live browser session is actually healthy (backend, alive, "
        "idle time). Call this after a navigate/click/type/snapshot failure — or after "
        "a couple of failures in a row — to tell 'the connection died, a retry already "
        "rebuilt it' apart from 'my selector is wrong', instead of guessing.",
        _STATUS_SCHEMA,
    )
    async def browser_status(args: dict) -> dict:
        try:
            sess = await _browser_pane.get_or_create(cwd)
            st = sess.status()
            lines = [f"{k}: {v}" for k, v in st.items()]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_status failed: {e}"}]}

    @tool(
        "browser_screenshot",
        "Get a real image of the live browser page at full resolution — not the "
        "downscaled/low-quality feed used for the live pane. Use this when the text/DOM "
        "snapshot is ambiguous, when the operator reports seeing something the snapshot "
        "doesn't explain, or to visually confirm state (a checkbox, a dialog, a captcha) "
        "before or after acting on it.",
        _SCREENSHOT_SCHEMA,
    )
    async def browser_screenshot(args: dict) -> dict:
        try:
            data = await _run_with_retry(cwd, lambda sess: sess.screenshot())
            b64 = base64.b64encode(data).decode("ascii")
            return {"content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_screenshot failed: {e}"}]}

    @tool(
        "browser_solve_captcha",
        "Get past a captcha blocking the live browser. Handles reCAPTCHA v2/v3, hCaptcha "
        "and Cloudflare Turnstile widgets: it detects the widget, has it solved externally, "
        "and injects the response token plus fires the site's callback. Do NOT try to click "
        "the checkbox or pick the 'select all fire hydrants' tiles first — the image grid is "
        "never touched, the token replaces the whole interaction, so call this as soon as you "
        "see a captcha. Costs real money per call (~$0.001-0.003), so don't call it "
        "speculatively — confirm a captcha is actually there (browser_snapshot or "
        "browser_screenshot) first. Afterwards, verify: snapshot the page, and submit the "
        "form yourself if it didn't auto-submit. A full-page Cloudflare 'Verify you are "
        "human' interstitial is refused with an explanation — that one needs the operator.",
        _SOLVE_CAPTCHA_SCHEMA,
    )
    async def browser_solve_captcha(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            import captcha_solver as _cs
            if not _cs.configured():
                return {"content": [{"type": "text", "text": (
                    "⚠️ No 2captcha API key configured. Set TWOCAPTCHA_API_KEY in .env or "
                    f"store it in the safe as '{_cs.API_KEY_SECRET}'."
                )}]}
            budget = args.get("budget_seconds")
            try:
                budget = float(budget) if budget is not None else 180.0
            except (TypeError, ValueError):
                budget = 180.0
            selector = args.get("image_selector") or None
            res = await _run_with_retry(
                cwd, lambda sess: sess.solve_captcha(budget=budget, image_selector=selector)
            )

            if res.get("mode") == "image":
                return {"content": [{"type": "text", "text": (
                    f"Image captcha solved in {res['seconds']}s (cost ${res['cost']}).\n"
                    f"Answer: {res['text']}\n"
                    "Nothing was filled in — type this into the captcha field yourself with "
                    "browser_type, then submit."
                )}]}

            lines = [
                f"Solved a {res['kind']} captcha in {res['seconds']}s (cost ${res['cost']}).",
                f"Token written to: {', '.join(res['fields']) if res['fields'] else 'NO field found'}",
                f"Site callback fired: {', '.join(res['callbacks']) if res['callbacks'] else 'none found'}",
            ]
            if res.get("extra_widgets"):
                lines.append(f"⚠️ {res['extra_widgets']} more captcha widget(s) on this page — rerun if needed.")
            for note in res.get("notes") or []:
                lines.append(f"note: {note}")
            if not res["callbacks"]:
                lines.append(
                    "No callback fired, so the page may not know yet. Snapshot it: if the "
                    "submit button is still disabled the token didn't take; if the form is "
                    "now submittable, just submit it."
                )
            else:
                lines.append("Verify with browser_snapshot, then submit the form if it didn't auto-submit.")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_solve_captcha failed: {e}"}]}

    server = create_sdk_mcp_server(
        name="browser", version="1.0.0",
        tools=[
            browser_navigate, browser_click, browser_type, browser_upload, browser_select,
            browser_snapshot, browser_status, browser_screenshot, browser_solve_captcha,
        ],
    )
    return {"browser": server}
