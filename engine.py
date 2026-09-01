"""
engine.py — Transport-neutral engine block extracted from bot.py (spec-040 Phase B).

Contains: run_engine, DEFAULT_AGENTS, prompts, audit, live-client registry (spec-028),
reconcile_board (spec-034), _build_ctx, _graceful_shutdown, state dicts,
resolve_project/build_registry, key_of.

bot.py re-exports all engine symbols for backward compatibility.
webapp.py imports engine directly; it must NOT import bot.py.
"""
import asyncio
import contextlib
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
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    query as _sdk_query,
)
from claude_agent_sdk.types import HookContext, PostToolUseHookInput, PreCompactHookInput
from second_opinion import build_antigravity_server
import modules as _modules                # spec-065: module enable/disable registry
import accounts as _accounts              # multi-subscription switch (CLAUDE_CONFIG_DIR per run)
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

MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable"}
# ⚠️ Aliases are NOT always the latest generation. Bundled CLI 2.1.191 resolves
# `opus`→opus-4-8 and `haiku`→haiku-4-5 (current), but `sonnet`→claude-sonnet-4-6
# — Sonnet 5 is reachable only by its explicit id. Re-probe after model releases:
#   claude_agent_sdk/_bundled/claude --model <alias> -p "Output only your exact model id."

# ─────────────────────────── sub-agent roster ───────────────────────────
# Default agents available to conductor sessions via the SDK Task tool.
# Models are configurable via env; Phase C will add per-project overrides.
# Explicit id, not the `sonnet` alias — the alias still resolves to Sonnet 4.6.
_EXECUTOR_MODEL = os.getenv("EXECUTOR_MODEL", "claude-sonnet-5")
_RESEARCHER_MODEL = os.getenv("RESEARCHER_MODEL", "claude-sonnet-5")
_QUICK_MODEL = os.getenv("QUICK_MODEL", "haiku")

# Effort level for the conductor/main session.
# "medium" reduces rate-limit burn (thinking weighs ~5× in the window) vs the
# SDK default of "high". Gate behind env so operators can escalate without a
# code change. Valid values: low | medium | high | xhigh | max.
# --effort is honored on Fable 5 (low..xhigh|max; official default high).
# A launch-window pin briefly overrode it in June 2026; do not re-add
# 'effort is ignored on fable' assumptions.
_DEFAULT_EFFORT: str = os.getenv("DEFAULT_EFFORT", "high")

# Effort the CLI pins internally when the native {"ultracode": true} settings flag is on.
# NOT "max" — the bundled CLI's own strings say so verbatim ("Enable ultracode for the
# session: xhigh effort plus standing dynamic-workflow orchestration", and `/effort` help:
# "- ultracode: xhigh + dynamic workflow orchestration"). The gap is real, not cosmetic:
# a same-prompt probe measured ~2.5k thinking tokens at xhigh vs ~12.5k at max.
# The UI must display THIS value while ultracode is on — asserted by
# test_ultracode_effort_label_matches_engine (see memory `opus5-alias-staleness-2026-07-24`
# for why label-vs-reality drift gets its own guard).
ULTRACODE_EFFORT: str = "xhigh"

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
    # spec-058 v2: adversarial verifier for ultracode workflows (Workflow opts.agentType or Task).
    # A dedicated skeptic role keeps verification INDEPENDENT of whoever produced the finding —
    # the same agent re-checking its own claim rubber-stamps it.
    "skeptic": AgentDefinition(
        description="Adversarial verifier. Tries to REFUTE a claim/finding with evidence. Read-only.",
        prompt=(
            "You are a skeptic sub-agent. Your job is to try to REFUTE the claim or finding in "
            "the task brief — not to confirm it. Hunt for counter-evidence: read the actual code/"
            "files, run read-only checks, look for the failure scenario not reproducing, missing "
            "preconditions, or an alternative explanation. Do NOT write or edit files.\n"
            "Verdict rules: default to REFUTED when the evidence is inconclusive; say CONFIRMED "
            "only when you personally traced concrete evidence that the claim holds. Return: "
            "verdict (CONFIRMED | REFUTED), the strongest counter-argument you found, and the "
            "evidence trail (files/lines/commands)."
        ),
        model=_RESEARCHER_MODEL,
        permissionMode="bypassPermissions",
        disallowedTools=["Write", "Edit", "NotebookEdit"],
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


# spec-078 Phase 2 — per-project brains. A global lean default for the SDK `skills` context
# filter: SKILLS_DEFAULT_ALLOW="a,b,c" restricts EVERY project to that skill set unless the
# project overrides via agents_config.skills. Unset → None → CLI default (all skills, no change).
_SKILLS_DEFAULT_ALLOW_ENV = os.getenv("SKILLS_DEFAULT_ALLOW")
_DEFAULT_SKILLS: "list[str] | str | None" = (
    [s.strip() for s in _SKILLS_DEFAULT_ALLOW_ENV.split(",") if s.strip()]
    if _SKILLS_DEFAULT_ALLOW_ENV else None
)


# spec-078 Phase 3a — one canonical brain per project.
#
# Two memory systems overlap today. The CLI's native auto-memory
# (~/.claude/projects/<slug>/memory/) only ever INGESTS: it appends what a session learned and
# never prunes, so its MEMORY.md index — loaded verbatim on every bootstrap — rots and grows
# (claude-ops-bot: 88 files, a 17 KB index ≈ 4.3k tokens per session, paid forever). The curated
# ./.claude-ops/memory/ is the opposite: deliberate, linted (tools/memory-lint.py), and capped by
# the cockpit's context pack before it ever reaches the prompt.
#
#   "auto"    — native auto-memory stays on (unchanged; the default, no surprises).
#   "project" — native auto-memory OFF; ./.claude-ops/memory/ is the project's ONLY brain.
#
# Opt-in per project via agents_config.memory. The switch is the env var the CLI actually reads
# (verified in the bundled binary: `process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY`); `--bare` would
# also disable it but takes hooks, plugins and CLAUDE.md discovery down with it.
_MEMORY_MODES = ("auto", "project")
_DEFAULT_MEMORY_MODE = "auto"


def _memory_env_overrides(mode: "str | None") -> dict:
    """Env additions that put a project into the requested memory mode.

    Unknown values degrade to "auto" rather than raise: a typo in agents_config must not take a
    project's sessions down.  webapp validates the value at the API boundary.
    """
    if mode == "project":
        return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    return {}


def _plugin_install_path(plugin_id: str) -> "str | None":
    """Resolve a plugin id → its local install path from ~/.claude/plugins/installed_plugins.json.

    Version-agnostic (reads the current installPath from disk, never hardcodes a version).
    Accepts either the full registry key ("marketing-skills@marketingskills") or just the
    name part ("marketing-skills"). Returns None if the plugin is not installed.
    """
    try:
        data = json.loads(
            (Path.home() / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        plugins = data.get("plugins") or {}
        entries = plugins.get(plugin_id)
        if entries is None:  # match by the name part before "@"
            for key, val in plugins.items():
                if key.split("@", 1)[0] == plugin_id:
                    entries = val
                    break
        if isinstance(entries, list) and entries:
            return entries[0].get("installPath")
    except Exception:
        return None
    return None


def _merge_project_skills(project_skills, default):
    """Resolve a project's skill filter against the global default.

    A plain list REPLACES the default (spec-078 behaviour, unchanged). A list whose entries are
    prefixed with "+" is ADDITIVE: the project keeps the global core and adds its own on top.

    Without the additive form, scoping (say) the video skills to one project means re-listing the
    entire engineering core in that project's settings — and every one of those copies rots the
    day the core changes. With no global default set (None = the CLI's own default, i.e. every
    skill), "+" has nothing to add to and degrades to None rather than silently narrowing the
    project down to its own few entries.
    """
    if project_skills is None or isinstance(project_skills, str):
        return project_skills if project_skills is not None else default
    plus = [s[1:].strip() for s in project_skills
            if isinstance(s, str) and s.startswith("+") and s[1:].strip()]
    if not plus:
        return project_skills
    if not isinstance(default, list):
        return default  # base is "everything" already — narrowing here would be a downgrade
    rest = [s for s in project_skills if isinstance(s, str) and not s.startswith("+")]
    merged, seen = [], set()
    for name in [*default, *plus, *rest]:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


def _build_agents_kwargs(agents_config: dict) -> dict:
    """Build keyword args for run_engine from a project's agents_config dict.

    Returns a dict that can be unpacked into run_engine(**kwargs).
    Empty / absent agents_config → {} (use defaults).

    Keys recognised in agents_config:
        executor_model   — model alias for the executor agent
        researcher_model — model alias for the researcher agent
        quick_model      — model alias for the quick agent
        conductor_prompt — bool; False → pass skip_conductor_prompt=True to run_engine
        skills           — spec-078: SDK skill context filter (list[str] | "all"); per-project
                           lean/curated skill set so a project pulls only ITS relevant skills
        plugins          — spec-078: list of plugin ids to load for this project only (opt-in),
                           e.g. ["marketing-skills"] on the marketing project — nothing elsewhere
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

    # spec-078: per-project skill filter + plugin opt-in (each project pulls only its own brains).
    if "skills" in agents_config:
        kwargs["project_skills"] = agents_config["skills"]
    if "plugins" in agents_config:
        kwargs["project_plugins"] = agents_config["plugins"]
    # spec-078 Phase 3a: "project" → curated ./.claude-ops/memory/ is the project's only brain.
    if "memory" in agents_config:
        kwargs["project_memory"] = agents_config["memory"]

    return kwargs


# Conductor directive appended to system_prompt when model is fable AND the run is not ultracode
# (ultracode activates the CLI's native Workflow contract for every model — see below).
# Kept as a module constant so it can be asserted in tests without instantiating run_engine.
CONDUCTOR_PROMPT = (
    "You are an orchestrator. Delegate substantial execution to sub-agents via the Task tool — "
    "pass them a self-contained brief (no chat history; just what they need). Reserve your own "
    "turns for planning, decision-making, and synthesising results. Do not run long code "
    "sequences or file-editing loops yourself. "
    "Prefer ≤3–5 concurrent sub-agents; sequence tasks rather than parallelising unnecessarily."
)

# spec-058 v2 (native): Ultracode now activates the CLI's NATIVE ultracode machinery via the
# --settings flag (ULTRACODE_SETTINGS below). The CLI itself injects the standing opt-in
# system-reminders ("Ultracode is on: … use the Workflow tool on every substantive task"),
# exposes the Workflow tool's Ultracode contract + quality patterns (adversarial verify, judge
# panels, loop-until-dry), and pins effort to "xhigh" internally. Verified empirically on
# claude-opus-4-8 with the bundled CLI 2.1.191: the Workflow tool is in the session tool list
# and a 2-agent workflow executes end-to-end under --settings '{"ultracode": true}'.
# ULTRACODE_PROMPT is therefore no longer the orchestration contract — it is a thin Cardloop
# COMPLEMENT to the native contract: names the local agent roster (usable as Workflow agentType
# or Task subagents), and pins cockpit-specific reporting rules. It must never restate or fight
# the native contract (no concurrency caps, no "prefer Task over Workflow").
ULTRACODE_PROMPT = (
    "Ultracode is active natively (see the Workflow tool's Ultracode section). Cardloop complement:\n"
    "- Prefer an authored Workflow over ad-hoc Task fan-out for anything multi-step: deterministic "
    "pipelines with adversarial verification beat improvised delegation. Verify findings with "
    "independent skeptics that try to REFUTE them before you act on them.\n"
    "- Named agent types available to Workflow (opts.agentType) and the Task tool: `executor` "
    "(Sonnet — writes files, runs commands), `researcher` (Sonnet — read-only research), `skeptic` "
    "(Sonnet — adversarial verifier: tries to refute a claim), `quick` (Haiku — fast cheap lookups). "
    "Pick per stage; the default workflow subagent is also fine.\n"
    "- The operator watches the cockpit and cannot read workflow internals: your final message must "
    "carry the complete synthesis (findings, decisions, evidence, next steps) — never a reference "
    "to sub-agent output."
)

# spec-058 v2: inline JSON passed to the SDK's `settings` option (forwarded verbatim to the CLI
# --settings flag, which accepts a path OR an inline JSON object). {"ultracode": true} is the
# same switch the interactive CLI flips for /effort ultracode: it survives headless --print mode,
# while `--effort ultracode` is rejected there ("Unknown --effort value") — do not "simplify"
# this back to an effort value. The flag pins effort to xhigh internally, so run_engine passes
# NO --effort when ultracode is on (a CLI effort flag would override the native pin).
ULTRACODE_SETTINGS = '{"ultracode": true}'

# Inbound cross-session messages (another Claude Code session writing to this one). The CLI
# gates them behind the `crossSessionInbound` setting, and Cardloop never set it — which is why
# a delivery could only ever be discovered after the fact, in the transcript.
#
#   "hold"   — the CLI does NOT hand the message to the model; it records it for the operator.
#              With peer_message surfacing (see _peer_message_event) that is the useful shape:
#              the operator sees who wrote what, and no foreign message can steer a live run.
#   "accept" — delivered to the model, i.e. another session can steer this one. Deliberate opt-in.
#   "refuse" — rejected outright.
#
# Unset (the default) passes NOTHING, so a stock install keeps the CLI's own default and the
# --settings flag stays absent unless ultracode is on.
_CROSS_SESSION_INBOUND_VALUES = frozenset({"accept", "hold", "refuse"})
CROSS_SESSION_INBOUND: str = (os.getenv("CROSS_SESSION_INBOUND", "").strip().lower()
                              if os.getenv("CROSS_SESSION_INBOUND", "").strip().lower()
                              in _CROSS_SESSION_INBOUND_VALUES else "")


def _compose_settings(ultracode: bool) -> "str | None":
    """Inline --settings JSON for this run: the native ultracode switch (+ inbound peer policy).

    Returns None when nothing needs setting — no --settings flag at all, byte-identical to the
    pre-feature behaviour. Invariant: with CROSS_SESSION_INBOUND unset,
    _compose_settings(True) == ULTRACODE_SETTINGS (locked by a test), so the ultracode path is
    unchanged for every existing install.
    """
    settings: dict = {}
    if ultracode:
        settings["ultracode"] = True
    if CROSS_SESSION_INBOUND:
        settings["crossSessionInbound"] = CROSS_SESSION_INBOUND
    if not settings:
        return None
    if settings == {"ultracode": True}:
        return ULTRACODE_SETTINGS   # exact string the ultracode tests pin
    return json.dumps(settings)


# spec-066: appended to system_prompt when the browser module is on, so the agent knows the
# live cockpit pane IS "the browser". Without this, asked to "open/launch the browser", an agent
# tends to spawn an external/headless browser (Playwright, Selenium) the operator can't see.
def _browser_prompt(backend: str, agent_actions: str) -> str:
    gate = (
        "You may navigate, read, click, type, upload files and select dropdown options."
        if agent_actions == "full"
        else "Read-only mode: browser_navigate, browser_snapshot and browser_status work; "
        "browser_click, browser_type, browser_upload and browser_select are refused until the "
        "operator enables full actions in Extensions → Browser."
    )
    # Only advertised when a solver key is actually configured — otherwise the agent
    # burns a turn calling a tool that can only answer "no API key". A shipped
    # mechanism the prompt never mentions is a mechanism agents never use (spec-038),
    # so this hint is load-bearing, not decoration.
    captcha = ""
    with contextlib.suppress(Exception):
        import captcha_solver as _cs
        if _cs.configured() and agent_actions == "full":
            captcha = (
                " If a captcha blocks the page, use browser_solve_captcha — do NOT click the "
                "checkbox or try to pick the 'select all fire hydrants' image tiles, and do not "
                "ask the operator to do it. It handles reCAPTCHA, hCaptcha and Turnstile widgets "
                "by injecting a solved token, so the image grid never has to be answered at all. "
                "It costs money per call, so confirm a captcha is really there first, and check "
                "the result afterwards rather than assuming the form went through."
            )
    return (
        f"A live browser pane is active (the 'browser' module, backend: {backend}). When asked to "
        "open, launch, show or use 'the browser', or to open a URL or web page, drive THIS pane with "
        "the mcp__browser__ tools (browser_navigate, browser_snapshot, browser_click, browser_type, "
        "browser_upload, browser_select, browser_status) — the operator watches it live in the "
        "cockpit. A file input needs browser_upload, not browser_click/browser_type: clicking an "
        "upload button only opens the OS's native file picker, which is invisible to this pane. A "
        "native <select> needs browser_select, not browser_click: clicking it pops OS/browser-native "
        "list UI outside the page — a required <select> can look filled in a snapshot yet still be "
        "unset, so a rejected form submit with no visible error is a strong hint to check for one. Do "
        f"NOT spawn an external or headless browser (Playwright, Selenium, a subprocess) for this. {gate}"
        f"{captcha}"
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

# Companion to IMAGES_PROMPT (same COPS_MEDIA_DIR gating): surface an arbitrary downloadable
# file (pdf, zip, csv, …) inline in the cockpit chat. cockpit-file copies the file into the
# chat-media dir and prints one "attached file: <url>" line the client renders as a download card.
FILES_PROMPT = (
    f"To send {OPERATOR_NAME} a downloadable FILE of any format (pdf, zip, csv, docx, json, "
    "audio, …) INSIDE the cockpit chat, run the helper `cockpit-file <path>` and paste the single "
    "`attached file: …` line it prints verbatim into your reply — the cockpit renders it as a "
    "download card the operator can save to their computer or phone. Use this for any file you "
    "want to hand over, instead of Telegram or pasting a bare filesystem path."
)

# docs/internal/sdk-feature-audit/02-subagent-output.md: a sub-agent's full final text becomes
# the tool_result of the parent's Agent tool_use, and every later turn in that chat re-sends the
# whole transcript — a long report (detailed findings, multi-file audit, log dump) is paid again
# on every subsequent turn, not just once. Same COPS_MEDIA_DIR gate as FILES_PROMPT above.
# Appended to sub-agent prompts that produce open-ended reports — NOT "quick", whose whole point
# is a short inline answer with no report.
SUBAGENT_FILES_PROMPT = (
    "If your final report would run long (detailed findings, multi-file audit, long log dump), "
    "write the FULL report to a file and run `cockpit-file <path>` — paste the `attached file: …` "
    "line it prints into your final reply, then summarize in 5-10 lines. Do not paste the full "
    "report body into your final reply."
)

# AskUserQuestion = interactive prompt (no reply in TG -> agent hangs or decides on its own).
# EnterPlanMode = the model self-invoking plan mode mid-turn: a turn that did not START in
# plan mode has no can_use_tool wired, so a later ExitPlanMode would hang unanswered. Plan
# mode is operator-initiated only (spec-080). Live smoke showed the tool is absent from SDK
# sessions anyway — this is a belt against future CLI versions exposing it.
DISALLOWED_TOOLS = ["AskUserQuestion", "EnterPlanMode"]

# spec-060: optional "second opinion" MCP tool, built once at import. Fronts two backends
# — Antigravity (agy) and Azure AI Foundry (grok/deepseek/gpt5). None when SECOND_OPINION=0
# or NO backend is available — then no tool is exposed and the engine behaves exactly as
# before. Building it invokes nothing.
_ANTIGRAVITY_MCP = build_antigravity_server()
if _ANTIGRAVITY_MCP:
    from second_opinion import _resolve_agy as _so_agy, _azure_configured as _so_azure
    _backends = [b for b, on in (("agy", _so_agy()), ("azure", _so_azure())) if on]
    print(f"[second_opinion] MCP tool enabled (backends: {', '.join(_backends) or 'none'})")

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
    # spec-071: asyncio.Task consuming the SDK stream while no engine turn is active.
    # None until started; paused (cancelled) for the duration of each turn.
    drain_task: object = None


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


def _pick_served_model(model_usage: "dict | None", requested: "str | None") -> "str | None":
    """Which model did THIS turn actually run on, out of a multi-model `model_usage` map?

    Since bundled CLI 2.1.252 one turn bills more than one model: the requested alias plus
    helper traffic (auto-mode tool classification) billed to Haiku — and the helper lands FIRST
    in the map. Reading `next(iter(...))` therefore names Haiku for every turn; that exact bug
    produced a false alias MISMATCH on 2026-08-31. Same rule as
    tools/verify_model_aliases.py::_pick_family_model: prefer the requested family, else the
    heaviest entry, which is the honest answer when the turn genuinely ran something else.

    Returns None when there is nothing to read (older CLI, or a turn that died before any
    model call) — callers fall back to the init-message `served_model`."""
    # isinstance, not truthiness: a non-dict (an older SDK's Any, a test double) is "no data",
    # and max() over it would explode inside the turn.
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    family = (requested or "").replace("claude-", "").split("-")[0]
    if family:
        same_family = [k for k in model_usage if str(k).startswith(f"claude-{family}")]
        if same_family:
            return same_family[0]
    return max(model_usage, key=lambda k: (model_usage.get(k) or {}).get("outputTokens") or 0)


def _abort_reason_for(exc: BaseException, hint: "str | None") -> "tuple[str, str | None, list | None]":
    """Name the abort cause, preferring the CLI's verdict only where the CLI has one.

    Precedence here is mechanical, not stylistic. A buffer overflow is OUR reader giving up on an
    oversized message, so no result frame exists for the CLI to have reported — our hint wins.
    A SIGTERM (handled by its own branch before this is called) is the same story. Everything
    else that surfaces as a ResultError carries the CLI's own `terminal_reason` ("max_turns",
    "api_error", ...), which is strictly more specific than the flat "sdk_error" we used to
    write for the whole catch-all bucket.

    Returns (reason, sdk_subtype, sdk_errors)."""
    if hint:
        return "buffer_overflow", None, None
    if isinstance(exc, ResultError):
        return (getattr(exc, "terminal_reason", None) or "sdk_error",
                getattr(exc, "subtype", None), getattr(exc, "errors", None))
    return "sdk_error", None, None


def _record_turn_abort(session_key: str, reason: str, detail: str = "",
                       sdk_subtype: "str | None" = None,
                       sdk_errors: "list | None" = None) -> None:
    """Root-fix B2: write the TRUE abort cause to the timeline at the moment it happens.

    The CLI stamps an ambiguous "[Request interrupted by user]" into the transcript on
    forced terminations; downstream digest builders used to grep that string and blame the
    operator for infra aborts. Cardloop always knows the real cause — record it. Never raises."""
    try:
        if _timeline_append_cb:
            _row = {"kind": "turn_aborted", "reason": reason, "detail": detail[:300]}
            if sdk_subtype:
                _row["sdk_subtype"] = sdk_subtype
            if sdk_errors:
                _row["sdk_errors"] = sdk_errors[:5]
            _timeline_append_cb(session_key, _row)
    except Exception:
        pass


def _buffer_overflow_hint(exc: BaseException) -> "str | None":
    """Recognize the SDK reader's per-message buffer overflow and name the likely cause.

    Returns an operator-actionable hint string, or None if the error is unrelated.
    The raw SDK message already states the byte limit; this adds the "what to do about it".
    """
    if "exceeded maximum buffer size" in str(exc):
        return (f"SDK message exceeded SDK_MAX_BUFFER_BYTES ({SDK_MAX_BUFFER_BYTES} bytes) — "
                "likely inline media in a tool result; raise SDK_MAX_BUFFER_BYTES or avoid "
                "reading large binary/base64 payloads inline")
    return None


def _rewind_refused_hint(exc: BaseException) -> "str | None":
    """Recognize the SDK's connect-time refusal of a truncating resume and name the cause.

    docs/internal/sdk-feature-audit/04-session-rewind.md §1: `resume_drops_turn` validation
    refuses to load a session whenever the range between `resume_session_at` and the next
    turn contains anything the caller didn't declare (e.g. a queued message or task
    notification the session absorbed mid-turn) — the CLI's own message contains the literal
    substring matched below. Per the SDK docstring this is deterministic: never retry the
    same resume_at/drops_turn pair, only re-resolve the split point (or fall back to a plain
    resume with no truncation).
    """
    if "Resume rejected by --resume-drops-turn:" in str(exc):
        return ("rewind refused — the discarded range was not a clean single turn "
                "(a queued message or background task notification landed in it); "
                "pick a different message to rewind to, or use /reset instead")
    return None


# ─────────────────────────── audit ───────────────────────────
AUDIT_DIR = DATA / "audit"
# STALL_SECONDS / MAX_SECONDS removed (root-fix C): the stall interrupt was deleted in
# spec-039 and the "absolute turn ceiling" never had an enforcement site — an unenforced
# bound with a live settings slider is worse than none. The real bounds are
# LIVE_CLIENT_MAX_PIN_SEC (persistent clients) and CARD_LINGER_MAX_SEC (card linger).

# ── Spec-028: persistent (long-lived) client feature flag ─────────────────────────────────────
# PERSISTENT_CLIENT=0 (default OFF) → behaviour is byte-identical to pre-028; all existing tests pass.
# PERSISTENT_CLIENT=1 → run_engine reuses the same ClaudeSDKClient across turns for non-ephemeral
# sessions (chat / deferred), skipping per-turn connect/disconnect overhead.
PERSISTENT_CLIENT: bool = os.getenv("PERSISTENT_CLIENT", "0") == "1"
# Max idle seconds before an unused live client is evicted (disconnected) automatically.
LIVE_CLIENT_TTL_SEC: int = int(os.getenv("LIVE_CLIENT_TTL_SEC", "600"))
# Max number of concurrent live clients held in the registry; LRU eviction beyond this.
LIVE_CLIENT_MAX: int = int(os.getenv("LIVE_CLIENT_MAX", "10"))
# Memory headroom guard for the same registry. A live client is a whole CLI subprocess
# (~0.4-0.6 GB RSS each, more with sub-agents), so a count-based cap alone does not bound
# memory: on ops the cgroup hit MemoryMax twice (2026-08-26 13:43, 2026-08-27 09:11) with
# the registry legally under LIVE_CLIENT_MAX, the kernel OOM-killed a `claude` child, and
# systemd restarted the whole service mid-turn — which the operator sees as a frozen chat,
# a dead browser pane, or a sent message that never appears. Above this fraction of the
# cgroup's memory.max we LRU-evict idle clients BEFORE connecting another one.
# 0 disables the guard (also inert when the process is not in a memory-limited cgroup).
LIVE_CLIENT_MEM_GUARD: float = float(os.getenv("LIVE_CLIENT_MEM_GUARD", "0.75"))
# Per-message JSON buffer cap for the SDK's stdout reader. The SDK default is 1 MiB
# (subprocess_cli._DEFAULT_MAX_BUFFER_SIZE) — a single inline image or a large tool_result
# routinely exceeds that, and the reader then kills the whole subprocess mid-turn
# ("Fatal error in message reader: JSON message exceeded maximum buffer size").
# The buffer is transient and bounded per in-flight message per client (worst case
# LIVE_CLIENT_MAX × this value), so a generous cap is cheap.
SDK_MAX_BUFFER_BYTES: int = int(os.getenv("SDK_MAX_BUFFER_BYTES", str(32 * 1024 * 1024)))

# spec-085 Phase 3: sub-agent visibility. With the SDK's forward_subagent_text on, a
# sub-agent's finalized TextBlocks arrive as AssistantMessages tagged with the spawning
# Agent tool_use id — we surface them in the cockpit's subagent lane (subtype "text").
# The caps bound a wide fan-out: the lane is a live peek, not a full nested transcript
# (that still comes from the per-agent .jsonl on demand). FORWARD_SUBAGENT_TEXT=0 turns
# both the SDK option and the forwarding off.
FORWARD_SUBAGENT_TEXT: int = int(os.getenv("FORWARD_SUBAGENT_TEXT", "1"))
SUBAGENT_TEXT_MAX_CHARS: int = 2000   # per forwarded block
SUBAGENT_TEXT_MAX_BLOCKS: int = 50    # per sub-agent per turn


def _subagent_text_events(msg, tool_to_task: dict, counts: dict) -> "list[dict]":
    """spec-085 Phase 3: map a sub-agent AssistantMessage to subagent/text engine events.

    `tool_to_task` maps a spawning Agent tool_use id → task_id (filled from
    TaskStartedMessage.tool_use_id); text from an unmapped parent id is dropped (no row
    to attach it to). `counts` tracks forwarded blocks per task for the per-turn cap.
    Thinking blocks and stream deltas are never forwarded — finalized text only."""
    if not FORWARD_SUBAGENT_TEXT or not isinstance(msg, AssistantMessage):
        return []
    task_id = tool_to_task.get(getattr(msg, "parent_tool_use_id", None))
    if not task_id:
        return []
    out: "list[dict]" = []
    for blk in msg.content:
        if not (isinstance(blk, TextBlock) and blk.text.strip()):
            continue
        n = counts.get(task_id, 0)
        if n >= SUBAGENT_TEXT_MAX_BLOCKS:
            break
        counts[task_id] = n + 1
        out.append({
            "type": "subagent", "subtype": "text", "task_id": task_id,
            "description": None, "status": None, "summary": None,
            "last_tool_name": None,
            "text": blk.text.strip()[:SUBAGENT_TEXT_MAX_CHARS],
        })
    return out

# ─────────────── cross-session (peer/channel) message surfacing ───────────────
#
# Another Claude Code session — or an in-process peer — can write to THIS session, and the CLI
# replays that delivery as a user turn carrying `origin` (a TypedDict: kind, from, name,
# fromSession, body, …). Tool-result echoes never carry origin, so this cannot fire on ordinary
# traffic.
#
# Until now the message loop ignored UserMessage entirely: an incoming peer message was
# invisible while the turn ran and only surfaced AFTER the fact, when the history feed matched
# the raw envelope with a regex (webapp._CROSS_AGENT_MSG_RE). origin["body"] is the CLI's own
# decoded body, byte-exact with what the model saw — the SDK explicitly says to render that
# instead of re-parsing the envelope.
#
# The filter is a DENYLIST, not an allowlist: the SDK documents that newer CLIs may emit kinds
# it does not list yet and that anything unrecognized should be treated as "not human".
_PEER_ORIGIN_SKIP_KINDS = frozenset({
    "human",                # the operator's own turn
    "auto-continuation",    # the CLI continuing itself
    "task-notification",    # background-task notifications: already their own lane
    "observer-activity",    # activity pings, not a message
})
PEER_MESSAGE_MAX_CHARS: int = 8000


def _peer_message_event(msg) -> "dict | None":
    """Map a UserMessage carrying a non-human `origin` to a peer_message engine event."""
    origin = getattr(msg, "origin", None)
    if not isinstance(origin, dict):
        return None
    kind = origin.get("kind")
    if not kind or kind in _PEER_ORIGIN_SKIP_KINDS:
        return None
    text = (origin.get("body") or "").strip()
    if not text:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "\n".join(b.text for b in content
                              if isinstance(b, TextBlock) and getattr(b, "text", "")).strip()
    if not text:
        return None
    return {
        "type": "peer_message",
        "kind": kind,
        # Sender-asserted per the SDK — fine for display and reply routing, never identity proof.
        "sender": (origin.get("name") or origin.get("from") or origin.get("server")
                   or "another session"),
        "from_session": origin.get("fromSession"),
        "text": text[:PEER_MESSAGE_MAX_CHARS],
    }

# Root-fix A4: bounded post-turn linger for ephemeral (card) runs whose deferring background
# tasks are still open when the turn's ResultMessage arrives — the `async with` exit would
# otherwise disconnect and SIGTERM them mid-work. No-op when the CLI's native deferral
# already resolved everything before the result (the set is empty by then).
CARD_LINGER_MAX_SEC: int = int(os.getenv("CARD_LINGER_MAX_SEC", "300"))
# Mirrors claude_agent_sdk._internal.query.DEFERRING_TASK_TYPES ("the set the CLI itself
# holds a result back for"). Duplicated rather than imported from a private _internal
# module; RE-VERIFY against the SDK on every claude-agent-sdk version bump.
DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})
# Root-fix C: observability for the SDK's silent drops. Unknown SystemMessage subtypes are
# logged once per subtype per process (see _process_messages); SDK_DEBUG_UNKNOWN_MESSAGES=1
# additionally surfaces the SDK's own logger.debug line for messages parse_message drops
# entirely (e.g. Agent-Teams teammate/cross-session messages have no dataclass and return
# None) — the current blind zone for teammate traffic.
_UNKNOWN_SUBTYPES_SEEN: "set[str]" = set()
if os.getenv("SDK_DEBUG_UNKNOWN_MESSAGES", "0") == "1":
    import logging as _logging
    _logging.getLogger("claude_agent_sdk").setLevel(_logging.DEBUG)
# spec-073: SDK file checkpointing — lets the cockpit rewind files to any user-message
# checkpoint (POST /api/projects/{id}/rewind). Cheap (CLI shadows edited files only).
FILE_CHECKPOINTS: bool = os.getenv("FILE_CHECKPOINTS", "1") not in ("0", "false", "False")
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
# spec-069 f9d60d: webapp predicate — True if a background Agent sub-agent for a session_key
# is still running. Eviction guards consult it so a live client is never SIGTERMed while its
# sub-agents work (RC#1 protected only the main turn; background sub-agents outlive it).
_has_live_subagents_cb = None
# spec-063 Stage 2a: webapp callback surfacing autonomous CLI turns (drain-observed) as
# first-class background runs. Signature: (session_key, phase: 'start'|'text'|'end', text?).
_bg_run_cb = None


def _register_webapp_callbacks(timeline_append, bus_publish, monitor_update=None,
                               has_live_subagents=None, bg_run_event=None,
                               create_pending_plan=None, resolve_plan=None,
                               pending_plan_id=None, create_pending_tool=None):
    """Inject webapp callbacks so engine.py can publish events without importing webapp."""
    global _timeline_append_cb, _bus_publish_cb, _monitor_update_cb, _has_live_subagents_cb, \
        _bg_run_cb, _create_pending_plan_cb, _resolve_plan_cb, _pending_plan_id_cb, \
        _create_pending_tool_cb
    _timeline_append_cb = timeline_append
    _bus_publish_cb = bus_publish
    _monitor_update_cb = monitor_update
    _has_live_subagents_cb = has_live_subagents
    _bg_run_cb = bg_run_event
    _create_pending_plan_cb = create_pending_plan
    _resolve_plan_cb = resolve_plan
    _pending_plan_id_cb = pending_plan_id
    _create_pending_tool_cb = create_pending_tool


# ─────────────────────────── spec-080: cockpit plan mode ──────────────────────────────────────
# The plan-approval gate. A plan-mode turn connects with permission_mode="plan" (the CLI
# hard-blocks mutations and injects its native 5-phase plan workflow) and wires can_use_tool.
# ExitPlanMode arrives as a can_use_tool request; the webapp parks it as a pending plan card
# (Approve/Reject in the cockpit) and this side awaits the decision Future.
#
# Post-approval there is NO permission-mode flip: set_permission_mode("bypassPermissions") is
# illegal unless the CLI was launched with --dangerously-skip-permissions, and launching plan
# WITH that flag disables plan-blocking entirely (both verified live, spec-080 Wave 0). After
# an Approve the gate callback simply rubber-stamps every subsequent tool call of the turn
# (verified working live). The NEXT turn reconnects into real bypassPermissions naturally via
# the fingerprint mismatch — one cold start per approved plan, accepted.
_create_pending_plan_cb = None   # webapp.create_pending_plan(ctx, sk, chat_id, text, path)
_resolve_plan_cb = None          # webapp.resolve_plan(ctx, plan_id, decision, feedback)
_pending_plan_id_cb = None       # webapp: session_key -> plan_id | None
# Dispatcher state (NOT captured in closures — a reused live client services later turns with
# the FIRST turn's closure, so per-turn data must be read from here at call time):
_plan_turn_chat: "dict[str, str | None]" = {}   # session_key -> chat_id of the current plan turn
_plan_gate_approved: "dict[str, bool]" = {}     # session_key -> True after Approve (turn-scoped)
_plan_write_paths: "dict[str, str]" = {}        # session_key -> last observed ~/.claude/plans Write


def _make_plan_gate_cb(session_key: str, ctx: "dict | None"):
    """can_use_tool callback for plan-mode turns. Reads all per-turn state from module dicts
    at CALL time (dispatcher pattern) so a stale closure on a reused client stays correct."""

    async def _plan_gate_cb(tool_name, tool_input, tp_ctx):
        try:
            if _plan_gate_approved.get(session_key):
                return PermissionResultAllow()
            if tool_name != "ExitPlanMode":
                # Plan mode: the CLI already hard-blocks mutating tools before this callback;
                # whatever falls through (reads, ask-rule tools) is fine to allow.
                return PermissionResultAllow()
            _input = tool_input or {}
            plan_text = _input.get("plan") or ""
            plan_file = _input.get("planFilePath") or _plan_write_paths.get(session_key)
            if not plan_text and plan_file:
                try:
                    plan_text = Path(plan_file).read_text()[:200_000]
                except Exception:
                    pass
            if _create_pending_plan_cb is None:
                # No cockpit wired (unit tests / standalone) — approve so the engine stays usable.
                _plan_gate_approved[session_key] = True
                return PermissionResultAllow()
            plan_id, fut = _create_pending_plan_cb(
                ctx, session_key, _plan_turn_chat.get(session_key), plan_text, plan_file)
            print(f"[plan-gate] {session_key}: plan {plan_id} awaiting operator decision")
            try:
                decision = await fut
            except asyncio.CancelledError:
                # /stop or client teardown while awaiting — mark the record, re-raise.
                try:
                    if _resolve_plan_cb is not None:
                        _resolve_plan_cb(None, plan_id, "cancelled",
                                         "turn was interrupted while awaiting approval")
                finally:
                    raise
            if (decision or {}).get("decision") == "approve":
                _plan_gate_approved[session_key] = True
                print(f"[plan-gate] {session_key}: plan {plan_id} APPROVED — executing in-turn")
                return PermissionResultAllow()
            feedback = (decision or {}).get("feedback") or ""
            print(f"[plan-gate] {session_key}: plan {plan_id} REJECTED — model will revise")
            return PermissionResultDeny(
                message=feedback or "Plan needs revision — reconsider and call ExitPlanMode again.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The gate must never wedge a turn on an internal error — deny with the reason so
            # the model can surface it instead of hanging.
            print(f"[plan-gate] {session_key}: gate error {exc!r} — denying ExitPlanMode")
            return PermissionResultDeny(message=f"plan gate internal error: {exc}")

    return _plan_gate_cb


# ─────────────────────────── spec-082 A: ask mode (per-tool approval) ─────────────────────────
# A third turn mode next to normal / plan. The turn connects with permission_mode="default"
# and a can_use_tool gate: every non-read-only tool call parks a decision in the cockpit and
# the turn waits for the operator's Allow / Always-allow / Deny — answerable from a phone.
#
# ⚠️ "default", NOT "bypassPermissions": under bypass the SDK SHADOWS can_use_tool entirely
# (it emits CanUseToolShadowedWarning) and every tool would run ungated with no error at all.
# permission_mode is part of the live-client fingerprint, so flipping the toggle reconnects
# the client with the gate correctly bound — the same path plan mode uses.
#
# Verified live against the real SDK/CLI (not just unit tests):
#   • Write under "default" → the gate IS consulted, even with the operator's own
#     ~/.claude/settings.json setting {"permissions":{"defaultMode":"bypassPermissions"}} —
#     the --permission-mode flag outranks the settings file, no inline --settings needed.
#   • Bash `echo …` under "default" → the gate is NOT consulted: the CLI auto-approves
#     commands it classifies as harmless BEFORE can_use_tool. So ask mode gates mutations,
#     not literally every tool call — do not "fix" that by claiming full coverage in the UI.
# Other residual surface: whole-tool `allow` rules in the operator's settings, and a sub-agent
# spawned by Task whose own tool calls are not individually surfaced here. Ask mode is a
# "don't change my repo without asking" guard rail, not a sandbox.
_create_pending_tool_cb = None   # webapp.create_pending_tool_decision(ctx, sk, chat_id, tool, preview)
_ask_turn_chat: "dict[str, str | None]" = {}   # session_key -> chat_id of the current ask turn

# Tools that cannot change anything: gating them turns every turn into a click marathon for
# zero safety. ONE list, deliberately conservative — extend it here, not at the call site.
ASK_GATE_READONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "NotebookRead", "TodoWrite", "Task",
})
# A parked decision must never hang a turn forever: auto-deny after this many seconds with a
# model-readable message so the agent can adapt or stop instead of blocking on a dead operator.
ASK_GATE_TIMEOUT_SEC = max(5, int(os.getenv("ASK_GATE_TIMEOUT_SEC", "900") or 900))
_ASK_PREVIEW_CHARS = 2000        # hard cap on the preview handed to the cockpit
# Best-effort scrub of obvious secret shapes before a tool preview leaves the process. Not a
# guarantee — the operator still reads the command — but it keeps a pasted key out of a push
# notification and out of the decision sidecar.
_ASK_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,}"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*)\S+)")


def _ask_scrub(text: str) -> str:
    """Mask obvious secret shapes in a preview string."""
    try:
        return _ASK_SECRET_RE.sub(lambda m: m.group(0)[:12] + "…[redacted]", text)
    except Exception:
        return text


def _ask_tool_preview(tool_name: str, tool_input: "dict | None") -> str:
    """One short, redacted, truncated human preview of what the tool is about to do.

    The operator decides from this string on a phone, so the first line must carry the
    decision: the command for Bash, the path for a write, the URL for a fetch."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    try:
        if tool_name == "Bash":
            body = str(inp.get("command") or "")
            desc = str(inp.get("description") or "")
            preview = body + (f"\n\n# {desc}" if desc else "")
        elif tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
            path = str(inp.get("file_path") or inp.get("notebook_path") or "")
            body = str(inp.get("new_string") or inp.get("content")
                       or inp.get("new_source") or "")
            preview = path + ("\n\n" + body if body else "")
        elif tool_name in ("WebFetch", "WebSearch"):
            preview = str(inp.get("url") or inp.get("query") or "")
        else:
            preview = json.dumps(inp, ensure_ascii=False, default=str)
    except Exception:
        preview = "<unpreviewable tool input>"
    return _ask_scrub(preview)[:_ASK_PREVIEW_CHARS]


def _make_ask_gate_cb(session_key: str, ctx: "dict | None"):
    """can_use_tool callback for ask-mode turns. Same dispatcher pattern as the plan gate:
    per-turn state is read from module dicts at CALL time, never captured, because a reused
    live client services later turns with the FIRST turn's closure."""

    async def _ask_gate_cb(tool_name, tool_input, tp_ctx):
        decision_id = None
        try:
            if tool_name in ASK_GATE_READONLY_TOOLS:
                return PermissionResultAllow()
            if _create_pending_tool_cb is None:
                # No cockpit wired (unit tests / standalone) — allow so the engine stays usable.
                return PermissionResultAllow()
            decision_id, fut = _create_pending_tool_cb(
                ctx, session_key, _ask_turn_chat.get(session_key), tool_name,
                _ask_tool_preview(tool_name, tool_input))
            if fut is None:
                return PermissionResultAllow()   # on the project's always-allow list
            try:
                decision = await asyncio.wait_for(fut, ASK_GATE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                msg = f"no operator response within {ASK_GATE_TIMEOUT_SEC}s — denied"
                if _resolve_plan_cb is not None:
                    _resolve_plan_cb(None, decision_id, "timeout", msg)
                print(f"[ask-gate] {session_key}: {tool_name} {decision_id} timed out — denied")
                return PermissionResultDeny(message=msg)
            except asyncio.CancelledError:
                # /stop or client teardown while awaiting — mark the record, re-raise.
                try:
                    if _resolve_plan_cb is not None:
                        _resolve_plan_cb(None, decision_id, "cancelled",
                                         "turn was interrupted while awaiting approval")
                finally:
                    raise
            verdict = (decision or {}).get("decision")
            if verdict in ("allow", "allow_always"):
                return PermissionResultAllow()
            feedback = (decision or {}).get("feedback") or ""
            return PermissionResultDeny(
                message=feedback or f"The operator denied {tool_name}. Do not retry it; "
                                    "continue with what is allowed or explain what you need.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The gate must never wedge a turn on an internal error — deny with the reason so
            # the model can surface it instead of hanging.
            print(f"[ask-gate] {session_key}: gate error {exc!r} — denying {tool_name}")
            return PermissionResultDeny(message=f"ask gate internal error: {exc}")

    return _ask_gate_cb


def _plan_client_fingerprint_ok(ctx: "dict | None", session_key: str, opts,
                                stable_append_hash, effort, memory_mode, account="") -> bool:
    """spec-080 C1 safeguard (spec-082 A reuses it for ask turns): a live client pinned by
    running background children is REUSED on fingerprint mismatch (deferred reconnect,
    spec-069 f9d60d) — but can_use_tool binds at connect time, so a gated turn serviced by a
    reused bypass client would silently run full-auto with no gate. Detect the deferred reuse
    and let the caller abort the turn."""
    try:
        registry = (ctx or {}).get("live_clients", _live_clients)
        entry = registry.get(session_key)
        if entry is None:
            return True
        want = _compute_fingerprint(opts, stable_append_hash=stable_append_hash,
                                    effort=effort, memory_mode=memory_mode, account=account)
        return entry.fingerprint == want
    except Exception:
        return True  # never block on safeguard errors; worst case is pre-fix behavior


def _session_has_live_subagents(session_key: str) -> bool:
    """True if a background sub-agent (Agent tool) for this session is still running.

    spec-069 f9d60d: RC#1 stopped eviction from killing the MAIN turn, but background
    sub-agents are subprocesses of the same live client and outlive the turn — when the
    turn ends (session_key leaves `running`) the client became evict-eligible and a
    disconnect() SIGTERMed the still-working sub-agents (exit 143, false 'failed' monitors,
    lost work). Eviction guards call this to keep the client alive while sub-agents run.
    Best-effort: any failure → False (never block eviction on a predicate error)."""
    try:
        return bool(_has_live_subagents_cb and _has_live_subagents_cb(session_key))
    except Exception:
        return False


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

# Status values the Agent tool's own tool_response reports when the call was synchronous
# (run_in_background=False) and the agent had already finished by the time PostToolUse fires.
# Same vocabulary as the SDK's TaskNotificationStatus / TaskUpdatedStatus (see
# claude_agent_sdk.types) and webapp._TASK_NOTIFICATION_STATUS_MAP's terminal side — kept as a
# separate small map here (engine.py and webapp.py intentionally don't import each other).
# Anything else, notably 'async_launched' (a genuine backgrounded launch), falls through to
# "running" — the caller must still wait for a later completion signal.
_AGENT_TOOL_TERMINAL_STATUS_MAP: dict[str, str] = {
    "completed": "done",
    "failed": "failed",
    "stopped": "stopped",
    "killed": "stopped",
    "cancelled": "stopped",
    "canceled": "stopped",
    "error": "failed",
    "timeout": "failed",
    "timed_out": "failed",
}


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


def _monitor_delta(tool_name, tool_input, tool_response, agent_type, tool_use_id=None):
    """Map a single tool result to a background-monitor delta, or None if irrelevant.

    Returned dict always carries "id"; first-seen deltas also carry kind/label/status.
    spec-069 P3 (RC#3): task-type monitors (Workflow/Monitor) also carry tool_use_id — the stable
    key shared with their completion TaskNotificationMessage (whose task_id is a DIFFERENT internal
    id), so the monitor can be flipped terminal by tool_use_id instead of the mismatching task_id.
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
                    "tool_use_id": tool_use_id, "agent": agent_type}

        if tool_name == "Workflow":
            tid = _rget(tr, "taskId")
            if not tid:
                return None
            return {"id": str(tid), "kind": "workflow", "status": "running",
                    "label": str(_rget(tr, "workflowName") or ti.get("name") or "workflow")[:200],
                    "tool_use_id": tool_use_id, "agent": agent_type}

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

        if tool_name == "Agent":
            # spec-069 P3-B: register a sub-agent monitor keyed by its agentId so that
            # RC#3's _reconcile_monitors_from_transcript can flip it to done automatically
            # when the <task-notification> for agentId arrives in the session transcript.
            # The hook's tool_response is a DICT — e.g. {'isAsync': True, 'status':
            # 'async_launched', 'agentId': '<id>', 'description': '<task>', ...}. Read agentId
            # straight from it; only fall back to a text-form regex if the dict lacks the key.
            tr = tool_response
            agent_id = _rget(tr, "agentId")
            if not agent_id:
                m = re.search(r"agentId:\s*([A-Za-z0-9]+)", _tool_response_to_str(tr) or "")
                agent_id = m.group(1) if m else None
            if not agent_id:
                return None
            label = str(ti.get("description") or _rget(tr, "description") or
                        ti.get("subagent_type") or "agent")[:200]
            # An Agent call made with run_in_background=False can finish and deliver its
            # FULL result inline, in this SAME PostToolUse event — the tool_response's own
            # `status` is already terminal ('completed'/'failed'/...), not 'async_launched'.
            # No <task-notification> is ever emitted for it afterwards (there is nothing left
            # to defer), so forcing "running" here left the monitor stuck until the sweeper's
            # 900s staleness fallback eventually guessed it dead and reported the wrong status.
            # Map a terminal tool_response status straight through so same-turn completions
            # flip immediately; an unrecognized value (notably 'async_launched', a genuine
            # backgrounded launch) falls through to "running" as before.
            raw_status = str(_rget(tr, "status") or "").strip().lower()
            status = _AGENT_TOOL_TERMINAL_STATUS_MAP.get(raw_status, "running")
            return {"id": str(agent_id), "kind": "agent", "status": status,
                    "label": label, "agent": ti.get("subagent_type")}
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
                    delta = _monitor_delta(tool_name, tool_input, tool_response, agent_type, tool_use_id)
                    if delta:
                        _monitor_update_cb(session_key, delta)
            except Exception:
                pass  # monitor tracking is best-effort — never break a turn

            # spec-080 backstop: remember the last plan-file Write for this session so the
            # ExitPlanMode gate can read the plan body even if input.plan/planFilePath come
            # up empty (V2 file-based flow; sub-agent Writes are visible here too).
            try:
                if tool_name == "Write" and isinstance(tool_input, dict):
                    _fp = str(tool_input.get("file_path") or "")
                    if _fp.startswith(str(Path.home() / ".claude" / "plans")):
                        _plan_write_paths[session_key] = _fp
            except Exception:
                pass

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


# ─────────────────────────── Root-fix A2: bundle-grep guard (PreToolUse) ───────────────────────
# A wide-context grep (`.{0,500}TOKEN.{0,500}`) against a one-line minified bundle makes the
# grep engine buffer the whole file per match — 3-6 GB RSS in seconds, enough to OOM-kill the
# entire service cgroup (dmesg: a single CLI subprocess at 5.77/6.06 GB in 2 of 10 recorded
# OOM kills). Deny the known-fatal shape and tell the model the safe alternative.
_WIDE_CTX_RE = re.compile(r"\.\{\s*\d*\s*,\s*(\d{2,})\s*\}")
_BUNDLE_PATH_RE = re.compile(r"node_modules|_bundled|\bdist/|\.min\.js|\bbundle", re.I)
_GREP_TOOL_RE = re.compile(r"\b(grep|ugrep|ug|rg)\b")


def _is_wide_bundle_grep(command: str) -> bool:
    """True for a grep-family command with a wide `.{...,N}` context regex aimed at
    bundle-ish paths (node_modules / _bundled / dist / minified). Pure, unit-testable."""
    try:
        if not _GREP_TOOL_RE.search(command):
            return False
        m = _WIDE_CTX_RE.search(command)
        if not m or int(m.group(1)) < 50:
            return False
        return bool(_BUNDLE_PATH_RE.search(command))
    except Exception:
        return False


async def _bundle_grep_guard_hook(
    hook_input: dict,
    tool_use_id: "str | None",
    context: "HookContext",
) -> dict:
    """PreToolUse guard for Bash: deny the known OOM-fatal wide-context-grep-on-bundle shape.

    Never raises; anything unexpected falls through to an empty (allow) output."""
    try:
        tool_input = hook_input.get("tool_input") if isinstance(hook_input, dict) else None
        command = (tool_input or {}).get("command", "") if isinstance(tool_input, dict) else ""
        if command and _is_wide_bundle_grep(command):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Wide-context grep on a minified bundle buffers gigabytes per match "
                        "and has OOM-killed this whole service before. Slice the file instead "
                        "(python -c / head -c / dd) or grep without the .{N,M} context window."
                    ),
                }
            }
    except Exception:
        pass
    return {}


# ─────────────────────── Irreversible-command guard (PreToolUse) ───────────────────────────────
# Every engine connection runs with permission_mode="bypassPermissions" (full-auto: no chat
# confirmation, no card confirmation). Per Anthropic's docs a PreToolUse hook that returns
# "deny" still blocks the tool call in that mode (this is the same mechanism the bundle-grep
# guard above already relies on in production) — see
# https://code.claude.com/docs/en/permissions#extend-permissions-with-hooks
# ("Hook decisions don't bypass permission rules... A blocking hook also takes precedence over
# allow rules") and https://code.claude.com/docs/en/permission-modes#skip-all-checks-with-bypasspermissions-mode.
# This is a last-resort circuit breaker for a hard-coded, minimal list of commands that are
# irreversible for the HOST (not for the project's own working directory — a plain `rm -rf` or
# `git push` inside the repo must keep working un-gated). Extend the list at runtime with
# DENY_COMMANDS_EXTRA (comma/newline-separated regexes, matched against the full command).
_SUBCMD_SPLIT_RE = re.compile(r"&&|\|\||\|&|;|&|\||\n")


def _split_subcommands(command: str) -> "list[str]":
    """Split a Bash command on the same shell operators Claude Code's own permission-rule
    matcher uses to isolate subcommands (see the "Compound commands" section of the permissions
    doc), so a dangerous shape hidden after `&&`/`;`/`|` still gets classified on its own."""
    return _SUBCMD_SPLIT_RE.split(command)


# A root/home path used as a whole argument (not a prefix of a longer path): "/", "~", "$HOME",
# "${HOME}", or the same followed by a "*" glob that would wipe everything under it ("/*",
# "~/*", "$HOME/*"). The lookbehind requires the token to start right after whitespace (so a
# trailing slash on a normal absolute path, e.g. "/home/alice/myproject/tmp/", never matches) and
# the lookahead requires nothing else glued onto it (so "/etc" or "$HOME/tmp" never match).
# A trailing slash is part of the same shape: `rm -rf ~/` wipes the home directory just
# as `rm -rf ~` does, and an agent is more likely to type the slashed form.
_BARE_ROOT_OR_HOME = r"(?<=\s)(?:/|~|\$\{?HOME\}?)(?:/\*?|\*)?(?![^\s;&|])"

_RM_ROOT_HOME_RE = re.compile(
    r"\brm\s+(?=[^;&|\n]*-[A-Za-z-]*[rR])(?=[^;&|\n]*-[A-Za-z-]*f)[^;&|\n]*?" + _BARE_ROOT_OR_HOME
)
_CHMOD_ROOT_RE = re.compile(
    r"\bchmod\s+(?=[^;&|\n]*-[A-Za-z-]*R)(?=[^;&|\n]*(?:777|000|a[+=]rwx))[^;&|\n]*?"
    + _BARE_ROOT_OR_HOME
)
_GIT_HARD_RESET_RE = re.compile(r"\bgit\s+reset\s+--hard\b")
_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")
_GIT_FORCE_FLAG_RE = re.compile(r"(?:--force(?:-with-lease)?\b|(?<!\S)-f\b)")
_GIT_BRANCH_RE = re.compile(r"\b(?:master|main)\b")
_GIT_PUSH_BARE_FORCE_RE = re.compile(r"^git\s+push\s+(?:--force(?:-with-lease)?|-f)\s*$")
_GIT_PUSH_REMOTE_FORCE_RE = re.compile(r"^git\s+push\s+\S+\s+(?:--force(?:-with-lease)?|-f)\s*$")
_DOCKER_PRUNE_RE = re.compile(r"\bdocker\s+system\s+prune\b")
_MKFS_RE = re.compile(r"\bmkfs(?:\.\w+)?\b")
_DD_TO_DEVICE_RE = re.compile(r"\bdd\b[^;&|\n]*\bof=/dev/(?!null\b|zero\b)")
_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&[^}]*\}\s*;\s*:")
_SSH_PATH_RE = re.compile(r"(?:~|\$\{?HOME\}?)/\.ssh\b")
_SSH_MUTATION_VERB_RE = re.compile(r"\b(?:rm|rmdir|mv|shred|truncate|chown)\b|>>?|\btee\b|sed\s+-i")
# `chmod 600 ~/.ssh/id_ed25519` is documented routine (it LOCKS the key down), so chmod is
# not a mutation verb above. Only a mode that opens the key to group/other is denied.
_SSH_CHMOD_OPEN_RE = re.compile(r"\bchmod\b[^;&|\n]*?(?:[0-7][0-7][1-7]|[ugoa]*[+=][^\s]*[rwx][^\s]*)[^;&|\n]*?(?:~|\$\{?HOME\}?)/\.ssh")


def _is_force_push_to_protected_branch(segment: str) -> bool:
    """True for a `git push --force`/`-f`/`--force-with-lease` naming master/main, or an
    unqualified force-push with no explicit ref — this project's own workflow (CLAUDE.md:
    "everything in one branch, master") means an unqualified force-push targets master too."""
    if not _GIT_PUSH_RE.search(segment):
        return False
    if not _GIT_FORCE_FLAG_RE.search(segment):
        return False
    if _GIT_BRANCH_RE.search(segment):
        return True
    stripped = segment.strip()
    return bool(
        _GIT_PUSH_BARE_FORCE_RE.match(stripped) or _GIT_PUSH_REMOTE_FORCE_RE.match(stripped)
    )


def _is_ssh_dir_mutation(segment: str) -> bool:
    """True for a command that writes to or removes something under ~/.ssh or $HOME/.ssh
    (rm/mv/chmod/redirection/etc). Plain reads (cat, ls, ssh-add) are left alone."""
    return bool(_SSH_PATH_RE.search(segment)) and bool(_SSH_MUTATION_VERB_RE.search(segment))


def _compile_extra_deny_patterns() -> "list[re.Pattern]":
    """DENY_COMMANDS_EXTRA: operator-supplied regexes (comma or newline separated), each matched
    against the full command. An invalid regex is skipped, never crashes startup."""
    raw = os.environ.get("DENY_COMMANDS_EXTRA", "")
    patterns: "list[re.Pattern]" = []
    for line in re.split(r"[\n,]", raw):
        line = line.strip()
        if not line:
            continue
        try:
            patterns.append(re.compile(line))
        except re.error:
            continue
    return patterns


_DENY_COMMAND_PATTERNS_EXTRA = _compile_extra_deny_patterns()


def _classify_dangerous_command(command: str) -> "str | None":
    """Pure classifier: returns a human-readable deny reason for a Bash command matching one
    of the hard-coded irreversible-for-the-host shapes, or None if the command is fine.
    Never raises. Unit-tested directly (see tests/test_deny_commands.py)."""
    try:
        if _FORK_BOMB_RE.search(command):
            return "fork bomb: exhausts host processes/memory"
        for segment in _split_subcommands(command):
            seg = segment.strip()
            if not seg:
                continue
            if _RM_ROOT_HOME_RE.search(seg):
                return "rm -rf targeting the filesystem root or $HOME wipes the host"
            if _CHMOD_ROOT_RE.search(seg):
                return "chmod -R on the filesystem root can lock out the whole host"
            if _is_force_push_to_protected_branch(seg):
                return "git push --force into master/main rewrites shared history irreversibly"
            if _GIT_HARD_RESET_RE.search(seg):
                return "git reset --hard discards uncommitted work irreversibly"
            if _is_ssh_dir_mutation(seg):
                return "writing to or deleting ~/.ssh can lock out or leak host SSH access"
            if _SSH_CHMOD_OPEN_RE.search(seg):
                return "opening ~/.ssh permissions to group/other exposes the host's SSH keys"
            if _DOCKER_PRUNE_RE.search(seg):
                return "docker system prune deletes every unused image/volume/network on the host"
            if _MKFS_RE.search(seg):
                return "mkfs formats a filesystem and destroys existing data"
            if _DD_TO_DEVICE_RE.search(seg):
                return "dd writing to a raw block device can overwrite the host disk"
            if _FORK_BOMB_RE.search(seg):
                return "fork bomb: exhausts host processes/memory"
        for extra in _DENY_COMMAND_PATTERNS_EXTRA:
            if extra.search(command):
                return f"blocked by an operator-defined DENY_COMMANDS_EXTRA pattern ({extra.pattern!r})"
    except Exception:
        return None
    return None


async def _dangerous_command_guard_hook(
    hook_input: dict,
    tool_use_id: "str | None",
    context: "HookContext",
) -> dict:
    """PreToolUse guard for Bash: deny a hard-coded, minimal list of commands that are
    irreversible for the HOST (rm -rf on / or $HOME, git push --force to master/main,
    git reset --hard, ~/.ssh writes/deletes, docker system prune, mkfs, dd onto a raw device,
    a fork bomb), even under permission_mode="bypassPermissions" where there is no chat/card
    confirmation to catch it. Never raises; anything unexpected falls through to allow."""
    try:
        tool_input = hook_input.get("tool_input") if isinstance(hook_input, dict) else None
        command = (tool_input or {}).get("command", "") if isinstance(tool_input, dict) else ""
        if command:
            reason = _classify_dangerous_command(command)
            if reason:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Blocked: {reason}. This shape is on the irreversible-for-the-host "
                            "deny list and cannot run even in full-auto mode."
                        ),
                    }
                }
    except Exception:
        pass
    return {}


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

def _compute_fingerprint(
    opts: "ClaudeAgentOptions",
    *,
    stable_append_hash: str = "",
    effort: str = "",
    memory_mode: str = "",
    account: str = "",
) -> str:
    """Hash the subset of opts fields that are immutable once a ClaudeSDKClient is connected.

    A fingerprint mismatch (e.g. /model switch, different system_prompt preset, effort change,
    or stable-append change such as ultracode/conductor/browser toggle) means we must evict the
    live entry and reconnect rather than reusing the old subprocess.

    Fields explicitly included:
    - cwd, model, permission_mode, setting_sources, disallowed_tools: hard session identity
    - system_prompt type/preset: preset selection
    - stable_append_hash: SHA-256 of the STABLE append pieces (conductor/ultracode/browser/images/
      files/board-PROTOCOL-header — everything except the volatile per-turn board-card snapshot).
      Callers compute this from stable_append_pieces and pass it in so that toggling ultracode,
      conductor, or browser forces a reconnect, while a mere board-card content change does not.
    - effort: thinking level (low/medium/high/xhigh/max). Changing it forces a reconnect because
      the subprocess honours effort at launch.

    Fields deliberately excluded: resume (session_id), env (per-turn TG_CHAT_ID etc.),
    agents roster (can't change the subprocess mid-session anyway), volatile board-card snapshot.
    """
    parts = [
        str(getattr(opts, "cwd", "")),
        str(getattr(opts, "model", "")),
        str(getattr(opts, "permission_mode", "")),
        str(sorted(getattr(opts, "setting_sources", []) or [])),
        str(sorted(getattr(opts, "disallowed_tools", []) or [])),
        # spec-078: per-project skill filter + opted-in plugins are launch-immutable (applied at
        # initialize), so a change must evict the live client for the new skill set to load.
        str(getattr(opts, "skills", None)),
        str(sorted((p or {}).get("path", "") for p in (getattr(opts, "plugins", []) or []))),
        # Capture the stable identity of the system_prompt (preset type/name) without the
        # per-turn append text — we don't want every TG nudge update to force a reconnect.
        str((getattr(opts, "system_prompt", None) or {}).get("type", "")),
        str((getattr(opts, "system_prompt", None) or {}).get("preset", "")),
        # FIX 2: include stable append content hash and effort level so toggling
        # ultracode/conductor/browser or changing the think-mode ladder forces a fresh client.
        stable_append_hash,
        effort,
        # spec-078 Phase 3a: the memory mode rides in `env`, which is deliberately EXCLUDED above
        # (it carries per-turn noise like TG_CHAT_ID). Pass it in explicitly — the CLI reads
        # CLAUDE_CODE_DISABLE_AUTO_MEMORY at launch, so flipping the mode must evict the live
        # subprocess or the project keeps the old brain until the next idle eviction.
        memory_mode,
        # The subscription this run is bound to. It rides in `env` (CLAUDE_CONFIG_DIR), which is
        # EXCLUDED above, and the CLI reads it at launch — so without this the operator could
        # switch accounts and a live client would keep burning the OLD subscription with no sign.
        account,
        # spec-058 v2: the --settings payload (native ultracode switch) is launch-immutable too.
        str(getattr(opts, "settings", "") or ""),
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
    # spec-071: stop the between-turns drain before disconnecting the subprocess.
    if entry.drain_task is not None and not entry.drain_task.done():
        entry.drain_task.cancel()
    # Disconnect the subprocess.
    try:
        await asyncio.wait_for(entry.client.disconnect(), timeout=10)
    except Exception as exc:
        print(f"[live-client] evict {session_key}: disconnect failed ({exc!r}), force-dropping")


async def rewind_conversation(
    cwd: str,
    resume_session_id: str,
    rewind_at_uuid: str,
    rewind_drop_turn_uuid: str,
    *,
    timeout: float = 30.0,
) -> str:
    """Fork `resume_session_id` at `rewind_at_uuid`, discarding `rewind_drop_turn_uuid`
    onward, and return the NEW forked session id.

    docs/internal/sdk-feature-audit/04-session-rewind.md is the design doc. Summary of the
    load-bearing decisions:

    - `fork_session=True` is MANDATORY, never operator-configurable. Paired with
      `resume_session_at` it produces a brand-new session id and leaves the source
      transcript file untouched on disk — the bare (non-fork) combination is UNVERIFIED
      (unknown whether it truncates the original file in place) and must never be used.
    - `resume_drops_turn` is always set alongside `resume_session_at` (never skipped): it is
      the SDK's own defense against silently discarding more than the caller intended (a
      queued message or task notification absorbed mid-turn). A violation raises ProcessError
      with "Resume rejected by --resume-drops-turn:" in the message — deterministic, never
      retry the same pair (see _rewind_refused_hint).
    - Connect-only, no prompt is ever sent: this never invokes the model and costs nothing.
      Verified against the bundled CLI binary (venv's claude_agent_sdk/_bundled/claude, via
      `strings`): the init SystemMessage's `data` dict is built as
      `{type:"system",subtype:"init",...,session_id:b.sessionId,...}` — session_id is a
      top-level key, populated at connect time BEFORE any query is sent, and the
      resume-drops-turn validation happens at CLI load time (also before any query). So the
      forked session id can be read off the very first message with zero turns/tokens spent.
      This confirms (rather than merely assumes, as the audit left it) that "rewind-only, no
      prompt" is implementable — it does not need to be folded into run_engine()'s
      prompt-requiring turn machinery, which would have meant touching the hot shared
      options-builder for a fundamentally different (query-less) connect.

    Raises:
        ProcessError — connect-time refusal (see _rewind_refused_hint) or any other CLI
            subprocess failure.
        asyncio.TimeoutError — no init message arrived within `timeout` seconds.
        RuntimeError — the connection closed, or the init message carried no session_id,
            before an init message was observed (should not happen against a conforming CLI;
            surfaced rather than silently treating the rewind as failed-but-unclear).
    """
    stderr_lines: list = []
    opts = ClaudeAgentOptions(
        cwd=cwd,
        resume=resume_session_id,
        resume_session_at=rewind_at_uuid,
        resume_drops_turn=rewind_drop_turn_uuid,
        fork_session=True,
        stderr=stderr_lines.append,
    )

    async def _wait_for_init(client: "ClaudeSDKClient") -> str:
        async for msg in client.receive_messages():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                sid = (msg.data or {}).get("session_id")
                if sid:
                    return str(sid)
                raise RuntimeError("rewind: init message carried no session_id")
        raise RuntimeError("rewind: connection closed before an init message arrived")

    try:
        async with ClaudeSDKClient(options=opts) as client:
            return await asyncio.wait_for(_wait_for_init(client), timeout=timeout)
    except ProcessError as exc:
        # Re-raise with the captured stderr folded in — by default the SDK does not pipe
        # stderr into the exception message (only "Check stderr output for details"), which
        # would silently swallow the literal "Resume rejected by --resume-drops-turn:" text
        # _rewind_refused_hint needs to match on. Passing stderr=stderr_lines.append above is
        # what makes this text reach us at all.
        if stderr_lines:
            raise ProcessError(f"CLI subprocess failed during rewind connect: {exc}",
                                exit_code=exc.exit_code,
                                stderr="\n".join(stderr_lines)[-4000:]) from exc
        raise


def _cgroup_mem_fraction() -> "float | None":
    """Return memory.current / memory.max for this process's cgroup, or None.

    None means "no usable signal" — not in a cgroup v2 hierarchy, no limit set
    (memory.max == "max"), or the files are unreadable. Callers must treat None as
    "guard inactive" and never as 0.0, so a container without limits behaves exactly
    as before this guard existed.
    """
    try:
        # cgroup v2: /proc/self/cgroup is a single "0::<path>" line.
        rel = ""
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    rel = parts[2]
                    break
        if not rel:
            return None
        base = Path("/sys/fs/cgroup") / rel.lstrip("/")
        limit_raw = (base / "memory.max").read_text(encoding="utf-8").strip()
        if limit_raw == "max":
            return None
        limit = int(limit_raw)
        if limit <= 0:
            return None
        current = int((base / "memory.current").read_text(encoding="utf-8").strip())
        return current / limit
    except Exception:
        return None


async def _enforce_memory_headroom(registry: dict, ctx: "dict | None", running_dict: dict) -> None:
    """LRU-evict idle live clients while the cgroup sits above LIVE_CLIENT_MEM_GUARD.

    Each live client is a CLI subprocess, so the registry is the one knob the cockpit has on
    its own memory. Busy clients (turn in flight or live sub-agents) are never evicted — the
    same rule the LIVE_CLIENT_MAX loop follows; if everything is busy we log and continue,
    because refusing the turn would be worse than the risk of one more subprocess.
    """
    if LIVE_CLIENT_MEM_GUARD <= 0:
        return
    frac = _cgroup_mem_fraction()
    if frac is None:
        return
    while frac > LIVE_CLIENT_MEM_GUARD:
        idle_keys = [k for k in registry
                     if k not in running_dict and not _session_has_live_subagents(k)]
        if not idle_keys:
            print(f"[live-client] memory at {frac:.0%} of cgroup limit but every client is busy "
                  f"— cannot free headroom, proceeding")
            return
        oldest_key = min(idle_keys, key=lambda k: registry[k].last_used)
        print(f"[live-client] memory guard: {frac:.0%} of cgroup limit — evicting idle {oldest_key}")
        await _evict_live_client(oldest_key, ctx)
        new_frac = _cgroup_mem_fraction()
        # A disconnect frees memory asynchronously (the child has to actually exit), so a
        # reading that did not move is expected; stop rather than evict the whole registry
        # over one slow teardown.
        if new_frac is None or new_frac >= frac:
            return
        frac = new_frac


async def _get_or_create_live_client(
    ctx: "dict | None",
    session_key: str,
    opts: "ClaudeAgentOptions",
    *,
    ephemeral: bool,
    stable_append_hash: str = "",
    effort: str = "",
    memory_mode: str = "",
    account: str = "",
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
    fingerprint = _compute_fingerprint(opts, stable_append_hash=stable_append_hash, effort=effort,
                                       memory_mode=memory_mode, account=account)

    existing = registry.get(session_key)
    if existing is not None:
        if existing.fingerprint != fingerprint and not _session_has_live_subagents(session_key):
            print(f"[live-client] fingerprint changed for {session_key} — evicting and reconnecting")
            await _evict_live_client(session_key, ctx)
            # Fall through to create a new entry.
        else:
            # Reuse: cancel the pending idle countdown and refresh the timestamp.
            # spec-069 f9d60d: when the fingerprint changed but background sub-agents are
            # still running, we deliberately REUSE the old client (deferring the option
            # change) instead of evicting — disconnect() would SIGTERM those sub-agents
            # mid-flight (exit 143). The new options take effect on the next turn, after
            # the sub-agents finish.
            if existing.fingerprint != fingerprint:
                print(f"[live-client] fingerprint changed for {session_key} but sub-agents are live — reusing client, deferring option change")
            if existing.idle_task is not None and not existing.idle_task.done():
                existing.idle_task.cancel()
            existing.last_used = time.monotonic()
            existing.idle_task = _schedule_idle_eviction(session_key, ctx)
            return existing.client

    # ── Enforce LIVE_CLIENT_MAX via LRU ──────────────────────────────────────────────────────────
    # spec-069 P1 (RC#1): never LRU-evict a client whose turn is in-flight — that would SIGTERM a
    # running orchestration mid-turn. Consider only idle entries; if every client is busy, allow a
    # temporary overflow rather than kill a live turn (the next idle turn-end brings the count down).
    _running_dict = ctx.get("running", running)
    while len(registry) >= LIVE_CLIENT_MAX:
        # spec-069 P1 (RC#1) + f9d60d: a client is "busy" if its turn is in-flight OR it still
        # has background sub-agents running — evicting either would SIGTERM live work.
        idle_keys = [k for k in registry
                     if k not in _running_dict and not _session_has_live_subagents(k)]
        if not idle_keys:
            print(f"[live-client] registry at {LIVE_CLIENT_MAX} but all clients busy — deferring LRU evict")
            break
        oldest_key = min(idle_keys, key=lambda k: registry[k].last_used)
        print(f"[live-client] LRU evict {oldest_key} (registry full at {LIVE_CLIENT_MAX})")
        await _evict_live_client(oldest_key, ctx)

    # ── Free memory headroom before adding another subprocess ────────────────────────────────────
    # LIVE_CLIENT_MAX bounds the COUNT; this bounds the actual footprint. Without it the cgroup
    # reaches MemoryMax, the kernel OOM-kills a `claude` child mid-turn and systemd restarts the
    # service under the operator (observed twice on ops, see LIVE_CLIENT_MEM_GUARD).
    await _enforce_memory_headroom(registry, ctx, _running_dict)

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
    # spec-071: service the stream from the start — run_engine pauses it around the turn.
    _start_drain(entry, ctx)
    print(f"[live-client] created entry for {session_key} (total: {len(registry)})")
    return client


def _schedule_idle_eviction(session_key: str, ctx: "dict | None") -> "asyncio.Task":
    """Create (and return) an asyncio Task that evicts session_key after LIVE_CLIENT_TTL_SEC of
    genuine idleness.

    The task is a module-level detached task — NOT tied to any turn coroutine — so it
    survives after the turn generator is exhausted.

    spec-069 P1 (RC#1): the TTL measures IDLE time, never total turn duration. If the TTL lapses
    while a turn for this session is still in-flight (session_key present in `running`), eviction is
    DEFERRED — the countdown restarts and re-checks. This kills the old bug where a long
    orchestration that legitimately ran past the TTL was SIGTERMed mid-turn and died silently.
    """
    _running_dict = (ctx or {}).get("running", running)

    async def _idle_waiter():
        deferred_sec = 0
        try:
            while True:
                await asyncio.sleep(LIVE_CLIENT_TTL_SEC)
                if session_key in _running_dict or _session_has_live_subagents(session_key):
                    # A turn is still running, OR background sub-agents are still working —
                    # never evict a live client while either holds live work. Evicting would
                    # disconnect() → SIGTERM the sub-agent subprocesses. (RC#1 + f9d60d)
                    # spec-071: but a pin that outlives any legitimate run is a stuck flag
                    # (observed: a dead turn held 'in-flight' 14 h and pinned the client all
                    # night). Past LIVE_CLIENT_MAX_PIN_SEC, presume stuck: clear the flag and
                    # evict loudly instead of deferring forever.
                    deferred_sec += LIVE_CLIENT_TTL_SEC
                    if deferred_sec >= LIVE_CLIENT_MAX_PIN_SEC:
                        print(f"[live-client] {session_key}: pinned {deferred_sec}s (> {LIVE_CLIENT_MAX_PIN_SEC}s cap) — presuming stuck, force-evicting")
                        # spec-080: a plan approval parked past the pin cap dies with the
                        # client — resolve it as cancelled so the record and the cockpit card
                        # do not stay 'awaiting' forever (the callback's CancelledError path
                        # also fires on disconnect; resolve first so the reason is specific).
                        try:
                            if _pending_plan_id_cb is not None and _resolve_plan_cb is not None:
                                _ppid = _pending_plan_id_cb(session_key)
                                if _ppid:
                                    _resolve_plan_cb(None, _ppid, "cancelled",
                                                     "session force-evicted after the "
                                                     f"{LIVE_CLIENT_MAX_PIN_SEC}s pin cap — resubmit the plan turn")
                        except Exception:
                            pass
                        _running_dict.pop(session_key, None)
                        await _evict_live_client(session_key, ctx)
                        return
                    print(f"[live-client] TTL lapsed for {session_key} but work is in-flight — deferring eviction")
                    continue
                print(f"[live-client] idle TTL expired for {session_key} — evicting")
                await _evict_live_client(session_key, ctx)
                return
        except asyncio.CancelledError:
            pass  # Normal: cancelled when the entry is reused or manually evicted.

    return asyncio.ensure_future(_idle_waiter())


# ─────────────────────────── spec-071: between-turns stream drain ─────────────────────────────
#
# The SDK's internal reader answers hook/control RPCs itself but pushes every regular message
# into a BOUNDED buffer (anyio memory stream, max_buffer_size=100 in query.py). Between turns
# nothing consumed that buffer: it filled, the reader blocked, the CLI's stdout pipe backed up
# and the whole CLI stalled — background sub-agents degraded to ~1 tool round per ~10 minutes
# and completion notifications were delivered only at the START of the next operator turn
# ("answers appear the moment I send a message"; diagnosis 2026-07-05).
#
# The drain owns the stream while no engine turn is active:
#   - keeps the pipe moving (sub-agents run at full speed between turns);
#   - flips monitors on TaskNotificationMessage in real time — webapp._monitor_update turns the
#     running→terminal transition into the event-driven auto-continue wake (spec-069 P2 v2);
#   - surfaces autonomous CLI turns (the CLI natively re-wakes the model on task-notifications
#     when the stream is serviced) via bg_text / bg_turn_end bus events so the cockpit hydrates.
#
# Single-consumer discipline: run_engine's live branch STOPS the drain before client.query()
# and restarts it in its finally — exactly one reader pulls from the SDK buffer at any time.

LIVE_CLIENT_DRAIN: bool = os.getenv("LIVE_CLIENT_DRAIN", "1") not in ("0", "false", "False")
# Hard bound on how long a live client may stay pinned by an "in-flight" turn / sub-agent
# monitors before it is presumed stuck and force-evicted (observed: a dead turn held the
# in-flight flag for 14 h and pinned the client all night). Raise for legitimately longer
# background work.
LIVE_CLIENT_MAX_PIN_SEC: int = int(os.getenv("LIVE_CLIENT_MAX_PIN_SEC", str(4 * 3600)))

# Terminal statuses a task-notification may carry → monitor status. Superset map shared by the
# in-turn handler and the drain (the old in-turn map lacked killed/cancelled — flips were lost).
_NOTIFICATION_STATUS_MAP: dict = {
    "completed": "done", "failed": "failed", "stopped": "stopped",
    "killed": "stopped", "cancelled": "stopped", "canceled": "stopped",
    "error": "failed", "timeout": "failed", "timed_out": "failed",
}


def _notification_monitor_delta(msg) -> "dict | None":
    """Map a TaskNotificationMessage OR TaskUpdatedMessage to a terminal monitor delta.

    Per SDK docs, a background task's terminal state can arrive ONLY as a TaskUpdatedMessage
    with a terminal patch.status (e.g. TaskStop reports status="killed" there and the matching
    notification is sometimes suppressed) — consumers must clear on EITHER message. Never raises."""
    try:
        task_id = getattr(msg, "task_id", None)
        if not task_id:
            return None
        status = getattr(msg, "status", None)
        if not status and isinstance(getattr(msg, "patch", None), dict):
            status = msg.patch.get("status")
        mapped = _NOTIFICATION_STATUS_MAP.get(str(status or "").lower())
        if not mapped:
            return None
        return {"id": str(task_id), "tool_use_id": getattr(msg, "tool_use_id", None),
                "status": mapped}
    except Exception:
        return None


async def _drain_between_turns(entry: "_LiveEntry", ctx: "dict | None") -> None:
    """Consume the live client's SDK stream until cancelled (see block comment above)."""
    session_key = entry.session_key
    client = entry.client
    bg_open = False  # an autonomous CLI turn is being surfaced as a background run
    try:
        async for msg in client.receive_messages():
            if isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
                delta = _notification_monitor_delta(msg)
                if delta and _monitor_update_cb:
                    print(f"[live-drain] {session_key}: task-notification id={delta['id']} → {delta['status']}")
                    try:
                        _monitor_update_cb(session_key, delta, only_existing=True)
                    except Exception:
                        pass
                continue
            # Sub-agent traffic (parent_tool_use_id set) — never surfaced in the chat lane;
            # the monitors panel is fed by the PostToolUse hook + transcript sweeper instead.
            if getattr(msg, "parent_tool_use_id", None):
                continue
            if isinstance(msg, UserMessage):
                # A peer/channel delivery that landed while NO turn was running. The mid-turn
                # case is covered in _process_messages(); without this branch the message hits
                # no if/elif below and vanishes — no log, no bus event, no timeline row. And
                # between turns is the LIKELIER arrival window: a peer message is injected by
                # another session, not triggered by the operator sending anything.
                _peer_ev = _peer_message_event(msg)
                if _peer_ev is not None:
                    print(f"[live-drain] {session_key}: {_peer_ev['kind']} message from "
                          f"{_peer_ev['sender']} ({len(_peer_ev['text'])} chars)")
                    if _bus_publish_cb:
                        try:
                            # kind= routes the bus event in the cockpit; the origin's own kind
                            # (peer-message / channel / ...) rides along as peer_kind so it is
                            # not clobbered by the routing key.
                            _bus_publish_cb(session_key, {**_peer_ev, "kind": "peer_message",
                                                          "peer_kind": _peer_ev["kind"]})
                        except Exception:
                            pass  # never let a publish error kill the drain
                continue
            if isinstance(msg, AssistantMessage):
                # spec-063 Stage 2a: an autonomous CLI turn (native task-notification wake)
                # is a first-class background run — streamed live via the webapp callback
                # (kind:run_start source:'bg' → seq-tagged text → run_end + web push).
                # The run opens on the FIRST top-level assistant message, text or not: a wake
                # that spends minutes in tool calls before its first sentence used to be
                # invisible the whole time (a 4-minute Workflow relaunch on 2026-09-01 left no
                # run bracket at all) and the cockpit read the CLI as idle. spec-088.
                texts = [blk.text for blk in msg.content
                         if isinstance(blk, TextBlock) and blk.text.strip()]
                if _bg_run_cb:
                    try:
                        if not bg_open:
                            _bg_run_cb(session_key, "start")
                            bg_open = True
                        for _t in texts:
                            _bg_run_cb(session_key, "text", _t)
                    except Exception:
                        pass
            elif isinstance(msg, ResultMessage):
                print(f"[live-drain] {session_key}: autonomous turn finished")
                if bg_open and _bg_run_cb:
                    try:
                        _bg_run_cb(session_key, "end")
                    except Exception:
                        pass
                bg_open = False
            elif isinstance(msg, SystemMessage):
                # Task lifecycle (TaskStarted/TaskProgress) and newer subtypes
                # (background_tasks_changed, status, …) reach the drain too. Until spec-088
                # surfaces them, log each distinct one once per process so the blind spot is
                # at least visible in the journal instead of vanishing with no trace.
                _st = f"drain:{type(msg).__name__}:{getattr(msg, 'subtype', None) or '?'}"
                if _st not in _UNKNOWN_SUBTYPES_SEEN:
                    _UNKNOWN_SUBTYPES_SEEN.add(_st)
                    print(f"[live-drain] {session_key}: unhandled {_st} between turns — logged once")
    except asyncio.CancelledError:
        # Normal pause path (turn starting / eviction) — close a half-open background run
        # so the cockpit strip doesn't dangle.
        if bg_open and _bg_run_cb:
            try:
                _bg_run_cb(session_key, "end")
            except Exception:
                pass
        raise
    except Exception as exc:
        # Close a half-open background run here too. The cockpit now treats a live bg run as
        # "CLI busy" (it gates /chat and the chat-queue drain), so leaking one on a reader
        # error would wedge the project as permanently busy.
        if bg_open and _bg_run_cb:
            try:
                _bg_run_cb(session_key, "end")
            except Exception:
                pass
        print(f"[live-drain] {session_key}: reader stopped ({exc!r})")


def _start_drain(entry: "_LiveEntry", ctx: "dict | None") -> None:
    """Start (or restart) the between-turns drain for a live entry. Idempotent."""
    if not LIVE_CLIENT_DRAIN:
        return
    if entry.drain_task is not None and not entry.drain_task.done():
        return
    entry.drain_task = asyncio.ensure_future(_drain_between_turns(entry, ctx))


async def _stop_drain(entry: "_LiveEntry") -> None:
    """Cancel the drain and wait for it to release the stream. Safe to call when absent.

    anyio memory-stream receive is cancellation-safe (a cancelled receive never consumes an
    item), so no message is lost across the pause/resume boundary."""
    task = entry.drain_task
    entry.drain_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


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
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        cwd=_OPS_SCRATCH_CWD,  # scratch dir: transcript never pollutes project session list
        system_prompt=_RECONCILE_SYSTEM,  # plain string — no tools, no preset
        allowed_tools=[],   # no tools — read-only classification pass
        disallowed_tools=[],
        # An internal helper must never write to a project's memory wiki. allowed_tools=[] blocks
        # Edit/Write, but the CLI's own memory-extraction pass uses internal tooling that the
        # allowlist does not gate — and it inherits THIS model. On 2026-06-23 a haiku helper wrote
        # four articles into two project wikis, one of them a pure ledger. Belt and braces.
        # Аккаунт: внутренний помощник обязан идти под тем же аккаунтом, что выбран в UI.
        # 01.09.2026 у основного аккаунта истёк refresh-токен, и reconcile падал каждые
        # несколько минут с "OAuth session expired", хотя активным был живой аккаунт —
        # потому что здесь env собирался без accounts.env_overrides() и CLI получал
        # дефолтный ~/.claude. Переключатель аккаунтов на этот вызов не влиял вообще.
        env={**_memory_env_overrides("project"), **_accounts.env_overrides()},
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


# ─────────────────────────── Resume self-healing ───────────────────────────

def _transcript_exists(cwd: str, session_id: str) -> bool:
    """True when the CLI transcript backing `session_id` is still on disk.

    Slug rule (every non-alphanumeric char → '-') mirrors webapp._sdk_sessions_dir; the two
    must stay in sync — test_resume_selfheal.py asserts they agree.
    """
    if not cwd or not session_id:
        return False
    try:
        slug = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        return (Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl").exists()
    except Exception:
        # Never let a path/permission hiccup drop a resume that would have worked.
        return True


def _forget_dead_session(ctx: "dict | None", session_key: str, dead_sid: str) -> None:
    """Drop a session_id whose transcript is gone from the layer-2 cache.

    The chats.json entry keeps the dead id only until this turn's write-back replaces it
    with the fresh session_id, so it is deliberately left alone here.
    """
    try:
        sessions = (ctx or {}).get("sessions")
        if isinstance(sessions, dict) and sessions.get(session_key) == dead_sid:
            sessions.pop(session_key, None)
            save = (ctx or {}).get("save_sessions")
            if callable(save):
                save()
    except Exception as exc:
        print(f"[session] could not clear dead sid for {session_key}: {exc!r}")


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
    plan_mode: bool = False,
    ask_mode: bool = False,
    chat_id: "str | None" = None,
    entrypoint: str = "chat",
    disallowed_tools_extra: "list | None" = None,
    project_skills: "list[str] | str | None" = None,
    project_plugins: "list[str] | None" = None,
    project_memory: "str | None" = None,
    project_account: "str | None" = None,
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
                                 Note: --effort is honored on Fable 5 (low..xhigh|max; official default high).
        ultracode             — spec-058 v2: when True, activate the CLI's NATIVE ultracode
                                 machinery (settings={"ultracode": true} → Workflow contract +
                                 standing opt-in reminders + internal xhigh effort pin; the
                                 effort arg is ignored) and append the thin ULTRACODE_PROMPT
                                 complement. False (default) → no change.
        ask_mode              — spec-082 A: per-tool approval. The turn connects with
                                 permission_mode="default" and a can_use_tool gate that parks
                                 every non-read-only tool call as an operator decision in the
                                 cockpit. Ignored when plan_mode is also set (plan wins).
        project_account       — pin this project to one Claude subscription (an id from
                                 accounts.py). None → the globally selected account. An
                                 override that is gone or not logged in degrades to the global
                                 one rather than failing the run.
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

    # spec-080 C4: plan mode and ultracode are mutually exclusive — the Workflow tool IS
    # available inside plan mode (verified live) and would spawn executing agents mid-plan,
    # and ULTRACODE_PROMPT names roster agents a plan turn does not carry. Plan wins.
    if plan_mode and ultracode:
        print(f"[plan-gate] {session_key}: ultracode suppressed for the plan-mode turn")
        ultracode = False
    # spec-082 A: ask ⊕ plan are mutually exclusive and plan wins server-side — a plan turn is
    # already read-only and already gated, so a second gate would only double the taps.
    if plan_mode and ask_mode:
        print(f"[ask-gate] {session_key}: ask mode suppressed — plan mode owns this turn")
        ask_mode = False
    if plan_mode:
        # Dispatcher state for the gate callback (read at call time, never captured):
        _plan_turn_chat[session_key] = chat_id
        _plan_gate_approved.pop(session_key, None)
        _plan_write_paths.pop(session_key, None)
    else:
        _plan_turn_chat.pop(session_key, None)
        _plan_gate_approved.pop(session_key, None)
    if ask_mode:
        _ask_turn_chat[session_key] = chat_id
    else:
        _ask_turn_chat.pop(session_key, None)

    # Conductor directive: inject for fable as orchestrator model (unless disabled per-project) — but
    # NOT when ultracode is on, since the native ultracode contract (Workflow tool + reminders)
    # takes over and the conductor's "≤3–5 concurrent" cap would fight ultracode fan-out.
    # NOT in plan mode either: the CLI injects its own plan workflow (built-in Explore/Plan
    # agent types), and the conductor names roster agents a plan turn does not carry.
    if not skip_conductor_prompt and not ultracode and not plan_mode and resolved_model and resolved_model.startswith("fable"):
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

    # spec-058 v2: Ultracode mode — append the thin Cardloop complement (same mechanism as the
    # conductor/board blocks). The actual contract comes from the native settings flag below.
    if ultracode:
        existing_append = system_prompt.get("append") or ""
        sep = "\n" if existing_append else ""
        system_prompt = dict(system_prompt)
        system_prompt["append"] = existing_append + sep + ULTRACODE_PROMPT

    # Sub-agent roster: use provided agents or fall back to the default roster.
    # Plan-mode turns drop the custom roster entirely: the CLI's built-in Explore/Plan agent
    # types drive the plan workflow (verified live), and a custom AgentDefinition with
    # permissionMode="bypassPermissions" could hand a child a way around plan-blocking.
    effective_agents = None if plan_mode else (agents if agents is not None else DEFAULT_AGENTS)

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

    # spec-058 v2: ultracode passes NO --effort — the native {"ultracode": true} settings flag
    # pins effort to "xhigh" inside the CLI, and an explicit CLI effort flag would OVERRIDE that
    # native pin (CLI flag wins over settings in the CLI's effort resolution). The ledger and the
    # live-client fingerprint record the effective "xhigh" so toggling ultracode still evicts.
    _sdk_effort = None if ultracode else (effort if effort is not None else _DEFAULT_EFFORT)
    _eff_effort = ULTRACODE_EFFORT if ultracode else (effort if effort is not None else _DEFAULT_EFFORT)

    # A stored session_id whose transcript is gone (CLI retention cleanup, a wiped
    # ~/.claude/projects/<slug>, a restored backup) makes the CLI exit 1 on --resume. The
    # live-client fallback below rebuilds the SAME options, so it exits 1 again and the turn
    # dies as `sdk_error` — every send in that chat vanishes with no visible error and the
    # project looks "unbound from its chats". Verify the transcript first and start fresh
    # when it is missing, dropping the dead id so nothing re-resumes it.
    if resume_session_id and not _transcript_exists(cwd, resume_session_id):
        print(f"[session] resume {session_key} sid={resume_session_id} — transcript missing, starting fresh")
        _forget_dead_session(ctx, session_key, resume_session_id)
        resume_session_id = None

    print(f"[session] resume {session_key} sid={resume_session_id or 'NEW'}")
    # spec-065 Phase C: expose live-browser tools only when the browser module is on.
    # Built per-run with this run's cwd bound, so the agent drives the SAME browser the
    # operator watches in the cockpit pane (browser_pane keys sessions by cwd).
    _mcp_servers = dict(_ANTIGRAVITY_MCP or {})
    # FIX 2: initialise browser identity before the try-block so stable_append_hash can see them.
    _agent_actions: str = "read"
    _browser_backend: str = "builtin"
    _browser_active: bool = False
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
            _browser_active = True
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
        system_prompt["append"] = _img_append + _img_sep + IMAGES_PROMPT + "\n" + FILES_PROMPT
        # Make the cockpit media helpers (cockpit-img, cockpit-file) reachable from the agent's
        # shell without a manual install step: prepend the repo's tools/ dir to PATH. Placing it
        # first also shadows any stale hand-copied helper elsewhere on PATH. Copy env first so we
        # never mutate the caller's secrets dict.
        env = dict(env or {})
        _tools_dir = str(Path(__file__).resolve().parent / "tools")
        _cur_path = env.get("PATH") or os.environ.get("PATH", "")
        # Always put tools/ FIRST, de-duping any existing entry. The old "skip if already
        # present" guard left a stale hand-copied helper winning when tools/ was on PATH but
        # not first — defeating the shadow-the-stale-copy intent (and making it env-fragile).
        _path_parts = [p for p in _cur_path.split(os.pathsep) if p and p != _tools_dir]
        env["PATH"] = os.pathsep.join([_tools_dir, *_path_parts])
        # Same gate: tell report-producing sub-agents about cockpit-file too — otherwise they
        # never learn the tool exists (each AgentDefinition has its own separate `prompt`,
        # untouched by the orchestrator's system_prompt append above). "quick" is deliberately
        # skipped: its whole point is a short inline answer, never a file.
        if effective_agents:
            effective_agents = {
                _name: (
                    dataclasses.replace(_def, prompt=_def.prompt + "\n\n" + SUBAGENT_FILES_PROMPT)
                    if _name != "quick" else _def
                )
                for _name, _def in effective_agents.items()
            }

    # FIX 2: Build a hash of the STABLE append pieces so that toggling ultracode/conductor/browser
    # or changing RESPONSE_LANGUAGE forces a live-client reconnect, while a mere board-card content
    # change does NOT.  We explicitly enumerate stable signals rather than using the full
    # system_prompt["append"] text (which already contains the volatile board-card snapshot).
    # Stable = conductor on/off, ultracode on/off, browser prompt (varies by backend+actions),
    # images/files on/off, and the base DEFAULT_NUDGE (captures RESPONSE_LANGUAGE changes).
    # Volatile = the actual card-text snapshot inside _board_block — excluded deliberately.
    _conductor_active = (
        not skip_conductor_prompt and not ultracode
        and bool(resolved_model and resolved_model.startswith("fable"))
    )
    _stable_append_pieces = [
        DEFAULT_NUDGE,                                          # base nudge (captures RESPONSE_LANGUAGE)
        CONDUCTOR_PROMPT if _conductor_active else "",          # conductor on/off
        BOARD_PROTOCOL if _board_block else "",                 # board protocol header (card snapshot excluded)
        ULTRACODE_PROMPT if ultracode else "",                  # ultracode on/off
        _browser_prompt(_browser_backend, _agent_actions) if _browser_active else "",  # browser variant
        IMAGES_PROMPT if (env or {}).get("COPS_MEDIA_DIR") else "",  # images on/off
        FILES_PROMPT if (env or {}).get("COPS_MEDIA_DIR") else "",   # files on/off
    ]
    _stable_content = "|".join(_stable_append_pieces)
    _stable_append_hash = hashlib.sha256(_stable_content.encode()).hexdigest()[:16]

    # spec-078 Phase 2: per-project brains. `skills` is the SDK context filter (None → CLI default =
    # all skills; list → only those; "all" → every skill). Default to the global lean allowlist so a
    # project pulls only ITS relevant skills. `plugins` opt-in loads a plugin for THIS project only
    # (e.g. marketing-skills on the marketing project) without enabling it globally for others.
    _effective_skills = _merge_project_skills(project_skills, _DEFAULT_SKILLS)

    # spec-078 Phase 3a: one canonical brain. "project" turns the CLI's native auto-memory off so
    # the curated ./.claude-ops/memory/ is all the project loads.
    _memory_mode = project_memory or _DEFAULT_MEMORY_MODE
    if _memory_mode not in _MEMORY_MODES:
        print(f"[memory] unknown mode {_memory_mode!r} for {project_name} — falling back to 'auto'")
        _memory_mode = _DEFAULT_MEMORY_MODE
    # Multi-subscription: the active account binds this run to a config dir (and therefore to a
    # set of credentials). `main` yields {} — no CLAUDE_CONFIG_DIR at all, i.e. exactly the
    # single-account behaviour. A registered-but-broken account degrades to main rather than
    # taking the turn down; accounts.env_overrides() logs when it does.
    _account_id = _accounts.resolve(project_account)
    _account_env = _accounts.env_overrides(_account_id)
    if not _account_env:
        _account_id = _accounts.MAIN_ID
    else:
        print(f"[accounts] {session_key} runs on {_account_id} "
              f"(CLAUDE_CONFIG_DIR={_account_env['CLAUDE_CONFIG_DIR']})")
    _effective_env = {**(env or {}), **_memory_env_overrides(_memory_mode), **_account_env}

    _project_plugin_cfgs: "list[dict]" = []
    for _pid in (project_plugins or []):
        _pp = _plugin_install_path(_pid)
        if _pp:
            _project_plugin_cfgs.append({"type": "local", "path": _pp})
        else:
            print(f"[skills] project plugin {_pid!r} not found in installed_plugins.json — skipped")

    opts = ClaudeAgentOptions(
        model=resolved_model,
        fallback_model=fallback,
        # spec-080: plan turns connect in the CLI's native plan mode (hard read-only + its own
        # 5-phase workflow injection). permission_mode is part of the live-client fingerprint,
        # so toggling plan on/off reconnects the client with correctly-bound options.
        # spec-082 A: ask turns connect in "default" — NOT bypassPermissions, which SHADOWS
        # can_use_tool (CanUseToolShadowedWarning) and would run every tool ungated.
        permission_mode=("plan" if plan_mode else ("default" if ask_mode else "bypassPermissions")),
        # can_use_tool only when a gate is active — under bypassPermissions it would be
        # shadowed anyway and the SDK emits CanUseToolShadowedWarning noise.
        can_use_tool=(_make_plan_gate_cb(session_key, ctx) if plan_mode
                      else (_make_ask_gate_cb(session_key, ctx) if ask_mode else None)),
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        # spec-085 Phase 3: sub-agent finalized text rides the stream (parent_tool_use_id
        # tagged) and surfaces in the cockpit's subagent lane. Constant per process, so the
        # live-client fingerprint stays stable.
        forward_subagent_text=bool(FORWARD_SUBAGENT_TEXT),
        cwd=cwd,
        setting_sources=["user", "project", "local"],
        # spec-078: per-project skill filter + opted-in plugins. setting_sources stays intact so the
        # guard-self-lifecycle PreToolUse hook + permissions + env are fully preserved.
        skills=_effective_skills,  # type: ignore[arg-type]
        plugins=_project_plugin_cfgs,  # type: ignore[arg-type]
        resume=resume_session_id,
        disallowed_tools=list(DISALLOWED_TOOLS) + list(disallowed_tools_extra or []),
        system_prompt=system_prompt,
        env=_effective_env,
        mcp_servers=_mcp_servers,
        agents=effective_agents,
        effort=_sdk_effort,  # type: ignore[arg-type]
        # spec-058 v2: native ultracode switch (Workflow contract + xhigh pin) as inline
        # --settings JSON. None → no flag at all.
        settings=_compose_settings(ultracode),
        hooks={
            "PreToolUse": [HookMatcher(
                matcher="Bash",
                hooks=[_bundle_grep_guard_hook, _dangerous_command_guard_hook],
            )],
            "PostToolUse": [HookMatcher(hooks=[_post_tool_hook])],
            "PreCompact": [HookMatcher(hooks=[_pre_compact_hook])],
        },
        # include_hook_events=False (default) — HookEventMessage lifecycle noise adds no extra
        # data beyond what the hook callback already captures, and would flood _process_messages.
        include_partial_messages=_stream_partial,
        # Spec-029 item 3: structured output for card runs. None → no change (chat/TG paths).
        output_format=output_format,
        # spec-073: track file changes so rewind_files() can restore any checkpoint.
        enable_file_checkpointing=FILE_CHECKPOINTS,
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
    # Root-fix A4: ids of still-open background tasks of the types the CLI natively defers
    # for (local_agent/local_workflow). The ephemeral (card) branch lingers on this set
    # before disconnecting — a card's `async with` exit used to kill children the CLI had
    # not finished waiting for. Mirrors the SDK's own tracking; populated in
    # _process_messages, drained by terminal notifications.
    _inflight_deferring: "set[str]" = set()

    # Shared inner generator: processes SDK messages and yields engine events.
    # Extracted so both the live-client branch and the `async with` branch share
    # identical event-processing logic with no duplication.
    async def _process_messages(client):
        """Read messages from `client` and yield engine events."""
        nonlocal last_ctx_tokens, last_usage, _turn_start_ms
        _turn_start_ms = __import__("time").monotonic() * 1000
        served_model: "str | None" = None  # FIX 1: populated from SDK init SystemMessage
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
        # spec-085 Phase 3: per-turn sub-agent text forwarding state (see _subagent_text_events).
        _sub_tool_to_task: dict = {}
        _sub_text_counts: dict = {}
        # spec-085 Phase 3 fix: task_id -> task_type, learned from TaskStartedMessage. Progress
        # and notification messages carry NO task_type of their own, and the CLI reports a
        # sub-agent's own tool executions as tasks too — without this the cockpit could not tell
        # "an agent" from "a bash call inside an agent" and drew a lane row for each.
        _sub_task_types: dict = {}
        async for msg in client.receive_response():
            # Background sub-agent traffic: the CLI forwards sub-agents' AssistantMessages
            # (and their stream events) on the SAME stdout with parent_tool_use_id set.
            # Without this guard their tool/text blocks interleave with the orchestrator's
            # own text_delta stream — the chat canvas splits the answer MID-WORD at delta
            # boundaries and their usage inflates _turn_max_pt. The chat lane stays
            # orchestrator-only; the ONE exception (spec-085 Phase 3) is a sub-agent's
            # finalized TextBlocks, surfaced as capped subagent/text lane events.
            if getattr(msg, "parent_tool_use_id", None):
                for _sub_ev in _subagent_text_events(msg, _sub_tool_to_task, _sub_text_counts):
                    yield _sub_ev
                continue
            if isinstance(msg, UserMessage):
                # A delivery from another session/peer, surfaced live instead of only in history.
                _peer_ev = _peer_message_event(msg)
                if _peer_ev is not None:
                    print(f"[peer] {session_key}: {_peer_ev['kind']} message from "
                          f"{_peer_ev['sender']} ({len(_peer_ev['text'])} chars)")
                    yield _peer_ev
                continue
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
                        # FIX 1(d): record the actual served model id (may differ from requested
                        # alias when fallback_model engaged — e.g. fable→opus degradation).
                        "model_served": served_model,
                        "effort": _eff_effort,
                        "ultracode": ultracode,
                        "context_tokens": last_ctx_tokens,
                        "fresh_tokens": _fresh,
                        "cache_read_tokens": _cache_read,
                        "cache_hit_pct": _cache_hit_pct,
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                        "duration_ms": _dur,
                    })
                # A clean interrupt never raises: client.interrupt() makes the CLI end the
                # query loop and say so HERE, not through an exception. Without this the only
                # trace of an operator Stop is the optimistic `operator_stop` row the caller
                # writes before it knows whether the interrupt even landed.
                _mu = getattr(msg, "model_usage", None) or {}
                _canonical_served = _pick_served_model(_mu, resolved_model) or served_model
                _tr = getattr(msg, "terminal_reason", None)
                if _tr in ("aborted_streaming", "aborted_tools"):
                    _record_turn_abort(session_key, _tr, "clean interrupt (client.interrupt())")
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
                    # FIX 1(c): actual model id served by the SDK (None when init event absent).
                    "model_served": served_model,
                    # FIX 1(f): stop_reason from ResultMessage (available in SDK types.py).
                    "stop_reason": getattr(msg, "stop_reason", None),
                    # The CLI's own account of how the query loop ended ("completed",
                    # "max_turns", "aborted_streaming", "aborted_tools", ...). Cardloop used to
                    # guess this; the CLI knows the cases we cannot see.
                    "terminal_reason": getattr(msg, "terminal_reason", None),
                    # is_error: the ONE ResultMessage field this repo never read anywhere.
                    "is_error": getattr(msg, "is_error", None),
                    # Why this turn was initiated (human prompt vs task notification vs peer).
                    "origin": getattr(msg, "origin", None),
                    # Structured error payload the CLI attaches to a failed result.
                    "errors": getattr(msg, "errors", None),
                    # The alias/id this turn ASKED for, so a consumer can compare without
                    # re-deriving it from project state.
                    "model_requested": resolved_model,
                    # What actually ran, per turn, from model_usage — unlike model_served
                    # above (init message only, i.e. once per live-client lifetime).
                    "canonical_served": _canonical_served,
                    # Raw per-model usage (2-3 small dicts): lets a later pass split the
                    # turn's own cost from auto-mode helper traffic without re-deriving it.
                    "model_usage": _mu or None,
                }
            elif isinstance(msg, SystemMessage):
                if isinstance(msg, TaskStartedMessage):
                    # Root-fix A4: track deferring-type tasks so the ephemeral branch can
                    # linger for them before disconnecting.
                    if getattr(msg, "task_type", None) in DEFERRING_TASK_TYPES and msg.task_id:
                        _inflight_deferring.add(msg.task_id)
                    # spec-085 Phase 3: remember the spawning tool_use id so forwarded
                    # sub-agent text can be attached to this task's lane row.
                    if getattr(msg, "tool_use_id", None):
                        _sub_tool_to_task[msg.tool_use_id] = msg.task_id
                    _sub_task_types[msg.task_id] = getattr(msg, "task_type", None) or ""
                    yield {
                        "type": "subagent",
                        "subtype": "started",
                        "task_id": msg.task_id,
                        "description": msg.description,
                        "status": None,
                        "summary": None,
                        "last_tool_name": None,
                        "task_type": getattr(msg, "task_type", None),
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
                        "task_type": _sub_task_types.get(msg.task_id) or None,
                    }
                elif isinstance(msg, TaskNotificationMessage):
                    # Card b6f5cc: a task-completion notification flips a tracked background monitor
                    # to a terminal status. spec-069 P3 (RC#3): a Workflow/Monitor task's monitor is
                    # registered under the TOOL's taskId, but this notification's task_id is a DIFFERENT
                    # internal id — so we also pass tool_use_id, the stable shared key, and _monitor_update
                    # falls back to matching by it. only_existing guard → no phantom monitor is spawned.
                    try:
                        # spec-071: shared superset status map — the old inline map lacked
                        # killed/cancelled, so those flips were silently lost in-turn.
                        _nd = _notification_monitor_delta(msg)
                        if _nd:
                            _inflight_deferring.discard(_nd["id"])  # root-fix A4
                        if getattr(msg, "task_id", None):
                            _inflight_deferring.discard(msg.task_id)
                        if _monitor_update_cb and _nd:
                            print(f"[monitor] task-notification id={_nd['id']} tool_use_id={_nd.get('tool_use_id')} → {_nd['status']}")
                            _monitor_update_cb(session_key, _nd, only_existing=True)
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
                        "task_type": _sub_task_types.get(msg.task_id) or None,
                        # docs/internal/sdk-feature-audit/02-subagent-output.md: the CLI always
                        # names an on-disk file alongside a task-completion notification
                        # (required field on TaskNotificationMessage); surface it so the cockpit
                        # can offer the raw output even when the agent forgot to cockpit-file it.
                        # getattr(..., None) is defensive only — the SDK guarantees this key.
                        "output_file": getattr(msg, "output_file", None),
                    }
                elif isinstance(msg, TaskUpdatedMessage):
                    # spec-071: a background task's terminal state can arrive ONLY here (per SDK
                    # docs — e.g. TaskStop reports status="killed" with the notification
                    # suppressed). Flip the monitor from this message too; no yield (UI noise).
                    try:
                        _nd = _notification_monitor_delta(msg)
                        if _nd:
                            _inflight_deferring.discard(_nd["id"])  # root-fix A4
                        if getattr(msg, "task_id", None):
                            _inflight_deferring.discard(msg.task_id)
                        if _monitor_update_cb and _nd:
                            print(f"[monitor] task-updated id={_nd['id']} → {_nd['status']}")
                            _monitor_update_cb(session_key, _nd, only_existing=True)
                    except Exception:
                        pass
                elif msg.subtype == "init":
                    # FIX 1(a): capture the actual model the SDK initialised with.
                    # The init SystemMessage carries {"type":"system","subtype":"init","data":{...}}
                    # where data["model"] is the full model id (e.g. "claude-fable-5").
                    # Keep it silent (no yield) unless a family mismatch is detected below.
                    _init_model = (msg.data or {}).get("model") if isinstance(msg.data, dict) else None
                    if _init_model:
                        served_model = str(_init_model)
                    # FIX 1(b): emit model_info event ONCE if served family != requested alias.
                    # Family match rule: requested alias substring present in served id.
                    # "fable" in "claude-fable-5" → match; "opus" in "claude-opus-4-8" → match.
                    if served_model and resolved_model:
                        # Normalise: strip "claude-" prefix to get the family token (fable, opus, sonnet…)
                        _req_alias = (resolved_model or "").replace("claude-", "").split("-")[0]
                        _mismatch = _req_alias not in served_model.lower()
                        if _mismatch:
                            yield {
                                "type": "model_info",
                                "requested": resolved_model,
                                "served": served_model,
                                "fallback": True,
                            }
                else:
                    # Other SystemMessage subtypes remain silent toward the UI, but a NEW
                    # subtype from a newer CLI must not vanish from observability entirely —
                    # log each distinct one once per process (root-fix C).
                    _st = str(getattr(msg, "subtype", None) or "?")
                    if _st not in _UNKNOWN_SUBTYPES_SEEN:
                        _UNKNOWN_SUBTYPES_SEEN.add(_st)
                        print(f"[engine] unhandled SystemMessage subtype {_st!r} observed "
                              f"({session_key}) — logged once for observability")

    # ── Spec-028 Phase 2: live-client branch (flag-gated, ephemeral=False only) ──────────────────
    # When PERSISTENT_CLIENT=0 (default) _get_or_create_live_client returns None immediately and
    # we fall through to the pre-028 `async with` path — byte-identical behaviour.
    try:
        live = await _get_or_create_live_client(
            ctx, session_key, opts, ephemeral=ephemeral,
            stable_append_hash=_stable_append_hash, effort=_eff_effort,
            memory_mode=_memory_mode, account=_account_id,
        )
    except Exception as _lc_exc:
        # Live-client setup failure must never silently swallow the turn — degrade gracefully.
        print(f"[live-client] setup failed for {session_key} ({_lc_exc!r}), falling back to fresh client")
        live = None

    # spec-080 C1 (spec-082 A: same for ask turns): a client pinned by live background children
    # is reused on fingerprint mismatch (deferred reconnect). For a GATED turn that reuse is
    # fatal-but-silent: the old client has no can_use_tool bound and the CLI subprocess is still
    # in bypassPermissions — the turn would execute full-auto with no gate and no error. Abort
    # loudly instead.
    if (plan_mode or ask_mode) and live is not None and not _plan_client_fingerprint_ok(
            ctx, session_key, opts, _stable_append_hash, _eff_effort, _memory_mode, _account_id):
        _mode_name = "Plan mode" if plan_mode else "Ask mode"
        print(f"[{'plan' if plan_mode else 'ask'}-gate] {session_key}: live client pinned by "
              f"running background tasks — aborting gated turn instead of running ungated")
        yield {"type": "error", "exc": RuntimeError(
            f"{_mode_name} could not activate: this session's client is pinned by still-running "
            f"background tasks and cannot be reconnected. Wait for them to finish "
            "(or stop them) and resend the message.")}
        _running.pop(session_key, None)
        return

    if live is not None:
        # ── Persistent-client path ────────────────────────────────────────────────────────────────
        # The client is already connected; we skip __aenter__ / __aexit__.
        # running[session_key] is set here (replacing the True placeholder) so the watchdog and
        # /stop command can interrupt mid-turn.  We MUST pop it in finally (the adapter's finally
        # also pops it, making this double-safe).
        # We do NOT call client.disconnect() — the live-client registry owns the lifecycle.
        # spec-071: the engine turn is the sole stream consumer — pause the between-turns drain
        # BEFORE query() so exactly one reader pulls from the SDK message buffer at a time.
        _lc_registry: "dict[str, _LiveEntry]" = (ctx or {}).get("live_clients", _live_clients)
        _lc_entry = _lc_registry.get(session_key)
        if _lc_entry is not None and _lc_entry.client is live:
            await _stop_drain(_lc_entry)
        _running[session_key] = live
        try:
            async for event in _process_messages(live):
                yield event
        except ProcessError as exc:
            if exc.exit_code == 143:
                # SIGTERM to the CLI subprocess — expected on interrupt/stop/service shutdown.
                # Log concisely and do not propagate; avoids asyncio "never retrieved" noise.
                print(f"[engine] subprocess terminated (143) — expected on interrupt/shutdown ({session_key})")
                _record_turn_abort(session_key, "terminated", str(exc))
            else:
                # Subprocess state is unknown after an error — evict so the next turn reconnects fresh.
                _hint = _buffer_overflow_hint(exc)
                if _hint:
                    print(f"[engine] {_hint} ({session_key})")
                print(f"[live-client] error during turn for {session_key} ({exc!r}) — evicting")
                _reason, _sub, _errs = _abort_reason_for(exc, _hint)
                _record_turn_abort(session_key, _reason, str(exc),
                                   sdk_subtype=_sub, sdk_errors=_errs)
                await _evict_live_client(session_key, ctx)
                yield {"type": "error", "exc": exc}
        except Exception as exc:
            # Subprocess state is unknown after an error — evict so the next turn reconnects fresh.
            _hint = _buffer_overflow_hint(exc)
            if _hint:
                print(f"[engine] {_hint} ({session_key})")
            print(f"[live-client] error during turn for {session_key} ({exc!r}) — evicting")
            _reason, _sub, _errs = _abort_reason_for(exc, _hint)
            _record_turn_abort(session_key, _reason, str(exc),
                               sdk_subtype=_sub, sdk_errors=_errs)
            await _evict_live_client(session_key, ctx)
            yield {"type": "error", "exc": exc}
        finally:
            # DO NOT disconnect — the live client must survive for the next turn.
            # The adapter (safe_run / api_project_chat finally) clears running[k] separately;
            # we do it here too as a safety net for ctx-isolated callers.
            _running.pop(session_key, None)
            # spec-071: resume the between-turns drain (skip if the entry was evicted mid-turn).
            _lc_entry = _lc_registry.get(session_key)
            if _lc_entry is not None and _lc_entry.client is live:
                _start_drain(_lc_entry, ctx)
    else:
        # ── Standard fresh-client path (pre-028 behaviour, unchanged) ────────────────────────────
        try:
            async with ClaudeSDKClient(options=opts) as client:
                _running[session_key] = client  # replace True-placeholder (for /stop)
                async for event in _process_messages(client):
                    yield event
                # Root-fix A4: an ephemeral (card) client whose deferring background tasks
                # (local_agent / local_workflow) are still open at turn end must NOT let
                # `__aexit__` disconnect yet — that SIGTERMs the children mid-work. Linger,
                # keep flipping monitors from the still-open stream, bounded by
                # CARD_LINGER_MAX_SEC. No-op when the set is already empty (the common case
                # if the CLI's native deferral resolved everything before the result).
                # NOTE: continuing to read after receive_response() relies on the SDK's
                # receive channel not being closed by that early return — true for
                # claude-agent-sdk 0.2.127; re-verify on SDK bumps.
                if _inflight_deferring:
                    _mono = __import__("time").monotonic
                    _linger_deadline = _mono() + CARD_LINGER_MAX_SEC
                    print(f"[engine] {session_key}: {len(_inflight_deferring)} deferring "
                          f"background task(s) still open at turn end — lingering up to "
                          f"{CARD_LINGER_MAX_SEC}s before disconnect")
                    try:
                        _stream = client.receive_messages()
                        while _inflight_deferring:
                            _remaining = _linger_deadline - _mono()
                            if _remaining <= 0:
                                break
                            try:
                                msg = await asyncio.wait_for(
                                    _stream.__anext__(), timeout=min(_remaining, 30.0))
                            except asyncio.TimeoutError:
                                continue
                            except StopAsyncIteration:
                                break
                            if isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
                                _nd = _notification_monitor_delta(msg)
                                if _nd:
                                    _inflight_deferring.discard(_nd["id"])
                                    if _monitor_update_cb:
                                        print(f"[linger] task {_nd['id']} → {_nd['status']}")
                                        _monitor_update_cb(session_key, _nd, only_existing=True)
                                if getattr(msg, "task_id", None):
                                    _inflight_deferring.discard(msg.task_id)
                    except Exception as _linger_exc:
                        print(f"[engine] card linger aborted for {session_key}: {_linger_exc!r}")
                    if _inflight_deferring:
                        # Timed out with children still open: flip their monitors NOW so the
                        # completion-wake machinery hears about it immediately instead of
                        # waiting for the 15-minute staleness sweep.
                        for _tid in list(_inflight_deferring):
                            try:
                                if _monitor_update_cb:
                                    _monitor_update_cb(session_key, {
                                        "id": _tid, "status": "failed",
                                        "tail": "(card run ended before this background task "
                                                "finished — it dies with the card's client)",
                                    }, only_existing=True)
                            except Exception:
                                pass
                        _inflight_deferring.clear()
        except ProcessError as exc:
            if exc.exit_code == 143:
                # SIGTERM to the CLI subprocess — expected on interrupt/stop/service shutdown.
                # Log concisely and do not propagate; avoids asyncio "never retrieved" noise.
                print(f"[engine] subprocess terminated (143) — expected on interrupt/shutdown ({session_key})")
                _record_turn_abort(session_key, "terminated", str(exc))
            else:
                _hint = _buffer_overflow_hint(exc)
                if _hint:
                    print(f"[engine] {_hint} ({session_key})")
                _reason, _sub, _errs = _abort_reason_for(exc, _hint)
                _record_turn_abort(session_key, _reason, str(exc),
                                   sdk_subtype=_sub, sdk_errors=_errs)
                yield {"type": "error", "exc": exc}
        except Exception as exc:
            _hint = _buffer_overflow_hint(exc)
            if _hint:
                print(f"[engine] {_hint} ({session_key})")
            _reason, _sub, _errs = _abort_reason_for(exc, _hint)
            _record_turn_abort(session_key, _reason, str(exc),
                               sdk_subtype=_sub, sdk_errors=_errs)
            yield {"type": "error", "exc": exc}


def _build_ctx(*, web_port: int = None, web_password: str = None) -> dict:
    """Build the shared context dict passed to webapp.start().

    Values come from module-level state so the cockpit and kanban auto-run share
    the same topics/sessions/running/etc. dicts.

    Also registers webapp callbacks to avoid circular import in hooks.

    web_port, web_password: passed in from bot.py (it already read + applied env).
    """
    import webapp as _webapp  # lazy — called only at startup after webapp is fully loaded
    _register_webapp_callbacks(_webapp._timeline_append, _webapp._bus_publish, _webapp._monitor_update,
                               _webapp._has_live_agent_monitors, _webapp._bg_run_event,
                               create_pending_plan=_webapp.create_pending_plan,
                               resolve_plan=_webapp.resolve_decision,
                               pending_plan_id=_webapp._pending_plan_id,
                               create_pending_tool=_webapp.create_pending_tool_decision)

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
        # session rewind (docs/internal/sdk-feature-audit/04-session-rewind.md): exposed via
        # ctx so webapp.py can fork a conversation without importing engine.py directly
        # (same anti-circular-import convention as evict_live_client/run_engine above).
        "rewind_conversation": rewind_conversation,
        "rewind_refused_hint": _rewind_refused_hint,
        # spec-080: pending-plan store hooks exposed via ctx so e2e_fake_engine (which never
        # touches engine.py's private callback registry) can drive the exact same store.
        "create_pending_plan": _webapp.create_pending_plan,
        "resolve_plan": _webapp.resolve_plan,
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
