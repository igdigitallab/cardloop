#!/usr/bin/env python3
"""
Claude-Ops-Bot — Claude Code over Telegram.
Forum group: each topic is bound to a project (thread_id -> cwd).
Full permissions (bypassPermissions), subscription auth (no ANTHROPIC_API_KEY by default),
global + project CLAUDE.md loaded via setting_sources. Spec: ~/vault/01-Projects/Claude-Ops-Bot/.
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
from typing import AsyncGenerator

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
)
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
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", str(Path.home()))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "fable")

WEB_PORT = int(os.environ.get("WEB_PORT", "8787"))           # web cockpit port
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")            # passphrase for cockpit login

MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}  # CLI resolves aliases to latest

# ─────────────────────────── sub-agent roster ───────────────────────────
# Default agents available to conductor sessions via the SDK Task tool.
# Models are configurable via env; Phase C will add per-project overrides.
_EXECUTOR_MODEL = os.environ.get("EXECUTOR_MODEL", "sonnet")
_RESEARCHER_MODEL = os.environ.get("RESEARCHER_MODEL", "sonnet")
_QUICK_MODEL = os.environ.get("QUICK_MODEL", "haiku")

DEFAULT_AGENTS: dict = {
    "executor": AgentDefinition(
        description="General code and infra execution agent. Writes files, runs bash commands.",
        prompt=(
            "You are an executor sub-agent. Carry out the task brief you receive completely "
            "and autonomously. Write files, run bash commands, and fix errors as needed. "
            "Report results concisely."
        ),
        model=_EXECUTOR_MODEL,
        permissionMode="bypassPermissions",
    ),
    "researcher": AgentDefinition(
        description="Read-only research agent. Web lookups, file reads, grep. No writes.",
        prompt=(
            "You are a researcher sub-agent. Gather information requested in the task brief. "
            "Use web search, file reads, and grep. Do NOT write or edit files."
        ),
        model=_RESEARCHER_MODEL,
        permissionMode="bypassPermissions",
        disallowedTools=["Write", "Edit", "NotebookEdit"],
    ),
    "quick": AgentDefinition(
        description="Fast lookup and simple transform agent. Cheap, low-latency questions.",
        prompt=(
            "You are a quick-response sub-agent. Answer the task brief concisely and directly."
        ),
        model=_QUICK_MODEL,
        permissionMode="bypassPermissions",
    ),
}


def _build_agents_kwargs(agents_config: dict) -> dict:
    """Build keyword args for run_engine from a project's agents_config dict.

    Returns a dict that can be unpacked into run_engine(**kwargs).
    Empty / absent agents_config → {} (use defaults).

    Keys recognised in agents_config:
        executor_model   — model alias for the executor agent
        researcher_model — model alias for the researcher agent
        quick_model      — model alias for the quick agent
        conductor_prompt — bool; False → pass skip_conductor_prompt=True to run_engine
    """
    if not agents_config:
        return {}

    kwargs: dict = {}

    model_overrides = {
        "executor":   agents_config.get("executor_model"),
        "researcher": agents_config.get("researcher_model"),
        "quick":      agents_config.get("quick_model"),
    }
    has_model_override = any(v for v in model_overrides.values())
    if has_model_override:
        overridden: dict = {}
        for agent_name, agent_def in DEFAULT_AGENTS.items():
            override_model = model_overrides.get(agent_name)
            if override_model:
                overridden[agent_name] = AgentDefinition(
                    description=agent_def.description,
                    prompt=agent_def.prompt,
                    model=override_model,
                    permissionMode=agent_def.permissionMode,
                    disallowedTools=agent_def.disallowedTools,
                )
            else:
                overridden[agent_name] = agent_def
        kwargs["agents"] = overridden

    if "conductor_prompt" in agents_config:
        kwargs["skip_conductor_prompt"] = not agents_config["conductor_prompt"]

    return kwargs


# Conductor directive appended to system_prompt when model is fable.
# Kept as a module constant so it can be asserted in tests without instantiating run_engine.
CONDUCTOR_PROMPT = (
    "You are an orchestrator. Delegate substantial execution to sub-agents via the Task tool — "
    "pass them a self-contained brief (no chat history; just what they need). Reserve your own "
    "turns for planning, decision-making, and synthesising results. Do not run long code "
    "sequences or file-editing loops yourself."
)

# Maximum TaskProgressMessage events forwarded to SSE per task (prevents flood on long runs).
MAX_SUBAGENT_PROGRESS = int(os.environ.get("MAX_SUBAGENT_PROGRESS", "10"))

# Personalisation: set via env; neutral defaults work without .env for new users.
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "the operator")
RESPONSE_LANGUAGE = os.environ.get("RESPONSE_LANGUAGE", "")   # empty = no language directive

# ─────────────────────────── named constants ───────────────────────────
TG_CHUNK = 4000          # max size of a single TG message (characters)

# Operating-brief: injected into EVERY session (all topics), on top of global and project CLAUDE.md.
# ⚠️ nudge — ONLY what genuinely differs from a terminal. Everything about "how to work"
# (scan, surgical edits, permissions, destructive ops) lives in CLAUDE.md (project + ~/CLAUDE.md) —
# the agent loads them via setting_sources and reads the same files as the terminal. Do not duplicate
# here: extra context per turn = dumber agent. Keep short.
_lang_directive = f", answer in {RESPONSE_LANGUAGE}" if RESPONSE_LANGUAGE else ""
TELEGRAM_NUDGE = (
    "Channel is a Telegram bot, not an interactive terminal. Otherwise you are regular Claude Code: "
    "follow the project CLAUDE.md and ~/CLAUDE.md (already loaded) — all working rules are there.\n"
    f"- No interactive dialogs/buttons: if you need clarification or a choice — ask as plain TEXT at "
    f"the end of your reply and finish the turn; {OPERATOR_NAME} will reply in the next message and the session continues.\n"
    f"- Reply concisely{_lang_directive}, in natural prose: what you did → what's next. Do not echo the tool log "
    f"(it's visible in the status) and avoid long code listings — {OPERATOR_NAME} sees edits in files.\n"
    f"- To send a file/screenshot to {OPERATOR_NAME} in this topic: `tg-reply <path> [caption]`.\n"
    "- Key decisions / pitfalls / rejected approaches → write to `.claude-ops/memory/` (see project CLAUDE.md)."
)
# AskUserQuestion = interactive prompt (no reply in TG -> agent hangs or decides on its own).
DISALLOWED_TOOLS = ["AskUserQuestion"]

TOPICS_F = DATA / "topics.json"      # LAYER 1: thread -> project binding (persistent)
SESSIONS_F = DATA / "sessions.json"  # LAYER 2: thread -> session_id (cleared by /reset)

def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _home_sub(*parts: str) -> str:
    """Returns a string path relative to $HOME (dynamic, no hardcoded /home/<user>)."""
    return str(Path.home().joinpath(*parts))


def _load_registry_json() -> dict:
    """Loads data/registry.json (gitignored) if present.
    Format: {"alias": "relative-from-HOME"} — paths relative to $HOME.
    Returns {} if the file is missing or malformed."""
    reg_f = HERE / "data" / "registry.json"
    if not reg_f.exists():
        return {}
    try:
        raw = json.loads(reg_f.read_text())
        return {k: _home_sub(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


# project registry: normalized_alias -> cwd. Covers topic names and folder basenames.
# Operator's real aliases live in gitignored data/registry.json
# (template: registry.example.json). Auto-scan of $HOME adds folder basenames.
_REG_RAW: dict = _load_registry_json()
# Functional alias (NOT personal): General forum topic → default cwd.
# DEFAULT_CWD is parameterised via env, so it stays in code rather than registry.json.
_REG_RAW.setdefault("general", DEFAULT_CWD)


def build_registry() -> dict:
    reg = dict(_REG_RAW)
    base = Path.home()  # dynamic, no hardcoded /home/<user>
    for d in sorted(base.iterdir()):
        if d.is_dir() and ((d / ".git").exists() or (d / "CLAUDE.md").exists()):
            reg.setdefault(_norm(d.name), str(d))
    return reg


REGISTRY = build_registry()


def resolve_project(name: str):
    """name -> (display, cwd) or None. Accepts alias, basename, or absolute path."""
    name = name.strip()
    if name.startswith("/") and Path(name).is_dir():
        return Path(name).name, name
    cwd = REGISTRY.get(_norm(name))
    if cwd and Path(cwd).is_dir():
        return Path(cwd).name, cwd
    return None


# ─────────────────────────── state ───────────────────────────
def _read(f, default):
    try:
        return json.loads(f.read_text())
    except Exception:
        return default


topics = _read(TOPICS_F, {})       # "chat:thread" -> {project, cwd, model}
sessions = _read(SESSIONS_F, {})   # "chat:thread" -> session_id
costs = {}                         # "chat:thread" -> last cost usd
running = {}                       # "chat:thread" -> ClaudeSDKClient (for /stop)
rate_limits = {}                   # rate_limit_type -> {status, resets_at, utilization, ts} (passive)


def save_topics():
    TOPICS_F.write_text(json.dumps(topics, ensure_ascii=False, indent=2))


def save_sessions():
    SESSIONS_F.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))


def key_of(update: Update) -> str:
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    return f"{chat}:{thread}"


def binding_for(update: Update) -> dict:
    """Topic binding. General / no topic -> default. Name-based auto-binding is NOT here
    (that's in on_topic_created). Here we only read + return default for General."""
    k = key_of(update)
    if k in topics:
        return topics[k]
    # topic without a binding -> fall back to DEFAULT_CWD, mark project as unbound
    thread = update.effective_message.message_thread_id
    if not thread:
        return {"project": "General", "cwd": DEFAULT_CWD, "model": DEFAULT_MODEL}
    return None  # unknown topic -> ask user to run /project


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


def short(cmd: str, limit=90) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= limit else cmd[:limit] + "…"


# ─────────────────────────── audit + watchdog ───────────────────────────
AUDIT_DIR = DATA / "audit"
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "300"))   # no events for N sec -> interrupt
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "1800"))      # overall task ceiling (30 min)
_DESTRUCTIVE = ("git push", "push origin", "reset --hard", "rebase", "git clean", "--force",
                "rm -rf", "rm -r ", "rm -f", "drop table", "drop database", "delete from",
                "truncate", "coolify", "docker rm", "docker stop", "compose down",
                "systemctl restart", "systemctl stop")


def _is_destructive(cmd: str) -> bool:
    low = cmd.lower()
    return any(p in low for p in _DESTRUCTIVE)


def audit(project: str, kind: str, text: str):
    """Appends to data/audit/audit-YYYY-MM.log — permanent trail of full-auto bot actions on prod."""
    try:
        AUDIT_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(AUDIT_DIR / f"audit-{time.strftime('%Y-%m')}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} [{project}] {kind}: {text}\n")
    except Exception:
        pass


# ─────────────────────────── ENGINE (async event generator) ───────────────────────────
#
# run_engine — independent event generator. Knows nothing about Telegram, aiohttp, or any transport.
# Transports (TG adapter run_agent) consume its events.
#
# Event schema:
#   {"type": "tool",       "name": str, "input": dict}        — tool invoked by the agent
#   {"type": "text",       "text": str}                        — text block from model response
#   {"type": "result",     "session_id": str|None,
#                          "cost_usd": float|None}             — final ResultMessage
#   {"type": "rate_limit", "rate_limit_type": str, ...}        — RateLimitEvent (passive)
#   {"type": "error",      "exc": BaseException}               — exception from SDK
#
# IMPORTANT — running[session_key]:
#   The adapter (on_message) sets running[k] = True SYNCHRONOUSLY before the first await (race!).
#   run_engine replaces it with the real ClaudeSDKClient immediately after creation.
#   Clearing running.pop(k) is the adapter's responsibility (in finally).

async def run_engine(  # type: ignore[return]
    project_name: str,
    cwd: str,
    prompt: str,
    session_key: str,
    model: str = None,
    system_prompt: dict = None,
    env: dict = None,
    resume_session_id: str = None,
    agents: "dict | None" = None,
    skip_conductor_prompt: bool = False,
) -> "AsyncGenerator[dict, None]":
    """Async SDK event generator. Single source of truth for prompt execution.

    Args:
        project_name          — project name (for audit log)
        cwd                   — working directory
        prompt                — user prompt
        session_key           — key in running/sessions (e.g. "chat:thread")
        model                 — model (alias from MODELS or raw string)
        system_prompt         — dict {type,preset,append}, default is TG preset
        env                   — extra env vars for the agent (TG_CHAT_ID etc.)
        resume_session_id     — session_id to resume (None = new session)
        agents                — sub-agent roster; defaults to DEFAULT_AGENTS when None
        skip_conductor_prompt — if True, suppress conductor directive even for fable model

    Yields event dicts. SDK exceptions are wrapped as {"type": "error", "exc": ...}.
    """
    if system_prompt is None:
        system_prompt = {"type": "preset", "preset": "claude_code", "append": TELEGRAM_NUDGE}

    resolved_model = MODELS.get(model, model) if model else MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)

    # Conductor directive: inject when using fable as orchestrator model (unless disabled per-project).
    if not skip_conductor_prompt and resolved_model and resolved_model.startswith("fable"):
        existing_append = system_prompt.get("append") or ""
        sep = "\n" if existing_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = existing_append + sep + CONDUCTOR_PROMPT

    # Sub-agent roster: use provided agents or fall back to the default roster.
    effective_agents = agents if agents is not None else DEFAULT_AGENTS

    # Fallback model: if fable is unavailable at runtime, degrade to opus silently.
    fallback = "opus" if resolved_model and resolved_model.startswith("fable") else None

    opts = ClaudeAgentOptions(
        model=resolved_model,
        fallback_model=fallback,
        permission_mode="bypassPermissions",
        cwd=cwd,
        setting_sources=["user", "project"],
        resume=resume_session_id,
        disallowed_tools=DISALLOWED_TOOLS,
        system_prompt=system_prompt,
        env=env or {},
        agents=effective_agents,
    )

    audit(project_name, "TASK", short(prompt, 300))

    last_ctx_tokens = 0   # real context size = prompt tokens of the last AssistantMessage
    try:
        async with ClaudeSDKClient(options=opts) as client:
            running[session_key] = client   # replace True-placeholder with the real client (for /stop)
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    # usage of the last assistant message = full prompt of the current turn:
                    # input + cache_read + cache_creation == get_context_usage().totalTokens (verified)
                    u = getattr(msg, "usage", None) or {}
                    pt = (u.get("input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0))
                    if pt:
                        last_ctx_tokens = pt
                    for blk in msg.content:
                        if isinstance(blk, TextBlock) and blk.text.strip():
                            yield {"type": "text", "text": blk.text}
                        elif isinstance(blk, ToolUseBlock):
                            yield {"type": "tool", "name": blk.name, "input": blk.input or {}}
                elif isinstance(msg, RateLimitEvent):
                    i = msg.rate_limit_info
                    yield {
                        "type": "rate_limit",
                        "rate_limit_type": i.rate_limit_type,
                        "status": i.status,
                        "resets_at": i.resets_at,
                        "utilization": i.utilization,
                    }
                elif isinstance(msg, ResultMessage):
                    yield {
                        "type": "result",
                        "session_id": getattr(msg, "session_id", None),
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                        "context_tokens": last_ctx_tokens,
                    }
                elif isinstance(msg, SystemMessage):
                    if isinstance(msg, TaskStartedMessage):
                        yield {
                            "type": "subagent",
                            "subtype": "started",
                            "task_id": msg.task_id,
                            "description": msg.description,
                            "status": None,
                            "summary": None,
                            "last_tool_name": None,
                        }
                    elif isinstance(msg, TaskProgressMessage):
                        yield {
                            "type": "subagent",
                            "subtype": "progress",
                            "task_id": msg.task_id,
                            "description": msg.description,
                            "status": None,
                            "summary": None,
                            "last_tool_name": getattr(msg, "last_tool_name", None),
                        }
                    elif isinstance(msg, TaskNotificationMessage):
                        yield {
                            "type": "subagent",
                            "subtype": "notification",
                            "task_id": msg.task_id,
                            "description": msg.summary,   # notification has no description field
                            "status": msg.status,
                            "summary": msg.summary,
                            "last_tool_name": None,
                        }
                    # Other SystemMessage subtypes remain silent
    except Exception as exc:
        yield {"type": "error", "exc": exc}


# ─────────────────────────── TG adapter ───────────────────────────
#
# run_agent — run_engine consumer for the Telegram channel.
# Renders the status message (edit), watchdog, heartbeat, audit log, and final reply.
# Behaviour is 1-to-1 with the original — only the event source is replaced by the generator.

async def run_agent(context, update, prompt: str):
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    cwd, model = b["cwd"], b.get("model", DEFAULT_MODEL)
    # slot already reserved in on_message (running[k]=True) — here we just do the work

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
        """Interrupts a stalled task: no events for stall_s OR max_s exceeded.
        Thresholds come from cockpit global settings (settings.json), falling back to env defaults."""
        stall_s, max_s = STALL_SECONDS, MAX_SECONDS
        try:
            import webapp as _wa
            stall_s = int(_wa._get_global_setting("watchdog_stall_sec", STALL_SECONDS) or STALL_SECONDS)
            max_s = int(_wa._get_global_setting("watchdog_max_sec", MAX_SECONDS) or MAX_SECONDS)
        except Exception:
            pass
        try:
            while True:
                await asyncio.sleep(min(stall_s, 20))
                now = time.time()
                idle = now - last_event[0]
                if idle > stall_s:
                    if stall_s < 60:
                        stalled["reason"] = f"no events for {int(idle)}s"
                    else:
                        stalled["reason"] = f"no events for {int(idle // 60)} min"
                elif now - t_start > max_s:
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
    webapp._bus_publish(k, {"kind": "run_start", "source": "tg", "prompt": prompt, "run_id": None})
    try:
        # Project secrets (Spec 007) augment env; TG_CHAT_ID/TG_THREAD_ID take priority
        project_secrets = webapp._secrets_read(cwd)
        agent_env = {**project_secrets, "TG_CHAT_ID": str(chat), "TG_THREAD_ID": str(thread or 0)}
        agents_config = b.get("agents_config") or {}
        agent_kwargs = _build_agents_kwargs(agents_config)
        async for event in run_engine(
            project_name=b["project"],
            cwd=cwd,
            prompt=prompt,
            session_key=k,
            model=model,
            system_prompt={"type": "preset", "preset": "claude_code", "append": TELEGRAM_NUDGE},
            env=agent_env,
            resume_session_id=sessions.get(k),
            **agent_kwargs,
        ):
            last_event[0] = time.time()   # any SDK event = "alive" for watchdog
            etype = event["type"]

            if etype == "text":
                answer.append(event["text"])
                log_lines.append("💬 " + short(event["text"].replace("\n", " "), 70))
                webapp._bus_publish(k, {"kind": "text", "text": event["text"], "run_id": None})

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
                webapp._bus_publish(k, {
                    "kind": "tool", "run_id": None,
                    "tool": webapp._format_tool(name, inp if isinstance(inp, dict) else {}),
                })
                await push_status()

            elif etype == "result":
                if event.get("session_id"):
                    sessions[k] = event["session_id"]
                    save_sessions()
                if event.get("cost_usd") is not None:
                    costs[k] = event["cost_usd"]

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
                webapp._bus_publish(k, {"kind": "subagent", "run_id": None, **event})

            elif etype == "error":
                engine_exc = event["exc"]

    except Exception as exc:
        engine_exc = exc
    finally:
        hb.cancel()
        wd.cancel()
        webapp._bus_publish(k, {
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
    k = key_of(update)
    if k not in topics and msg.message_thread_id:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   "🔌 Topic is not bound to a project. Bind it: /project <name>")
        return
    # race-condition guard: reserve slot SYNCHRONOUSLY before the first await
    if k in running:
        await send(context, update.effective_chat.id, msg.message_thread_id,
                   "⏳ Already running in this topic. Use /stop to interrupt.")
        return
    running[k] = True  # placeholder; run_engine will replace with the real client
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
        running.pop(k, None)
        await send(context, cid, tid, f"⚠️ Task launch error: {e}")


async def safe_run(context, update, prompt):
    """Background task wrapper: PTB does not catch exceptions from asyncio.create_task itself."""
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    k = key_of(update)
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
        running.pop(k, None)  # always clear the reservation, even if run_agent crashed before its try


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
    k = key_of(update)
    r = resolve_project(name)
    if r:
        topics[k] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL}
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
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    if not b:
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   "🔌 Topic not bound. /project <name>")
        return
    sid = sessions.get(k, "—")
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"📍 <b>{b['project']}</b>\ncwd: <code>{b['cwd']}</code>\nmodel: {b.get('model', DEFAULT_MODEL)}\n"
               f"session: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_reset(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    sessions.pop(k, None)
    save_sessions()
    b = topics.get(k) or binding_for(update)
    proj = b["project"] if b else "—"
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🔄 Context reset. Project <b>{proj}</b> preserved.", parse_mode=ParseMode.HTML)


async def cmd_resume(update, context):
    if not authorized(update):
        return
    k = key_of(update)
    if context.args:
        sessions[k] = context.args[0]
        save_sessions()
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"⏯ Resuming session <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)
    else:
        sid = sessions.get(k, "—")
        await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
                   f"Current topic session: <code>{sid}</code>", parse_mode=ParseMode.HTML)


async def cmd_model(update, context):
    if not authorized(update):
        return
    k = key_of(update)
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
    if k in topics:
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
    k = key_of(update)
    prev = topics.get(k, {})
    topics[k] = {"project": r[0], "cwd": r[1], "model": prev.get("model", DEFAULT_MODEL)}
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
    k = f"{update.effective_chat.id}:{tid}"
    r = resolve_project(name)
    if r:
        topics[k] = {"project": r[0], "cwd": r[1], "model": DEFAULT_MODEL}
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
    k = key_of(update)
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
    k = key_of(update)
    c = costs.get(k)
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
    k = key_of(update)
    client = running.get(k)
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


# ─────────────────────────── main ───────────────────────────

def _build_ctx(ptb_app) -> dict:
    """Build the shared context dict passed to webapp.start().

    ptb_app is the PTB Application instance in TG mode, or None in web-only mode.
    All other values come from module-level state so both modes share the same
    topics/sessions/running/etc. dicts.
    """
    return {
        "port": WEB_PORT,
        "password": WEB_PASSWORD,
        "topics": topics,
        "sessions": sessions,
        "running": running,
        "costs": costs,
        "rate_limits": rate_limits,
        "resolve_project": resolve_project,
        "REGISTRY": REGISTRY,
        "save_sessions": save_sessions,
        "save_topics": save_topics,
        "DATA": DATA,
        "DEFAULT_CWD": DEFAULT_CWD,
        "DEFAULT_MODEL": DEFAULT_MODEL,
        "VAULT_PROJECTS": Path(os.environ["VAULT_PROJECTS"]) if os.environ.get("VAULT_PROJECTS") else None,
        "HERE": HERE,
        # Engine + models for kanban auto-run
        "run_engine": run_engine,
        "MODELS": MODELS,
        "DEFAULT_AGENTS": DEFAULT_AGENTS,
        # Per-project agents_config helper (Spec 017 Phase C)
        "_build_agents_kwargs": _build_agents_kwargs,
        # PTB app reference for TG pings from _run_card and notify_on_error.
        # None in web-only mode — all callers guard on ptb_app is None → no-op.
        "ptb_app": ptb_app,
        # Needed to synthesise session_key "<chat>:<thread>" when creating new projects
        "GROUP_CHAT_ID": GROUP_CHAT_ID,
    }


async def _amain() -> None:
    """Async entry point.

    Always starts the web cockpit + engine on the current asyncio loop.
    PTB (Telegram polling) starts only when BOT_TOKEN is set.  In web-only
    mode ptb_app=None is passed to webapp so all TG side-effects are skipped.

    Loop ownership: a single asyncio loop drives both aiohttp and PTB.
    PTB is started via the manual lifecycle (initialize/start/start_polling)
    rather than run_polling() so it does NOT take over the loop.
    """
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
            await asyncio.Event().wait()
        finally:
            await ptb_app.updater.stop()
            await ptb_app.stop()
            await ptb_app.shutdown()
    else:
        # ── Web-only mode ─────────────────────────────────────────────────
        # No BOT_TOKEN set.  The web cockpit and engine run standalone.
        # Telegram-specific features (forum-topic auto-bind, TG pings) are
        # simply absent; all ptb_app guards in webapp produce no-ops.
        ctx = _build_ctx(None)
        await webapp.start(None, ctx)
        print("Claude-Ops-Bot started (web-only mode, no Telegram).")

        # Idle until shutdown signal
        await asyncio.Event().wait()


def main():
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
