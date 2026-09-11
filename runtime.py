"""One place a chat's runtime (provider × account × model) gets decided.

Today that decision is re-derived independently in four call sites (chat POST, the chat
queue drain, board cards, autopilot), each reading a slightly different source, with two
measured hazards:

  * ``_chat_provider`` (webapp.py) returns ``"claude"`` for ANY unrecognised provider value
    — fail-OPEN. Harmless with two providers, a real hazard once a third (Ollama, planned)
    exists: an unavailable/unknown provider must never silently become a Claude billing
    event.
  * ``accounts.resolve()`` degrades a broken account to ``main`` — correct for a *project
    default* (an operator never explicitly asked for that account this turn) and wrong as
    the answer to an *explicit* "run this chat on `work`" (the operator asked for a specific
    subscription; silently running on another one is a wrong-billing bug wearing a
    resilience feature's clothes).

This module is the single place that inheritance and fail-closed validation happen, so the
four call sites can all defer to it instead of re-implementing (and re-diverging on) the
same policy. It is pure logic: no aiohttp, no ``webapp.py``/``engine.py`` import. The only
I/O it performs is reading the accounts registry (``accounts.py``), which the design brief
explicitly allows — everything else (the chat record, the project record, the live
provider registry) is handed in as plain data the caller already loaded.

See ``docs/internal/specs/spec-092-unified-runtime-picker.md`` for the full design (that
directory is gitignored — internal design history, not shipped — so this module carries
the load-bearing decisions inline as docstrings/comments instead of only living in the spec).

Design note — ``provider`` vs ``backend``:
    ``provider`` is which agent HARNESS runs the turn (its tool loop, hooks, sub-agent
    support): ``claude`` (Claude Code) or ``codex`` (OpenAI Codex). ``backend`` is which
    inference ENDPOINT actually answers the model calls. For Claude and Codex today those
    are the same thing in practice (``anthropic`` / ``chatgpt``), so a single field would
    have worked so far. The planned Ollama route breaks that equivalence on purpose: per
    spec-092 it rides the *Claude Code* harness (provider stays ``claude`` — same tools,
    hooks, plan/ask mode, sub-agents) with an env overlay (``ANTHROPIC_BASE_URL`` pointed at
    a local shim) swapping only the endpoint (``backend`` becomes ``ollama``). Collapsing
    the two into one field would make that combination unrepresentable without inventing a
    fake third "provider" that isn't a different harness at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping, Sequence

import accounts as _accounts_mod

# The bedrock provider. Every fallback chain in this module bottoms out here, matching the
# hardcoded default already in webapp.py's `_chat_provider` and `_effective_card_provider` —
# this module generalizes that rule, it does not change its outcome for existing installs.
DEFAULT_PROVIDER = "claude"

# Which inference endpoint a provider talks to when nothing overrides it. Only `claude` has
# a second, non-default backend today (`ollama`, spec-092 P3) — Codex's OSS/local route is
# an open question in the spec (§10 "contested"), so it is deliberately NOT offered here;
# adding it later is a one-line change to this map, not a schema change.
DEFAULT_BACKENDS: dict[str, str] = {"claude": "anthropic", "codex": "chatgpt"}


class RuntimeResolutionError(ValueError):
    """Raised by resolve_runtime() when a decision cannot be made safely.

    This is the fail-CLOSED half of the contract: an explicit-but-broken pick (an unknown
    provider, an unusable account named on purpose) must stop the run, not quietly answer
    with a different runtime than the one asked for.
    """


# ---------------------------------------------------------------------------
# The live provider registry, as pure data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderInfo:
    """One row of the live provider registry (the shape ``GET /api/agent-providers``
    already returns, minus the HTTP wrapper). The caller builds these from that endpoint's
    payload or from the in-process provider modules directly — this module never fetches
    the registry itself, it only reasons about rows handed to it.
    """
    provider: str
    available: bool
    models: tuple[str, ...] = ()
    # Endpoints this provider's harness can be pointed at. Empty means "the provider IS the
    # endpoint" (the common case: claude→anthropic, codex→chatgpt) — validation then falls
    # back to DEFAULT_BACKENDS instead of requiring every ProviderInfo to spell that out.
    backends: tuple[str, ...] = ()
    # Per-turn options this runtime can actually honour. Absence of a key means "cannot
    # honour it" (fail-closed default), not "assume yes" — see capability_conflicts().
    capabilities: Mapping[str, bool] = field(default_factory=dict)


def available_providers(providers: Mapping[str, ProviderInfo]) -> dict[str, bool]:
    """Bridge from the registry shape to the `known_providers` map chat_provider() wants."""
    return {pid: info.available for pid, info in providers.items()}


# ---------------------------------------------------------------------------
# The immutable decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunContext:
    """The runtime pinned for ONE turn, at the moment the message is ACCEPTED.

    This is deliberately NOT re-derived at drain time (the chat queue's measured bug:
    `_chat_queue_enqueue` stores effort/ultracode/plan_mode/ask_mode but not
    provider/model/account, so a message typed against one runtime can drain against
    whatever the chat record says *later*, after an operator switched it mid-queue). A
    `RunContext` is the fix: build it once when the message is accepted, store it WITH the
    queued item, and hand it to the engine unchanged at drain time.

    `session_id` and `codex_thread_id` are both carried regardless of which `provider` is
    currently active — spec-092's handoff design keeps both ids forever so that switching
    back to a provider this chat has used before can resume its own conversation instead of
    starting cold.
    """
    chat_id: str
    provider: str
    backend: str
    model: str
    account: str
    revision: int
    session_id: "str | None" = None
    codex_thread_id: "str | None" = None
    effort: "str | None" = None
    ultracode: bool = False
    plan_mode: bool = False
    ask_mode: bool = False

    def __post_init__(self) -> None:
        if not self.chat_id:
            raise ValueError("RunContext requires a non-empty chat_id")
        if not self.provider:
            raise ValueError("RunContext requires a non-empty provider")
        if not self.backend:
            raise ValueError("RunContext requires a non-empty backend")
        if not self.account:
            raise ValueError("RunContext requires a resolved account (never a placeholder)")
        if self.revision < 0:
            raise ValueError("revision must be >= 0")


# ---------------------------------------------------------------------------
# 3. chat_provider — fail-closed, with a separate named legacy path
# ---------------------------------------------------------------------------

def chat_provider(chat: "Mapping[str, Any] | None", *, known_providers: Mapping[str, bool]) -> "str | None":
    """What provider THIS CHAT RECORD says to use, judged only by its own `provider` key.

    Two distinct rules, on purpose:

    * **Legacy compatibility** (a real, separately-named path): a chat record with NO
      `provider` key at all predates the provider field entirely and is unconditionally
      Claude. This is NOT availability-checked against `known_providers` — those records
      were created before a registry existed, and the whole point of the compatibility rule
      is that they must keep behaving exactly as they always have, independent of whatever
      the registry says today.
    * **Fail-closed** (the fix): a chat record that DOES name a provider, and that provider
      is missing from `known_providers` or `known_providers[provider]` is falsy (unknown OR
      currently unavailable), returns ``None``. The caller MUST treat that as an error — it
      must never fall back to "claude" or to any other default. (That silent fallback is
      exactly the bug this module exists to remove: webapp.py's old `_chat_provider`
      returned "claude" for an unrecognised value with no error at all.)

    `known_providers` maps provider id → available (bool); build it from a live registry
    with `available_providers()`.
    """
    if not isinstance(chat, Mapping) or "provider" not in chat:
        return DEFAULT_PROVIDER
    value = chat.get("provider")
    if not isinstance(value, str) or not known_providers.get(value):
        return None
    return value


# ---------------------------------------------------------------------------
# 2. resolve_runtime — the single inheritance chain
# ---------------------------------------------------------------------------

def _model_field_for_provider(provider: str) -> str:
    """Project-level model field name for a provider.

    `claude` keeps the legacy bare `model` key (it predates multi-provider support and
    renaming it would touch every existing project record for no behavioural gain). Every
    later provider follows `<provider>_model` — this already matches the real `codex_model`
    field in `_PROJECT_SETTING_FIELDS` and generalizes to a future `ollama_model` without
    hardcoding "codex" as a special case anywhere in this function.
    """
    return "model" if provider == DEFAULT_PROVIDER else f"{provider}_model"


def _resolve_provider(
    chat: "Mapping[str, Any] | None",
    project: "Mapping[str, Any] | None",
    global_defaults: "Mapping[str, Any] | None",
    providers: Mapping[str, ProviderInfo],
) -> str:
    if isinstance(chat, Mapping) and "provider" in chat:
        chosen = chat_provider(chat, known_providers=available_providers(providers))
        if chosen is None:
            raise RuntimeResolutionError(
                f"chat pins provider {chat.get('provider')!r}, which is unknown or "
                f"currently unavailable — refusing to silently substitute another provider"
            )
        return chosen
    # Chat does not pin a provider: inherit project → global → the bedrock default.
    # `default_provider` is a forward-compatible read — no existing project record has this
    # key, so this branch is a harmless no-op today and returns DEFAULT_PROVIDER exactly as
    # the pre-registry code always did, while giving the picker's project tier somewhere to
    # land later without another schema change.
    candidate = (
        (project or {}).get("default_provider")
        or (global_defaults or {}).get("provider")
        or DEFAULT_PROVIDER
    )
    if candidate not in providers or not providers[candidate].available:
        raise RuntimeResolutionError(
            f"resolved provider {candidate!r} (from project/global default) is not "
            f"available — fix the project/global setting instead of guessing another provider"
        )
    return candidate


def _resolve_model(
    chat: "Mapping[str, Any] | None",
    project: "Mapping[str, Any] | None",
    global_defaults: "Mapping[str, Any] | None",
    providers: Mapping[str, ProviderInfo],
    provider: str,
) -> str:
    if isinstance(chat, Mapping) and chat.get("model"):
        model = chat["model"]
    else:
        field_name = _model_field_for_provider(provider)
        if project and project.get(field_name):
            model = project[field_name]
        else:
            defaults = (global_defaults or {}).get("models") or {}
            model = defaults.get(provider)
            if not model:
                raise RuntimeResolutionError(
                    f"no model resolved for provider {provider!r} — no chat, project "
                    f"({field_name!r}) or global default supplied one"
                )
    info = providers.get(provider)
    if info is not None and info.models and model not in info.models:
        raise RuntimeResolutionError(
            f"resolved model {model!r} does not belong to provider {provider!r} "
            f"(known models: {', '.join(info.models)})"
        )
    return model


def _resolve_backend(
    chat: "Mapping[str, Any] | None",
    project: "Mapping[str, Any] | None",
    global_defaults: "Mapping[str, Any] | None",
    provider: str,
) -> str:
    if isinstance(chat, Mapping) and chat.get("backend"):
        return chat["backend"]
    if project and project.get("backend"):
        return project["backend"]
    defaults = (global_defaults or {}).get("backends") or DEFAULT_BACKENDS
    return defaults.get(provider, provider)


def _resolve_account(
    chat: "Mapping[str, Any] | None",
    project: "Mapping[str, Any] | None",
    accounts_mod: Any,
) -> str:
    """chat (strict, explicit pin) → project (soft, may degrade) → global (soft, may degrade).

    The strict/soft split is the whole point of this function. `accounts.resolve()` already
    implements the soft policy correctly (a broken project override falls through to the
    global active account instead of failing the run — that is documented and desired
    behaviour for a *default*). What it does NOT do, and must not be asked to do, is answer
    for an operator who explicitly picked an account on THIS chat: that pick is validated
    strictly and raises rather than silently running on a different subscription.
    """
    chat_account = (chat or {}).get("account") if isinstance(chat, Mapping) else None
    if chat_account:
        ok, reason = accounts_mod.validate(chat_account)
        if not ok:
            raise RuntimeResolutionError(
                f"chat pins account {chat_account!r}, which is unusable ({reason}) — "
                f"refusing to silently degrade to another subscription"
            )
        return chat_account
    project_account = (project or {}).get("account") if project else None
    return accounts_mod.resolve(project_account)


def resolve_runtime(
    *,
    chat_id: str,
    chat: "Mapping[str, Any] | None",
    providers: Mapping[str, ProviderInfo],
    project: "Mapping[str, Any] | None" = None,
    global_defaults: "Mapping[str, Any] | None" = None,
    turn_options: "Mapping[str, Any] | None" = None,
    revision: int = 0,
    accounts_mod: Any = _accounts_mod,
) -> RunContext:
    """Resolve the ONE runtime this turn runs on: chat → project → global default.

    `turn_options` (effort/ultracode/plan_mode/ask_mode) are per-turn values the caller has
    already decided for THIS message — they are carried through unchanged, never
    re-interpreted or silently cleared here. Deciding whether they are actually honourable
    on the resolved runtime is `capability_conflicts()`'s job, deliberately kept separate:
    resolving "what runtime" and judging "is that compatible with what was asked" are two
    different failure modes and conflating them is how the old ask_mode-silently-cleared bug
    (webapp.py:13272) happened in the first place.
    """
    provider = _resolve_provider(chat, project, global_defaults, providers)
    backend = _resolve_backend(chat, project, global_defaults, provider)
    model = _resolve_model(chat, project, global_defaults, providers, provider)
    account = _resolve_account(chat, project, accounts_mod)
    opts = turn_options or {}
    return RunContext(
        chat_id=chat_id,
        provider=provider,
        backend=backend,
        model=model,
        account=account,
        revision=revision,
        session_id=(chat or {}).get("session_id") if chat else None,
        codex_thread_id=(chat or {}).get("codex_thread_id") if chat else None,
        effort=opts.get("effort"),
        ultracode=bool(opts.get("ultracode", False)),
        plan_mode=bool(opts.get("plan_mode", False)),
        ask_mode=bool(opts.get("ask_mode", False)),
    )


# ---------------------------------------------------------------------------
# 4. validate_runtime_change — synchronous PATCH-time check
# ---------------------------------------------------------------------------

def validate_runtime_change(
    patch: Mapping[str, Any],
    *,
    providers: Mapping[str, ProviderInfo],
    accounts_list: Sequence[Mapping[str, Any]],
    current_provider: "str | None" = None,
) -> "tuple[bool, str]":
    """Is `patch` (a subset of {provider, model, backend, account}) a legal runtime change?

    Existence/availability only — this is the picker's "can this even be submitted" gate,
    not the strict-vs-soft account judgment `resolve_runtime()` makes at run time. An
    account that exists but is currently logged out is still a legal PATCH target (the UI
    shows it greyed, per spec-092's picker mockup); it only becomes a hard error if a run
    is actually launched against it as an explicit pin.
    """
    provider = patch["provider"] if "provider" in patch else current_provider
    if "provider" in patch:
        if provider not in providers:
            return False, f"unknown provider: {provider!r}"
        if not providers[provider].available:
            return False, f"provider {provider!r} is not currently available"

    if "model" in patch:
        model = patch["model"]
        if provider is None:
            return False, "cannot validate a model without a resolvable provider"
        if provider not in providers:
            return False, f"unknown provider: {provider!r}"
        info = providers[provider]
        if info.models and model not in info.models:
            return False, f"model {model!r} does not belong to provider {provider!r}"

    if "backend" in patch:
        backend = patch["backend"]
        if provider is None:
            return False, "cannot validate a backend without a resolvable provider"
        info = providers.get(provider)
        allowed = info.backends if (info and info.backends) else (DEFAULT_BACKENDS.get(provider, provider),)
        if backend not in allowed:
            return False, f"backend {backend!r} is not offered by provider {provider!r}"

    if "account" in patch:
        account = patch["account"]
        known_ids = {str(a.get("id")) for a in accounts_list}
        if str(account) not in known_ids:
            return False, f"unknown account: {account!r}"

    return True, ""


# ---------------------------------------------------------------------------
# 5. capability_conflicts — error, never downgrade
# ---------------------------------------------------------------------------

def capability_conflicts(rc: RunContext, capabilities: Mapping[str, bool]) -> list[str]:
    """Reasons `rc`'s requested per-turn options cannot be honoured by its own runtime.

    Contract for callers: a non-empty list means ERROR the run/PATCH, never silently drop
    the option and continue. That silent-downgrade path is exactly the measured bug this
    module exists to close (webapp.py:13272 clears `ask_mode` to False for a Codex run with
    no error and no visible sign to the operator that their approval gate just vanished).

    A capability key that is simply ABSENT from `capabilities` is treated as unsupported,
    not as "assume compatible" — fail-closed the same way `chat_provider()` is. (Today's
    live `/api/agent-providers` payload does not yet expose an `ask_mode` capability key at
    all; until the wiring PR adds one, every runtime's ask_mode requests will conflict here,
    which is the safe default, not a bug in this function.)
    """
    conflicts: list[str] = []
    if rc.ask_mode and not capabilities.get("ask_mode"):
        conflicts.append(
            f"{rc.provider} cannot honour ask_mode (no per-tool approval hook available)"
        )
    if rc.plan_mode and not capabilities.get("plan_mode"):
        conflicts.append(f"{rc.provider} does not support plan_mode")
    if rc.ultracode and not capabilities.get("multi_agent"):
        conflicts.append(f"{rc.provider} does not support multi-agent (ultracode) turns")
    return conflicts


# ---------------------------------------------------------------------------
# 6. apply_change — compare-and-swap
# ---------------------------------------------------------------------------

def apply_change(
    chat: MutableMapping[str, Any],
    patch: Mapping[str, Any],
    expected_revision: int,
    *,
    providers: "Mapping[str, ProviderInfo] | None" = None,
    accounts_list: "Sequence[Mapping[str, Any]] | None" = None,
) -> "tuple[bool, str, dict]":
    """Apply `patch` to `chat` iff `chat`'s current revision matches `expected_revision`.

    Two tabs PATCHing the same chat's runtime without this would clobber each other with no
    warning (spec-092 §7). The revision travels round-trip with the picker UI: a tab reads
    it with the chat, submits it back unchanged, and a rejection here means "someone else
    changed the runtime since you loaded it — reload and decide again" rather than silently
    overwriting their change or applying yours on top of stale assumptions.

    `chat` is mutated in place on success (matching the rest of this codebase's chats.json
    handling — see `api_project_chats_patch`), so the caller's existing load→mutate→persist
    pattern keeps working unchanged. Returns (ok, error, snapshot) — snapshot is always a
    fresh dict copy, safe to serialize as the API response either way.

    Validation is optional (`providers`/`accounts_list` default to None, meaning "the caller
    already validated via validate_runtime_change() and is not asking this function to redo
    it") — but when provided, an invalid patch is rejected even with a matching revision;
    a stale-but-valid revision check must not be the ONLY gate before mutating shared state.
    """
    current_revision = int(chat.get("runtime_revision", 0))
    if current_revision != expected_revision:
        return (
            False,
            f"stale revision: chat is at {current_revision}, patch expected {expected_revision}",
            dict(chat),
        )
    if providers is not None:
        ok, reason = validate_runtime_change(
            patch,
            providers=providers,
            accounts_list=accounts_list or [],
            current_provider=chat.get("provider"),
        )
        if not ok:
            return False, reason, dict(chat)
    for key in ("provider", "model", "backend", "account"):
        if key in patch:
            chat[key] = patch[key]
    chat["runtime_revision"] = current_revision + 1
    chat["runtime_updated_at"] = time.time()
    return True, "", dict(chat)


# ---------------------------------------------------------------------------
# Ollama leak guard (spec-092 P3) — pure env-dict construction
# ---------------------------------------------------------------------------

def ollama_env_overlay(
    rc: RunContext,
    *,
    base_url: str,
    auth_token: str = "local",
    haiku_model: "str | None" = None,
) -> dict[str, str]:
    """Env additions that route a Claude-Code-harness turn at the ollama shim.

    Empty dict unless the resolved runtime is BOTH `provider == "claude"` (same agent
    harness/tool loop) AND `backend == "ollama"` (the endpoint swap). That double condition
    is the leak guard spec-092 calls for by name: a run whose backend is anything else must
    never receive `ANTHROPIC_BASE_URL`, or it silently redirects subscription-billed traffic
    to a $0 local shim and looks like a normal working turn (`test_ollama_env_never_leaks_
    into_claude_run` exists specifically to pin this).
    """
    if rc.provider != DEFAULT_PROVIDER or rc.backend != "ollama":
        return {}
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "ANTHROPIC_MODEL": rc.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku_model or rc.model,
    }


__all__ = [
    "DEFAULT_PROVIDER", "DEFAULT_BACKENDS",
    "RuntimeResolutionError", "ProviderInfo", "RunContext",
    "available_providers", "chat_provider", "resolve_runtime",
    "validate_runtime_change", "capability_conflicts", "apply_change",
    "ollama_env_overlay",
]
