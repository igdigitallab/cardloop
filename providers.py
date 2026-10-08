"""The table of engines a cockpit turn can run on (spec-095 D6).

Before this module every call site that had to pick an engine, a resume kwarg, a continuity
field or a default model did it with its own `provider == "codex"` ternary — two dozen of them in
webapp.py, each of which quietly resolved any other provider name to Claude. A third provider
would have turned every one of them into a three-way branch, so the facts that differ per
provider live here, once, and the call sites ask the table.

Claude is the cockpit's own harness (`is_default`): it takes the full engine kwarg set, resolves
models through the Claude alias list and mirrors its session id into the legacy flat
`ctx["sessions"]` map. Every other provider is an adapter around a foreign CLI with its own model
ids, a `<name>_model` project field and the compact engine kwarg set — the convention
`runtime.model_field_for_provider` already encodes.

Adapter engines receive the per-turn `effort` exactly as the cockpit sent it, including the
cockpit-only "ultra" that is cleared for Claude alone — an adapter engine must whitelist the levels
it understands (codex_engine does).

A provider may carry a per-project privacy GATE (`gate`): a pure `project -> refusal text | None`
hook, default open. It is the ONE place such a rule lives; `webapp._provider_gate_refusal` is the only
caller, and every site that selects the provider (chat create, PATCH, card, board default) or launches
a run (queue drain, direct POST, card) asks it — a refusal is an error, never a reason to run on
another engine. NO registered provider uses it: Grok was gated per project (spec-095 D5) until the
operator decided that choosing a provider in the picker IS the consent, as it is for Codex and Claude.

Deliberately NOT here: history readers, session lists, usage, rate limits, search and the
`/api/agent-providers` rows. Those differ inherently per provider and a table would only hide it.
Nor are the Claude-only feature gates (account pinning, ultracode, auto-rotate, reconcile): they
stay `== "claude"` at their sites, because a new provider must NOT inherit them by default.

This module must stay importable from `board.py`, so it never imports `webapp`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import codex_engine
import grok_engine
import grok_history
import grok_sends
import runtime

DEFAULT: str = runtime.DEFAULT_PROVIDER

# spec-092: Claude's static capability map, shared by GET /api/agent-providers (display) and the
# ask/plan/ultracode capability check at the chat-send decision point
# (runtime.capability_conflicts) — one dict, not two copies that can drift. `ask_mode` is real:
# the CLI's can_use_tool gate (spec-082 A). Codex's equivalent lives in
# codex_engine.capabilities() and deliberately has NO ask_mode key — it has no per-tool approval
# hook at all, which is exactly the gap runtime.capability_conflicts exists to fail loudly on
# instead of silently clearing the flag.
CLAUDE_CAPABILITIES: dict = {
    "chat": True, "board": True, "history": True, "search": True,
    "usage": True, "plan_mode": True, "multi_agent": True,
    "skills": True, "plugins": True, "interrupt": True,
    "ask_mode": True,
}


def _no_gate(project: Mapping[str, Any]) -> "str | None":
    """The default gate: open. A module-level function so `ProviderSpec.has_gate` can tell it
    from a real one by identity."""
    return None


def _no_ledger(ctx: Mapping[str, Any], session_id: str, prompt: str) -> None:
    """The default send ledger: nothing is recorded. Module-level for the same identity test."""
    return None


@dataclass(frozen=True)
class ProviderSpec:
    """Everything a run site needs to know that differs between providers."""

    name: str
    # Human name used in operator-facing error text ("invalid <label> model").
    label: str
    # Key in the launch `ctx` under which the async-generator engine factory lives.
    engine_key: str
    # Chat / free-chat record field that persists the provider's own resume id.
    continuity_field: str
    # Engine kwarg that receives that id.
    resume_kwarg: str
    # Key of the engine's `result` event that carries the id the engine just minted/kept.
    result_key: str
    # Built-in model used when neither the chat nor the project names one.
    fallback_model: Callable[[Mapping[str, Any]], str]
    # Cheap, synchronous "is this provider turned on" probe (no auth round trip).
    enabled: Callable[[], bool]
    capabilities: Callable[[], dict]
    # Per-project privacy gate: the refusal text, or None when the project may use this
    # provider. The default is open — Claude is the cockpit's own harness and Codex has no gate.
    gate: Callable[[Mapping[str, Any]], "str | None"] = _no_gate
    # Project/free-chat record field the gate reads ("" = no gate). Carried through the project
    # views and the settings writer from this one name.
    gate_field: str = ""
    # Witness of the prompts the cockpit really sent into a session: `(ctx, session_id, prompt)`.
    # A provider whose history lives in files its own model can write (Grok) records here so a
    # later provider crossing can tell an operator-authored row from a forged one.
    send_ledger: Callable[[Mapping[str, Any], str, str], None] = _no_ledger
    # `(ctx, cwd, session_id) -> bool`: does the provider still HAVE this session? Blocking (the run
    # sites call it in an executor). None = cannot tell. Grok answers an unknown resume id with a
    # hard error on every turn, so its run sites drop a stale id and start a new session instead.
    session_exists: "Callable[[Mapping[str, Any], str, str], bool] | None" = None

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT

    @property
    def has_gate(self) -> bool:
        """False for a provider without a privacy gate: its run sites then skip the project
        lookup entirely, so Claude and Codex runs do exactly the work they did before."""
        return self.gate is not _no_gate

    @property
    def model_field(self) -> str:
        """Project-record field holding this provider's default model."""
        return runtime.model_field_for_provider(self.name)

    @property
    def keeps_send_ledger(self) -> bool:
        return self.send_ledger is not _no_ledger

    def note_send(self, ctx: Mapping[str, Any], session_id: "str | None", prompt: "str | None") -> None:
        """Record that `prompt` was sent into `session_id` (called when the engine's `result` names
        the id). A no-op for a provider without a ledger; never raises into a run."""
        if not self.keeps_send_ledger or not session_id or not isinstance(prompt, str):
            return
        try:
            self.send_ledger(ctx, session_id, prompt)
        except Exception as exc:  # noqa: BLE001 - a ledger fault must not fail the turn
            print(f"[{self.name}] could not record a sent prompt: {exc!r}")

    def engine(self, ctx: Mapping[str, Any]) -> "Callable[..., Any] | None":
        return ctx.get(self.engine_key)

    def resume_id(self, chat: "Mapping[str, Any] | None") -> "str | None":
        """The id this provider should resume, read from a chat/free-chat record."""
        return (chat or {}).get(self.continuity_field) or None

    def result_id(self, event: Mapping[str, Any]) -> "str | None":
        return event.get(self.result_key)

    def project_model(self, project: Mapping[str, Any], ctx: "Mapping[str, Any] | None" = None) -> str:
        """The project's configured model for this provider, else the built-in default."""
        return project.get(self.model_field) or self.fallback_model(ctx or {})


_REGISTRY: "dict[str, ProviderSpec]" = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    """Add a provider. Names, engine keys and continuity fields must be unique: a shared
    `continuity_field` (the natural `session_id` for another Anthropic-protocol engine) would make
    a chat flipped back to its earlier provider resume the id the other one minted."""
    if spec.name in _REGISTRY:
        raise ValueError(f"provider {spec.name!r} is already registered")
    for other in _REGISTRY.values():
        for attr in ("engine_key", "continuity_field"):
            if getattr(other, attr) == getattr(spec, attr):
                raise ValueError(
                    f"provider {spec.name!r} reuses {attr}={getattr(spec, attr)!r} of {other.name!r}")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ProviderSpec:
    """The spec for `name`. Unknown is an error, never Claude — a run launched on the wrong
    engine because a name failed to resolve is the failure spec-092 finding 2 named."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown provider {name!r} (registered: {', '.join(_REGISTRY)})") from None


def names() -> "tuple[str, ...]":
    return tuple(_REGISTRY)


def specs() -> "tuple[ProviderSpec, ...]":
    return tuple(_REGISTRY.values())


def is_adapter(name: Any) -> bool:
    """True for a registered provider that is not the cockpit's own harness."""
    spec = _REGISTRY.get(name) if isinstance(name, str) else None
    return spec is not None and not spec.is_default


def adapters() -> "tuple[ProviderSpec, ...]":
    """Every provider except the cockpit's own harness."""
    return tuple(s for s in _REGISTRY.values() if not s.is_default)


def normalize(raw: Any) -> str:
    """A stored/posted provider value → a registered name, defaulting to Claude.

    This is the PERMISSIVE direction, for labelling and record creation where a bad value has no
    error path. Anything that launches a run must use `runtime.chat_provider` and fail closed."""
    return raw if isinstance(raw, str) and raw in _REGISTRY else DEFAULT


def known_map() -> "dict[str, bool]":
    """{name: enabled} — the map `runtime.chat_provider` judges a chat record against."""
    return {s.name: bool(s.enabled()) for s in _REGISTRY.values()}


def continuity_fields() -> "tuple[str, ...]":
    return tuple(s.continuity_field for s in _REGISTRY.values())


def adapter_model_fields() -> "tuple[str, ...]":
    return tuple(s.model_field for s in adapters())


def gate_fields() -> "tuple[str, ...]":
    """The project-record flags that opt a project in to a gated provider."""
    return tuple(s.gate_field for s in _REGISTRY.values() if s.gate_field)


def gate_refusal(name: str, project: "Mapping[str, Any] | None") -> "str | None":
    """Why `project` may NOT use provider `name` right now, or None when it may.

    Strict like `get`: an unregistered name raises instead of passing. A missing project record
    is judged as an empty one (which a gated provider refuses) — never as "no gate"."""
    return get(name).gate(project or {})


register(ProviderSpec(
    name="claude",
    label="Claude",
    engine_key="run_engine",
    continuity_field="session_id",
    resume_kwarg="resume_session_id",
    result_key="session_id",
    fallback_model=lambda ctx: ctx.get("DEFAULT_MODEL", "sonnet"),
    enabled=lambda: True,
    capabilities=lambda: CLAUDE_CAPABILITIES,
))

# `codex_engine` is looked up through the module on every call (not captured) so a test or an
# operator hot-patching `codex_engine.codex_enabled` still takes effect.
register(ProviderSpec(
    name="codex",
    label="Codex",
    engine_key="run_codex_engine",
    continuity_field="codex_thread_id",
    resume_kwarg="resume_thread_id",
    result_key="thread_id",
    fallback_model=lambda ctx: codex_engine.DEFAULT_CODEX_MODEL,
    enabled=lambda: codex_engine.codex_enabled(),
    capabilities=lambda: codex_engine.capabilities(),
))


# `grok_engine` is looked up through the module on every call (not captured), like codex_engine
# above. `fallback_model` is a constant on purpose: `_run_card` resolves the spec before its
# `try`, so it must never raise.
register(ProviderSpec(
    name="grok",
    label="Grok",
    engine_key="run_grok_engine",
    continuity_field="grok_session_id",
    resume_kwarg="resume_session_id",
    result_key="provider_session_id",
    fallback_model=lambda ctx: grok_engine.DEFAULT_GROK_MODEL,
    enabled=lambda: grok_engine.grok_enabled(),
    capabilities=lambda: grok_engine.capabilities(),
    send_ledger=lambda ctx, session_id, prompt: grok_sends.record(ctx.get("DATA"), session_id, prompt),
    # a resume is allowed only for a session the cockpit vouches for in this cwd (spec-096 P9): an id that
    # merely exists on disk may be a plant, and the engine would bind it on the first resume
    session_exists=lambda ctx, cwd, session_id: grok_history.resumable(
        session_id, cwd, grok_home=grok_engine.grok_home(ctx), data_dir=(ctx or {}).get("DATA")),
))
