"""One place a run's runtime (provider x account x model) gets decided.

Today that decision is re-derived independently in four call sites (chat POST, the chat
queue drain, board cards, autopilot), each reading a slightly different source, with two
measured hazards:

  * ``_chat_provider`` (webapp.py) returns ``"claude"`` for ANY unrecognised provider value
    -- fail-OPEN. Harmless with two providers, a real hazard once a third (Ollama, planned)
    exists: an unavailable/unknown provider must never silently become a Claude billing
    event.
  * ``accounts.resolve()`` degrades a broken account to ``main`` -- correct for a *project
    default* (an operator never explicitly asked for that account this turn) and wrong as
    the answer to an *explicit* "run this chat on `work`" (the operator asked for a specific
    subscription; silently running on another one is a wrong-billing bug wearing a
    resilience feature's clothes).

This module is the single place that inheritance and fail-closed validation happen, so the
four call sites can all defer to it instead of re-implementing (and re-diverging on) the
same policy. It is pure logic: no aiohttp, no ``webapp.py``/``engine.py`` import. The only
I/O it performs is reading the accounts registry (``accounts.py``), which the design brief
explicitly allows -- everything else (the chat record, the project record, the live
provider registry) is handed in as plain data the caller already loaded.

See ``docs/internal/specs/spec-092-unified-runtime-picker.md`` for the full design (that
directory is gitignored -- internal design history, not shipped -- so this module carries
the load-bearing decisions inline as docstrings/comments instead of only living in the spec).

Design note -- ``provider`` vs ``backend``:
    ``provider`` is which agent HARNESS runs the turn (its tool loop, hooks, sub-agent
    support): ``claude`` (Claude Code) or ``codex`` (OpenAI Codex). ``backend`` is which
    inference ENDPOINT actually answers the model calls. The default backend for every
    provider is ``""`` (native -- the CLI's own subscription auth), matching the convention
    already shipped in ``engine.py``'s live-client fingerprint (``backend=""`` is
    byte-identical to a caller that never mentions the argument). Ollama is NOT its own
    provider: the picker's "Ollama (local)" menu row means ``provider="claude"`` (same
    harness -- same tools, hooks, plan/ask mode, sub-agents) with ``backend="ollama"`` (an
    env overlay swaps only the endpoint, pointing ``ANTHROPIC_BASE_URL`` at a local shim).
    Collapsing the two into one field would make that combination unrepresentable without
    inventing a fake third "provider" that is not a different harness at all.

    Env leak surface: the four ``ANTHROPIC_*`` overlay vars (see ``ollama_env_overlay``)
    are inherited by the WHOLE subprocess tree the CLI spawns for that run -- including
    every ``Bash`` tool call the agent makes. A shell command the agent runs also sees the
    shim endpoint via the ordinary child-inherits-parent-env rule; this is not scoped to the
    SDK client alone.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, MutableMapping, Sequence

import accounts as _accounts_mod

# The bedrock provider. Every fallback chain in this module bottoms out here, matching the
# hardcoded default already in webapp.py's `_chat_provider` and `_effective_card_provider` --
# this module generalizes that rule, it does not change its outcome for existing installs.
DEFAULT_PROVIDER = "claude"

# The native/default inference backend for any provider -- matches engine.py's
# `_compute_fingerprint(..., backend="")` convention exactly, so a RunContext's `backend`
# field can be threaded straight into `run_engine(backend=rc.backend)` with no translation.
DEFAULT_BACKEND = ""

# A caller may mistake the picker's "Ollama (local)" row for a fourth registry provider.
# It is not one -- see the module docstring -- so this string is refused as a `provider`
# value everywhere in this module, regardless of what a live registry snapshot might
# (incorrectly) contain under that key.
OLLAMA_BACKEND = "ollama"

# The env vars an ollama overlay sets -- and, just as importantly, the vars a NON-ollama run
# must actively clear. Order matches the CLI's own read order in the spec.
OLLAMA_ENV_VAR_NAMES: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

# The origins a RunContext can be built for. `chat` is the interactive cockpit turn; `card`
# is a kanban auto-run (webapp.py's `_run_card`); `director` is the fully autonomous
# autopilot loop (`features/autopilot/director.py`) which has no chat/card entity at all;
# `wake` is a completion-wake enqueue (webapp.py's post-run wake, today built with
# `chat_id=None`) whose `origin_id` is its PARENT run's id -- see `wake_context_from()`.
ORIGIN_KINDS: tuple[str, ...] = ("chat", "card", "director", "wake")


class RuntimeResolutionError(ValueError):
    """Raised whenever this module cannot make a decision safely.

    One exception type covers the whole module's contract on purpose: a bare `ValueError`
    from a constructor and a `RuntimeResolutionError` from `resolve_runtime()` used to be
    two different things a caller had to catch separately; now `except RuntimeResolutionError`
    catches everything this module refuses to decide, including RunContext's own invariants.
    """


# ---------------------------------------------------------------------------
# The live provider registry, as pure data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderInfo:
    """One row of the live provider registry (the shape ``GET /api/agent-providers``
    already returns, minus the HTTP wrapper). The caller builds these from that endpoint's
    payload or from the in-process provider modules directly -- this module never fetches
    the registry itself, it only reasons about rows handed to it.
    """
    provider: str
    available: bool
    models: tuple[str, ...] = ()
    # Endpoints this provider's harness can be pointed at, INCLUDING its own native one
    # (conventionally listed as ""). Empty tuple means "not stated" -- validation then
    # falls back to `(DEFAULT_BACKEND,)`, i.e. only the native endpoint is legal.
    backends: tuple[str, ...] = ()
    # Models a NON-NATIVE backend serves, keyed by backend id. A local endpoint runs its own
    # model names (`qwen3.8:27b-q4_K_M`), which have nothing to do with the provider's cloud
    # aliases -- validating an ollama selection against `models` would reject every legal
    # choice. A backend absent from this map falls back to `models` (the native case).
    backend_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Per-turn options this runtime can actually honour. Absence of a key means "cannot
    # honour it" (fail-closed default), not "assume yes" -- see capability_conflicts().
    capabilities: Mapping[str, bool] = field(default_factory=dict)


def available_providers(providers: Mapping[str, ProviderInfo]) -> dict[str, bool]:
    """Bridge from the registry shape to the `known_providers` map chat_provider() wants."""
    return {pid: info.available for pid, info in providers.items()}


# ---------------------------------------------------------------------------
# The immutable decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunContext:
    """The runtime pinned for ONE turn, at the moment it is ACCEPTED.

    This is deliberately NOT re-derived at drain time (the chat queue's measured bug:
    `_chat_queue_enqueue` stores effort/ultracode/plan_mode/ask_mode but not
    provider/model/account, so a message typed against one runtime can drain against
    whatever the chat record says *later*, after an operator switched it mid-queue). A
    `RunContext` is the fix: build it once when the message is accepted, store it WITH the
    queued item, and hand it to the engine unchanged at drain time.

    Identity is a discriminated origin, not a bare `chat_id` -- three of this module's four
    integration points have no chat entity at all: board cards (their own card id), the
    autopilot director (fully autonomous, nothing to name), and a completion wake (which
    inherits its PARENT RUN's context wholesale -- see `wake_context_from()`, its
    `origin_id` is that parent run's id, not a new identity of its own).

    `session_id` and `codex_thread_id` are both carried regardless of which `provider` is
    currently active -- spec-092's handoff design keeps both ids forever so that switching
    back to a provider this chat has used before can resume its own conversation instead of
    starting cold.
    """
    origin_kind: str
    origin_id: str
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
        if self.origin_kind not in ORIGIN_KINDS:
            raise RuntimeResolutionError(
                f"unknown origin_kind {self.origin_kind!r} (must be one of {ORIGIN_KINDS})"
            )
        if not self.origin_id:
            raise RuntimeResolutionError("RunContext requires a non-empty origin_id")
        if not self.provider:
            raise RuntimeResolutionError("RunContext requires a non-empty provider")
        # backend == "" (DEFAULT_BACKEND) is the normal, valid, native case -- only reject
        # a genuinely missing value (None would violate the type, "" is legal and common).
        if self.backend is None:
            raise RuntimeResolutionError("RunContext requires a backend (use \"\" for native)")
        if not self.model:
            raise RuntimeResolutionError("RunContext requires a non-empty model")
        if not self.account:
            raise RuntimeResolutionError("RunContext requires a resolved account (never a placeholder)")
        if self.revision < 0:
            raise RuntimeResolutionError("revision must be >= 0")


def wake_context_from(parent: RunContext, wake_id: str) -> RunContext:
    """A completion wake inherits its PARENT run's entire resolved runtime unchanged.

    This is NOT a re-resolution -- resolve_runtime() must not be called again for a wake,
    because by definition nothing about the runtime is supposed to have changed between the
    run that finished and the wake it triggers. Only the origin identity changes.
    """
    if not wake_id:
        raise RuntimeResolutionError("wake_context_from requires a non-empty wake_id")
    return replace(parent, origin_kind="wake", origin_id=wake_id)


# ---------------------------------------------------------------------------
# 3. chat_provider -- the ONE place a chat record's own provider signal is read
# ---------------------------------------------------------------------------

class ProviderStatus(Enum):
    """Three states, not two, because the caller's remedy differs:

    * OK          -- a usable value (including the legacy no-key -> claude default).
    * UNAVAILABLE -- a KNOWN provider that is currently down (e.g. the spec's measured
                     Ollama port closing three minutes after a probe). Temporary: the
                     selection is still meaningful and should stay visible/retryable in
                     the UI, not be treated as a data error.
    * UNKNOWN     -- the record names a provider that is not in the registry at all (e.g.
                     one removed years ago). Permanent: the selection itself is stale.
    """
    OK = "ok"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderLookup:
    """Result of chat_provider(): `status` plus the relevant value.

    For OK, `value` is the resolved provider id (including the legacy default). For
    UNAVAILABLE/UNKNOWN, `value` is the raw string the chat record named (for the error
    message a caller builds), or None if the record's `provider` key was not even a string.
    """
    status: ProviderStatus
    value: "str | None"


def chat_provider(chat: "Mapping[str, Any] | None", *, known_providers: Mapping[str, bool]) -> ProviderLookup:
    """What THIS CHAT RECORD alone says about its provider -- the single source of truth.

    `resolve_runtime()` defers to this function for every chat it is given (not just
    records that happen to name a provider) -- a chat with no `provider` key at all gets
    EXACTLY the same answer here as it does standalone, with no separate availability
    re-check layered on top elsewhere. Two independent readings of the same input used to
    disagree (this function said "claude, unconditionally" for a no-key record while
    `resolve_runtime()` re-validated that answer against the registry and could raise on
    it) -- that duplication is gone; this is the only place the judgment is made.

    Two distinct rules:

    * **Legacy compatibility**: a chat record with NO `provider` key at all predates the
      provider field entirely and is unconditionally OK/"claude". This is NOT availability-
      checked against `known_providers` -- those records were created before a registry
      existed, and the whole point of the compatibility rule is that they must keep
      behaving exactly as they always have, independent of whatever the registry says today
      (even a hypothetical future where "claude" itself is marked unavailable).
    * **Fail-closed** (the fix this module exists for): a chat record that DOES name a
      provider gets UNKNOWN if that value never appears in `known_providers` at all, or
      UNAVAILABLE if it is a registered key currently marked falsy. The caller MUST treat
      either as an error -- it must never fall back to "claude" or any other default. (The
      silent fallback this replaces is exactly the bug this module exists to remove:
      webapp.py's old `_chat_provider` returned "claude" for an unrecognised value with no
      error at all.)

    `known_providers` maps provider id -> available (bool); build it with
    `available_providers()` from a live registry.
    """
    if not isinstance(chat, Mapping) or "provider" not in chat:
        return ProviderLookup(ProviderStatus.OK, DEFAULT_PROVIDER)
    value = chat.get("provider")
    if not isinstance(value, str):
        return ProviderLookup(ProviderStatus.UNKNOWN, None)
    if value not in known_providers:
        return ProviderLookup(ProviderStatus.UNKNOWN, value)
    if not known_providers[value]:
        return ProviderLookup(ProviderStatus.UNAVAILABLE, value)
    return ProviderLookup(ProviderStatus.OK, value)


# ---------------------------------------------------------------------------
# 2. resolve_runtime -- the single inheritance chain
# ---------------------------------------------------------------------------

def _model_field_for_provider(provider: str) -> str:
    """Project-level model field name for a provider.

    `claude` keeps the legacy bare `model` key (it predates multi-provider support and
    renaming it would touch every existing project record for no behavioural gain). Every
    later provider follows `<provider>_model` -- this already matches the real `codex_model`
    field in `_PROJECT_SETTING_FIELDS` and generalizes to a future `ollama_model` without
    hardcoding "codex" as a special case anywhere in this function.
    """
    return "model" if provider == DEFAULT_PROVIDER else f"{provider}_model"


def _resolve_provider(
    chat: "Mapping[str, Any] | None",
    global_defaults: "Mapping[str, Any] | None",
    providers: Mapping[str, ProviderInfo],
) -> str:
    """chat's OWN signal (via chat_provider(), the single source of truth) when there is a
    chat at all; otherwise falls through to the global default and the bedrock provider.

    There is deliberately NO project tier here today: the only real per-project provider-ish
    field in the current schema is `board_provider`, which is a CARD-scoped compatibility
    default with its own different fallback shape (project override, not "claude"), not a
    generic chat-provider default. Inventing a `project.default_provider` field this
    function alone would read (and no wiring would ever set) is worse than an honest gap --
    when a real project-level chat-provider setting gets a name (next to `board_provider`),
    it slots in here as its own tier without disturbing the chat-present branch above.
    """
    if chat is not None:
        lookup = chat_provider(chat, known_providers=available_providers(providers))
        if lookup.status is ProviderStatus.OK:
            return lookup.value  # type: ignore[return-value]
        if lookup.status is ProviderStatus.UNAVAILABLE:
            raise RuntimeResolutionError(
                f"chat pins provider {lookup.value!r}, which is temporarily unavailable -- "
                f"the selection is still valid, retry once it is back"
            )
        raise RuntimeResolutionError(
            f"chat pins provider {lookup.value!r}, which is not a registered provider -- "
            f"this selection is stale and must be changed"
        )
    candidate = (global_defaults or {}).get("provider") or DEFAULT_PROVIDER
    if candidate not in providers or not providers[candidate].available:
        raise RuntimeResolutionError(
            f"resolved provider {candidate!r} (global default) is not available"
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
                    f"no model resolved for provider {provider!r} -- no chat, project "
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
    defaults = (global_defaults or {}).get("backends") or {}
    return defaults.get(provider, DEFAULT_BACKEND)


def _resolve_account(
    chat: "Mapping[str, Any] | None",
    project: "Mapping[str, Any] | None",
    accounts_mod: Any,
) -> str:
    """chat (strict, explicit pin) -> project (soft, may degrade) -> global (soft, may degrade).

    The strict/soft split is the whole point of this function. `accounts.resolve()` already
    implements the soft policy correctly (a broken project override falls through to the
    global active account instead of failing the run -- that is documented and desired
    behaviour for a *default*). What it does NOT do, and must not be asked to do, is answer
    for an operator who explicitly picked an account on THIS chat: that pick is validated
    strictly and raises rather than silently running on a different subscription.
    """
    chat_account = (chat or {}).get("account") if isinstance(chat, Mapping) else None
    if chat_account:
        ok, reason = accounts_mod.validate(chat_account)
        if not ok:
            raise RuntimeResolutionError(
                f"chat pins account {chat_account!r}, which is unusable ({reason}) -- "
                f"refusing to silently degrade to another subscription"
            )
        return chat_account
    project_account = (project or {}).get("account") if project else None
    return accounts_mod.resolve(project_account)


def resolve_runtime(
    *,
    origin_kind: str,
    origin_id: str,
    chat: "Mapping[str, Any] | None",
    providers: Mapping[str, ProviderInfo],
    project: "Mapping[str, Any] | None" = None,
    global_defaults: "Mapping[str, Any] | None" = None,
    turn_options: "Mapping[str, Any] | None" = None,
    revision: int = 0,
    accounts_mod: Any = _accounts_mod,
) -> RunContext:
    """Resolve the ONE runtime this turn runs on: chat -> project -> global default.

    `chat` here is specifically the per-CHAT override record (its own `provider`, `model`,
    `account`, `backend`, `session_id`, `codex_thread_id` keys) -- this function's fallback
    shape (legacy no-key -> claude) is chat-specific. A `card`/`director`/`wake` origin that
    needs a RunContext either has its own, DIFFERENT inheritance rule (e.g. a card's
    compatibility default is its project's `board_provider`, not "claude" -- see
    `_effective_card_provider` in webapp.py) and should resolve its own fields before
    constructing `RunContext` directly, or (for `wake`) should call `wake_context_from()`
    instead of this function entirely.

    `turn_options` (effort/ultracode/plan_mode/ask_mode) are per-turn values the caller has
    already decided for THIS message -- they are carried through unchanged, never
    re-interpreted or silently cleared here. Deciding whether they are actually honourable
    on the resolved runtime is `capability_conflicts()`'s job, deliberately kept separate:
    resolving "what runtime" and judging "is that compatible with what was asked" are two
    different failure modes and conflating them is how the old ask_mode-silently-cleared bug
    (webapp.py:13272) happened in the first place.
    """
    provider = _resolve_provider(chat, global_defaults, providers)
    backend = _resolve_backend(chat, project, global_defaults, provider)
    model = _resolve_model(chat, project, global_defaults, providers, provider)
    account = _resolve_account(chat, project, accounts_mod)
    opts = turn_options or {}
    return RunContext(
        origin_kind=origin_kind,
        origin_id=origin_id,
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
# 4. validate_runtime_change -- validates the RESULTING state, not the patch alone
# ---------------------------------------------------------------------------

def validate_runtime_change(
    patch: Mapping[str, Any],
    *,
    providers: Mapping[str, ProviderInfo],
    accounts_list: Sequence[Mapping[str, Any]],
    current: "Mapping[str, Any] | None" = None,
) -> "tuple[bool, str]":
    """Is `patch` (a subset of {provider, model, backend, account}) a legal runtime change?

    Validates the RESULT of applying `patch` on top of `current` (the chat's present
    provider/model/backend), not the patch in isolation -- `{"provider": "codex"}` alone
    used to pass because only `"model" in patch` was checked; the chat kept its old Claude
    model and the very next turn died in `resolve_runtime()`'s model check. A change that
    would leave the record unrunnable is rejected HERE, at PATCH time.

    Existence/availability, not run-time strictness -- this is the picker's "can this even
    be submitted" gate, not the strict-vs-soft account judgment `resolve_runtime()` makes at
    run time. An account that exists but is currently logged out is still a legal PATCH
    target (the UI shows it greyed, per spec-092's picker mockup); it only becomes a hard
    error if a run is actually launched against it as an explicit pin.
    """
    resulting = {**(current or {}), **patch}
    # A pre-provider-field chat record has NO `provider` key at all, and the module's own
    # legacy rule says that record is Claude (see chat_provider). A raw dict merge does not
    # know that, so a model-only PATCH on any chat created before the field existed was
    # rejected as "no resolvable provider" -- the operator could not even change the model on
    # their oldest chats. Resolve the same way the rest of the module does instead of
    # re-deriving it here: one legacy rule, one place.
    provider = resulting.get("provider")
    if provider is None and "provider" not in patch and isinstance(current, Mapping) \
            and "provider" not in current:
        # `current` must be a REAL record that simply predates the field. A `current=None`
        # caller has no record at all and gets no legacy default -- "I did not give you the
        # chat" is not the same statement as "this chat predates the provider field".
        legacy = chat_provider(current, known_providers={p: True for p in providers})
        if legacy.status is ProviderStatus.OK:
            provider = legacy.value
            resulting["provider"] = provider

    if "provider" in patch and patch["provider"] == OLLAMA_BACKEND:
        return False, (
            f"{OLLAMA_BACKEND!r} is not a provider -- select provider={DEFAULT_PROVIDER!r} "
            f"with backend={OLLAMA_BACKEND!r} instead"
        )

    if provider is None:
        if "model" in patch:
            return False, "cannot validate a model without a resolvable provider (pass `current` or include `provider` in the patch)"
        if "backend" in patch:
            return False, "cannot validate a backend without a resolvable provider (pass `current` or include `provider` in the patch)"
    else:
        if provider not in providers:
            return False, f"unknown provider: {provider!r}"
        if not providers[provider].available:
            return False, f"provider {provider!r} is not currently available"

        info = providers[provider]
        backend = resulting.get("backend")
        if backend is not None:
            allowed = info.backends if info.backends else (DEFAULT_BACKEND,)
            if backend not in allowed:
                return False, f"backend {backend!r} is not offered by provider {provider!r}"

        # The model must belong to the ENDPOINT that will serve it, not to the provider in
        # the abstract: a local backend runs its own model names. Checked after the backend
        # so a patch that moves both dimensions at once is judged against its own result.
        model = resulting.get("model")
        legal_models = info.models
        if backend:
            # `or info.models` would be wrong here: an EMPTY entry for a non-native backend
            # means "this endpoint's model list is unknown", and falling back to the cloud
            # aliases would then accept `sonnet` as a legal pick for a local box that has no
            # such model — a turn that dies on its first token. Unknown is a refusal, which
            # is this module's fail-closed rule everywhere else.
            backend_list = info.backend_models.get(backend)
            if not backend_list:
                return False, (
                    f"no models are known for backend {backend!r} -- it cannot be selected "
                    f"until its model list is available"
                )
            legal_models = backend_list
        if model is None:
            if "provider" in patch or ("backend" in patch and patch["backend"]):
                where = f"backend {backend!r}" if backend else f"provider {provider!r}"
                return False, (
                    f"switching to {where} leaves no model for it -- include "
                    f"a model that belongs to it in the same patch"
                )
        elif legal_models and model not in legal_models:
            where = f"backend {backend!r}" if backend else f"provider {provider!r}"
            return False, f"model {model!r} does not belong to {where}"

    if "account" in patch:
        account = patch["account"]
        # null/"" is the INHERIT marker (chat -> project -> global active), not an account id.
        # resolve_runtime()'s own chain already reads a missing account exactly that way, and
        # the picker needs a way to hand a pinned chat BACK to the project default -- without
        # this branch `str(None)` was compared against the id set and every "inherit" pick was
        # rejected as an unknown account.
        if account not in (None, ""):
            known_ids = {str(a.get("id")) for a in accounts_list}
            if str(account) not in known_ids:
                return False, f"unknown account: {account!r}"

    return True, ""


# ---------------------------------------------------------------------------
# 5. capability_conflicts -- error, never downgrade
# ---------------------------------------------------------------------------

def capability_conflicts(rc: RunContext, capabilities: Mapping[str, bool]) -> list[str]:
    """Reasons `rc`'s requested per-turn options cannot be honoured by its own runtime.

    Contract for callers: a non-empty list means ERROR the run/PATCH, never silently drop
    the option and continue. That silent-downgrade path is exactly the measured bug this
    module exists to close (webapp.py:13272 clears `ask_mode` to False for a Codex run with
    no error and no visible sign to the operator that their approval gate just vanished).

    A capability key that is simply ABSENT from `capabilities` is treated as unsupported,
    not as "assume compatible" -- fail-closed the same way `chat_provider()` is. (Today's
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
# 6. apply_change -- compare-and-swap, returning a NEW dict
# ---------------------------------------------------------------------------

def _coerce_revision(value: Any, *, label: str) -> "tuple[int | None, str]":
    """Parse a revision-like value strictly. None/missing -> 0 (a brand-new record has never
    been revised). A negative, non-integer-valued float, or unparsable string is a DATA
    ERROR reported through the caller's normal (ok, reason) contract -- never a bare
    exception, and never silently coerced/truncated into something that gets baked forward.
    """
    if value is None:
        return 0, ""
    if isinstance(value, bool):  # bool is an int subclass; not a real revision, reject it
        return None, f"{label} must be an integer, got a bool ({value!r})"
    if isinstance(value, int):
        if value < 0:
            return None, f"{label} must be >= 0, got {value}"
        return value, ""
    if isinstance(value, float):
        if value.is_integer() and value >= 0:
            return int(value), ""
        return None, f"{label} must be a non-negative whole number, got {value}"
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None, f"{label} must be an integer, got {value!r}"
        if parsed < 0:
            return None, f"{label} must be >= 0, got {parsed}"
        return parsed, ""
    return None, f"{label} must be an integer, got {type(value).__name__}"


_RUNTIME_PATCH_KEYS = ("provider", "model", "backend", "account")


def apply_change(
    chat: Mapping[str, Any],
    patch: Mapping[str, Any],
    expected_revision: Any,
    *,
    providers: "Mapping[str, ProviderInfo] | None" = None,
    accounts_list: "Sequence[Mapping[str, Any]] | None" = None,
) -> "tuple[bool, str, dict]":
    """Compute the next state of `chat` after `patch`, iff its revision matches, WITHOUT
    mutating `chat` -- the caller decides when (and whether) to swap the returned dict in.

    Two tabs PATCHing the same chat's runtime without a CAS would clobber each other with no
    warning (spec-092 Sec7). The revision travels round-trip with the picker UI: a tab reads
    it with the chat, submits it back unchanged, and a rejection here means "someone else
    changed the runtime since you loaded it -- reload and decide again."

    In-memory CAS is NOT sufficient on its own. Two requests can both read revision 0
    from disk, both pass this function's comparison (each computed independently, in
    memory, against the same stale snapshot), and the second write clobbers the first. The
    caller MUST hold the project's `_chats_lock()` (or equivalent) across the WHOLE
    read -> apply_change() -> persist cycle -- this function only refuses a revision it can
    SEE is stale; it cannot see a concurrent writer racing it between calls.

    A patch that changes nothing (empty, only unrecognised keys, or values identical to the
    current ones) returns `ok=True` WITHOUT bumping `runtime_revision` -- minting a fresh
    revision for a no-op patch would false-reject the next legitimate writer for no reason.

    `expected_revision`/the chat's stored `runtime_revision` are both parsed with
    `_coerce_revision` (see there): a `"1"` from JSON compares equal to `1`; a corrupt value
    (negative, non-integer, unparsable) is reported through the normal `(ok, reason, dict)`
    contract, never a bare exception baked forward into the record.
    """
    stored_revision, err = _coerce_revision(chat.get("runtime_revision"), label="chat's stored runtime_revision")
    if err:
        return False, err, dict(chat)
    wanted_revision, err = _coerce_revision(expected_revision, label="expected_revision")
    if err:
        return False, err, dict(chat)
    if stored_revision != wanted_revision:
        return False, (
            f"stale revision: chat is at {stored_revision}, patch expected {wanted_revision}"
        ), dict(chat)

    if providers is not None:
        ok, reason = validate_runtime_change(
            patch, providers=providers, accounts_list=accounts_list or [], current=chat,
        )
        if not ok:
            return False, reason, dict(chat)

    changed = {
        key: value for key, value in patch.items()
        if key in _RUNTIME_PATCH_KEYS and chat.get(key) != value
    }
    if not changed:
        return True, "", dict(chat)

    new_chat = dict(chat)
    new_chat.update(changed)
    new_chat["runtime_revision"] = stored_revision + 1
    new_chat["runtime_updated_at"] = time.time()
    return True, "", new_chat


# ---------------------------------------------------------------------------
# Ollama leak guard (spec-092 P3) -- pure env-dict construction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvOverlay:
    """What a caller must DO to a run's env, not just what to add.

    `to_set` are the vars to inject; `to_unset` are names that must be ACTIVELY removed from
    the run's env (not merely "not added") -- if `ANTHROPIC_BASE_URL` is already present in
    the service's own environment (a systemd unit, a leftover debugging export), a non-
    ollama run inherits it and silently talks to the shim unless something removes it. An
    empty dict used to mean "no overlay" with no way to express "and clear whatever might
    already be there" -- that is the gap this type closes.
    """
    to_set: Mapping[str, str]
    to_unset: tuple[str, ...]


def ollama_env_overlay(
    rc: RunContext,
    *,
    base_url: "str | None" = None,
    auth_token: str = "local",
    haiku_model: "str | None" = None,
) -> EnvOverlay:
    """Env additions/removals that route (or don't route) a run at the ollama shim.

    Gated on `rc.backend == "ollama"` ALONE -- not on `rc.provider`. The picker's "Ollama
    (local)" menu row is `provider="claude"` + `backend="ollama"` (see the module
    docstring); gating on provider as well would mean a plain `provider="claude"` chat that
    forgot to also carry `backend="ollama"` for some reason gets no overlay while still
    being labelled Ollama in the UI -- backend is the field that actually decides which
    endpoint gets used, so it is the only field this function consults.

    Every non-ollama call returns `to_unset=OLLAMA_ENV_VAR_NAMES` -- the caller must clear
    those names from the run's env, not just skip adding them, or a leftover value from
    outside this module's control silently redirects subscription-billed traffic to a $0
    local shim while looking like a normal working turn
    (`test_ollama_env_never_leaks_into_claude_run` pins exactly this).
    """
    if rc.backend != OLLAMA_BACKEND:
        return EnvOverlay(to_set={}, to_unset=OLLAMA_ENV_VAR_NAMES)
    if not base_url:
        raise RuntimeResolutionError("an ollama backend requires a base_url to overlay")
    return EnvOverlay(
        to_set={
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "ANTHROPIC_MODEL": rc.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku_model or rc.model,
        },
        to_unset=(),
    )


# ─────────────────── which `claude` binary serves a run ───────────────────
# The SDK ships its own CLI under claude_agent_sdk/_bundled/ and PREFERS it over PATH.
# That binary owns two decisions nothing in this repo can make:
#   1. what a bare alias resolves to (`opus` -> claude-opus-5 vs claude-opus-5-5), and
#   2. whether the API serves the model at all — an id newer than the CLI is a hard 400:
#      "Claude Code 2.1.276 does not support this model; version 2.1.280 or newer is
#      required" (measured 2026-09-22 on claude-opus-5-5, released the day before).
# So a model can ship days before any claude-agent-sdk release bundles a CLI new enough to
# run it, and in that window the cockpit cannot reach it by alias OR by explicit id.
# CLAUDE_CLI_PATH points the SDK at an externally installed CLI
# (`npm i -g --prefix ~/.npm-global @anthropic-ai/claude-code@<ver>`) for exactly that
# window. Unset, or pointing at anything that is not an executable file, means the bundled
# binary — i.e. previous behaviour, byte for byte. It lives HERE and not in engine.py so
# that webapp's own SDK calls (the /rotate handoff summarizer) resolve it identically
# without importing engine, which would be circular.


def resolve_cli_path(raw: "str | None" = None) -> "str | None":
    """The CLI override, or None to let the SDK use its bundled binary.

    A misconfigured value degrades to the bundle instead of breaking every run: a typo in
    .env must not take the cockpit down. Surrounding quotes are tolerated because the
    cockpit's .env loader (bot.py `_load_env`) does not strip them.
    """
    value = (os.getenv("CLAUDE_CLI_PATH") if raw is None else raw) or ""
    value = value.strip().strip('"').strip("'")
    if not value:
        return None
    path = os.path.expanduser(value)
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    print(f"[cli-path] CLAUDE_CLI_PATH={value!r} is not an executable file — "
          "falling back to the SDK's bundled CLI")
    return None


CLI_PATH: "str | None" = resolve_cli_path()
if CLI_PATH:
    print(f"[cli-path] external CLI in use: {CLI_PATH}")


__all__ = [
    "CLI_PATH", "resolve_cli_path",
    "DEFAULT_PROVIDER", "DEFAULT_BACKEND", "OLLAMA_BACKEND", "OLLAMA_ENV_VAR_NAMES",
    "ORIGIN_KINDS",
    "RuntimeResolutionError", "ProviderInfo", "ProviderStatus", "ProviderLookup",
    "RunContext", "wake_context_from",
    "available_providers", "chat_provider", "resolve_runtime",
    "validate_runtime_change", "capability_conflicts", "apply_change",
    "EnvOverlay", "ollama_env_overlay",
]
