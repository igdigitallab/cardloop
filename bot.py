#!/usr/bin/env python3
"""
Claude-Ops-Bot — Claude Code over Telegram.
Forum group: each topic is bound to a project (thread_id -> cwd).
Full permissions (bypassPermissions), subscription auth (no ANTHROPIC_API_KEY by default),
global + project CLAUDE.md loaded via setting_sources. Spec: ~/vault/01-Projects/Claude-Ops-Bot/.

spec-040 Phase B: this file is a thin PTB shim.  The transport-neutral engine block
(run_engine, state dicts, audit, reconcile_board, etc.) now lives in engine.py.
"""
import asyncio
import html
import json
import os
import re
import subprocess
import time
import traceback
from pathlib import Path

import webapp          # web cockpit (webapp.py) — started alongside the bot, state shared via ctx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # optional — web-only mode if empty
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}

WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))           # web cockpit port
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")            # passphrase for cockpit login

# ── spec-040 Phase B: engine imported AFTER _load_env() + auth ────────────────
# engine.py reads env vars at module level; importing it before _load_env() would
# cause env-dependent constants (DEFAULT_CWD, OPERATOR_NAME, …) to use defaults.
import engine
from engine import (
    HERE, DATA,
    DEFAULT_CWD, DEFAULT_MODEL, MODELS,
    DEFAULT_AGENTS, _build_agents_kwargs,
    CONDUCTOR_PROMPT, DEFAULT_NUDGE, DISALLOWED_TOOLS,
    BOARD_PROTOCOL, TOPICS_F, SESSIONS_F,
    OPERATOR_NAME, RESPONSE_LANGUAGE, _lang_directive,
    AUDIT_DIR, STALL_SECONDS, MAX_SECONDS,
    PERSISTENT_CLIENT, LIVE_CLIENT_TTL_SEC, LIVE_CLIENT_MAX,
    topics, sessions, costs, running, rate_limits, pending_handoff, context_warned,
    save_topics, save_sessions,
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

# ─────────────────────────── TG-specific constants ───────────────────────────
TG_CHUNK = 4000          # max size of a single TG message (characters)
TG_QUEUE_F = DATA / "tg_queue.json"  # LAYER 3: per-topic message queue (persists across restarts)
TG_QUEUE_MAX = int(os.environ.get("TG_QUEUE_MAX", "5"))  # max messages queued per topic

# Operating-brief injected into TG sessions (channel-specific nudge).
# DEFAULT_NUDGE (transport-neutral) lives in engine.py; TELEGRAM_NUDGE adds TG-specific rules.
# ⚠️ nudge — ONLY what genuinely differs from a terminal. Everything about "how to work"
# (scan, surgical edits, permissions, destructive ops) lives in CLAUDE.md (project + ~/CLAUDE.md) —
# the agent loads them via setting_sources and reads the same files as the terminal. Do not duplicate
# here: extra context per turn = dumber agent. Keep short.
TELEGRAM_NUDGE = (
    "Channel is a Telegram bot, not an interactive terminal. Otherwise you are regular Claude Code: "
    "follow the project CLAUDE.md and ~/CLAUDE.md (already loaded) — all working rules are there.\n"
    f"- No interactive dialogs/buttons: if you need clarification or a choice — ask as plain TEXT at "
    f"the end of your reply and finish the turn; {OPERATOR_NAME} will reply in the next message and the session continues.\n"
    f"- Reply concisely{_lang_directive}, in natural prose: what you did → what's next. Do not echo the tool log "
    f"(it's visible in the status) and avoid long code listings — {OPERATOR_NAME} sees edits in files.\n"
    f"- To send a file/screenshot to {OPERATOR_NAME} in this topic: `tg-reply <path> [caption]`.\n"
    "- Key decisions / pitfalls / rejected approaches → write to `.claude-ops/memory/` (see project CLAUDE.md).\n"
    # Option picker: the cockpit chat UI renders a CLI-style interactive picker when the
    # agent ends a message with a ```options fenced block (2–6 mutually-exclusive choices).
    # Use it sparingly — only when the user genuinely must pick one of a small closed set.
    "- When presenting the operator a small set of mutually-exclusive choices (2–6 options), "
    "you MAY end your message with a ```options fenced block (one choice per line) to render "
    "a clickable picker in the chat UI; otherwise reply normally."
)

# ─────────────────────────── TG message queue ───────────────────────────
# Per-topic FIFO queue of pending user messages received while a run is in progress.
# Survives restarts via TG_QUEUE_F (data/tg_queue.json — gitignored inside data/).
# In-memory canonical dict: {session_key: [{"prompt": str, "msg_id": int}, ...]}
# All mutations are synchronous (no await between read and write) — race-safe.
_TG_QUEUE: "dict[str, list[dict]]" = engine._read(TG_QUEUE_F, {})


def _tg_queue_flush() -> None:
    """Atomically flushes _TG_QUEUE to disk. Swallows all exceptions."""
    try:
        tmp = TG_QUEUE_F.with_suffix(".tmp")
        tmp.write_text(json.dumps(_TG_QUEUE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(TG_QUEUE_F)
    except Exception:
        pass


def _tg_queue_enqueue(session_key: str, prompt: str, msg_id: int) -> "int | None":
    """Appends a message to the queue for session_key.

    Returns 1-indexed queue position if added, or None if the queue is full (TG_QUEUE_MAX).
    Mutation is synchronous → flush immediately.
    """
    lst = _TG_QUEUE.setdefault(session_key, [])
    if len(lst) >= TG_QUEUE_MAX:
        return None
    lst.append({"prompt": prompt, "msg_id": msg_id})
    _tg_queue_flush()
    return len(lst)  # 1-indexed position after append


def _tg_queue_pop(session_key: str) -> "dict | None":
    """Pops and returns the first (oldest) message from the queue, or None if empty.

    Mutation is synchronous → flush immediately.
    """
    lst = _TG_QUEUE.get(session_key)
    if not lst:
        return None
    item = lst.pop(0)
    if not lst:
        _TG_QUEUE.pop(session_key, None)
    _tg_queue_flush()
    return item


def _tg_queue_clear(session_key: str) -> int:
    """Removes all queued messages for session_key. Returns the count removed."""
    lst = _TG_QUEUE.pop(session_key, [])
    if lst:
        _tg_queue_flush()
    return len(lst)


def _tg_queue_len(session_key: str) -> int:
    """Returns number of messages currently queued for session_key."""
    return len(_TG_QUEUE.get(session_key, []))


def _tg_key_of(update: Update) -> str:
    """TG-specific key constructor: ``{chat_id}:{thread_id}``.

    Used ONLY by the Telegram adapter (bot.py). Will be deleted in Phase D.
    """
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    return f"{chat}:{thread}"


def binding_for(update: Update) -> dict:
    """Topic binding. General / no topic -> default. Name-based auto-binding is NOT here
    (that's in on_topic_created). Here we only read + return default for General.

    Phase 0 compat: after key migration topics.json uses slug keys, but each migrated
    entry carries a ``tg_key`` field with the original ``chat:thread`` string.  We do
    a two-pass lookup: direct key hit first (pre-migration entries), then reverse scan
    on ``tg_key`` (post-migration entries).  The scan is O(n) but n ≤ ~50 in practice.
    Removed in Phase D.
    """
    k = _tg_key_of(update)
    if k in topics:
        return topics[k]
    # Phase 0 reverse-lookup: find the entry whose tg_key matches.
    for entry in topics.values():
        if entry.get("tg_key") == k:
            return entry
    # topic without a binding -> fall back to DEFAULT_CWD, mark project as unbound
    thread = update.effective_message.message_thread_id
    if not thread:
        return {"project": "General", "cwd": DEFAULT_CWD, "model": DEFAULT_MODEL}
    return None  # unknown topic -> ask user to run /project


def _tg_key_in_topics(tg_key: str) -> bool:
    """Phase 0 compat: check whether a TG chat:thread key is bound to any project.

    After migration topics uses slug keys, so direct ``tg_key in topics`` fails.
    We also scan entries for the ``tg_key`` field added by migration.
    Removed in Phase D.
    """
    if tg_key in topics:
        return True
    return any(v.get("tg_key") == tg_key for v in topics.values())


# ─────────────────────────── auth ───────────────────────────
def authorized(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)


# ─────────────────────────── helpers ───────────────────────────
async def _tg_call(factory, tries=6):
    """Single TG API call that survives TRANSIENT failures — the main cause of "lost replies"
    on long tasks: RetryAfter (flood-control), NetworkError/Bad Gateway, TimedOut.
    factory — zero-argument callable returning a FRESH coroutine (needed for retries).
    BadRequest is NOT caught here — it is a logic error (broken HTML) handled by the caller."""
    delay = 1.0
    for attempt in range(tries):
        try:
            return await factory()
        except RetryAfter as e:
            await asyncio.sleep(getattr(e, "retry_after", delay) + 0.5)
        except (NetworkError, TimedOut):
            if attempt == tries - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def send(context, chat, thread, text, **kw):
    # Chunk by lines (not blindly by bytes) to avoid splitting HTML tags/entities at chunk boundaries.
    for chunk in _smart_chunks(text, TG_CHUNK):
        try:
            await _tg_call(lambda c=chunk: context.bot.send_message(
                chat, c, message_thread_id=thread or None, **kw))
        except BadRequest:
            # HTML/MD parser choked — send as plain text so the message gets through.
            # IMPORTANT: chunk arrives already html-escaped (<b>... &lt;... &amp;...).
            # Without unescape the operator would see raw &lt;b&gt; — unreadable.
            kw2 = {k: v for k, v in kw.items() if k != "parse_mode"}
            plain = html.unescape(re.sub(r"</?(b|i|code|pre|a)[^>]*>", "", chunk))
            await _tg_call(lambda p=plain: context.bot.send_message(
                chat, p, message_thread_id=thread or None, **kw2))


CODE_MAX_LINES = 20      # longer blocks are collapsed (TG reply should convey intent, not walls of code)
CODE_PREVIEW_LINES = 10  # how many lines to show before collapsing


def _render_code_block(body: str, lang: str = "") -> str:
    """``` block ``` -> monospace <pre>. Long blocks (> CODE_MAX_LINES) are collapsed to a preview +
    marker: the operator can see edits in files/diff anyway; code walls in TG are noise."""
    lines = body.split("\n")
    while lines and not lines[-1].strip():   # strip trailing blank lines
        lines.pop()
    n = len(lines)
    if n > CODE_MAX_LINES:
        head = "\n".join(lines[:CODE_PREVIEW_LINES])
        tag = f"{lang} · " if lang else ""
        return f"<pre>{html.escape(head)}\n…</pre><i>‹{tag}{n} lines of code collapsed›</i>"
    return f"<pre>{html.escape(chr(10).join(lines))}</pre>"


def md_to_html(text: str) -> str:
    """Model's Markdown response -> safe Telegram HTML (supports <b><i><code><pre><a>).
    Strategy: extract code/links into placeholders BEFORE escaping (to avoid breaking them and
    to count lines), escape the rest, apply light markdown, restore placeholders.
    All done bot-side — the agent doesn't think about formatting and stays as smart as in a terminal."""
    stash = []

    def _stash(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00P{len(stash) - 1}\x00"   # \x00 is not touched by html.escape or markdown regexes

    # 1) code blocks ```lang\n...``` (before escaping — need to count lines)
    text = re.sub(r"```([^\n]*)\n?(.*?)```",
                  lambda m: _stash(_render_code_block(m.group(2), (m.group(1) or "").strip())),
                  text, flags=re.DOTALL)
    # 2) inline `code`
    text = re.sub(r"`([^`\n]+?)`",
                  lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    # 3) links [text](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  lambda m: _stash(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'),
                  text)
    # 4) escape the rest (raw <title>/<div> from model output become safe)
    text = html.escape(text)
    # 5) line by line: headings #..###### -> bold; list markers -*+ -> •
    out = []
    for line in text.split("\n"):
        h = re.match(r"\s*#{1,6}\s+(.*)", line)
        if h:
            out.append(f"<b>{h.group(1).rstrip()}</b>")
        else:
            out.append(re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line))
    text = "\n".join(out)
    # 6) **bold** and *italic* (italic conservative — avoid catching "2 * 3" and list remnants)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])", r"<i>\1</i>", text)
    # 7) restore code/links
    return re.sub(r"\x00P(\d+)\x00", lambda m: stash[int(m.group(1))], text)


async def report_error(context, chat, thread, where: str, exc: BaseException):
    """Sends a crash report to the operator: location + type + traceback in a copyable <pre> block.
    All send errors are suppressed — if the PTB httpx client is already closed (service shutdown)
    there is nowhere to send anyway, and a second unhandled exception would only clutter logs."""
    chat = chat or GROUP_CHAT_ID or (next(iter(ALLOWED_USERS), None))
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    head = f"💥 <b>Crash</b>\nWhere: {html.escape(where)}\nWhat: <b>{type(exc).__name__}</b>: {html.escape(str(exc))}"
    block = tb[-3500:]  # tail of traceback — most relevant part
    text = f"{head}\n<pre>{html.escape(block)}</pre>"
    # Log to stdout → journalctl → error-scanner picks up Traceback → incident in Failed.
    print(f"[crash] {where}: {type(exc).__name__}: {exc}\n{tb}", flush=True)
    try:
        await _tg_call(lambda: context.bot.send_message(
            chat, text, message_thread_id=thread or None, parse_mode=ParseMode.HTML))
        return
    except Exception:
        pass
    try:
        plain = f"💥 Crash\nWhere: {where}\nWhat: {type(exc).__name__}: {exc}\n\n{block}"
        await _tg_call(lambda: context.bot.send_message(
            chat, plain[:TG_CHUNK], message_thread_id=thread or None))
    except Exception as send_exc:
        print(f"[report_error] failed to send crash report to TG ({type(send_exc).__name__}): {exc!r}")


def _chunks(s, n):
    if not s:
        return [""]
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


def _smart_chunks(s: str, n: int):
    """Split by lines (then by spaces) to avoid tearing HTML tags/entities at chunk boundaries.
    If a single line is longer than n — fall back to hard splitting."""
    if not s:
        return [""]
    out, buf = [], ""
    for line in s.splitlines(keepends=True):
        if len(line) > n:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_chunks(line, n))
            continue
        if len(buf) + len(line) > n:
            out.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out or [""]


# ─────────────────────────── TG adapter ───────────────────────────
#
# run_agent — run_engine consumer for the Telegram channel.
# Renders the status message (edit), watchdog, heartbeat, audit log, and final reply.
# Behaviour is 1-to-1 with the original — only the event source is replaced by the generator.

# spec-039: _maybe_rotate_tg and _maybe_warn_tg removed — TG auto-rotation and
# TG context-warn ping deleted. No code path may auto-reset a session by token count.


async def run_agent(context, update, prompt: str):
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    cwd, model = b["cwd"], b.get("model", DEFAULT_MODEL)
    # spec-040 Phase 0: session_key is the slug (transport-neutral); k is TG-only.
    # on_message reserves running[session_key] (not running[k]).
    session_key = key_of(cwd)

    # Spec-028 Phase 1: build a ctx dict so run_engine uses the shared running dict and,
    # when PERSISTENT_CLIENT=1, the live-client registry.
    _engine_ctx = {
        "running": running,
        "live_clients": _live_clients,
    }

    status = await context.bot.send_message(
        chat, f"⚙️ <b>{b['project']}</b> · {model}\n<i>thinking…</i>",
        message_thread_id=thread or None, parse_mode=ParseMode.HTML,
    )
    log_lines, answer, n_edits = [], [], 0
    last_edit = 0.0
    t_start = time.time()
    last_event = [t_start]        # updated on every SDK event (for watchdog)
    stalled = {"reason": None}

    def _elapsed():
        s = int(time.time() - t_start)
        return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    async def push_status(force=False):
        nonlocal last_edit
        now = asyncio.get_event_loop().time()
        if not force and now - last_edit < 2.0:
            return
        last_edit = now
        tail = "\n".join(log_lines[-8:]) or "thinking…"
        # timer in the header always changes → no "message is not modified" error and shows liveness
        body = f"⚙️ <b>{b['project']}</b> · {model} · ⏱ {_elapsed()}\n{html.escape(tail)}"
        try:
            await context.bot.edit_message_text(body, chat, status.message_id, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def heartbeat():
        """Ticks the status every ~12 s even without new tool calls — signals "alive, not hung"."""
        try:
            while True:
                await asyncio.sleep(12)
                await push_status(force=True)
        except asyncio.CancelledError:
            pass

    async def watchdog():
        """Absolute turn ceiling: interrupts only when MAX_SECONDS is exceeded.

        spec-039: stall interrupt removed — a quiet or long-running turn must not be
        interrupted. Only the absolute ceiling (watchdog_max_sec, default 7200 s) fires.
        The interrupt ends the current turn only; the session and live client survive.
        """
        max_s = MAX_SECONDS
        try:
            import webapp as _wa
            max_s = int(_wa._get_global_setting("watchdog_max_sec", MAX_SECONDS) or MAX_SECONDS)
        except Exception:
            pass
        try:
            while True:
                await asyncio.sleep(20)
                if time.time() - t_start > max_s:
                    stalled["reason"] = f"exceeded limit of {max_s // 60} min"
                cl = running.get(k)
                if stalled["reason"] and hasattr(cl, "interrupt"):
                    try:
                        await cl.interrupt()
                        print(f"[watchdog] interrupted task {k}: {stalled['reason']}")
                        await send(context, chat, thread,
                                   md_to_html(f"⏱ Watchdog triggered: {stalled['reason']} — task interrupted."),
                                   parse_mode=ParseMode.HTML)
                    finally:
                        return
        except asyncio.CancelledError:
            pass

    hb = asyncio.create_task(heartbeat())
    wd = asyncio.create_task(watchdog())
    engine_exc = None
    subagent_progress_counts: dict = {}   # task_id -> count of progress events seen
    _tg_last_result_event: dict | None = None  # Phase D: track for auto-resume
    # spec-040 Phase 0: publish to cockpit bus using slug session_key (not TG chat:thread).
    # Cockpit subscribes by session_key = project["tg_thread"] which is now the slug.
    webapp._bus_publish(session_key, {"kind": "run_start", "source": "tg", "prompt": prompt, "run_id": None})
    try:
        # Project secrets (Spec 007) augment env; TG_CHAT_ID/TG_THREAD_ID take priority.
        # secret: references are resolved against the built-in store; TG vars are merged after so they always win.
        project_secrets = await webapp._resolve_secret_refs(webapp._secrets_read(cwd))
        agent_env = {**project_secrets, "TG_CHAT_ID": str(chat), "TG_THREAD_ID": str(thread or 0)}
        agents_config = b.get("agents_config") or {}
        agent_kwargs = _build_agents_kwargs(agents_config)
        # Spec-021 Phase 4: inject handoff summary into the first turn of a fresh session.
        # Only fires when there is no existing session (post-rotation) and a pending handoff exists.
        resume_sid = sessions.get(session_key)
        effective_prompt = prompt
        try:
            if resume_sid is None and session_key in pending_handoff:
                summary = pending_handoff.pop(session_key)
                effective_prompt = (
                    "<prior-session-summary>\n"
                    "The previous session was rotated to stay lean. Summary of where we left off below.\n"
                    "Continue this work if the new message relates to it; ignore this block if starting "
                    "something unrelated.\n\n"
                    f"{summary}\n"
                    "</prior-session-summary>\n\n"
                    f"{prompt}"
                )
                print(f"[rotation] injected handoff into first post-rotation turn for {session_key}")
        except Exception as _inj_exc:
            print(f"[rotation] handoff injection failed (continuing without it): {_inj_exc}")
            effective_prompt = prompt
        async for event in run_engine(
            project_name=b["project"],
            cwd=cwd,
            prompt=effective_prompt,
            session_key=session_key,
            model=model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": TELEGRAM_NUDGE,  # TG adapter keeps TG-specific nudge
                "exclude_dynamic_sections": True,
            },
            env=agent_env,
            resume_session_id=resume_sid,
            **agent_kwargs,
            ctx=_engine_ctx,
            ephemeral=False,
        ):
            last_event[0] = time.time()   # any SDK event = "alive" for watchdog
            etype = event["type"]

            if etype == "text":
                answer.append(event["text"])
                log_lines.append("💬 " + short(event["text"].replace("\n", " "), 70))
                webapp._bus_publish(session_key, {"kind": "text", "text": event["text"], "run_id": None})

            elif etype == "tool":
                name = event["name"]
                inp = event["input"]
                if name == "Bash":
                    cmd = inp.get("command", "")
                    log_lines.append(f"$ {short(cmd, 70)}")
                    audit(b["project"], "BASH⚠️" if _is_destructive(cmd) else "BASH", cmd)
                elif name in ("Edit", "Write", "NotebookEdit"):
                    n_edits += 1
                    fp = str(inp.get("file_path", ""))
                    log_lines.append(f"✏️ {name}: {short(fp, 60)}")
                    audit(b["project"], name.upper(), fp)
                else:
                    log_lines.append(f"🔧 {name}")
                webapp._bus_publish(session_key, {
                    "kind": "tool", "run_id": None,
                    "tool": webapp._format_tool(name, inp if isinstance(inp, dict) else {}),
                })
                await push_status()

            elif etype == "result":
                _tg_last_result_event = event  # Phase D: capture for auto-resume
                if event.get("session_id"):
                    sessions[session_key] = event["session_id"]
                    save_sessions()
                if event.get("cost_usd") is not None:
                    costs[session_key] = event["cost_usd"]

            elif etype == "rate_limit":
                rl_type = event.get("rate_limit_type")
                if rl_type:
                    rate_limits[rl_type] = {
                        "status": event.get("status"),
                        "resets_at": event.get("resets_at"),
                        "utilization": event.get("utilization"),
                        "ts": time.time(),
                    }

            elif etype == "subagent":
                subtype = event.get("subtype")
                task_id = event.get("task_id", "")
                description = event.get("description", "")
                if subtype == "started":
                    log_lines.append(f"⚙ sub-agent started: {short(description, 60)}")
                    await push_status()
                elif subtype == "progress":
                    # Rate-limit: forward to SSE but skip excessive TG status updates.
                    count = subagent_progress_counts.get(task_id, 0) + 1
                    subagent_progress_counts[task_id] = count
                    if count <= MAX_SUBAGENT_PROGRESS:
                        tool = event.get("last_tool_name") or ""
                        detail = f" [{tool}]" if tool else ""
                        log_lines.append(f"  ↳ {short(description, 50)}{detail}")
                        await push_status()
                elif subtype == "notification":
                    status = event.get("status") or ""
                    summary = event.get("summary") or ""
                    icon = "✓" if status == "completed" else "✗"
                    line = f"{icon} sub-agent {status}: {short(summary or description, 80)}"
                    log_lines.append(line)
                    answer.append(f"\n_{line}_")   # append terminal result to final reply
                    await push_status()
                webapp._bus_publish(session_key, {"kind": "subagent", "run_id": None, **event})

            elif etype == "text_delta":
                pass  # TG adapter: ignore streaming deltas — final reply built from {type:"text"} blocks

            elif etype == "error":
                engine_exc = event["exc"]

    except Exception as exc:
        engine_exc = exc
    finally:
        hb.cancel()
        wd.cancel()
        webapp._bus_publish(session_key, {
            "kind": "run_end",
            "outcome": "ok" if engine_exc is None else "fail",
            "run_id": None,
        })
        # running.pop is cleared in safe_run.finally (authoritative location)

    # If the engine failed — delete the status message and re-raise (safe_run/report_error handles it)
    if engine_exc is not None:
        try:
            await context.bot.delete_message(chat, status.message_id)
        except Exception:
            pass
        raise engine_exc

    # Final: FIRST send the reply, THEN delete the status message.
    # Order is critical: if sending the reply fails even after retries — the last progress
    # remains on screen (not blank). Deleting status before sending was the cause of
    # "both the progress log and the reply disappeared" on long tasks.
    footer = []
    if n_edits:
        footer.append(f"✏️ files edited: {n_edits}")
    if stalled["reason"]:
        footer.append(f"⚠️ auto-interrupted by watchdog: {stalled['reason']}")
    ans = md_to_html("\n".join(answer).strip() or "(agent finished with no text reply)")
    if footer:
        ans += "\n\n— — —\n" + "\n".join(footer)
    await send(context, chat, thread, ans, parse_mode=ParseMode.HTML)
    try:
        await context.bot.delete_message(chat, status.message_id)
    except Exception:
        pass
    audit(b["project"], "DONE", f"edits={n_edits}" + (f" STALLED:{stalled['reason']}" if stalled["reason"] else ""))

    # spec-039: auto-resume and TG auto-rotation/warn calls removed.
    # _maybe_auto_resume default is now OFF; _maybe_rotate_tg / _maybe_warn_tg deleted.

    # spec-034 L2: board reconciler — schedule as background task (never blocks the reply).
    agent_reply = "\n".join(answer).strip()
    asyncio.create_task(
        reconcile_board(cwd=cwd, name=b["project"], user_msg=prompt, agent_summary=agent_reply)
    )


# ─────────────────────────── handlers ───────────────────────────
async def fetch_files(context, msg) -> list:
    """Downloads attachments (document/photo) to data/inbox/ and returns absolute paths.
    Telegram getFile limit is 20 MB. The agent then reads them by path via Read."""
    inbox = DATA / "inbox"
    inbox.mkdir(exist_ok=True)
    paths = []
    if msg.document:
        d = msg.document
        f = await context.bot.get_file(d.file_id)
        name = (d.file_name or f"doc_{msg.message_id}").replace("/", "_")
        dest = inbox / f"{msg.message_id}_{name}"
        await f.download_to_drive(str(dest))
        paths.append(str(dest))
    if msg.photo:
        f = await context.bot.get_file(msg.photo[-1].file_id)
        dest = inbox / f"{msg.message_id}.jpg"
        await f.download_to_drive(str(dest))
        paths.append(str(dest))
    return paths


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    has_file = bool(msg.document or msg.photo)
    if not text and not has_file:
        return
    k = _tg_key_of(update)
    if not _tg_key_in_topics(k) and msg.message_thread_id:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   "🔌 Topic is not bound to a project. Bind it: /project <name>")
        return
    # spec-040 Phase 0: derive slug session_key for engine state (running/sessions/costs).
    # k (chat:thread) is still used for TG queue operations; session_key (slug) for everything else.
    _b = binding_for(update)
    session_key = key_of(_b["cwd"]) if (_b and _b.get("cwd")) else k
    # race-condition guard: reserve slot SYNCHRONOUSLY before the first await
    if session_key in running:
        # Engine is busy — enqueue the message instead of rejecting it.
        # Build the full prompt first (attachments are not downloaded here — queue plain text only).
        base_text = (msg.text or msg.caption or "").strip()
        if base_text or not (msg.document or msg.photo):
            q_prompt = base_text or "(no text)"
            if msg.forward_origin:
                q_prompt = ("[This is a forwarded message / alert from one of my services. "
                            "Diagnose the cause and fix it.]\n\n" + q_prompt)
            pos = _tg_queue_enqueue(k, q_prompt, msg.message_id)
            if pos is None:
                await send(context, update.effective_chat.id, msg.message_thread_id,
                           f"⚠️ Queue is full ({TG_QUEUE_MAX} messages). Use /stop or wait.")
            else:
                await send(context, update.effective_chat.id, msg.message_thread_id,
                           f"⏳ Queued #{pos} — will run after the current turn finishes.")
        else:
            # File-only message while busy: skip silently with notice
            await send(context, update.effective_chat.id, msg.message_thread_id,
                       "⚠️ File attachments cannot be queued while a run is in progress. "
                       "Please resend after the current turn finishes.")
        return
    running[session_key] = True  # placeholder; run_engine will replace with the real client
    cid, tid = update.effective_chat.id, msg.message_thread_id
    try:
        # attachments -> download, pass paths to agent
        files = []
        if has_file:
            try:
                files = await fetch_files(context, msg)
            except Exception as e:
                await send(context, cid, tid, f"⚠️ Failed to download attachment ({e}). Possibly >20 MB.")
        base = text or "Look at the attached file and do whatever is needed with it."
        if msg.forward_origin:
            prompt = ("[This is a forwarded message / alert from one of my services. "
                      "Diagnose the cause and fix it.]\n\n" + base)
        else:
            prompt = base
        if files:
            prompt += ("\n\n[Attached files — absolute paths on the server, read them via Read:\n"
                       + "\n".join(files) + "]")
        await context.bot.send_chat_action(cid, ChatAction.TYPING, message_thread_id=tid or None)
        asyncio.create_task(safe_run(context, update, prompt))
    except Exception as e:
        running.pop(session_key, None)
        await send(context, cid, tid, f"⚠️ Task launch error: {e}")


async def _drain_tg_queue(context, update) -> None:
    """After a turn finishes, pop and run the next queued message for this topic (if any).

    Called from safe_run.finally — AFTER running.pop(k) so the slot is free.
    Sends a status notice before starting the queued run so the operator sees it was dequeued.
    If the queue is empty, returns immediately (no-op).
    """
    k = _tg_key_of(update)
    item = _tg_queue_pop(k)
    if item is None:
        return
    remaining = _tg_queue_len(k)
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    try:
        notice = (f"▶️ Running queued message"
                  + (f" ({remaining} more in queue)" if remaining else "")
                  + ".")
        await send(context, chat, thread, notice)
    except Exception:
        pass
    # Reserve the slot synchronously before the first await (same race guard as on_message).
    # spec-040 Phase 0: use slug session_key, not TG key, for running dict.
    _b_drain = binding_for(update)
    _sk_drain = key_of(_b_drain["cwd"]) if (_b_drain and _b_drain.get("cwd")) else k
    if _sk_drain in running:
        # Another message snuck in between pop and now — put the item back at the front.
        _TG_QUEUE.setdefault(k, []).insert(0, item)
        _tg_queue_flush()
        return
    running[_sk_drain] = True
    try:
        await context.bot.send_chat_action(chat, ChatAction.TYPING, message_thread_id=thread or None)
    except Exception:
        pass
    asyncio.create_task(_safe_run_queued(context, update, item["prompt"]))


async def _safe_run_queued(context, update, prompt: str) -> None:
    """Runs a dequeued prompt through run_agent, then drains again (chain drain)."""
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    k = _tg_key_of(update)
    # spec-040 Phase 0: resolve slug session_key for running dict (matches on_message reservation).
    _b2 = binding_for(update)
    _sk2 = key_of(_b2["cwd"]) if (_b2 and _b2.get("cwd")) else k
    try:
        await run_agent(context, update, prompt)
    except Exception as e:
        if "exit code 143" in str(e) or "exit code 137" in str(e):
            print(f"[safe_run_queued] CLI killed during shutdown, prompt={short(prompt, 60)}")
        else:
            await report_error(context, chat, thread, f"run_agent(queued) · {short(prompt, 60)}", e)
    finally:
        running.pop(_sk2, None)
        await _drain_tg_queue(context, update)


async def safe_run(context, update, prompt):
    """Background task wrapper: PTB does not catch exceptions from asyncio.create_task itself."""
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    k = _tg_key_of(update)
    # spec-040 Phase 0: resolve slug session_key for running dict (matches on_message reservation).
    _b3 = binding_for(update)
    _sk3 = key_of(_b3["cwd"]) if (_b3 and _b3.get("cwd")) else k
    try:
        await run_agent(context, update, prompt)
    except Exception as e:
        # SIGTERM/SIGKILL to CLI on systemctl restart/stop -> SDK returns exit 143/137.
        # This is a normal shutdown, not a bot bug — don't report it as a "crash" in TG.
        if "exit code 143" in str(e) or "exit code 137" in str(e):
            print(f"[safe_run] CLI killed during shutdown (exit 143/137), prompt={short(prompt, 60)}")
        else:
            await report_error(context, chat, thread, f"run_agent · {short(prompt, 60)}", e)
    finally:
        running.pop(_sk3, None)  # always clear the reservation, even if run_agent crashed before its try
        # Drain the queue: if messages were enqueued while this run was active, start the next one.
        await _drain_tg_queue(context, update)


async def on_error(update, context):
    """Global PTB error handler (command handlers etc.)."""
    chat = thread = None
    where = "handler"
    if isinstance(update, Update):
        if update.effective_chat:
            chat = update.effective_chat.id
        if update.effective_message:
            thread = update.effective_message.message_thread_id
            txt = update.effective_message.text or update.effective_message.caption
            if txt:
                where = f"update · {short(txt, 60)}"
    await report_error(context, chat, thread, where, context.error)


async def on_topic_created(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """New topic -> auto-bind by name via registry."""
    if not authorized(update):
        return
    msg = update.effective_message
    name = msg.forum_topic_created.name
    k = _tg_key_of(update)
    r = resolve_project(name)
    if r:
        # spec-040 Phase 0: register under slug key; store tg_key for TG reverse lookup.
        slug = key_of(r[1])
        topics[slug] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL, "tg_key": k}
        topics.pop(k, None)  # remove stale TG-key entry if any
        save_topics()
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   f"✅ Bound topic to <b>{r[0]}</b>\n<code>{r[1]}</code>", parse_mode=ParseMode.HTML)
    else:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   f"🔌 Topic «{html.escape(name)}» did not match any project. Bind manually: /project &lt;name|path&gt;",
                   parse_mode=ParseMode.HTML)


# ── commands ──
async def cmd_start(update, context):
    if not authorized(update):
        return
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               "👋 Claude-Ops. Send a task or forward an alert to a project topic.\n"
               "Commands: /whoami /reset /resume /model /project /newtopic /diff /cost /stop")


async def cmd_whoami(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "🔌 Topic not bound. /project <name>")
        return
    session_key = key_of(b["cwd"])
    sid = sessions.get(session_key, "—")
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"📍 <b>{b['project']}</b>\ncwd: <code>{b['cwd']}</code>\nmodel: {b.get('model', DEFAULT_MODEL)}\n"
               f"session: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_reset(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    session_key = key_of(b["cwd"]) if (b and b.get("cwd")) else k
    sessions.pop(session_key, None)
    save_sessions()
    cleared = _tg_queue_clear(k)
    # Clear context-warn state so a fresh session can warn again.
    context_warned.discard(session_key)
    # spec-039: evict the live client so PERSISTENT_CLIENT=1 truly starts fresh.
    try:
        await _evict_live_client(session_key, None)
    except Exception as _exc:
        print(f"[cmd_reset] live-client eviction error for {session_key}: {_exc!r}")
    proj = b["project"] if b else "—"
    queue_note = f" Queue cleared ({cleared} message(s))." if cleared else ""
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🔄 Context reset. Project <b>{proj}</b> preserved.{queue_note}", parse_mode=ParseMode.HTML)


async def cmd_resume(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    session_key = key_of(b["cwd"]) if (b and b.get("cwd")) else k
    if context.args:
        sessions[session_key] = context.args[0]
        save_sessions()
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"⏯ Resuming session <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)
    else:
        sid = sessions.get(session_key, "—")
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"Current topic session: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_model(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "🔌 Bind the topic first: /project <name>")
        return
    if not context.args or context.args[0] not in MODELS:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Usage: /model opus|sonnet|haiku")
        return
    b["model"] = context.args[0]
    # After Phase 0 migration, topics key is the slug; fall back to TG key for pre-migration entries.
    _sk_model = key_of(b["cwd"]) if b.get("cwd") else k
    if _sk_model in topics or k in topics:
        save_topics()
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🧠 Topic model: <b>{context.args[0]}</b> (takes effect from the next request)", parse_mode=ParseMode.HTML)


async def cmd_project(update, context):
    if not authorized(update):
        return
    if not context.args:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Usage: /project <name|path>. Known: " + ", ".join(sorted(set(_REG_RAW))))
        return
    r = resolve_project(" ".join(context.args))
    if not r:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "❌ Project/path not found.")
        return
    k = _tg_key_of(update)
    slug = key_of(r[1])
    prev = topics.get(slug) or topics.get(k, {})
    # spec-040 Phase 0: register under slug key; store tg_key for TG reverse lookup.
    topics[slug] = {"project": r[0], "cwd": r[1], "model": prev.get("model", DEFAULT_MODEL),
                    "tg_key": k}
    # Remove any stale TG-key entry for this slot.
    topics.pop(k, None)
    save_topics()
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"📌 Topic bound to <b>{r[0]}</b>\n<code>{r[1]}</code>", parse_mode=ParseMode.HTML)


async def cmd_newtopic(update, context):
    """Bot creates a forum topic and binds it to a project."""
    if not authorized(update):
        return
    if not context.args:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "Usage: /newtopic <project name>")
        return
    name = " ".join(context.args)
    res = await context.bot.create_forum_topic(chat_id=update.effective_chat.id, name=name)
    tid = res.message_thread_id
    tg_key = f"{update.effective_chat.id}:{tid}"
    r = resolve_project(name)
    if r:
        # spec-040 Phase 0: register under slug key; store tg_key for TG reverse lookup.
        slug = key_of(r[1])
        topics[slug] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL, "tg_key": tg_key}
        save_topics()
        note = f" → bound to <code>{r[1]}</code>"
    else:
        note = " (did not match any project — bind with /project inside the topic)"
    await context.bot.send_message(update.effective_chat.id,
                                   f"🆕 Created topic «{html.escape(name)}»{note}",
                                   message_thread_id=tid, parse_mode=ParseMode.HTML)


async def cmd_diff(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        return
    try:
        out = subprocess.run(["git", "-C", b["cwd"], "diff", "--stat"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        out = f"error: {e}"
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"<b>git diff --stat</b> ({b['project']})\n<pre>{html.escape(out or '(empty)')}</pre>",
               parse_mode=ParseMode.HTML)


async def cmd_cost(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    session_key = key_of(b["cwd"]) if (b and b.get("cwd")) else k
    c = costs.get(session_key)
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"💰 Last request: ${c:.4f}" if c is not None else "💰 No data yet")


_RL_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "Weekly limit",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
    "overage": "Overage",
}
_RL_ICON = {"allowed": "🟢", "allowed_warning": "🟡", "rejected": "🔴"}


def _fmt_reset(ts):
    if not ts:
        return "—"
    delta = ts - time.time()
    if delta <= 0:
        return "soon"
    h, m = int(delta // 3600), int((delta % 3600) // 60)
    return f"in {h}h {m}m" if h else f"in {m}m"


def format_usage() -> str:
    if not rate_limits:
        return ("📊 No limit data yet — it will arrive with the first request to the bot "
                "(delivered together with responses).")
    lines = ["📊 <b>Subscription limits</b> (passive, from recent responses):"]
    for t in ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"]:
        d = rate_limits.get(t)
        if not d:
            continue
        icon = _RL_ICON.get(d["status"], "⚪")
        util = d.get("utilization")
        pct = f" · used {util * 100:.0f}% ({100 - util * 100:.0f}% remaining)" if util is not None else ""
        lines.append(f"{icon} <b>{_RL_LABELS.get(t, t)}</b>: resets {_fmt_reset(d['resets_at'])}{pct}")
    lines.append("\n<i>Exact % only arrives when approaching the limit; otherwise just status and reset time.</i>")
    return "\n".join(lines)


async def cmd_usage(update, context):
    if not authorized(update):
        return
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               format_usage(), parse_mode=ParseMode.HTML)


async def cmd_stop(update, context):
    if not authorized(update):
        return
    k = _tg_key_of(update)
    b = topics.get(k) or binding_for(update)
    session_key = key_of(b["cwd"]) if (b and b.get("cwd")) else k
    client = running.get(session_key)
    cid, tid = update.effective_chat.id, update.effective_message.message_thread_id
    if client is None:
        await send(context, cid, tid, "Nothing to interrupt.")
    elif hasattr(client, "interrupt"):
        try:
            await client.interrupt()
        except Exception:
            pass
        await send(context, cid, tid, "🛑 Interrupting…")
    else:
        await send(context, cid, tid, "⏳ Task is still starting up — please wait a moment.")


# ─────────────────────────── /later — deferred runs (Spec 020) ───────────────────────────

def _parse_time_spec(spec: str) -> tuple:
    """Parse time spec: 'reset', 'Nh', 'Nm', 'HH:MM', or ISO-8601 UTC.
    Returns (fire_at: str|None, fire_on_reset: bool)."""
    import re as _re
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    spec = spec.strip()
    if spec.lower() == "reset":
        return (None, True)
    # Nh / Nm
    m = _re.match(r'^(\d+)([hm])$', spec.lower())
    if m:
        n = int(m.group(1))
        delta = n * 3600 if m.group(2) == 'h' else n * 60
        fire_ts = time.time() + delta
        return (webapp._unix_to_iso(fire_ts), False)
    # HH:MM
    m = _re.match(r'^(\d{1,2}):(\d{2})$', spec)
    if m:
        from zoneinfo import ZoneInfo
        tz_name = os.environ.get("OPERATOR_TZ", "America/Los_Angeles")
        local_tz = ZoneInfo(tz_name)
        now_local = _dt.now(local_tz)
        target = now_local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if target <= now_local:
            target += _td(days=1)
        return (webapp._unix_to_iso(target.timestamp()), False)
    # ISO-8601
    try:
        dt = _dt.fromisoformat(spec.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return (webapp._unix_to_iso(dt.timestamp()), False)
    except Exception:
        pass
    raise ValueError(f"Unrecognised time spec: {spec!r}")


async def cmd_later(update, context):
    """Handle /later <time_spec> <prompt> — queue a deferred run."""
    if not authorized(update):
        return
    cid = update.effective_chat.id
    tid = update.effective_message.message_thread_id
    args_text = (update.effective_message.text or "").split(None, 2)
    # args_text[0] = "/later", [1] = time_spec, [2] = prompt
    if len(args_text) < 3:
        await send(context, cid, tid,
                   "Usage: /later <time_spec> <prompt>\n\ntime_spec: reset | 2h | 30m | HH:MM | ISO-8601")
        return
    time_spec = args_text[1]
    prompt_text = args_text[2].strip()
    if not prompt_text:
        await send(context, cid, tid, "Usage: /later <time_spec> <prompt>")
        return
    k = _tg_key_of(update)
    binding = topics.get(k) or binding_for(update)
    if binding is None:
        await send(context, cid, tid,
                   "This topic is not bound to a project. Use /project <name> first.")
        return
    project = binding.get("project", "")
    # spec-040 Phase 0: deferred records use the slug session_key.
    later_session_key = key_of(binding["cwd"]) if binding.get("cwd") else k
    try:
        fire_at, fire_on_reset = _parse_time_spec(time_spec)
    except ValueError as e:
        await send(context, cid, tid, f"Invalid time spec: {e}")
        return
    record = {
        "id": webapp._new_deferred_id(),
        "project": project,
        "session_key": later_session_key,
        "prompt": prompt_text[:4096],
        "fire_at": fire_at,
        "fire_on_reset": fire_on_reset,
        "created": webapp._utcnow_iso(),
        "status": "pending",
        "fired_at": None,
        "error": None,
        "attempts": 0,
    }
    records = webapp._load_deferred()
    records.append(record)
    webapp._save_deferred(records)
    trigger_str = "after rate-limit reset" if fire_on_reset else f"at {fire_at}"
    await send(context, cid, tid,
               f"[QUEUED] Deferred run queued [{project}] {trigger_str}\nPrompt: {prompt_text[:80]}...")


# ─────────────────────────── main ───────────────────────────

def _build_ctx(ptb_app) -> dict:
    """Build the shared context dict passed to webapp.start().

    Delegates to engine._build_ctx() passing the TG-specific values (WEB_PORT,
    WEB_PASSWORD, GROUP_CHAT_ID) that only bot.py knows at this point.
    """
    return _engine_build_ctx(
        ptb_app,
        web_port=WEB_PORT,
        web_password=WEB_PASSWORD,
        group_chat_id=GROUP_CHAT_ID,
    )


async def _amain() -> None:
    """Async entry point.

    Always starts the web cockpit + engine on the current asyncio loop.
    PTB (Telegram polling) starts only when BOT_TOKEN is set.  In web-only
    mode ptb_app=None is passed to webapp so all TG side-effects are skipped.

    Loop ownership: a single asyncio loop drives both aiohttp and PTB.
    PTB is started via the manual lifecycle (initialize/start/start_polling)
    rather than run_polling() so it does NOT take over the loop.
    """
    # spec-039: stop event — SIGTERM/SIGINT handlers set this instead of raising;
    # the main coroutine awaits it, then performs graceful cleanup and returns.
    # Systemd owns process termination — we never call os._exit or kill ourselves
    # (cgroup gotcha: any such call inside the cgroup tears down the daemon mid-flight).
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

    # spec-040 Phase 0: migrate legacy chat:thread session keys to slug format.
    # Runs here (startup, before serving) — NOT at import time — to avoid mutating
    # data/*.json as a side-effect of ``import bot`` in tests.
    _run_startup_migration()

    if BOT_TOKEN:
        # ── Telegram mode ──────────────────────────────────────────────────
        ptb_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
        ptb_app.add_handler(CommandHandler("start", cmd_start))
        ptb_app.add_handler(CommandHandler("help", cmd_start))
        ptb_app.add_handler(CommandHandler("whoami", cmd_whoami))
        ptb_app.add_handler(CommandHandler("reset", cmd_reset))
        ptb_app.add_handler(CommandHandler("clear", cmd_reset))
        ptb_app.add_handler(CommandHandler("resume", cmd_resume))
        ptb_app.add_handler(CommandHandler("model", cmd_model))
        ptb_app.add_handler(CommandHandler("project", cmd_project))
        ptb_app.add_handler(CommandHandler("newtopic", cmd_newtopic))
        ptb_app.add_handler(CommandHandler("diff", cmd_diff))
        ptb_app.add_handler(CommandHandler("cost", cmd_cost))
        ptb_app.add_handler(CommandHandler("usage", cmd_usage))
        ptb_app.add_handler(CommandHandler("stop", cmd_stop))
        ptb_app.add_handler(CommandHandler("later", cmd_later))
        ptb_app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, on_topic_created))
        ptb_app.add_handler(MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            on_message))
        ptb_app.add_error_handler(on_error)

        ctx = _build_ctx(ptb_app)

        # Start web cockpit first (aiohttp, non-blocking)
        await webapp.start(ptb_app, ctx)

        # Start PTB on the same loop via manual lifecycle
        await ptb_app.initialize()
        await ptb_app.start()
        await ptb_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        print("Claude-Ops-Bot started (Telegram + web cockpit).")

        # Idle until shutdown signal
        try:
            await _stop_event.wait()
        finally:
            # spec-039 graceful shutdown — two-phase:
            # Phase 1 (UNBOUNDED): flush sessions + evict live clients.  Must always
            #   run fully — losing session state on restart is worse than a slow stop.
            await _graceful_shutdown(_live_clients)

            # Phase 2 (BOUNDED ≤12 s): tear down webapp background loops + aiohttp
            #   runner, then stop PTB.  Wrapped in wait_for so we can never again
            #   block long enough for systemd's TimeoutStopSec to fire.
            async def _bounded_teardown_tg() -> None:
                await webapp.stop()
                await ptb_app.updater.stop()
                await ptb_app.stop()
                await ptb_app.shutdown()

            try:
                await asyncio.wait_for(_bounded_teardown_tg(), timeout=12.0)
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
    else:
        # ── Web-only mode ─────────────────────────────────────────────────
        # No BOT_TOKEN set.  The web cockpit and engine run standalone.
        # Telegram-specific features (forum-topic auto-bind, TG pings) are
        # simply absent; all ptb_app guards in webapp produce no-ops.
        ctx = _build_ctx(None)
        await webapp.start(None, ctx)
        print("Claude-Ops-Bot started (web-only mode, no Telegram).")

        # Idle until shutdown signal
        try:
            await _stop_event.wait()
        finally:
            # Phase 1 (UNBOUNDED): flush state.
            await _graceful_shutdown(_live_clients)

            # Phase 2 (BOUNDED ≤12 s): tear down webapp.
            try:
                await asyncio.wait_for(webapp.stop(), timeout=12.0)
                print("[shutdown] clean teardown complete (web-only)")
            except asyncio.TimeoutError:
                print("[shutdown] WARNING: bounded teardown timed out (12 s) — "
                      "forcing loop exit; state was already flushed in Phase 1")

            # Phase 3: cancel lingering tasks.
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
