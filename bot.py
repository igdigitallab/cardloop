#!/usr/bin/env python3
"""
Cardloop — a self-hosted cockpit for driving the Claude Agent SDK full-auto.

Two channels: the web cockpit (PWA) and the kanban board auto-run. One engine.

This file is a thin launcher: it loads env + auth config, builds the shared ctx,
and starts the aiohttp web cockpit (webapp.py) + engine on a single asyncio loop.
The transport-neutral engine block (run_engine, state dicts, audit, reconcile_board,
etc.) lives in engine.py.
"""
import asyncio
import os
from pathlib import Path

import webapp          # web cockpit (webapp.py) — started alongside, state shared via ctx

# ─────────────────────────── config ───────────────────────────
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)


def _load_env():
    # Opt-out: set COPS_NO_DOTENV=1 to skip auto-loading .env (tests, or deployments
    # that inject env directly via systemd/Docker). Keeps default behavior unchanged.
    if os.environ.get("COPS_NO_DOTENV"):
        return
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# ── Auth mode: "subscription" (default) or "api_key" ──────────────────────────
# subscription: SDK uses OAuth credentials from ~/.claude/.credentials.json.
#   ANTHROPIC_API_KEY is forcibly removed to prevent accidental API billing.
# api_key: ANTHROPIC_API_KEY is passed through; the SDK uses it and BILLS the
#   Anthropic API. Use only as a conscious opt-in — never the default.
CLAUDE_AUTH_MODE = os.environ.get("CLAUDE_AUTH_MODE", "subscription")
if CLAUDE_AUTH_MODE == "subscription":
    # Remove any API key from the environment so the SDK cannot accidentally
    # fall back to API billing.  This is the money-safety guard.
    os.environ.pop("ANTHROPIC_API_KEY", None)
# api_key mode: do nothing — ANTHROPIC_API_KEY stays in os.environ and the
# SDK will pick it up automatically.

WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))           # web cockpit port
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")            # passphrase for cockpit login

# ── engine imported AFTER _load_env() + auth ──────────────────────────────────
# engine.py reads env vars at module level; importing it before _load_env() would
# cause env-dependent constants (DEFAULT_CWD, OPERATOR_NAME, …) to use defaults.
import engine  # noqa: E402,F401  (after env load; re-exported for tests)
# Re-exported from engine so `import bot; bot.X` keeps working for tests and any
# external caller (webapp imports engine directly and does NOT import bot).
from engine import (  # noqa: E402,F401  (deliberate: import after env load; re-exports)
    HERE, DATA,
    DEFAULT_CWD, DEFAULT_MODEL, MODELS,
    DEFAULT_AGENTS, _build_agents_kwargs,
    CONDUCTOR_PROMPT, DEFAULT_NUDGE, DISALLOWED_TOOLS,
    BOARD_PROTOCOL, TOPICS_F, SESSIONS_F,
    OPERATOR_NAME, RESPONSE_LANGUAGE, _lang_directive,
    AUDIT_DIR, STALL_SECONDS, MAX_SECONDS,
    PERSISTENT_CLIENT, LIVE_CLIENT_TTL_SEC, LIVE_CLIENT_MAX,
    topics, sessions, costs, running, rate_limits, pending_handoff, context_warned,
    save_topics, save_sessions, save_handoff,
    key_of, resolve_project, build_registry, REGISTRY, _REG_RAW,
    _read, _migrate_session_keys, _run_startup_migration,
    audit, short, _is_destructive,
    _tool_response_to_str, _make_post_tool_use_hook, _HOOK_OUTPUT_TRUNCATE,
    _live_clients, _evict_live_client,
    _build_board_append, reconcile_board, _apply_reconcile_ops,
    run_engine,
    _build_ctx as _engine_build_ctx, _graceful_shutdown,
    _LiveEntry,
    ClaudeSDKClient,
)


def _build_ctx() -> dict:
    """Build the shared context dict passed to webapp.start()."""
    return _engine_build_ctx(
        web_port=WEB_PORT,
        web_password=WEB_PASSWORD,
    )


async def _amain() -> None:
    """Async entry point — starts the web cockpit + engine on the asyncio loop.

    Loop ownership: a single asyncio loop drives aiohttp.  Systemd owns process
    termination — we never call os._exit or kill ourselves (cgroup gotcha: any
    such call inside the cgroup tears down the daemon mid-flight).
    """
    # spec-039: stop event — SIGTERM/SIGINT handlers set this instead of raising;
    # the main coroutine awaits it, then performs graceful cleanup and returns.
    _stop_event = asyncio.Event()

    def _handle_shutdown_signal():
        print("[signal] shutdown requested — initiating graceful flush")
        _stop_event.set()

    loop = asyncio.get_running_loop()
    import signal as _signal
    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(_sig, _handle_shutdown_signal)
        except (NotImplementedError, RuntimeError):
            # Windows / restricted environments — fall back to default behaviour.
            pass

    # spec-040 Phase 0: migrate legacy session keys to slug format.
    # Runs here (startup, before serving) — NOT at import time — to avoid mutating
    # data/*.json as a side-effect of ``import bot`` in tests.
    _run_startup_migration()

    ctx = _build_ctx()
    await webapp.start(ctx)
    print("Cardloop started (web cockpit + kanban auto-run).")

    # Idle until shutdown signal
    try:
        await _stop_event.wait()
    finally:
        # spec-039 graceful shutdown — two-phase:
        # Phase 1 (UNBOUNDED): flush sessions + evict live clients.  Must always
        #   run fully — losing session state on restart is worse than a slow stop.
        await _graceful_shutdown(_live_clients)

        # Phase 2 (BOUNDED ≤12 s): tear down webapp background loops + aiohttp runner.
        try:
            await asyncio.wait_for(webapp.stop(), timeout=12.0)
            print("[shutdown] clean teardown complete")
        except asyncio.TimeoutError:
            # State is already flushed (Phase 1 finished).  Log and fall through
            # so asyncio.run() can cancel remaining tasks and exit the loop.
            print("[shutdown] WARNING: bounded teardown timed out (12 s) — "
                  "forcing loop exit; state was already flushed in Phase 1")

        # Phase 3: cancel any remaining non-current tasks so asyncio.run() returns
        # immediately rather than waiting for them to drain.
        current = asyncio.current_task()
        remaining = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if remaining:
            print(f"[shutdown] cancelling {len(remaining)} lingering task(s)")
            for t in remaining:
                t.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)


def _check_web_password(password: str) -> None:
    """Raise RuntimeError if the web password is empty or unset.

    Factored out of main() so tests can call it directly without triggering sys.exit.
    """
    if not password:
        raise RuntimeError(
            "FATAL: WEB_PASSWORD must be set (refusing to start with blank password)"
        )


def main():
    _check_web_password(WEB_PASSWORD)
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
