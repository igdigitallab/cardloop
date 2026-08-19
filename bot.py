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
import tunnel          # spec-082 B: --tunnel / CARDLOOP_TUNNEL zero-config remote access + QR

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
import codex_engine  # noqa: E402  (isolated optional provider; SDK import stays lazy)
# Re-exported from engine so `import bot; bot.X` keeps working for tests and any
# external caller (webapp imports engine directly and does NOT import bot).
from engine import (  # noqa: E402,F401  (deliberate: import after env load; re-exports)
    HERE, DATA,
    DEFAULT_CWD, DEFAULT_MODEL, MODELS,
    DEFAULT_AGENTS, _build_agents_kwargs,
    CONDUCTOR_PROMPT, DEFAULT_NUDGE, DISALLOWED_TOOLS,
    BOARD_PROTOCOL, TOPICS_F, SESSIONS_F,
    OPERATOR_NAME, RESPONSE_LANGUAGE, _lang_directive,
    AUDIT_DIR,
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
    ctx = _engine_build_ctx(
        web_port=WEB_PORT,
        web_password=WEB_PASSWORD,
    )
    # spec-072: E2E smoke harness opt-in. Swaps ctx["run_engine"] for a scripted,
    # network-free fake (e2e_fake_engine.py) so tests/e2e/ can drive a REAL cockpit
    # subprocess deterministically — no SDK calls, no tokens. Never set in production;
    # a fresh deployment's .env never sets this flag, so this is a no-op by default.
    if os.environ.get("E2E_FAKE_ENGINE") == "1":
        from e2e_fake_engine import run_engine as _e2e_run_engine
        ctx["run_engine"] = _e2e_run_engine
        print("[e2e] E2E_FAKE_ENGINE=1 — ctx['run_engine'] replaced by e2e_fake_engine.run_engine")
    ctx["run_codex_engine"] = codex_engine.run_codex_engine
    ctx["codex_provider_info"] = codex_engine.provider_info
    return ctx


async def _maybe_start_tunnel(tunnel_enabled: bool) -> "str | None":
    """spec-082 B: bring up a cloudflared quick tunnel + print its URL/QR, or — when
    tunnelling is off/unavailable — print a QR for the LAN URL if WEB_HOST binds a
    non-localhost interface. Returns the public tunnel URL, or None.

    Factored out of _amain() so it's unit-testable without booting the whole cockpit.
    Never raises and never blocks startup: any failure just leaves the cockpit
    reachable on localhost only.
    """
    web_host = os.environ.get("WEB_HOST", "127.0.0.1")
    tunnel_url = None
    if tunnel_enabled:
        try:
            # Defense in depth: main() already refuses to start at all with a bad
            # password, so this normally can't fail here — but a public tunnel must
            # never come up if this check would, even if this helper is ever driven
            # some other way than through main().
            _check_web_password(WEB_PASSWORD)
        except RuntimeError as e:
            print(f"[tunnel] refusing to start the tunnel — {e}")
        else:
            tunnel_url = await tunnel.start_tunnel(WEB_PORT)
            if tunnel_url:
                print("=" * 72)
                print(f"[tunnel] cockpit is reachable from the INTERNET at: {tunnel_url}")
                print("[tunnel] protected only by WEB_PASSWORD (+ TOTP if enabled) — keep it strong.")
                print("[tunnel] this hostname is ephemeral — it changes on every restart.")
                print("=" * 72)
                tunnel.print_qr(tunnel_url, label="[tunnel] scan to open on your phone:")
    if not tunnel_url:
        lan = tunnel.lan_url(web_host, WEB_PORT)
        if lan:
            print(f"[webapp] LAN URL: {lan}")
            tunnel.print_qr(lan, label="[webapp] scan to open on your phone (same network):")
    return tunnel_url


async def _amain(*, tunnel_enabled: bool = False) -> None:
    """Async entry point — starts the web cockpit + engine on the asyncio loop.

    Loop ownership: a single asyncio loop drives aiohttp.  Systemd owns process
    termination — we never call os._exit or kill ourselves (cgroup gotcha: any
    such call inside the cgroup tears down the daemon mid-flight).

    tunnel_enabled: spec-082 B. When set, brings up a cloudflared quick tunnel after
    the cockpit starts and prints the public URL + a terminal QR code. Opt-in only —
    see main()/_tunnel_requested(). Never blocks or aborts startup on failure.
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
    if codex_engine.codex_enabled():
        # Auth/model discovery failures degrade only the optional provider. Claude
        # startup and all legacy state remain available.
        info = await codex_engine.provider_info(force=True)
        ctx["codex_startup_info"] = info
        if info.get("available"):
            print(f"[codex] ready via {info.get('auth_type')} auth ({len(info.get('models', []))} models)")
        else:
            print(f"[codex] unavailable; Claude remains active: {info.get('error')}")
    await webapp.start(ctx)
    print("Cardloop started (web cockpit + kanban auto-run).")

    # spec-082 B: zero-config remote access. Never aborts startup — a tunnel failure
    # just means the cockpit stays reachable on localhost only.
    await _maybe_start_tunnel(tunnel_enabled)

    # Idle until shutdown signal
    try:
        await _stop_event.wait()
    finally:
        # spec-082 B: tear down the quick tunnel (if any) FIRST, before the session
        # flush below — bounded internally (QuickTunnel.stop()), so this never delays
        # shutdown, and it guarantees no orphan cloudflared process survives us.
        await tunnel.stop_tunnel()

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
    """Raise RuntimeError if the web password is empty, unset, or still the shipped placeholder.

    Factored out of main() so tests can call it directly without triggering sys.exit.
    """
    if not password:
        raise RuntimeError(
            "FATAL: WEB_PASSWORD must be set (refusing to start with blank password)"
        )
    if password.strip().upper() == "CHANGE_ME":
        raise RuntimeError(
            "FATAL: WEB_PASSWORD is still the placeholder 'CHANGE_ME' — "
            "set a real password in .env before starting"
        )


def _parse_args(argv=None):
    """Parse Cardloop's own CLI flags.

    argv=None reads sys.argv[1:] (the normal `python bot.py ...` case). Tests pass an
    explicit list so pytest's own argv never leaks into this parser.
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="Cardloop — a self-hosted cockpit for driving the Claude Agent SDK full-auto.",
    )
    parser.add_argument(
        "--tunnel", action="store_true",
        help=(
            "Expose the cockpit on a public https://*.trycloudflare.com URL via a "
            "cloudflared quick tunnel and print a scannable QR code (equivalent to "
            "CARDLOOP_TUNNEL=1). Opt-in: the tunnel is a public URL protected only by "
            "WEB_PASSWORD (+ TOTP if enabled). Requires the cloudflared binary — falls "
            "back to localhost-only with an install hint if it's missing."
        ),
    )
    return parser.parse_args(argv)


def _tunnel_requested(args) -> bool:
    """--tunnel (CLI, manual runs) OR CARDLOOP_TUNNEL=1 (env, for the systemd unit)."""
    if args.tunnel:
        return True
    return os.environ.get("CARDLOOP_TUNNEL", "").strip().lower() in ("1", "true", "yes")


def main():
    args = _parse_args()
    _check_web_password(WEB_PASSWORD)
    asyncio.run(_amain(tunnel_enabled=_tunnel_requested(args)))


if __name__ == "__main__":
    main()
