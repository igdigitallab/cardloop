"""
webapp.py — browser cockpit for Claude-Ops-Bot.

Runs in the same process/loop as the PTB bot.
All state objects are passed via ctx — mutations are visible to the bot.
Does NOT import bot.py directly (re-import would create a second instance).
"""

import asyncio
import glob
import hashlib
import json
import logging
import ipaddress
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import traceback as _tb
import unicodedata
import uuid as _uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional, TypedDict

import aiohttp
from aiohttp import web

# Spec-019: Schedules registry module
import schedules as _schedules

# Spec-026 Phase 3: built-in encrypted secret store
import secretstore as _secretstore

# Spec-026 Phase 2: TOTP second factor
import totp as _totp


# ─────────────────────────── named constants ───────────────────────────

# Scratch dir for internal one-shot helper queries (handoff summarizer, auto-titler, etc.)
# so their transcripts never appear in any project's session dropdown.
_OPS_SCRATCH_CWD = str(Path.home() / ".claude" / "ops-scratch")
Path(_OPS_SCRATCH_CWD).mkdir(parents=True, exist_ok=True)

_BUS_QUEUE_SIZE = 100   # maxsize per-session bus queue; full → drop (non-blocking)
_BUS_GLOBAL_SIZE = 200  # maxsize global bus queue (all sessions)

# ─────────────────────────── Deferred Runs (Spec 020) ───────────────────────────
_DEFERRED_POLL_SEC = int(os.environ.get("DEFERRED_POLL_SEC", "30"))
_DEFERRED_MAX_ATTEMPTS = int(os.environ.get("DEFERRED_MAX_ATTEMPTS", "5"))
_DEFERRED_FREE_THRESHOLD = float(os.environ.get("DEFERRED_FREE_THRESHOLD", "0.10"))
_DEFERRED_RESET_FALLBACK_SEC = int(os.environ.get("DEFERRED_RESET_FALLBACK_SEC", str(6 * 3600)))
_DEFERRED_FILE: "Path | None" = None  # set in _deferred_init(ctx)

# Phase D: auto-resume on rate-limit
# AUTO_RESUME_ON_RATE_LIMIT=1 (default) — create a fire_on_reset deferred record whenever a run
# terminates with api_error_status=429 (rate-limited mid-flight). Set to 0 to disable.
_AUTO_RESUME_ON_RATE_LIMIT = int(os.environ.get("AUTO_RESUME_ON_RATE_LIMIT", "0"))  # spec-039: default OFF
# Maximum consecutive auto-resume records allowed in one chain (loop guard).
# Counted via auto_resume_count on the deferred record.
_AUTO_RESUME_MAX = int(os.environ.get("AUTO_RESUME_MAX", "3"))

# ─────────────────────────── Context Window + Rotation (Spec 021 / Spec 039) ───────────────────────
# CONTEXT_WINDOW: the real model context window in tokens.
# Defaults to 1 000 000 (Opus 4.8 on Claude Max subscription — confirmed via get_context_usage()).
# Override via env if the window ever changes.
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "1000000"))
# CONTEXT_ROTATE_AT: token count that triggers auto-rotation (dead code since spec-039).
# Kept so env-var reads don't break; default scaled to 95% of CONTEXT_WINDOW.
CONTEXT_ROTATE_AT = int(os.environ.get("CONTEXT_ROTATE_AT", str(int(CONTEXT_WINDOW * 0.95))))
CONTEXT_ROTATION = os.environ.get("CONTEXT_ROTATION", "1") == "1"
# CONTEXT_WARN_AT: token count that triggers a one-time early warning.
# Fires on the first turn that crosses this threshold (upward only), before the hard backstop.
# Suppressed once rotation fires (i.e. no warn if already at/above CONTEXT_ROTATE_AT).
# Default: 85% of CONTEXT_WINDOW (~850 000); honor env override if set.
CONTEXT_WARN_AT = int(os.environ.get("CONTEXT_WARN_AT", str(int(CONTEXT_WINDOW * 0.85))))

# Spec-029 item 3: structured card results via SDK output_format.
# STRUCTURED_CARDS=1 enables requesting structured JSON output from card runs so the agent's
# self-reported summary/status/changes are captured deterministically in structured_output.
# Default OFF: the feature is fully fallback-guarded (missing/malformed → prose path), but is
# kept off by default until validated in production to avoid any risk of regressing card runs.
# The structured_output improves SUMMARY TEXT only; card board-column assignment (ok/failed)
# is still driven by the exception-based `ok` flag — that path is unchanged.
STRUCTURED_CARDS = os.environ.get("STRUCTURED_CARDS", "0") == "1"

# JSON schema requested from the agent when STRUCTURED_CARDS=1.
# The agent fills this at the end of its card run; we read structured_output from ResultMessage.
_CARD_OUTPUT_SCHEMA: "dict" = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Brief summary of what was done (shown in Review column).",
            },
            "status": {
                "type": "string",
                "enum": ["done", "partial", "failed"],
                "description": "Self-reported completion status.",
            },
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of files/actions changed (optional, may be empty).",
            },
        },
        "required": ["summary", "status"],
        "additionalProperties": False,
    },
}

# Prompt sent to haiku to produce a handoff summary of the current session.
ROTATION_SUMMARY_PROMPT = (
    "Summarize this session for handoff: active tasks + their state, key decisions, "
    "important file paths, unresolved questions. Be dense, ≤500 words, English."
)

# Strong references for long-lived background tasks created via asyncio.create_task.
# Prevents GC from collecting tasks before they complete (Python docs warning).
_BG_TASKS: set = set()

# spec-039 shutdown: the 5 always-on background loops spawned at startup are tracked
# here so webapp.stop() can cancel + await them.  Populated exclusively by start().
_STARTUP_BG_TASKS: list = []

# spec-039 shutdown: the AppRunner created in start() is stored here so stop() can
# call runner.cleanup().  None until start() completes.
_runner = None


def _spawn_bg(coro):
    """Creates a fire-and-forget task, protected from GC via _BG_TASKS.
    The task result is not used by the caller — side effects only."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t



# ─────────────────────────── activity bus ───────────────────────────
#
# Lightweight in-process event bus: dict[session_key -> set[asyncio.Queue]].
# Everything runs in one event loop → plain set/dict, no asyncio.Lock needed.
# Queue maxsize=_BUS_QUEUE_SIZE: full → drop (put_nowait in try/except), producer is non-blocking.

_bus: dict[str, set[asyncio.Queue]] = {}
# Global subscribers — receive ALL events from all sessions, with session_key injected.
# Used for the application-wide activity stream (unread indicators in the sidebar).
_bus_global: set[asyncio.Queue] = set()

# ── Tab activity state (board card ops:b2a081) ───────────────────────────────
# session_key → timestamp of last run_end (set when a turn finishes).
# Cleared by POST /api/projects/{id}/seen (operator opened the tab).
# Used to compute awaiting = last_finished_ts > last_seen_ts.
_awaiting: dict[str, float] = {}
# session_key → timestamp of last operator "seen" action (tab focus/open).
_seen: dict[str, float] = {}


def _bus_subscribe(session_key: str) -> "asyncio.Queue[dict]":
    """Creates a queue and registers a subscriber for session_key."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_BUS_QUEUE_SIZE)
    _bus.setdefault(session_key, set()).add(q)
    return q


def _bus_unsubscribe(session_key: str, q: "asyncio.Queue[dict]") -> None:
    """Removes a subscriber; clears the key if no subscribers remain."""
    subscribers = _bus.get(session_key)
    if subscribers is not None:
        subscribers.discard(q)
        if not subscribers:
            _bus.pop(session_key, None)


def _bus_subscribe_global() -> "asyncio.Queue[dict]":
    """Subscribe to ALL events from all sessions (events carry a session_key field)."""
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_BUS_GLOBAL_SIZE)
    _bus_global.add(q)
    return q


def _bus_unsubscribe_global(q: "asyncio.Queue[dict]") -> None:
    _bus_global.discard(q)


def _bus_publish(session_key: str, event: dict, persist: bool = True) -> None:
    """Publishes an event to all subscriber queues. Full queue → drop (non-blocking).

    persist=True (default) — also append to the timeline JSONL (prior behaviour for all callers).
    persist=False — live fan-out only, no timeline write. Used by the spec-035 per-event
    web-chat publish so the SSE/LiveTurn feed gets every event without changing timeline
    granularity (chat text was never per-event persisted; tool events already reach the
    timeline via the PostToolUse hook — avoid double-recording)."""
    subscribers = _bus.get(session_key)
    if subscribers:
        for q in list(subscribers):  # list() — snapshot, since _bus_unsubscribe may be called concurrently
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer — drop, do not block the producer
    # Global broadcast — enrich the event with session_key so the frontend can match it to a project
    if _bus_global:
        enriched = {**event, "session_key": session_key}
        for q in list(_bus_global):
            try:
                q.put_nowait(enriched)
            except asyncio.QueueFull:
                pass
    # Timeline persistence — single write point for all bus events (unless persist=False)
    if persist:
        _timeline_append(session_key, event)
    # Tab activity state — track awaiting (run finished, operator hasn't looked yet).
    # run_end → project is now awaiting operator attention.
    # run_start → new run started, clear any previous awaiting marker.
    kind = event.get("kind")
    if kind == "run_end":
        _awaiting[session_key] = time.time()
    elif kind == "run_start":
        _awaiting.pop(session_key, None)


# ─────────────────────────── live turn buffer (spec-035) ─────────────────────
# In-memory ring buffer of events for the current (or last) agent turn.
# Keyed by session_key. Retained for 300s after turn completion for reconnect replay.

_live_turns: dict[str, dict] = {}

_LIVE_TURN_MAXLEN = 2000  # ring buffer cap per session
_LIVE_TURN_RETAIN_SEC = 300  # seconds to keep a completed turn in memory


def _live_turn_create(session_key: str, model: str) -> dict:
    """Creates a new LiveTurn for session_key and stores it. Returns the turn dict."""
    turn: dict = {
        "turn_id": str(_uuid.uuid4()),
        "started_at": time.time(),
        "model": model,
        "status": "running",
        "seq": 0,  # monotonic counter — next seq to assign
        "events": deque(maxlen=_LIVE_TURN_MAXLEN),
        "cost_usd": None,
    }
    _live_turns[session_key] = turn
    return turn


def _live_turn_append(session_key: str, event: dict) -> dict:
    """Assigns the next seq to event, appends to the ring buffer, updates cost_usd.
    Returns the seq-tagged event dict (shallow copy + seq)."""
    turn = _live_turns.get(session_key)
    if turn is None:
        return event  # guard: no active turn (shouldn't happen in normal flow)
    seq = turn["seq"]
    turn["seq"] = seq + 1
    tagged = {"seq": seq, **event}
    turn["events"].append(tagged)
    # Accumulate cost from result events if present
    cost = event.get("cost_usd")
    if cost is not None:
        try:
            turn["cost_usd"] = (turn["cost_usd"] or 0.0) + float(cost)
        except (TypeError, ValueError):
            pass
    return tagged


def _live_turn_finish(session_key: str, status: str) -> None:
    """Marks the LiveTurn as done/error (idempotent). Schedules cleanup after retain period."""
    turn = _live_turns.get(session_key)
    if turn is None or turn["status"] != "running":
        return  # idempotent — already finished or gone
    turn["status"] = status
    try:
        asyncio.get_event_loop().call_later(_LIVE_TURN_RETAIN_SEC, _live_turn_drop, session_key)
    except RuntimeError:
        pass  # no running loop (e.g., during tests with custom loops) — skip cleanup scheduling


def _live_turn_drop(session_key: str) -> None:
    """Removes the LiveTurn from memory."""
    _live_turns.pop(session_key, None)


# ─────────────────────────── timeline persistence ─────────────────────────────
#
# Every bus event is persisted to JSONL: DATA/timeline/<slug>.jsonl.
# Slug = cwd.replace('/', '-'), matching _sdk_sessions_dir.
# Rotation: file >5MB → rename to .jsonl.1 (one backup copy; overwrites old .1).
# Write errors are swallowed — never breaks a run.
# Init: start() calls _timeline_init(ctx) — passes DATA and the topics dict.

_TIMELINE_DATA_DIR: "Path | None" = None   # DATA/timeline/ — set in start()
_TIMELINE_TOPICS: "dict | None" = None     # ref to ctx["topics"] — for session_key→cwd lookup
_TIMELINE_MAX_SIZE = 5 * 1024 * 1024       # 5 MB — rotation threshold
_TIMELINE_TEXT_LIMIT = 2000                # chars — text field truncation limit


def _timeline_init(ctx: dict) -> None:
    """Called from start() — stores references for _timeline_append."""
    global _TIMELINE_DATA_DIR, _TIMELINE_TOPICS
    _TIMELINE_DATA_DIR = ctx["DATA"] / "timeline"
    _TIMELINE_TOPICS = ctx["topics"]
    try:
        _TIMELINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _timeline_slug_from_cwd(cwd: str) -> str:
    """Stable slug from cwd (identical to _sdk_sessions_dir): '/' → '-'."""
    return cwd.replace("/", "-")


def _timeline_path(session_key: str) -> "Path | None":
    """Returns the Path to the .jsonl file for session_key, or None if DATA is not initialised.
    Resolves session_key → cwd via _TIMELINE_TOPICS; falls back to _unknown.jsonl if not found."""
    if _TIMELINE_DATA_DIR is None:
        return None
    cwd: str | None = None
    if _TIMELINE_TOPICS:
        topic_data = _TIMELINE_TOPICS.get(session_key)
        if topic_data:
            cwd = topic_data.get("cwd")
    if cwd:
        slug = _timeline_slug_from_cwd(cwd)
    else:
        # session_key may be a free-chat id or unknown topic — encode safely
        safe = session_key.replace("/", "-").replace(":", "-")
        slug = safe if safe else "_unknown"
    return _TIMELINE_DATA_DIR / f"{slug}.jsonl"


def _timeline_append(session_key: str, event: dict) -> None:
    """Appends an event to the JSONL log. Swallows errors (never breaks a run).
    Never logs env fields — they don't appear in events; guard against future changes."""
    try:
        path = _timeline_path(session_key)
        if path is None:
            return
        # Build record: add ts, truncate text, exclude env
        record: dict = {"ts": time.time(), "session_key": session_key}
        for k, v in event.items():
            if k == "env":
                continue  # env — secrets, never in timeline
            if k == "text" and isinstance(v, str) and len(v) > _TIMELINE_TEXT_LIMIT:
                record[k] = v[:_TIMELINE_TEXT_LIMIT] + "…"
            else:
                record[k] = v
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # Rotation: if the file already exists and is > 5MB — rename to .1
        try:
            if path.exists() and path.stat().st_size > _TIMELINE_MAX_SIZE:
                backup = path.with_suffix(".jsonl.1")
                path.rename(backup)
        except Exception:
            pass
        # Append
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # never break a run


# ─────────────────────────── tool formatter ───────────────────────────

def _format_tool(name: str, inp: dict) -> dict:
    """Unified tool-event formatter: returns a rich structure keyed by tool type.
    Used in all three places: chat SSE, bus publish, session-history."""
    if not isinstance(inp, dict):
        inp = {}

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return {"name": name, "kind": "bash", "cmd": cmd, "desc": desc}

    elif name in ("Edit", "MultiEdit", "NotebookEdit"):
        file_path = inp.get("file_path", "")
        if name == "Edit":
            old_str = inp.get("old_string", "")
            new_str = inp.get("new_string", "")
            if isinstance(old_str, str) and len(old_str) > 400:
                old_str = old_str[:400] + "…"
            if isinstance(new_str, str) and len(new_str) > 400:
                new_str = new_str[:400] + "…"
            return {"name": name, "kind": "edit", "file": file_path, "old": old_str, "new": new_str}
        elif name == "MultiEdit":
            edits = inp.get("edits", [])
            count = len(edits) if isinstance(edits, list) else 0
            return {"name": name, "kind": "edit", "file": file_path, "count": count}
        else:  # NotebookEdit
            cell_type = inp.get("cell_type", "")
            return {"name": name, "kind": "edit", "file": file_path, "cell_type": cell_type}

    elif name == "Write":
        file_path = inp.get("file_path", "")
        content = inp.get("content", "")
        if isinstance(content, str) and len(content) > 600:
            preview = content[:600] + "…"
        else:
            preview = content if isinstance(content, str) else ""
        return {"name": name, "kind": "write", "file": file_path, "preview": preview}

    elif name == "Read":
        file_path = inp.get("file_path", "")
        return {"name": name, "kind": "read", "file": file_path}

    elif name in ("Glob", "Grep"):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return {"name": name, "kind": "search", "pattern": pattern, "path": path}

    else:
        # other: take the first value as summary
        first = next(iter(inp.values()), "") if inp else ""
        summary = str(first)
        if len(summary) > 200:
            summary = summary[:200] + "…"
        return {"name": name, "kind": "other", "summary": summary}


COOKIE_MAX_AGE = 2592000  # 30 days in seconds

# WEB_COOKIE_SECURE: controls the Secure flag on the auth cookie.
# When True the browser only sends the cookie over HTTPS, which is correct
# behind an HTTPS reverse proxy or Cloudflare Tunnel.
# When False (default) the cookie is also sent over plain http — necessary
# for local or LAN access without TLS (http://192.168.x.x:8787 etc.).
# ⚠️  Do NOT set to true unless you are accessing the cockpit over HTTPS;
# otherwise the browser silently drops the cookie and login appears broken.
_WEB_COOKIE_SECURE: bool = os.environ.get("WEB_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


# ─────────────────────────── auth ───────────────────────────
#
# Scheme: cookie cops_auth = hex(scrypt(password, salt=AUTH_SALT, n=2^14, r=8, p=1)).
# Salt — AUTH_SALT from env (first run → auto-generated and printed to stderr).
# Comparison — hmac.compare_digest (constant-time).
# Rate-limit: ≥5 failed attempts from one IP in 5 min → 429 for 5 min.

import hmac as _hmac

# Spec-012 Ph3: pattern for exact match of path /api/projects/{id}/incident.
# Pre-compiled once — used in auth_middleware for tight-exempt.
# Deliberately NOT endswith("/incident"): won't pass ../incident/evil or GET.
_INCIDENT_PATH_RE = re.compile(r"^/api/projects/[^/]+/incident$")

# Rate-limit for the push endpoint: at most _INCIDENT_PUSH_MAX calls per _INCIDENT_PUSH_WINDOW sec
# with a valid token, per-project. Prevents a storm of heal launches.
_INCIDENT_PUSH_MAX = 30
_INCIDENT_PUSH_WINDOW = 60  # seconds
_INCIDENT_IP_MAX = 300      # per-IP backstop (before project resolution/secret read — against unauth flood)
_incident_ip_history: dict[str, list[float]] = {}
# {project_id: [timestamp, ...]} — history of successful calls
_incident_push_history: dict[str, list[float]] = {}

# scrypt salt: taken from env WEB_COOKIE_SALT or auto-generated at startup.
AUTH_SALT: bytes = os.environ.get("WEB_COOKIE_SALT", "").encode() or (
    lambda s: (print(f"[auth] generated WEB_COOKIE_SALT={s} — add to .env", flush=True), s.encode())[1]
)(secrets.token_hex(16))


def _derive_token(password: str) -> str:
    """Derives the cookie token via scrypt (stdlib, no new dependencies)."""
    dk = hashlib.scrypt(
        password.encode(),
        salt=AUTH_SALT,
        n=1 << 14,  # 16384 — balance of speed and security (< 100ms on server)
        r=8,
        p=1,
        dklen=32,
    )
    return dk.hex()


# Backwards-compatibility alias for middleware (used in tests)
def _make_token(password: str) -> str:
    return _derive_token(password)


# Rate-limit: {ip: [(timestamp, ok:bool), ...]}
_login_attempts: dict[str, list[tuple[float, bool]]] = {}
_LOGIN_WINDOW = 300   # 5 minutes
_LOGIN_MAX_FAIL = 5   # max failed attempts
_RETRY_AFTER_BASE = 30   # seconds for first throttle response
_RETRY_AFTER_CAP = 900   # maximum back-off cap (15 min)


def _peer_is_trusted_proxy(remote: str) -> bool:
    """True if the direct socket peer is in TRUSTED_PROXIES (CSV of IPs/CIDRs).

    Empty/unset TRUSTED_PROXIES → no peer is trusted, so forwarding headers are
    ignored (prevents a direct client from spoofing CF-Connecting-IP / XFF to
    evade the rate limiter). Set this to your reverse proxy's address(es) when
    deploying behind Cloudflare / Traefik / nginx.
    """
    raw = os.environ.get("TRUSTED_PROXIES", "").strip()
    if not raw or not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if ip in ipaddress.ip_network(part, strict=False):
                return True
        except (ValueError, TypeError):
            # malformed entry, or IPv4/IPv6 version mismatch — skip
            continue
    return False


def _client_ip(req) -> str:
    """Extract the real client IP from a request.

    Forwarding headers (CF-Connecting-IP → first X-Forwarded-For entry) are
    honoured ONLY when the direct peer (req.remote) is a configured trusted
    proxy; otherwise the socket peer is used. Falls back to "unknown".
    """
    remote = req.remote or "unknown"
    if _peer_is_trusted_proxy(remote):
        cf_ip = req.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip
        xff = req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if xff:
            return xff
    return remote


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """Check whether this IP has exceeded the failed-attempt threshold.

    Returns (blocked, retry_after_seconds).  retry_after grows with consecutive
    failures so a sustained flood backs off, but different IPs are independent.
    Successful logins are NOT counted toward the failure budget.
    """
    now = time.monotonic()
    attempts = _login_attempts.get(ip, [])
    # Keep only records within the window; ignore successes for the failure count
    attempts = [(t, ok) for t, ok in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    fails = sum(1 for _, ok in attempts if not ok)
    if fails < _LOGIN_MAX_FAIL:
        return False, 0
    # Compute a growing delay: base * 2^(excess-1), capped
    excess = fails - _LOGIN_MAX_FAIL  # 0 on first threshold hit, grows with more fails
    delay = min(_RETRY_AFTER_BASE * (2 ** excess), _RETRY_AFTER_CAP)
    return True, int(delay)


def _record_attempt(ip: str, success: bool) -> None:
    """Record a login attempt.  Successes reset the failure window for this IP."""
    now = time.monotonic()
    if success:
        # A successful login clears prior failures so the operator is never locked out
        # by their own previous mistakes once they authenticate correctly.
        _login_attempts[ip] = [(now, True)]
        return
    bucket = _login_attempts.setdefault(ip, [])
    bucket.append((now, False))
    # Cap growth (single-IP attack)
    if len(bucket) > 200:
        _login_attempts[ip] = bucket[-200:]


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Add conservative HTTP security headers to every response.

    Deliberately omits Content-Security-Policy because the SPA loads inline
    scripts / dynamic imports that a strict CSP would break.
    """
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Outer middleware: logs unhandled exceptions and returns JSON 500."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ConnectionResetError, ConnectionAbortedError):
        # Client closed the connection (typical for SSE/long-poll: closed tab, tunnel dropped).
        # This is NOT an incident. The response may have already started streaming → json_response
        # is impossible. Re-raise (aiohttp will clean up the transport; CancelledError is a
        # BaseException and passes through on its own).
        raise
    except Exception as exc:
        request_id = _uuid.uuid4().hex[:8]
        logging.exception("UNHANDLED exc_class=%s path=%s request_id=%s", type(exc).__name__, request.path, request_id)
        # Spec-012 Ph1: cockpit's own error → in-process card, immediately (no round-trip
        # through the log scanner). Fire-and-forget; hash dedup prevents the scanner from doubling it.
        try:
            _spawn_bg(_report_incident(request.app["ctx"], type(exc).__name__, request.path))
        except Exception:
            pass
        return web.json_response(
            {"error": type(exc).__name__, "request_id": request_id},
            status=500,
        )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Guards /api/* — passes /api/health and /api/login without a cookie.
    Spec-012 Ph3: also passes POST /api/projects/{id}/incident (it has its own
    token-auth in the body/header). Match is TIGHT via pre-compiled _INCIDENT_PATH_RE —
    endpoints without trailing id, /incident/evil, or GET will not be exempt."""
    path = request.path
    # Unprotected endpoints
    if path in ("/api/health", "/api/login"):
        return await handler(request)
    # Spec-012 Ph3: push-incident — its own auth (token). POST only, exact path only.
    if request.method == "POST" and _INCIDENT_PATH_RE.match(path):
        return await handler(request)
    # Only /api/* paths are checked
    if path.startswith("/api/"):
        password = request.app["ctx"]["password"]
        expected = request.app["ctx"]["_auth_token"]
        token = request.cookies.get("cops_auth", "")
        if not _hmac.compare_digest(token, expected):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# ─────────────────────────── git helpers ───────────────────────────

async def _git_cmd(cwd: str, *args, timeout: float = 3.0):
    """Runs a git command in cwd, returns stdout or None on error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode == 0:
                return stdout.decode().strip()
            return None
        except asyncio.TimeoutError:
            proc.kill()
            return None
    except Exception:
        return None


async def _git_info(cwd: str) -> dict | None:
    """Returns {branch, dirty, unpushed} or None if not a git repo."""
    branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return None

    status_out = await _git_cmd(cwd, "status", "--porcelain") or ""
    dirty = len([l for l in status_out.splitlines() if l.strip()])

    unpushed_out = await _git_cmd(cwd, "rev-list", "@{u}..", "--count")
    try:
        unpushed = int(unpushed_out) if unpushed_out is not None else 0
    except ValueError:
        unpushed = 0

    return {
        "branch": branch, "dirty": dirty, "unpushed": unpushed,
        "visibility": _git_visibility_cached(cwd),
    }


# ── GitHub visibility (private/public) — cache + background gh to NOT block polling ──
_GIT_VIS_CACHE: "dict[str, tuple[str | None, float]]" = {}   # cwd → (visibility, ts)
_GIT_VIS_TTL = 3600.0   # repo visibility changes rarely → cache for one hour


async def _git_visibility_refresh(cwd: str) -> None:
    """Fetches private/public via gh and stores in cache. Network call → background only.
    Swallows everything (no remote / not on GitHub / gh not authorised → None)."""
    vis: "str | None" = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "view", "--json", "visibility", "-q", ".visibility",
            cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode == 0:
            v = out.decode(errors="replace").strip().lower()
            if v in ("private", "public"):
                vis = v
    except Exception:
        vis = None
    _GIT_VIS_CACHE[cwd] = (vis, time.time())


def _git_visibility_cached(cwd: str) -> "str | None":
    """Visibility cache; on miss/expiry — background refresh, returns current value (stale/None)
    without blocking polling. Called from an async context (needs a running loop for _spawn_bg)."""
    entry = _GIT_VIS_CACHE.get(cwd)
    if entry is None or (time.time() - entry[1]) > _GIT_VIS_TTL:
        try:
            _spawn_bg(_git_visibility_refresh(cwd))
        except Exception:
            pass
    return entry[0] if entry else None


# ─────────────────────────── project helpers ───────────────────────────

def _project_id(cwd: str) -> str:
    """Project id = basename of cwd without trailing /."""
    return Path(cwd.rstrip("/")).name


def _session_labels_path(ctx: dict) -> Path:
    return ctx["DATA"] / "session_labels.json"


def _load_session_labels(ctx: dict) -> dict:
    """{session_id → user_label}. The SDK has no label support — this is our layer."""
    p = _session_labels_path(ctx)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session_labels(ctx: dict, data: dict) -> None:
    _session_labels_path(ctx).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _inherit_label_from_free_chat(ctx: dict, session_key: str, sid: str) -> None:
    """If session_key is a free-chat with a label and sid has no label yet —
    inherits the tab label. Called when the SDK assigns a session_id for the first time."""
    if not (session_key and session_key.startswith("free-") and sid):
        return
    free = _load_free_chats(ctx)
    entry = free.get(session_key)
    if not entry or not entry.get("label"):
        return
    labels = _load_session_labels(ctx)
    if sid in labels:
        return  # already labelled (manual rename) — leave it alone
    labels[sid] = entry["label"]
    _save_session_labels(ctx, labels)


def _free_chats_path(ctx: dict) -> Path:
    return ctx["DATA"] / "free_chats.json"


# ── Prompt templates ──────────────────────────────────────────────────────────

# Adapted from addyosmani/agent-skills (MIT)
# https://github.com/addyosmani/agent-skills/blob/main/LICENSE
#
# These are the built-in default prompt templates shipped with every ClaudeOps
# instance. They are seeded into data/prompts.json on first startup (or when a
# template with the matching slug_id is absent). Operators can delete or edit any
# template; deleted defaults are recorded in the file and never re-inserted.
#
# Merge rules (enforced by _seed_default_prompts):
#   1. Load current prompts.json (may be empty / missing).
#   2. Collect slugs of every entry whose "slug_id" field matches a default slug.
#   3. Collect slugs listed in the top-level "__deleted_defaults" array (operator-
#      removed; persisted so restarts never resurface them).
#   4. Insert only defaults whose slug_id is absent from both sets.
#   5. Save atomically; never modify or remove existing operator entries.
DEFAULT_PROMPT_TEMPLATES: list[dict] = [
    {
        "slug_id": "spec-writer",
        "id": "default-spec-writer",
        "title": "Spec writer",
        "category": "Define",
        "text": (
            "You are a spec-writer. Before drafting anything, surface your assumptions explicitly:\n\n"
            "ASSUMPTIONS I'M MAKING:\n"
            "1. [tech stack / environment]\n"
            "2. [auth model / data layer]\n"
            "3. [scope boundary]\n"
            "→ Correct me now or I'll proceed with these.\n\n"
            "Then write a spec covering: Objective (who/why/success), Commands (exact CLI flags),\n"
            "Architecture (component boundaries), Data model (key entities), API surface (endpoints/\n"
            "contracts), Task breakdown (ordered, verifiable). Each section max 10 lines unless depth\n"
            "is required. Output to specs/spec-NNN-<name>.md."
        ),
    },
    {
        "slug_id": "debug-triage",
        "id": "default-debug-triage",
        "title": "Debug triage",
        "category": "Verify",
        "text": (
            "Something broke. Stop-the-Line protocol:\n"
            "1. STOP adding features or making other changes.\n"
            "2. PRESERVE evidence: paste exact error, last working commit, env.\n"
            "3. REPRODUCE: make the failure happen reliably. If not reproducible → document and monitor.\n"
            "4. ISOLATE: binary-search the change set. Smallest reproducer.\n"
            "5. ROOT CAUSE: don't fix symptoms. Find why, not what.\n"
            "6. FIX: targeted, minimal change. Add a regression test.\n"
            "7. VERIFY: run tests. Confirm fix in the same environment the bug appeared.\n"
            "8. RESUME only after step 7 passes."
        ),
    },
    {
        "slug_id": "pre-deploy-gate",
        "id": "default-pre-deploy-gate",
        "title": "Pre-deploy gate",
        "category": "Ship",
        "text": (
            "Pre-deploy gate — confirm each before deploying:\n"
            "CODE:     [ ] tests pass  [ ] build clean  [ ] lint/types pass  [ ] no debug console.log\n"
            "SECURITY: [ ] no secrets in code/git  [ ] npm audit no criticals  [ ] input validation\n"
            "INFRA:    [ ] env vars set  [ ] migration ran  [ ] rollback plan exists\n"
            "MONITOR:  [ ] error rate baseline noted  [ ] rollback trigger defined (error % or latency)\n\n"
            "If any box is unchecked → stop and report. Do not deploy."
        ),
    },
]

_DEFAULT_SLUGS: set[str] = {t["slug_id"] for t in DEFAULT_PROMPT_TEMPLATES}


def _seed_default_prompts(ctx: dict) -> None:
    """Insert missing default prompt templates into prompts.json.

    Safe to call on every startup:
    - Existing operator entries (any id / slug_id) are never modified.
    - Defaults that the operator deleted are listed in __deleted_defaults and
      never re-inserted.
    - Defaults already present (matched by slug_id) are skipped.
    - No duplicate slug_ids are ever created.
    """
    p = _prompts_path(ctx)
    try:
        raw: dict | list = json.loads(p.read_text()) if p.exists() else []
    except Exception:
        raw = []

    # Support both old (plain list) and new (dict with __deleted_defaults) formats
    if isinstance(raw, dict):
        prompts: list = raw.get("prompts", [])
        deleted_defaults: list = raw.get("__deleted_defaults", [])
    else:
        prompts = raw
        deleted_defaults = []

    existing_slugs: set[str] = {e.get("slug_id", "") for e in prompts}
    skip_slugs: set[str] = existing_slugs | set(deleted_defaults)

    to_add = [t for t in DEFAULT_PROMPT_TEMPLATES if t["slug_id"] not in skip_slugs]
    if not to_add:
        return

    prompts = to_add + prompts  # defaults first for discoverability

    payload: dict | list
    if deleted_defaults:
        payload = {"__deleted_defaults": deleted_defaults, "prompts": prompts}
    else:
        payload = prompts

    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _prompts_path(ctx: dict) -> Path:
    return ctx["DATA"] / "prompts.json"

def _load_prompts(ctx: dict) -> list:
    """Return the prompts list from prompts.json.

    The file may be a plain JSON array (legacy) or a dict with keys
    ``prompts`` and ``__deleted_defaults`` (current format written by
    _seed_default_prompts / _save_prompts when deleted_defaults exist).
    Always returns a plain list so callers are unaffected.
    """
    p = _prompts_path(ctx)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            return raw.get("prompts", [])
        return raw
    except Exception:
        return []


def _load_prompts_raw(ctx: dict) -> tuple[list, list]:
    """Return (prompts_list, deleted_defaults_list) from prompts.json.

    Internal helper for operations that must preserve __deleted_defaults.
    """
    p = _prompts_path(ctx)
    if not p.exists():
        return [], []
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            return raw.get("prompts", []), raw.get("__deleted_defaults", [])
        return raw, []
    except Exception:
        return [], []


def _save_prompts(ctx: dict, prompts: list, deleted_defaults: list | None = None) -> None:
    """Persist prompts to prompts.json.

    If deleted_defaults is provided (even an empty list) the file is written in
    dict format so the __deleted_defaults list survives round-trips. Otherwise
    uses the legacy plain-list format for backward compatibility with instances
    that have never had a default deleted.
    """
    if deleted_defaults is not None:
        payload: dict | list = {"__deleted_defaults": deleted_defaults, "prompts": prompts}
    else:
        payload = prompts
    _prompts_path(ctx).write_text(json.dumps(payload, ensure_ascii=False, indent=2))


async def api_prompts_list(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    return web.json_response({"prompts": _load_prompts(ctx)})

async def api_prompt_create(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    title = (data.get("title") or "").strip()
    text  = (data.get("text")  or "").strip()
    if not title or not text:
        raise web.HTTPBadRequest(text="title and text required")
    category = (data.get("category") or "").strip() or None
    prompt = {"id": str(_uuid.uuid4())[:8], "title": title, "text": text, **({"category": category} if category else {})}
    prompts, deleted_defaults = _load_prompts_raw(ctx)
    prompts.append(prompt)
    _save_prompts(ctx, prompts, deleted_defaults if deleted_defaults else None)
    return web.json_response({"prompt": prompt})

async def api_prompt_delete(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    prompts, deleted_defaults = _load_prompts_raw(ctx)
    # If the deleted entry is a default, record its slug_id so it is not re-seeded.
    for entry in prompts:
        if entry.get("id") == pid:
            slug = entry.get("slug_id", "")
            if slug in _DEFAULT_SLUGS and slug not in deleted_defaults:
                deleted_defaults.append(slug)
            break
    prompts = [p for p in prompts if p.get("id") != pid]
    _save_prompts(ctx, prompts, deleted_defaults if deleted_defaults else None)
    return web.json_response({"ok": True})

async def api_prompt_update(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    prompts, deleted_defaults = _load_prompts_raw(ctx)
    for p in prompts:
        if p.get("id") == pid:
            if "title" in data: p["title"] = (data["title"] or "").strip()
            if "text" in data: p["text"] = (data["text"] or "").strip()
            if "category" in data:
                cat = (data.get("category") or "").strip() or None
                if cat: p["category"] = cat
                else: p.pop("category", None)
            _save_prompts(ctx, prompts, deleted_defaults if deleted_defaults else None)
            return web.json_response({"prompt": p})
    return web.json_response({"error": "not found"}, status=404)


def _load_free_chats(ctx: dict) -> dict:
    """{free_id → {label, cwd, model, created_at}}. File may be absent — returns {}."""
    p = _free_chats_path(ctx)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_free_chats(ctx: dict, data: dict) -> None:
    p = _free_chats_path(ctx)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


_TOPICS_MTIME: "float | None" = None  # mtime of the last loaded version of topics.json


def _maybe_reload_topics(ctx: dict) -> None:
    """Hot-reload topics.json from disk when edited externally (without restarting the process).

    Why: `topics` is loaded once at bot startup (bot.py) and lives as an
    in-memory dict in ctx["topics"]. A direct Edit/Write by an agent from the cockpit
    bypassed that dict → the change was invisible until restart. The disk is authoritative —
    bot runtime commands always call save_topics() — so reading from disk is safe.
    We update the dict IN-PLACE (clear+update) so both the bot and cockpit share
    the same object. mtime gate: only re-parse when the file changes.
    A corrupted/partially-written file (race with save_topics) → JSONDecodeError →
    silently keep the current version and retry on the next request."""
    global _TOPICS_MTIME
    try:
        path = ctx["DATA"] / "topics.json"
        mtime = path.stat().st_mtime
    except OSError:
        return
    if _TOPICS_MTIME is not None and mtime == _TOPICS_MTIME:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        ctx["topics"].clear()
        ctx["topics"].update(data)
        _TOPICS_MTIME = mtime


def _collect_projects(ctx: dict) -> list[dict]:
    """Deduplicates by cwd, builds a project list from ctx["topics"].
    Appends free-chats as virtual projects (id=free-<uuid>, session_key=its own id).
    Archived project ids are excluded from the result."""
    _maybe_reload_topics(ctx)
    archived = _load_archived(ctx)
    groups_data = _load_groups(ctx)
    assignments = groups_data["assignments"]
    valid_groups = set(groups_data["groups"])
    # Spec-031: load favorites set once
    fav_data = _load_favorites(ctx)
    fav_set = set(fav_data.get("favorites", []))
    seen: set[str] = set()
    out = []
    for key, b in ctx["topics"].items():
        cwd = b.get("cwd", "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        pid = _project_id(cwd)
        if pid in archived:
            continue
        raw_group = assignments.get(pid)
        # session_key — string key (session identifier for this project)
        out.append({
            "id": pid,
            "name": b.get("project", pid),
            "cwd": cwd,
            "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
            "session_key": key,
            "is_free": False,
            "log_cmd": b.get("log_cmd"),
            "test_cmd": b.get("test_cmd"),
            "notify_on_error": bool(b.get("notify_on_error", False)),
            "git_enabled": b.get("git_enabled", True) is not False,
            "agents_config": b.get("agents_config") or {},
            "group": raw_group if raw_group in valid_groups else None,
            "favorite": pid in fav_set,
        })
    out.sort(key=lambda x: x["name"].lower())

    # Free chats — separate section, sorted by creation time
    free = _load_free_chats(ctx)
    free_items = sorted(free.items(), key=lambda kv: kv[1].get("created_at", 0))
    for fid, b in free_items:
        # Spec-031: read group assignment for free chats (previously hardcoded None)
        raw_free_group = assignments.get(fid)
        out.append({
            "id": fid,
            "name": b.get("label", fid),
            "cwd": b.get("cwd", str(Path.home())),
            "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
            "session_key": fid,  # session_key for free = its own id (string with free- prefix)
            "is_free": True,
            "group": raw_free_group if raw_free_group in valid_groups else None,
            "favorite": fid in fav_set,
        })
    return out


def _find_project_by_id(ctx: dict, pid: str) -> dict | None:
    """Finds a project by id (basename of cwd)."""
    for p in _collect_projects(ctx):
        if p["id"] == pid:
            return p
    return None


# ─────────────────────────── archive store ───────────────────────────────────

def _archived_path(ctx: dict) -> Path:
    return ctx["DATA"] / "archived.json"

def _load_archived(ctx: dict) -> set:
    """Returns a set of archived project ids."""
    p = _archived_path(ctx)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()

def _save_archived(ctx: dict, archived: set) -> None:
    _archived_path(ctx).write_text(
        json.dumps(sorted(archived), ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ─────────────────────────── groups store ────────────────────────────────────

def _groups_path(ctx: dict) -> Path:
    return ctx["DATA"] / "project_groups.json"

def _load_groups(ctx: dict) -> dict:
    """Returns {groups:[...], assignments:{...}}."""
    p = _groups_path(ctx)
    if not p.exists():
        return {"groups": [], "assignments": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"groups": [], "assignments": {}}
        return {
            "groups": data.get("groups", []) if isinstance(data.get("groups"), list) else [],
            "assignments": data.get("assignments", {}) if isinstance(data.get("assignments"), dict) else {},
        }
    except Exception:
        return {"groups": [], "assignments": {}}

def _save_groups(ctx: dict, data: dict) -> None:
    _groups_path(ctx).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────── favorites store (Spec-031) ──────────────────────

def _favorites_path(ctx: dict) -> Path:
    return ctx["DATA"] / "project_favorites.json"


def _load_favorites(ctx: dict) -> dict:
    """Returns {favorites: [pid, ...]}. Defaults to empty on missing/corrupt file."""
    p = _favorites_path(ctx)
    if not p.exists():
        return {"favorites": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"favorites": []}
        favs = data.get("favorites", [])
        if not isinstance(favs, list):
            return {"favorites": []}
        return {"favorites": favs}
    except Exception:
        return {"favorites": []}


def _save_favorites(ctx: dict, data: dict) -> None:
    _favorites_path(ctx).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────── trash store (Spec-025) ─────────────────────────

TRASH_RETENTION_DAYS = int(os.environ.get("TRASH_RETENTION_DAYS", "7"))


def _trash_dir(ctx: dict) -> Path:
    d = ctx["DATA"] / "trash"
    d.mkdir(exist_ok=True)
    return d


def _path_allowlist_check(cwd: str, ctx: dict, _home_override: str | None = None) -> str | None:
    """Returns an error string if cwd fails the path allowlist, else None (OK).

    Rules (ALL must hold, else reject):
    1. real is strictly under home (startswith home+sep) and real != home
    2. real is not the bot dir (HERE) and not an ancestor of HERE
    3. No symlink escape (realpath vs raw path stays under home)
    """
    try:
        real = os.path.realpath(cwd)
        home = os.path.realpath(_home_override if _home_override else os.path.expanduser("~"))
        here = str(ctx.get("HERE", ""))
        here_real = os.path.realpath(here) if here else None

        # Rule 1: strictly under home
        if not real.startswith(home + os.sep) or real == home:
            return "cwd is not strictly under home directory"

        # Rule 2: not the bot dir, not an ancestor of bot dir
        if here_real:
            if real == here_real:
                return "cwd is the claude-ops-bot directory"
            if here_real.startswith(real + os.sep):
                return "cwd is an ancestor of the claude-ops-bot directory"

        # Rule 3: symlink escape check — raw path (without realpath) must also start under home
        raw_abs = os.path.abspath(cwd)
        if not raw_abs.startswith(home + os.sep):
            return "cwd resolves outside home via symlink"

        return None  # all checks passed
    except Exception as e:
        return f"path resolution error: {e}"


def _run_janitor_trash_purge(ctx: dict) -> list:
    """Purge trash entries older than TRASH_RETENTION_DAYS.
    Returns list of purged entry names.
    CRITICAL: only ever rm's paths strictly under data/trash/."""
    import time as _time
    trash_dir = _trash_dir(ctx)
    trash_real = trash_dir.resolve()
    purged = []
    now = _time.time()

    for sidecar in list(trash_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            deleted_at_str = data.get("deleted_at", "")
            deleted_ts = _time.mktime(_time.strptime(deleted_at_str, "%Y-%m-%dT%H:%M:%SZ"))
            age_days = (now - deleted_ts) / 86400
            if age_days < TRASH_RETENTION_DAYS:
                continue

            entry = sidecar.stem
            folder = trash_dir / entry

            # CRITICAL path guard: folder must be strictly under data/trash/
            if folder.exists():
                folder_real = folder.resolve()
                if not str(folder_real).startswith(str(trash_real) + os.sep):
                    print(f"[janitor-purge] REFUSED: {folder} is not under data/trash")
                    continue
                shutil.rmtree(str(folder_real))

            sidecar.unlink(missing_ok=True)
            purged.append(entry)
            print(f"[janitor-purge] purged: {entry}")
        except Exception as e:
            print(f"[janitor-purge] WARNING: error purging {sidecar}: {e}")

    return purged


async def _janitor_trash_purge_loop(ctx: dict) -> None:
    """Periodically purge trash entries older than TRASH_RETENTION_DAYS.
    This is the ONLY place in the entire system that calls rm -rf,
    and only on paths strictly under data/trash/."""
    _PURGE_INTERVAL_SEC = 3600  # check every hour
    while True:
        await asyncio.sleep(_PURGE_INTERVAL_SEC)
        try:
            _run_janitor_trash_purge(ctx)
        except Exception as e:
            print(f"[janitor-purge] ERROR: {e}")


def _find_project_by_id_any(ctx: dict, pid: str) -> dict | None:
    """Finds a project by id WITHOUT filtering archived ones.
    Searches topics and free_chats directly."""
    _maybe_reload_topics(ctx)
    seen: set[str] = set()
    for key, b in ctx["topics"].items():
        cwd = b.get("cwd", "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        if _project_id(cwd) == pid:
            return {
                "id": pid,
                "name": b.get("project", pid),
                "cwd": cwd,
                "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
                "session_key": key,
                "is_free": False,
            }
    # Also check free chats
    free = _load_free_chats(ctx)
    if pid in free:
        b = free[pid]
        return {
            "id": pid,
            "name": b.get("label", pid),
            "cwd": b.get("cwd", str(Path.home())),
            "model": b.get("model", ctx.get("DEFAULT_MODEL", "sonnet")),
            "session_key": pid,
            "is_free": True,
        }
    return None


def _find_vault_specs_dir(ctx: dict, project_name: str, cwd: str) -> Path | None:
    """Tries several name variants for a folder in VAULT_PROJECTS.
    If VAULT_PROJECTS is not set (None) — returns None (feature disabled)."""
    vault: Optional[Path] = ctx.get("VAULT_PROJECTS")
    if not vault or not vault.is_dir():
        return None
    candidates = [
        project_name,
        project_name.lower(),
        Path(cwd).name,
        Path(cwd).name.lower(),
    ]
    # Case-insensitive scan of existing folders
    try:
        existing = {d.name: d for d in vault.iterdir() if d.is_dir()}
    except Exception:
        return None
    for c in candidates:
        if c in existing:
            return existing[c]
        # case-insensitive
        cl = c.lower()
        for name, path in existing.items():
            if name.lower() == cl:
                return path
    return None


# ─────────────────────────── API handlers ───────────────────────────

async def api_health(req: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def api_login(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    # Rate-limit by real client IP (respects CF-Connecting-IP / X-Forwarded-For)
    ip = _client_ip(req)
    blocked, retry_after = _check_rate_limit(ip)
    if blocked:
        resp = web.json_response({"error": "too many attempts, try later"}, status=429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    try:
        body = await req.json()
        password = body.get("password", "")
    except Exception:
        _record_attempt(ip, False)
        return web.json_response({"error": "bad request"}, status=400)
    if not _hmac.compare_digest(password, ctx["password"]):
        _record_attempt(ip, False)
        return web.json_response({"error": "bad password"}, status=401)

    # Spec-026 Phase 2: TOTP second factor.
    # SAFETY: only required when an ACTIVE secret is already enrolled.
    # Until the operator enrolls and activates 2FA this block is a no-op —
    # deploying this code cannot lock anyone out.
    try:
        active_secret = _secretstore.get("__totp_secret__")
    except Exception:
        active_secret = None

    if active_secret:
        # 2FA is active — require a TOTP code (or a recovery code)
        totp_code = str(body.get("totp", "")).strip()
        if not totp_code:
            # Wrong TOTP counts as a failed attempt (keeps rate-limiter honoured)
            _record_attempt(ip, False)
            return web.json_response({"error": "totp_required"}, status=401)

        # Try TOTP first (with replay protection — a code can't be reused in-window)
        if _totp.verify_no_replay(active_secret, totp_code):
            pass  # valid — fall through to issue cookie
        else:
            # Try recovery code
            try:
                hashes_json = _secretstore.get("__totp_recovery__")
                hashes = json.loads(hashes_json) if hashes_json else []
            except Exception:
                hashes = []

            ok, remaining = _totp.verify_and_consume(totp_code, hashes)
            if ok:
                # Consumed one recovery hash — persist the updated list
                try:
                    _secretstore.set(
                        "__totp_recovery__",
                        json.dumps(remaining),
                        category="totp",
                    )
                except Exception:
                    pass  # persist best-effort; still grant login
            else:
                _record_attempt(ip, False)
                return web.json_response({"error": "totp_invalid"}, status=401)

    _record_attempt(ip, True)
    token = ctx["_auth_token"]
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        "cops_auth", token,
        httponly=True,
        secure=_WEB_COOKIE_SECURE,
        path="/",
        max_age=COOKIE_MAX_AGE,
        samesite="Lax",
    )
    return resp


async def api_logout(req: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie("cops_auth", path="/")
    return resp


async def api_me(req: web.Request) -> web.Response:
    return web.json_response({"authed": True})


async def api_projects(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    projects = _collect_projects(ctx)

    def _count_incidents(cwd: str) -> int:
        try:
            _, _, cols = _load_board(cwd)
        except Exception:
            return 0
        return sum(1 for col_cards in cols.values() for c in col_cards if _is_incident_card(c))

    def _activity_state(p: dict) -> dict:
        """Returns running/awaiting state for a project (O(1) dict lookup)."""
        sk = p.get("session_key") or p.get("tg_thread", "")
        running = ctx["running"].get(sk) is not None
        finished_ts = _awaiting.get(sk, 0.0)
        seen_ts = _seen.get(sk, 0.0)
        awaiting = finished_ts > 0 and finished_ts > seen_ts
        return {"running": running, "awaiting": awaiting}

    async def enrich(p: dict) -> dict:
        # For free chats git checks are meaningless (cwd is usually $HOME, not a project repo)
        if p.get("is_free"):
            return {**p, "health": {"git": None}, "incidents": 0, **_activity_state(p)}
        # git disabled by project setting — don't show git status
        if not _git_enabled(p):
            return {**p, "health": {"git": None}, "incidents": _count_incidents(p["cwd"]), **_activity_state(p)}
        try:
            git = await _git_info(p["cwd"])
        except Exception:
            git = None
        return {**p, "health": {"git": git}, "incidents": _count_incidents(p["cwd"]), **_activity_state(p)}

    try:
        enriched = await asyncio.gather(*[enrich(p) for p in projects])
    except Exception:
        enriched = [{**p, "health": {"git": None}} for p in projects]

    return web.json_response({"projects": list(enriched)})


async def api_project_archive(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id_any(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if project.get("is_free"):
        return web.json_response({"error": "free chats cannot be archived"}, status=400)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "project busy"}, status=409)
    archived = _load_archived(ctx)
    archived.add(pid)
    _save_archived(ctx, archived)
    return web.json_response({"archived": True})


async def api_project_unarchive(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id_any(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    archived = _load_archived(ctx)
    if pid not in archived:
        return web.json_response({"error": "project not archived"}, status=400)
    archived.discard(pid)
    _save_archived(ctx, archived)
    return web.json_response({"archived": False})


# ─────────────────────────── Spec-025: Project Delete ────────────────────────

async def api_project_delete_precheck(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/delete-precheck
    Returns git status for the delete confirmation modal."""
    import subprocess
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id_any(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = project["cwd"]
    result: dict = {
        "is_git": False,
        "uncommitted_count": 0,
        "unpushed_count": 0,
        "branch": None,
        "has_remote": False,
    }
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            result["is_git"] = True
            # Branch
            rb = subprocess.run(
                ["git", "-C", cwd, "branch", "--show-current"],
                capture_output=True, text=True, timeout=5,
            )
            result["branch"] = rb.stdout.strip() or None
            # Uncommitted (staged + unstaged)
            rs = subprocess.run(
                ["git", "-C", cwd, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            result["uncommitted_count"] = len([l for l in rs.stdout.splitlines() if l.strip()])
            # Remote
            rr = subprocess.run(
                ["git", "-C", cwd, "remote"],
                capture_output=True, text=True, timeout=5,
            )
            result["has_remote"] = bool(rr.stdout.strip())
            # Unpushed commits
            if result["has_remote"] and result["branch"]:
                ru = subprocess.run(
                    ["git", "-C", cwd, "rev-list", "--count", f"origin/{result['branch']}..HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if ru.returncode == 0:
                    try:
                        result["unpushed_count"] = int(ru.stdout.strip())
                    except ValueError:
                        pass
    except Exception:
        pass
    return web.json_response(result)


async def api_project_delete(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/delete  body: {confirm_name}
    Moves the project cwd to data/trash/<id>-<ts>/ with a sidecar JSON.
    Cleans up cockpit state. Deletes TG topic. Writes audit line.
    NEVER rm's anything — only shutil.move."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]

    # Parse body
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    confirm_name = body.get("confirm_name", "")

    # Guardrail 1: project must exist (archived or not)
    project = _find_project_by_id_any(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Guardrail 1b: must be archived
    archived = _load_archived(ctx)
    if pid not in archived:
        return web.json_response({"error": "only archived projects can be deleted"}, status=409)

    # Guardrail 2: confirm_name must match
    project_name = project["name"]
    if confirm_name != project_name:
        return web.json_response({"error": "confirm_name does not match project name"}, status=400)

    cwd = project["cwd"]
    _session_key_del = project.get("session_key") or project.get("tg_thread", "")

    # Guardrail 3: path allowlist
    allowlist_err = _path_allowlist_check(cwd, ctx)
    if allowlist_err:
        return web.json_response({"error": f"path rejected: {allowlist_err}"}, status=400)

    # Guardrail 4: not busy — check running dict by session key
    if ctx["running"].get(_session_key_del) is not None:
        return web.json_response({"error": "project is busy"}, status=409)
    # Also check if any running session maps to this cwd
    for sk, val in list(ctx["running"].items()):
        if val is not None:
            topic_data = ctx["topics"].get(sk)
            if topic_data and topic_data.get("cwd") == cwd:
                return web.json_response({"error": "project is busy"}, status=409)

    # All guardrails passed — proceed with move
    ts = int(time.time())
    trash_name = f"{pid}-{ts}"
    trash_dir = _trash_dir(ctx)
    trash_dest = trash_dir / trash_name
    sidecar_path = trash_dir / f"{trash_name}.json"

    # Parse TG topic info
    tg_chat_id = None
    tg_thread_id = None
    if _session_key_del and ":" in str(_session_key_del):
        parts = str(_session_key_del).split(":", 1)
        try:
            tg_chat_id = int(parts[0])
            tg_thread_id = int(parts[1])
        except (ValueError, IndexError):
            pass

    # Step 1: Check cwd exists
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        return web.json_response({"error": "project directory does not exist"}, status=400)

    # Step 2: Move cwd to trash
    shutil.move(str(cwd_path), str(trash_dest))

    # Write sidecar
    sidecar = {
        "id": pid,
        "name": project_name,
        "original_cwd": cwd,
        "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "tg_chat": tg_chat_id,
        "tg_thread": tg_thread_id,
    }
    try:
        sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[delete] WARNING: could not write sidecar: {e}")

    # Step 3: Clean cockpit state (each in try/except — partial failure must not abort)
    # topics.json — remove bindings for this cwd
    try:
        _maybe_reload_topics(ctx)
        keys_to_remove = [k for k, b in ctx["topics"].items() if b.get("cwd") == cwd]
        for k in keys_to_remove:
            del ctx["topics"][k]
        ctx["save_topics"]()
    except Exception as e:
        print(f"[delete] WARNING: topics cleanup failed: {e}")

    # sessions.json
    try:
        sessions_to_remove = [k for k in list(ctx["sessions"].keys()) if k == _session_key_del]
        for k in sessions_to_remove:
            del ctx["sessions"][k]
        ctx["save_sessions"]()
    except Exception as e:
        print(f"[delete] WARNING: sessions cleanup failed: {e}")

    # spec-039: evict any live client for this session key
    try:
        _evict_fn = ctx.get("evict_live_client")
        if _evict_fn is not None and _session_key_del:
            await _evict_fn(_session_key_del, ctx)
    except Exception as e:
        print(f"[delete] WARNING: live-client eviction failed: {e}")

    # archived.json
    try:
        ar = _load_archived(ctx)
        ar.discard(pid)
        _save_archived(ctx, ar)
    except Exception as e:
        print(f"[delete] WARNING: archived cleanup failed: {e}")

    # project_groups.json
    try:
        groups_data = _load_groups(ctx)
        groups_data["assignments"].pop(pid, None)
        _save_groups(ctx, groups_data)
    except Exception as e:
        print(f"[delete] WARNING: groups cleanup failed: {e}")

    # timeline/<slug>.jsonl
    try:
        slug = _timeline_slug_from_cwd(cwd)
        timeline_file = ctx["DATA"] / "timeline" / f"{slug}.jsonl"
        if timeline_file.exists():
            timeline_file.unlink()
        rotated = ctx["DATA"] / "timeline" / f"{slug}.jsonl.1"
        if rotated.exists():
            rotated.unlink()
    except Exception as e:
        print(f"[delete] WARNING: timeline cleanup failed: {e}")

    # Step 4: Write audit line
    try:
        audit_dir = ctx["DATA"] / "audit"
        audit_dir.mkdir(exist_ok=True)
        ts_str = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(audit_dir / f"audit-{time.strftime('%Y-%m')}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts_str} [{project_name}] DELETE⚠️: id={pid} cwd={cwd} trash={trash_dest}\n")
    except Exception as e:
        print(f"[delete] WARNING: audit write failed: {e}")

    # Step 5 (last, irreversible): delete TG topic
    purge_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts + TRASH_RETENTION_DAYS * 86400))
    if tg_chat_id and tg_thread_id:
        try:
            ptb_app = ctx.get("ptb_app")
            if ptb_app:
                await ptb_app.bot.delete_forum_topic(
                    chat_id=tg_chat_id, message_thread_id=tg_thread_id
                )
        except Exception as e:
            print(f"[delete] WARNING: deleteForumTopic failed: {e}")

    return web.json_response({
        "deleted": True,
        "trash_path": str(trash_dest),
        "purge_at": purge_at,
    })


async def api_trash_list(req: web.Request) -> web.Response:
    """GET /api/trash — list trashed projects."""
    ctx = req.app["ctx"]
    trash_dir = _trash_dir(ctx)
    result = []
    now = time.time()
    for sidecar in sorted(trash_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            deleted_at_str = data.get("deleted_at", "")
            deleted_ts = time.mktime(time.strptime(deleted_at_str, "%Y-%m-%dT%H:%M:%SZ"))
            days_left = max(0, TRASH_RETENTION_DAYS - int((now - deleted_ts) / 86400))
            result.append({
                "entry": sidecar.stem,
                "id": data.get("id"),
                "name": data.get("name"),
                "original_cwd": data.get("original_cwd"),
                "deleted_at": deleted_at_str,
                "days_left": days_left,
            })
        except Exception:
            pass
    return web.json_response({"trash": result})


async def api_trash_restore(req: web.Request) -> web.Response:
    """POST /api/trash/{entry}/restore — move folder back, rebind topics.json.
    Note: TG topic is NOT restored (already deleted)."""
    ctx = req.app["ctx"]
    entry = req.match_info["entry"]
    # Validate entry name (no path traversal)
    if "/" in entry or ".." in entry or not entry:
        return web.json_response({"error": "invalid entry name"}, status=400)

    trash_dir = _trash_dir(ctx)
    sidecar_path = trash_dir / f"{entry}.json"
    folder_path = trash_dir / entry

    if not sidecar_path.exists():
        return web.json_response({"error": "trash entry not found"}, status=404)

    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return web.json_response({"error": "could not read trash metadata"}, status=500)

    original_cwd = sidecar.get("original_cwd")
    if not original_cwd:
        return web.json_response({"error": "missing original_cwd in sidecar"}, status=400)

    # Validate restore destination against the path allowlist before moving anything.
    # Prevents a tampered sidecar from restoring a folder to an arbitrary path.
    allowlist_err = _path_allowlist_check(original_cwd, ctx)
    if allowlist_err:
        return web.json_response(
            {"error": f"restore destination rejected: {allowlist_err}"}, status=400
        )

    # Check collision
    if Path(original_cwd).exists():
        return web.json_response(
            {"error": f"cannot restore: original path is occupied: {original_cwd}"}, status=409
        )

    if not folder_path.exists():
        return web.json_response({"error": "trash folder not found"}, status=404)

    # Move folder back
    shutil.move(str(folder_path), original_cwd)

    # Rebind topics.json
    pid = sidecar.get("id")
    name = sidecar.get("name", pid)
    tg_chat = sidecar.get("tg_chat")
    tg_thread = sidecar.get("tg_thread")

    if tg_chat and tg_thread:
        session_key = f"{tg_chat}:{tg_thread}"
        try:
            _maybe_reload_topics(ctx)
            ctx["topics"][session_key] = {
                "project": name,
                "cwd": original_cwd,
                "model": ctx.get("DEFAULT_MODEL", "sonnet"),
            }
            ctx["save_topics"]()
        except Exception as e:
            print(f"[restore] WARNING: topics rebind failed: {e}")

    # Remove sidecar
    try:
        sidecar_path.unlink()
    except Exception:
        pass

    return web.json_response({"restored": True, "cwd": original_cwd})


async def api_projects_archived(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    archived_ids = _load_archived(ctx)
    _maybe_reload_topics(ctx)
    result = []
    seen: set[str] = set()
    for key, b in ctx["topics"].items():
        cwd = b.get("cwd", "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        pid = _project_id(cwd)
        if pid in archived_ids:
            result.append({"id": pid, "name": b.get("project", pid), "cwd": cwd})
    result.sort(key=lambda x: x["name"].lower())
    return web.json_response({"projects": result})


async def api_project_group_set(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    group = body.get("group")
    if group is not None and not isinstance(group, str):
        return web.json_response({"error": "group must be a string or null"}, status=400)
    if group is not None and not group.strip():
        return web.json_response({"error": "group label cannot be empty"}, status=400)
    groups_data = _load_groups(ctx)
    if group is not None:
        group = group.strip()
        # Auto-add group if it doesn't exist
        if group not in groups_data["groups"]:
            groups_data["groups"].append(group)
        groups_data["assignments"][pid] = group
    else:
        groups_data["assignments"].pop(pid, None)
    _save_groups(ctx, groups_data)
    return web.json_response({"ok": True})


async def api_project_favorite(req: web.Request) -> web.Response:
    """Spec-031: POST /api/projects/{id}/favorite — toggle favorite status."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    favorite = body.get("favorite")
    if not isinstance(favorite, bool):
        return web.json_response({"error": "favorite must be a boolean"}, status=400)
    fav_data = _load_favorites(ctx)
    favs = fav_data.get("favorites", [])
    if favorite:
        if pid not in favs:
            favs.append(pid)
    else:
        favs = [f for f in favs if f != pid]
    _save_favorites(ctx, {"favorites": favs})
    return web.json_response({"ok": True})


async def api_project_groups_get(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    return web.json_response(_load_groups(ctx))


async def api_project_groups_manage(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    new_groups = body.get("groups")
    if not isinstance(new_groups, list):
        return web.json_response({"error": "groups must be a list"}, status=400)
    # Validate: all items must be non-empty strings
    for g in new_groups:
        if not isinstance(g, str) or not g.strip():
            return web.json_response({"error": "group labels must be non-empty strings"}, status=400)
    new_groups = [g.strip() for g in new_groups]
    groups_data = _load_groups(ctx)
    # Remove assignments for deleted groups
    new_set = set(new_groups)
    groups_data["assignments"] = {
        pid: label for pid, label in groups_data["assignments"].items()
        if label in new_set
    }
    groups_data["groups"] = new_groups
    _save_groups(ctx, groups_data)
    return web.json_response({"ok": True})


# ── Spec-030 Phase 1: atomic project-group management endpoints ───────────────

async def api_project_groups_create(req: web.Request) -> web.Response:
    """POST /api/project-groups/create  body: {name}
    Append a new empty group.  Idempotent: already-exists → 200 with current state."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"error": "name must be a non-empty string"}, status=400)
    name = name.strip()
    groups_data = _load_groups(ctx)
    if name not in groups_data["groups"]:
        groups_data["groups"].append(name)
        _save_groups(ctx, groups_data)
    return web.json_response(groups_data)


async def api_project_groups_rename(req: web.Request) -> web.Response:
    """POST /api/project-groups/rename  body: {from, to}
    Rename a group in the groups list AND remap all matching assignments."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    from_name = body.get("from")
    to_name = body.get("to")
    if not isinstance(from_name, str) or not from_name.strip():
        return web.json_response({"error": "from must be a non-empty string"}, status=400)
    if not isinstance(to_name, str) or not to_name.strip():
        return web.json_response({"error": "to must be a non-empty string"}, status=400)
    from_name = from_name.strip()
    to_name = to_name.strip()
    groups_data = _load_groups(ctx)
    if from_name not in groups_data["groups"]:
        return web.json_response({"error": f"group '{from_name}' not found"}, status=400)
    # Collision: target already exists and is different from the source
    if to_name != from_name and to_name in groups_data["groups"]:
        return web.json_response({"error": f"group '{to_name}' already exists"}, status=400)
    # Rename in groups list (preserve order)
    groups_data["groups"] = [
        to_name if g == from_name else g for g in groups_data["groups"]
    ]
    # Remap every assignment whose value matches from_name
    groups_data["assignments"] = {
        pid: (to_name if label == from_name else label)
        for pid, label in groups_data["assignments"].items()
    }
    _save_groups(ctx, groups_data)
    return web.json_response(groups_data)


async def api_project_groups_delete(req: web.Request) -> web.Response:
    """POST /api/project-groups/delete  body: {name}
    Remove a group and unassign all projects pointing to it.  Idempotent if absent."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"error": "name must be a non-empty string"}, status=400)
    name = name.strip()
    groups_data = _load_groups(ctx)
    if name in groups_data["groups"]:
        groups_data["groups"] = [g for g in groups_data["groups"] if g != name]
        groups_data["assignments"] = {
            pid: label for pid, label in groups_data["assignments"].items()
            if label != name
        }
        _save_groups(ctx, groups_data)
    return web.json_response(groups_data)


async def api_project_groups_reorder(req: web.Request) -> web.Response:
    """POST /api/project-groups/reorder  body: {order: [...]}
    Set the groups list to `order`.  Must be an exact permutation (same set)."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    order = body.get("order")
    if not isinstance(order, list):
        return web.json_response({"error": "order must be a list"}, status=400)
    for item in order:
        if not isinstance(item, str) or not item.strip():
            return web.json_response({"error": "order items must be non-empty strings"}, status=400)
    order = [item.strip() for item in order]
    groups_data = _load_groups(ctx)
    if set(order) != set(groups_data["groups"]) or len(order) != len(groups_data["groups"]):
        return web.json_response(
            {"error": "order must be a permutation of the current groups (no additions or removals)"},
            status=400,
        )
    groups_data["groups"] = order
    _save_groups(ctx, groups_data)
    return web.json_response(groups_data)


async def api_project_claude_md(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    path = Path(project["cwd"]) / "CLAUDE.md"
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            exists = True
        else:
            content = ""
            exists = False
    except Exception as e:
        content = f"[read error: {e}]"
        exists = False
    return web.json_response({"path": str(path), "content": content, "exists": exists})


async def api_project_readme(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = Path(project["cwd"])
    # try common README filename variants
    candidates = ["README.md", "readme.md", "Readme.md", "README.MD",
                  "README.markdown", "README.rst", "README.txt", "README"]
    path, content, exists = cwd / "README.md", "", False
    try:
        for name in candidates:
            p = cwd / name
            if p.exists():
                path, content, exists = p, p.read_text(encoding="utf-8"), True
                break
    except Exception as e:
        content, exists = f"[read error: {e}]", False
    return web.json_response({"path": str(path), "content": content, "exists": exists})


_README_CANDIDATES = ["README.md", "readme.md", "Readme.md", "README.MD",
                      "README.markdown", "README.rst", "README.txt", "README"]


async def _write_doc(req: web.Request, resolve_path):
    """Shared writer for CLAUDE.md/README: POST {content} → overwrite file.
    resolve_path(cwd)→Path picks the target file (respects existing filename variant)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    path = resolve_path(Path(project["cwd"]))
    try:
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"write error: {e}"}, status=500)
    return web.json_response({"path": str(path), "content": content, "exists": True})


async def api_project_claude_md_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/claude-md — overwrite CLAUDE.md."""
    return await _write_doc(req, lambda cwd: cwd / "CLAUDE.md")


async def api_project_readme_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/readme — overwrite existing README (or create README.md)."""
    def _pick(cwd: Path) -> Path:
        for name in _README_CANDIDATES:
            if (cwd / name).exists():
                return cwd / name
        return cwd / "README.md"
    return await _write_doc(req, _pick)


def _spec_dirs(ctx: dict, project: dict) -> list[tuple[Path, str]]:
    """Spec folders for the project: LOCAL <cwd>/specs/ (priority) + vault <name>/specs/.
    Returns [(dir, source)] for existing ones only. Agents often write specs locally
    while the human writes in vault; the cockpit shows both."""
    dirs: list[tuple[Path, str]] = []
    local = Path(project["cwd"]) / "specs"
    if local.is_dir():
        dirs.append((local, "local"))
    vault_proj = _find_vault_specs_dir(ctx, project["name"], project["cwd"])
    if vault_proj is not None:
        vdir = vault_proj / "specs"
        if vdir.is_dir():
            dirs.append((vdir, "vault"))
    return dirs


async def api_project_specs(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    specs = []
    seen: set[str] = set()  # dedup by name; local folder comes first → wins
    for d, src in _spec_dirs(ctx, project):
        try:
            for f in sorted(d.glob("*.md")):
                if f.name in seen:
                    continue
                seen.add(f.name)
                specs.append({"name": f.name, "path": str(f), "source": src})
        except Exception:
            pass
    return web.json_response({"specs": specs})


async def api_project_spec_content(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    spec_name = req.match_info["name"]

    # Path traversal guard: basename only, .md only
    spec_name = Path(spec_name).name
    if not spec_name.endswith(".md"):
        return web.json_response({"error": "only .md files allowed"}, status=400)

    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Search by name in local first, then vault (same priority as in the list)
    for d, _src in _spec_dirs(ctx, project):
        try:
            candidate = (d / spec_name).resolve()
            if not str(candidate).startswith(str(d.resolve())):
                continue  # path traversal — skip
            if candidate.is_file():
                content = candidate.read_text(encoding="utf-8")
                return web.json_response({"name": spec_name, "content": content})
        except Exception:
            continue
    return web.json_response({"error": "not found"}, status=404)


async def api_project_logs(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/logs — runtime logs via log_cmd from topics.json."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    log_cmd: str | None = project.get("log_cmd") or None
    if not log_cmd:
        return web.json_response({"lines": [], "configured": False, "cmd": None})

    # Delegates subprocess execution to _run_log_cmd (same streams: PIPE+STDOUT, no cwd).
    # Timeout matched to original (8 s). On timeout → 504 (restores original behaviour).
    # raise_on_timeout=True so TimeoutError propagates here (not swallowed in _run_log_cmd).
    try:
        raw = await _run_log_cmd(log_cmd, timeout=8.0, raise_on_timeout=True)
        lines = raw.splitlines()
        # last 300 lines, newest first
        tail = lines[-300:] if len(lines) > 300 else lines
        tail.reverse()
        return web.json_response({"lines": tail, "configured": True, "cmd": log_cmd})
    except asyncio.TimeoutError:
        return web.json_response({"error": "log_cmd timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─────────────────────────── Skills picker ───────────────────────────

def _parse_skill_frontmatter(text: str) -> dict | None:
    """Parses YAML frontmatter from SKILL.md → {name, description}.
    Minimal parser: finds '---\n...---', reads 'key: value' lines.
    Multi-line values (via '|' or '>') are not supported — they are rare in SKILL.md headers."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    block = text[4:end]
    out: dict[str, str] = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key and val:
            out[key] = val
    if "name" not in out:
        return None
    return {"name": out["name"], "description": out.get("description", "")}


def _scan_skills_dir(skills_dir: Path) -> list[dict]:
    """Returns a list of {name, description} from <dir>/<skill>/SKILL.md (case-insensitive filename)."""
    out: list[dict] = []
    if not skills_dir.is_dir():
        return out
    for sub in sorted(skills_dir.iterdir()):
        if not sub.is_dir():
            continue
        # SKILL.md or skill.md
        skill_file = None
        for candidate in ("SKILL.md", "skill.md"):
            p = sub / candidate
            if p.is_file():
                skill_file = p
                break
        if skill_file is None:
            continue
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = _parse_skill_frontmatter(text)
        if meta:
            out.append(meta)
    return out


async def api_project_skills(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/skills → {global: [...], project: [...]}.
    Parses SKILL.md from ~/.claude/skills/ (global) and <cwd>/.claude/skills/ (project)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    global_skills = _scan_skills_dir(Path.home() / ".claude" / "skills")
    cwd = Path(project["cwd"])
    project_skills = _scan_skills_dir(cwd / ".claude" / "skills")
    return web.json_response({"global": global_skills, "project": project_skills})


async def api_project_activity(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    project_name = project["name"]
    audit_dir: Path = ctx["DATA"] / "audit"
    marker = f"[{project_name}]"
    lines: list[str] = []

    try:
        if audit_dir.is_dir():
            # Take all audit-*.log files, sort by name (chronological)
            log_files = sorted(audit_dir.glob("audit-*.log"))
            for log_file in log_files:
                try:
                    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        if marker in line:
                            lines.append(line)
                except Exception:
                    pass
    except Exception:
        pass

    # Last 120 lines, newest first
    tail = lines[-120:] if len(lines) > 120 else lines
    tail.reverse()

    return web.json_response({"lines": tail})


# ─────────────────────────── task board (TASKS.md / DONE.md) ───────────────────────────
#
# Board primitives live in board.py (spec-034 L0). Re-exported here for backward compatibility
# so that existing call sites and tests (which import from webapp) are unchanged.

from board import (  # noqa: E402
    BOARD_COLUMNS,
    _CARD_ID_RE,
    _CARD_RE,
    _DESC_LINE_RE,
    _LABEL_TO_COL,
    _MARKER_RE,
    _PLAIN_CARD_RE,
    _board_locks,
    _count_potential_cards,
    _done_path,
    _extract_id_and_text,
    _get_board_lock,
    _load_board,
    _new_card_id,
    _parse_tasks,
    _pop_card,
    _save_board,
    _serialize_tasks,
    _tasks_path,
    _valid_card_id,
)


# ─────────────────────────── Error scanner (incidents) ───────────────────────────
#
# Crash scanner (logs + tests) → cards in the Failed section of TASKS.md.
# Incident card = regular card with marker ID of the form "err-<hash6>".
# Metadata (source, seen, first, last, excerpt) stored in description ('  > ' lines)
# as key=value — survives parser round-trip and is visible to agents in plain-md.
#
# Dedup: hash by (source_type, normalised_message, file?, line?). If a card with that
# err-<hash> already exists in Failed/Review/InProgress — update seen+last in description,
# do NOT create a new card (otherwise one hung worker generates 1000 cards overnight).

# Python traceback: "Traceback (most recent call last):" ... last line with type
_PY_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):\n((?:.+\n)+?)([A-Z][\w.]*(?:Error|Exception|Warning|Exit)):\s*(.+)",
    re.MULTILINE,
)
# Generic ERROR/CRITICAL: log line like "... ERROR ... msg" / "... CRITICAL ... msg"
_GENERIC_ERR_RE = re.compile(
    r"^.*\b(ERROR|CRITICAL|FATAL)\b[:\s]+(.+?)$", re.MULTILINE,
)
# pytest: "FAILED tests/test_x.py::test_y - AssertionError: msg"
_PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+([\w./\-]+)::([\w\[\]\-]+)(?:\s+-\s+(.+))?$", re.MULTILINE,
)
# Standard unhandled-exception line: "UNHANDLED exc_class=<Type> path=<route>"
_UNHANDLED_RE = re.compile(r"\bUNHANDLED\s+exc_class=(\S+)\s+path=(\S+)", re.MULTILINE)
# Noisy messages that appear frequently in logs but are not errors.
# Checked case-insensitively against both ERROR-line msg AND traceback (exc_type + exc_msg).
_LOG_NOISE_SUBSTRINGS = (
    "deprecat",                     # DeprecationWarning
    "GET /api/health",              # health-checks
    "200 OK",
    "telegram.ext.updater",         # PTB polling — transient, auto-retried by network_retry_loop
    "telegram.error.networkerror",  # TG API 5xx (Bad Gateway etc.) — auto-retried
    "telegram.error.timedout",      # TG API timeouts — auto-retried
)


def _hash6(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:6]


def _norm_msg(msg: str) -> str:
    """Normalises a message for hashing: removes numbers, temporary ids, paths.
    Goal — '<id=42>' and '<id=99>' give the same hash, while '<KeyError>' and '<ValueError>' differ."""
    s = msg.lower()
    s = re.sub(r"0x[0-9a-f]+", "0xN", s)            # addresses
    s = re.sub(r"\b\d{4,}\b", "N", s)               # long numbers (PID/timestamp)
    s = re.sub(r"/[\w/.\-]+", "/PATH", s)           # paths
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _parse_log_errors(log_text: str, source: str = "log") -> list[dict]:
    """Extracts errors from log text. Returns list[{source, type, message, excerpt, hash}].
    Dedup WITHIN the list: identical errors in one run → one record (seen counted above)."""
    out: list[dict] = []
    seen_hashes: set[str] = set()
    traceback_exc_types: set[str] = set()

    # Python tracebacks first (more structured)
    for m in _PY_TRACEBACK_RE.finditer(log_text):
        trace_body = m.group(1)
        exc_type = m.group(2)
        exc_msg = m.group(3).strip()
        # Noise filter: benign transient exceptions (e.g. telegram.error.NetworkError on polling) —
        # checked on (exc_type + exc_msg) so that fully-qualified type names match _LOG_NOISE_SUBSTRINGS.
        combined = f"{exc_type} {exc_msg}".lower()
        if any(noise in combined for noise in _LOG_NOISE_SUBSTRINGS):
            continue
        traceback_exc_types.add(exc_type)
        excerpt_lines = trace_body.strip().split("\n")[-3:] + [f"{exc_type}: {exc_msg}"]
        excerpt = "\n".join(ln.strip()[:200] for ln in excerpt_lines)
        h = _hash6(f"{source}|{exc_type}|{_norm_msg(exc_msg)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append({
            "source": source, "type": exc_type, "message": exc_msg,
            "excerpt": excerpt, "hash": h,
        })

    # Generic ERROR/CRITICAL — filter duplicates from python tracebacks (already in out)
    for m in _GENERIC_ERR_RE.finditer(log_text):
        level = m.group(1)
        msg = m.group(2).strip()
        if any(noise in msg.lower() for noise in _LOG_NOISE_SUBSTRINGS):
            continue
        # If the line contains "Traceback" — already handled above
        if "Traceback" in msg:
            continue
        # "UNHANDLED exc_class=..." is handled in a separate pass below — don't duplicate
        if "UNHANDLED exc_class=" in msg:
            continue
        h = _hash6(f"{source}|{level}|{_norm_msg(msg)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        out.append({
            "source": source, "type": level, "message": msg[:300],
            "excerpt": msg[:300], "hash": h,
        })

    # UNHANDLED standard line: "UNHANDLED exc_class=<Type> path=<route>"
    for m in _UNHANDLED_RE.finditer(log_text):
        exc_class = m.group(1)
        path = m.group(2)
        # If there is already a card for this type from a traceback (richer) — don't duplicate.
        # The UNHANDLED pass is primarily needed when a full traceback is absent (systemd OnFailure).
        if exc_class in traceback_exc_types:
            continue
        h = _hash6(f"{source}|UNHANDLED|{_norm_msg(exc_class + ' ' + path)}")
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        matched_line = m.group(0)
        out.append({
            "source": source, "type": exc_class,
            "message": f"unhandled at {path}",
            "excerpt": matched_line, "hash": h,
        })

    return out


def _parse_pytest_failures(pytest_output: str) -> list[dict]:
    """Extracts FAILED lines from pytest output."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _PYTEST_FAILED_RE.finditer(pytest_output):
        file_ = m.group(1)
        test = m.group(2)
        reason = (m.group(3) or "").strip()
        h = _hash6(f"test|{file_}|{test}|{_norm_msg(reason)}")
        if h in seen:
            continue
        seen.add(h)
        out.append({
            "source": "test", "type": "FAILED", "message": f"{test} — {reason}" if reason else test,
            "excerpt": f"{file_}::{test}\n{reason}".strip(), "hash": h,
            "file": file_, "test": test,
        })
    return out


# ID marker for err-cards: 'err-<hash6>'. Description — k=v lines.
_ERR_DESC_RE = re.compile(r"^(source|seen|first|last|excerpt)=(.*)$")


def _parse_incident_desc(desc: str | None) -> dict:
    """Parses the description of an err-card into a dict. Unknown lines are ignored."""
    out: dict = {}
    if not desc:
        return out
    for line in desc.splitlines():
        m = _ERR_DESC_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _format_incident_desc(meta: dict) -> str:
    """Serialises err-card metadata to description lines.
    Excerpt goes LAST — may be multi-line, but we treat it as one logical record
    (stored as one line with \\n replaced by ' / ' for compactness)."""
    lines: list[str] = []
    for key in ("source", "seen", "first", "last"):
        if key in meta:
            lines.append(f"{key}={meta[key]}")
    excerpt = meta.get("excerpt", "")
    if excerpt:
        # Collapse multi-line excerpt to one line. splitlines() catches ALL
        # line separators (incl. U+2028/U+2029/\x85) — otherwise they would break the board format.
        compact = " / ".join(excerpt.splitlines())[:400]
        lines.append(f"excerpt={compact}")
    return "\n".join(lines)


def _is_incident_card(card: dict) -> bool:
    """An incident card has an id starting with 'err-'."""
    return card.get("id", "").startswith("err-")


def _incident_title(err: dict) -> str:
    """Short card title: '[ERR] AttributeError: msg' / '[TEST] test_name — reason'."""
    msg = err["message"][:80]
    if err["source"] == "test":
        return f"[TEST] {msg}"
    if err["source"] == "log":
        return f"[ERR] {err['type']}: {msg}" if err.get("type") else f"[ERR] {msg}"
    return f"[{err['source'].upper()}] {msg}"


async def _run_log_cmd(log_cmd: str, timeout: float = 10.0, raise_on_timeout: bool = False) -> str:
    """Runs log_cmd and returns stdout (+ stderr).
    UI-controlled cmd from topics.json → exec (not shell) to prevent injection.
    raise_on_timeout=True: on timeout kills the process and re-raises asyncio.TimeoutError
    (instead of returning ""). Used by the HTTP route to return 504.
    raise_on_timeout=False (default): swallows TimeoutError and returns "" —
    preserves the scanner's behaviour (_scan_project_errors)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(log_cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            if raise_on_timeout:
                raise
            return ""
        return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        raise
    except Exception:
        return ""


async def _scan_project_errors(project: dict) -> list[dict]:
    """Scans one project: log_cmd only → list[errors]. Does NOT write to disk.
    Tests are run ONLY via the 'Run tests' button (api_project_test), not here.

    Spec-012 Ph0: high-water-mark fingerprint.
    State: data/scan_state.json  {cwd: {"last_line": "<sha1>", "last_scan_ts": <float>}}.
    Logic:
      - No fingerprint (first scan): parse only the LAST 50 lines and save fingerprint
        without immediately creating cards for the whole tail (avoids flood from old errors).
      - Fingerprint exists: find the last occurrence of the line with sha1==fingerprint, parse everything AFTER.
        If fingerprint not found (log rotated/moved out of window): fallback = last 500 lines
        (downstream dedup protects against duplicates).
      - After parsing: update last_line = sha1(last non-empty line), last_scan_ts = now.
    Key in state: cwd (stable absolute project path).
    """
    errors: list[dict] = []
    log_cmd = project.get("log_cmd")

    if log_cmd:
        log_text = await _run_log_cmd(log_cmd)
        if log_text:
            all_lines = log_text.splitlines()
            cwd_key = project.get("cwd", "")
            now_ts = time.time()
            _FP_BLOCK = 6  # block of last N lines as a position "fingerprint" — resilient
                           # to repeated SINGLE lines (heartbeat / "200 OK"), where
                           # single-line fingerprint missed new errors between two identical lines.
                           # Block captures the real end position.

            state = _scan_state_load()
            last_block = state.get(cwd_key, {}).get("block")  # list[sha1] of last N lines from previous scan
            line_hashes = [hashlib.sha1(ln.encode("utf-8", "replace")).hexdigest() for ln in all_lines]

            if not last_block:
                # First scan (no state): parse only the last 50 lines to avoid flooding
                # the board with historical errors. Block fingerprint saved below.
                if all_lines:
                    errors.extend(_parse_log_errors("\n".join(all_lines[-50:]), source="log"))
            else:
                # FIRST occurrence of the block (forward) — everything AFTER is considered new.
                # Forward-bias is safer than last-occurrence: on block repeat we rather
                # re-parse old lines (dedup + dismissed will suppress) than miss new ones.
                bl = len(last_block)
                end_idx = None
                for end in range(bl, len(line_hashes) + 1):
                    if line_hashes[end - bl:end] == last_block:
                        end_idx = end
                        break
                # Block not found (rotation / state wiped) → fallback 500 (dedup/dismissed cover us).
                new_lines = all_lines[end_idx:] if end_idx is not None else all_lines[-500:]
                if new_lines:
                    errors.extend(_parse_log_errors("\n".join(new_lines), source="log"))

            # ALWAYS save the block for the end of current output (even whitespace-only — otherwise
            # we'd stay in "first scan" mode and miss lines beyond the 50-line tail).
            state[cwd_key] = {"block": line_hashes[-_FP_BLOCK:], "last_scan_ts": now_ts}
            _scan_state_save(state)

    return errors


async def _ingest_errors_to_board(cwd: str, name: str, errors: list[dict]) -> tuple[int, int]:
    """Writes/updates err-cards in TASKS.md. Returns (added, updated).
    Under board-lock. Dedup: card err-<hash> already exists → update seen/last in description."""
    if not errors:
        return (0, 0)

    lock = _get_board_lock(cwd)
    async with lock:
        raw, preamble, cols = _load_board(cwd)
        # Guard: if file is missing/unparseable — better not to touch it
        potential = _count_potential_cards(raw)
        parsed_count = sum(len(v) for v in cols.values())
        if raw.strip() and parsed_count < potential:
            return (0, 0)  # suspicious file — don't write

        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")
        added = 0
        updated = 0

        # Index of existing err-cards: hash → (column, card)
        existing: dict[str, tuple[str, dict]] = {}
        for col_key, col_cards in cols.items():
            for card in col_cards:
                cid = card.get("id", "")
                if cid.startswith("err-"):
                    h = cid[4:]
                    existing[h] = (col_key, card)

        now_float = time.time()
        dismissed_snapshot = _dismissed_load()  # once per batch, not per error
        for err in errors:
            h = err["hash"]
            if h in existing:
                # Update seen+last, don't move the column (user may have already moved it)
                col_key, card = existing[h]
                meta = _parse_incident_desc(card.get("description"))
                try:
                    seen_n = int(meta.get("seen", "1")) + 1
                except ValueError:
                    seen_n = 2
                meta["seen"] = str(seen_n)
                meta["last"] = now_iso
                # first / source / excerpt — keep from first occurrence
                card["description"] = _format_incident_desc(meta)
                updated += 1
            else:
                # Spec-012 Ph0 Task B: don't resurrect dismissed incidents within TTL window
                _dts = dismissed_snapshot.get(h)
                if _dts is not None and (now_float - _dts) < _DISMISS_TTL:
                    continue
                # New card in Failed
                meta = {
                    "source": err["source"],
                    "seen": "1",
                    "first": now_iso,
                    "last": now_iso,
                    "excerpt": err.get("excerpt", ""),
                }
                cols["failed"].append({
                    "id": f"err-{h}",
                    "text": _incident_title(err),
                    "description": _format_incident_desc(meta),
                })
                added += 1

        if added or updated:
            _save_board(cwd, name, preamble, cols)
        return (added, updated)


_REPORT_DEBOUNCE: "dict[str, float]" = {}   # hash → ts of last in-process report
_REPORT_DEBOUNCE_SEC = 10.0                  # same incident written at most once per N sec


async def _report_incident(ctx: dict, exc_class: str, where: str, project_id: str = "claude-ops-bot") -> None:
    """Spec-012 Ph1/Ph3: DIRECT (in-process) report of one incident → card,
    bypassing the log scanner and its delay. Hash is identical to what `_parse_log_errors`
    produces for the line `UNHANDLED exc_class=.. path=..` (source="log") → dedup: whoever
    is first (this path or the scanner) creates the card; the second bumps seen. Resolves
    the project itself (all work in background so it doesn't slow the response).
    Swallows all exceptions — observability must not drop a request.
    Reused by the push endpoint (Ph3)."""
    try:
        h = _hash6(f"log|UNHANDLED|{_norm_msg(exc_class + ' ' + where)}")
        # Debounce: an endpoint failing on EVERY request must not trigger an I/O storm
        # of writes to TASKS.md. Same hash written at most once per N sec (card already
        # created; rare skipped seen++ will be caught by the background scanner). Before board-lock.
        # Key includes project_id — otherwise the same error in DIFFERENT projects (path
        # normalised to /PATH → shared hash) would suppress each other cross-project.
        dkey = f"{project_id}\x00{h}"
        now = time.time()
        last = _REPORT_DEBOUNCE.get(dkey)
        if last is not None and (now - last) < _REPORT_DEBOUNCE_SEC:
            return
        _REPORT_DEBOUNCE[dkey] = now
        if len(_REPORT_DEBOUNCE) > 256:   # cap dict growth
            for k in [k for k, v in _REPORT_DEBOUNCE.items() if now - v > _REPORT_DEBOUNCE_SEC]:
                _REPORT_DEBOUNCE.pop(k, None)
        proj = _find_project_by_id(ctx, project_id)
        if not proj:
            return
        err = {
            "source": "log",
            "type": exc_class,
            "message": f"unhandled at {where}",
            "excerpt": f"UNHANDLED exc_class={exc_class} path={where}",
            "hash": h,
        }
        await _ingest_errors_to_board(proj["cwd"], proj["name"], [err])
    except Exception:
        pass


async def _scan_and_ingest(project: dict, ctx: dict | None = None) -> dict:
    """Full cycle: scan project, ingest to board, optional TG notification.
    Returns {ok, added, updated, scanned}."""
    try:
        errors = await _scan_project_errors(project)
    except Exception as e:
        return {"ok": False, "error": str(e), "scanned": 0, "added": 0, "updated": 0}

    try:
        added, updated = await _ingest_errors_to_board(project["cwd"], project["name"], errors)
    except Exception as e:
        return {"ok": False, "error": str(e), "scanned": len(errors), "added": 0, "updated": 0}

    # TG notification for NEW incidents (not dedup-updates)
    if added > 0 and ctx and project.get("notify_on_error"):
        try:
            ptb_app = ctx.get("ptb_app")
            tg_thread_str = project.get("session_key") or project.get("tg_thread", "")
            if ptb_app and ":" in tg_thread_str:
                chat_s, thread_s = tg_thread_str.split(":", 1)
                chat_id = int(chat_s)
                thread_id = int(thread_s) if thread_s.isdigit() else None
                msg = f"🚨 <b>{added}</b> new incidents in <b>{project['name']}</b> — check the board."
                await ptb_app.bot.send_message(
                    chat_id, msg, message_thread_id=thread_id, parse_mode="HTML",
                )
        except Exception as e:
            print(f"[scan_and_ingest] tg notify failed for {project['name']}: {e}")

    return {"ok": True, "scanned": len(errors), "added": added, "updated": updated}


async def api_project_scan_errors(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/scan-errors — manual scanner run for one project."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if not project.get("log_cmd"):
        return web.json_response({
            "ok": False, "error": "log_cmd not configured in topics.json",
        }, status=400)
    res = await _scan_and_ingest(project, ctx)
    return web.json_response(res)


async def api_project_incidents(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/incidents — count of active incidents (for sidebar badge).
    Active = err-cards in Failed/Review/InProgress (not in Done)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        _, _, cols = _load_board(project["cwd"])
    except Exception:
        return web.json_response({"count": 0, "by_column": {}})
    by_col = {}
    total = 0
    for key, col_cards in cols.items():
        n = sum(1 for c in col_cards if _is_incident_card(c))
        if n:
            by_col[key] = n
            total += n
    return web.json_response({"count": total, "by_column": by_col})


async def api_project_incident(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/incident — Spec-012 Ph3: optional incident push.

    Cookie-auth exempt (auth_middleware skips this route), but requires double opt-in:
    (1) global flag incident_push_enabled=True in settings.json,
    (2) secret CLAUDEOPS_INCIDENT_TOKEN in .claude-ops/secrets/ of the project.

    Check order (fail-safe):
    1. Global flag OFF → 404 (don't reveal that the endpoint exists).
    2. Project not found → 404.
    3. Token: secret CLAUDEOPS_INCIDENT_TOKEN not set (per-project opt-in) → 403.
       Token from X-Incident-Token / body → constant-time compare; mismatch → 403.
    4. JSON: bad parse → 400; exc_class empty → 400; sanitize (strip newlines, cap).
    5. Rate-limit per-project (30/min) → 429.
    6. _report_incident fire-and-forget (dedup = same hash as log-scanner).
    7. {"ok": True} — token/secret NEVER in response.
    """
    ctx = req.app["ctx"]
    now = time.time()

    # 1. Global master flag (cheap, before any I/O; off by default → 404)
    if _get_global_setting("incident_push_enabled", False) is not True:
        return web.json_response({"error": "not found"}, status=404)

    # 1.5. Per-IP backstop — BEFORE project resolution and secret read, so unauth flood
    # doesn't hit disk (_secrets_read) on every request.
    ip = _client_ip(req) or "?"
    ip_hist = [t for t in _incident_ip_history.get(ip, []) if now - t < _INCIDENT_PUSH_WINDOW]
    if len(ip_hist) >= _INCIDENT_IP_MAX:
        return web.json_response({"error": "too many requests"}, status=429)
    ip_hist.append(now)
    _incident_ip_history[ip] = ip_hist
    if len(_incident_ip_history) > 4096:   # cap dict growth
        for k in [k for k, v in list(_incident_ip_history.items()) if not v or now - v[-1] > _INCIDENT_PUSH_WINDOW]:
            _incident_ip_history.pop(k, None)

    # 2. Project
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "not found"}, status=404)

    # 3. Parse body ONCE (bad JSON → 400, not masked as 403 token-mismatch)
    try:
        body = await req.json()
        if not isinstance(body, dict):
            raise ValueError("not a dict")
    except Exception:
        return web.json_response({"error": "bad request: invalid JSON"}, status=400)

    # 4. Token — per-project opt-in (header preferred, otherwise from body)
    expected_token = _secrets_read(project["cwd"]).get("CLAUDEOPS_INCIDENT_TOKEN") or ""
    if not expected_token:
        return web.json_response({"error": "forbidden"}, status=403)   # push not enabled for this project
    body_token = body.get("token", "")
    presented_token = req.headers.get("X-Incident-Token", "") or (body_token if isinstance(body_token, str) else "")
    if not _hmac.compare_digest(str(presented_token), str(expected_token)):
        return web.json_response({"error": "forbidden"}, status=403)

    # 5. Sanitize: splitlines() catches ALL line separators (incl. U+2028/U+2029/\x85) —
    # otherwise a token-holder could inject '## Section' / '- [ ] card' into TASKS.md.
    def _sanitize(s, maxlen: int) -> str:
        return " ".join(str(s).splitlines()).strip()[:maxlen]

    exc_class = _sanitize(body.get("exc_class", ""), 120)
    where = _sanitize(body.get("where") or body.get("path") or "(push)", 200)
    if not exc_class:
        return web.json_response({"error": "bad request: exc_class required"}, status=400)

    # 6. Per-project rate-limit
    history = [t for t in _incident_push_history.get(pid, []) if now - t < _INCIDENT_PUSH_WINDOW]
    if len(history) >= _INCIDENT_PUSH_MAX:
        return web.json_response({"error": "too many requests"}, status=429)
    history.append(now)
    _incident_push_history[pid] = history

    # 7. Report — fire-and-forget. Dedup by hash shared with log-scanner (one error = one card).
    _spawn_bg(_report_incident(ctx, exc_class, where, project_id=pid))

    # 8. Response — token/secret NEVER revealed
    return web.json_response({"ok": True})


async def api_project_notify_toggle(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/notify-on-error {enabled: bool} — TG notifications on new errors.

    When enabled: scanner sends a ping to the project's TG topic when new incidents are detected
    ("crashed"). Flag notify_on_error in topics.json for all
    entries with the same cwd. Auth: standard middleware.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if project.get("is_free"):
        return web.json_response({"error": "notifications are not available for free chats"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    enabled = bool(body.get("enabled", False))

    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            b["notify_on_error"] = enabled
            changed += 1

    if changed:
        save_topics = ctx.get("save_topics")
        if callable(save_topics):
            save_topics()

    return web.json_response({"ok": True, "notify_on_error": enabled, "topics_updated": changed})


# Background task: scans all projects every SCAN_INTERVAL_SEC seconds.
# Spec-012 Ph0: default lowered to 60s (incremental parse — cheap; env override preserved).
_SCAN_INTERVAL_SEC = int(os.environ.get("ERROR_SCAN_INTERVAL", "60"))  # 1 min (was 5 min)

# ─────────────────────────── Scan state (Spec 012 Ph0) ────────────────────────
#
# High-water mark: per-project fingerprint of the last processed log line.
# File: data/scan_state.json  {<cwd>: {"last_line": "<sha1>", "last_scan_ts": <float>}}
# Dismissed incidents: data/dismissed_incidents.json  {<hash6>: <dismissed_ts>}
# Both files live in data/ (gitignored). All helpers swallow ALL exceptions — none
# can break the scanner.

_SCAN_STATE_PATH: "Path | None" = None      # set in _scan_state_init(ctx)
_DISMISSED_PATH: "Path | None" = None       # set in _scan_state_init(ctx)
_DISMISS_TTL = 24 * 3600                    # 24 h — deleted/done card is not resurrected

# ─────────────────────────── Card Queue (sequential per-project) ───────────────────────────
# data/card_queue.json: {session_key: [card_id, ...]} — FIFO queue of pending cards.
# One card per project at a time; others wait in the queue.

_QUEUE_PATH: "Path | None" = None           # set in _scan_state_init(ctx)

_QUEUE_DRAIN_INTERVAL_SEC = 3               # backstop drain cycle interval

# In-memory canonical queue: {session_key: [card_id, ...]}. Single source of truth
# within the process. All mutations change _QUEUE SYNCHRONOUSLY and immediately flush to disk.
# This eliminates the RMW race (read-modify-write via await): _drain_queue makes several
# mutations via await, and concurrent enqueue/remove are not lost — mutation is atomic within
# one event-loop turn (no await between reading and writing _QUEUE).
_QUEUE: "dict[str, list[str]]" = {}


def _scan_state_init(ctx: dict) -> None:
    """Called from start() — sets paths to state files. Loads persisted queue into _QUEUE."""
    global _SCAN_STATE_PATH, _DISMISSED_PATH, _QUEUE_PATH
    _SCAN_STATE_PATH = ctx["DATA"] / "scan_state.json"
    _DISMISSED_PATH = ctx["DATA"] / "dismissed_incidents.json"
    _QUEUE_PATH = ctx["DATA"] / "card_queue.json"
    # Load persisted queue into in-memory canonical dict (restart-resume).
    # Clear _QUEUE first — test isolation and re-init safety.
    _QUEUE.clear()
    try:
        if _QUEUE_PATH is not None and _QUEUE_PATH.exists():
            data = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        _QUEUE[k] = [c for c in v if isinstance(c, str)]
    except Exception:
        pass


def _queue_flush() -> None:
    """Flushes in-memory _QUEUE to disk. Swallows ALL exceptions.
    _QUEUE_PATH is None → memory only, no crash (tests without init)."""
    try:
        if _QUEUE_PATH is None:
            return
        _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _QUEUE_PATH.write_text(json.dumps(_QUEUE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _queue_enqueue(session_key: str, card_id: str) -> bool:
    """Appends card_id to the tail of the queue (dedup). Returns True if actually added,
    False if already present (dedup). _QUEUE mutation is synchronous → flush."""
    try:
        lst = _QUEUE.setdefault(session_key, [])
        if card_id in lst:
            return False
        lst.append(card_id)
        _queue_flush()
        return True
    except Exception:
        return False


def _queue_remove(session_key: str, card_id: str) -> None:
    """Removes card_id from the queue for session_key (absent — silent). Mutation is synchronous → flush."""
    try:
        lst = _QUEUE.get(session_key)
        if lst is not None and card_id in lst:
            lst.remove(card_id)
            _queue_flush()
    except Exception:
        pass


def _queue_for(session_key: str) -> list:
    """Returns the list of card_ids in the queue for session_key (FIFO) — a copy from _QUEUE."""
    try:
        return list(_QUEUE.get(session_key, []))
    except Exception:
        return []


def _scan_state_load() -> dict:
    """Loads {cwd: {last_line, last_scan_ts}}. Errors/absent → {}."""
    try:
        if _SCAN_STATE_PATH is None or not _SCAN_STATE_PATH.exists():
            return {}
        data = json.loads(_SCAN_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _scan_state_save(state: dict) -> None:
    """Saves state to disk. Swallows ALL exceptions."""
    try:
        if _SCAN_STATE_PATH is None:
            return
        _SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCAN_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _dismissed_load() -> dict:
    """Loads {hash6: dismissed_ts}. Errors/absent → {}."""
    try:
        if _DISMISSED_PATH is None or not _DISMISSED_PATH.exists():
            return {}
        data = json.loads(_DISMISSED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dismissed_save(dismissed: dict) -> None:
    """Saves dismissed to disk. Swallows ALL exceptions."""
    try:
        if _DISMISSED_PATH is None:
            return
        _DISMISSED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DISMISSED_PATH.write_text(json.dumps(dismissed), encoding="utf-8")
    except Exception:
        pass


def _dismissed_add(h: str) -> None:
    """Records hash as dismissed(now). Prunes expired entries (>TTL). Swallows ALL exceptions."""
    try:
        now = time.time()
        dismissed = _dismissed_load()
        dismissed[h] = now
        # Pruning: remove entries older than TTL
        dismissed = {k: v for k, v in dismissed.items() if now - v < _DISMISS_TTL}
        _dismissed_save(dismissed)
    except Exception:
        pass


def _dismissed_is_active(h: str, now: float) -> bool:
    """True if hash was dismissed less than _DISMISS_TTL seconds ago."""
    try:
        dismissed = _dismissed_load()
        ts = dismissed.get(h)
        if ts is None:
            return False
        return (now - ts) < _DISMISS_TTL
    except Exception:
        return False

# ─────────────────────── global settings (data/settings.json) ───────────────────────
#
# Global cockpit knobs, override env defaults at runtime (hot-reload by mtime).
# Per-project settings live in topics.json (model/notify_on_error/log_cmd/
# test_cmd/git_enabled). Globals are in a separate file as they are not project-bound.
# Init: start() calls _settings_init(ctx).

_SETTINGS_PATH: "Path | None" = None
_SETTINGS_CACHE: dict = {}
_SETTINGS_MTIME: float = 0.0

# Key → (type, min, max) for POST validation. None bounds = no range check.
_GLOBAL_SETTINGS_SPEC = {
    "scan_interval_sec": ("int", 30, 3600),
    "default_model": ("model", None, None),          # "" → ctx default
    # watchdog_stall_sec removed — stall interrupt deleted (spec-039)
    "watchdog_max_sec": ("int", 60, 14400),
    # Spec-012 Ph3: global master flag for push endpoint. OFF by default —
    # operator must explicitly enable. Without this flag POST /incident → 404.
    "incident_push_enabled": ("bool", None, None),
    # Board reconciler controls (Task A):
    # - board_reconcile_enabled: when False the reconciler is a complete no-op.
    # - board_reconcile_on_match: "done" → auto-archive matched cards;
    #   "review" → remap done→review so operator closes manually.
    "board_reconcile_enabled": ("bool", None, None),
    "board_reconcile_on_match": ("enum", ("done", "review"), None),
    # Card 43665f — model routing: default model used for board-card agent runs.
    # "" / absent → falls back to "sonnet". Does NOT affect chat runs.
    "board_card_model": ("model", None, None),
}


def _settings_init(ctx: dict) -> None:
    """Called from start() — sets path to data/settings.json."""
    global _SETTINGS_PATH
    _SETTINGS_PATH = ctx["DATA"] / "settings.json"


def _load_global_settings() -> dict:
    """Reads settings.json with mtime gate. Corrupted file → previous cache."""
    global _SETTINGS_CACHE, _SETTINGS_MTIME
    if _SETTINGS_PATH is None:
        return {}
    try:
        mtime = _SETTINGS_PATH.stat().st_mtime
    except FileNotFoundError:
        _SETTINGS_CACHE = {}
        return {}
    except Exception:
        return _SETTINGS_CACHE if isinstance(_SETTINGS_CACHE, dict) else {}
    if mtime != _SETTINGS_MTIME:
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _SETTINGS_CACHE = data
                _SETTINGS_MTIME = mtime
        except Exception:
            pass  # corrupted/partial file during race — keep previous cache
    return _SETTINGS_CACHE if isinstance(_SETTINGS_CACHE, dict) else {}


def _get_global_setting(key: str, fallback=None):
    """Effective value of a global setting: from settings.json or fallback.
    Stored None/absent → fallback."""
    val = _load_global_settings().get(key)
    return fallback if val is None else val


def _save_global_settings(data: dict) -> None:
    """Atomically writes settings.json and forces cache re-read."""
    global _SETTINGS_MTIME
    if _SETTINGS_PATH is None:
        return
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_name(_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SETTINGS_PATH)
    _SETTINGS_MTIME = 0.0


# ── UI-state sync (cockpit layout across devices) ─────────────────────────────
# Server-side store for the UI layout (open tabs, active tab, sidebar order,
# split-view) — so it follows the user across devices instead of living only
# in the local browser's localStorage. Stored under a namespace key;
# currently single-tenant ("default"). The ONLY place to swap to user_id
# for multi-user is _ui_state_ns() (see spec-013-multi-user).
# Semantics: server = source of truth, frontend debounces writes, last-write-wins
# (for one person on two devices conflicts are insignificant).
_UI_STATE_PATH: "Path | None" = None
_UI_STATE_MAX_BYTES = 64 * 1024   # layout is tiny; protects against file bloat


def _ui_state_init(ctx: dict) -> None:
    """Called from start() — sets path to data/ui_state.json."""
    global _UI_STATE_PATH
    _UI_STATE_PATH = ctx["DATA"] / "ui_state.json"


def _ui_state_ns(req: web.Request) -> str:
    """Namespace for UI layout storage. Single-tenant → "default".
    The ONLY place to change to user_id for multi-user (see spec-013)."""
    return "default"


def _ui_state_load_all() -> dict:
    """Reads the entire ui_state.json ({ns: state}). Corrupted/absent → {}."""
    if _UI_STATE_PATH is None:
        return {}
    try:
        data = json.loads(_UI_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}  # corrupted/partial file during race — don't crash the cockpit


def _ui_state_save_all(data: dict) -> None:
    """Atomically writes ui_state.json (tmp+replace)."""
    if _UI_STATE_PATH is None:
        return
    _UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _UI_STATE_PATH.with_name(_UI_STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_UI_STATE_PATH)


async def api_ui_state_get(req: web.Request) -> web.Response:
    """GET /api/ui-state → {state: {...}} — cockpit layout for this user."""
    ns = _ui_state_ns(req)
    state = _ui_state_load_all().get(ns)
    return web.json_response({"state": state if isinstance(state, dict) else {}})


async def api_ui_state_put(req: web.Request) -> web.Response:
    """PUT /api/ui-state {state: {...}} — save layout. Body is an opaque
    JSON object (frontend decides the keys); server stores per namespace."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    state = body.get("state")
    if not isinstance(state, dict):
        return web.json_response({"error": "state must be an object"}, status=400)
    if len(json.dumps(state, ensure_ascii=False).encode("utf-8")) > _UI_STATE_MAX_BYTES:
        return web.json_response({"error": "state too large"}, status=413)
    ns = _ui_state_ns(req)
    all_state = _ui_state_load_all()
    all_state[ns] = state
    _ui_state_save_all(all_state)
    return web.json_response({"ok": True})


def _effective_default_model(ctx: dict) -> str:
    """Default model for new projects: global setting or ctx['DEFAULT_MODEL']."""
    return _get_global_setting("default_model", None) or ctx.get("DEFAULT_MODEL", "sonnet")


def _effective_card_model(card: dict) -> str:
    """Model resolution order for board-card agent runs (Card 43665f).

    1. card['model'] if set and valid
    2. board_card_model global setting if set and valid
    3. fallback: 'sonnet'

    Intentionally does NOT fall back to the project model — cards are cheap
    by default while chat/interactive runs keep using the project model.
    """
    # 1. Per-card override
    card_model = (card.get("model") or "").strip().lower()
    if card_model in _ALLOWED_MODELS:
        return card_model
    # 2. Global board_card_model setting
    global_card_model = (_get_global_setting("board_card_model", "") or "").strip().lower()
    if global_card_model in _ALLOWED_MODELS:
        return global_card_model
    # 3. Cheap fallback
    return "sonnet"


def _git_enabled(project: dict) -> bool:
    """git_enabled per-project (topics.json). Default True (git enabled).
    False → cockpit does NOT use git: card runs are legacy, git-sync returns 409,
    health check does not flag missing .git. Existing .git is not physically touched."""
    return project.get("git_enabled", True) is not False


# ─────────────────────── API: settings (global + per-project) ───────────────────────

_PROJECT_SETTING_FIELDS = ("git_enabled", "model", "notify_on_error", "log_cmd", "test_cmd", "agents_config", "type", "self_heal")


def _validate_global_settings(partial: dict) -> "tuple[dict, str | None]":
    """Validates a partial update against _GLOBAL_SETTINGS_SPEC.
    None/"" → reset key to default. Returns (clean, None) or ({}, error)."""
    clean: dict = {}
    for key, val in partial.items():
        spec = _GLOBAL_SETTINGS_SPEC.get(key)
        if spec is None:
            return {}, f"unknown key: {key}"
        typ, lo, hi = spec
        if val is None or val == "":
            clean[key] = None
            continue
        if typ == "bool":
            if not isinstance(val, bool):
                return {}, f"{key}: expected bool"
            clean[key] = val
        elif typ == "int":
            try:
                iv = int(val)
            except (TypeError, ValueError):
                return {}, f"{key}: expected integer"
            if (lo is not None and iv < lo) or (hi is not None and iv > hi):
                return {}, f"{key}: out of range [{lo}, {hi}]"
            clean[key] = iv
        elif typ == "model":
            sv = str(val).strip().lower()
            if sv not in _ALLOWED_MODELS:
                return {}, f"{key}: model not in {sorted(_ALLOWED_MODELS)}"
            clean[key] = sv
        elif typ == "enum":
            # lo holds the tuple of allowed values for enum specs
            allowed = lo  # type: ignore[assignment]
            sv = str(val).strip().lower()
            if sv not in allowed:
                return {}, f"{key}: must be one of {list(allowed)}"
            clean[key] = sv
    return clean, None


async def api_settings_get(req: web.Request) -> web.Response:
    """GET /api/settings — global settings: stored + effective values + spec."""
    ctx = req.app["ctx"]
    stored = dict(_load_global_settings())
    effective = {
        "scan_interval_sec": int(_get_global_setting("scan_interval_sec", _SCAN_INTERVAL_SEC)),
        "default_model": _get_global_setting("default_model", ctx.get("DEFAULT_MODEL", "sonnet")),
        # watchdog_stall_sec removed (spec-039)
        "watchdog_max_sec": int(_get_global_setting("watchdog_max_sec", int(os.environ.get("MAX_SECONDS", "7200")))),
        # Board reconciler settings (Task A); True/done are the defaults.
        "board_reconcile_enabled": _get_global_setting("board_reconcile_enabled", True),
        "board_reconcile_on_match": _get_global_setting("board_reconcile_on_match", "done"),
        # Card 43665f: board card model default (empty string = use sonnet).
        "board_card_model": _get_global_setting("board_card_model", "") or "",
    }
    # Build spec; enum specs use "allowed" list instead of min/max.
    spec: dict = {}
    for k, v in _GLOBAL_SETTINGS_SPEC.items():
        typ, lo, hi = v
        if typ == "enum":
            spec[k] = {"type": typ, "allowed": list(lo), "min": None, "max": None}
        else:
            spec[k] = {"type": typ, "min": lo, "max": hi}
    return web.json_response({"stored": stored, "effective": effective, "spec": spec})


async def api_settings_post(req: web.Request) -> web.Response:
    """POST /api/settings — partial update of global settings (validated)."""
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    clean, err = _validate_global_settings(body)
    if err:
        return web.json_response({"error": err}, status=400)
    current = dict(_load_global_settings())
    for k, v in clean.items():
        if v is None:
            current.pop(k, None)   # reset to default
        else:
            current[k] = v
    _save_global_settings(current)
    return web.json_response({"ok": True, "stored": current})


def _project_settings_view(project: dict) -> dict:
    return {
        "git_enabled": _git_enabled(project),
        "model": project.get("model"),
        "notify_on_error": bool(project.get("notify_on_error", False)),
        "log_cmd": project.get("log_cmd") or "",
        "test_cmd": project.get("test_cmd") or "",
        "agents_config": project.get("agents_config") or {},
    }


async def api_project_settings_get(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/settings — per-project settings."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    return web.json_response(_project_settings_view(project))


async def api_project_settings_post(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/settings — partial update of per-project settings in topics.json.

    Writes to ALL topics entries with this cwd (like rename). git_enabled and others are
    picked up by hot-reload. Returns the updated settings slice."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)

    updates: dict = {}
    for k, v in body.items():
        if k not in _PROJECT_SETTING_FIELDS:
            return web.json_response({"error": f"unknown key: {k}"}, status=400)
        if k in ("git_enabled", "notify_on_error"):
            if not isinstance(v, bool):
                return web.json_response({"error": f"{k}: expected bool"}, status=400)
            updates[k] = v
        elif k == "model":
            sv = str(v).strip().lower()
            if sv not in _ALLOWED_MODELS:
                return web.json_response({"error": f"model: not in {sorted(_ALLOWED_MODELS)}"}, status=400)
            updates[k] = sv
        elif k == "agents_config":
            if not isinstance(v, dict):
                return web.json_response({"error": "agents_config: expected object"}, status=400)
            clean_cfg: dict = {}
            for cfg_key, cfg_val in v.items():
                if cfg_key in ("executor_model", "researcher_model", "quick_model"):
                    sv2 = str(cfg_val).strip().lower()
                    if sv2 not in _ALLOWED_MODELS:
                        return web.json_response(
                            {"error": f"agents_config.{cfg_key}: model not in {sorted(_ALLOWED_MODELS)}"},
                            status=400,
                        )
                    clean_cfg[cfg_key] = sv2
                elif cfg_key == "conductor_prompt":
                    if not isinstance(cfg_val, bool):
                        return web.json_response(
                            {"error": "agents_config.conductor_prompt: expected bool"},
                            status=400,
                        )
                    clean_cfg[cfg_key] = cfg_val
                else:
                    return web.json_response(
                        {"error": f"agents_config: unknown key {cfg_key!r}"},
                        status=400,
                    )
            updates[k] = clean_cfg if clean_cfg else None
        else:  # log_cmd / test_cmd — strings; empty → reset key
            updates[k] = str(v) if v else None

    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            for k, v in updates.items():
                if v is None:
                    b.pop(k, None)
                else:
                    b[k] = v
            changed += 1
    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    project = _find_project_by_id(ctx, req.match_info["id"]) or project
    return web.json_response({"ok": True, "topics_updated": changed, "settings": _project_settings_view(project)})


async def _send_tg_ping(ctx: dict, project: dict, msg: str) -> None:
    """Sends an HTML message to the project's TG topic. Non-critical."""
    try:
        ptb_app = ctx.get("ptb_app")
        tg_thread_str = project.get("session_key") or project.get("tg_thread", "")
        if ptb_app and tg_thread_str and ":" in str(tg_thread_str):
            chat_s, thread_s = str(tg_thread_str).split(":", 1)
            chat_id = int(chat_s)
            thread_id = int(thread_s) if thread_s.isdigit() else None
            await ptb_app.bot.send_message(
                chat_id, msg, message_thread_id=thread_id, parse_mode="HTML",
            )
    except Exception as e:
        print(f"[tg_ping] TG ping failed: {e}")


async def _sync_forum_topic_name(ctx: dict, session_key: str, name: str) -> None:
    """editForumTopic: syncs the TG topic name with the project name (after rename).
    Non-critical. For synthetic keys (topic doesn't exist) — silently ignored."""
    try:
        ptb_app = ctx.get("ptb_app")
        if not (ptb_app and session_key and ":" in str(session_key)):
            return
        chat_s, thread_s = str(session_key).split(":", 1)
        if not thread_s.isdigit() or int(thread_s) == 0:
            return
        await ptb_app.bot.edit_forum_topic(
            chat_id=int(chat_s), message_thread_id=int(thread_s), name=name,
        )
    except Exception as e:
        print(f"[rename] edit_forum_topic failed (possibly synthetic key): {e}")


async def _notify_new_incidents(ctx: dict, project: dict, n_added: int) -> None:
    """TG ping "crashed": on new incident detection, if notify_on_error is enabled.
    Lists up to 3 incidents from Failed. Non-critical."""
    try:
        _, _, cols = _load_board(project["cwd"])
    except Exception:
        return
    texts = [c["text"] for c in cols.get("failed", []) if _is_incident_card(c)]
    if not texts:
        return
    head = "\n".join(f"• {t[:100]}" for t in texts[:3])
    more = f"\n…and {len(texts) - 3} more" if len(texts) > 3 else ""
    msg = (
        f"❌ <b>{project['name']}</b>: {n_added} new errors\n{head}{more}\n"
        f"<i>Board tab → Failed.</i>"
    )
    await _send_tg_ping(ctx, project, msg)


async def _error_scanner_loop(ctx: dict):
    """Background task: periodically scans all projects with log_cmd."""
    # First run 30s after startup (give the bot time to settle)
    await asyncio.sleep(30)
    while True:
        try:
            projects = _collect_projects(ctx)
            for proj in projects:
                if proj.get("is_free"):
                    continue
                if not proj.get("log_cmd"):
                    continue
                res = await _scan_and_ingest(proj, ctx)
                if res.get("added") or res.get("updated"):
                    print(f"[scanner] {proj['name']}: +{res['added']} new, "
                          f"~{res['updated']} updated (from {res['scanned']} events)")

                # "Crashed" notification: new incidents + notify_on_error enabled
                if res.get("added", 0) and proj.get("notify_on_error"):
                    await _notify_new_incidents(ctx, proj, res.get("added", 0))

        except Exception as e:
            print(f"[scanner] loop error: {e}")
        await asyncio.sleep(int(_get_global_setting("scan_interval_sec", _SCAN_INTERVAL_SEC)))


# _board_payload is imported from board (spec-034 L0)
from board import _board_payload  # noqa: E402 (already imported above; re-stated for clarity)


async def api_project_tasks(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd, name = project["cwd"], project["name"]
    tp = _tasks_path(cwd)
    # Under lock: add ops-markers to cards that lack them (only if file changed).
    # Lock serialises cockpit operations; agent writes directly — on conflict skip the write.
    async with _get_board_lock(cwd):
        raw, preamble, cols = _load_board(cwd)
        if tp.exists():
            canon = _serialize_tasks(preamble, cols, name)
            if canon != raw:
                # Re-read: if agent wrote between _load_board and here — skip.
                try:
                    current = tp.read_text(encoding="utf-8")
                except OSError:
                    current = ""
                if current == raw:
                    # Guard: don't write if parsed card count dropped.
                    # That means the agent wrote something the parser didn't recognise —
                    # overwriting would destroy data. Better to lose a marker than a card.
                    raw_card_count = _count_potential_cards(raw)
                    parsed_card_count = sum(len(v) for v in cols.values())
                    if parsed_card_count < raw_card_count:
                        print(
                            f"[api_project_tasks] WARNING: skipping write to {tp} — "
                            f"parser found {parsed_card_count} cards out of {raw_card_count} "
                            f"potential (agent wrote unrecognised format?)"
                        )
                    else:
                        tp.write_text(canon, encoding="utf-8")
    # F: add card queue to response; annotate has_spec per card (card 5e1c0a)
    payload = _board_payload_with_specs(cwd, ctx["DATA"])
    payload["queued"] = _queue_for((project.get("session_key") or project.get("tg_thread", "")))
    return web.json_response(payload)


async def api_create_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    column = body.get("column", "backlog")
    description = body.get("description") or None
    if description is not None:
        description = str(description).strip() or None
    cwd, name = project["cwd"], project["name"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        if column not in cols:
            column = "backlog"
        new_card: dict = {"id": _new_card_id(), "text": text}
        if description:
            new_card["description"] = description
        cols[column].insert(0, new_card)
        _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))


# _pop_card is imported from board (spec-034 L0)

# ─────────────────────────── F1: card auto-run ───────────────────────────

async def _git_diff_card(cwd: str) -> tuple[str, str]:
    """Returns (diff_full, diff_stat) via asyncio subprocess. Empty strings on error."""
    async def _run(*args):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except Exception:
            return ""
    diff_full, diff_stat = await asyncio.gather(
        _run("diff"),
        _run("diff", "--stat"),
    )
    return diff_full, diff_stat


# ─────────────────────────── C2: worktree helpers ───────────────────────────

async def _card_run_mode(cwd: str, git_enabled: bool = True) -> str:
    """Determines card run mode: 'worktree' or 'legacy'.
    worktree = git enabled AND git repo AND clean working tree. Otherwise — legacy (run in cwd).
    git_enabled=False (project setting) → always legacy, git not touched at all."""
    if not git_enabled:
        return "legacy"
    info = await _git_info(cwd)
    if info is None:
        return "legacy"
    # git status --porcelain: empty output = clean working tree
    status = await _git_cmd(cwd, "status", "--porcelain")
    if status is None or status.strip():
        return "legacy"
    return "worktree"


async def _card_worktree_setup(cwd: str, card_id: str) -> "dict | None":
    """Creates worktree <cwd>/.worktrees/card-<id> on branch card-<id>.
    If it already exists — cleans it first. Returns {wt_path, base_branch} or None on error."""
    try:
        base_branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if not base_branch:
            return None
        wt_path = str(Path(cwd) / ".worktrees" / f"card-{card_id}")
        # Clean up if it already exists (re-run)
        if Path(wt_path).exists():
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
        # Delete branch if it still exists (404-safe)
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-D", f"card-{card_id}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        # Create new worktree
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "worktree", "add", wt_path, "-b", f"card-{card_id}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        if proc.returncode != 0:
            print(f"[worktree_setup] git worktree add failed: {stderr.decode(errors='replace').strip()}")
            return None
        return {"wt_path": wt_path, "base_branch": base_branch}
    except Exception as e:
        print(f"[worktree_setup] error: {e}")
        return None


async def _commit_in_worktree(wt_path: str, card_id: str, prompt: str) -> bool:
    """Auto-commit in worktree. Returns True if a commit was made (there were changes)."""
    try:
        # Check for changes
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if not stdout.decode().strip():
            return False  # no changes
        # git add -A
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "add", "-A",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
        # git commit
        short_prompt = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        commit_msg = f"card {card_id}: {short_prompt}"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "commit", "-m", commit_msg,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15.0)
        return proc.returncode == 0
    except Exception as e:
        print(f"[commit_in_worktree] error: {e}")
        return False


async def _diff_from_worktree(wt_path: str, base_branch: str) -> tuple[str, str]:
    """Returns (diff_full, diff_stat) from worktree vs base_branch."""
    async def _run(*args):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", wt_path, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except Exception:
            return ""
    diff_full, diff_stat = await asyncio.gather(
        _run("diff", f"{base_branch}...HEAD"),
        _run("diff", "--stat", f"{base_branch}...HEAD"),
    )
    return diff_full, diff_stat


def _write_run_meta(data_dir: Path, card_id: str, meta: dict) -> None:
    """Writes machine-readable JSON sidecar DATA/runs/<card_id>.json with run metadata."""
    try:
        runs_dir = data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        (runs_dir / f"{card_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[_write_run_meta] error writing {card_id}.json: {e}")


def _read_run_meta(data_dir: Path, card_id: str) -> "dict | None":
    """Reads run JSON metadata. None if not found or corrupted."""
    try:
        p = data_dir / "runs" / f"{card_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ─────────────────────────── AppCtx TypedDict ───────────────────────────
# Annotation of ctx fields used in _run_card and helpers.
# Runtime is the same dict; TypedDict is for type checking only (mypy/pyright).

class AppCtx(TypedDict, total=False):
    topics: dict
    sessions: dict
    running: dict
    costs: dict
    rate_limits: dict
    DATA: Path
    HERE: Path
    DEFAULT_MODEL: str
    DEFAULT_CWD: str
    VAULT_PROJECTS: Optional[Path]
    password: str
    _auth_token: str
    port: int
    GROUP_CHAT_ID: int
    save_sessions: object   # callable
    save_topics: object     # callable
    resolve_project: object  # callable
    run_engine: object      # async generator factory
    ptb_app: object
    MODELS: dict
    REGISTRY: dict


# ─────────────────────────── _run_card helpers ───────────────────────────


def _build_agents_kwargs(ctx: dict, agents_config: dict) -> dict:
    """Build run_engine keyword args from a project's agents_config dict.

    Delegates to bot._build_agents_kwargs (exposed via ctx) to avoid importing bot directly.
    Returns {} when agents_config is empty or the helper is unavailable (uses run_engine defaults).
    """
    fn = ctx.get("_build_agents_kwargs")
    if fn is None or not agents_config:
        return {}
    return fn(agents_config)


def _write_sidecar(
    data_dir: Path,
    card_id: str,
    name: str,
    prompt: str,
    answer_text: str,
    ok: bool,
    exc_info: str | None,
    diff_stat: str,
    diff_full: str,
    run_mode: str = "legacy",
    wt_branch: str | None = None,
    base_branch: str | None = None,
    wt_path: str | None = None,
    has_changes: bool = False,
) -> None:
    """Writes the card result sidecar to DATA/runs/<card_id>.md
    and machine-readable JSON to DATA/runs/<card_id>.json."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    outcome = "ok" if ok else "fail"
    try:
        runs_dir = data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        sidecar_lines = [
            f"# Card result {card_id}",
            "",
            f"**Project:** {name}",
            f"**Time:** {ts}",
            f"**Outcome:** {outcome}",
            f"**Mode:** {run_mode}",
            "",
            "## Task",
            "",
            prompt,
            "",
            "## Agent response",
            "",
            answer_text,
        ]
        if exc_info:
            sidecar_lines += ["", "## Error", "", f"```\n{exc_info}\n```"]
        if diff_stat:
            sidecar_lines += ["", "## Git diff --stat", "", f"```\n{diff_stat}\n```"]
        if diff_full:
            sidecar_lines += ["", "## Git diff (full)", "", f"```diff\n{diff_full}\n```"]
        (runs_dir / f"{card_id}.md").write_text("\n".join(sidecar_lines), encoding="utf-8")
        # Machine-readable JSON sidecar for apply/discard/frontend
        meta = {
            "card_id": card_id,
            "ts": ts,
            "outcome": outcome,
            "mode": run_mode,
            "branch": wt_branch,
            "base_branch": base_branch,
            "wt_path": wt_path,
            "has_changes": has_changes,
            "applied": False,
            "discarded": False,
        }
        (runs_dir / f"{card_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[_run_card] error writing sidecar {card_id}: {e}")


async def _move_card_after_run(
    ctx: AppCtx,
    cwd: str,
    name: str,
    card: dict,
    card_id: str,
    ok: bool,
) -> None:
    """Moves the card to Review (ok) or Failed (err) under board-lock."""
    try:
        target_col = "review" if ok else "failed"
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            moved = _pop_card(cols, card_id)
            if moved is None:
                moved = card
            cols[target_col].append(moved)
            _save_board(cwd, name, preamble, cols)
    except Exception as e:
        print(f"[_run_card] error moving card {card_id}: {e}")


# spec-039: _notify_tg_rotation, _notify_tg_context_warn, _build_rotation_tail_prompt,
# and _do_session_rotation removed — auto-rotation machinery deleted.
# CONTEXT_ROTATE_AT / CONTEXT_ROTATION / CONTEXT_WARN_AT constants are kept as dead
# no-ops so env-var reads and any remaining references don't break.


async def _notify_tg(ctx: AppCtx, session_key: str, prompt: str, ok: bool) -> None:
    """Sends a ping to the TG topic about card completion. Non-critical — errors are logged."""
    try:
        ptb = ctx.get("ptb_app")
        if ptb is None:
            return
        parts = session_key.split(":", 1)
        chat_id = int(parts[0])
        thread_id = int(parts[1]) if len(parts) > 1 and parts[1] not in ("0", "") else None
        icon = "✅" if ok else "❌"
        short_text = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        target_label = "Review" if ok else "Failed"
        await ptb.bot.send_message(
            chat_id,
            f"{icon} Card '{short_text}' → {target_label}",
            message_thread_id=thread_id,
        )
    except Exception as e:
        print(f"[_run_card] TG ping failed: {e}")


async def _run_card(
    ctx: AppCtx,
    webapp_app,
    project: dict,
    card: dict,
    session_key: str,
    run_mode: str = "legacy",
    wt_info: "dict | None" = None,
) -> None:
    """Background task F1: orchestrator — runs a card via run_engine, writes sidecar, moves the card.

    run_mode: 'worktree' | 'legacy'. wt_info: {wt_path, base_branch} or None.
    """
    run_engine = ctx.get("run_engine")
    cwd = project["cwd"]
    name = project["name"]
    # Card 43665f: model resolution — card override → board_card_model setting → sonnet.
    # Deliberately does NOT use the project model (that is for chat runs).
    model = _effective_card_model(card)
    prompt = card["text"]
    # If description is present — append it to the agent prompt
    card_desc = card.get("description")
    if card_desc:
        prompt = f"{prompt}\n\n{card_desc}"
    card_id = card["id"]
    # Board card: the cockpit ITSELF moves it to Review on success (_move_card_after_run).
    # We tell the agent about the lifecycle so it finishes cleanly and gives a summary for review.
    # The agent must NOT edit TASKS.md manually — that would break canonicalisation/ops-markers.
    prompt = (
        f"{prompt}\n\n[This is board card '{card_id}' in project '{name}'. Complete the task. "
        f"When done — the card will automatically move to Review for human inspection: "
        f"finish the work and end with a BRIEF summary of what was done (it will appear in Review). "
        f"Do NOT edit TASKS.md manually — the cockpit handles the move.]"
    )
    DATA: Path = ctx["DATA"]

    # In worktree mode the agent works in wt_path, otherwise in cwd
    effective_cwd = wt_info["wt_path"] if (run_mode == "worktree" and wt_info) else cwd

    # Spec-021 Part 2: cwd-lock — prevent two runs in the same working directory
    # (different session_keys, same cwd — e.g. two projects pointing to same dir).
    _cwd_lock_key = effective_cwd
    cwd_locks = ctx.get("cwd_locks")
    if cwd_locks is None:
        # Lazy init if start() hasn't added it yet
        ctx["cwd_locks"] = {}
        cwd_locks = ctx["cwd_locks"]
    if cwd_locks.get(_cwd_lock_key):
        print(f"[_run_card] cwd locked by another run: {_cwd_lock_key} — skipping card {card_id}")
        return
    cwd_locks[_cwd_lock_key] = True

    answer_parts: list[str] = []
    exc_info: str | None = None
    ok = False
    has_changes = False
    _card_last_result_event: "dict | None" = None  # Phase D: track for auto-resume
    # Spec-029 item 3: holds parsed structured_output dict when STRUCTURED_CARDS=1 and valid.
    _card_structured_output: "dict | None" = None

    try:
        try:
            if run_engine is None:
                raise RuntimeError("run_engine not available in ctx (old launch without F1)")

            # Publish run start to the bus (activity-stream subscribers will see it live)
            _bus_publish(session_key, {
                "kind": "run_start",
                "source": "card",
                "prompt": prompt,
                "run_id": card_id,
            })

            # Spec-021 Part 2: cards always start fresh — never resume the shared chat session.
            # This isolates card context from chat history (and from other cards).
            resume_sid = None
            # Project secrets — only from cwd of the main project (not worktree), isolated by cwd.
            # secret: references are resolved against the built-in store before injecting into the agent env.
            project_secrets = await _resolve_secret_refs(_secrets_read(cwd))
            # Spec-038: inject cockpit media env (same pattern as api_project_chat).
            # Guard: project may lack "id" in test/minimal contexts — skip media injection then.
            _proj_id = project.get("id")
            if _proj_id:
                _card_media_dir = ctx["DATA"] / "chat-media" / _proj_id
                _card_media_dir.mkdir(parents=True, exist_ok=True)
                project_secrets = {
                    **project_secrets,
                    "COPS_PROJECT_ID": _proj_id,
                    "COPS_MEDIA_DIR": str(_card_media_dir),
                }
            agents_config = project.get("agents_config") or {}
            agents_kwargs = _build_agents_kwargs(ctx, agents_config)
            # ephemeral=True: cards are always isolated — they MUST NOT reuse a live client
            # from the shared chat session (different cwd, synthetic session key).
            # Spec-029 item 3: request structured output only when STRUCTURED_CARDS=1.
            _card_output_fmt = _CARD_OUTPUT_SCHEMA if STRUCTURED_CARDS else None
            async for event in run_engine(
                project_name=name,
                cwd=effective_cwd,
                prompt=prompt,
                session_key=session_key,
                model=model,
                resume_session_id=resume_sid,
                env=project_secrets,
                **agents_kwargs,
                ctx=ctx,
                ephemeral=True,
                output_format=_card_output_fmt,
            ):
                etype = event["type"]
                if etype == "text":
                    answer_parts.append(event["text"])
                    _bus_publish(session_key, {"kind": "text", "text": event["text"], "run_id": card_id})
                elif etype == "text_delta":
                    pass  # card runner: ignore streaming deltas — answer built from finalized {type:"text"} blocks
                elif etype == "tool":
                    inp = event.get("input") or {}
                    tool_data = _format_tool(event.get("name", "?"), inp if isinstance(inp, dict) else {})
                    _bus_publish(session_key, {
                        "kind": "tool",
                        "run_id": card_id,
                        "tool": tool_data,
                    })
                elif etype == "result":
                    _card_last_result_event = event  # Phase D: capture for auto-resume
                    # Spec-021 Part 2: do NOT write card session_id back to ctx["sessions"].
                    # Cards are isolated — each card run is its own fresh session.
                    # Spec-029 item 3: extract structured_output when STRUCTURED_CARDS=1.
                    # Fallback: if absent/malformed/wrong-type, _card_structured_output stays None
                    # and the prose path (answer_parts) is used — zero regression risk.
                    if STRUCTURED_CARDS:
                        _raw_so = event.get("structured_output")
                        if isinstance(_raw_so, dict) and "summary" in _raw_so and "status" in _raw_so:
                            _card_structured_output = _raw_so
                        elif _raw_so is not None:
                            print(f"[_run_card] structured_output malformed for {card_id}: {_raw_so!r} — falling back to prose")
                elif etype == "error":
                    raise event["exc"]

            ok = True

        except Exception as e:
            exc_info = f"{type(e).__name__}: {e}\n\n{_tb.format_exc()}"

        # Worktree: auto-commit + diff from branch; legacy: diff from cwd
        if run_mode == "worktree" and wt_info:
            wt_path = wt_info["wt_path"]
            base_branch = wt_info["base_branch"]
            has_changes = await _commit_in_worktree(wt_path, card_id, prompt)
            if has_changes:
                diff_full, diff_stat = await _diff_from_worktree(wt_path, base_branch)
            else:
                diff_full, diff_stat = "", ""
            wt_branch = f"card-{card_id}"
            wt_path_val = wt_path
        else:
            # legacy: git diff from working tree
            diff_full, diff_stat = await _git_diff_card(cwd)
            has_changes = bool(diff_full or diff_stat)
            wt_path_val = None
            base_branch = None
            wt_branch = None

        # sidecar DATA/runs/<card_id>.md + JSON meta
        # Spec-029 item 3: prefer structured summary when available and STRUCTURED_CARDS=1.
        # Fallback: prose from answer_parts (always populated; structured path is additive only).
        if _card_structured_output is not None:
            _so_summary = str(_card_structured_output.get("summary", "")).strip()
            _so_status = _card_structured_output.get("status", "")
            _so_changes = _card_structured_output.get("changes") or []
            _changes_text = ("\n\nChanges:\n" + "\n".join(f"- {c}" for c in _so_changes)) if _so_changes else ""
            answer_text = (
                f"[{_so_status.upper()}] {_so_summary}{_changes_text}"
                if _so_summary else
                "\n".join(answer_parts).strip() or "(agent finished without a text response)"
            )
        else:
            answer_text = "\n".join(answer_parts).strip() or "(agent finished without a text response)"
        _write_sidecar(
            DATA, card_id, name, prompt, answer_text, ok, exc_info, diff_stat, diff_full,
            run_mode=run_mode,
            wt_branch=wt_branch,
            base_branch=base_branch,
            wt_path=wt_path_val,
            has_changes=has_changes,
        )

        # move card (reload board — may have changed while agent was running)
        await _move_card_after_run(ctx, cwd, name, card, card_id, ok)

        # TG ping (non-critical)
        await _notify_tg(ctx, session_key, prompt, ok)

        # Phase D: auto-resume if run was killed by rate-limit
        _resume_sid = ctx["sessions"].get(session_key)
        await _maybe_auto_resume(
            ctx=ctx,
            session_key=session_key,
            original_prompt=card.get("text", prompt),
            last_result_event=_card_last_result_event,
            resume_session_id=_resume_sid,
        )

    finally:
        # Publish run completion to bus (BEFORE releasing the lock)
        _bus_publish(session_key, {
            "kind": "run_end",
            "outcome": "ok" if ok else "fail",
            "run_id": card_id,
        })
        # lock is released UNCONDITIONALLY, even if sidecar write/move failed
        ctx["running"].pop(session_key, None)
        # Spec-021 Part 2: release cwd-lock unconditionally
        ctx.get("cwd_locks", {}).pop(_cwd_lock_key, None)

    # D: after releasing the lock — drain the queue (next card, if any)
    try:
        _aiohttp_app = ctx.get("_aiohttp_app")
        if _aiohttp_app is not None:
            await _drain_queue(ctx, _aiohttp_app, project)
    except Exception as _dq_exc:
        print(f"[_run_card] _drain_queue error: {_dq_exc}")


# ─────────────────────────── Card Queue: _start_card_run / _drain_queue ───────────────────────────


async def _start_card_run(ctx: AppCtx, app, project: dict, card_id: str) -> dict:
    """Reusable, race-safe: reserves lock SYNCHRONOUSLY, moves card to in_progress,
    launches _run_card in background. Returns {"started": bool, ...}.

    Race-safety guarantee: check AND set of ctx["running"][session_key] happen
    without a single await between them — this is the only guard against double-start.
    """
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    cwd = project["cwd"]
    name = project["name"]

    # run_engine absent — degraded mode
    if ctx.get("run_engine") is None:
        return {"started": False, "reason": "no_engine"}

    # ── SYNCHRONOUS check+reserve (NO await between check and set) ──
    if ctx["running"].get(session_key) is not None:
        return {"started": False, "reason": "busy"}
    ctx["running"][session_key] = True
    # ── end of critical section ──

    # Move card under board-lock
    card = None
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            ctx["running"].pop(session_key, None)
            return {"started": False, "reason": "not_found"}
        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

    # C2: mode + worktree
    run_mode = await _card_run_mode(cwd, git_enabled=_git_enabled(project))
    wt_info: dict | None = None
    if run_mode == "worktree":
        wt_info = await _card_worktree_setup(cwd, card_id)
        if wt_info is None:
            run_mode = "legacy"

    _spawn_bg(_run_card(ctx, app, project, card, session_key, run_mode=run_mode, wt_info=wt_info))
    return {"started": True, "card_id": card_id}


async def _drain_queue(ctx: AppCtx, app, project: dict) -> "str | None":
    """Tries to launch the next card from the queue.
    If project is busy — returns None. Skips stale/missing cards.
    Returns card_id if a run was started, otherwise None.
    """
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    cwd = project["cwd"]

    # Fast non-await check: busy → do nothing
    if ctx["running"].get(session_key) is not None:
        return None

    # Runnable columns (card must be in one of these to be launched)
    _RUNNABLE = {"backlog", "review", "failed"}

    q = _queue_for(session_key)
    for card_id in q:
        # Load board
        try:
            _, _, cols = _load_board(cwd)
        except Exception:
            return None

        # Orphan-guard: if someone is hanging in in_progress (incl. orphan after restart
        # when running-lock was lost but card stayed in the column) — don't start a second.
        if cols.get("in_progress"):
            return None

        # Check that the card still exists in a runnable column
        found_runnable = any(
            c["id"] == card_id
            for col_key, col_cards in cols.items()
            if col_key in _RUNNABLE
            for c in col_cards
        )
        if not found_runnable:
            # Stale or moved entry — remove from queue, try next
            _queue_remove(session_key, card_id)
            continue

        # Try to launch
        result = await _start_card_run(ctx, app, project, card_id)
        if result["started"]:
            _queue_remove(session_key, card_id)
            return card_id
        elif result.get("reason") == "busy":
            # Race — leave in queue, try later
            return None
        else:
            # not_found or no_engine — remove stale and try next
            # (stale first entry must not block a valid one)
            _queue_remove(session_key, card_id)
            continue

    return None


async def api_move_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    to = body.get("to", "")
    cwd, name = project["cwd"], project["name"]

    # ── F1: auto-launch on move to in_progress ──
    if to == "in_progress":
        session_key = (project.get("session_key") or project.get("tg_thread", ""))
        run_engine = ctx.get("run_engine")

        # Degraded mode: if engine is unavailable (old launch) — behave as plain move
        if run_engine is None:
            print("[api_move_task] run_engine not in ctx — degrading to manual move")
            async with _get_board_lock(cwd):
                _, preamble, cols = _load_board(cwd)
                card = _pop_card(cols, card_id)
                if card is None:
                    return web.json_response({"error": "card not found"}, status=404)
                cols["in_progress"].append(card)
                _save_board(cwd, name, preamble, cols)
            return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))

        # Use _start_card_run (race-safe: lock reserved synchronously inside)
        result = await _start_card_run(ctx, req.app, project, card_id)
        if result["started"]:
            return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))
        elif result.get("reason") == "busy":
            # Project busy — queue the card instead of 409.
            # "enqueued":True signals queuing; board["queued"] is the current queue list
            # (don't overwrite it with the flag).
            _queue_enqueue(session_key, card_id)
            board = _board_payload_with_specs(cwd, ctx["DATA"])
            board["queued"] = _queue_for(session_key)
            return web.json_response({**board, "ok": True, "enqueued": True})
        else:
            # not_found or no_engine
            reason = result.get("reason", "unknown")
            if reason == "not_found":
                return web.json_response({"error": "card not found"}, status=404)
            return web.json_response({"error": reason}, status=400)

    # ── Regular move (backlog / review / failed / done) ──
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        card = _pop_card(cols, card_id)
        if card is None:
            return web.json_response({"error": "card not found"}, status=404)

        if to == "done":
            # Spec-012 Ph0 Task B: err-card moved to Done → record as dismissed
            if card_id.startswith("err-"):
                _dismissed_add(card_id[4:])
            dp = _done_path(cwd)
            header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
            if not header.strip():
                header = f"# Done — {name}\n"
            stamp = time.strftime("%Y-%m-%d")
            new = header.rstrip() + f"\n- [x] {card['text']} · {stamp}\n"
            dp.write_text(new, encoding="utf-8")
            _save_board(cwd, name, preamble, cols)
        elif to in cols:
            cols[to].append(card)
            _save_board(cwd, name, preamble, cols)
        else:
            cols["backlog"].append(card)
            _save_board(cwd, name, preamble, cols)
            return web.json_response({"error": "unknown column"}, status=400)
    # F: card manually moved out of queue — remove from queue
    _queue_remove(session_key, card_id)
    return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))


async def api_delete_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    cwd, name = project["cwd"], project["name"]
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        if _pop_card(cols, card_id) is None:
            return web.json_response({"error": "card not found"}, status=404)
        # Spec-012 Ph0 Task B: err-card deleted → record as dismissed
        if card_id.startswith("err-"):
            _dismissed_add(card_id[4:])
        _save_board(cwd, name, preamble, cols)
    # F: card deleted — remove from queue
    _queue_remove(session_key, card_id)
    return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))


async def api_run_batch(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/cards/run-batch — queues multiple cards.
    Body: {"card_ids": ["id1", "id2", ...]}.
    Response: {"ok": True, "queued": <N queued>, "started": <card_id or null>}.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    raw_ids = body.get("card_ids")
    if not isinstance(raw_ids, list):
        return web.json_response({"error": "card_ids must be a list"}, status=400)

    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    cwd = project["cwd"]

    # Runnable columns — card must be in one of these
    _RUNNABLE = {"backlog", "review", "failed"}

    # Load board once
    try:
        _, _, cols = _load_board(cwd)
    except Exception:
        cols = {key: [] for key, _, _ in BOARD_COLUMNS}

    # Build set of all runnable card_ids
    runnable_ids: set = set()
    for col_key, col_cards in cols.items():
        if col_key in _RUNNABLE:
            for c in col_cards:
                runnable_ids.add(c["id"])

    enqueued = 0
    for raw_id in raw_ids:
        if not isinstance(raw_id, str):
            continue
        if not _valid_card_id(raw_id):
            continue
        if raw_id not in runnable_ids:
            continue
        # _queue_enqueue → True only if actually added (dedup → False) — don't overcounting
        if _queue_enqueue(session_key, raw_id):
            enqueued += 1

    # Drain immediately — first card starts if project is free
    _aiohttp_app = ctx.get("_aiohttp_app") or req.app
    started_id = await _drain_queue(ctx, _aiohttp_app, project)

    return web.json_response({"ok": True, "queued": enqueued, "started": started_id})


async def _queue_drain_loop(ctx: dict) -> None:
    """E: Backstop loop: every _QUEUE_DRAIN_INTERVAL_SEC checks all projects with a queue
    and drains them. Handles: restart (queue survived it), TG interleave
    (TG run freed the project — drain not triggered via _run_card).
    """
    await asyncio.sleep(10)  # give the bot time to settle
    while True:
        try:
            _aiohttp_app = ctx.get("_aiohttp_app")
            if _aiohttp_app is not None:
                projects = _collect_projects(ctx)
                for proj in projects:
                    # per-project try/except: failure in one project doesn't take down the rest
                    try:
                        if proj.get("is_free"):
                            continue
                        session_key = (proj.get("session_key") or proj.get("tg_thread", ""))
                        if not _queue_for(session_key):
                            continue
                        await _drain_queue(ctx, _aiohttp_app, proj)
                    except Exception as pe:
                        print(f"[queue_drain_loop] project {proj.get('name')} error: {pe}")
        except Exception as e:
            print(f"[queue_drain_loop] error: {e}")
        await asyncio.sleep(_QUEUE_DRAIN_INTERVAL_SEC)


async def api_update_task(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    # description: if the key is present — update it (None = delete, string = set)
    update_description = "description" in body
    description = body.get("description")
    if description is not None:
        description = str(description).strip() or None
    # model: optional per-card override (Card 43665f). Empty/absent = clear override.
    update_model = "model" in body
    card_model: str | None = None
    if update_model:
        raw_model = (body.get("model") or "").strip().lower()
        if raw_model and raw_model not in _ALLOWED_MODELS:
            return web.json_response(
                {"error": f"model: must be one of {sorted(_ALLOWED_MODELS)} or empty to clear"},
                status=400,
            )
        card_model = raw_model or None  # "" → clear override
    cwd, name = project["cwd"], project["name"]
    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        found = False
        for col_cards in cols.values():
            for card in col_cards:
                if card["id"] == card_id:
                    card["text"] = text
                    if update_description:
                        if description:
                            card["description"] = description
                        else:
                            card.pop("description", None)
                    if update_model:
                        if card_model:
                            card["model"] = card_model
                        else:
                            card.pop("model", None)
                    found = True
                    break
            if found:
                break
        if not found:
            return web.json_response({"error": "card not found"}, status=404)
        _save_board(cwd, name, preamble, cols)
    return web.json_response(_board_payload_with_specs(cwd, ctx["DATA"]))


async def api_tasks_done(req: web.Request) -> web.Response:
    """Contents of the DONE.md archive — loaded on demand (sessions don't read it)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    dp = _done_path(project["cwd"])
    content = dp.read_text(encoding="utf-8", errors="replace") if dp.exists() else ""
    return web.json_response({"content": content, "exists": dp.exists()})


# ─────────────────────────── activity-stream SSE ───────────────────────────
#
# GET /api/projects/{id}/activity-stream  — project event stream (session-specific)
# GET /api/activity-stream                — global stream of all sessions
# Client holds the connection; finally guarantees unsubscription on disconnect.

async def _sse_stream(
    req: web.Request,
    q: "asyncio.Queue[dict]",
    unsubscribe,
    replay_events: "list | None" = None,
) -> web.StreamResponse:
    """Shared SSE loop: reads from queue q, writes to StreamResponse.
    unsubscribe — callable(q) for GUARANTEED unsubscription in finally.
    replay_events — optional list of seq-tagged events to emit before entering the live loop
    (spec-035 reconnect replay). Events with a 'seq' field are emitted with SSE 'id:' prefix."""
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)
    try:
        # Spec-035 L2: replay buffered events for reconnecting clients
        if replay_events:
            for rev in replay_events:
                payload = json.dumps(rev, ensure_ascii=False)
                seq = rev.get("seq")
                if seq is not None:
                    await resp.write(f"id: {seq}\ndata: {payload}\n\n".encode())
                else:
                    await resp.write(f"data: {payload}\n\n".encode())
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25.0)
                payload = json.dumps(event, ensure_ascii=False)
                seq = event.get("seq")
                if seq is not None:
                    await resp.write(f"id: {seq}\ndata: {payload}\n\n".encode())
                else:
                    await resp.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                # Heartbeat — keep the connection alive through a tunnel (Cloudflare / nginx).
                # Client may have dropped — write would then raise ConnectionResetError; this is normal,
                # NOT an incident (heartbeat-write was unguarded before → leaked into error_middleware).
                try:
                    await resp.write(b": ping\n\n")
                except (ConnectionResetError, ConnectionAbortedError):
                    break
            except (ConnectionResetError, ConnectionAbortedError):
                break
            except asyncio.CancelledError:
                break
            except Exception:
                break
    finally:
        unsubscribe(q)
    return resp


async def api_project_activity_stream(req: web.Request) -> web.StreamResponse:
    """GET /api/projects/{id}/activity-stream — bus event stream for a specific project.
    Spec-035 L2: supports reconnect replay via Last-Event-ID header or ?since= query param."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    # Spec-035: resolve reconnect cursor from Last-Event-ID header or ?since= query param
    cursor_str = req.headers.get("Last-Event-ID") or req.rel_url.query.get("since")
    replay_events: list = []
    if cursor_str is not None:
        try:
            cursor = int(cursor_str)
            turn = _live_turns.get(session_key)
            if turn is not None:
                replay_events = [e for e in turn["events"] if e["seq"] > cursor]
        except (ValueError, TypeError):
            pass  # invalid cursor — skip replay
    q = _bus_subscribe(session_key)
    return await _sse_stream(req, q, lambda q: _bus_unsubscribe(session_key, q), replay_events=replay_events)


async def api_activity_stream_all(req: web.Request) -> web.StreamResponse:
    """GET /api/activity-stream — unified stream of ALL bus events (unread indicators in sidebar)."""
    q = _bus_subscribe_global()
    return await _sse_stream(req, q, _bus_unsubscribe_global)


async def api_project_live(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/live — snapshot of the current (or last) LiveTurn buffer.

    Spec-035 L3: returns {running, turn_id, started_at, model, cost_usd, cursor, events}.
    cursor = latest seq in the buffer; clients subscribe from this point to avoid duplicate events.
    events = all buffered events in chronological order (oldest to newest).
    Retained for 300 s after turn completion so cold-open UIs can replay the full turn.
    """
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    running = ctx["running"].get(session_key) is not None
    turn = _live_turns.get(session_key)
    pending_handoff = (ctx.get("pending_handoff") or {}).get(session_key) or None
    if turn is None:
        return web.json_response({
            "running": running,
            "turn_id": None,
            "started_at": None,
            "model": None,
            "cost_usd": None,
            "cursor": 0,
            "events": [],
            "pending_handoff": pending_handoff,
        })
    events_list = list(turn["events"])
    cursor = events_list[-1]["seq"] if events_list else turn["seq"]
    try:
        return web.json_response({
            "running": running,
            "turn_id": turn["turn_id"],
            "started_at": turn["started_at"],
            "model": turn["model"],
            "cost_usd": turn["cost_usd"],
            "cursor": cursor,
            "events": events_list,
            "pending_handoff": pending_handoff,
        })
    except (TypeError, ValueError):
        # Secondary defence: if an event payload is still not JSON-safe despite the
        # buffering-site coercion above, sanitise by converting non-serialisable values
        # to their string representation so the endpoint never returns a 500.
        import json as _json

        def _safe(obj):
            if isinstance(obj, dict):
                return {k: _safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_safe(v) for v in obj]
            try:
                _json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)

        safe_events = [_safe(e) for e in events_list]
        return web.json_response({
            "running": running,
            "turn_id": turn["turn_id"],
            "started_at": turn["started_at"],
            "model": turn["model"],
            "cost_usd": turn["cost_usd"],
            "cursor": cursor,
            "events": safe_events,
            "pending_handoff": pending_handoff,
        })


# ─────────────────────────── timeline read endpoint ───────────────────────────
#
# GET /api/projects/{id}/timeline?limit=N&before=<ts>
# Reads DATA/timeline/<slug>.jsonl (+ .jsonl.1 for older history).
# Returns array of events in chronological order (newest at the bottom).
# Pagination: before=<ts> — events with ts < before only.
# Corrupted JSONL lines → skip (graceful).

_TIMELINE_DEFAULT_LIMIT = 200
_TIMELINE_MAX_LIMIT = 500


def _timeline_read_events(session_key: str, limit: int, before: float | None) -> list[dict]:
    """Reads events from JSONL (current file + .1 for older history).
    Returns list of events in chronological order, ≤ limit items,
    with before — only events with ts < before."""
    path = _timeline_path(session_key)
    if path is None or not isinstance(path, Path):
        return []

    # Gather lines from both files: .1 (older) first, then current
    files: list[Path] = []
    backup = path.with_suffix(".jsonl.1")
    if backup.exists():
        files.append(backup)
    if path.exists():
        files.append(path)

    events: list[dict] = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue  # graceful: corrupted line → skip
                    if not isinstance(obj, dict):
                        continue
                    ts = obj.get("ts")
                    if before is not None and isinstance(ts, (int, float)) and ts >= before:
                        continue
                    events.append(obj)
        except Exception:
            continue

    # Sort chronologically by ts (newest at the bottom)
    events.sort(key=lambda e: e.get("ts", 0))
    # Take the last `limit`
    return events[-limit:]


async def api_project_timeline(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/timeline?limit=N&before=<ts> — project event history."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    try:
        limit = int(req.rel_url.query.get("limit", _TIMELINE_DEFAULT_LIMIT))
        limit = max(1, min(limit, _TIMELINE_MAX_LIMIT))
    except (ValueError, TypeError):
        limit = _TIMELINE_DEFAULT_LIMIT

    before: float | None = None
    before_str = req.rel_url.query.get("before")
    if before_str:
        try:
            before = float(before_str)
        except (ValueError, TypeError):
            pass

    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    events = _timeline_read_events(str(session_key), limit, before)
    return web.json_response({"events": events})


# ─────────────────────────── free chats (not bound to a project) ───────────────────────────
#
# Free chat — virtual "project" with cwd=$HOME, no git, no TG binding.
# Each click on "new free" creates a separate tab with its own session_id.
# Stored in data/free_chats.json: {free-<uuid>: {label, cwd, model, created_at}}.

_FREE_DEFAULT_CWD = str(Path.home())


async def api_free_create(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        body = {}
    cwd = (body.get("cwd") or _FREE_DEFAULT_CWD).rstrip("/")
    model = (body.get("model") or _effective_default_model(ctx)).strip().lower()
    if model not in _ALLOWED_MODELS:
        model = _effective_default_model(ctx)

    # Label — user-supplied or auto "Free HH:MM"
    label = (body.get("label") or "").strip()
    if not label:
        label = f"Free {time.strftime('%H:%M')}"

    fid = f"free-{_uuid.uuid4().hex[:8]}"
    free = _load_free_chats(ctx)
    free[fid] = {
        "label": label,
        "cwd": cwd,
        "model": model,
        "created_at": time.time(),
    }
    _save_free_chats(ctx, free)
    return web.json_response({"id": fid, **free[fid]})


async def api_free_rename(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    fid = req.match_info["id"]
    free = _load_free_chats(ctx)
    if fid not in free:
        return web.json_response({"error": "free chat not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    label = (body.get("label") or "").strip()
    if not label:
        return web.json_response({"error": "label is empty"}, status=400)
    if len(label) > 100:
        label = label[:100]
    free[fid]["label"] = label
    _save_free_chats(ctx, free)

    # If the tab already has an active Claude session — propagate the same label to it,
    # so renaming the tab automatically renames the session in SessionSelector as well.
    active_sid = ctx["sessions"].get(fid)
    if active_sid:
        labels = _load_session_labels(ctx)
        labels[active_sid] = label
        _save_session_labels(ctx, labels)

    return web.json_response({"ok": True, "id": fid, "label": label})


async def api_free_delete(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    fid = req.match_info["id"]
    free = _load_free_chats(ctx)
    if fid not in free:
        return web.json_response({"error": "free chat not found"}, status=404)

    # Cannot delete if a run is in progress — client must stop it first
    if ctx["running"].get(fid) is not None:
        return web.json_response({"error": "chat is busy, stop it first"}, status=409)

    free.pop(fid)
    _save_free_chats(ctx, free)
    # Clean up session_id if one existed
    if ctx["sessions"].pop(fid, None) is not None:
        save = ctx.get("save_sessions")
        if callable(save):
            save()
    return web.json_response({"ok": True})


# ─────────────────────────── Claude Code subscription limits ───────────────────────────
#
# GET /api/usage  → current snapshot of subscription limits (5h window, weekly, opus/sonnet, overage).
# Source of truth for PERCENTAGES — the official oauth endpoint https://api.anthropic.com/api/oauth/usage
# (the same one hit by `/usage` in Claude Code itself). Passive RateLimitEvent from SDK gives only
# status+resets_at, WITHOUT utilization for this subscription (verified 2026-05-30) — that's why % was missing.
# Token taken from ~/.claude/.credentials.json (SDK refreshes it automatically). Cache 60s — frontend polls every 30s.

_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# CLAUDE_CREDENTIALS_PATH: path to Claude OAuth credentials file.
# Defaults to ~/.claude/.credentials.json (the standard Claude CLI location).
# Override only when running multiple Claude accounts or in non-standard setups.
_CREDS_PATH = os.path.expanduser(
    os.environ.get("CLAUDE_CREDENTIALS_PATH", "") or "~/.claude/.credentials.json"
)
_usage_cache: dict = {"data": None, "ts": 0.0}
_usage_lock: asyncio.Lock | None = None  # lazy — created inside the running event loop
_USAGE_TTL = 60.0


def _get_usage_lock() -> asyncio.Lock:
    """Returns the module-level usage lock, creating it lazily inside the running loop."""
    global _usage_lock
    if _usage_lock is None:
        _usage_lock = asyncio.Lock()
    return _usage_lock


def _read_oauth_token() -> str | None:
    try:
        with open(_CREDS_PATH) as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def _iso_to_unix(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def _norm_window(d):
    """OAuth window {utilization:0-100, resets_at:ISO} → frontend format {utilization:0-1, resets_at:unix}."""
    if not isinstance(d, dict):
        return None
    util = d.get("utilization")
    return {
        "status": "allowed",
        "resets_at": _iso_to_unix(d.get("resets_at")),
        "utilization": (util / 100.0) if isinstance(util, (int, float)) else None,
        "ts": time.time(),
    }


async def _fetch_oauth_usage():
    token = _read_oauth_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(_OAUTH_USAGE_URL, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None


async def api_usage(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    now = time.time()
    async with _get_usage_lock():
        cached = _usage_cache["data"]
        if cached is None or (now - _usage_cache["ts"]) > _USAGE_TTL:
            raw = await _fetch_oauth_usage()
            if raw is not None:
                limits = {}
                for k in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
                    nv = _norm_window(raw.get(k))
                    if nv:
                        limits[k] = nv
                eu = raw.get("extra_usage")
                if isinstance(eu, dict) and eu.get("is_enabled") and eu.get("utilization") is not None:
                    limits["overage"] = {
                        "status": "allowed",
                        "resets_at": None,
                        "utilization": eu["utilization"] / 100.0,
                        "ts": now,
                    }
                _usage_cache["data"] = limits
                _usage_cache["ts"] = now
                cached = limits
        # oauth unavailable (no token / 401 / network) → fallback to passive SDK snapshot
        if not cached:
            cached = ctx.get("rate_limits") or {}
    return web.json_response({"limits": cached, "now": time.time()})


# ─────────────────────────── Deferred Runs (Spec 020) ────────────────────────────

async def _maybe_auto_resume(
    ctx: dict,
    session_key: str,
    original_prompt: str,
    last_result_event: "dict | None",
    resume_session_id: "str | None" = None,
    parent_auto_resume_count: int = 0,
) -> None:
    """Phase D: auto-resume after rate-limit.

    Called after any run completes (run_agent, api_project_chat, _run_card,
    _execute_deferred). If the run ended with api_error_status=429 and
    AUTO_RESUME_ON_RATE_LIMIT is enabled, creates a fire_on_reset deferred record
    that will re-run the task once the 5-hour window resets.

    Chain guard: if parent_auto_resume_count >= AUTO_RESUME_MAX, sends a TG
    notification instead of creating another auto-resume record.

    Does nothing (silently) when:
    - AUTO_RESUME_ON_RATE_LIMIT is 0
    - last_result_event is None or api_error_status != 429
    - session_key not found in topics
    - _DEFERRED_FILE is not initialised (deferred system not started)
    """
    if not _AUTO_RESUME_ON_RATE_LIMIT:
        return
    if last_result_event is None:
        return
    if last_result_event.get("api_error_status") != 429:
        return
    if _DEFERRED_FILE is None:
        return

    topics = ctx.get("topics") or {}
    topic = topics.get(session_key)
    if topic is None:
        return

    project_name = topic.get("project", "unknown")

    # Loop guard
    if parent_auto_resume_count >= _AUTO_RESUME_MAX:
        await _notify_operator(
            ctx,
            f"[WARN] Auto-resume limit reached ({_AUTO_RESUME_MAX}) for [{project_name}]. "
            f"Manual restart required."
        )
        return

    # Build continuation prompt (resume_session_id preserves context in the SDK)
    short_original = original_prompt[:200]
    continuation_prompt = (
        f"Continue the interrupted task exactly where you stopped. "
        f"Original request: {short_original}"
    )

    resets_at_str = _get_resets_at_display(ctx)

    record = {
        "id": _new_deferred_id(),
        "project": project_name,
        "session_key": session_key,
        "prompt": continuation_prompt,
        "fire_at": None,
        "fire_on_reset": True,
        "created": _utcnow_iso(),
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
        # Phase D fields
        "auto_resume": True,
        "auto_resume_count": parent_auto_resume_count + 1,
        "resume_session_id": resume_session_id,
        "original_prompt_preview": short_original,
    }
    records = _load_deferred()
    records.append(record)
    _save_deferred(records)

    await _notify_operator(
        ctx,
        f"⏸ {project_name}: rate-limited, auto-resume queued"
        + (f" (resets ~{resets_at_str})" if resets_at_str else "")
        + f" [{record['id']}]"
    )


def _get_resets_at_display(ctx: dict) -> str:
    """Returns a human-readable LA time string for the next rate-limit reset, or ''."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("OPERATOR_TZ", "America/Los_Angeles"))
        # First try the passive rate_limits dict (populated by any recent run_engine call)
        rate_limits = ctx.get("rate_limits") or {}
        resets_at = None
        for _rl_type in ("five_hour", "seven_day"):
            entry = rate_limits.get(_rl_type)
            if entry and entry.get("resets_at"):
                resets_at = entry["resets_at"]
                break
        if resets_at is None:
            return ""
        dt = datetime.fromtimestamp(float(resets_at), tz=tz)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _deferred_init(ctx: dict) -> None:
    global _DEFERRED_FILE
    _DEFERRED_FILE = ctx["DATA"] / "deferred.json"


def _load_deferred() -> list:
    if _DEFERRED_FILE is None or not _DEFERRED_FILE.exists():
        return []
    try:
        return json.loads(_DEFERRED_FILE.read_text())
    except Exception:
        return []


def _save_deferred(records: list) -> None:
    if _DEFERRED_FILE is None:
        return
    tmp = Path(str(_DEFERRED_FILE) + ".tmp")
    tmp.write_text(json.dumps(records, indent=2))
    os.replace(str(tmp), str(_DEFERRED_FILE))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unix_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _new_deferred_id() -> str:
    return "def-" + secrets.token_hex(4)


async def _get_cached_usage_data(ctx: dict) -> dict:
    """Returns cached usage limits dict. Uses the existing _usage_cache; refreshes if stale."""
    now = time.time()
    async with _get_usage_lock():
        cached = _usage_cache["data"]
        if cached is None or (now - _usage_cache["ts"]) > _USAGE_TTL:
            raw = await _fetch_oauth_usage()
            if raw is not None:
                limits: dict = {}
                for k in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
                    nv = _norm_window(raw.get(k))
                    if nv:
                        limits[k] = nv
                _usage_cache["data"] = limits
                _usage_cache["ts"] = now
                cached = limits
        if not cached:
            cached = ctx.get("rate_limits") or {}
    return cached or {}


async def _notify_operator(ctx: dict, text: str) -> None:
    """Surface an operator notification in the cockpit via the global activity bus
    (primary channel since spec-040 removed Telegram). Also sends TG if a bot is
    configured (legacy; skipped when ptb_app is None). Non-critical — errors logged."""
    # In-cockpit toast — derive a level from the message prefix used by callers.
    if text.startswith("[ERROR]"):
        level = "error"
    elif text.startswith("[OK]"):
        level = "success"
    else:
        level = "info"
    try:
        _bus_publish("__notify__", {"kind": "notification", "level": level, "text": text}, persist=False)
    except Exception as e:
        print(f"[deferred] cockpit notify error: {e}")
    # Legacy TG path — no-op after TG removal (ptb_app is None).
    ptb_app = ctx.get("ptb_app")
    if ptb_app is None:
        return
    allowed = os.environ.get("ALLOWED_USERS", "")
    if not allowed:
        return
    for uid_str in allowed.split(","):
        uid_str = uid_str.strip()
        if uid_str.isdigit():
            try:
                await ptb_app.bot.send_message(chat_id=int(uid_str), text=text[:4096])
            except Exception as e:
                print(f"[deferred] notify_operator error: {e}")
            break


async def _execute_deferred(ctx: dict, record: dict) -> None:
    """Execute a single deferred run. Mirrors _run_card logic for run_engine invocation."""
    records = _load_deferred()
    rec = next((r for r in records if r["id"] == record["id"]), None)
    session_key = record["session_key"]
    _deferred_last_result_event: "dict | None" = None  # Phase D: track for auto-resume
    try:
        topics = ctx["topics"]
        topic = topics.get(session_key)
        if topic is None:
            raise ValueError(f"session_key {session_key!r} not found in topics")
        cwd = topic.get("cwd") or ctx.get("DEFAULT_CWD") or str(Path.home())
        project_name = topic.get("project", "unknown")
        prompt = record["prompt"]
        model = topic.get("model") or ctx.get("DEFAULT_MODEL", "sonnet")
        run_engine = ctx.get("run_engine")
        if run_engine is None:
            raise RuntimeError("run_engine not available in ctx")

        project_secrets = _secrets_read(cwd)
        agents_config = topic.get("agents_config") or {}
        agents_kwargs = _build_agents_kwargs(ctx, agents_config)

        _bus_publish(session_key, {
            "kind": "run_start",
            "source": "deferred",
            "prompt": prompt,
            "run_id": record["id"],
        })

        # Phase D: use record's resume_session_id if present (preserves interrupted context)
        resume_sid = record.get("resume_session_id") or ctx["sessions"].get(session_key)

        answer_parts: list = []
        # ephemeral=False: deferred runs share the project's session (same as the chat path).
        async for event in run_engine(
            project_name=project_name,
            cwd=cwd,
            prompt=prompt,
            session_key=session_key,
            model=model,
            resume_session_id=resume_sid,
            env=project_secrets,
            **agents_kwargs,
            ctx=ctx,
            ephemeral=False,
        ):
            etype = event["type"]
            if etype == "text":
                answer_parts.append(event["text"])
                _bus_publish(session_key, {"kind": "text", "text": event["text"], "run_id": record["id"]})
            elif etype == "text_delta":
                pass  # deferred runner: ignore streaming deltas — answer built from finalized {type:"text"} blocks
            elif etype == "result":
                _deferred_last_result_event = event  # Phase D: capture for auto-resume
                if event.get("session_id"):
                    ctx["sessions"][session_key] = event["session_id"]
                    ctx["save_sessions"]()
            elif etype == "error":
                raise event["exc"]

        _bus_publish(session_key, {"kind": "run_end", "outcome": "ok", "run_id": record["id"]})

        result_text = "\n".join(answer_parts).strip()
        if rec:
            rec["status"] = "fired"
            rec["error"] = None
            _save_deferred(records)
        notify_text = (
            f"[OK] Deferred run complete [{record['project']}]: {result_text[:200]}"
            if result_text else
            f"[OK] Deferred run complete [{record['project']}]: {record['prompt'][:80]}..."
        )
        await _notify_operator(ctx, notify_text)

        # Phase D: auto-resume if this deferred run was also killed by rate-limit
        _resume_sid_def = ctx["sessions"].get(session_key)
        parent_count = record.get("auto_resume_count", 0)
        await _maybe_auto_resume(
            ctx=ctx,
            session_key=session_key,
            original_prompt=record.get("original_prompt_preview", prompt),
            last_result_event=_deferred_last_result_event,
            resume_session_id=_resume_sid_def,
            parent_auto_resume_count=parent_count,
        )

    except Exception as e:
        _bus_publish(session_key, {"kind": "run_end", "outcome": "fail", "run_id": record["id"]})
        if rec:
            rec["status"] = "failed"
            rec["error"] = str(e)[:200]
            _save_deferred(records)
        await _notify_operator(ctx, f"[ERROR] Deferred run failed [{record.get('project', '?')}]: {e}")
    finally:
        ctx["running"].pop(session_key, None)


async def _deferred_loop(ctx: dict) -> None:
    """Deferred runs polling loop (Spec 020). Poll interval: DEFERRED_POLL_SEC. Startup delay: 15s."""
    import random as _random
    await asyncio.sleep(15)
    while True:
        try:
            records = _load_deferred()
            changed = False
            for record in records:
                if record.get("status") != "pending":
                    continue
                fire_on_reset = record.get("fire_on_reset", False)
                fire_at = record.get("fire_at")
                fire_now = False

                if fire_on_reset:
                    reset_unknown = False
                    try:
                        usage = await _get_cached_usage_data(ctx)
                        limit = usage.get("five_hour")
                        if limit is None:
                            reset_unknown = True
                        else:
                            util = limit.get("utilization")
                            strict_reset = record.get("strict_reset")
                            if (not strict_reset) and util is not None and util < _DEFERRED_FREE_THRESHOLD:
                                # Free-window shortcut: auto-resume records fire early when
                                # the window is already mostly free. Strict (button-created)
                                # records skip this and wait for the actual reset boundary.
                                fire_now = True
                            else:
                                resets_at = limit.get("resets_at")
                                if resets_at is None:
                                    reset_unknown = True
                                else:
                                    jitter = record.get("_jitter")
                                    if jitter is None:
                                        jitter = _random.randint(30, 90)
                                        record["_jitter"] = jitter
                                        changed = True
                                    fire_now = (time.time() >= resets_at + jitter)
                    except Exception as e:
                        print(f"[deferred_loop] usage fetch error: {e}")
                        reset_unknown = True

                    if reset_unknown and not fire_now:
                        # Fallback: usage API can't tell us the reset boundary. A 5h window always
                        # resets within 5h, so after _DEFERRED_RESET_FALLBACK_SEC the window has
                        # certainly reset — fire anyway instead of leaving the record pending forever.
                        created_ts = _iso_to_unix(record.get("created") or "")
                        elapsed = (time.time() - created_ts) if created_ts else None
                        if elapsed is not None and elapsed >= _DEFERRED_RESET_FALLBACK_SEC:
                            fire_now = True
                            record["fired_via"] = "reset_fallback"
                            record.pop("reset_wait_reason", None)
                            changed = True
                            print(f"[deferred_loop] {record['id']}: reset boundary unavailable for {int(elapsed)}s — firing via fallback")
                        else:
                            if record.get("reset_wait_reason") != "usage_unavailable":
                                record["reset_wait_reason"] = "usage_unavailable"
                                changed = True
                            _el = int(elapsed) if elapsed is not None else "?"
                            print(f"[deferred_loop] {record['id']}: reset boundary unavailable, waiting for fallback (elapsed={_el}s / {_DEFERRED_RESET_FALLBACK_SEC}s)")
                            continue
                    elif not fire_now and record.get("reset_wait_reason"):
                        # usage healthy again — clear stale stuck reason
                        record.pop("reset_wait_reason", None)
                        changed = True
                elif fire_at:
                    try:
                        ts = _iso_to_unix(fire_at)
                        fire_now = ts is not None and (time.time() >= ts)
                    except Exception:
                        continue
                else:
                    continue

                if not fire_now:
                    continue

                # Check if session is busy
                k = record["session_key"]
                if ctx["running"].get(k):
                    record["attempts"] = record.get("attempts", 0) + 1
                    changed = True
                    if record["attempts"] >= _DEFERRED_MAX_ATTEMPTS:
                        record["status"] = "failed"
                        record["error"] = "project busy after max attempts"
                        _save_deferred(records)
                        changed = False
                        await _notify_operator(
                            ctx,
                            f"[ERROR] Deferred run {record['id']} failed: project busy after {_DEFERRED_MAX_ATTEMPTS} attempts"
                        )
                    else:
                        record["fire_at"] = _unix_to_iso(time.time() + 300)
                        record["fire_on_reset"] = False
                        record.pop("_jitter", None)
                        _save_deferred(records)
                        changed = False
                    continue

                # Fire: mark synchronously before any await
                record["status"] = "fired"
                record["fired_at"] = _utcnow_iso()
                _save_deferred(records)
                changed = False
                await _notify_operator(
                    ctx,
                    f"[START] Starting deferred run [{record['project']}]: {record['prompt'][:80]}..."
                )
                # Reserve running lock synchronously before creating task
                ctx["running"][k] = True
                _spawn_bg(_execute_deferred(ctx, record))

            if changed:
                _save_deferred(records)
        except Exception as e:
            print(f"[deferred_loop] error: {e}")

        await asyncio.sleep(_DEFERRED_POLL_SEC)


async def api_deferred_create(req: web.Request) -> web.Response:
    """POST /api/deferred — queue a deferred run."""
    ctx = req.app["ctx"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    project = (body.get("project") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    fire_at = body.get("fire_at")
    fire_on_reset = body.get("fire_on_reset", False)
    card_id = body.get("card_id")

    if not project:
        return web.json_response({"error": "project required"}, status=400)
    if not prompt:
        return web.json_response({"error": "prompt required"}, status=400)
    if fire_at and fire_on_reset:
        return web.json_response({"error": "provide exactly one of fire_at or fire_on_reset"}, status=400)
    if not fire_at and not fire_on_reset:
        return web.json_response({"error": "provide exactly one of fire_at or fire_on_reset"}, status=400)

    # Resolve project: try by id first (frontend sends basename(cwd)), fall back to display name.
    # _find_project_by_id returns a dict with keys: id, name, cwd, session_key, ...
    proj = _find_project_by_id(ctx, project)
    if proj is not None:
        session_key = (proj.get("session_key") or proj.get("tg_thread", ""))
        project = proj.get("name") or project  # store display name for notifications/list
    else:
        # Fallback: match by display name (TG /later path and tests that pass a display name)
        topics = ctx["topics"]
        session_key = None
        for k, v in topics.items():
            if v.get("project") == project:
                session_key = k
                break
    if session_key is None:
        return web.json_response({"error": f"unknown project: {project}"}, status=400)

    if fire_at:
        if _iso_to_unix(fire_at) is None:
            return web.json_response({"error": "invalid fire_at format (ISO-8601 UTC)"}, status=400)

    record = {
        "id": _new_deferred_id(),
        "project": project,
        "session_key": session_key,
        "prompt": prompt[:4096],
        "fire_at": fire_at if fire_at else None,
        "fire_on_reset": bool(fire_on_reset),
        "created": _utcnow_iso(),
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }
    if card_id is not None:
        record["card_id"] = card_id
    # Explicit user-initiated "after reset" always means the real reset boundary,
    # never the util<10% free-window shortcut (which is reserved for auto-resume).
    if fire_on_reset:
        record["strict_reset"] = True

    records = _load_deferred()
    records.append(record)
    _save_deferred(records)

    trigger_str = "after rate-limit reset" if fire_on_reset else f"at {fire_at}"
    await _notify_operator(ctx, f"[QUEUED] Deferred run queued [{project}] {trigger_str}: {prompt[:80]}...")

    return web.json_response({"id": record["id"], "status": "pending"}, status=201)


async def api_deferred_list(req: web.Request) -> web.Response:
    """GET /api/deferred — list deferred runs with optional filters."""
    records = _load_deferred()
    status_filter = req.rel_url.query.get("status")
    project_filter = req.rel_url.query.get("project")
    if status_filter:
        records = [r for r in records if r.get("status") == status_filter]
    if project_filter:
        records = [r for r in records if r.get("project") == project_filter]
    return web.json_response(records)


async def api_deferred_delete(req: web.Request) -> web.Response:
    """DELETE /api/deferred/{id} — cancel a pending deferred run."""
    deferred_id = req.match_info["id"]
    records = _load_deferred()
    rec = next((r for r in records if r["id"] == deferred_id), None)
    if rec is None:
        return web.json_response({"error": "not found"}, status=404)
    status = rec.get("status")
    if status in ("fired", "failed"):
        return web.json_response({"error": f"already {status}"}, status=409)
    rec["status"] = "cancelled"
    _save_deferred(records)
    return web.json_response({"cancelled": True})


async def api_deferred_update(req: web.Request) -> web.Response:
    """PATCH /api/deferred/{id} — edit a pending deferred run (prompt and/or trigger)."""
    deferred_id = req.match_info["id"]
    records = _load_deferred()
    rec = next((r for r in records if r["id"] == deferred_id), None)
    if rec is None:
        return web.json_response({"error": "not found"}, status=404)
    status = rec.get("status")
    if status != "pending":
        return web.json_response({"error": f"cannot edit: status is {status}"}, status=409)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    # Edit prompt if provided
    if "prompt" in body:
        prompt = (body["prompt"] or "").strip()
        if not prompt:
            return web.json_response({"error": "prompt required"}, status=400)
        rec["prompt"] = prompt[:4096]

    # Edit trigger if provided
    fire_at = body.get("fire_at")
    fire_on_reset = body.get("fire_on_reset")
    has_fire_at = "fire_at" in body and fire_at
    has_fire_on_reset = "fire_on_reset" in body and fire_on_reset

    if has_fire_at and has_fire_on_reset:
        return web.json_response({"error": "provide exactly one of fire_at or fire_on_reset"}, status=400)

    if has_fire_at:
        if _iso_to_unix(fire_at) is None:
            return web.json_response({"error": "invalid fire_at format (ISO-8601 UTC)"}, status=400)
        rec["fire_at"] = fire_at
        rec["fire_on_reset"] = False
        rec.pop("strict_reset", None)
        rec.pop("_jitter", None)
        rec.pop("reset_wait_reason", None)
    elif has_fire_on_reset:
        rec["fire_on_reset"] = True
        rec["fire_at"] = None
        rec["strict_reset"] = True
        rec.pop("_jitter", None)
        rec.pop("reset_wait_reason", None)
    # else: neither trigger key present — leave existing trigger as-is

    # Reset retry state on any successful edit
    rec["attempts"] = 0
    rec["error"] = None

    _save_deferred(records)
    return web.json_response(rec)


# ─────────────────────────── project model change ───────────────────────────
#
# POST /api/projects/{id}/model  {model: "opus"|"sonnet"|"haiku"}
# Updates model in ALL topics with the same cwd (one project may have multiple TG topics),
# persists via save_topics() from ctx. Takes effect on the next request (current session is not touched).

_ALLOWED_MODELS: set[str] = {"opus", "sonnet", "haiku", "fable"}


async def api_project_set_model(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    model = (body.get("model") or "").strip().lower()
    if model not in _ALLOWED_MODELS:
        return web.json_response(
            {"error": f"model must be one of: {', '.join(sorted(_ALLOWED_MODELS))}"},
            status=400,
        )

    # Free chat: model is stored in free_chats.json by its id
    if project.get("is_free"):
        free = _load_free_chats(ctx)
        if project["id"] in free:
            free[project["id"]]["model"] = model
            _save_free_chats(ctx, free)
        return web.json_response({"ok": True, "model": model, "topics_updated": 1})

    # Regular project — update all topics with the same cwd
    cwd = project["cwd"]
    changed = 0
    for b in ctx["topics"].values():
        if b.get("cwd") == cwd:
            b["model"] = model
            changed += 1

    if changed:
        save_topics = ctx.get("save_topics")
        if callable(save_topics):
            save_topics()

    return web.json_response({"ok": True, "model": model, "topics_updated": changed})


# ─────────────────────────── git sync (commit + push) ───────────────────────────
#
# POST /api/projects/{id}/git/sync  {message?: str}
# If there are local changes → git add -A + git commit -m <msg>. Then git push.
# Default message: "wip: YYYY-MM-DD HH:MM" (if message field is empty).
# Returns {ok, committed, pushed, log}; on error status 500 + {error, log}.

async def api_project_upload(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/upload — multipart file → data/inbox/ → {path, name, size}."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    DATA: Path = ctx["DATA"]
    inbox = DATA / "inbox"
    inbox.mkdir(exist_ok=True)

    try:
        reader = await req.multipart()
    except Exception:
        return web.json_response({"error": "expected multipart/form-data"}, status=400)

    field = await reader.next()
    if field is None:
        return web.json_response({"error": "no file field"}, status=400)

    filename = field.filename or "upload"
    safe_name = re.sub(r'[^\w.\-]', '_', filename)
    ts = int(time.time() * 1000)
    dest = inbox / f"web_{ts}_{safe_name}"

    MAX_UPLOAD = 20 * 1024 * 1024
    size = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    return web.json_response({"error": "file too large (max 20 MB)"}, status=413)
                fh.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"path": str(dest), "name": filename, "size": size})


# ──────────────────────── chat media (spec-038) ──────────────────────────────
#
# GET /api/projects/{id}/media/{filename}
# Serves agent-produced screenshots stored under data/chat-media/<project_id>/.
# Auth: inherited from the /api/* cookie middleware (cops_auth).
# Path-traversal guard: rejects any filename containing /, \, or ..; also
# verifies os.path.realpath of the resolved file stays inside the media dir.

_MEDIA_CONTENT_TYPES: dict[str, str] = {
    # Images (spec-038)
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif":  "image/gif",
    # Videos (spec-038 extension)
    "mp4":  "video/mp4",
    "webm": "video/webm",
    "mov":  "video/quicktime",
    "ogg":  "video/ogg",
    "ogv":  "video/ogg",
}


async def api_project_media(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/media/{filename} — serve agent screenshot to the cockpit."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    filename = req.match_info["filename"]

    # Path-traversal guard: reject suspicious filenames before any filesystem access.
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.json_response({"error": "invalid filename"}, status=400)

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = _MEDIA_CONTENT_TYPES.get(ext)
    if content_type is None:
        return web.json_response({"error": "unsupported media type"}, status=415)

    DATA: Path = ctx["DATA"]
    media_dir = DATA / "chat-media" / project["id"]
    target = media_dir / filename

    # Secondary guard: confirm the resolved real path is inside the media dir.
    try:
        real_target = os.path.realpath(str(target))
        real_media  = os.path.realpath(str(media_dir))
    except Exception:
        return web.json_response({"error": "invalid path"}, status=400)
    if not real_target.startswith(real_media + os.sep) and real_target != real_media:
        return web.json_response({"error": "invalid filename"}, status=400)

    if not target.exists():
        return web.json_response({"error": "file not found"}, status=404)

    return web.FileResponse(target, headers={"Content-Type": content_type})


async def api_project_git_sync(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    if not _git_enabled(project):
        return web.json_response({"error": "git disabled for this project (settings)"}, status=409)
    cwd = project["cwd"]

    try:
        body = await req.json()
    except Exception:
        body = {}
    msg = (body.get("message") or "").strip() or f"wip: {time.strftime('%Y-%m-%d %H:%M')}"

    async def _git(*args) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")

    log_parts: list[str] = []
    committed = False
    pushed = False

    # 1. Check status
    rc, status = await _git("status", "--porcelain")
    if rc != 0:
        return web.json_response({"error": "git status failed", "log": status}, status=500)

    # 2. If dirty — stage and commit
    if status.strip():
        rc, out = await _git("add", "-A")
        log_parts.append(f"$ git add -A\n{out}".rstrip())
        if rc != 0:
            return web.json_response({"error": "git add failed", "log": "\n\n".join(log_parts)}, status=500)

        rc, out = await _git("commit", "-m", msg)
        log_parts.append(f"$ git commit -m {msg!r}\n{out}".rstrip())
        if rc != 0:
            return web.json_response({"error": "git commit failed", "log": "\n\n".join(log_parts)}, status=500)
        committed = True

    # 3. Push (even if no commit — there may be local commits not yet pushed)
    rc, out = await _git("push")
    log_parts.append(f"$ git push\n{out}".rstrip())
    if rc != 0:
        return web.json_response({"error": "git push failed", "log": "\n\n".join(log_parts)}, status=500)
    pushed = True

    return web.json_response({
        "ok": True,
        "committed": committed,
        "pushed": pushed,
        "message": msg if committed else None,
        "log": "\n\n".join(log_parts),
    })


# ─────────────────────────── project test runner ───────────────────────────
#
# POST /api/projects/{id}/test → auto-detect test command, run, output to cockpit.
# Detection in decreasing specificity: pytest-cfg/tests/ → npm test → make test.

def _detect_test_cmd(cwd: str):
    """Returns (cmd:list[str], human:str) or None if no test method found."""
    p = Path(cwd)
    # Python / pytest
    has_pytest_cfg = any((p / n).exists() for n in
                         ("pytest.ini", "tox.ini", "setup.cfg")) \
        or (p / "tests").is_dir() or (p / "test").is_dir()
    if (p / "pyproject.toml").exists():
        try:
            if "pytest" in (p / "pyproject.toml").read_text(errors="replace"):
                has_pytest_cfg = True
        except Exception:
            pass
    if has_pytest_cfg:
        if (p / "venv" / "bin" / "pytest").exists():
            return (["venv/bin/pytest", "-q"], "venv/bin/pytest -q")
        if (p / "venv" / "bin" / "python").exists():
            return (["venv/bin/python", "-m", "pytest", "-q"], "venv/bin/python -m pytest -q")
        return (["python3", "-m", "pytest", "-q"], "python3 -m pytest -q")
    # Node
    pkg = p / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(errors="replace"))
            if (data.get("scripts") or {}).get("test"):
                return (["npm", "test", "--silent"], "npm test")
        except Exception:
            pass
    # Make
    mk = p / "Makefile"
    if mk.exists():
        try:
            if re.search(r"^test:", mk.read_text(errors="replace"), re.M):
                return (["make", "test"], "make test")
        except Exception:
            pass
    return None


async def api_project_test(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    cwd = project["cwd"]
    detected = _detect_test_cmd(cwd)
    if detected is None:
        return web.json_response({
            "detected": False, "ok": False, "cmd": None, "exit_code": None,
            "output": "Could not find how to run tests: no pytest config/tests/ dir, "
                      "test script in package.json, or test target in Makefile.",
        })
    cmd, human = detected
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
    except Exception as e:
        return web.json_response({"error": f"launch failed: {e}", "cmd": human}, status=500)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        rc = proc.returncode or 0
        timed_out = False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        out, rc, timed_out = b"", -1, True
    text = out.decode(errors="replace")
    if len(text) > 20000:
        text = "…(beginning truncated)\n" + text[-20000:]
    if timed_out:
        text = (text + "\n⏱ interrupted by 300s timeout").strip()
    return web.json_response({
        "detected": True, "ok": (rc == 0 and not timed_out),
        "cmd": human, "exit_code": rc, "timed_out": timed_out, "output": text,
    })


# ─────────────────────────── quality gate ───────────────────────────────────
#
# _run_quality_gate(wt_path, env) — runs tests IN the card worktree.
# Reuses _detect_test_cmd. Timeout 300s. Verdict: safe/risky/unknown.

_GATE_MAX_OUTPUT = 20_000  # chars


async def _run_quality_gate(wt_path: str, env: "dict | None" = None) -> dict:
    """Runs tests in the card worktree. Returns:
    {verdict:"safe|risky|unknown", tests:{detected, ok, cmd, exit_code, output, timed_out}}.
    Verdict: tests passed→safe, failed/timeout→risky, not found→unknown.
    """
    detected = _detect_test_cmd(wt_path)
    if detected is None:
        return {
            "verdict": "unknown",
            "tests": {
                "detected": False,
                "ok": False,
                "cmd": None,
                "exit_code": None,
                "output": "Test command not found (no pytest config/tests/ dir, npm test, or make test).",
                "timed_out": False,
            },
            "lint": None,
        }

    cmd, human = detected
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=wt_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=run_env,
        )
    except Exception as e:
        return {
            "verdict": "risky",
            "tests": {
                "detected": True,
                "ok": False,
                "cmd": human,
                "exit_code": -1,
                "output": f"Failed to launch tests: {e}",
                "timed_out": False,
            },
            "lint": None,
        }

    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        rc = proc.returncode or 0
        timed_out = False
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        out, rc, timed_out = b"", -1, True

    text = out.decode(errors="replace")
    if len(text) > _GATE_MAX_OUTPUT:
        text = "…(beginning truncated)\n" + text[-_GATE_MAX_OUTPUT:]
    if timed_out:
        text = (text + "\n⏱ interrupted by 300s timeout").strip()

    ok = (rc == 0 and not timed_out)
    verdict = "safe" if ok else "risky"

    return {
        "verdict": verdict,
        "tests": {
            "detected": True,
            "ok": ok,
            "cmd": human,
            "exit_code": rc,
            "output": text,
            "timed_out": timed_out,
        },
        "lint": None,  # lint — out of scope (spec-009, design decision 2)
    }


async def api_card_check(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/check — run quality gate in card worktree.
    Returns verdict safe/risky/unknown. Writes gate:{verdict,ts} to meta sidecar.
    Legacy or no worktree → {verdict:"unknown", reason:"legacy"}.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    # Legacy or no worktree meta → unknown without running
    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path"):
        return web.json_response({
            "verdict": "unknown",
            "reason": "legacy",
            "tests": None,
            "lint": None,
        })

    wt_path = meta["wt_path"]
    if not Path(wt_path).exists():
        return web.json_response({"error": "worktree not found on disk"}, status=404)

    # Inject project secrets (tests may need keys)
    cwd = project["cwd"]
    project_secrets = _secrets_read(cwd)

    result = await _run_quality_gate(wt_path, env=project_secrets or None)

    # Write gate result to meta sidecar
    gate_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["gate"] = {"verdict": result["verdict"], "ts": gate_ts}
    _write_run_meta(DATA, card_id, meta)

    # Publish event to Timeline (observability)
    # session_key for the event: taken from topics by project cwd (same as apply/discard)
    try:
        topics: dict = ctx.get("topics", {})
        session_key: str = next(
            (k for k, v in topics.items() if isinstance(v, dict) and v.get("cwd") == cwd),
            f"0:{project['id']}",
        )
        _bus_publish(session_key, {
            "kind": "gate",
            "verdict": result["verdict"],
            "run_id": card_id,
        })
    except Exception:
        pass  # bus event must not break the response

    return web.json_response(result)


# ─────────────────────────── file browser ───────────────────────────

# Directories and filenames hidden from listing
_FS_EXCLUDE_DIRS: set[str] = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", ".worktrees", ".mypy_cache", ".pytest_cache",
}

# Files/patterns hidden from listing and reading.
# Rule: name starts with ".env" — BUT ".env.example" is allowed.
def _is_secret_name(name: str) -> bool:
    """True if the filename is considered secret (must not be shown/read)."""
    if name.startswith(".env") and name != ".env.example":
        return True
    return False


def _read_file_content(target: Path, root: Path, rel: str) -> web.Response:
    """Shared helper for reading a file: size/binary/text checks + JSON response.
    root is used to normalise rel_norm in the response.
    Does not check secrecy and traversal — that is the caller's responsibility."""
    if not target.exists() or not target.is_file():
        return web.json_response({"error": "not a file"}, status=404)

    try:
        size = target.stat().st_size
    except Exception:
        return web.json_response({"error": "stat failed"}, status=500)

    _MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
    if size > _MAX_FILE_SIZE:
        return web.json_response({"error": "file too large", "size": size})

    try:
        with open(target, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            return web.json_response({"error": "binary file", "size": size})
    except Exception:
        return web.json_response({"error": "read failed"}, status=500)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return web.json_response({"error": f"read error: {e}"}, status=500)

    lang = target.suffix.lstrip(".") if target.suffix else ""
    try:
        rel_norm = str(target.relative_to(root))
    except ValueError:
        rel_norm = rel

    return web.json_response({"path": rel_norm, "content": content, "lang": lang, "size": size})


def _resolve_safe(cwd: str, rel: str):
    """Returns (resolved_path, cwd_resolved) or raises ValueError on traversal."""
    cwd_resolved = Path(cwd).resolve()
    # Strip leading / if present — rel should be relative
    rel_clean = rel.lstrip("/")
    target = (cwd_resolved / rel_clean).resolve()
    if not str(target).startswith(str(cwd_resolved) + "/") and target != cwd_resolved:
        raise ValueError("path traversal detected")
    return target, cwd_resolved


async def api_project_files(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/files?path=<rel> — directory listing."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    rel = req.rel_url.query.get("path", "")

    try:
        target, cwd_resolved = _resolve_safe(project["cwd"], rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    if not target.exists() or not target.is_dir():
        return web.json_response({"error": "not a directory"}, status=404)

    # Normalise rel for response (relative to cwd)
    try:
        rel_norm = str(target.relative_to(cwd_resolved))
        if rel_norm == ".":
            rel_norm = ""
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Block navigation INTO excluded directories (.git/venv/node_modules…) directly
    if any(part in _FS_EXCLUDE_DIRS for part in target.relative_to(cwd_resolved).parts):
        return web.json_response({"error": "directory hidden"}, status=404)

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for item in items:
            name = item.name
            # Exclude hidden directories and secrets
            if item.is_dir() and name in _FS_EXCLUDE_DIRS:
                continue
            if item.is_file() and _is_secret_name(name):
                continue
            # Also hide secrets in folders
            if item.is_dir() and _is_secret_name(name):
                continue
            if item.is_symlink():
                # Resolve symlink and check it doesn't leave cwd
                try:
                    linked = item.resolve()
                    if not str(linked).startswith(str(cwd_resolved)):
                        continue  # symlink points outside — hide
                except Exception:
                    continue
            entry_type = "dir" if item.is_dir() else "file"
            size = 0
            if item.is_file():
                try:
                    size = item.stat().st_size
                except Exception:
                    size = 0
            entries.append({"name": name, "type": entry_type, "size": size})
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)

    return web.json_response({"path": rel_norm, "entries": entries})


async def api_project_file(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/file?path=<rel> — file contents."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)

    try:
        target, cwd_resolved = _resolve_safe(project["cwd"], rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Check name for secrets (anti-traversal kept via _resolve_safe)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)

    # Deny reading inside excluded directories (.git/venv/node_modules…)
    try:
        rel_parts = target.relative_to(cwd_resolved).parts
        if any(part in _FS_EXCLUDE_DIRS for part in rel_parts):
            return web.json_response({"error": "access denied"}, status=403)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    return _read_file_content(target, cwd_resolved, rel)


# ── Global file browser (from $HOME) ─────────────────────────────────────────
# Not bound to a project — listing/reading from $HOME with the same security rules.

_GLOBAL_FS_EXCLUDE: set[str] = {
    "node_modules", "venv", ".venv", "__pycache__",
    "dist", ".worktrees", ".mypy_cache", ".pytest_cache",
}

# Directories under $HOME that must never be listed or read — they contain
# credentials, private keys, or operator-specific config.
_GLOBAL_SENSITIVE_DIRS: frozenset[str] = frozenset({
    ".ssh",
    ".gnupg",
    ".claude",
    ".config",  # covers .config/claude-ops and any other sensitive sub-dirs
})


def _resolve_global_safe(home: Path, rel: str):
    """Like _resolve_safe, but root = $HOME. Raises ValueError on traversal."""
    rel_clean = rel.lstrip("/")
    target = (home / rel_clean).resolve()
    if not str(target).startswith(str(home) + "/") and target != home:
        raise ValueError("path traversal detected")
    return target


def _is_global_sensitive_path(target: Path, home: Path) -> bool:
    """Return True if *target* is inside (or is) a sensitive $HOME subdirectory.

    Sensitive top-level dirs: .ssh, .gnupg, .claude, .config — these hold
    private keys, GPG keyrings, Claude credentials, and operator config.
    """
    try:
        rel = target.relative_to(home)
    except ValueError:
        return False  # not under home at all — _resolve_global_safe already blocks this
    parts = rel.parts
    if not parts:
        return False  # home itself — not sensitive
    return parts[0] in _GLOBAL_SENSITIVE_DIRS


async def api_global_files(req: web.Request) -> web.Response:
    """GET /api/global/files?path=<rel> — directory listing from $HOME."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Block sensitive credential directories
    if _is_global_sensitive_path(target, home):
        return web.json_response({"error": "access denied"}, status=403)

    if not target.exists() or not target.is_dir():
        return web.json_response({"error": "not a directory"}, status=404)

    try:
        rel_norm = str(target.relative_to(home))
        if rel_norm == ".":
            rel_norm = ""
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for item in items:
            name = item.name
            if item.is_dir() and name in _GLOBAL_FS_EXCLUDE:
                continue
            # Hide sensitive subdirectories from listing
            if item.is_dir() and name in _GLOBAL_SENSITIVE_DIRS:
                continue
            if item.is_file() and _is_secret_name(name):
                continue
            entry_type = "dir" if item.is_dir() else "file"
            size = 0
            if item.is_file():
                try:
                    size = item.stat().st_size
                except Exception:
                    size = 0
            entries.append({"name": name, "type": entry_type, "size": size})
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)

    return web.json_response({"path": rel_norm, "entries": entries})


async def api_global_file(req: web.Request) -> web.Response:
    """GET /api/global/file?path=<rel> — file contents from $HOME."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)

    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)

    # Block sensitive credential directories (listing + reading)
    if _is_global_sensitive_path(target, home):
        return web.json_response({"error": "access denied"}, status=403)

    # Check secrets BEFORE reading (anti-traversal kept via _resolve_global_safe)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)

    return _read_file_content(target, home, rel)


async def api_global_file_write(req: web.Request) -> web.Response:
    """POST /api/global/file?path=<rel> — write file contents."""
    home = Path.home()
    rel = req.rel_url.query.get("path", "")
    if not rel:
        return web.json_response({"error": "path required"}, status=400)
    try:
        target = _resolve_global_safe(home, rel)
    except ValueError:
        return web.json_response({"error": "invalid path"}, status=400)
    if _is_secret_name(target.name):
        return web.json_response({"error": "access denied"}, status=403)
    if not target.exists() or not target.is_file():
        return web.json_response({"error": "not a file"}, status=404)
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    content = data.get("content", "")
    try:
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return web.json_response({"error": f"write error: {e}"}, status=500)
    return web.json_response({"ok": True, "path": rel})


async def api_card_run(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/tasks/{card}/run — sidecar from DATA/runs/<card>.md (404-safe).
    Also returns meta (mode, has_changes, applied, discarded) from JSON sidecar."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    DATA: Path = ctx["DATA"]
    sidecar = DATA / "runs" / f"{card_id}.md"
    meta = _read_run_meta(DATA, card_id)
    if sidecar.exists():
        content = sidecar.read_text(encoding="utf-8", errors="replace")
        return web.json_response({"content": content, "exists": True, "meta": meta})
    return web.json_response({"content": "", "exists": False, "meta": meta})


# ─────────────────────────── Card spec sidecar (card 5e1c0a) ───────────────────────────


def _card_specs_dir(data_dir: Path) -> Path:
    """Returns the card-specs directory path (data/card-specs/). Does not create it."""
    return data_dir / "card-specs"


def _card_spec_path(data_dir: Path, card_id: str) -> Path:
    """Returns the sidecar path for the given card_id.  Caller must have validated card_id."""
    return _card_specs_dir(data_dir) / f"{card_id}.md"


def _board_payload_with_specs(cwd: str, data_dir: Path) -> dict:
    """Like _board_payload(cwd) but annotates each card with has_spec: bool.

    Cost: one os.listdir() on data/card-specs/ (O(1) I/O), then an O(cards) set lookup
    per card — never a stat() per card.
    """
    payload = _board_payload(cwd)
    # Build the set of card ids that have a spec sidecar.
    specs_dir = _card_specs_dir(data_dir)
    spec_ids: set[str] = set()
    try:
        for name in specs_dir.iterdir():
            if name.suffix == ".md":
                spec_ids.add(name.stem)
    except (FileNotFoundError, NotADirectoryError):
        pass  # dir absent → no specs yet
    # Annotate cards in every column.
    for col in payload.get("columns", []):
        for card in col.get("cards", []):
            card["has_spec"] = card["id"] in spec_ids
    return payload


async def api_card_spec_get(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/cards/{card}/spec — read card spec sidecar.

    Returns { exists: bool, content: str }.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    DATA: Path = ctx["DATA"]
    spec_path = _card_spec_path(DATA, card_id)
    if spec_path.exists():
        content = spec_path.read_text(encoding="utf-8", errors="replace")
        return web.json_response({"exists": True, "content": content})
    return web.json_response({"exists": False, "content": ""})


async def api_card_spec_put(req: web.Request) -> web.Response:
    """PUT /api/projects/{id}/cards/{card}/spec — write (or delete) card spec sidecar.

    Body: { content: str }
    Empty/whitespace content → delete the file (exists:false).
    Atomic write via tmp file + rename.
    Returns new state: { exists: bool, content: str }.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    content: str = body.get("content", "")
    DATA: Path = ctx["DATA"]
    spec_path = _card_spec_path(DATA, card_id)

    if not content or not content.strip():
        # Empty → delete file if it exists.
        try:
            spec_path.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            return web.json_response({"error": f"delete error: {e}"}, status=500)
        return web.json_response({"exists": False, "content": ""})

    # Non-empty → write atomically.
    try:
        specs_dir = _card_specs_dir(DATA)
        specs_dir.mkdir(parents=True, exist_ok=True)
        tmp = spec_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(spec_path)
    except Exception as e:
        return web.json_response({"error": f"write error: {e}"}, status=500)
    return web.json_response({"exists": True, "content": content})


async def api_card_apply(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/apply — apply worktree branch (merge --no-ff) into the main tree."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path") or not meta.get("branch"):
        return web.json_response(
            {"error": "card was run in working tree (legacy mode) or no meta — gate unavailable"},
            status=400,
        )

    if meta.get("applied"):
        return web.json_response({"error": "card already applied"}, status=400)
    if meta.get("discarded"):
        return web.json_response({"error": "card already discarded"}, status=400)

    wt_path = meta["wt_path"]
    branch = meta["branch"]
    base_branch = meta.get("base_branch", "main")
    cwd = project["cwd"]
    name = project["name"]

    # Check that worktree physically exists
    if not Path(wt_path).exists():
        return web.json_response({"error": "worktree not found on disk — possibly deleted after restart"}, status=400)

    try:
        # Ensure HEAD is on base_branch
        current_branch = await _git_cmd(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if current_branch != base_branch:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "checkout", base_branch,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                return web.json_response(
                    {"error": f"could not switch to {base_branch}: {err.decode(errors='replace').strip()}"},
                    status=500,
                )

        # Merge --no-ff
        prompt_short = meta.get("card_id", card_id)
        merge_msg = f"Apply card {card_id}"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "merge", "--no-ff", branch, "-m", merge_msg,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode != 0:
            # Merge conflict — abort and return 409
            abort_proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "merge", "--abort",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(abort_proc.communicate(), timeout=10.0)
            err_detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            return web.json_response(
                {"error": "merge conflict", "detail": err_detail},
                status=409,
            )

        # Successful merge: delete worktree + branch
        rm_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(rm_proc.communicate(), timeout=10.0)
        br_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-d", branch,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(br_proc.communicate(), timeout=5.0)

        # Update JSON meta
        meta["applied"] = True
        _write_run_meta(DATA, card_id, meta)

        # Move card Review → Done
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            card = _pop_card(cols, card_id)
            dp = _done_path(cwd)
            header = dp.read_text(encoding="utf-8") if dp.exists() else f"# Done — {name}\n"
            if not header.strip():
                header = f"# Done — {name}\n"
            stamp = time.strftime("%Y-%m-%d")
            card_text = card["text"] if card else card_id
            new_done = header.rstrip() + f"\n- [x] {card_text} · {stamp}\n"
            dp.write_text(new_done, encoding="utf-8")
            _save_board(cwd, name, preamble, cols)

        return web.json_response({"ok": True, "applied": True, "card_id": card_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout during merge"}, status=500)
    except Exception as e:
        return web.json_response({"error": f"internal error: {e}"}, status=500)


async def api_card_discard(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/tasks/{card}/discard — discard worktree card (branch deleted)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    card_id = req.match_info["card"]
    if not _valid_card_id(card_id):
        return web.json_response({"error": "bad card id"}, status=400)

    DATA: Path = ctx["DATA"]
    meta = _read_run_meta(DATA, card_id)

    if not meta or meta.get("mode") != "worktree" or not meta.get("wt_path") or not meta.get("branch"):
        return web.json_response(
            {"error": "card was run in working tree (legacy mode) or no meta — gate unavailable"},
            status=400,
        )

    if meta.get("applied"):
        return web.json_response({"error": "card already applied"}, status=400)
    if meta.get("discarded"):
        return web.json_response({"error": "card already discarded"}, status=400)

    wt_path = meta["wt_path"]
    branch = meta["branch"]
    cwd = project["cwd"]
    name = project["name"]

    try:
        # Delete worktree (if it exists)
        if Path(wt_path).exists():
            rm_proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "worktree", "remove", "--force", wt_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm_proc.communicate(), timeout=10.0)

        # Delete branch (404-safe)
        br_proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "branch", "-D", branch,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(br_proc.communicate(), timeout=5.0)

        # Update JSON meta
        meta["discarded"] = True
        _write_run_meta(DATA, card_id, meta)

        # Move card Review → Backlog
        async with _get_board_lock(cwd):
            _, preamble, cols = _load_board(cwd)
            card = _pop_card(cols, card_id)
            if card is None:
                card = {"id": card_id, "text": card_id}
            cols["backlog"].append(card)
            _save_board(cwd, name, preamble, cols)

        return web.json_response({"ok": True, "discarded": True, "card_id": card_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout during discard"}, status=500)
    except Exception as e:
        return web.json_response({"error": f"internal error: {e}"}, status=500)


# ─────────────────────────── Multi-chat per project (spec-037) ───────────────
#
# data/chats.json: { "<project_id>": { "active": "<chat_id>", "chats": [...] } }
# Each chat owns its own session_id (null = fresh).
# ctx["sessions"] is kept as a DERIVED CACHE of active-chat session_id so that
# TG (run_agent) and _run_card continue to work unchanged via read-through.

_CHATS_LOCK: asyncio.Lock | None = None


def _chats_lock() -> asyncio.Lock:
    """Returns (lazily created) the global chats.json mutation lock."""
    global _CHATS_LOCK
    if _CHATS_LOCK is None:
        _CHATS_LOCK = asyncio.Lock()
    return _CHATS_LOCK


def _chats_path(ctx: dict) -> Path:
    return ctx["DATA"] / "chats.json"


def _load_chats(ctx: dict) -> dict:
    """Loads chats.json. Missing or corrupt → {}."""
    p = _chats_path(ctx)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_chats(ctx: dict, data: dict) -> None:
    """Atomically writes chats.json (tmp + os.replace)."""
    p = _chats_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _new_chat_id() -> str:
    """Generates a short opaque chat id (same pattern as card ids: secrets.token_hex(3))."""
    return secrets.token_hex(3)


def _valid_chat_id(chat_id: str) -> bool:
    """True if chat_id is safe to use as a dict key (no traversal chars, reasonable length)."""
    import re as _re
    return bool(_re.fullmatch(r"[a-f0-9]{6}", chat_id))


def _mirror_active_chat_to_sessions(ctx: dict, project_id: str, session_key: str, chats_data: dict) -> None:
    """Mirror the active chat's session_id into ctx['sessions'] so TG/cards read through.
    Also persists sessions.json. Call inside the chats lock after any mutation that changes
    active chat or its session_id."""
    entry = chats_data.get(project_id)
    if not entry:
        return
    active_id = entry.get("active")
    if not active_id:
        return
    chat = next((c for c in entry.get("chats", []) if c["id"] == active_id), None)
    if chat is None:
        return
    sid = chat.get("session_id")
    if sid:
        ctx["sessions"][session_key] = sid
    else:
        ctx["sessions"].pop(session_key, None)
    try:
        ctx["save_sessions"]()
    except Exception as _e:
        print(f"[chats] save_sessions error: {_e}")


def _ensure_chat_entry(ctx: dict, project_id: str, session_key: str) -> dict:
    """Returns the chats.json block for project_id, seeding it from the existing session if absent.
    Caller MUST hold _chats_lock(). Returns the FULL chats_data dict (mutated in place)."""
    chats_data = _load_chats(ctx)
    if project_id in chats_data:
        return chats_data
    # Migration: seed "Main" from the existing live session_id (zero context loss).
    existing_sid = ctx["sessions"].get(session_key) or None
    chat_id = _new_chat_id()
    chats_data[project_id] = {
        "active": chat_id,
        "chats": [
            {
                "id": chat_id,
                "name": "Main",
                "session_id": existing_sid,
                "created_at": time.time(),
            }
        ],
    }
    _save_chats(ctx, chats_data)
    return chats_data


# ─── Chats CRUD endpoints ────────────────────────────────────────────────────


async def api_project_chats_list(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/chats → {active, chats:[{id,name,session_id,created_at}]}"""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _chats_lock():
        chats_data = _ensure_chat_entry(ctx, project["id"], session_key)
        entry = chats_data[project["id"]]
    return web.json_response({"active": entry["active"], "chats": entry["chats"]})


async def api_project_chats_create(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/chats  {name?} → created chat entry"""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        body = {}
    name = (body.get("name") or "").strip() or "Chat"
    if len(name) > 80:
        name = name[:80]
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _chats_lock():
        chats_data = _ensure_chat_entry(ctx, project["id"], session_key)
        entry = chats_data[project["id"]]
        chat_id = _new_chat_id()
        new_chat = {"id": chat_id, "name": name, "session_id": None, "created_at": time.time()}
        entry["chats"].append(new_chat)
        _save_chats(ctx, chats_data)
    return web.json_response(new_chat, status=201)


async def api_project_chats_patch(req: web.Request) -> web.Response:
    """PATCH /api/projects/{id}/chats/{chat_id}  {name?, active?}
    Rename and/or set the active chat.
    Setting active=true mirrors that chat's session_id into ctx['sessions']."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    chat_id = req.match_info["chat_id"]
    if not _valid_chat_id(chat_id):
        return web.json_response({"error": "invalid chat_id"}, status=400)
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _chats_lock():
        chats_data = _ensure_chat_entry(ctx, project["id"], session_key)
        entry = chats_data[project["id"]]
        chat = next((c for c in entry["chats"] if c["id"] == chat_id), None)
        if chat is None:
            return web.json_response({"error": "chat not found"}, status=404)
        if "name" in body:
            name = (body["name"] or "").strip()
            if name:
                chat["name"] = name[:80]
        if body.get("active"):
            entry["active"] = chat_id
            _save_chats(ctx, chats_data)
            _mirror_active_chat_to_sessions(ctx, project["id"], session_key, chats_data)
        else:
            _save_chats(ctx, chats_data)
    return web.json_response({"active": entry["active"], "chat": chat})


async def api_project_chats_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/chats/{chat_id}
    Removes the chat. Refuses if it is the last one.
    If the active chat is deleted, falls back to another chat as active and mirrors
    its session_id into ctx['sessions']."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    chat_id = req.match_info["chat_id"]
    if not _valid_chat_id(chat_id):
        return web.json_response({"error": "invalid chat_id"}, status=400)
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    async with _chats_lock():
        chats_data = _ensure_chat_entry(ctx, project["id"], session_key)
        entry = chats_data[project["id"]]
        if len(entry["chats"]) <= 1:
            return web.json_response({"error": "cannot delete the last chat"}, status=400)
        was_active = entry["active"] == chat_id
        entry["chats"] = [c for c in entry["chats"] if c["id"] != chat_id]
        if was_active:
            # Fall back to the first remaining chat
            entry["active"] = entry["chats"][0]["id"]
        _save_chats(ctx, chats_data)
        if was_active:
            _mirror_active_chat_to_sessions(ctx, project["id"], session_key, chats_data)
    return web.json_response({"ok": True, "active": entry["active"]})


# ─────────────────────────── C2: project sessions ───────────────────────────

def _sdk_sessions_dir(cwd: str) -> Path:
    """SDK folder with .jsonl sessions for the given cwd."""
    return Path.home() / ".claude" / "projects" / cwd.replace("/", "-")


def _migrate_cwd_keyed_state(old_cwd: str, new_cwd: str, ctx: dict) -> list[str]:
    """Migrates cwd-keyed state when a project is renamed.

    SDK conversation history (~/.claude/projects/<slug>/) and the Timeline feed
    (DATA/timeline/<slug>.jsonl) are keyed by slug = cwd.replace('/','-').
    When cwd changes they must be physically moved — otherwise the cockpit reads an empty
    new slug, and sessions/feed "disappear" (files are intact under the old slug).
    Best-effort: migration errors do NOT roll back an already-completed folder move —
    we return a list of warnings for the API response.
    """
    warnings: list[str] = []

    # 1. SDK conversation history dir: ~/.claude/projects/<slug>
    try:
        old_sdk = _sdk_sessions_dir(old_cwd)
        new_sdk = _sdk_sessions_dir(new_cwd)
        if old_sdk.exists() and old_sdk != new_sdk:
            if new_sdk.exists():
                # Destination occupied — move files one by one, no clobber
                for f in old_sdk.iterdir():
                    dest = new_sdk / f.name
                    if not dest.exists():
                        shutil.move(str(f), str(dest))
            else:
                new_sdk.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_sdk), str(new_sdk))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"sdk-sessions: {e}")

    # 2. Timeline: DATA/timeline/<slug>.jsonl (+ .jsonl.1 backup)
    try:
        data_dir = ctx.get("DATA")
        if data_dir is not None:
            tdir = Path(data_dir) / "timeline"
            old_slug = old_cwd.replace("/", "-")
            new_slug = new_cwd.replace("/", "-")
            if old_slug != new_slug:
                for suffix in (".jsonl", ".jsonl.1"):
                    src = tdir / f"{old_slug}{suffix}"
                    if src.exists():
                        dst = tdir / f"{new_slug}{suffix}"
                        if not dst.exists():
                            shutil.move(str(src), str(dst))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"timeline: {e}")

    return warnings


def _session_preview(jsonl_path: Path) -> str:
    """Extract the first human-readable message from a session jsonl file (~70 chars)."""
    try:
        lines_read = 0
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines_read += 1
                if lines_read > 80:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Option 1: enqueue operation with a content string
                if obj.get("operation") == "enqueue":
                    content = obj.get("content")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        return (text[:70] + "…") if len(text) > 70 else text
                # Option 2: message with role=user
                msg = obj.get("message", {})
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        return (text[:70] + "…") if len(text) > 70 else text
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = (block.get("text") or "").strip()
                                if text:
                                    return (text[:70] + "…") if len(text) > 70 else text
    except Exception:
        pass
    return "(untitled)"


def _session_message_count(jsonl_path: Path) -> int:
    """Count user+assistant message records in a session jsonl (spec-042 label).

    Cheap full-file scan; used only by the sessions-list endpoint (≤30 sessions).
    Tolerant of malformed lines — best-effort, never raises."""
    count = 0
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message", {})
                if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
                    count += 1
    except Exception:
        pass
    return count


def _session_context_tokens(jsonl_path: Path) -> int:
    """Return the context token count from the LAST assistant message with usage data.

    Reads the transcript .jsonl and sums input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens from the usage object on the last assistant turn.
    Reads from the end (last 64 KB) for efficiency.
    Returns 0 if no usage found or on any error.
    """
    try:
        last_usage: dict | None = None
        chunk_size = 65536
        with open(jsonl_path, "rb") as fh:
            fh.seek(0, 2)  # seek to end
            file_size = fh.tell()
            read_start = max(0, file_size - chunk_size)
            fh.seek(read_start)
            raw = fh.read()
        # Decode and split into lines; first line may be partial — skip it if we seeked mid-file
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if read_start > 0 and lines:
            lines = lines[1:]  # drop potentially partial first line
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message", {})
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if isinstance(usage, dict):
                last_usage = usage
        if last_usage is None:
            return 0
        return (
            (last_usage.get("input_tokens") or 0)
            + (last_usage.get("cache_read_input_tokens") or 0)
            + (last_usage.get("cache_creation_input_tokens") or 0)
        )
    except Exception:
        return 0


async def api_project_sessions(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/sessions — list of SDK sessions for the project."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    _sk = (project.get("session_key") or project.get("tg_thread", ""))
    active_sid = ctx["sessions"].get(_sk)
    sdk_dir = _sdk_sessions_dir(project["cwd"])

    if not sdk_dir.is_dir():
        return web.json_response({"sessions": []})

    labels = _load_session_labels(ctx)

    sessions = []
    try:
        for f in sdk_dir.glob("*.jsonl"):
            sid = f.stem
            try:
                mtime = f.stat().st_mtime
            except Exception:
                mtime = 0
            last_used = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            preview = _session_preview(f)
            sessions.append({
                "session_id": sid,
                "last_used": last_used,
                "preview": preview,
                "is_active": sid == active_sid,
                "label": labels.get(sid) or None,
                "message_count": _session_message_count(f),
                "context_tokens": _session_context_tokens(f),
            })
    except Exception:
        pass

    sessions.sort(key=lambda s: s["last_used"], reverse=True)
    if len(sessions) > 30:
        sessions = sessions[:30]

    return web.json_response({"sessions": sessions})


async def api_project_session_label(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/sessions/{sid}/label  {label}
    Manual label for ANY session (our layer on top of SDK). Empty label → remove label.
    Storage is global by session_id (data/session_labels.json), project id is route-only."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    sid = os.path.basename(req.match_info["sid"])  # anti-traversal: basename only
    if not sid:
        return web.json_response({"error": "bad session id"}, status=400)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    label = (body.get("label") or "").strip()
    if len(label) > 100:
        label = label[:100]
    labels = _load_session_labels(ctx)
    if label:
        labels[sid] = label
    else:
        labels.pop(sid, None)
    _save_session_labels(ctx, labels)
    return web.json_response({"ok": True, "session_id": sid, "label": label or None})


async def api_project_set_session(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/session — switch or reset session."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    _sk = (project.get("session_key") or project.get("tg_thread", ""))

    # Lock: cannot change session while project is busy
    if ctx["running"].get(_sk) is not None:
        return web.json_response(
            {"error": "project busy, session change unavailable"},
            status=409,
        )

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    action = body.get("action")

    if action == "new":
        # Spec-037: reset the ACTIVE chat's session_id (not all sessions).
        # Also mirror into ctx["sessions"] as derived cache.
        async with _chats_lock():
            _ss_data = _load_chats(ctx)
            _ss_proj = _ss_data.get(project["id"])
            if _ss_proj:
                _ss_active_id = _ss_proj.get("active")
                _ss_active = next(
                    (c for c in _ss_proj.get("chats", []) if c["id"] == _ss_active_id),
                    None,
                )
                if _ss_active is not None:
                    _ss_active["session_id"] = None
                    _save_chats(ctx, _ss_data)
        ctx["sessions"].pop(_sk, None)
        ctx["save_sessions"]()
        # Clear context-warn state so a fresh session can warn again.
        try:
            _cw = ctx.get("context_warned")
            if _cw is not None:
                _cw.discard(_sk)
        except Exception:
            pass
        # spec-039: evict the live client so PERSISTENT_CLIENT=1 truly starts fresh.
        try:
            _evict_fn = ctx.get("evict_live_client")
            if _evict_fn is not None:
                await _evict_fn(_sk, ctx)
        except Exception as _exc:
            print(f"[api_project_set_session] live-client eviction error for {_sk}: {_exc!r}")
        return web.json_response({"active": None})

    elif action == "resume":
        session_id = body.get("session_id", "")
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        # Sanitise: basename only (no / or ..) — against escaping to another .jsonl
        if session_id != Path(session_id).name or session_id in ("", ".", ".."):
            return web.json_response({"error": "invalid session_id"}, status=400)
        # Validate — file must exist
        sdk_dir = _sdk_sessions_dir(project["cwd"])
        candidate = sdk_dir / f"{session_id}.jsonl"
        if not candidate.is_file():
            return web.json_response({"error": "session not found"}, status=400)
        # Spec-037: write session_id to the active chat entry + mirror to ctx["sessions"].
        async with _chats_lock():
            _sr_data = _load_chats(ctx)
            _sr_proj = _sr_data.get(project["id"])
            if _sr_proj:
                _sr_active_id = _sr_proj.get("active")
                _sr_active = next(
                    (c for c in _sr_proj.get("chats", []) if c["id"] == _sr_active_id),
                    None,
                )
                if _sr_active is not None:
                    _sr_active["session_id"] = session_id
                    _save_chats(ctx, _sr_data)
        ctx["sessions"][_sk] = session_id
        ctx["save_sessions"]()
        return web.json_response({"active": session_id})

    else:
        return web.json_response({"error": "action must be 'new' or 'resume'"}, status=400)


_SERVICE_BLOCK_RE = re.compile(
    r"<(?P<tag>task-notification|prior-session-summary|system-reminder"
    r"|command-name|command-message|command-args)"
    r"[^>]*>.*?</(?P=tag)>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_service_blocks(text: str) -> str:
    """Remove SDK-injected service XML blocks from a user-turn string.

    Blocks stripped: <task-notification>, <prior-session-summary>,
    <system-reminder>, <command-name>, <command-message>, <command-args>.
    Returns the cleaned text with surrounding whitespace removed.
    """
    return _SERVICE_BLOCK_RE.sub("", text).strip()


def _session_history(jsonl_path: Path, limit: int = 100) -> list[dict]:
    """Parses an SDK session transcript → feed [{role, text, tools}].
    user(str)=human reply; user(list)=tool_result, skip.
    assistant(list)=text/tool_use blocks. Other types — noise."""
    msgs: list[dict] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type")
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                if t == "user":
                    c = m.get("content")
                    if isinstance(c, str):
                        cleaned = _strip_service_blocks(c)
                        if cleaned:
                            msgs.append({"role": "user", "text": cleaned, "tools": []})
                    # content-list on user = tool_result → skip (not a human reply)
                elif t == "assistant":
                    c = m.get("content")
                    if not isinstance(c, list):
                        continue
                    text_parts, tools = [], []
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text" and (b.get("text") or "").strip():
                            text_parts.append(b["text"])
                        elif b.get("type") == "tool_use":
                            inp = b.get("input") or {}
                            tool_name = b.get("name", "?")
                            tools.append(_format_tool(tool_name, inp if isinstance(inp, dict) else {}))
                    if text_parts or tools:
                        msgs.append({"role": "assistant", "text": "\n".join(text_parts), "tools": tools})
    except Exception:
        pass
    return msgs[-limit:] if len(msgs) > limit else msgs


def _session_context_tokens(jsonl_path: Path) -> int:
    """Actual session context size = prompt tokens of the last assistant turn
    (input + cache_read + cache_creation). Matches get_context_usage().totalTokens.
    0 if no transcript/usage."""
    ctx, _, _ = _session_last_turn(jsonl_path)
    return ctx


def _session_last_turn(jsonl_path: Path) -> "tuple[int, int | None, int | None]":
    """Single-pass scan of a session transcript returning data from the last assistant turn.

    Returns:
        (context_tokens, last_turn_at_ms, last_cache_hit_pct)

        context_tokens   — prompt tokens of the last assistant turn
                           (input + cache_read + cache_creation); 0 if absent.
        last_turn_at_ms  — unix milliseconds of the last assistant line's "timestamp"
                           field (ISO-8601). Falls back to the file's mtime if the
                           field is absent or unparseable. None if no assistant turn.
        last_cache_hit_pct — cache hit % for the last assistant turn per the formula:
                             round(cache_read / (cache_read + input_tokens) * 100).
                             Note: cache_creation is NOT included in the ratio (it is
                             the write side, not the read side). None if no usage.
    """
    context_tokens = 0
    last_turn_at_ms: "int | None" = None
    last_cache_hit_pct: "int | None" = None
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                # Context tokens (input + cache_read + cache_creation)
                u = (o.get("message") or {}).get("usage") or {}
                pt = (u.get("input_tokens", 0)
                      + u.get("cache_read_input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0))
                if pt:
                    context_tokens = pt
                # Timestamp → epoch ms
                ts_raw = o.get("timestamp")
                ts_ms: "int | None" = None
                if ts_raw:
                    try:
                        # Handle trailing Z (not supported by fromisoformat before 3.11)
                        ts_str = ts_raw.rstrip("Z").replace("Z", "+00:00")
                        if ts_str.endswith("+00:00") or "+" in ts_str[10:] or ts_str.count("-") > 2:
                            from datetime import datetime, timezone
                            dt = datetime.fromisoformat(ts_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            ts_ms = int(dt.timestamp() * 1000)
                        else:
                            from datetime import datetime, timezone
                            dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                            ts_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        ts_ms = None
                last_turn_at_ms = ts_ms
                # Cache hit % — mirror bot.py formula (cache_read / (cache_read + input_tokens))
                # cache_creation is the write side (not counted in hit ratio)
                cache_read = u.get("cache_read_input_tokens", 0) or 0
                input_fresh = u.get("input_tokens", 0) or 0
                ratio_pt = cache_read + input_fresh
                if ratio_pt > 0:
                    last_cache_hit_pct = round(cache_read / ratio_pt * 100)
                else:
                    last_cache_hit_pct = None
    except Exception:
        pass
    # If we found an assistant turn but no parseable timestamp, fall back to file mtime
    if last_turn_at_ms is None and last_cache_hit_pct is not None:
        try:
            last_turn_at_ms = int(jsonl_path.stat().st_mtime * 1000)
        except Exception:
            pass
    return context_tokens, last_turn_at_ms, last_cache_hit_pct


async def api_project_session_history(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/session-history?session_id=<opt.> — feed for active (or specified) session."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    sid = req.rel_url.query.get("session_id", "") or ctx["sessions"].get((project.get("session_key") or project.get("tg_thread", "")))
    if not sid:
        # Spec-043 C: explicit context_tokens:0 so the frontend can distinguish
        # "fresh session (known zero)" from "no data yet (null/missing)".
        return web.json_response({"messages": [], "session_id": None, "context_tokens": 0, "last_cache_hit_pct": None})
    # Sanitise (basename-only)
    if sid != Path(sid).name or sid in (".", ".."):
        return web.json_response({"error": "invalid session_id"}, status=400)

    jsonl = _sdk_sessions_dir(project["cwd"]) / f"{sid}.jsonl"
    if not jsonl.is_file():
        return web.json_response({"messages": [], "session_id": sid})

    context_tokens, last_turn_at_ms, last_cache_hit_pct = _session_last_turn(jsonl)
    return web.json_response({
        "messages": _session_history(jsonl),
        "session_id": sid,
        "context_tokens": context_tokens,
        "context_window": CONTEXT_WINDOW,
        "last_turn_at": last_turn_at_ms,
        "last_cache_hit_pct": last_cache_hit_pct,
    })


# ─────────────────────────── Chat message queue (server-side) ───────────────
# Per-session FIFO queue of messages the user submitted while the agent was
# busy.  Persisted to DATA/chat-queue.json so queued messages survive a page
# reload or server restart.
# Each item: {"id": str, "text": str, "created_at": float}
#
# Endpoints:
#   GET    /api/projects/{id}/chat/queue          → {"items": [...]}
#   POST   /api/projects/{id}/chat/queue          → {"item": {...}}  (enqueue)
#   PATCH  /api/projects/{id}/chat/queue/{msg_id} → {"item": {...}}  (edit)
#   DELETE /api/projects/{id}/chat/queue/{msg_id} → {"ok": true}     (remove)
#
# The existing chat endpoint (POST /chat) enqueues via _chat_queue_enqueue
# instead of returning "busy" when a run is in progress.
# When the run finishes the GET /live poll / done-SSE event is the signal for
# the frontend to drain (it calls sendMessage with the first queued item).

import uuid as _uuid_mod

_CHAT_QUEUE: "dict[str, list[dict]]" = {}  # session_key → [{id, text, created_at}, ...]
_CHAT_QUEUE_MAX = int(os.environ.get("CHAT_QUEUE_MAX", "20"))
_CHAT_QUEUE_FILE: "Path | None" = None  # set by _chat_queue_init() in start()


def _chat_queue_init(ctx: dict) -> None:
    """Called from start() — sets the persistence path and loads existing queue from disk.
    Safe to call multiple times (idempotent); swallows all I/O errors."""
    global _CHAT_QUEUE_FILE
    _CHAT_QUEUE_FILE = ctx["DATA"] / "chat-queue.json"
    try:
        if _CHAT_QUEUE_FILE.exists():
            raw = json.loads(_CHAT_QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _CHAT_QUEUE.clear()
                _CHAT_QUEUE.update(raw)
    except Exception:
        pass  # corrupted file — start fresh, do not break startup


def _chat_queue_flush() -> None:
    """Atomically persist _CHAT_QUEUE to disk.  Swallows all I/O errors."""
    if _CHAT_QUEUE_FILE is None:
        return
    try:
        tmp = _CHAT_QUEUE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_CHAT_QUEUE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CHAT_QUEUE_FILE)
    except Exception:
        pass


def _chat_queue_enqueue(session_key: str, text: str) -> "dict | None":
    """Append a message to the chat queue for session_key.
    Returns the new item dict, or None if the queue is full."""
    lst = _CHAT_QUEUE.setdefault(session_key, [])
    if len(lst) >= _CHAT_QUEUE_MAX:
        return None
    item: dict = {"id": str(_uuid_mod.uuid4()), "text": text, "created_at": time.time()}
    lst.append(item)
    _chat_queue_flush()
    return item


def _chat_queue_get(session_key: str) -> list:
    """Returns a copy of the queue for session_key."""
    return list(_CHAT_QUEUE.get(session_key, []))


def _chat_queue_pop(session_key: str) -> "dict | None":
    """Pops and returns the oldest item, or None if empty."""
    lst = _CHAT_QUEUE.get(session_key)
    if not lst:
        return None
    item = lst.pop(0)
    if not lst:
        _CHAT_QUEUE.pop(session_key, None)
    _chat_queue_flush()
    return item


def _chat_queue_edit(session_key: str, msg_id: str, new_text: str) -> "dict | None":
    """Edits the text of a queued item by id.  Returns the updated item or None if not found."""
    for item in _CHAT_QUEUE.get(session_key, []):
        if item["id"] == msg_id:
            item["text"] = new_text
            _chat_queue_flush()
            return dict(item)
    return None


def _chat_queue_delete(session_key: str, msg_id: str) -> bool:
    """Removes item by id.  Returns True if found and removed."""
    lst = _CHAT_QUEUE.get(session_key)
    if not lst:
        return False
    for i, item in enumerate(lst):
        if item["id"] == msg_id:
            lst.pop(i)
            if not lst:
                _CHAT_QUEUE.pop(session_key, None)
            _chat_queue_flush()
            return True
    return False


async def api_chat_queue_list(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/chat/queue — return pending queued messages."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    return web.json_response({"items": _chat_queue_get(session_key)})


async def api_chat_queue_add(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/chat/queue — enqueue a message (called when project is busy)."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    item = _chat_queue_enqueue(session_key, text)
    if item is None:
        return web.json_response({"error": "queue full"}, status=429)
    return web.json_response({"item": item}, status=201)


async def api_chat_queue_edit(req: web.Request) -> web.Response:
    """PATCH /api/projects/{id}/chat/queue/{msg_id} — edit queued message text."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    msg_id = req.match_info["msg_id"]
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    new_text = (body.get("text") or "").strip()
    if not new_text:
        return web.json_response({"error": "empty text"}, status=400)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    updated = _chat_queue_edit(session_key, msg_id, new_text)
    if updated is None:
        return web.json_response({"error": "not found (already consumed or invalid id)"}, status=404)
    return web.json_response({"item": updated})


async def api_chat_queue_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/chat/queue/{msg_id} — remove a queued message."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    msg_id = req.match_info["msg_id"]
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    removed = _chat_queue_delete(session_key, msg_id)
    if not removed:
        return web.json_response({"error": "not found (already consumed or invalid id)"}, status=404)
    return web.json_response({"ok": True})


# ─────────────────────────── Chat queue backend drain ───────────────────────────
# Spec-041 A3: backend-authoritative delivery of queued chat messages.
# Mirrors _execute_deferred / _queue_drain_loop for the chat queue.


async def _chat_queue_execute(ctx: dict, session_key: str, item: dict) -> None:
    """Dispatch one queued chat message through run_engine.

    The CALLER must reserve ctx['running'][session_key] synchronously before
    spawning this coroutine (see _chat_queue_drain_one).  This function does NOT
    reserve the lock itself — only releases it in finally.
    """
    run_id = _uuid.uuid4().hex[:6]
    outcome = "fail"
    try:
        topics = ctx["topics"]
        topic = topics.get(session_key)
        if topic is None:
            print(f"[chat_queue] session_key {session_key!r} not in topics — dropping item {item['id']}")
            return
        cwd = topic.get("cwd") or ctx.get("DEFAULT_CWD") or str(Path.home())
        project_name = topic.get("project", "unknown")
        model = topic.get("model") or ctx.get("DEFAULT_MODEL", "sonnet")
        prompt = item["text"]

        run_engine = ctx.get("run_engine")
        if run_engine is None:
            raise RuntimeError("run_engine not available in ctx")

        project_secrets = _secrets_read(cwd)
        agents_config = topic.get("agents_config") or {}
        agents_kwargs = _build_agents_kwargs(ctx, agents_config)

        _bus_publish(session_key, {
            "kind": "run_start",
            "source": "chat",
            "prompt": prompt,
            "run_id": run_id,
        })

        resume_session_id = ctx["sessions"].get(session_key)

        async for event in run_engine(
            project_name=project_name,
            cwd=cwd,
            prompt=prompt,
            session_key=session_key,
            model=model,
            resume_session_id=resume_session_id,
            env=project_secrets,
            **agents_kwargs,
            ctx=ctx,
            ephemeral=False,
        ):
            etype = event["type"]
            if etype == "text":
                _bus_publish(session_key, {"kind": "text", "text": event["text"], "run_id": run_id})
            elif etype == "result":
                if event.get("session_id"):
                    ctx["sessions"][session_key] = event["session_id"]
                    ctx["save_sessions"]()
            elif etype == "error":
                raise event["exc"]

        outcome = "ok"
        _bus_publish(session_key, {"kind": "run_end", "source": "chat", "outcome": "ok", "run_id": run_id})

    except Exception as e:
        print(f"[chat_queue] execute error for {session_key} item {item['id']}: {e}")
        _bus_publish(session_key, {"kind": "run_end", "source": "chat", "outcome": "fail", "run_id": run_id})

    finally:
        ctx["running"].pop(session_key, None)
        # Chain: if more items are queued for this session, fire the next one immediately.
        try:
            await _chat_queue_drain_one(ctx, session_key)
        except Exception as _drain_exc:
            print(f"[chat_queue] chain drain error for {session_key}: {_drain_exc}")


async def _chat_queue_drain_one(ctx: dict, session_key: str) -> bool:
    """Pop and dispatch the next queued chat item for session_key if the session is free.

    Returns True if an item was dequeued and spawned, False otherwise.
    The lock is reserved SYNCHRONOUSLY before any await/spawn to prevent races.
    """
    if ctx["running"].get(session_key) is not None:
        return False
    items = _chat_queue_get(session_key)
    if not items:
        return False
    item = _chat_queue_pop(session_key)
    if item is None:
        return False
    # Reserve lock synchronously before the first await.
    ctx["running"][session_key] = True
    _spawn_bg(_chat_queue_execute(ctx, session_key, item))
    return True


async def _chat_queue_drain_loop(ctx: dict) -> None:
    """Backstop loop: every _QUEUE_DRAIN_INTERVAL_SEC checks all session_keys with queued
    chat items and drains them.  Handles: restart (queue survived it), lock freed by
    non-chat path (card/TG) without triggering chain drain.
    """
    await asyncio.sleep(12)  # give the bot time to settle
    while True:
        try:
            session_keys = list(_CHAT_QUEUE.keys())
            for sk in session_keys:
                try:
                    await _chat_queue_drain_one(ctx, sk)
                except Exception as pe:
                    print(f"[chat_queue_drain_loop] session {sk!r} error: {pe}")
        except Exception as e:
            print(f"[chat_queue_drain_loop] error: {e}")
        await asyncio.sleep(_QUEUE_DRAIN_INTERVAL_SEC)


# ─────────────────────────── C1: SSE chat ───────────────────────────
#
# POST /api/projects/{id}/chat  body: {"prompt": str}
# Response: text/event-stream with events:
#   data: {"type":"text","text":"..."}
#   data: {"type":"tool","name":"...","input":"..."}
#   data: {"type":"result"}
#   data: {"type":"error","error":"..."}
#   data: {"type":"done"}
#
# Lock SHARED with TG and F1 cards (session_key = (project.get("session_key") or project.get("tg_thread", ""))).
# Disconnect-resilient: if client closed the tab (ConnectionResetError on write),
# the run_engine generator continues to completion, session_id is saved, lock is released.

async def api_project_chat(req: web.Request) -> web.Response:
    ctx = req.app["ctx"]
    pid = req.match_info["id"]

    # Check run_engine upfront (degraded: old launch without F1/C1)
    run_engine = ctx.get("run_engine")
    if run_engine is None:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )
        await resp.prepare(req)
        payload = json.dumps({"type": "error", "error": "run_engine unavailable"}, ensure_ascii=False)
        await resp.write(f"data: {payload}\n\n".encode())
        return resp

    # Parse request body
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return web.json_response({"error": "empty prompt"}, status=400)

    # Thinking mode selector: "max" | "default" | "min" (from UI). Map to run_engine effort values.
    # "default" → None (run_engine uses _DEFAULT_EFFORT env; unchanged behaviour for all callers).
    # "max"     → "high" (highest effort for non-fable models; fable ignores it per SDK note).
    # "min"     → "low"  (no extended thinking; fastest / cheapest for non-fable models).
    _think_mode = (body.get("think_mode") or "default").strip()
    _effort_override: "str | None" = None
    if _think_mode == "max":
        _effort_override = "high"
    elif _think_mode == "min":
        _effort_override = "low"
    # "default" → _effort_override stays None → run_engine uses _DEFAULT_EFFORT

    # Spec-037: optional chat_id to target a specific chat tab (falls back to active chat).
    _req_chat_id: "str | None" = (body.get("chat_id") or "").strip() or None
    if _req_chat_id and not _valid_chat_id(_req_chat_id):
        return web.json_response({"error": "invalid chat_id"}, status=400)

    # Resolve project
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    cwd = project["cwd"]
    name = project["name"]
    model = project.get("model", ctx.get("DEFAULT_MODEL", "sonnet"))
    session_key = (project.get("session_key") or project.get("tg_thread", ""))  # SHARED key with TG and F1

    # Lock check (SYNCHRONOUSLY — before first await, against race)
    # Spec-041 A3: enqueue on busy instead of returning an error — backend drains it.
    if ctx["running"].get(session_key) is not None:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no"},
        )
        await resp.prepare(req)
        item = _chat_queue_enqueue(session_key, prompt)
        if item is None:
            payload = json.dumps({"type": "error", "error": "queue full"}, ensure_ascii=False)
        else:
            payload = json.dumps({"type": "queued", "item": item}, ensure_ascii=False)
        await resp.write(f"data: {payload}\n\n".encode())
        return resp

    # Reserve slot SYNCHRONOUSLY before first await
    ctx["running"][session_key] = True
    # Spec-035: start live turn buffer for this session
    _live_turn_create(session_key, model)
    # Generate a short run id so the bus run_start/run_end pair is correlated.
    _chat_run_id = _uuid.uuid4().hex[:6]
    # Publish run_start to the activity bus so other tabs (and recovery after stream drop)
    # can reconstruct the in-flight turn without requiring a hard refresh.
    _bus_publish(session_key, {
        "kind": "run_start",
        "source": "chat",
        "prompt": prompt,
        "run_id": _chat_run_id,
    }, persist=True)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(req)

    client_gone = False

    async def _send(payload_dict: dict):
        nonlocal client_gone
        if client_gone:
            return
        try:
            line = f"data: {json.dumps(payload_dict, ensure_ascii=False)}\n\n"
            await resp.write(line.encode())
        except (ConnectionResetError, ConnectionAbortedError, Exception) as exc:
            # Client closed the tab — mark it, but do NOT interrupt the generator
            # (task continues in background, session_id will be saved)
            client_gone = True
            print(f"[api_project_chat] client disconnected ({type(exc).__name__}), task continues in background")

    _chat_last_result_event: "dict | None" = None  # Phase D: track for auto-resume
    # spec-034 L2: accumulate agent reply text for board reconciler
    _chat_answer_parts: list = []
    # Tracks whether the run completed successfully (used for bus run_end outcome).
    _chat_run_ok: bool = False
    # Spec-037: the resolved chat_id for this run (used to write session_id back)
    _active_chat_id_for_run: "str | None" = None

    try:
        # Spec-037: resolve session_id from the active chat (or explicitly requested chat).
        # Falls back to ctx["sessions"] so existing code paths are unaffected if chats.json
        # does not yet exist (migration seeds it on first access, but guard anyway).
        _chat_resume_sid: "str | None" = None
        try:
            async with _chats_lock():
                _chat_entry = _ensure_chat_entry(ctx, project["id"], session_key)
                _proj_chats = _chat_entry.get(project["id"], {})
                _target_chat_id = _req_chat_id or _proj_chats.get("active")
                _target_chat = next(
                    (c for c in _proj_chats.get("chats", []) if c["id"] == _target_chat_id),
                    None,
                )
                if _target_chat is not None:
                    _active_chat_id_for_run = _target_chat["id"]
                    _chat_resume_sid = _target_chat.get("session_id") or None
                    # Keep ctx["sessions"] in sync (derived cache)
                    if _chat_resume_sid:
                        print(f"[session] chat-resume-write {session_key} sid={_chat_resume_sid}")
                        ctx["sessions"][session_key] = _chat_resume_sid
                    else:
                        ctx["sessions"].pop(session_key, None)
        except Exception as _ce:
            print(f"[api_project_chat] chats resolve error (falling back): {_ce}")
        resume_sid = _chat_resume_sid if _chat_resume_sid is not None else ctx["sessions"].get(session_key)
        # Project secrets are injected into the agent's env (values only in-process, not in the API).
        # secret: references are resolved against the built-in store; TG vars are merged after (they win).
        project_secrets = await _resolve_secret_refs(_secrets_read(cwd))
        # Spec-038: inject cockpit media env so the cockpit-img helper knows where to write files
        # and which URL prefix to emit — mirroring how the TG channel injects TG_CHAT_ID/TG_THREAD_ID.
        _media_dir = ctx["DATA"] / "chat-media" / project["id"]
        _media_dir.mkdir(parents=True, exist_ok=True)
        project_secrets = {
            **project_secrets,
            "COPS_PROJECT_ID": project["id"],
            "COPS_MEDIA_DIR": str(_media_dir),
        }
        agents_config = project.get("agents_config") or {}
        agents_kwargs = _build_agents_kwargs(ctx, agents_config)
        # Spec-021 Phase 4: inject handoff summary into the first turn of a fresh session.
        # Only fires when there is no existing session (post-rotation) and a pending handoff exists.
        effective_prompt = prompt
        try:
            if resume_sid is None:
                ph = ctx.get("pending_handoff") or {}
                pending_summary = ph.pop(session_key, None)
                if pending_summary is not None:
                    # spec-042: persist the pop so a restart doesn't re-inject the same summary
                    ctx.get("save_handoff", lambda: None)()
                    effective_prompt = (
                        "<prior-session-summary>\n"
                        "The previous session was rotated to stay lean. Summary of where we left off below.\n"
                        "Continue this work if the new message relates to it; ignore this block if starting "
                        "something unrelated.\n\n"
                        f"{pending_summary}\n"
                        "</prior-session-summary>\n\n"
                        f"{prompt}"
                    )
                    print(f"[rotation] injected handoff into first post-rotation turn for {session_key}")
        except Exception as _inj_exc:
            print(f"[rotation] handoff injection failed (continuing without it): {_inj_exc}")
            effective_prompt = prompt
        # ephemeral=False: chat sessions share state with the project (resumable, context-tracked).
        # effort: None when think_mode="default" (preserves _DEFAULT_EFFORT); "high"/"low" otherwise.
        async for event in run_engine(
            project_name=name,
            cwd=cwd,
            prompt=effective_prompt,
            session_key=session_key,
            model=model,
            resume_session_id=resume_sid,
            env=project_secrets,
            **agents_kwargs,
            ctx=ctx,
            ephemeral=False,
            effort=_effort_override,
        ):
            etype = event.get("type")
            # Spec-035 + bug-A fix: buffer every event in the live turn ring.
            # For tool events, buffer the already-formatted rich event (same shape as the SSE
            # payload) so that cold-open replay via GET /live produces identical rendering to
            # the live SSE stream.  Raw engine tool events carry {name, input} but no `kind`
            # field; the frontend ToolBlock falls back to the bare-name "other" branch when
            # `kind` is absent, causing replayed tool calls to collapse to bare names on
            # tab-switch / browser refresh.  Formatting before buffering fixes this.
            if etype == "tool":
                inp = event.get("input") or {}
                tool_data = _format_tool(event["name"], inp if isinstance(inp, dict) else {})
                _buffered_event = {"type": "tool", **tool_data}
            elif etype == "error":
                # Coerce exc to string before buffering so the live buffer is always
                # JSON-serializable.  Raw exception objects (e.g. ProcessError) would
                # cause a TypeError in web.json_response at GET /live.
                exc_obj = event.get("exc")
                _buffered_event = {
                    "type": "error",
                    "error": str(exc_obj) if exc_obj is not None else event.get("error", "unknown error"),
                }
            else:
                _buffered_event = event
            _live_ev = _live_turn_append(session_key, _buffered_event)
            _bus_publish(session_key, _live_ev, persist=False)
            if etype == "text_delta":
                # Spec-029 §1: forward incremental text delta to the cockpit over SSE.
                # The existing {type:"text"} block (finalized AssistantMessage TextBlock) still
                # arrives below and remains the source of truth — the UI reconciles by replacing
                # the accumulated delta with the canonical final text.
                # The _send wrapper's (ConnectionResetError, ConnectionAbortedError) guard covers
                # benign client disconnects here exactly as it does for all other events.
                await _send({"type": "text_delta", "text": event["text"]})
            elif etype == "text":
                await _send({"type": "text", "text": event["text"]})
                _chat_answer_parts.append(event["text"])  # spec-034 L2: collect for reconciler
            elif etype == "tool":
                # tool_data already computed above for buffering — reuse it
                await _send({"type": "tool", **tool_data})
            elif etype == "result":
                _chat_last_result_event = event  # Phase D: capture for auto-resume
                sid = event.get("session_id")
                if sid:
                    # Spec-037: write session_id back to the specific chat entry (atomic).
                    # Also mirrors to ctx["sessions"] as derived cache so TG/cards still work.
                    _wrote_back = False
                    if _active_chat_id_for_run:
                        try:
                            async with _chats_lock():
                                _cb_data = _load_chats(ctx)
                                _cb_proj = _cb_data.get(project["id"])
                                if _cb_proj:
                                    _cb_chat = next(
                                        (c for c in _cb_proj.get("chats", [])
                                         if c["id"] == _active_chat_id_for_run),
                                        None,
                                    )
                                    if _cb_chat is not None:
                                        _cb_chat["session_id"] = sid
                                        _save_chats(ctx, _cb_data)
                                        _wrote_back = True
                                        # Mirror active chat → ctx["sessions"]
                                        if _cb_proj.get("active") == _active_chat_id_for_run:
                                            ctx["sessions"][session_key] = sid
                                            try:
                                                ctx["save_sessions"]()
                                            except Exception:
                                                pass
                        except Exception as _wb_exc:
                            print(f"[api_project_chat] session_id write-back error: {_wb_exc}")
                    if not _wrote_back:
                        # Fallback: legacy flat-map path (no chats entry yet or error)
                        ctx["sessions"][session_key] = sid
                        ctx["save_sessions"]()
                    _inherit_label_from_free_chat(ctx, session_key, sid)
                ctx_tokens = event.get("context_tokens", 0)
                # Spec-022: pass through per-turn cost visibility fields
                # utilization: best-effort from 60s-cached oauth data — never make a fresh call here
                _utilization: float | None = None
                _cached = _usage_cache.get("data")
                if _cached and (time.time() - _usage_cache.get("ts", 0)) <= _USAGE_TTL:
                    # pick five_hour window utilization as the primary signal
                    _w = (_cached.get("five_hour") or {})
                    _utilization = _w.get("utilization") if isinstance(_w, dict) else None
                # Context early-warning: upward crossing of CONTEXT_WARN_AT (once per session).
                # Only fires in the warn zone (>= CONTEXT_WARN_AT but < CONTEXT_ROTATE_AT).
                _ctx_warn = False
                try:
                    _ctx_warned: "set | None" = ctx.get("context_warned")
                    if (
                        _ctx_warned is not None
                        and CONTEXT_WARN_AT <= ctx_tokens < CONTEXT_ROTATE_AT
                        and session_key not in _ctx_warned
                    ):
                        _ctx_warn = True
                        _ctx_warned.add(session_key)
                except Exception as _cw_exc:
                    print(f"[context-warn] state check failed: {_cw_exc}")
                await _send({
                    "type": "result",
                    "context_tokens": ctx_tokens,
                    "context_window": CONTEXT_WINDOW,
                    "cache_read_tokens": event.get("cache_read_tokens"),
                    "fresh_tokens": event.get("fresh_tokens"),
                    "prompt_tokens": event.get("prompt_tokens"),
                    "cache_hit_pct": event.get("cache_hit_pct"),
                    "duration_ms": event.get("duration_ms"),
                    "utilization": _utilization,
                    **({"context_warn": True} if _ctx_warn else {}),
                })
                # spec-039: TG context-warn ping and auto-rotation removed.
                # context_warn=True in the SSE result frame above still feeds the cockpit.
            elif etype == "error":
                exc = event.get("exc")
                await _send({"type": "error", "error": str(exc) if exc else "unknown error"})
            elif etype == "rate_limit":
                rl_type = event.get("rate_limit_type")
                if rl_type:
                    ctx["rate_limits"][rl_type] = {
                        "status": event.get("status"),
                        "resets_at": event.get("resets_at"),
                        "utilization": event.get("utilization"),
                        "ts": time.time(),
                    }
                await _send({"type": "rate_limit", "status": event.get("status", "")})
            elif etype == "subagent":
                # Forward sub-agent lifecycle events as-is; cockpit UI display: Phase C.
                await _send({
                    "type": "subagent",
                    "subtype": event.get("subtype"),
                    "task_id": event.get("task_id"),
                    "description": event.get("description"),
                    "status": event.get("status"),
                    "summary": event.get("summary"),
                    "last_tool_name": event.get("last_tool_name"),
                })
            # other types — ignore

        # Spec-035: mark turn as done before sending the final SSE frame
        _live_turn_finish(session_key, "done")
        _chat_run_ok = True
        await _send({"type": "done"})

        # spec-034 L2: board reconciler — schedule as background task (never blocks the response).
        _reconcile_fn = ctx.get("reconcile_board")
        if _reconcile_fn is not None:
            _agent_reply = "\n".join(_chat_answer_parts).strip()
            asyncio.ensure_future(
                _reconcile_fn(cwd=cwd, name=name, user_msg=prompt, agent_summary=_agent_reply)
            )

        # Phase D: auto-resume if killed by rate-limit (before lock release so session_key is valid)
        _resume_sid_chat = ctx["sessions"].get(session_key)
        await _maybe_auto_resume(
            ctx=ctx,
            session_key=session_key,
            original_prompt=prompt,
            last_result_event=_chat_last_result_event,
            resume_session_id=_resume_sid_chat,
        )

    finally:
        # Lock released UNCONDITIONALLY (even if the generator threw an exception)
        ctx["running"].pop(session_key, None)
        # Spec-035: ensure turn is finished (idempotent — no-op if already marked done)
        _live_turn_finish(session_key, "error")
        # Publish run_end to the activity bus so other tabs can finalize the in-flight turn
        # display and so the originating tab can recover after a dropped direct stream.
        _bus_publish(session_key, {
            "kind": "run_end",
            "source": "chat",
            "outcome": "ok" if _chat_run_ok else "fail",
            "run_id": _chat_run_id,
        }, persist=True)
        # Spec-041 A3: snappy delivery — drain next queued chat item immediately on lock release.
        # Wrap in try/except so a drain failure never breaks the response teardown.
        try:
            await _chat_queue_drain_one(ctx, session_key)
        except Exception as _cqd_exc:
            print(f"[api_project_chat] chat queue drain error: {_cqd_exc}")

    return resp


# ─────────────────────────── Stop endpoint (chat/stop) ───────────────────────

async def api_project_chat_stop(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/chat/stop — interrupts the current agent run.
    Calls client.interrupt() on the real SDK client from ctx["running"].
    Returns {ok, stopped}; stopped=false if nothing to interrupt."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    client = ctx["running"].get(session_key)

    if client is not None and hasattr(client, "interrupt"):
        try:
            await client.interrupt()
        except Exception:
            pass
        return web.json_response({"ok": True, "stopped": True})

    return web.json_response({"ok": True, "stopped": False})


async def api_project_running(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/running — whether an agent run is active for this project."""
    ctx = req.app["ctx"]
    project = _find_project_by_id(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    return web.json_response({"running": ctx["running"].get(session_key) is not None})


async def api_project_seen(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/seen — operator opened/focused the project tab.

    Clears the awaiting marker so the attention badge disappears.
    Returns {ok: true, awaiting: false} on success.
    This is the O(1) approach: no per-tab SSE, just a lightweight POST on tab focus.
    """
    ctx = req.app["ctx"]
    project = _find_project_by_id_any(ctx, req.match_info["id"])
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    _seen[session_key] = time.time()
    # Clear awaiting only if the last_seen timestamp is ≥ last_finished timestamp.
    finished_ts = _awaiting.get(session_key, 0.0)
    seen_ts = _seen[session_key]
    if seen_ts >= finished_ts:
        _awaiting.pop(session_key, None)
    return web.json_response({"ok": True, "awaiting": False})


# ─────────────────────────── Spec-021: Manual session rotate endpoint ────────

async def api_project_rotate(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/rotate — cockpit "Wrap & reset" button.

    spec-039: auto-rotation removed.  This is now a MANUAL reset: pop the session and
    evict the live client so the next turn starts completely fresh.  Returns
    {ok: true, reset: true} on success, {ok: true, reset: false} when there is
    nothing to clear (no active session).  409 if the project is currently running.

    spec-042: accepts optional JSON body {handoff: true} to generate a cheap haiku
    summary of the current session transcript BEFORE clearing the session.  The summary
    is stored in pending_handoff[session_key] and persisted to disk so it survives
    restarts.  The first fresh-session turn will auto-inject it via <prior-session-summary>.
    Response includes {handoff: true/false} indicating whether a summary was stored.

    BUG FIX (chats.json dual-layer): clearing only ctx["sessions"] is futile because
    _mirror_active_chat_to_sessions and api_project_chat re-populate sessions from the
    active chat's session_id on every turn.  We now clear BOTH layers atomically.
    A running-lock sentinel is held for the whole operation so a concurrent turn cannot
    resurrect the session through the chat-layer fallback.
    """
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Parse optional JSON body tolerantly — missing body or non-JSON is treated as {}
    try:
        body = await req.json() if req.can_read_body else {}
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    do_handoff = bool(body.get("handoff"))

    session_key = (project.get("session_key") or project.get("tg_thread", ""))

    # Refuse if project is currently running
    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "project busy"}, status=409)

    # Reserve the running slot for the duration of the rotate so a concurrent
    # turn cannot slip through and resurrect the session via the chats-layer fallback.
    # We use True (not a string) because api_project_chat_stop checks hasattr(value, "interrupt")
    # — True has no such attribute and is safely ignored by the stop endpoint.
    ctx["running"][session_key] = True
    try:
        # Resolve the effective session_id from BOTH layers.  ctx["sessions"] is a
        # derived cache; chats.json is the source of truth for named chats (spec-037).
        _layer1_sid = ctx["sessions"].get(session_key)
        _active_chat_sid: "str | None" = None
        _chats_entry: "dict | None" = None   # the project's block in chats_data
        _chats_data_snapshot: "dict | None" = None
        try:
            _cd = _load_chats(ctx)
            _entry = _cd.get(project["id"])
            if _entry:
                _active_id = _entry.get("active")
                _active_chat = next(
                    (c for c in _entry.get("chats", []) if c["id"] == _active_id),
                    None,
                )
                if _active_chat:
                    _active_chat_sid = _active_chat.get("session_id") or None
                    _chats_entry = _entry
                    _chats_data_snapshot = _cd
        except Exception as _ce:
            print(f"[session] rotate chats-read error for {session_key}: {_ce!r}")

        effective_sid = _layer1_sid or _active_chat_sid

        # Nothing to do when no active session exists in either layer
        if effective_sid is None and session_key not in ctx.get("live_clients", {}):
            return web.json_response({"ok": True, "reset": False, "reason": "no active session"})

        # spec-042: build handoff summary BEFORE clearing so we still have the session_id.
        # Build outside the chats lock — _build_handoff may await and could deadlock.
        stored = False
        if do_handoff:
            sid_for_handoff = effective_sid
            if sid_for_handoff:
                cwd = project.get("cwd") or ""
                if not cwd:
                    # Fall back to topics lookup (same as other routes)
                    _topic = ctx.get("topics", {}).get(session_key, {})
                    cwd = _topic.get("cwd", "") if isinstance(_topic, dict) else ""
                try:
                    summary = await _build_handoff(ctx, session_key, cwd, sid_for_handoff)
                    if summary:
                        ctx["pending_handoff"][session_key] = summary
                        ctx.get("save_handoff", lambda: None)()
                        stored = True
                        print(f"[api_project_rotate] handoff summary stored for {session_key} ({len(summary)} chars)")
                        # Auto-title the closed session (best-effort, never blocks reset)
                        try:
                            _labels = _load_session_labels(ctx)
                            if sid_for_handoff not in _labels:  # never override a manual rename
                                _title = await _build_session_title(summary)
                                if _title:
                                    _labels[sid_for_handoff] = _title
                                    _save_session_labels(ctx, _labels)
                                    print(f"[handoff] auto-titled session {sid_for_handoff}: {_title!r}")
                        except Exception as _title_exc:
                            print(f"[handoff] auto-title failed (non-blocking): {_title_exc!r}")
                except Exception as _hoff_exc:
                    # Never block the reset because of a summary failure
                    print(f"[api_project_rotate] handoff build failed (continuing with blank reset): {_hoff_exc!r}")

        # Clear BOTH layers atomically.
        # Order: clear chats.json first (source of truth), then sessions (derived cache),
        # so no mirror call can re-add the session_id after we've removed it from sessions.
        print(f"[session] rotate-clear-chat {session_key} active_chat_sid={_active_chat_sid!r}")
        if _chats_entry is not None and _chats_data_snapshot is not None:
            try:
                async with _chats_lock():
                    # Re-load under the lock to avoid clobbering concurrent writes
                    _fresh = _load_chats(ctx)
                    _fresh_entry = _fresh.get(project["id"])
                    if _fresh_entry:
                        _fresh_active_id = _fresh_entry.get("active")
                        for _c in _fresh_entry.get("chats", []):
                            if _c["id"] == _fresh_active_id:
                                _c["session_id"] = None
                                break
                    _save_chats(ctx, _fresh)
            except Exception as _cl_exc:
                print(f"[session] rotate chats-clear error for {session_key}: {_cl_exc!r}")

        # Pop the sessions layer and persist
        ctx["sessions"].pop(session_key, None)
        ctx["save_sessions"]()
        print(f"[session] rotate-done {session_key} (both layers cleared, handoff={stored})")

        # Clear context-warn state so the fresh session can warn again
        try:
            _cw = ctx.get("context_warned")
            if _cw is not None:
                _cw.discard(session_key)
        except Exception:
            pass

        # Evict live client so PERSISTENT_CLIENT=1 truly starts fresh
        try:
            _evict_fn = ctx.get("evict_live_client")
            if _evict_fn is not None:
                await _evict_fn(session_key, ctx)
        except Exception as _exc:
            print(f"[api_project_rotate] live-client eviction error for {session_key}: {_exc!r}")

        return web.json_response({"ok": True, "reset": True, "handoff": stored})

    finally:
        # Always release the sentinel so subsequent turns are not blocked
        ctx["running"].pop(session_key, None)


# ─────────────────────────── Session context (session-context) ─────────────

_CTX_TOOL_READ  = {"Read", "Glob", "Grep"}
_CTX_TOOL_EDIT  = {"Edit", "Write", "NotebookEdit"}
_CTX_TOOL_BASH  = {"Bash"}
_CTX_LIST_LIMIT = 200


def _session_context(jsonl_path: Path) -> dict:
    """Parses an SDK transcript: extracts read/edited/commands from assistant tool_use blocks.
    Dedup by value, first occurrence wins. Limit ~200 per category."""
    read: list[str]     = []
    edited: list[str]   = []
    commands: list[str] = []
    seen_read: set[str]     = set()
    seen_edited: set[str]   = set()
    seen_commands: set[str] = set()

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                c = m.get("content")
                if not isinstance(c, list):
                    continue
                for block in c:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp  = block.get("input") or {}
                    if not isinstance(inp, dict):
                        continue

                    if name in _CTX_TOOL_READ:
                        # Read → file_path; Glob/Grep → pattern or path
                        val = (inp.get("file_path") or inp.get("path") or inp.get("pattern") or "").strip()
                        if val and val not in seen_read and len(read) < _CTX_LIST_LIMIT:
                            seen_read.add(val)
                            read.append(val)

                    elif name in _CTX_TOOL_EDIT:
                        val = (inp.get("file_path") or "").strip()
                        if val and val not in seen_edited and len(edited) < _CTX_LIST_LIMIT:
                            seen_edited.add(val)
                            edited.append(val)

                    elif name in _CTX_TOOL_BASH:
                        raw = (inp.get("command") or "").strip()
                        val = (raw[:80] + "…") if len(raw) > 80 else raw
                        if val and val not in seen_commands and len(commands) < _CTX_LIST_LIMIT:
                            seen_commands.add(val)
                            commands.append(val)
    except Exception:
        pass

    return {"read": read, "edited": edited, "commands": commands}


# ─────────────────────────── spec-042: handoff producer ─────────────────────

# Lazy import of claude_agent_sdk types needed only for handoff generation.
# These mirror the imports in engine.py (which owns the canonical SDK imports).
# noqa: E402 — placed near the consumer functions for readability.
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage as _AssistantMessage,
    ClaudeAgentOptions as _ClaudeAgentOptions,
    TextBlock as _TextBlock,
    query as _sdk_query_handoff,
)

# Max chars of rendered dialog sent to haiku in a single chunk.
# ~150k chars ≈ ~40k tokens; well within haiku's 200k token window.
_HANDOFF_CHUNK_CHARS = 150_000
# Max number of map-reduce chunks before truncating (log a warning).
_HANDOFF_MAX_CHUNKS = 8


async def _build_handoff(ctx: dict, session_key: str, cwd: str, session_id: "str | None") -> str:
    """Build a handoff summary string for the given session (spec-042).

    Returns a non-empty string on success, or "" when there is nothing useful to say
    (no session, empty transcript, haiku failure — all treated as blank handoff).
    Never raises; the caller must always get a string back so the reset can proceed.

    Structure of the returned string:
        [narrative from haiku]

        ---
        Files touched: ...
        Commands: ...
        Recent commits: ...
        Open cards:
        ...
    """
    try:
        return await _build_handoff_inner(ctx, session_key, cwd, session_id)
    except Exception as exc:
        print(f"[handoff] _build_handoff unexpected error for {session_key}: {exc!r}")
        return ""


async def _build_handoff_inner(ctx: dict, session_key: str, cwd: str, session_id: "str | None") -> str:
    """Inner implementation — wrapped by _build_handoff for exception safety."""

    # ── 1. Locate transcript ──────────────────────────────────────────────────
    if not session_id:
        return ""

    jsonl_path = _sdk_sessions_dir(cwd) / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        print(f"[handoff] transcript not found: {jsonl_path}")
        return ""

    # Empty file check
    try:
        if jsonl_path.stat().st_size == 0:
            return ""
    except OSError:
        return ""

    # ── 2. Final board reconcile (best-effort) ────────────────────────────────
    _reconcile_fn = ctx.get("reconcile_board")
    if _reconcile_fn is not None:
        try:
            # Use the project name from the session_key lookup via ctx topics
            _project_name = ""
            _topic_entry = ctx.get("topics", {}).get(session_key, {})
            if isinstance(_topic_entry, dict):
                _project_name = _topic_entry.get("project", "")
            await _reconcile_fn(cwd=cwd, name=_project_name, user_msg="", agent_summary="")
        except Exception as _rec_exc:
            print(f"[handoff] reconcile_board failed (non-blocking): {_rec_exc!r}")

    # ── 3. Deterministic FACTS (no model call) ────────────────────────────────
    context_info = _session_context(jsonl_path)
    edited_files = context_info.get("edited", [])
    commands = context_info.get("commands", [])

    # Recent git commits (last 15, wrapped in try for non-git dirs)
    recent_commits: list[str] = []
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", cwd, "log", "--oneline", "-n", "15"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            recent_commits = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    except Exception as _git_exc:
        print(f"[handoff] git log failed (non-blocking): {_git_exc!r}")

    # Open board cards
    open_cards = ""
    try:
        from board import board_summary as _board_summary
        open_cards = _board_summary(cwd) or ""
    except Exception as _board_exc:
        print(f"[handoff] board_summary failed (non-blocking): {_board_exc!r}")

    # ── 4. Narrative via haiku (map-reduce for large transcripts) ─────────────
    narrative = ""
    try:
        history = _session_history(jsonl_path, limit=1000)  # no UI limit — full transcript
        rendered_parts: list[str] = []
        for entry in history:
            role = entry.get("role", "?")
            text = (entry.get("text") or "").strip()
            tools = entry.get("tools") or []
            if text:
                rendered_parts.append(f"[{role}]: {text}")
            for t in tools:
                rendered_parts.append(f"[tool]: {t}")
        full_dialog = "\n".join(rendered_parts)

        if full_dialog:
            handoff_model = os.environ.get("HANDOFF_MODEL", "haiku")
            opts = _ClaudeAgentOptions(
                model=handoff_model,
                permission_mode="bypassPermissions",
                cwd=_OPS_SCRATCH_CWD,  # scratch dir: transcript never pollutes project session list
                allowed_tools=[],
                disallowed_tools=[],
                effort="low",
            )

            if len(full_dialog) <= _HANDOFF_CHUNK_CHARS:
                # Single-pass — common case
                narrative = await _haiku_summarize(
                    ROTATION_SUMMARY_PROMPT + "\n\n" + full_dialog, opts
                )
            else:
                # Map-reduce for large transcripts
                chunks = []
                offset = 0
                while offset < len(full_dialog):
                    chunks.append(full_dialog[offset : offset + _HANDOFF_CHUNK_CHARS])
                    offset += _HANDOFF_CHUNK_CHARS

                if len(chunks) > _HANDOFF_MAX_CHUNKS:
                    print(
                        f"[handoff] transcript too large: {len(chunks)} chunks, "
                        f"truncating to {_HANDOFF_MAX_CHUNKS} for {session_key}"
                    )
                    chunks = chunks[:_HANDOFF_MAX_CHUNKS]

                chunk_summaries: list[str] = []
                for i, chunk in enumerate(chunks):
                    chunk_prompt = (
                        f"Summarize this portion (part {i + 1}/{len(chunks)}) of the session for handoff. "
                        "Key decisions, actions, file changes, open questions. Dense, ≤200 words, English.\n\n"
                        + chunk
                    )
                    chunk_summary = await _haiku_summarize(chunk_prompt, opts)
                    if chunk_summary:
                        chunk_summaries.append(chunk_summary)

                if chunk_summaries:
                    combined = "\n\n---\n".join(chunk_summaries)
                    final_prompt = (
                        ROTATION_SUMMARY_PROMPT
                        + "\n\nThese are partial summaries — combine into one handoff:\n\n"
                        + combined
                    )
                    narrative = await _haiku_summarize(final_prompt, opts)
    except Exception as _narr_exc:
        print(f"[handoff] narrative generation failed (non-blocking): {_narr_exc!r}")

    # ── 5. Assemble ───────────────────────────────────────────────────────────
    parts: list[str] = []
    if narrative:
        parts.append(narrative)

    fact_lines: list[str] = []
    if edited_files:
        fact_lines.append("Files touched: " + ", ".join(edited_files[:30]))
    if commands:
        fact_lines.append("Commands: " + " | ".join(commands[:20]))
    if recent_commits:
        fact_lines.append("Recent commits:\n" + "\n".join(f"  {c}" for c in recent_commits))
    if open_cards:
        fact_lines.append("Open cards:\n" + open_cards)

    if fact_lines:
        parts.append("---\n" + "\n".join(fact_lines))

    return "\n\n".join(parts)


async def _build_session_title(summary: str) -> str:
    """Generate a short human-readable title for a closed session (spec-042 extension).

    Uses the already-built handoff summary as input (cheap: small text, single pass).
    Returns "" when summary is blank or the model call fails — never raises.
    The caller should only persist the title when a non-empty string is returned.
    """
    if not summary or not summary.strip():
        return ""
    try:
        handoff_model = os.environ.get("HANDOFF_MODEL", "haiku")
        opts = _ClaudeAgentOptions(
            model=handoff_model,
            permission_mode="bypassPermissions",
            cwd=_OPS_SCRATCH_CWD,  # scratch dir: transcript never pollutes project session list
            allowed_tools=[],
            disallowed_tools=[],
            effort="low",
        )
        truncated = summary[:1500]
        prompt = (
            "Produce a concise title of at most 6 words for a work-session history list. "
            "Output ONLY the title — no surrounding quotes, no trailing punctuation, no explanation.\n\n"
            + truncated
        )
        raw = await _haiku_summarize(prompt, opts)
        if not raw:
            return ""
        # Post-process: first line only, strip quotes/backticks, trailing period, collapse whitespace
        title = raw.splitlines()[0]
        title = title.strip().strip("\"'`")
        if title.endswith("."):
            title = title[:-1]
        title = " ".join(title.split())
        title = title[:60]
        return title
    except Exception as exc:
        print(f"[handoff] _build_session_title failed (non-blocking): {exc!r}")
        return ""


async def _haiku_summarize(prompt: str, opts: "_ClaudeAgentOptions") -> str:
    """Send a one-shot prompt to haiku (or HANDOFF_MODEL) and return the text response.

    Returns "" on failure or empty response. Never raises.
    Mirrors the pattern used in engine.py reconcile_board (engine.py:1018-1030).
    """
    text_parts: list[str] = []
    try:
        async for msg in _sdk_query_handoff(prompt=prompt, options=opts):
            if isinstance(msg, _AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, _TextBlock) and blk.text.strip():
                        text_parts.append(blk.text)
    except Exception as exc:
        print(f"[handoff] haiku call failed: {exc!r}")
        return ""
    return "\n".join(text_parts).strip()


async def api_project_session_context(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/session-context?session_id=<opt.>
    Returns {read, edited, commands, session_id} for the active (or specified) session."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    sid = req.rel_url.query.get("session_id", "") or ctx["sessions"].get((project.get("session_key") or project.get("tg_thread", "")))
    if not sid:
        return web.json_response({"read": [], "edited": [], "commands": [], "session_id": None})

    # Sanitise basename-only (same as session-history)
    if sid != Path(sid).name or sid in (".", ".."):
        return web.json_response({"error": "invalid session_id"}, status=400)

    jsonl = _sdk_sessions_dir(project["cwd"]) / f"{sid}.jsonl"
    if not jsonl.is_file():
        return web.json_response({"read": [], "edited": [], "commands": [], "session_id": sid})

    data = _session_context(jsonl)
    data["session_id"] = sid
    return web.json_response(data)


# ─────────────────────────── Project memory (memory) ─────────────────────────

_MEMORY_MAX_SIZE = 256 * 1024  # 256 KB

# Valid slug for a memory file: lowercase letters/digits, dash, 2-62 chars total.
# MEMORY.md is allowed separately (index).
_MEMORY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.md$")


def _project_memory_dir(cwd: str) -> Path:
    """New project memory location: <cwd>/.claude-ops/memory/ — committed to repo."""
    return Path(cwd) / ".claude-ops" / "memory"


def _valid_memory_name(name: str) -> bool:
    """True if name is a valid slug.md without path components."""
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name == "MEMORY.md":
        return True
    return bool(_MEMORY_NAME_RE.match(name))


def _memory_read_all(cwd: str) -> tuple[list[dict], bool]:
    """Reads all *.md from the new location (.claude-ops/memory/).
    If the new location doesn't exist but the old (sdk) one does — AUTO-MIGRATE
    (copy files) then read the new location. This way delete/write operations
    (which work only with the new location) stop returning 404 for legacy memory.
    Returns (files, from_legacy). files = [{name, content}], MEMORY.md first."""
    new_dir = _project_memory_dir(cwd)
    if new_dir.is_dir():
        return _read_memory_dir(new_dir), False
    # Auto-migrate old location ~/.claude/projects/<cwd>/memory/ → .claude-ops/memory/
    old_dir = _sdk_sessions_dir(cwd) / "memory"
    if old_dir.is_dir():
        migrated = False
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            for f in old_dir.glob("*.md"):
                dest = new_dir / f.name
                if not dest.exists():
                    dest.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            migrated = True
        except Exception as e:
            print(f"[memory] auto-migration legacy→new failed for {cwd}: {e}")
        if migrated and new_dir.is_dir():
            return _read_memory_dir(new_dir), False
        # migration failed — read the old location as-is (legacy)
        return _read_memory_dir(old_dir), True
    return [], False


def _read_memory_dir(mem_dir: Path) -> list[dict]:
    """Helper: reads *.md from the specified memory directory."""
    files: list[dict] = []
    try:
        md_files = sorted(
            mem_dir.glob("*.md"),
            key=lambda p: (0 if p.name == "MEMORY.md" else 1, p.name),
        )
        for f in md_files:
            try:
                size = f.stat().st_size
                if size > _MEMORY_MAX_SIZE:
                    content = f"[file too large: {size} bytes]"
                else:
                    content = f.read_text(encoding="utf-8", errors="replace")
                files.append({"name": f.name, "content": content})
            except Exception:
                pass
    except Exception:
        pass
    return files


def _memory_write(cwd: str, name: str, content: str) -> None:
    """Atomically writes a memory file, creating the directory if absent.
    Then rebuilds the MEMORY.md index."""
    if not _valid_memory_name(name):
        raise ValueError(f"invalid memory file name: {name!r}")
    if len(content.encode("utf-8")) > _MEMORY_MAX_SIZE:
        raise ValueError("content exceeds _MEMORY_MAX_SIZE")
    mem_dir = _project_memory_dir(cwd)
    mem_dir.mkdir(parents=True, exist_ok=True)
    target = mem_dir / name
    # Atomic write via tmp
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    if name != "MEMORY.md":
        _memory_reindex(cwd)


def _memory_delete(cwd: str, name: str) -> bool:
    """Deletes a memory file. Returns True if deleted, False if not found."""
    if not _valid_memory_name(name):
        raise ValueError(f"invalid memory file name: {name!r}")
    if name == "MEMORY.md":
        raise ValueError("cannot delete MEMORY.md directly")
    target = _project_memory_dir(cwd) / name
    if not target.exists():
        return False
    target.unlink()
    _memory_reindex(cwd)
    return True


def _memory_reindex(cwd: str) -> None:
    """Rebuilds MEMORY.md as an index of all entries in .claude-ops/memory/.
    Line format: - [Title](file.md) — hook (from frontmatter or first line)."""
    mem_dir = _project_memory_dir(cwd)
    if not mem_dir.is_dir():
        return
    entries = sorted(
        (p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"),
        key=lambda p: p.name,
    )
    lines = ["# Project memory\n", "\n"]
    for entry in entries:
        try:
            raw = entry.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw = ""
        title, hook = _memory_parse_entry(entry.name, raw)
        line = f"- [{title}]({entry.name})"
        if hook:
            line += f" — {hook}"
        lines.append(line + "\n")
    index_path = mem_dir / "MEMORY.md"
    index_path.write_text("".join(lines), encoding="utf-8")


def _memory_parse_entry(filename: str, content: str) -> tuple[str, str]:
    """Extracts (title, hook) from a memory entry file.
    Title — from frontmatter 'title' or the first line with #/text.
    Hook — first non-empty sentence of the body after frontmatter."""
    lines = content.splitlines()
    idx = 0
    fm: dict[str, str] = {}
    # Parse YAML frontmatter (--- ... ---)
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            kv = lines[idx].split(":", 1)
            if len(kv) == 2:
                fm[kv[0].strip()] = kv[1].strip()
            idx += 1
        idx += 1  # skip closing ---

    # Title: from frontmatter or from first body line
    title = fm.get("title", "")
    if not title:
        for line in lines[idx:]:
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
            if line:
                title = line[:60]
                break
    if not title:
        title = filename[:-3]  # strip .md

    # Hook: first non-empty sentence of the body
    hook = fm.get("hook", "")
    if not hook:
        for line in lines[idx:]:
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("---"):
                hook = line[:100]
                break

    return title, hook


async def api_project_memory(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/memory
    Returns {files:[{name, content}], exists}.
    New location: <cwd>/.claude-ops/memory/. Fallback: old ~/.claude/projects/.
    MEMORY.md — first in the list (index)."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    files, _legacy = _memory_read_all(project["cwd"])
    if not files:
        return web.json_response({"files": [], "exists": False})
    return web.json_response({"files": files, "exists": True})


async def api_project_memory_write(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/memory/{name}
    Creates or updates a memory entry. Updates the MEMORY.md index.
    Returns the updated {files, exists} list."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    name = req.match_info["name"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    if not _valid_memory_name(name):
        return web.json_response({"error": "invalid file name"}, status=400)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    content = body.get("content", "")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be string"}, status=400)
    if len(content.encode("utf-8")) > _MEMORY_MAX_SIZE:
        return web.json_response({"error": "content too large"}, status=400)

    try:
        _memory_write(project["cwd"], name, content)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"write failed: {e}"}, status=500)

    files, _ = _memory_read_all(project["cwd"])
    return web.json_response({"files": files, "exists": True})


async def api_project_memory_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/memory/{name}
    Deletes a memory entry. Updates the MEMORY.md index.
    Returns the updated {files, exists} list."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    name = req.match_info["name"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    if not _valid_memory_name(name):
        return web.json_response({"error": "invalid file name"}, status=400)

    if name == "MEMORY.md":
        return web.json_response({"error": "cannot delete MEMORY.md"}, status=400)

    try:
        deleted = _memory_delete(project["cwd"], name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"delete failed: {e}"}, status=500)

    if not deleted:
        return web.json_response({"error": "not found"}, status=404)

    files, _ = _memory_read_all(project["cwd"])
    exists = bool(files)
    return web.json_response({"files": files, "exists": exists})


# ─────────────────────────── Project secrets (secrets) ──────────────────────────────────────
#
# Storage: <cwd>/.claude-ops/secrets/secrets.env (chmod 600, not in git)
# Format:  KEY=VALUE per line, # — comments, empty lines — ignored
# Security:
#   - Key names: ^[A-Z_][A-Z0-9_]*$ (env-compatible, anti-injection)
#   - Values are NEVER returned via API — only the list of names
#   - .claude-ops/secrets/ added to .gitignore on first write
#   - chmod 600 on secrets.env on every write
#   - Isolated by cwd: secrets of one project are not visible to another

_SECRETS_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRETS_MAX_VALUE_SIZE = 8 * 1024   # 8 KB per value
_SECRETS_MAX_KEYS = 100              # max keys per project


def _project_secrets_path(cwd: str) -> Path:
    """Path to secrets file: <cwd>/.claude-ops/secrets/secrets.env."""
    return Path(cwd) / ".claude-ops" / "secrets" / "secrets.env"


def _secrets_read(cwd: str) -> dict:
    """Reads KEY=VALUE from secrets.env. No file → {}.
    Comments (#) and empty lines are ignored."""
    path = _project_secrets_path(cwd)
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                if _SECRETS_KEY_RE.match(k):
                    result[k] = v
    except Exception:
        pass
    return result


# ─────────────────────────── Secret reference resolver (Spec 026 Phase 3) ──────
# A secret VALUE of the form  secret:<name>  is resolved at runtime via the
# built-in encrypted store (secretstore.py).  Plain values pass through UNCHANGED
# — the feature is inert until someone opts in by using the secret: prefix.
#
# Contract:
#   - Reference format:  secret:<name>   (case-sensitive, no leading space)
#   - Resolved via secretstore.get(name); None → RuntimeError (fail loud).
#   - Resolved values are NEVER logged or printed.
#   - No in-memory cache: the store is a local encrypted file; reads are cheap.
#     (The previous vw-based resolver had a 300-second TTL cache; that is dropped
#     here because local file I/O does not need it.  If profiling ever shows it is
#     a bottleneck, add caching inside secretstore.py.)

async def _resolve_secret_refs(secrets: dict) -> dict:
    """Resolve any secret: references in a project secrets dict.

    For each (key, value): if value is a str starting with 'secret:', the
    remainder is treated as a secret name in the built-in encrypted store and
    resolved via secretstore.get().  All other values pass through unchanged.

    Returns a new dict with the same keys.  Raises RuntimeError on any
    resolution failure — the caller's agent run must not proceed with a missing
    secret (fail loud, never inject empty).
    """
    result: dict = {}
    for k, v in secrets.items():
        if isinstance(v, str) and v.startswith("secret:"):
            # Resolve via built-in store (secretstore.py)
            secret_name = v[len("secret:"):]
            resolved = _secretstore.get(secret_name)
            if resolved is None:
                raise RuntimeError(
                    f"secret: name '{secret_name}' (env key '{k}') not found in the "
                    f"built-in secret store — add it with  `secret set {secret_name} <value>`"
                )
            result[k] = resolved
        else:
            result[k] = v
    return result


# ─────────────────────────── Global Vault API (Spec 026 Phase 3) ──────────────
# Name validation for the global vault — same character set as secretstore.py.
_VAULT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_log = logging.getLogger(__name__)


async def api_vault_list(req: web.Request) -> web.Response:
    """GET /api/secrets — list names and categories (NEVER values).

    Response: {"secrets": [{"name": ..., "category": ...}, ...]}
    Requires valid auth cookie (covered by auth_middleware).
    """
    try:
        metas = _secretstore.list_meta()
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)

    # Strip notes and values — only name + category for the list view
    return web.json_response({
        "secrets": [{"name": m["name"], "category": m["category"]} for m in metas]
    })


async def api_vault_get(req: web.Request) -> web.Response:
    """GET /api/secrets/{name} — reveal a single secret (value + metadata).

    The decrypted value is returned on-demand.  Every call is audit-logged
    (name only, never the value).
    Requires valid auth cookie.
    """
    name = req.match_info["name"]
    if not _VAULT_NAME_RE.match(name):
        return web.json_response({"error": "invalid secret name"}, status=400)

    try:
        entry = _secretstore.get_full(name)
    except (RuntimeError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=503)

    if entry is None:
        return web.json_response({"error": "secret not found"}, status=404)

    # Audit: log the reveal (name only — never log the value)
    _log.info("vault reveal: name=%r (value not logged)", name)

    return web.json_response({
        "name": entry["name"],
        "value": entry["value"],
        "category": entry.get("category", ""),
        "notes": entry.get("notes", ""),
        "updated_at": entry.get("updated_at", ""),
    })


async def api_vault_set(req: web.Request) -> web.Response:
    """POST /api/secrets — create or update a secret.

    Body: {"name": str, "value": str, "category": str (opt), "notes": str (opt)}
    Requires valid auth cookie.
    """
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    name = body.get("name", "")
    value = body.get("value", "")
    category = body.get("category", "")
    notes = body.get("notes", "")

    if not isinstance(name, str) or not _VAULT_NAME_RE.match(name):
        return web.json_response({"error": "invalid secret name"}, status=400)
    if not isinstance(value, str):
        return web.json_response({"error": "value must be a string"}, status=400)

    try:
        _secretstore.set(name, value, category=category, notes=notes)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)

    return web.json_response({"name": name, "category": category, "ok": True})


async def api_vault_delete(req: web.Request) -> web.Response:
    """DELETE /api/secrets/{name} — remove a secret.

    Requires valid auth cookie.
    """
    name = req.match_info["name"]
    if not _VAULT_NAME_RE.match(name):
        return web.json_response({"error": "invalid secret name"}, status=400)

    try:
        removed = _secretstore.delete(name)
    except (ValueError, RuntimeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not removed:
        return web.json_response({"error": "secret not found"}, status=404)

    return web.json_response({"name": name, "deleted": True})


# ─────────────────────────── TOTP enrollment API (Spec 026, Phase 2) ──────────
#
# All four endpoints require an authenticated cookie (covered by auth_middleware).
# The TOTP secret is stored encrypted in the built-in vault under reserved names:
#   __totp_secret__   — the ACTIVE base32 TOTP secret
#   __totp_pending__  — staged secret during enrollment (not yet active)
#   __totp_recovery__ — JSON array of sha256 hashes of one-time recovery codes
#
# Break-glass (host shell): `secret rm __totp_secret__`
# This removes the active secret and falls back to password-only login instantly.


async def api_totp_status(req: web.Request) -> web.Response:
    """GET /api/auth/totp/status — report whether TOTP is currently active.

    Response: {"enabled": bool}
    Requires valid auth cookie.
    """
    try:
        active = _secretstore.get("__totp_secret__")
    except Exception:
        active = None
    return web.json_response({"enabled": bool(active)})


async def api_totp_enroll(req: web.Request) -> web.Response:
    """POST /api/auth/totp/enroll — begin TOTP enrollment.

    Generates a fresh TOTP secret and 10 recovery codes.  Stores the secret
    as __totp_pending__ (NOT active yet — operator must call /activate to
    confirm they can produce a valid code).

    Response (one-time — codes are not stored plaintext after this):
        {
            "secret": "<base32>",
            "otpauth_uri": "otpauth://totp/...",
            "recovery_codes": ["xxxx-xxxx", ...]
        }

    The recovery_codes list is shown ONCE here; after /activate only the
    SHA-256 hashes are retained in __totp_recovery__.
    Requires valid auth cookie.
    """
    secret = _totp.random_base32(32)
    codes = _totp.gen_recovery_codes(10)

    # Retrieve issuer / account from env / ctx for the provisioning URI
    ctx = req.app["ctx"]
    issuer = os.environ.get("TOTP_ISSUER", "ClaudeOps")
    account = os.environ.get("OPERATOR_NAME", "operator")

    uri = _totp.provisioning_uri(secret, account=account, issuer=issuer)

    # Persist the pending secret (overwrites any previous pending enrollment)
    try:
        _secretstore.set("__totp_pending__", secret, category="totp",
                         notes="pending TOTP enrollment — not yet active")
    except Exception as exc:
        return web.json_response({"error": f"store error: {exc}"}, status=503)

    return web.json_response({
        "secret": secret,
        "otpauth_uri": uri,
        "recovery_codes": codes,
    })


async def api_totp_activate(req: web.Request) -> web.Response:
    """POST /api/auth/totp/activate — confirm enrollment with a valid TOTP code.

    Body: {"code": "<6-digit TOTP>"}

    Verifies *code* against __totp_pending__.  On success:
      - Promotes pending → __totp_secret__ (active)
      - Generates + stores fresh recovery code hashes in __totp_recovery__
      - Deletes __totp_pending__
      - Returns {"enabled": true}

    On bad code → 400 {"error": "totp_invalid"}
    On no pending secret → 400 {"error": "no_pending_enrollment"}
    Requires valid auth cookie.
    """
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    code = str(body.get("code", "")).strip()

    try:
        pending = _secretstore.get("__totp_pending__")
    except Exception:
        pending = None

    if not pending:
        return web.json_response({"error": "no_pending_enrollment"}, status=400)

    if not _totp.verify(pending, code):
        return web.json_response({"error": "totp_invalid"}, status=400)

    # Enrollment confirmed — generate 10 fresh recovery codes and store hashes
    codes = _totp.gen_recovery_codes(10)
    hashes = [_totp.hash_code(c) for c in codes]

    try:
        _secretstore.set("__totp_secret__", pending, category="totp",
                         notes="active TOTP secret")
        _secretstore.set("__totp_recovery__", json.dumps(hashes), category="totp",
                         notes="SHA-256 hashes of one-time recovery codes")
        _secretstore.delete("__totp_pending__")
    except Exception as exc:
        return web.json_response({"error": f"store error: {exc}"}, status=503)

    return web.json_response({
        "enabled": True,
        "recovery_codes": codes,  # shown once — operator must save these
    })


async def api_totp_disable(req: web.Request) -> web.Response:
    """DELETE /api/auth/totp — disable TOTP (authenticated break-glass via cockpit).

    Removes __totp_secret__, __totp_recovery__, and __totp_pending__ from the
    vault.  Login immediately reverts to password-only.

    Shell break-glass (no cockpit access needed):
        secret rm __totp_secret__

    Response: {"enabled": false}
    Requires valid auth cookie.
    """
    try:
        _secretstore.delete("__totp_secret__")
    except Exception:
        pass
    try:
        _secretstore.delete("__totp_recovery__")
    except Exception:
        pass
    try:
        _secretstore.delete("__totp_pending__")
    except Exception:
        pass
    return web.json_response({"enabled": False})


# ──────────────────────────────────────────────────────────────────────────────


def _secrets_ensure_gitignore(cwd: str) -> None:
    """Ensures .claude-ops/secrets/ is in the project .gitignore.
    Appends the line if absent."""
    gitignore = Path(cwd) / ".gitignore"
    line = ".claude-ops/secrets/"
    try:
        if gitignore.exists():
            content = gitignore.read_text(encoding="utf-8")
            if line in content:
                return
            # Append to end
            if not content.endswith("\n"):
                content += "\n"
            content += f"{line}\n"
        else:
            content = f"{line}\n"
        gitignore.write_text(content, encoding="utf-8")
    except Exception:
        pass


def _secrets_write(cwd: str, data: dict) -> None:
    """Atomically writes secrets.env (tmp+replace), chmod 600, mkdir.
    Ensures .claude-ops/secrets/ is in .gitignore."""
    path = _project_secrets_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# Project secrets — DO NOT commit or share with third parties\n"]
    for k, v in sorted(data.items()):
        lines.append(f"{k}={v}\n")

    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    # chmod 600 on final file (in case replace didn't preserve permissions on some FS)
    try:
        path.chmod(0o600)
    except Exception:
        pass

    _secrets_ensure_gitignore(cwd)


def _secrets_set(cwd: str, key: str, value: str) -> None:
    """Sets (adds/updates) one KEY=VALUE pair."""
    if not _SECRETS_KEY_RE.match(key):
        raise ValueError(f"invalid key name: {key!r}")
    if len(value.encode("utf-8")) > _SECRETS_MAX_VALUE_SIZE:
        raise ValueError("value too large (max 8KB)")
    data = _secrets_read(cwd)
    if key not in data and len(data) >= _SECRETS_MAX_KEYS:
        raise ValueError(f"too many keys (max {_SECRETS_MAX_KEYS})")
    data[key] = value
    _secrets_write(cwd, data)


def _secrets_delete(cwd: str, key: str) -> bool:
    """Deletes a key. Returns True if deleted, False if it didn't exist."""
    if not _SECRETS_KEY_RE.match(key):
        raise ValueError(f"invalid key name: {key!r}")
    data = _secrets_read(cwd)
    if key not in data:
        return False
    del data[key]
    _secrets_write(cwd, data)
    return True


# ─────────────────────────── Secrets API (CRUD) ─────────────────────────────


async def api_project_secrets(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/secrets — list of key NAMES (no values).
    ⚠️ Secret values are never returned to the client."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


async def api_project_secrets_set(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/secrets/{key} — set a secret.
    Body: {value: str}. Returns updated list of names (no values)."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    key = req.match_info["key"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Anti-traversal: key name must not contain path components
    if "/" in key or "\\" in key or ".." in key:
        return web.json_response({"error": "invalid key name"}, status=400)
    if not _SECRETS_KEY_RE.match(key):
        return web.json_response({"error": "invalid key name (must match ^[A-Z_][A-Z0-9_]*$)"}, status=400)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request body"}, status=400)

    value = body.get("value", "")
    if not isinstance(value, str):
        return web.json_response({"error": "value must be string"}, status=400)

    try:
        _secrets_set(project["cwd"], key, value)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"write failed: {e}"}, status=500)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


async def api_project_secrets_delete(req: web.Request) -> web.Response:
    """DELETE /api/projects/{id}/secrets/{key} — delete a secret."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    key = req.match_info["key"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    # Anti-traversal
    if "/" in key or "\\" in key or ".." in key:
        return web.json_response({"error": "invalid key name"}, status=400)
    if not _SECRETS_KEY_RE.match(key):
        return web.json_response({"error": "invalid key name"}, status=400)

    try:
        deleted = _secrets_delete(project["cwd"], key)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"delete failed: {e}"}, status=500)

    if not deleted:
        return web.json_response({"error": "key not found"}, status=404)

    data = _secrets_read(project["cwd"])
    return web.json_response({"keys": sorted(data.keys()), "exists": bool(data)})


# ─────────────────────────── new project: templates + initialisation ───────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$")

# ── Archetype helpers ──────────────────────────────────────────────────────────

def _infer_archetype(intent: str) -> str:
    """Infer project archetype from intent string.

    Returns one of: 'software' | 'content' | 'ops' | 'scratchpad'.
    Default is 'software' — most common for power users.
    """
    text = intent.lower()
    software_kw = {
        "build", "code", "app", "bot", "api", "service", "deploy", "backend", "frontend",
        "website", "web", "server", "library", "package", "cli", "script", "nextjs", "react",
        "python", "node", "django", "flask", "fastapi", "docker", "kubernetes",
    }
    ops_kw = {
        "automate", "automation", "infra", "infrastructure", "pipeline", "workflow", "monitor",
        "devops", "cron", "schedule", "backup", "migrate", "setup", "configure",
        "install", "provision",
    }
    content_kw = {
        "write", "blog", "post", "article", "research", "study", "plan", "document",
        "report", "essay", "book", "content", "draft", "newsletter", "marketing", "seo",
        "copy", "creative",
    }
    words = set(text.split())
    if words & software_kw:
        return "software"
    if words & ops_kw:
        return "ops"
    if words & content_kw:
        return "content"
    return "software"


def _intent_to_slug(intent: str) -> str:
    """Derive a kebab-case slug from an intent string."""
    # Normalize unicode → ASCII approximation
    text = unicodedata.normalize("NFKD", intent).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    # Truncate to 40 chars, trim trailing dash
    text = text[:40].rstrip("-")
    if len(text) < 2:
        return ""
    return text


def _intent_to_display_name(intent: str) -> str:
    """Derive a human-friendly display name from intent string (max 5 words, title-cased)."""
    words = intent.strip().split()[:5]
    return " ".join(w.capitalize() for w in words) if words else ""


def _render_template_archetype(template_name: str, vars: dict, here: Path, project_type: str) -> str:
    """Render a template file, stripping archetype-conditional blocks.

    Supported markers (must be on their own lines):
      {{#if_software_ops}} ... {{/if_software_ops}}  — kept for software/ops, removed for others
      {{#if_content}} ... {{/if_content}}             — kept for content, removed for others
      {{#if_scratchpad}} ... {{/if_scratchpad}}       — kept for scratchpad, removed for others
    """
    text = _render_template(template_name, vars, here)

    def _process_block(t: str, tag: str, keep: bool) -> str:
        open_tag = "{{#" + tag + "}}"
        close_tag = "{{/" + tag + "}}"
        while open_tag in t:
            start = t.index(open_tag)
            end = t.index(close_tag, start) + len(close_tag)
            inner = t[start + len(open_tag):t.index(close_tag, start)]
            if keep:
                t = t[:start] + inner + t[end:]
            else:
                # Remove the block including any surrounding blank line
                block = t[start:end]
                t = t[:start] + t[end:]
                # Clean up double blank lines left behind
                t = re.sub(r"\n{3,}", "\n\n", t)
        return t

    is_software_ops = project_type in ("software", "ops")
    text = _process_block(text, "if_software_ops", is_software_ops)
    text = _process_block(text, "if_content", project_type == "content")
    text = _process_block(text, "if_scratchpad", project_type == "scratchpad")
    return text


def _build_onboarding_prompt(project_type: str, cwd: str, intent: str) -> str:
    """Build an archetype-aware onboarding prompt for a new project."""
    git_step = (
        "\n- After scaffolding: run `git init` + initial commit if not already done."
        if project_type in ("software", "ops") else ""
    )
    stack_step = (
        "\n- Ask about the stack (1 question) if not obvious from the intent."
        if project_type in ("software", "ops") else ""
    )
    error_handler_step = (
        "\nSTEP 3 — error handler:\n"
        "- If this is a service or bot, add a global error handler "
        "(FastAPI/aiohttp middleware, PTB add_error_handler, or CLI try/except in main → logger.error). "
        "The cockpit scanner greps for `UNHANDLED exc_class=<Type> path=<route>`. "
        "Without it the cockpit is blind to runtime errors.\n"
        "- Update ## ClaudeOps Integration Status in CLAUDE.md once set up."
        if project_type in ("software", "ops") else ""
    )

    return (
        f"New {project_type} project initialized. Folder: {cwd}.\n"
        f"Intent: \"{intent}\"\n\n"
        f"Starter files are in place. Your job: be a proactive partner, not an interrogator.\n\n"
        f"STEP 1 — Propose and scaffold immediately:\n"
        f"- Based on the intent \"{intent}\", infer the project goal, then:\n"
        f"- Rewrite the Goal section in CLAUDE.md (1-2 sentences about what and why).\n"
        f"- Add 3 real starter tasks to ## Backlog in TASKS.md (remove placeholder cards). "
        f"Make them specific and actionable: verb + object + done-criterion.\n"
        f"- End with ONE brief question: ask what's most important to clarify first, "
        f"or suggest \"start with task 1?\""
        f"{git_step}{stack_step}\n\n"
        f"STEP 2 — After my response:\n"
        f"- Adapt CLAUDE.md further based on what I say.\n"
        f"- If I mentioned existing code/files → scan them (Read a few), brief summary.\n"
        f"- Fill in README.md minimally."
        f"{error_handler_step}\n\n"
        f"Keep it lean. Propose, don't interrogate. Lead with action, not questions."
    )

def _build_audit_prompt(ctx: dict, project_name: str) -> str:
    """Audit prompt: preamble + baseline checklist from templates/reference/audit-prompt.md.
    The baseline lives in a file — edit it there without touching the code."""
    here: Path = ctx["HERE"]
    base = (here / "templates" / "reference" / "audit-prompt.md").read_text(encoding="utf-8")
    return (
        f"🩺 Audit of project **{project_name}**.\n\n"
        f"Go through this checklist (baseline below). For EACH problem found, create "
        f"a new card in `## Backlog` of `TASKS.md` (format: `- [ ] text` strictly inside the section; "
        f"the `ops:ID` marker will be added automatically — don't add it manually).\n\n"
        f"At the end — a short summary in chat: 'N problems found, M cards created'.\n\n"
        f"---\n\n{base}"
    )


def _render_template(template_name: str, vars: dict, here: Path) -> str:
    """Reads templates/<template_name>, replaces {{var}} → value from vars."""
    tpl_path = here / "templates" / template_name
    try:
        text = tpl_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"template not found: {tpl_path}")
    for key, val in vars.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


async def api_new_project(req: web.Request) -> web.Response:
    """POST /api/projects/new — creates a new project folder with starter templates and
    launches initialisation via run_engine (like an F1 card).

    Body (all optional):
      intent: str  — what the user wants to work on (free-text)
      type:   str  — archetype override: 'software'|'content'|'ops'|'scratchpad'
      name:   str  — legacy display name override (kept for back-compat)
    """
    ctx = req.app["ctx"]
    run_engine = ctx.get("run_engine")

    # Parse body
    try:
        body = await req.json()
    except Exception:
        body = {}

    intent: str = (body.get("intent") or "").strip()
    name: str = (body.get("name") or "").strip()  # legacy compat
    project_type: str = (body.get("type") or "").strip()

    # Infer archetype from intent if not supplied
    if not project_type:
        project_type = _infer_archetype(intent) if intent else "software"

    # Derive slug from intent; fall back to untitled-<ts>
    ts = int(time.time())
    derived_slug = _intent_to_slug(intent) if intent else ""
    slug = derived_slug if derived_slug else f"untitled-{ts}"

    # Derive display name: intent-derived > legacy name arg > slug
    if intent:
        display_name = _intent_to_display_name(intent) or slug
    elif name:
        display_name = name
    else:
        display_name = slug

    # Create folder ~/projects/<slug>/
    projects_dir = Path.home() / "projects"
    projects_dir.mkdir(exist_ok=True)
    cwd = projects_dir / slug

    # If slug already exists (e.g. from a previous run with same intent), disambiguate with ts
    if cwd.exists():
        slug = f"{slug}-{ts}"
        cwd = projects_dir / slug

    try:
        cwd.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return web.json_response({"error": f"folder already exists: {cwd}"}, status=409)

    here: Path = ctx["HERE"]
    tpl_vars = {
        "name": display_name,
        "date": time.strftime("%Y-%m-%d"),
        "slug": slug,
        "type": project_type,
    }

    # Write templates (archetype-aware)
    try:
        (cwd / "CLAUDE.md").write_text(
            _render_template_archetype("CLAUDE.md.tpl", tpl_vars, here, project_type),
            encoding="utf-8",
        )
        (cwd / "README.md").write_text(
            _render_template("README.md.tpl", tpl_vars, here),
            encoding="utf-8",
        )

        # .gitignore only for software/ops
        if project_type in ("software", "ops"):
            (cwd / ".gitignore").write_text(
                _render_template(".gitignore.tpl", tpl_vars, here),
                encoding="utf-8",
            )

        # TASKS.md: render template (archetype-aware), then parse and add init card to In Progress
        tasks_raw = _render_template_archetype("TASKS.md.tpl", tpl_vars, here, project_type)
        preamble, cols = _parse_tasks(tasks_raw)
        init_card = {"id": _new_card_id(), "text": "Initialise project"}
        cols["in_progress"].append(init_card)
        (cwd / "TASKS.md").write_text(_serialize_tasks(preamble, cols, display_name), encoding="utf-8")
    except Exception as e:
        # Rollback: delete folder if template writes failed
        shutil.rmtree(str(cwd), ignore_errors=True)
        return web.json_response({"error": f"error writing templates: {e}"}, status=500)

    # git init for software/ops (non-fatal)
    if project_type in ("software", "ops"):
        try:
            _git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Claude-Ops",
                "GIT_AUTHOR_EMAIL": "claude-ops@localhost",
                "GIT_COMMITTER_NAME": "Claude-Ops",
                "GIT_COMMITTER_EMAIL": "claude-ops@localhost",
            }
            subprocess.run(["git", "init"], cwd=str(cwd), check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial scaffold"],
                cwd=str(cwd), check=True, capture_output=True, env=_git_env,
            )
            print(f"[new_project] git init + initial commit in {cwd}")
        except Exception as e:
            print(f"[new_project] git init failed (non-fatal): {e}")

    # Register in topics.json.
    # spec-040 Phase 0+D: new projects get a slug-based session key (transport-neutral).
    # TG forum topic creation removed in Phase D.
    pid = _project_id(str(cwd))
    session_key = pid  # slug key — canonical from Phase 0 onward

    topic_entry: dict = {
        "id": str(_uuid.uuid4()),  # spec-046: stable UUID for future migration
        "project": display_name,
        "cwd": str(cwd),
        "model": _effective_default_model(ctx),
        "type": project_type,
    }

    ctx["topics"][session_key] = topic_entry
    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    project = _find_project_by_id(ctx, pid)
    if project is None:
        # Build a minimal object in case a cwd duplicate displaced our entry
        project = {
            "id": pid,
            "name": display_name,
            "cwd": str(cwd),
            "model": _effective_default_model(ctx),
            "session_key": session_key,
            "is_free": False,
        }

    # If run_engine is unavailable — return without launching (degraded mode)
    if run_engine is None:
        return web.json_response({
            "id": pid, "cwd": str(cwd), "name": display_name,
            "session_key": session_key, "started": False,
        })

    # Check lock (theoretically free slot — just created)
    if ctx["running"].get(session_key) is not None:
        return web.json_response({
            "id": pid, "cwd": str(cwd), "name": display_name,
            "session_key": session_key, "started": False,
        })

    # Reserve slot SYNCHRONOUSLY (race guard — same as in api_move_task)
    ctx["running"][session_key] = True

    # Build archetype-aware onboarding prompt and assign to init_card
    init_card["text"] = _build_onboarding_prompt(project_type, str(cwd), intent)
    # Use the project's default model for onboarding (not board_card_model)
    init_card["model"] = _effective_default_model(ctx)
    _spawn_bg(_run_card(ctx, req.app, project, init_card, session_key))

    return web.json_response({
        "id": pid,
        "cwd": str(cwd),
        "name": display_name,
        "session_key": session_key,
        "started": True,
    })


async def api_project_rename(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/rename  {slug: str}
    Renames the project folder and updates all topics.json entries with the same cwd."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    slug = (body.get("slug") or "").strip()
    if not slug:
        return web.json_response({"error": "slug is required"}, status=400)
    if not _SLUG_RE.match(slug):
        return web.json_response(
            {"error": "slug must match ^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$ (kebab-case)"},
            status=400,
        )

    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "project busy, cannot rename"}, status=409)

    old_cwd = Path(project["cwd"])
    new_cwd = old_cwd.parent / slug

    if new_cwd.exists():
        return web.json_response({"error": f"folder already taken: {new_cwd}"}, status=409)

    try:
        shutil.move(str(old_cwd), str(new_cwd))
    except Exception as e:
        return web.json_response({"error": f"rename error: {e}"}, status=500)

    # Update all topics entries with the same old cwd
    old_cwd_str = str(old_cwd)
    for b in ctx["topics"].values():
        if b.get("cwd") == old_cwd_str:
            b["cwd"] = str(new_cwd)
            b["project"] = slug

    save_topics = ctx.get("save_topics")
    if callable(save_topics):
        save_topics()

    # Migrate cwd-keyed state (SDK conversation history + Timeline),
    # otherwise after cwd changes sessions and feed "disappear" — files stay under the old slug.
    migrate_warnings = _migrate_cwd_keyed_state(old_cwd_str, str(new_cwd), ctx)

    # Sync TG forum topic name (if the project has a real topic)
    await _sync_forum_topic_name(ctx, session_key, slug)

    resp_body = {
        "ok": True,
        "new_id": new_cwd.name,
        "new_cwd": str(new_cwd),
        "new_name": slug,
    }
    if migrate_warnings:
        resp_body["warnings"] = migrate_warnings
    return web.json_response(resp_body)


_DETECT_ERROR_HANDLER_SUBSTRINGS = (
    "@app.exception_handler",
    "add_error_handler",
    "error_middleware",
    "app.add_middleware",
    "@exception_handler",
    "setup_exception_handlers",
    "UNHANDLED exc_class=",   # project uses the cockpit's standard log line
)
_DETECT_EH_CONFORMANCE_RE = re.compile(r"(?im)^\s*-?\s*error handler:\s*(.+)$")
_DETECT_EH_EXCLUDE_DIRS = {"venv", ".venv", "node_modules", ".git", "dist", "build", "__pycache__", ".worktrees"}


def _detect_error_handler(cwd: Path, claude_md_text: str) -> bool:
    """Fast (bounded) detector for the presence of a global error handler in the project.

    (a) Self-declaration: ## ClaudeOps conformance + line 'error handler: <non-empty/no>'
    (b) Code heuristic: walk *.py (up to 60 files / 3 MB), look for substring markers.
    Returns True on first match. try/except → False on any error."""
    try:
        # (a) Self-declaration — ONLY in section ## ClaudeOps conformance
        # (otherwise 'error handler:' from any other section would give a false positive)
        if "## ClaudeOps conformance" in claude_md_text:
            section = claude_md_text.split("## ClaudeOps conformance", 1)[1].split("\n## ", 1)[0]
            m = _DETECT_EH_CONFORMANCE_RE.search(section)
            if m:
                val = m.group(1).strip().lower()
                if val not in {"нет", "no", "-", "—", ""}:
                    return True

        # (b) Code heuristic — bounded scan; os.walk prunes noisy directories,
        # not descending into venv/node_modules (rglob would anyway).
        files_checked = 0
        bytes_read = 0
        _MAX_FILES = 60
        _MAX_BYTES = 3 * 1024 * 1024  # 3 MB
        for root, dirs, names in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in _DETECT_EH_EXCLUDE_DIRS]
            for name in names:
                if not name.endswith(".py"):
                    continue
                if files_checked >= _MAX_FILES or bytes_read >= _MAX_BYTES:
                    return False
                try:
                    text = Path(root, name).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                files_checked += 1
                bytes_read += len(text)
                for substr in _DETECT_ERROR_HANDLER_SUBSTRINGS:
                    if substr in text:
                        return True
    except Exception:
        return False
    return False


async def api_project_health(req: web.Request) -> web.Response:
    """GET /api/projects/{id}/health — quick project structure check without an agent."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    cwd = Path(project["cwd"])

    def _check(key: str, label: str, condition: bool, hint: str | None, optional: bool = False) -> dict:
        return {"key": key, "label": label, "ok": condition, "hint": hint if not condition else None, "optional": optional}

    items: list[dict] = []

    # 1. CLAUDE.md exists
    claude_md = cwd / "CLAUDE.md"
    has_claude_md = claude_md.is_file()
    items.append(_check("claude_md", "CLAUDE.md", has_claude_md, "Create CLAUDE.md with a project description"))

    # 2. CLAUDE.md contains the "Cockpit rules" section
    cockpit_rules = False
    claude_md_text = ""
    if has_claude_md:
        try:
            claude_md_text = claude_md.read_text(encoding="utf-8", errors="replace")
            cockpit_rules = "Правила работы в кокпите" in claude_md_text
        except Exception:
            pass
    items.append(_check(
        "claude_md_cockpit_rules", "Cockpit rules section",
        cockpit_rules, "Run audit or ✏️ update manually",
    ))

    # 3. TASKS.md exists with preamble
    tasks_md = cwd / "TASKS.md"
    has_tasks = False
    if tasks_md.is_file():
        try:
            tasks_content = tasks_md.read_text(encoding="utf-8", errors="replace")
            # Presence of any ops-marker OR the "Формат карточки" preamble marker is enough
            has_tasks = "<!--ops:" in tasks_content or "Формат карточки" in tasks_content
        except Exception:
            pass
    items.append(_check("tasks_md", "TASKS.md with preamble", has_tasks, "Create TASKS.md with column format"))

    # 4. README.md exists (any case)
    has_readme = any((cwd / name).is_file() for name in _README_CANDIDATES)
    items.append(_check("readme", "README.md", has_readme, "Run audit"))

    # 5. .gitignore exists and contains .env
    gitignore = cwd / ".gitignore"
    has_gitignore_env = False
    if gitignore.is_file():
        try:
            has_gitignore_env = ".env" in gitignore.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    items.append(_check("gitignore", ".gitignore with .env", has_gitignore_env, "Add .env to .gitignore"))

    # 6. git init (.git folder exists) — if git is disabled by setting, don't require it
    if not _git_enabled(project):
        items.append(_check("git_init", "git (disabled in settings)", True, None))
    else:
        has_git = (cwd / ".git").exists()
        items.append(_check("git_init", "git init", has_git, "Run git init in the project folder"))

    # ── Capability checks ──────────────────────────────────────────────────────
    # cap_log_cmd: cockpit receives logs and runtime errors only if log_cmd is set
    items.append(_check(
        "cap_log_cmd", "log_cmd set (logs to cockpit)",
        bool(project.get("log_cmd")),
        "Set log_cmd — otherwise the cockpit won't see logs and runtime errors",
    ))
    # cap_error_handler: global error handler in code or declared in CLAUDE.md
    items.append(_check(
        "cap_error_handler", "Global error handler",
        _detect_error_handler(cwd, claude_md_text),
        "Add a global error handler to the service/bot (writes errors to log) "
        "OR declare it in CLAUDE.md (## ClaudeOps conformance)",
    ))
    # cap_test_cmd: optional — does not affect score
    items.append(_check(
        "cap_test_cmd", "test_cmd set (opt., via button)",
        bool(project.get("test_cmd")),
        "Opt.: set test_cmd for the 'Run tests' button and quality gate",
        optional=True,
    ))

    score = sum(1 for i in items if i["ok"] and not i.get("optional"))
    total = sum(1 for i in items if not i.get("optional"))
    if total == 0:
        color = "green"
    elif score == total:
        color = "green"
    elif score >= total / 2:
        color = "yellow"
    else:
        color = "red"

    return web.json_response({"items": items, "score": score, "total": total, "color": color})


async def api_project_audit(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/audit — creates an audit card and launches it via run_engine."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    run_engine = ctx.get("run_engine")
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    cwd = project["cwd"]
    name = project["name"]

    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "project busy"}, status=409)

    # Create audit card in In Progress
    audit_card = {"id": _new_card_id(), "text": f"🩺 Audit project '{name}'"}
    audit_prompt = _build_audit_prompt(ctx, name)

    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        cols["in_progress"].append(audit_card)
        _save_board(cwd, name, preamble, cols)

    if run_engine is None:
        return web.json_response({"ok": True, "card_id": audit_card["id"], "started": False})

    # Reserve slot SYNCHRONOUSLY
    ctx["running"][session_key] = True

    # Replace card text with full prompt before launching
    audit_card["text"] = audit_prompt
    _spawn_bg(_run_card(ctx, req.app, project, audit_card, session_key))

    return web.json_response({"ok": True, "card_id": audit_card["id"], "started": True})


_UPGRADE_PROMPT_TPL = """🔧 Bring project '{name}' up to cockpit standard.

IMPORTANT: Do NOT rewrite existing content of CLAUDE.md/TASKS.md/README.md/.gitignore — only ADD what is missing. If a file doesn't exist — create it from the template.

Reference templates are in `{tpl_dir}`:
- `CLAUDE.md.tpl` — structural example; **must** contain a "Cockpit rules" section — copy it into the project CLAUDE.md (if not already there), replace `{{{{name}}}}` variables with the actual name.
- `TASKS.md.tpl` — card format preamble. If the current TASKS.md lacks a preamble with the phrase «Формат карточки» — add it BEFORE the first `##` column.
- `README.md.tpl` — if README is absent, create a minimal one.
- `.gitignore.tpl` — if the current file lacks `.env` — add a Secrets section.

Steps:
1. Read `CLAUDE.md`, `TASKS.md`, `README.md`, `.gitignore` (if present) in the current cwd.
2. Read the templates in `{tpl_dir}/*.tpl`.
3. For each missing block — add it, preserving all existing content.
4. DO NOT TOUCH cards in TASKS.md — only the preamble above the first `##`.
5. At the end — a short summary in chat: 'Added/updated: A, B, C; left untouched: X, Y'.
"""


async def api_project_upgrade(req: web.Request) -> web.Response:
    """POST /api/projects/{id}/upgrade — '🔧 Bring up to standard' card: supplements CLAUDE.md/TASKS.md/README/.gitignore from templates without overwriting existing content."""
    ctx = req.app["ctx"]
    pid = req.match_info["id"]
    project = _find_project_by_id(ctx, pid)
    if project is None:
        return web.json_response({"error": "project not found"}, status=404)

    run_engine = ctx.get("run_engine")
    session_key = (project.get("session_key") or project.get("tg_thread", ""))
    cwd = project["cwd"]
    name = project["name"]

    if ctx["running"].get(session_key) is not None:
        return web.json_response({"error": "project busy"}, status=409)

    card = {"id": _new_card_id(), "text": f"🔧 Bring '{name}' up to standard"}
    here: Path = ctx.get("HERE") or Path(__file__).resolve().parent
    tpl_dir = str(here / "templates")
    prompt = _UPGRADE_PROMPT_TPL.format(name=name, tpl_dir=tpl_dir)

    async with _get_board_lock(cwd):
        _, preamble, cols = _load_board(cwd)
        cols["in_progress"].append(card)
        _save_board(cwd, name, preamble, cols)

    if run_engine is None:
        return web.json_response({"ok": True, "card_id": card["id"], "started": False})

    ctx["running"][session_key] = True
    card["text"] = prompt
    _spawn_bg(_run_card(ctx, req.app, project, card, session_key))
    return web.json_response({"ok": True, "card_id": card["id"], "started": True})


# ─────────────────────────── static files (SPA) ───────────────────────────

PLACEHOLDER_HTML = (
    "Frontend not built yet: cd web && npm install && npm run build"
)


async def spa_handler(req: web.Request) -> web.Response:
    """Serves static files from web/dist. SPA routing — fallback to index.html."""
    dist: Path = req.app["ctx"]["HERE"] / "web" / "dist"
    index = dist / "index.html"

    # If dist doesn't exist at all — show placeholder
    if not dist.exists() or not index.exists():
        return web.Response(text=PLACEHOLDER_HTML, content_type="text/plain")

    # Normalise path
    rel = req.path.lstrip("/") or "index.html"
    target = (dist / rel).resolve()

    # Cache policy: Vite emits content-hashed files under /assets/ — cache them
    # forever (immutable). Everything else (index.html, SPA fallback, favicon)
    # must revalidate so a new deploy is picked up without a manual hard-refresh.
    def _cache_headers(path_rel: str) -> dict:
        if path_rel.startswith("assets/"):
            return {"Cache-Control": "public, max-age=31536000, immutable"}
        return {"Cache-Control": "no-cache"}

    # Guard against escaping dist
    try:
        target.relative_to(dist.resolve())
    except ValueError:
        # path traversal → serve index (safe)
        return web.FileResponse(index, headers=_cache_headers("index.html"))

    if target.is_file():
        return web.FileResponse(target, headers=_cache_headers(rel))

    # SPA fallback
    return web.FileResponse(index, headers=_cache_headers("index.html"))


# ─────────────────────────── Spec-019: Schedules API ───────────────────────

async def api_schedules_get(req: web.Request) -> web.Response:
    """GET /api/schedules — returns normalised schedule registry.
    Query params: ?project=id, ?status=broken,stale, ?source=cron,systemd"""
    cache = _schedules._read_cache()
    records: list[dict] = cache.get("records", [])

    # Apply filters
    project_filter = req.rel_url.query.get("project", "").strip()
    status_filter = req.rel_url.query.get("status", "").strip()
    source_filter = req.rel_url.query.get("source", "").strip()

    if project_filter:
        records = [r for r in records if r.get("project") == project_filter]
    if status_filter:
        allowed = {s.strip() for s in status_filter.split(",") if s.strip()}
        records = [r for r in records if r.get("status") in allowed]
    if source_filter:
        allowed_src = {s.strip() for s in source_filter.split(",") if s.strip()}
        records = [r for r in records if r.get("source") in allowed_src]

    # Sort: next_run ascending (nulls last)
    def sort_key(r):
        nr = r.get("next_run")
        if nr is None:
            return (1, "")
        return (0, str(nr))

    records = sorted(records, key=sort_key)

    # Spec 020: merge deferred pending records
    deferred = _load_deferred()
    for dr in deferred:
        if dr.get("status") == "pending":
            trigger = "after rate-limit reset" if dr.get("fire_on_reset") else (dr.get("fire_at") or "?")
            records.append({
                "id": dr["id"],
                "source": "deferred",
                "schedule": trigger,
                "command": dr.get("prompt", "")[:80],
                "project": dr.get("project"),
                "last_run": None,
                "next_run": dr.get("fire_at"),
                "status": "ok",
                "purpose": f"Deferred: {dr.get('prompt', '')[:60]}",
                "annotations": {"fire_on_reset": dr.get("fire_on_reset", False), "deferred_id": dr["id"]},
            })

    return web.json_response({
        "scanned_at": cache.get("scanned_at"),
        "source_statuses": cache.get("source_statuses", []),
        "records": records,
    })


async def api_schedules_scan(req: web.Request) -> web.Response:
    """POST /api/schedules/scan — triggers immediate background re-scan."""
    ctx = req.app["ctx"]
    _spawn_bg(_schedules.run_scan(ctx))
    return web.json_response({"queued": True})


async def api_schedules_investigate(req: web.Request) -> web.Response:
    """POST /api/schedules/{id}/investigate — create Backlog card for investigation."""
    ctx = req.app["ctx"]
    record_id = req.match_info.get("id", "").strip()
    if not record_id:
        return web.json_response({"error": "missing id"}, status=400)
    result = await _schedules.investigate_schedule(ctx, record_id)
    if result is None:
        return web.json_response({"error": "schedule entry not found"}, status=404)
    return web.json_response(result)


# ─────────────────────────── entry point ───────────────────────────

async def start(ptb_app, ctx: dict) -> None:
    """Starts the aiohttp cockpit server in the same process/loop as the bot. Non-blocking."""
    # Ensure error_middleware (logging.exception) actually writes to the log,
    # even if the root logger hasn't been configured yet (otherwise ERROR goes to lastResort).
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING,
                             format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = ctx["port"]
    try:
        # Derive token once at startup (scrypt is slow — not per-request)
        ctx["_auth_token"] = _derive_token(ctx["password"])

        # Timeline: initialise bus persistence (DATA/timeline/)
        _timeline_init(ctx)
        # Chat message queue: initialise persistence and reload surviving items (DATA/chat-queue.json)
        _chat_queue_init(ctx)
        _settings_init(ctx)
        _ui_state_init(ctx)
        # Spec-012 Ph0: initialise paths to scan_state + dismissed_incidents files
        _scan_state_init(ctx)
        # Spec-019: Schedules registry — initialise file paths
        _schedules._schedules_init(ctx)
        # Spec-020: Deferred Runs — initialise file path
        _deferred_init(ctx)
        # Spec-021: cwd-lock dict — prevents simultaneous runs in the same working directory
        ctx["cwd_locks"] = {}
        # Seed built-in default prompt templates (merge, never overwrite operator entries)
        _seed_default_prompts(ctx)

        app = web.Application(
            middlewares=[security_headers_middleware, error_middleware, auth_middleware],
            client_max_size=20 * 1024 * 1024,
        )
        app["ctx"] = ctx

        # F1: save reference to PTB app for TG pings from _run_card
        app["ptb_app"] = ptb_app
        # Also put in ctx for access from _run_card via ctx["ptb_app"]
        ctx["ptb_app"] = ptb_app
        # Card Queue: save aiohttp app in ctx for _drain_queue from _run_card and loop
        ctx["_aiohttp_app"] = app

        # API routes
        app.router.add_get("/api/health", api_health)
        app.router.add_post("/api/login", api_login)
        app.router.add_post("/api/logout", api_logout)
        app.router.add_get("/api/me", api_me)
        app.router.add_get("/api/projects", api_projects)
        app.router.add_get("/api/settings", api_settings_get)
        app.router.add_post("/api/settings", api_settings_post)
        # Cross-device UI layout (open tabs/active/sidebar/split)
        app.router.add_get("/api/ui-state", api_ui_state_get)
        app.router.add_put("/api/ui-state", api_ui_state_put)
        app.router.add_get("/api/projects/{id}/settings", api_project_settings_get)
        app.router.add_post("/api/projects/{id}/settings", api_project_settings_post)
        # "+ New project" — creates untitled-<ts>/, adds to topics.json, spawns onboarding
        app.router.add_post("/api/projects/new", api_new_project)
        # Spec-023: Project Archive — static route MUST be before {id} routes
        app.router.add_get("/api/projects/archived", api_projects_archived)
        app.router.add_post("/api/projects/{id}/archive", api_project_archive)
        app.router.add_post("/api/projects/{id}/unarchive", api_project_unarchive)
        # Spec-025: Project Delete (trash + grace period)
        app.router.add_get("/api/projects/{id}/delete-precheck", api_project_delete_precheck)
        app.router.add_post("/api/projects/{id}/delete", api_project_delete)
        app.router.add_get("/api/trash", api_trash_list)
        app.router.add_post("/api/trash/{entry}/restore", api_trash_restore)
        # Spec-024: Project Groups
        app.router.add_post("/api/projects/{id}/group", api_project_group_set)
        app.router.add_get("/api/project-groups", api_project_groups_get)
        app.router.add_post("/api/project-groups", api_project_groups_manage)
        # Spec-030 Phase 1: atomic group management
        app.router.add_post("/api/project-groups/create", api_project_groups_create)
        app.router.add_post("/api/project-groups/rename", api_project_groups_rename)
        app.router.add_post("/api/project-groups/delete", api_project_groups_delete)
        app.router.add_post("/api/project-groups/reorder", api_project_groups_reorder)
        # Spec-031: Favorites
        app.router.add_post("/api/projects/{id}/favorite", api_project_favorite)
        app.router.add_get("/api/projects/{id}/claude-md", api_project_claude_md)
        app.router.add_post("/api/projects/{id}/claude-md", api_project_claude_md_write)
        app.router.add_get("/api/projects/{id}/readme", api_project_readme)
        app.router.add_post("/api/projects/{id}/readme", api_project_readme_write)
        app.router.add_get("/api/projects/{id}/specs", api_project_specs)
        app.router.add_get("/api/projects/{id}/specs/{name}", api_project_spec_content)
        app.router.add_get("/api/projects/{id}/logs", api_project_logs)
        app.router.add_get("/api/projects/{id}/activity", api_project_activity)
        # Task board (TASKS.md / DONE.md)
        app.router.add_get("/api/projects/{id}/tasks", api_project_tasks)
        app.router.add_post("/api/projects/{id}/tasks", api_create_task)
        app.router.add_get("/api/projects/{id}/tasks/done", api_tasks_done)
        app.router.add_post("/api/projects/{id}/tasks/{card}/move", api_move_task)
        app.router.add_delete("/api/projects/{id}/tasks/{card}", api_delete_task)
        app.router.add_route("PATCH", "/api/projects/{id}/tasks/{card}", api_update_task)
        # Card Queue: batch-launch multiple cards
        app.router.add_post("/api/projects/{id}/cards/run-batch", api_run_batch)
        # F1: card result sidecar
        app.router.add_get("/api/projects/{id}/tasks/{card}/run", api_card_run)
        # Card 5e1c0a: card spec sidecar (optional attached markdown doc)
        app.router.add_get("/api/projects/{id}/cards/{card}/spec", api_card_spec_get)
        app.router.add_put("/api/projects/{id}/cards/{card}/spec", api_card_spec_put)
        # C2-gate: apply / discard worktree card; quality gate (check)
        app.router.add_post("/api/projects/{id}/tasks/{card}/apply", api_card_apply)
        app.router.add_post("/api/projects/{id}/tasks/{card}/discard", api_card_discard)
        app.router.add_post("/api/projects/{id}/tasks/{card}/check", api_card_check)
        # C1: SSE chat for project
        app.router.add_post("/api/projects/{id}/chat", api_project_chat)
        # C1-stop: interrupt current agent run
        app.router.add_post("/api/projects/{id}/chat/stop", api_project_chat_stop)
        # Chat message queue (server-side persist across reload; editable/deletable)
        app.router.add_get("/api/projects/{id}/chat/queue", api_chat_queue_list)
        app.router.add_post("/api/projects/{id}/chat/queue", api_chat_queue_add)
        app.router.add_patch("/api/projects/{id}/chat/queue/{msg_id}", api_chat_queue_edit)
        app.router.add_delete("/api/projects/{id}/chat/queue/{msg_id}", api_chat_queue_delete)
        app.router.add_get("/api/projects/{id}/running", api_project_running)
        # ops:b2a081 — tab activity: operator marks project as seen (clears attention badge)
        app.router.add_post("/api/projects/{id}/seen", api_project_seen)
        # Spec-021: manual session rotation (wrap & reset)
        app.router.add_post("/api/projects/{id}/rotate", api_project_rotate)
        # Agent skills: global (~/.claude/skills/) + project (<cwd>/.claude/skills/)
        app.router.add_get("/api/projects/{id}/skills", api_project_skills)
        # Incident scanner: manual run + active err-card count
        app.router.add_post("/api/projects/{id}/scan-errors", api_project_scan_errors)
        app.router.add_get("/api/projects/{id}/incidents", api_project_incidents)
        app.router.add_post("/api/projects/{id}/incident", api_project_incident)
        app.router.add_post("/api/projects/{id}/notify-on-error", api_project_notify_toggle)
        # Activity-stream: live bus event stream (cards, external runs)
        app.router.add_get("/api/projects/{id}/activity-stream", api_project_activity_stream)
        # Spec-035: live turn snapshot (cold open + replay cursor)
        app.router.add_get("/api/projects/{id}/live", api_project_live)
        # Timeline: project event history (JSONL bus log) + pagination
        app.router.add_get("/api/projects/{id}/timeline", api_project_timeline)
        # Global stream of all events (for unread indicators in sidebar)
        app.router.add_get("/api/activity-stream", api_activity_stream_all)
        # Git sync — commit (if dirty) + push in one click
        app.router.add_post("/api/projects/{id}/git/sync", api_project_git_sync)
        # Project test runner (auto-detect pytest/npm/make)
        app.router.add_post("/api/projects/{id}/test", api_project_test)
        app.router.add_post("/api/projects/{id}/upload", api_project_upload)
        # Spec-038: serve agent-produced screenshots to the cockpit chat (auth-guarded)
        app.router.add_get("/api/projects/{id}/media/{filename}", api_project_media)
        # Project model change (takes effect on next request)
        app.router.add_post("/api/projects/{id}/model", api_project_set_model)
        # Subscription limits (5h + weekly) — for badge in tab bar
        app.router.add_get("/api/usage", api_usage)
        # Prompt templates (global, data/prompts.json)
        app.router.add_get("/api/prompts", api_prompts_list)
        app.router.add_post("/api/prompts", api_prompt_create)
        app.router.add_delete("/api/prompts/{id}", api_prompt_delete)
        app.router.add_route("PATCH", "/api/prompts/{id}", api_prompt_update)
        # Free chats (not bound to a project, cwd=$HOME)
        app.router.add_post("/api/free", api_free_create)
        app.router.add_post("/api/free/{id}/rename", api_free_rename)
        app.router.add_delete("/api/free/{id}", api_free_delete)
        # C2: project session management
        app.router.add_get("/api/projects/{id}/sessions", api_project_sessions)
        app.router.add_post("/api/projects/{id}/sessions/{sid}/label", api_project_session_label)
        app.router.add_post("/api/projects/{id}/session", api_project_set_session)
        app.router.add_get("/api/projects/{id}/session-history", api_project_session_history)
        # Spec-037: multi-chat per project
        app.router.add_get("/api/projects/{id}/chats", api_project_chats_list)
        app.router.add_post("/api/projects/{id}/chats", api_project_chats_create)
        app.router.add_route("PATCH", "/api/projects/{id}/chats/{chat_id}", api_project_chats_patch)
        app.router.add_delete("/api/projects/{id}/chats/{chat_id}", api_project_chats_delete)
        # File browser (read-only)
        app.router.add_get("/api/projects/{id}/files", api_project_files)
        app.router.add_get("/api/projects/{id}/file", api_project_file)
        # Global file browser (from $HOME, not bound to a project)
        app.router.add_get("/api/global/files", api_global_files)
        app.router.add_get("/api/global/file", api_global_file)
        app.router.add_post("/api/global/file", api_global_file_write)
        # Session context (read: Feature A)
        app.router.add_get("/api/projects/{id}/session-context", api_project_session_context)
        # Project memory (read+write: Feature B)
        app.router.add_get("/api/projects/{id}/memory", api_project_memory)
        app.router.add_post("/api/projects/{id}/memory/{name}", api_project_memory_write)
        app.router.add_delete("/api/projects/{id}/memory/{name}", api_project_memory_delete)
        # Project secrets (Spec 007): names only in API, values — agent via env only
        app.router.add_get("/api/projects/{id}/secrets", api_project_secrets)
        app.router.add_post("/api/projects/{id}/secrets/{key}", api_project_secrets_set)
        app.router.add_delete("/api/projects/{id}/secrets/{key}", api_project_secrets_delete)
        # Project folder rename (kebab-case slug)
        app.router.add_post("/api/projects/{id}/rename", api_project_rename)
        # Quick project structure check without an agent
        app.router.add_get("/api/projects/{id}/health", api_project_health)
        # Project audit: creates card + launches run_engine
        app.router.add_post("/api/projects/{id}/audit", api_project_audit)
        # '🔧 Bring up to standard' — supplements existing files with templates without overwriting
        app.router.add_post("/api/projects/{id}/upgrade", api_project_upgrade)

        # Spec-019: Schedules registry
        app.router.add_get("/api/schedules", api_schedules_get)
        app.router.add_post("/api/schedules/scan", api_schedules_scan)
        app.router.add_post("/api/schedules/{id}/investigate", api_schedules_investigate)

        # Spec-020: Deferred Runs
        app.router.add_post("/api/deferred", api_deferred_create)
        app.router.add_get("/api/deferred", api_deferred_list)
        app.router.add_delete("/api/deferred/{id}", api_deferred_delete)
        app.router.add_patch("/api/deferred/{id}", api_deferred_update)

        # Spec-026 Phase 3: Global Vault (built-in encrypted secret store)
        app.router.add_get("/api/secrets", api_vault_list)
        app.router.add_get("/api/secrets/{name}", api_vault_get)
        app.router.add_post("/api/secrets", api_vault_set)
        app.router.add_delete("/api/secrets/{name}", api_vault_delete)

        # Spec-026 Phase 2: TOTP second factor enrollment
        app.router.add_get("/api/auth/totp/status", api_totp_status)
        app.router.add_post("/api/auth/totp/enroll", api_totp_enroll)
        app.router.add_post("/api/auth/totp/activate", api_totp_activate)
        app.router.add_delete("/api/auth/totp", api_totp_disable)

        # Static files — everything else (SPA)
        app.router.add_route("*", "/{path_info:.*}", spa_handler)

        # WEB_HOST: interface to bind on.
        # Default 127.0.0.1 is safe (local only).  Set to 0.0.0.0 to expose
        # to LAN / a reverse proxy.  Never expose to the internet without
        # an HTTPS reverse proxy and WEB_COOKIE_SECURE=true.
        web_host = os.environ.get("WEB_HOST", "127.0.0.1")

        global _runner
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, web_host, port)
        await site.start()
        _runner = runner  # stored for stop() to call runner.cleanup()
        print(f"[webapp] listening on {web_host}:{port}")

        # Background incident scanner: log_cmd → cards in Failed
        _STARTUP_BG_TASKS.append(_spawn_bg(_error_scanner_loop(ctx)))
        print(f"[webapp] incident scanner started (interval {_SCAN_INTERVAL_SEC}s)")
        # Card Queue: backstop drain loop (restart-resume + TG-interleave)
        _STARTUP_BG_TASKS.append(_spawn_bg(_queue_drain_loop(ctx)))
        # Spec-041 A3: Chat Queue backstop drain loop
        _STARTUP_BG_TASKS.append(_spawn_bg(_chat_queue_drain_loop(ctx)))
        print(f"[webapp] chat queue drain loop started (interval {_QUEUE_DRAIN_INTERVAL_SEC}s)")
        # Spec-019: Schedules registry — background scan loop
        _STARTUP_BG_TASKS.append(_spawn_bg(_schedules._schedules_scan_loop(ctx)))
        print(f"[webapp] schedules scanner started (interval {_schedules._SCAN_INTERVAL_SEC}s)")
        print(f"[webapp] queue drain loop started (interval {_QUEUE_DRAIN_INTERVAL_SEC}s)")
        # Spec-020: Deferred Runs — polling loop
        _STARTUP_BG_TASKS.append(_spawn_bg(_deferred_loop(ctx)))
        print(f"[webapp] deferred runs loop started (interval {_DEFERRED_POLL_SEC}s)")
        # Spec-025: Trash purge janitor
        _STARTUP_BG_TASKS.append(_spawn_bg(_janitor_trash_purge_loop(ctx)))
        print(f"[webapp] janitor trash purge loop started (interval 3600s, retention {TRASH_RETENTION_DAYS}d)")
    except Exception as e:
        print(f"[webapp] ERROR during startup: {e}")


async def stop() -> None:
    """Tear down the webapp: cancel startup background loops and clean up the aiohttp runner.

    spec-039 shutdown: called from _amain()'s finally block (both TG and web-only branches)
    after _graceful_shutdown() has already flushed state.  Safe to call even if start()
    was never reached (tasks list empty, runner None).

    Does NOT call systemctl / kill / os._exit — cgroup gotcha (GOTCHAS.md).
    """
    global _runner

    # 1. Cancel all 5 always-on startup background loops and wait for them to exit.
    if _STARTUP_BG_TASKS:
        tasks_snapshot = list(_STARTUP_BG_TASKS)
        _STARTUP_BG_TASKS.clear()
        for t in tasks_snapshot:
            if not t.done():
                t.cancel()
        # Gather with return_exceptions so one stubborn task doesn't prevent the others
        # from being awaited.  Suppressed CancelledError is expected here.
        results = await asyncio.gather(*tasks_snapshot, return_exceptions=True)
        cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
        print(f"[webapp] stopped {len(tasks_snapshot)} background loop(s) ({cancelled} cancelled cleanly)")

    # 2. Clean up the aiohttp AppRunner (closes the TCP site and frees the socket).
    if _runner is not None:
        try:
            await _runner.cleanup()
            print("[webapp] aiohttp runner cleaned up")
        except Exception as exc:
            print(f"[webapp] WARNING: runner.cleanup() raised: {exc!r}")
        finally:
            _runner = None
