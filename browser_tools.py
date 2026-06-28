"""spec-065 Phase C — agent MCP tools that drive the live browser pane.

These tools act on the SAME browser session the operator watches in the cockpit
pane (keyed by the project cwd in browser_pane._SESSIONS), so the agent's actions
are visible live. The server is built per-run with `cwd` bound, and is only wired
into the engine when the `browser` module is enabled (see engine.py).
"""
from __future__ import annotations

import browser_pane as _browser_pane

_NAV_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "URL to open (scheme optional — https:// is assumed)."}},
    "required": ["url"],
}
_CLICK_SCHEMA = {
    "type": "object",
    "properties": {"selector": {"type": "string", "description": "CSS selector of the element to click."}},
    "required": ["selector"],
}
_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Text to type."},
        "selector": {"type": "string", "description": "Optional CSS selector of a field to fill; omit to type into the focused element."},
    },
    "required": ["text"],
}
_SNAPSHOT_SCHEMA = {"type": "object", "properties": {}}


def build_browser_server(cwd: str, agent_actions: str = "read") -> dict:
    """Return {"browser": <sdk-mcp-server>} bound to `cwd`, or {} if unavailable.

    spec-066 safety gate: ``agent_actions`` ∈ {"read", "full"}. Read tools
    (navigate, snapshot) are always allowed; mutating tools (click, type — they can
    submit/post as the operator's logged-in identity on a stealth profile) are
    refused with a note when ``agent_actions != "full"``. The operator flips this in
    Extensions → Browser; the default ("read") never silently acts as the operator.
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
            sess = await _browser_pane.get_or_create(cwd)
            await sess.navigate(str(args.get("url") or ""))
            snap = await sess.snapshot()
            return {"content": [{"type": "text", "text": f"Navigated to {snap['url']} — {snap['title']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_navigate failed: {e}"}]}

    @tool("browser_click", "Click an element in the live browser by CSS selector.", _CLICK_SCHEMA)
    async def browser_click(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            sess = await _browser_pane.get_or_create(cwd)
            await sess.click(str(args.get("selector") or ""))
            return {"content": [{"type": "text", "text": f"Clicked {args.get('selector')!r}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_click failed: {e}"}]}

    @tool("browser_type", "Type text in the live browser (optionally into a field given by CSS selector).", _TYPE_SCHEMA)
    async def browser_type(args: dict) -> dict:
        if not _can_mutate:
            return {"content": [{"type": "text", "text": _GATE_MSG}]}
        try:
            sess = await _browser_pane.get_or_create(cwd)
            await sess.type_text(str(args.get("text") or ""), args.get("selector") or None)
            return {"content": [{"type": "text", "text": "Typed."}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_type failed: {e}"}]}

    @tool("browser_snapshot", "Read the current page in the live browser: url, title, and visible text.", _SNAPSHOT_SCHEMA)
    async def browser_snapshot(args: dict) -> dict:
        try:
            sess = await _browser_pane.get_or_create(cwd)
            snap = await sess.snapshot()
            return {"content": [{"type": "text", "text": f"URL: {snap['url']}\nTitle: {snap['title']}\n\n{snap['text']}"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"⚠️ browser_snapshot failed: {e}"}]}

    server = create_sdk_mcp_server(
        name="browser", version="1.0.0",
        tools=[browser_navigate, browser_click, browser_type, browser_snapshot],
    )
    return {"browser": server}
