#!/usr/bin/env python3
"""
Claude-Ops-Bot — Claude Code over Telegram.
Forum group: each topic is bound to a project (thread_id -> cwd).
Full permissions (bypassPermissions), subscription auth (no ANTHROPIC_API_KEY by default),
global + project CLAUDE.md loaded via setting_sources. Spec: ~/vault/01-Projects/Claude-Ops-Bot/.
"""
import asyncio
import dataclasses
import hashlib
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
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookContext, PostToolUseHookInput
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

# Effort level for the conductor/main session.
# "medium" reduces rate-limit burn (thinking weighs ~5× in the window) vs the
# SDK default of "high". Gate behind env so operators can escalate without a
# code change. Valid values: low | medium | high | xhigh | max.
# Note: on Fable 5 thinking always runs high regardless; effort is silently
# ignored or coerced by the CLI for subscription models — no SDK error is raised.
_DEFAULT_EFFORT: str = os.environ.get("DEFAULT_EFFORT", "medium")

DEFAULT_AGENTS: dict = {
    "executor": AgentDefinition(
        description="General code and infra execution agent. Writes files, runs bash commands.",
        # Adapted from addyosmani/agent-skills (MIT)
        # https://github.com/addyosmani/agent-skills/blob/main/LICENSE
        prompt=(
            "You are an executor sub-agent. Carry out the task brief you receive completely "
            "and autonomously. Write files, run bash commands, and fix errors as needed. "
            "Report results concisely.\n\n"
            "PLANNING MODE — read-only first. Map the dependency graph before writing any code: "
            "schema → models → endpoints → client → UI. "
            "Implement bottom-up. Each task: title + acceptance criteria + test signal. Max 1 day per task.\n\n"
            "SOURCE-DRIVEN — before writing framework-specific code, state the exact stack "
            "(read package.json / pyproject.toml / go.mod). "
            "Fetch official docs for the relevant pattern (WebFetch / WebSearch). "
            "Implement only what the docs describe. Cite the URL in a comment. "
            "Training data goes stale — verify, don't assume.\n\n"
            "DOUBT CHECK — before committing: is this decision non-trivial? "
            "(New branching logic? Crosses module boundary? Irreversible in production?) "
            "If YES → run the doubt cycle: Claim → Contract → Adversarial → Reconcile → Stop. "
            "Stop after 3 cycles or when findings are already handled."
        ),
        model=_EXECUTOR_MODEL,
        permissionMode="bypassPermissions",
        # Minimal tool set: executor needs read/write/run + web for doc lookups.
        tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch"],
        maxTurns=40,
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
        # Minimal tool set: read-only lookups only.
        tools=["Bash", "Read", "Glob", "Grep", "WebFetch", "WebSearch"],
        maxTurns=20,
    ),
    "quick": AgentDefinition(
        description="Fast lookup and simple transform agent. Cheap, low-latency questions.",
        prompt=(
            "You are a quick-response sub-agent. Answer the task brief concisely and directly."
        ),
        model=_QUICK_MODEL,
        permissionMode="bypassPermissions",
        # Minimal tool set: lightweight lookups only; no web fetch needed for simple transforms.
        tools=["Bash", "Read", "Glob", "Grep"],
        effort="low",   # haiku + low effort: fastest possible response, no extended thinking overhead
        maxTurns=10,
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
                    tools=agent_def.tools,
                    effort=agent_def.effort,
                    maxTurns=agent_def.maxTurns,
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
    "sequences or file-editing loops yourself. "
    "Prefer ≤3–5 concurrent sub-agents; sequence tasks rather than parallelising unnecessarily."
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
TG_QUEUE_F = DATA / "tg_queue.json"  # LAYER 3: per-topic message queue (persists across restarts)

TG_QUEUE_MAX = int(os.environ.get("TG_QUEUE_MAX", "5"))  # max messages queued per topic

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

# ── Spec-028 Phase 2: live-client registry ────────────────────────────────────────────────────
# Only populated when PERSISTENT_CLIENT=1; empty (and dormant) otherwise.

@dataclasses.dataclass
class _LiveEntry:
    """Holds a connected ClaudeSDKClient that survives across turns."""
    client: object              # ClaudeSDKClient
    fingerprint: str            # hash of immutable opts fields; mismatch → evict+recreate
    last_used: float            # time.monotonic() timestamp of the last turn start
    idle_task: object           # asyncio.Task for TTL-based eviction; None until scheduled
    session_key: str            # key in running / _live_clients


_live_clients: "dict[str, _LiveEntry]" = {}  # session_key -> _LiveEntry
# Spec-021 Phase 4: one-shot handoff summaries pending injection into the next turn after rotation.
# {session_key: summary_text}. Cleared immediately after injection so it fires exactly once.
# NOTE: In-memory only — lost on service restart between rotation and next turn; that is acceptable.
pending_handoff: "dict[str, str]" = {}
# Context early-warn: tracks session keys that have already received the CONTEXT_WARN_AT alert.
# Cleared on rotation (_do_session_rotation) and on /reset so a fresh session can warn again.
context_warned: "set[str]" = set()

# ─────────────────────────── TG message queue ───────────────────────────
# Per-topic FIFO queue of pending user messages received while a run is in progress.
# Survives restarts via TG_QUEUE_F (data/tg_queue.json — gitignored inside data/).
# In-memory canonical dict: {session_key: [{"prompt": str, "msg_id": int}, ...]}
# All mutations are synchronous (no await between read and write) — race-safe.
_TG_QUEUE: "dict[str, list[dict]]" = _read(TG_QUEUE_F, {})


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

# ── Spec-028: persistent (long-lived) client feature flag ─────────────────────────────────────
# PERSISTENT_CLIENT=0 (default OFF) → behaviour is byte-identical to pre-028; all existing tests pass.
# PERSISTENT_CLIENT=1 → run_engine reuses the same ClaudeSDKClient across turns for non-ephemeral
# sessions (chat / deferred), skipping per-turn connect/disconnect overhead.
PERSISTENT_CLIENT: bool = os.environ.get("PERSISTENT_CLIENT", "0") == "1"
# Max idle seconds before an unused live client is evicted (disconnected) automatically.
LIVE_CLIENT_TTL_SEC: int = int(os.environ.get("LIVE_CLIENT_TTL_SEC", "600"))
# Max number of concurrent live clients held in the registry; LRU eviction beyond this.
LIVE_CLIENT_MAX: int = int(os.environ.get("LIVE_CLIENT_MAX", "10"))
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


# ─────────────────────────── Spec-029 §2: PostToolUse hook ────────────────────────────────────
#
# Enriches the audit log and timeline with actual tool OUTPUT.
# Previously only the tool invocation (command / file path) was recorded; now the result
# (stdout, edit outcome, etc.) is also captured — greatly reducing "what actually ran?" debugging.
#
# Safety guarantees (hot-path):
#   1. Entire body wrapped in try/except — a hook crash NEVER breaks a turn.
#   2. Output is truncated to _HOOK_OUTPUT_TRUNCATE chars — protects against huge Bash stdout.
#   3. env / secret values are never passed; the hook receives tool_response only.
#   4. Returns {} (empty SyncHookJSONOutput) — no side-effects on the model's view of the output.

_HOOK_OUTPUT_TRUNCATE = 500  # chars — keep audit lines readable, cap hot-path I/O


def _tool_response_to_str(tool_response: object) -> str:
    """Convert a raw tool_response to a single-line string, truncated to _HOOK_OUTPUT_TRUNCATE.

    tool_response may be:
      - dict  (e.g. {"stdout": "...", "stderr": "...", "interrupted": False} for Bash)
      - str   (plain text for Read, Edit, etc.)
      - other (fallback repr)
    Never raises.
    """
    try:
        if isinstance(tool_response, dict):
            # Prefer stdout; include stderr only when stdout is empty.
            stdout = str(tool_response.get("stdout", "") or "")
            stderr = str(tool_response.get("stderr", "") or "")
            interrupted = tool_response.get("interrupted", False)
            parts = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"[stderr] {stderr}")
            if interrupted:
                parts.append("[interrupted]")
            raw = " ".join(parts) if parts else repr(tool_response)
        else:
            raw = str(tool_response)
    except Exception:
        return "<unparseable>"

    # Collapse newlines to spaces for single-line audit entries.
    single = raw.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(single) > _HOOK_OUTPUT_TRUNCATE:
        return single[:_HOOK_OUTPUT_TRUNCATE] + "…"
    return single


def _make_post_tool_use_hook(project_name: str, session_key: str):
    """Return an async HookCallback that records tool output in the audit log and timeline.

    Closes over `project_name` and `session_key` so the hook can route audit lines to the
    correct project without receiving env or secrets.  `webapp` is imported at module level
    so timeline publishing works when webapp is initialised (production) and is silently
    skipped when it is not (tests that don't set up _TIMELINE_DATA_DIR).
    """
    async def _post_tool_use_hook(
        hook_input: "PostToolUseHookInput",
        tool_use_id: "str | None",
        context: "HookContext",
    ) -> dict:
        """Record tool output to audit log and timeline. Never raises."""
        try:
            tool_name = hook_input.get("tool_name", "?") if isinstance(hook_input, dict) else getattr(hook_input, "tool_name", "?")
            tool_response = hook_input.get("tool_response") if isinstance(hook_input, dict) else getattr(hook_input, "tool_response", None)

            output_str = _tool_response_to_str(tool_response)

            # Determine ok/err: dict with "error" key, or exception-like object.
            is_err = False
            try:
                if isinstance(tool_response, dict):
                    is_err = bool(tool_response.get("error") or tool_response.get("is_error"))
                elif hasattr(tool_response, "is_error"):
                    is_err = bool(tool_response.is_error)
            except Exception:
                pass

            status = "err" if is_err else "ok"
            audit_text = f"{tool_name} {status} {output_str}"
            audit(project_name, "RESULT", audit_text)

            # Also publish to timeline via the webapp bus (only available post-init).
            try:
                webapp._timeline_append(session_key, {
                    "kind": "tool_result",
                    "tool": tool_name,
                    "status": status,
                    "output": output_str,
                })
            except Exception:
                pass  # webapp not initialised or timeline write error — never break a turn
        except Exception:
            pass  # entire hook body is guarded — never propagate to the SDK

        return {}  # empty SyncHookJSONOutput — no model-visible side-effects

    return _post_tool_use_hook


# ─────────────────────────── Spec-028: live-client helpers ─────────────────────────────────────
#
# These helpers are only active when PERSISTENT_CLIENT=1.
# With the flag OFF they are never called and the behaviour is byte-identical to pre-028.

def _compute_fingerprint(opts: "ClaudeAgentOptions") -> str:
    """Hash the subset of opts fields that are immutable once a ClaudeSDKClient is connected.

    A fingerprint mismatch (e.g. /model switch, different system_prompt preset) means we must
    evict the live entry and reconnect rather than reusing the old subprocess.

    Fields deliberately excluded: resume (session_id), env (per-turn TG_CHAT_ID etc.),
    agents roster (can't change the subprocess mid-session anyway), effort.
    """
    parts = [
        str(getattr(opts, "cwd", "")),
        str(getattr(opts, "model", "")),
        str(getattr(opts, "permission_mode", "")),
        str(sorted(getattr(opts, "setting_sources", []) or [])),
        str(sorted(getattr(opts, "disallowed_tools", []) or [])),
        # Capture the stable identity of the system_prompt (preset type/name) without the
        # per-turn append text — we don't want every TG nudge update to force a reconnect.
        str((getattr(opts, "system_prompt", None) or {}).get("type", "")),
        str((getattr(opts, "system_prompt", None) or {}).get("preset", "")),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


async def _evict_live_client(session_key: str, ctx: "dict | None") -> None:
    """Disconnect and remove a live client entry. Safe to call even if the key is absent.

    Cancels the idle timer, disconnects the subprocess (with a 10 s timeout guard), and
    removes the entry from _live_clients (and from ctx["live_clients"] if ctx is provided).
    """
    registry: "dict[str, _LiveEntry]" = (ctx or {}).get("live_clients", _live_clients)
    entry = registry.pop(session_key, None)
    if entry is None:
        return
    # Cancel the pending idle-eviction task.  We do NOT await it — the task is fire-and-forget
    # and its CancelledError is handled internally.  Awaiting a shielded cancelled task raises
    # CancelledError in the caller, which is never what we want here.
    if entry.idle_task is not None and not entry.idle_task.done():
        entry.idle_task.cancel()
    # Disconnect the subprocess.
    try:
        await asyncio.wait_for(entry.client.disconnect(), timeout=10)
    except Exception as exc:
        print(f"[live-client] evict {session_key}: disconnect failed ({exc!r}), force-dropping")


async def _get_or_create_live_client(
    ctx: "dict | None",
    session_key: str,
    opts: "ClaudeAgentOptions",
    *,
    ephemeral: bool,
) -> "object | None":
    """Return a reusable connected ClaudeSDKClient for session_key, or None.

    Returns None whenever the persistent-client path should NOT be taken:
    - Feature flag is OFF (PERSISTENT_CLIENT=False)
    - The call site explicitly requests ephemeral isolation (_run_card, _do_session_rotation)
    - ctx is None (ctx-less test/legacy callers; they use the standard `async with` path)

    On flag-ON + non-ephemeral:
    - Existing matching entry → cancel idle timer, bump last_used, return client.
    - Fingerprint mismatch (model switch etc.) → evict old, create new.
    - No entry → create, connect, register, start idle timer.
    - Enforces LIVE_CLIENT_MAX via LRU eviction.
    """
    if not PERSISTENT_CLIENT or ephemeral or ctx is None:
        return None

    registry: "dict[str, _LiveEntry]" = ctx.get("live_clients", _live_clients)
    fingerprint = _compute_fingerprint(opts)

    existing = registry.get(session_key)
    if existing is not None:
        if existing.fingerprint != fingerprint:
            print(f"[live-client] fingerprint changed for {session_key} — evicting and reconnecting")
            await _evict_live_client(session_key, ctx)
            # Fall through to create a new entry.
        else:
            # Reuse: cancel the pending idle countdown and refresh the timestamp.
            if existing.idle_task is not None and not existing.idle_task.done():
                existing.idle_task.cancel()
            existing.last_used = time.monotonic()
            existing.idle_task = _schedule_idle_eviction(session_key, ctx)
            return existing.client

    # ── Enforce LIVE_CLIENT_MAX via LRU ──────────────────────────────────────────────────────────
    while len(registry) >= LIVE_CLIENT_MAX:
        oldest_key = min(registry, key=lambda k: registry[k].last_used)
        print(f"[live-client] LRU evict {oldest_key} (registry full at {LIVE_CLIENT_MAX})")
        await _evict_live_client(oldest_key, ctx)

    # ── Create and connect ────────────────────────────────────────────────────────────────────────
    client = ClaudeSDKClient(options=opts)
    await client.connect()
    entry = _LiveEntry(
        client=client,
        fingerprint=fingerprint,
        last_used=time.monotonic(),
        idle_task=None,
        session_key=session_key,
    )
    registry[session_key] = entry
    entry.idle_task = _schedule_idle_eviction(session_key, ctx)
    print(f"[live-client] created entry for {session_key} (total: {len(registry)})")
    return client


def _schedule_idle_eviction(session_key: str, ctx: "dict | None") -> "asyncio.Task":
    """Create (and return) an asyncio Task that evicts session_key after LIVE_CLIENT_TTL_SEC idle.

    The task is a module-level detached task — NOT tied to any turn coroutine — so it
    survives after the turn generator is exhausted.
    """
    async def _idle_waiter():
        try:
            await asyncio.sleep(LIVE_CLIENT_TTL_SEC)
            print(f"[live-client] idle TTL expired for {session_key} — evicting")
            await _evict_live_client(session_key, ctx)
        except asyncio.CancelledError:
            pass  # Normal: cancelled when the entry is reused or manually evicted.

    return asyncio.ensure_future(_idle_waiter())


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
    *,
    ctx: "dict | None" = None,
    ephemeral: bool = False,
    output_format: "dict | None" = None,
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
        ctx                   — shared context dict (Spec-028 Phase 1): used for running[]
                                 lookup and live-client registry when PERSISTENT_CLIENT=1.
                                 None → falls back to module-level globals (pre-028 behaviour).
        ephemeral             — if True, always use a fresh ClaudeSDKClient (no live-client
                                 reuse). Set by _run_card and _do_session_rotation which must
                                 be fully isolated from shared sessions.
        output_format         — Spec-029 item 3: optional JSON-schema dict for structured output.
                                 When provided, ClaudeAgentOptions.output_format is set and
                                 ResultMessage.structured_output is passed through the result event.
                                 Shape: {"type": "json_schema", "schema": {...}}.
                                 None (default) → no change to existing behaviour (chat/TG runs).

    Yields event dicts. SDK exceptions are wrapped as {"type": "error", "exc": ...}.
    """
    if system_prompt is None:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": TELEGRAM_NUDGE,
            "exclude_dynamic_sections": True,
        }

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

    # Spec-029 §2: PostToolUse hook — records tool output to audit log + timeline.
    _post_tool_hook = _make_post_tool_use_hook(project_name, session_key)

    # Spec-029 §1: live streaming — emit text_delta events for incremental cockpit display.
    # STREAM_PARTIAL=0 disables without code changes (e.g. for debugging or regression isolation).
    # Default ON: clean reconciliation (the final {type:"text"} remains authoritative, deltas are
    # preview-only; no double-render because the frontend replaces accumulated delta text on receipt
    # of the finalized block via finalizeStreamingWithMetrics).
    _stream_partial = os.environ.get("STREAM_PARTIAL", "1") not in ("0", "false", "False")

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
        effort=_DEFAULT_EFFORT,  # type: ignore[arg-type]
        hooks={"PostToolUse": [HookMatcher(hooks=[_post_tool_hook])]},
        # include_hook_events=False (default) — HookEventMessage lifecycle noise adds no extra
        # data beyond what the hook callback already captures, and would flood _process_messages.
        include_partial_messages=_stream_partial,
        # Spec-029 item 3: structured output for card runs. None → no change (chat/TG paths).
        output_format=output_format,
    )

    audit(project_name, "TASK", short(prompt, 300))

    # Spec-028 Phase 1: resolve which running-dict to use.
    # ctx is provided by call sites that have a context dict (run_agent, api_project_chat,
    # _execute_deferred).  Legacy / ctx-less callers (tests, _maybe_rotate_tg slim ctx) fall
    # back to the module-global `running` dict — behaviour is identical to pre-028.
    _running: dict = ctx["running"] if ctx is not None and "running" in ctx else running

    last_ctx_tokens = 0   # real context size = prompt tokens of the last AssistantMessage
    # Spec-022: track per-turn usage for cost visibility
    last_usage: dict = {}
    _turn_start_ms: float = 0.0  # wall-clock fallback when SDK duration_ms is absent

    # Shared inner generator: processes SDK messages and yields engine events.
    # Extracted so both the live-client branch and the `async with` branch share
    # identical event-processing logic with no duplication.
    async def _process_messages(client):
        """Read messages from `client` and yield engine events."""
        nonlocal last_ctx_tokens, last_usage, _turn_start_ms
        _turn_start_ms = __import__("time").monotonic() * 1000
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
                last_usage = u  # capture for the result event
                for blk in msg.content:
                    if isinstance(blk, TextBlock) and blk.text.strip():
                        yield {"type": "text", "text": blk.text}
                    elif isinstance(blk, ToolUseBlock):
                        yield {"type": "tool", "name": blk.name, "input": blk.input or {}}
            elif isinstance(msg, StreamEvent):
                # Spec-029 §1: incremental text delta for live cockpit streaming.
                # Only content_block_delta / text_delta carries visible text — all other
                # event subtypes (message_start, message_delta, content_block_start/stop,
                # input_json_delta for tool calls, etc.) are silently ignored here.
                # The finalised AssistantMessage TextBlock above remains the source of truth.
                try:
                    evt = msg.event
                    if (
                        evt.get("type") == "content_block_delta"
                        and evt.get("delta", {}).get("type") == "text_delta"
                    ):
                        delta_text = evt["delta"].get("text", "")
                        if delta_text:
                            yield {"type": "text_delta", "text": delta_text}
                except Exception:
                    pass  # never let a malformed partial event break the turn
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
                # Spec-022: per-turn cost visibility fields
                _u = last_usage
                _cache_read = _u.get("cache_read_input_tokens", 0) or 0
                _fresh = (_u.get("input_tokens", 0) or 0) + (_u.get("cache_creation_input_tokens", 0) or 0)
                _pt = _cache_read + _fresh  # == last_ctx_tokens when >0
                _cache_hit_pct = round((_cache_read / _pt) * 100) if _pt > 0 else 0
                # Duration: prefer SDK attribute, fall back to wall-clock measurement.
                # SDK may expose duration_ms or duration_api_ms on ResultMessage.
                _dur = getattr(msg, "duration_ms", None)
                if _dur is None:
                    _dur = getattr(msg, "duration_api_ms", None)
                if _dur is None and _turn_start_ms > 0:
                    _dur = round(__import__("time").monotonic() * 1000 - _turn_start_ms)
                yield {
                    "type": "result",
                    "session_id": getattr(msg, "session_id", None),
                    "cost_usd": getattr(msg, "total_cost_usd", None),
                    "context_tokens": last_ctx_tokens,
                    # api_error_status: HTTP status when run failed (e.g. 429 = rate-limited).
                    # None on success. Available since SDK v2.1.110.
                    "api_error_status": getattr(msg, "api_error_status", None),
                    # Spec-022: per-turn cache/token metrics (facts from SDK usage)
                    "cache_read_tokens": _cache_read,
                    "fresh_tokens": _fresh,
                    "prompt_tokens": _pt if _pt > 0 else last_ctx_tokens,
                    "cache_hit_pct": _cache_hit_pct,
                    "duration_ms": _dur,
                    # Spec-029 item 3: structured output from ResultMessage (None when not requested
                    # or when the CLI did not populate it). Consumers that set output_format should
                    # read this field; all other consumers ignore it (it is always present as None).
                    "structured_output": getattr(msg, "structured_output", None),
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

    # ── Spec-028 Phase 2: live-client branch (flag-gated, ephemeral=False only) ──────────────────
    # When PERSISTENT_CLIENT=0 (default) _get_or_create_live_client returns None immediately and
    # we fall through to the pre-028 `async with` path — byte-identical behaviour.
    try:
        live = await _get_or_create_live_client(ctx, session_key, opts, ephemeral=ephemeral)
    except Exception as _lc_exc:
        # Live-client setup failure must never silently swallow the turn — degrade gracefully.
        print(f"[live-client] setup failed for {session_key} ({_lc_exc!r}), falling back to fresh client")
        live = None

    if live is not None:
        # ── Persistent-client path ────────────────────────────────────────────────────────────────
        # The client is already connected; we skip __aenter__ / __aexit__.
        # running[session_key] is set here (replacing the True placeholder) so the watchdog and
        # /stop command can interrupt mid-turn.  We MUST pop it in finally (the adapter's finally
        # also pops it, making this double-safe).
        # We do NOT call client.disconnect() — the live-client registry owns the lifecycle.
        _running[session_key] = live
        try:
            async for event in _process_messages(live):
                yield event
        except Exception as exc:
            # Subprocess state is unknown after an error — evict so the next turn reconnects fresh.
            print(f"[live-client] error during turn for {session_key} ({exc!r}) — evicting")
            await _evict_live_client(session_key, ctx)
            yield {"type": "error", "exc": exc}
        finally:
            # DO NOT disconnect — the live client must survive for the next turn.
            # The adapter (safe_run / api_project_chat finally) clears running[k] separately;
            # we do it here too as a safety net for ctx-isolated callers.
            _running.pop(session_key, None)
    else:
        # ── Standard fresh-client path (pre-028 behaviour, unchanged) ────────────────────────────
        try:
            async with ClaudeSDKClient(options=opts) as client:
                _running[session_key] = client  # replace True-placeholder (for /stop)
                async for event in _process_messages(client):
                    yield event
        except Exception as exc:
            yield {"type": "error", "exc": exc}


# ─────────────────────────── TG adapter ───────────────────────────
#
# run_agent — run_engine consumer for the Telegram channel.
# Renders the status message (edit), watchdog, heartbeat, audit log, and final reply.
# Behaviour is 1-to-1 with the original — only the event source is replaced by the generator.

async def _maybe_rotate_tg(context, chat, thread, k: str, b: dict, last_result_event: "dict | None"):
    """Spec-021: auto session rotation for the TG channel.

    Mirrors the web-path guards: global toggle (CONTEXT_ROTATION), token threshold
    (CONTEXT_ROTATE_AT), and a TG-queue-drain guard — rotation is skipped while
    _TG_QUEUE[k] has pending messages and fires after the last drained turn instead.
    Called exactly once at the end of run_agent, so no once-per-turn flag is needed
    (unlike the web path, which checks inside the event loop).

    Wiring: direct call into webapp._do_session_rotation — same direction as the
    existing webapp._maybe_auto_resume call (only webapp->bot imports are forbidden).
    Rotation failure must never break the TG turn — the whole body is guarded.
    """
    try:
        ctx_tokens = (last_result_event or {}).get("context_tokens", 0) or 0
        if not webapp.CONTEXT_ROTATION or ctx_tokens <= webapp.CONTEXT_ROTATE_AT:
            return
        if _TG_QUEUE.get(k):
            print(f"[rotation] skipping — TG queue not empty for {k}")
            return
        # _do_session_rotation only reads run_engine/sessions/save_sessions from ctx.
        # ptb_app is intentionally omitted: the TG notification is sent below via
        # send() (already in the right chat/thread, with retry) instead of
        # webapp._notify_tg_rotation — exactly one notification, never both.
        rot_ctx = {
            "sessions": sessions,
            "save_sessions": save_sessions,
            "run_engine": run_engine,
        }
        rot_project = {"name": b["project"], "model": b.get("model", DEFAULT_MODEL)}
        summary = await webapp._do_session_rotation(rot_ctx, k, rot_project, b["cwd"])
        if summary is not None:
            # Spec-021 Phase 4: mark pending handoff so next turn gets the summary injected.
            try:
                pending_handoff[k] = summary
            except Exception as _ph_exc:
                print(f"[rotation] failed to store pending_handoff: {_ph_exc}")
            await send(
                context, chat, thread,
                md_to_html(f"♻️ Session rotated at {ctx_tokens // 1000}K tokens — handoff saved"),
                parse_mode=ParseMode.HTML,
            )
    except Exception as rot_exc:
        print(f"[rotation] TG-path rotation failed (continuing with old session): {rot_exc}")


async def _maybe_warn_tg(context, chat, thread, k: str, last_result_event: "dict | None"):
    """Spec-021: one-time early context warning for the TG channel.

    Fires when context crosses webapp.CONTEXT_WARN_AT (upward only) and has not yet
    reached the hard backstop webapp.CONTEXT_ROTATE_AT.  Tracks the crossing in the
    module-level context_warned set so the warning is sent at most once per session.
    Cleared by cmd_reset (/reset) and by _do_session_rotation so a fresh session can warn again.

    Mirrors the anti-spam and try/except guard pattern of _maybe_rotate_tg.
    """
    try:
        ctx_tokens = (last_result_event or {}).get("context_tokens", 0) or 0
        warn_at = webapp.CONTEXT_WARN_AT
        rotate_at = webapp.CONTEXT_ROTATE_AT
        # Only fire in the warn zone (at/above warn threshold but below the rotation backstop).
        if not (warn_at <= ctx_tokens < rotate_at):
            return
        # Anti-spam: only on the upward crossing (first turn in the warn zone).
        if k in context_warned:
            return
        context_warned.add(k)
        k_display = round(ctx_tokens / 1000)
        backstop_k = round(rotate_at / 1000)
        await send(
            context, chat, thread,
            md_to_html(
                f"⚠️ Контекст ~{k_display}K (бэкстоп {backstop_k}K). "
                f"Стоит /rotate или сжать следующий запрос, пока не влетел в жирный ход."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as warn_exc:
        print(f"[context-warn] TG-path warn failed: {warn_exc}")


async def run_agent(context, update, prompt: str):
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id or 0
    k = key_of(update)
    b = topics.get(k) or binding_for(update)
    cwd, model = b["cwd"], b.get("model", DEFAULT_MODEL)
    # slot already reserved in on_message (running[k]=True) — here we just do the work

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
    _tg_last_result_event: dict | None = None  # Phase D: track for auto-resume
    webapp._bus_publish(k, {"kind": "run_start", "source": "tg", "prompt": prompt, "run_id": None})
    try:
        # Project secrets (Spec 007) augment env; TG_CHAT_ID/TG_THREAD_ID take priority.
        # secret: references are resolved against the built-in store; TG vars are merged after so they always win.
        project_secrets = await webapp._resolve_secret_refs(webapp._secrets_read(cwd))
        agent_env = {**project_secrets, "TG_CHAT_ID": str(chat), "TG_THREAD_ID": str(thread or 0)}
        agents_config = b.get("agents_config") or {}
        agent_kwargs = _build_agents_kwargs(agents_config)
        # Spec-021 Phase 4: inject handoff summary into the first turn of a fresh session.
        # Only fires when there is no existing session (post-rotation) and a pending handoff exists.
        resume_sid = sessions.get(k)
        effective_prompt = prompt
        try:
            if resume_sid is None and k in pending_handoff:
                summary = pending_handoff.pop(k)
                effective_prompt = (
                    "<prior-session-summary>\n"
                    "The previous session was rotated to stay lean. Summary of where we left off below.\n"
                    "Continue this work if the new message relates to it; ignore this block if starting "
                    "something unrelated.\n\n"
                    f"{summary}\n"
                    "</prior-session-summary>\n\n"
                    f"{prompt}"
                )
                print(f"[rotation] injected handoff into first post-rotation turn for {k}")
        except Exception as _inj_exc:
            print(f"[rotation] handoff injection failed (continuing without it): {_inj_exc}")
            effective_prompt = prompt
        async for event in run_engine(
            project_name=b["project"],
            cwd=cwd,
            prompt=effective_prompt,
            session_key=k,
            model=model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": TELEGRAM_NUDGE,
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
                _tg_last_result_event = event  # Phase D: capture for auto-resume
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

            elif etype == "text_delta":
                pass  # TG adapter: ignore streaming deltas — final reply built from {type:"text"} blocks

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

    # Phase D: auto-resume if TG run was killed by rate-limit
    _tg_ctx = {
        "topics": topics,
        "sessions": sessions,
        "running": running,
        "rate_limits": rate_limits,
        "ptb_app": context.application if hasattr(context, "application") else None,
    }
    _resume_sid_tg = sessions.get(k)
    await webapp._maybe_auto_resume(
        ctx=_tg_ctx,
        session_key=k,
        original_prompt=prompt,
        last_result_event=_tg_last_result_event,
        resume_session_id=_resume_sid_tg,
    )

    # Spec-021: auto session rotation for the TG channel (after auto-resume check).
    await _maybe_rotate_tg(context, chat, thread, k, b, _tg_last_result_event)

    # Context early-warning: one-time TG alert when approaching the rotation backstop.
    # Fires only in the warn zone (>= CONTEXT_WARN_AT, < CONTEXT_ROTATE_AT); muted after rotation.
    await _maybe_warn_tg(context, chat, thread, k, _tg_last_result_event)


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


async def _drain_tg_queue(context, update) -> None:
    """After a turn finishes, pop and run the next queued message for this topic (if any).

    Called from safe_run.finally — AFTER running.pop(k) so the slot is free.
    Sends a status notice before starting the queued run so the operator sees it was dequeued.
    If the queue is empty, returns immediately (no-op).
    """
    k = key_of(update)
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
    if k in running:
        # Another message snuck in between pop and now — put the item back at the front.
        _TG_QUEUE.setdefault(k, []).insert(0, item)
        _tg_queue_flush()
        return
    running[k] = True
    try:
        await context.bot.send_chat_action(chat, ChatAction.TYPING, message_thread_id=thread or None)
    except Exception:
        pass
    asyncio.create_task(_safe_run_queued(context, update, item["prompt"]))


async def _safe_run_queued(context, update, prompt: str) -> None:
    """Runs a dequeued prompt through run_agent, then drains again (chain drain)."""
    chat = update.effective_chat.id
    thread = update.effective_message.message_thread_id
    k = key_of(update)
    try:
        await run_agent(context, update, prompt)
    except Exception as e:
        if "exit code 143" in str(e) or "exit code 137" in str(e):
            print(f"[safe_run_queued] CLI killed during shutdown, prompt={short(prompt, 60)}")
        else:
            await report_error(context, chat, thread, f"run_agent(queued) · {short(prompt, 60)}", e)
    finally:
        running.pop(k, None)
        await _drain_tg_queue(context, update)


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
    cleared = _tg_queue_clear(k)
    # Clear context-warn state so a fresh session can warn again.
    context_warned.discard(k)
    b = topics.get(k) or binding_for(update)
    proj = b["project"] if b else "—"
    queue_note = f" Queue cleared ({cleared} message(s))." if cleared else ""
    await send(context, update.effective_chat.id, update.effective_message.message_thread_id,
               f"🔄 Context reset. Project <b>{proj}</b> preserved.{queue_note}", parse_mode=ParseMode.HTML)


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
    k = key_of(update)
    binding = topics.get(k)
    if binding is None:
        await send(context, cid, tid,
                   "This topic is not bound to a project. Use /project <name> first.")
        return
    project = binding.get("project", "")
    try:
        fire_at, fire_on_reset = _parse_time_spec(time_spec)
    except ValueError as e:
        await send(context, cid, tid, f"Invalid time spec: {e}")
        return
    record = {
        "id": webapp._new_deferred_id(),
        "project": project,
        "session_key": k,
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
        # Spec-021 Phase 4: pending handoff summaries awaiting injection (shared with webapp via ctx)
        "pending_handoff": pending_handoff,
        # Context early-warn: tracks session keys that have already fired the CONTEXT_WARN_AT alert.
        # Shared by reference — webapp.py reads/writes it via ctx["context_warned"].
        "context_warned": context_warned,
        # Spec-028: persistent-client feature flag + registry (exported so webapp can read without
        # importing bot.py; webapp passes ctx to run_engine which reads these fields).
        "PERSISTENT_CLIENT": PERSISTENT_CLIENT,
        "live_clients": _live_clients,
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
