"""
engine.py — Transport-neutral engine block extracted from bot.py (spec-040 Phase B).

Contains: run_engine, DEFAULT_AGENTS, prompts, audit, live-client registry (spec-028),
reconcile_board (spec-034), _build_ctx, _graceful_shutdown, state dicts,
resolve_project/build_registry, key_of.

bot.py re-exports all engine symbols for backward compatibility.
webapp.py imports engine directly; it must NOT import bot.py.
"""
import asyncio
import dataclasses
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import AsyncGenerator

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolUseBlock,
    query as _sdk_query,
)
from claude_agent_sdk.types import HookContext, PostToolUseHookInput, PreCompactHookInput
from second_opinion import build_antigravity_server
import modules as _modules                # spec-065: module enable/disable registry
import browser_tools as _browser_tools    # spec-065: agent browser tools (built per-run)
from board import (
    board_summary,
    _load_board,
    _save_board,
    _get_board_lock,
    _tasks_path,
    _pop_card,
    _new_card_id,
    _count_potential_cards,
    BOARD_COLUMNS,
)

# ─────────────────────────── config ───────────────────────────
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

# Scratch dir for internal one-shot helper queries (reconciler, etc.) so their
# transcripts never appear in any project's session dropdown.
_OPS_SCRATCH_CWD = str(Path.home() / ".claude" / "ops-scratch")
Path(_OPS_SCRATCH_CWD).mkdir(parents=True, exist_ok=True)

DEFAULT_CWD = os.getenv("DEFAULT_CWD", str(Path.home()))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "fable")

MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}  # CLI resolves aliases to latest

# ─────────────────────────── sub-agent roster ───────────────────────────
# Default agents available to conductor sessions via the SDK Task tool.
# Models are configurable via env; Phase C will add per-project overrides.
_EXECUTOR_MODEL = os.getenv("EXECUTOR_MODEL", "sonnet")
_RESEARCHER_MODEL = os.getenv("RESEARCHER_MODEL", "sonnet")
_QUICK_MODEL = os.getenv("QUICK_MODEL", "haiku")

# Effort level for the conductor/main session.
# "medium" reduces rate-limit burn (thinking weighs ~5× in the window) vs the
# SDK default of "high". Gate behind env so operators can escalate without a
# code change. Valid values: low | medium | high | xhigh | max.
# Note: on Fable 5 thinking always runs high regardless; effort is silently
# ignored or coerced by the CLI for subscription models — no SDK error is raised.
_DEFAULT_EFFORT: str = os.getenv("DEFAULT_EFFORT", "high")

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

# spec-058: Ultracode mode directive. Appended to system_prompt["append"] (same mechanism as
# CONDUCTOR_PROMPT) when a chat run sets ultracode=True. Mode — not an effort level: it flips the
# default disposition to fan-out + adversarial verification AND pins effort to max (forced in
# run_engine). Kept as a module constant so it can be asserted in tests.
ULTRACODE_PROMPT = (
    "You are in Ultracode mode. Default to decomposing the task and running parallel work — spawn "
    "sub-agents (the Task tool) or author a Workflow — for any substantial task; favour "
    "comprehensiveness over brevity. Adversarially verify findings before acting on them. Operate "
    "at maximum effort; token cost is not the constraint. Stay solo only for trivial or purely "
    "conversational turns."
)


# spec-066: appended to system_prompt when the browser module is on, so the agent knows the
# live cockpit pane IS "the browser". Without this, asked to "open/launch the browser", an agent
# tends to spawn an external/headless browser (Playwright, Selenium) the operator can't see.
def _browser_prompt(backend: str, agent_actions: str) -> str:
    gate = (
        "You may navigate, read, click and type."
        if agent_actions == "full"
        else "Read-only mode: browser_navigate and browser_snapshot work; browser_click and "
        "browser_type are refused until the operator enables full actions in Extensions → Browser."
    )
    return (
        f"A live browser pane is active (the 'browser' module, backend: {backend}). When asked to "
        "open, launch, show or use 'the browser', or to open a URL or web page, drive THIS pane with "
        "the mcp__browser__ tools (browser_navigate, browser_snapshot, browser_click, browser_type) — "
        "the operator watches it live in the cockpit. Do NOT spawn an external or headless browser "
        f"(Playwright, Selenium, a subprocess) for this. {gate}"
    )


# Maximum TaskProgressMessage events forwarded to SSE per task (prevents flood on long runs).
MAX_SUBAGENT_PROGRESS = int(os.getenv("MAX_SUBAGENT_PROGRESS", "10"))

# Personalisation: set via env; neutral defaults work without .env for new users.
OPERATOR_NAME = os.getenv("OPERATOR_NAME", "the operator")
RESPONSE_LANGUAGE = os.getenv("RESPONSE_LANGUAGE", "")   # empty = no language directive

_lang_directive = f", answer in {RESPONSE_LANGUAGE}" if RESPONSE_LANGUAGE else ""

# spec-040 Phase 0: neutral default used by run_engine() when caller passes system_prompt=None.
# Transport-agnostic — no TG-specific formatting or channel assumptions.
DEFAULT_NUDGE = (
    "You are Claude Code running as an automated engineering assistant in the cockpit IDE. "
    "Follow the project CLAUDE.md and ~/CLAUDE.md (already loaded) — all working rules are there.\n"
    f"- No interactive dialogs: if you need clarification or a choice — ask as plain text at the "
    f"end of your reply and finish the turn; {OPERATOR_NAME} will reply in the next message.\n"
    f"- Reply concisely{_lang_directive}, in natural prose: what you did → what's next.\n"
    "- Key decisions / pitfalls / rejected approaches → write to `.claude-ops/memory/` (see project CLAUDE.md).\n"
    "- When presenting a small set of mutually-exclusive choices (2–6 options), you MAY end your "
    "message with a ```options fenced block (one choice per line) to render a clickable picker "
    "in the chat UI; otherwise reply normally."
)

# spec-038: appended to system_prompt when the cockpit media plumbing is active (COPS_MEDIA_DIR
# set — true for cockpit chat + card runs). The whole inline-image mechanism (cockpit-img helper,
# /media route, lightbox) shipped, but the agent was never TOLD it exists — so agents fall back to
# Telegram or paste a raw path/link (neither renders in the chat). This is the missing wire.
IMAGES_PROMPT = (
    f"To show {OPERATOR_NAME} an image, screenshot or video INSIDE the cockpit chat, run the helper "
    "`cockpit-img <path> [caption]` and paste the single `![…](…)` line it prints verbatim into your "
    "reply — the cockpit renders it inline (tap to zoom full-screen). For an image the operator "
    "already uploaded (it appears in the conversation as an `attached file: <path>` line), you may "
    "instead echo that exact `attached file: <path>` line on its own line. Do NOT deliver images via "
    "Telegram or by pasting a bare filesystem path or URL — those do not render here."
)

# AskUserQuestion = interactive prompt (no reply in TG -> agent hangs or decides on its own).
DISALLOWED_TOOLS = ["AskUserQuestion"]

# spec-060: optional Antigravity "second opinion" MCP tool, built once at import.
# None when SECOND_OPINION=0 or the agy binary is absent — then no tool is exposed and
# the engine behaves exactly as before. Building it does NOT invoke agy.
_ANTIGRAVITY_MCP = build_antigravity_server()
if _ANTIGRAVITY_MCP:
    print("[second_opinion] Antigravity MCP tool enabled (agy detected)")

# spec-034 L1: Board protocol block injected into system_prompt["append"] when TASKS.md exists.
# Verbatim from spec — the cockpit owns the workflow rules, not per-project CLAUDE.md.
BOARD_PROTOCOL = (
    "\n## Board protocol (this project has a kanban board — it is the source of truth)\n"
    "- A new task/bug/request → it belongs on the board. For multi-step work, record a card first, then do it.\n"
    "- The open cards below are the live state. Do not let work happen invisibly off the board.\n"
    "- The cockpit reconciles the board after each turn — you do not need to hand-edit TASKS.md.\n"
)

TOPICS_F = DATA / "topics.json"      # LAYER 1: thread -> project binding (persistent)
SESSIONS_F = DATA / "sessions.json"  # LAYER 2: thread -> session_id (cleared by /reset)
HANDOFF_F = DATA / "handoff.json"    # spec-042: pending handoff summaries (survive restarts)
USAGE_LEDGER_F = DATA / "usage_ledger.jsonl"  # cost ledger: one JSON row per completed turn (append-only)


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
# Functional alias (NOT personal): the "general" project → default cwd.
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


# ─────────────────────────── session-key constructor ───────────────────────────
def key_of(cwd: str) -> str:
    """Canonical session-key constructor: project slug derived from cwd.

    Phase 0 (spec-040): all new session keys go through here so the format is defined
    in one place. Designed for future multi-user extension — the signature stays
    ``key_of(cwd)`` and an optional ``user_id`` parameter can be added later to
    produce ``{user_id}:{slug}`` keys without touching every call site.

    Defined early (before state loading) because _migrate_session_keys calls it at
    module load time.
    """
    return Path(cwd.rstrip("/")).name


# ─────────────────────────── state ───────────────────────────
def _read(f, default):
    try:
        return json.loads(f.read_text())
    except Exception:
        return default


def _migrate_session_keys(
    topics_data: dict,
    sessions_data: dict,
) -> "tuple[dict, dict, int]":
    """spec-040 Phase 0: rename legacy ``chat:thread`` keys to slug-based keys.

    Rules:
    - Only keys whose format is ``<digits>:<digits-or-zero>`` (TG chat:thread) are
      migrated; ``free-*`` and ``glasses:*`` keys and any already-slug keys are left
      untouched (idempotent).
    - The slug is derived from the entry's ``cwd`` field via ``key_of(cwd)``.
    - Entries without a ``cwd`` are skipped with a warning (kept under old key).
    - Slug collisions (two TG keys mapping to the same slug) keep the FIRST entry
      encountered; the duplicate is skipped and a warning is printed.
    - Migrated topic entries get a ``"tg_key"`` field added — stores the original
      ``chat:thread`` string so that ``binding_for()`` can still route TG messages
      to the correct project after migration.  Removed in Phase D.
    - sessions_data values (session_id strings) are preserved verbatim so SDK resume
      keeps working.
    - Repeated calls are no-ops (keys no longer match the TG pattern after migration).

    Returns ``(new_topics, new_sessions, migrated_count)``.
    """
    import re as _re
    _tg_key_pat = _re.compile(r"^-?\d+:\d+$")

    new_topics: dict = {}
    new_sessions: dict = {}
    migrated = 0

    # --- topics ---
    for k, v in topics_data.items():
        if not _tg_key_pat.match(k):
            # Already neutral key (slug / free-* / glasses:* / etc.) — keep as-is.
            if k in new_topics:
                print(f"[migrate] WARNING: duplicate neutral key {k!r} in topics — keeping first")
            else:
                new_topics[k] = v
            continue

        cwd = v.get("cwd", "")
        if not cwd:
            print(f"[migrate] WARNING: topics key {k!r} has no cwd — skipping")
            new_topics[k] = v  # keep under old key rather than lose the entry
            continue

        slug = key_of(cwd)
        if slug in new_topics:
            print(f"[migrate] WARNING: slug collision {slug!r} "
                  f"(from {k!r} cwd={cwd!r}) — keeping existing entry, skipping duplicate")
            continue

        # Store original TG key in the value so binding_for() can reverse-lookup after
        # migration.  This field is removed in Phase D when TG is fully deleted.
        entry = dict(v)
        entry["tg_key"] = k
        new_topics[slug] = entry
        migrated += 1

    # --- sessions ---
    # Build a reverse map: old TG key -> slug (from topics migration above).
    old_to_slug: dict[str, str] = {}
    for k, v in topics_data.items():
        if _tg_key_pat.match(k):
            cwd = v.get("cwd", "")
            if cwd:
                old_to_slug[k] = key_of(cwd)

    for k, session_id in sessions_data.items():
        if not _tg_key_pat.match(k):
            new_sessions[k] = session_id
            continue

        slug = old_to_slug.get(k)
        if slug is None:
            # Session key has no matching topic — keep under old key to preserve session_id.
            print(f"[migrate] WARNING: sessions key {k!r} has no matching topic entry — "
                  f"keeping under old key")
            new_sessions[k] = session_id
            continue

        if slug in new_sessions:
            print(f"[migrate] WARNING: slug collision {slug!r} in sessions — keeping existing")
            continue

        new_sessions[slug] = session_id

    return new_topics, new_sessions, migrated


def _run_startup_migration() -> None:
    """spec-040 Phase 0: run session-key migration exactly once at service startup.

    Called from _amain() before building ctx or starting any server.
    Must NOT be called at import time — doing so would mutate data/*.json as a side-effect
    of ``import bot`` in tests, corrupting production data files.

    Mutates the module-level dicts in-place so all importers that hold references
    to the same objects (via ``from engine import topics``) see the updated state.
    """
    new_t, new_s, n = _migrate_session_keys(topics, sessions)
    if n:
        topics.clear()
        topics.update(new_t)
        sessions.clear()
        sessions.update(new_s)
        TOPICS_F.write_text(json.dumps(topics, ensure_ascii=False, indent=2))
        SESSIONS_F.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))
        print(f"[migrate] Phase 0: migrated {n} session key(s) to slug format")


topics = _read(TOPICS_F, {})       # slug -> {project, cwd, model}
sessions = _read(SESSIONS_F, {})   # slug -> session_id
# NOTE: migration of legacy chat:thread keys is NOT done here (import-time side-effect).
# It runs in _run_startup_migration(), called from _amain() before serving requests.
costs = {}                         # session_key -> last cost usd
running = {}                       # session_key -> ClaudeSDKClient (for /stop)
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
# Spec-021 Phase 4 / spec-042: one-shot handoff summaries pending injection into the next turn after rotation.
# {session_key: summary_text}. Cleared immediately after injection so it fires exactly once.
# spec-042: persisted to HANDOFF_F (data/handoff.json) so summaries survive service restarts.
pending_handoff: "dict[str, str]" = _read(HANDOFF_F, {})
# Context early-warn: tracks session keys that have already received the CONTEXT_WARN_AT alert.
# Cleared on /reset so a fresh session can warn again.
context_warned: "set[str]" = set()


def save_topics():
    TOPICS_F.write_text(json.dumps(topics, ensure_ascii=False, indent=2))


def save_sessions():
    SESSIONS_F.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))


def save_handoff():
    """Persist pending_handoff to disk (spec-042). Called after rotation stores or injection pops an entry."""
    HANDOFF_F.write_text(json.dumps(pending_handoff, ensure_ascii=False, indent=2))


def append_usage_ledger(record: dict) -> None:
    """Append one per-turn usage row to the on-disk cost ledger (JSONL, append-only).

    Turns the per-turn cost/token facts the SDK already gives us (which until now lived only
    in RAM and vanished on restart) into durable history, so "Cardloop vs CLI / ultracode share"
    becomes a query instead of a feeling.  One write() of a <4 KB line under O_APPEND is atomic
    on POSIX, and the async loop is single-threaded, so concurrent turns can't interleave a line.
    Best-effort: any failure is swallowed so the ledger can NEVER break a turn."""
    try:
        with USAGE_LEDGER_F.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[ledger] append failed (non-blocking): {exc!r}")


def short(cmd: str, limit=90) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= limit else cmd[:limit] + "…"


# ─────────────────────────── audit + watchdog ───────────────────────────
AUDIT_DIR = DATA / "audit"
STALL_SECONDS = int(os.getenv("STALL_SECONDS", "300"))   # kept for settings UI; stall interrupt removed (spec-039)
MAX_SECONDS = int(os.getenv("MAX_SECONDS", "7200"))      # absolute turn ceiling (2 h) — spec-039

# ── Spec-028: persistent (long-lived) client feature flag ─────────────────────────────────────
# PERSISTENT_CLIENT=0 (default OFF) → behaviour is byte-identical to pre-028; all existing tests pass.
# PERSISTENT_CLIENT=1 → run_engine reuses the same ClaudeSDKClient across turns for non-ephemeral
# sessions (chat / deferred), skipping per-turn connect/disconnect overhead.
PERSISTENT_CLIENT: bool = os.getenv("PERSISTENT_CLIENT", "0") == "1"
# Max idle seconds before an unused live client is evicted (disconnected) automatically.
LIVE_CLIENT_TTL_SEC: int = int(os.getenv("LIVE_CLIENT_TTL_SEC", "600"))
# Max number of concurrent live clients held in the registry; LRU eviction beyond this.
LIVE_CLIENT_MAX: int = int(os.getenv("LIVE_CLIENT_MAX", "10"))
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


# ─────────────────────────── webapp callback registry ─────────────────────────────────────────
# Injected at startup via _register_webapp_callbacks() to avoid circular import.
# None until webapp is initialised (tests / import-time calls are safe no-ops).
_timeline_append_cb = None
_bus_publish_cb = None
_monitor_update_cb = None


def _register_webapp_callbacks(timeline_append, bus_publish, monitor_update=None):
    """Inject webapp callbacks so engine.py can publish events without importing webapp."""
    global _timeline_append_cb, _bus_publish_cb, _monitor_update_cb
    _timeline_append_cb = timeline_append
    _bus_publish_cb = bus_publish
    _monitor_update_cb = monitor_update


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


# ─────────────────────────── Background-task monitors (card b6f5cc) ────────────────────────────
#
# Claude Code agents can start long-running "service monitors" that survive a single turn:
#   - background Bash shells     — Bash(run_in_background=True), polled via BashOutput, KillShell
#   - Monitor / Workflow tasks   — run until TaskStop or session end (the literal "monitor" tools)
# In the terminal client these appear in a tasks panel.  We surface the same in the cockpit by
# reading their lifecycle out of the PostToolUse stream — no extra SDK plumbing needed.
#
# _monitor_delta() is a PURE function: given one tool result it returns a partial monitor record
# (or None).  webapp._monitor_update() owns the registry + timestamps + live bus fan-out.

def _rget(obj, key, default=None):
    """Read a key from a tool_response that may be a dict OR an attribute-style object."""
    try:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)
    except Exception:
        return default


_MONITOR_TAIL_MAX = 2000  # chars — keep the END of the output (it's a tail)


def _monitor_tail(tr) -> str:
    """Extract a clean, multi-line output tail from a tool_response for the monitor panel.

    Unlike _tool_response_to_str (single-line audit, repr() fallback), this preserves newlines
    (rendered in <pre>) and returns "" — not an ugly dict repr — when there is no output yet."""
    try:
        if isinstance(tr, dict):
            out = str(tr.get("stdout", "") or "")
            err = str(tr.get("stderr", "") or "")
            parts = []
            if out:
                parts.append(out)
            if err:
                parts.append(f"[stderr] {err}")
            s = "\n".join(parts)
        else:
            s = str(tr or "")
    except Exception:
        return ""
    s = s.strip()
    if len(s) > _MONITOR_TAIL_MAX:
        s = "…" + s[-_MONITOR_TAIL_MAX:]
    return s


def _monitor_delta(tool_name, tool_input, tool_response, agent_type):
    """Map a single tool result to a background-monitor delta, or None if irrelevant.

    Returned dict always carries "id"; first-seen deltas also carry kind/label/status.
    Never raises — the caller is on the hot path."""
    try:
        ti = tool_input if isinstance(tool_input, dict) else {}
        tr = tool_response

        if tool_name == "Bash" and ti.get("run_in_background"):
            bid = _rget(tr, "backgroundTaskId")
            if not bid:
                return None
            return {"id": str(bid), "kind": "bash", "status": "running",
                    "label": str(ti.get("command") or "")[:200],
                    "tail": _monitor_tail(tr), "agent": agent_type}

        if tool_name == "Monitor":
            tid = _rget(tr, "taskId")
            if not tid:
                return None
            label = ti.get("description") or ti.get("prompt") or ti.get("command") or "monitor"
            return {"id": str(tid), "kind": "monitor", "status": "running",
                    "label": str(label)[:200], "persistent": bool(_rget(tr, "persistent")),
                    "agent": agent_type}

        if tool_name == "Workflow":
            tid = _rget(tr, "taskId")
            if not tid:
                return None
            return {"id": str(tid), "kind": "workflow", "status": "running",
                    "label": str(_rget(tr, "workflowName") or ti.get("name") or "workflow")[:200],
                    "agent": agent_type}

        if tool_name == "BashOutput":
            bid = ti.get("bash_id") or _rget(tr, "backgroundTaskId")
            if not bid:
                return None
            # backgroundTaskId is present in the response only WHILE the command runs; its
            # absence on a poll means the shell has finished.  Otherwise keep status as-is
            # (long-running by nature) and just refresh the output tail.
            d = {"id": str(bid), "tail": _monitor_tail(tr)}
            if isinstance(tr, (dict,)) or hasattr(tr, "backgroundTaskId"):
                if not _rget(tr, "backgroundTaskId"):
                    d["status"] = "done"
            return d

        if tool_name in ("TaskOutput", "TaskGet"):
            tid = ti.get("task_id") or ti.get("taskId")
            if not tid:
                return None
            return {"id": str(tid), "tail": _monitor_tail(tr)}

        if tool_name == "KillShell":
            sid = ti.get("shell_id")
            return {"id": str(sid), "status": "stopped"} if sid else None

        if tool_name == "TaskStop":
            tid = ti.get("task_id") or ti.get("taskId")
            return {"id": str(tid), "status": "stopped"} if tid else None
    except Exception:
        return None
    return None


def _make_post_tool_use_hook(project_name: str, session_key: str):
    """Return an async HookCallback that records tool output in the audit log and timeline.

    Closes over `project_name` and `session_key` so the hook can route audit lines to the
    correct project without receiving env or secrets.  Uses _timeline_append_cb (injected
    at startup via _register_webapp_callbacks) for timeline publishing.
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
            tool_input = hook_input.get("tool_input") if isinstance(hook_input, dict) else getattr(hook_input, "tool_input", None)
            agent_type = hook_input.get("agent_type") if isinstance(hook_input, dict) else getattr(hook_input, "agent_type", None)

            output_str = _tool_response_to_str(tool_response)

            # Background-task monitors (card b6f5cc): surface long-running shells / monitor tasks.
            try:
                if _monitor_update_cb:
                    delta = _monitor_delta(tool_name, tool_input, tool_response, agent_type)
                    if delta:
                        _monitor_update_cb(session_key, delta)
            except Exception:
                pass  # monitor tracking is best-effort — never break a turn

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
                if _timeline_append_cb:
                    _timeline_append_cb(session_key, {
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


# ─────────────────────────── Spec-039: PreCompact observe hook ─────────────────────────────────

def _make_pre_compact_hook(project_name: str, session_key: str):
    """Return an async HookCallback that emits an audit line + bus/SSE event when native
    auto-compact fires inside a long-lived ClaudeSDKClient (PERSISTENT_CLIENT=1).

    Observe-only: the hook returns an empty dict, which is a valid SyncHookJSONOutput and
    does NOT block or alter the compaction.  A crash inside the hook is silenced so it
    never breaks a turn.
    """
    async def _pre_compact_hook(
        hook_input: "PreCompactHookInput",
        tool_use_id: "str | None",
        context: "HookContext",
    ) -> dict:
        """Record native auto-compact to audit log and cockpit activity bus. Never raises."""
        try:
            trigger = (
                hook_input.get("trigger", "auto")
                if isinstance(hook_input, dict)
                else getattr(hook_input, "trigger", "auto")
            )
            audit(project_name, "COMPACT", f"native auto-compact trigger={trigger}")

            # Publish to the cockpit activity bus / SSE so the UI can show a toast.
            try:
                if _bus_publish_cb:
                    _bus_publish_cb(session_key, {
                        "kind": "compact",
                        "trigger": trigger,
                        "project": project_name,
                    })
            except Exception:
                pass  # webapp not initialised or publish error — never break a turn
        except Exception:
            pass  # entire hook body is guarded — never propagate to the SDK

        return {}  # empty SyncHookJSONOutput — observe-only, no model-visible side-effects

    return _pre_compact_hook


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
    - The call site explicitly requests ephemeral isolation (_run_card)
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


# ─────────────────────────── Board context helpers (spec-034 L1) ─────────────────────────
#
# _build_board_append: builds the board protocol block + current open-card snapshot.
# Factored out so it can be unit-tested without instantiating run_engine.


def _build_board_append(cwd: str) -> str:
    """Return the board protocol + open-card snapshot to append to system_prompt.

    Returns '' when TASKS.md does not exist in cwd (guard: inject nothing).
    The returned string is ready to concatenate with a newline separator.
    """
    summary = board_summary(cwd)
    if not summary:
        # board_summary returns '' when TASKS.md does not exist
        return ""
    return BOARD_PROTOCOL + "\n" + summary + "\n"


# ─────────────────────────── Board reconciler (spec-034 L2) ──────────────────────────
#
# reconcile_board: background task fired after every chat turn.
# Makes ONE haiku one-shot (no tools) to extract board ops from the completed turn.
# Applied under the per-cwd board lock via board.py primitives.
# Safety: no delete, cap 5 ops/turn, JSON fail = no-op, BOARD_RECONCILE gate.

_RECONCILE_OPS_CAP = 5

# System prompt for the haiku reconciler — tells the model exactly what to produce.
_RECONCILE_SYSTEM = (
    "You are a board reconciliation assistant. Given a user message, an agent reply, "
    "and the current open board cards, you output ONLY a JSON array of board operations. "
    "Nothing else — no prose, no markdown fences, just the raw JSON array.\n\n"
    "Allowed operations:\n"
    '  {"op":"create","text":"short card title","column":"review|backlog","description":"optional detail"}\n'
    '  {"op":"move","id":"card-id","to":"review|done|in_progress"}\n\n'
    "Rules:\n"
    "- Output [] (empty array) if the turn was a question, clarification, or general chat.\n"
    "- Output [] if all mentioned work already has a matching open card.\n"
    "- Use 'create' only when work was done or requested that has NO matching open card.\n"
    "- Use 'move' to mark a card done (to=done) or move to review if work just completed.\n"
    "- Default column for new work just done this turn: 'review'. For future work: 'backlog'.\n"
    "- Never suggest deleting a card. Max 5 operations total.\n"
    "- Before creating a card, check the open cards list — reuse an existing card (move) "
    "rather than creating a duplicate.\n"
    "- Keep titles short (under 80 chars)."
)


def _norm_title(text: str) -> str:
    """Normalise a card title for deduplication (lowercase, strip punctuation/spaces)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


async def _apply_reconcile_ops(cwd: str, name: str, ops: list, on_match: str = "done",
                               session_key: str = "") -> None:
    """Apply a list of parsed reconcile ops under the board lock.

    Safety: no delete, cap 5, skip invalid. Audit-logs each applied op.

    on_match: "done" → auto-archive cards moved to done column (default);
              "review" → remap done→review so operator closes manually.

    spec-052 Phase 2: when session_key is set, each applied op is surfaced in
    that project's chat as a board_event (kind="reconcile") AFTER the board write
    succeeds — so ops rolled back by the data-loss guard are never announced.
    """
    if not ops:
        return

    lock = _get_board_lock(cwd)
    async with lock:
        raw, preamble, cols = _load_board(cwd)
        pre_count = _count_potential_cards(raw)

        applied = 0
        pending_events: list = []  # spec-052: board_events to fire after a successful write
        touched_specs: set = set()  # spec-052 P5/6: spec ids of moved linked cards
        for op in ops[:_RECONCILE_OPS_CAP]:
            if not isinstance(op, dict):
                continue
            op_type = op.get("op")

            if op_type == "create":
                text = (op.get("text") or "").strip()
                if not text:
                    continue
                column = op.get("column") or "backlog"
                if column not in ("backlog", "in_progress", "review"):
                    column = "backlog"
                description = op.get("description") or None

                # Dedupe: skip if normalised title matches any open card
                norm = _norm_title(text)
                open_cols = ("backlog", "in_progress", "review")
                existing_norms = {
                    _norm_title(c["text"])
                    for col_key in open_cols
                    for c in cols.get(col_key, [])
                }
                if norm in existing_norms:
                    print(f"[reconcile] skip create (duplicate): {text!r}")
                    continue

                card_id = _new_card_id()
                card: dict = {"id": card_id, "text": text}
                if description:
                    card["description"] = description
                cols[column].append(card)
                print(f"[reconcile] create card {card_id!r} in {column!r}: {text!r}")
                pending_events.append({
                    "event": "reconcile", "card_id": card_id, "title": text,
                    "column_from": None, "column_to": column, "severity": "info",
                    "summary": f"auto-created in {column}",
                })
                applied += 1

            elif op_type == "move":
                card_id = (op.get("id") or "").strip()
                to_col = op.get("to") or ""
                if not card_id or not to_col:
                    continue
                if to_col not in ("backlog", "in_progress", "review", "done"):
                    print(f"[reconcile] skip move — unknown target column {to_col!r}")
                    continue

                card = _pop_card(cols, card_id)
                if card is None:
                    print(f"[reconcile] skip move — card {card_id!r} not found")
                    continue
                if card.get("spec"):
                    touched_specs.add(card["spec"])

                # Policy remap: when on_match=="review", redirect done→review
                # so the operator closes cards manually instead of auto-archiving.
                if to_col == "done" and on_match == "review":
                    to_col = "review"
                    print(f"[reconcile] policy remap: done→review for card {card_id!r}")

                if to_col == "done":
                    # Write to DONE.md (append-only archive) via the shared helper
                    # so the format (incl. ops:id) stays consistent with the cockpit.
                    from board import _done_path, _done_archive_line  # noqa: F401
                    done_p = _done_path(cwd)
                    with open(done_p, "a", encoding="utf-8") as df:
                        df.write(_done_archive_line(card))
                    print(f"[reconcile] move card {card_id!r} → done (archived)")
                else:
                    cols[to_col].append(card)
                    print(f"[reconcile] move card {card_id!r} → {to_col!r}")
                pending_events.append({
                    "event": "reconcile", "card_id": card_id, "title": card.get("text", ""),
                    "column_from": None, "column_to": to_col,
                    "severity": "success" if to_col == "done" else "info",
                    "summary": f"auto-closed" if to_col == "done" else f"auto-moved to {to_col}",
                })
                applied += 1

            if applied >= _RECONCILE_OPS_CAP:
                break

        if applied == 0:
            return  # nothing to write

        # Data-loss guard: skip write if parsed card count dropped (indicates parser fault)
        new_raw_test = ""
        try:
            from board import _serialize_tasks  # noqa: F401
            from board import _serialize_tasks as _st
            new_raw_test = _st(preamble, cols, name)
            new_count = _count_potential_cards(new_raw_test)
            if new_count < pre_count - _RECONCILE_OPS_CAP:
                print(
                    f"[reconcile] data-loss guard: card count dropped "
                    f"{pre_count} → {new_count}, aborting write"
                )
                return
        except Exception as _guard_exc:
            print(f"[reconcile] data-loss guard check failed: {_guard_exc}, aborting write")
            return

        _save_board(cwd, name, preamble, cols)

    # spec-052 Phase 2: announce the applied ops in the project chat (outside the
    # board lock; only reached when the write above succeeded).
    if session_key and _bus_publish_cb and pending_events:
        for _ev in pending_events:
            try:
                _bus_publish_cb(session_key, {"kind": "board_event", "ts": time.time(), **_ev})
            except Exception:
                pass  # never let a notification break reconcile

    # spec-052 P5/6: regenerate the ## Tasks mirror for each spec whose card moved,
    # and announce a spec that just reached all-cards-done (auto-close).
    if touched_specs:
        try:
            from spec_mirror import sync_spec_mirror
        except Exception:
            sync_spec_mirror = None
        for _sid in touched_specs:
            if sync_spec_mirror is None:
                break
            try:
                _res = sync_spec_mirror(cwd, _sid)
            except Exception as _mx:
                print(f"[spec-mirror] sync failed for spec {_sid}: {_mx}")
                continue
            if _res and _res.get("newly_closed") and session_key and _bus_publish_cb:
                try:
                    _bus_publish_cb(session_key, {
                        "kind": "board_event", "event": "reconcile",
                        "card_id": f"spec-{_sid}", "title": f"spec-{_sid} complete",
                        "column_from": None, "column_to": None, "severity": "success",
                        "summary": f"All {_res['total']} cards done — spec auto-closed",
                        "ts": time.time(),
                    })
                except Exception:
                    pass


async def reconcile_board(
    cwd: str,
    name: str,
    user_msg: str,
    agent_summary: str,
    session_key: str = "",
) -> None:
    """Background board reconciler — fires after every chat turn.

    Makes ONE haiku one-shot call (no tools) to extract board ops.
    Applied under board lock. Never blocks the operator's reply (caller must
    asyncio.create_task this coroutine).

    Gates:
    - BOARD_RECONCILE env != "1" → skip entirely (no-op)
    - TASKS.md not present in cwd → skip
    - JSON parse failure → no-op (no board change)
    """
    # Gate: settings.json flag (overrides env when explicitly set).
    # Falls back to env BOARD_RECONCILE if the setting is unset or unreadable.
    # Lazy import: by the time reconcile_board is called, webapp is fully loaded.
    try:
        import webapp as _wa
        _reconcile_enabled = _wa._get_global_setting("board_reconcile_enabled", None)
    except Exception:
        _reconcile_enabled = None

    if _reconcile_enabled is False:
        # Operator explicitly disabled the reconciler via UI.
        return
    if _reconcile_enabled is None:
        # Setting unset → fall back to env gate (original behavior).
        if os.environ.get("BOARD_RECONCILE", "1") not in ("1", "true", "True"):
            return

    # Gate: TASKS.md must exist
    if not _tasks_path(cwd).exists():
        return

    # Build the current board snapshot for the reconciler
    summary = board_summary(cwd)

    reconcile_model = os.environ.get("BOARD_RECONCILE_MODEL", "haiku")

    # Build the user-facing prompt for haiku
    prompt_parts = [
        "## User message",
        user_msg[:2000] if user_msg else "(none)",
        "",
        "## Agent reply",
        agent_summary[:3000] if agent_summary else "(none)",
        "",
        "## Open board cards",
        summary if summary else "Board is empty.",
        "",
        "Output ONLY a JSON array of operations (or [] for none).",
    ]
    reconcile_prompt = "\n".join(prompt_parts)

    opts = ClaudeAgentOptions(
        model=reconcile_model,
        permission_mode="bypassPermissions",
        cwd=_OPS_SCRATCH_CWD,  # scratch dir: transcript never pollutes project session list
        system_prompt=_RECONCILE_SYSTEM,  # plain string — no tools, no preset
        allowed_tools=[],   # no tools — read-only classification pass
        disallowed_tools=[],
        effort="low",
    )

    # Collect haiku response.
    # _sdk_query (= claude_agent_sdk.query) is an async generator function — iterate directly,
    # do NOT await it first (that would raise TypeError for async generators).
    text_parts: list[str] = []
    try:
        async for msg in _sdk_query(prompt=reconcile_prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock) and blk.text.strip():
                        text_parts.append(blk.text)
    except Exception as exc:
        print(f"[reconcile] haiku call failed: {exc!r}")
        return

    raw_response = "\n".join(text_parts).strip()
    if not raw_response:
        return

    # Parse JSON — on failure, no-op
    try:
        ops = json.loads(raw_response)
        if not isinstance(ops, list):
            print(f"[reconcile] unexpected JSON (not a list): {raw_response[:200]!r}")
            return
    except json.JSONDecodeError as exc:
        # Try extracting a JSON array from prose (model sometimes wraps in markdown)
        m = re.search(r"\[.*\]", raw_response, re.DOTALL)
        if m:
            try:
                ops = json.loads(m.group(0))
            except json.JSONDecodeError:
                print(f"[reconcile] JSON parse failed: {exc!r} — no-op")
                return
        else:
            print(f"[reconcile] JSON parse failed: {exc!r} — no-op")
            return

    if not ops:
        return  # empty list → nothing to do

    # Read the on_match policy from settings (hot-read, no cache issue).
    try:
        import webapp as _wa  # noqa: F811 — already imported above in this function scope
        _on_match = _wa._get_global_setting("board_reconcile_on_match", "done") or "done"
    except Exception:
        _on_match = "done"

    await _apply_reconcile_ops(cwd, name, ops, on_match=_on_match, session_key=session_key)


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
    effort: "str | None" = None,
    ultracode: bool = False,
    entrypoint: str = "chat",
    disallowed_tools_extra: "list | None" = None,
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
                                 reuse). Set by _run_card which must be fully isolated from
                                 shared sessions.
        output_format         — Spec-029 item 3: optional JSON-schema dict for structured output.
                                 When provided, ClaudeAgentOptions.output_format is set and
                                 ResultMessage.structured_output is passed through the result event.
                                 Shape: {"type": "json_schema", "schema": {...}}.
                                 None (default) → no change to existing behaviour (chat/TG runs).
        effort                — thinking effort override for this run. None (default) → uses
                                 _DEFAULT_EFFORT (env DEFAULT_EFFORT, default "medium"). Pass an
                                 explicit value ("low", "medium", "high") to override per-request.
                                 Note: on Fable 5 effort is silently coerced by the CLI regardless.
        ultracode             — spec-058: when True, append ULTRACODE_PROMPT to the system prompt
                                 (fan-out + adversarial-verify disposition) AND pin effort to "max"
                                 (overriding the effort arg). False (default) → no change.
        entrypoint            — cost-ledger attribution tag for the on-disk usage ledger:
                                 "chat" (interactive cockpit, default), "card" (kanban auto-run),
                                 "deferred" (post-reset deferred run). Recorded per turn; does not
                                 affect execution.

    Yields event dicts. SDK exceptions are wrapped as {"type": "error", "exc": ...}.
    """
    if system_prompt is None:
        # spec-040: transport-neutral DEFAULT_NUDGE (cockpit + kanban auto-run).
        # Callers (TG adapter, cockpit) may pass an explicit system_prompt to override.
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": DEFAULT_NUDGE,
            "exclude_dynamic_sections": True,
        }

    resolved_model = MODELS.get(model, model) if model else MODELS.get(DEFAULT_MODEL, DEFAULT_MODEL)

    # Conductor directive: inject when using fable as orchestrator model (unless disabled per-project).
    if not skip_conductor_prompt and resolved_model and resolved_model.startswith("fable"):
        existing_append = system_prompt.get("append") or ""
        sep = "\n" if existing_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = existing_append + sep + CONDUCTOR_PROMPT

    # spec-034 L1: Board-aware context injection — append board protocol + open-card snapshot
    # when cwd contains TASKS.md. Guard: _build_board_append returns '' if TASKS.md absent.
    _board_block = _build_board_append(cwd)
    if _board_block:
        existing_append = system_prompt.get("append") or ""
        sep = "\n" if existing_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = existing_append + sep + _board_block

    # spec-058: Ultracode mode — append the fan-out/verify directive (same mechanism as the
    # conductor/board blocks). Effort is pinned to "max" below, regardless of the effort arg.
    if ultracode:
        existing_append = system_prompt.get("append") or ""
        sep = "\n" if existing_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = existing_append + sep + ULTRACODE_PROMPT

    # Sub-agent roster: use provided agents or fall back to the default roster.
    effective_agents = agents if agents is not None else DEFAULT_AGENTS

    # Fallback model: if fable is unavailable at runtime, degrade to opus silently.
    fallback = "opus" if resolved_model and resolved_model.startswith("fable") else None

    # Spec-029 §2: PostToolUse hook — records tool output to audit log + timeline.
    _post_tool_hook = _make_post_tool_use_hook(project_name, session_key)

    # Spec-039: PreCompact hook — observe-only; emits audit line + bus event when native
    # auto-compact fires inside a long-lived client (PERSISTENT_CLIENT=1).  Safe no-op when
    # flag is OFF because the hook only fires if a PreCompact SDK event is emitted.
    _pre_compact_hook = _make_pre_compact_hook(project_name, session_key)

    # Spec-029 §1: live streaming — emit text_delta events for incremental cockpit display.
    # STREAM_PARTIAL=0 disables without code changes (e.g. for debugging or regression isolation).
    # Default ON: clean reconciliation (the final {type:"text"} remains authoritative, deltas are
    # preview-only; no double-render because the frontend replaces accumulated delta text on receipt
    # of the finalized block via finalizeStreamingWithMetrics).
    _stream_partial = os.environ.get("STREAM_PARTIAL", "1") not in ("0", "false", "False")

    # spec-058: ultracode pins effort to "max" (overrides the per-request effort arg).
    # Captured once so the cost ledger records the SAME effort the SDK actually ran.
    _eff_effort = "max" if ultracode else (effort if effort is not None else _DEFAULT_EFFORT)

    print(f"[session] resume {session_key} sid={resume_session_id or 'NEW'}")
    # spec-065 Phase C: expose live-browser tools only when the browser module is on.
    # Built per-run with this run's cwd bound, so the agent drives the SAME browser the
    # operator watches in the cockpit pane (browser_pane keys sessions by cwd).
    _mcp_servers = dict(_ANTIGRAVITY_MCP or {})
    try:
        if _modules.is_enabled("browser"):
            # spec-066: gate mutating browser tools by the per-cwd agent_actions setting.
            try:
                import browser_backends as _browser_backends
                _bspec = _browser_backends.resolve(cwd)
                _agent_actions = _bspec.get("agent_actions", "read")
                _browser_backend = _bspec.get("backend", "builtin")
            except Exception:
                _agent_actions, _browser_backend = "read", "builtin"
            _mcp_servers.update(_browser_tools.build_browser_server(cwd, _agent_actions))
            # Tell the agent the live pane IS "the browser" (don't spawn an external one).
            _existing_append = system_prompt.get("append") or ""
            _sep = "\n" if _existing_append else ""
            system_prompt = dict(system_prompt)
            system_prompt["append"] = _existing_append + _sep + _browser_prompt(_browser_backend, _agent_actions)
    except Exception as _browser_mcp_exc:
        print(f"[browser] MCP wiring skipped: {_browser_mcp_exc!r}")
    # spec-038: tell the agent how to surface an image inline. Gate on the media env actually being
    # present (cockpit chat + card runs set it) so the hint only appears when the plumbing is live.
    if (env or {}).get("COPS_MEDIA_DIR"):
        _img_append = system_prompt.get("append") or ""
        _img_sep = "\n" if _img_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = _img_append + _img_sep + IMAGES_PROMPT
    opts = ClaudeAgentOptions(
        model=resolved_model,
        fallback_model=fallback,
        permission_mode="bypassPermissions",
        cwd=cwd,
        setting_sources=["user", "project", "local"],
        resume=resume_session_id,
        disallowed_tools=list(DISALLOWED_TOOLS) + list(disallowed_tools_extra or []),
        system_prompt=system_prompt,
        env=env or {},
        mcp_servers=_mcp_servers,
        agents=effective_agents,
        effort=_eff_effort,  # type: ignore[arg-type]
        hooks={
            "PostToolUse": [HookMatcher(hooks=[_post_tool_hook])],
            "PreCompact": [HookMatcher(hooks=[_pre_compact_hook])],
        },
        # include_hook_events=False (default) — HookEventMessage lifecycle noise adds no extra
        # data beyond what the hook callback already captures, and would flood _process_messages.
        include_partial_messages=_stream_partial,
        # Spec-029 item 3: structured output for card runs. None → no change (chat/TG paths).
        output_format=output_format,
    )

    audit(project_name, "TASK", short(prompt, 300))

    # Spec-028 Phase 1: resolve which running-dict to use.
    # ctx is provided by call sites that have a context dict (run_agent, api_project_chat,
    # _execute_deferred).  Legacy / ctx-less callers (tests) fall back to the module-global
    # `running` dict — behaviour is identical to pre-028.
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
        # Spec-043 C: track the max pt seen across all AssistantMessages in this turn
        # where a usage object is actually present (not None).  Using MAX rather than
        # last-seen protects against intermediate tool-use AssistantMessages that may
        # carry a usage dict with partial/0 values before the final message arrives
        # with the real full-context count.
        # Distinguishing "usage present = 0" (write 0) from "no usage on message"
        # (skip) ensures a turn where the SDK omits usage entirely does NOT silently
        # carry forward the previous turn's stale value.
        _turn_max_pt: "int | None" = None  # None = no usage-bearing message seen yet this turn
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                # usage of the last assistant message = full prompt of the current turn:
                # input + cache_read + cache_creation == get_context_usage().totalTokens (verified)
                _raw_usage = getattr(msg, "usage", None)
                # Normalise: dict → use as-is; truthy non-dict (rare) → {}; None → sentinel.
                u: "dict | None" = _raw_usage if isinstance(_raw_usage, dict) else (
                    {} if _raw_usage is not None else None
                )
                if u is not None:
                    # Usage object IS present on this message (even if all counts are 0).
                    # Track the maximum so the final full-context message wins over any
                    # preceding partial/zero values from intermediate tool-use messages.
                    pt = (u.get("input_tokens", 0)
                          + u.get("cache_read_input_tokens", 0)
                          + u.get("cache_creation_input_tokens", 0))
                    _turn_max_pt = pt if _turn_max_pt is None else max(_turn_max_pt, pt)
                    last_usage = u  # capture for the result event (last-seen wins for cost math)
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
                # Spec-043 C: commit the best (max) pt seen from AssistantMessages this turn.
                # _turn_max_pt is None only when NO AssistantMessage had a usage object at all
                # (e.g. error-only turns) — in that case we leave last_ctx_tokens unchanged
                # rather than overwriting it with a stale 0.
                if _turn_max_pt is not None:
                    last_ctx_tokens = _turn_max_pt
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
                # Cost ledger: persist this turn's usage facts to disk (only turns that actually
                # carried a usage object — _turn_max_pt is None on error-only/usage-less turns,
                # which we skip rather than log a misleading all-zeros row).
                if _turn_max_pt is not None:
                    append_usage_ledger({
                        "ts": time.time(),
                        "entrypoint": entrypoint,
                        "project": project_name,
                        "session_key": session_key,
                        "model": resolved_model,
                        "effort": _eff_effort,
                        "ultracode": ultracode,
                        "context_tokens": last_ctx_tokens,
                        "fresh_tokens": _fresh,
                        "cache_read_tokens": _cache_read,
                        "cache_hit_pct": _cache_hit_pct,
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                        "duration_ms": _dur,
                    })
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
                    # Card b6f5cc: a task-completion notification whose task_id matches a tracked
                    # background monitor (bg-bash / Monitor tool) flips it to a terminal status.
                    # only_existing guard → a plain sub-agent task_id never spawns a phantom monitor.
                    try:
                        if _monitor_update_cb and msg.task_id:
                            # TaskNotificationStatus is Literal['completed','failed','stopped'].
                            _st = str(getattr(msg, "status", "") or "").lower()
                            _mst = {"completed": "done", "failed": "failed",
                                    "stopped": "stopped"}.get(_st)
                            print(f"[monitor] task-notification id={msg.task_id} status={_st!r} → {_mst}")
                            if _mst:
                                _monitor_update_cb(session_key, {"id": str(msg.task_id), "status": _mst},
                                                   only_existing=True)
                    except Exception:
                        pass
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
        except ProcessError as exc:
            if exc.exit_code == 143:
                # SIGTERM to the CLI subprocess — expected on interrupt/stop/service shutdown.
                # Log concisely and do not propagate; avoids asyncio "never retrieved" noise.
                print(f"[engine] subprocess terminated (143) — expected on interrupt/shutdown ({session_key})")
            else:
                # Subprocess state is unknown after an error — evict so the next turn reconnects fresh.
                print(f"[live-client] error during turn for {session_key} ({exc!r}) — evicting")
                await _evict_live_client(session_key, ctx)
                yield {"type": "error", "exc": exc}
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
        except ProcessError as exc:
            if exc.exit_code == 143:
                # SIGTERM to the CLI subprocess — expected on interrupt/stop/service shutdown.
                # Log concisely and do not propagate; avoids asyncio "never retrieved" noise.
                print(f"[engine] subprocess terminated (143) — expected on interrupt/shutdown ({session_key})")
            else:
                yield {"type": "error", "exc": exc}
        except Exception as exc:
            yield {"type": "error", "exc": exc}


def _build_ctx(*, web_port: int = None, web_password: str = None) -> dict:
    """Build the shared context dict passed to webapp.start().

    Values come from module-level state so the cockpit and kanban auto-run share
    the same topics/sessions/running/etc. dicts.

    Also registers webapp callbacks to avoid circular import in hooks.

    web_port, web_password: passed in from bot.py (it already read + applied env).
    """
    import webapp as _webapp  # lazy — called only at startup after webapp is fully loaded
    _register_webapp_callbacks(_webapp._timeline_append, _webapp._bus_publish, _webapp._monitor_update)

    _web_port = web_port if web_port is not None else int(os.getenv("WEB_PORT", "8787"))
    _web_password = web_password if web_password is not None else os.getenv("WEB_PASSWORD", "")

    return {
        "port": _web_port,
        "password": _web_password,
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
        # Spec-021 Phase 4 / spec-042: pending handoff summaries awaiting injection (shared with webapp via ctx)
        "pending_handoff": pending_handoff,
        # spec-042: callable to persist pending_handoff to disk (save_handoff in engine.py).
        "save_handoff": save_handoff,
        # Context early-warn: tracks session keys that have already fired the CONTEXT_WARN_AT alert.
        # Shared by reference — webapp.py reads/writes it via ctx["context_warned"].
        "context_warned": context_warned,
        # Spec-028: persistent-client feature flag + registry (exported so webapp can read without
        # importing bot.py; webapp passes ctx to run_engine which reads these fields).
        "PERSISTENT_CLIENT": PERSISTENT_CLIENT,
        "live_clients": _live_clients,
        # spec-039: eviction callable exposed via ctx so webapp.py can evict live clients
        # without importing bot.py.  Signature: async (session_key: str, ctx: dict|None) -> None.
        "evict_live_client": _evict_live_client,
        # spec-034 L2: board reconciler callable (webapp.py must not import bot.py directly)
        "reconcile_board": reconcile_board,
    }


async def _graceful_shutdown(registry: "dict[str, object]") -> None:
    """Flush session state and evict all live clients on process shutdown.

    spec-039 safety constraint (cgroup gotcha): this function MUST NOT call
    systemctl, kill, or os._exit — it only persists state on the way down.
    Process termination is owned entirely by systemd.  Idempotent and exception-safe.

    `registry` is the live-client dict to drain (in production: the module-level
    `_live_clients`).  Eviction is done via a synthetic ctx so _evict_live_client
    pops from the correct dict regardless of whether it matches `_live_clients`.
    """
    # 1. Persist in-flight session_ids so the next startup can resume them.
    try:
        save_sessions()
        print("[shutdown] sessions.json flushed")
    except Exception as exc:
        print(f"[shutdown] WARNING: failed to flush sessions.json: {exc!r}")

    # 2. Gracefully disconnect all live CLI subprocesses.
    if not registry:
        return
    keys = list(registry.keys())
    print(f"[shutdown] evicting {len(keys)} live client(s): {keys}")
    # Build a synthetic ctx so _evict_live_client targets `registry`, not `_live_clients`,
    # in the rare case they are different objects (tests, future multi-registry setups).
    _shutdown_ctx = {"live_clients": registry}
    for key in keys:
        try:
            await _evict_live_client(key, _shutdown_ctx)
        except Exception as exc:
            print(f"[shutdown] WARNING: eviction failed for {key}: {exc!r}")
