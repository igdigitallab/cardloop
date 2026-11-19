"""spec-098 Forget: delete a session (or a reply and everything after it) everywhere the cockpit
controls.

One purge engine, per-provider adapters. This module owns the shared vocabulary (the adapter
protocol, the artifact and content records, the registry and the capability flags). The plan /
execute pipeline, the cockpit-store purges, the pending journal and the residue scan live here too
(added on top of this skeleton). Each provider module owns its own files and registers an adapter:
`forget_claude.py`, `forget_codex.py`, `forget_grok.py`.

Rollback: `FORGET_ENABLED=0` turns every capability off and the endpoints answer 404; turn marks,
run ids and the lineage log are inert without it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable


def forget_enabled() -> bool:
    """On unless the operator sets FORGET_ENABLED=0 (or false/no/off)."""
    return os.environ.get("FORGET_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


@dataclass
class SessionArtifacts:
    """What an adapter found for one session. `extra` is adapter-private and JSON-serialisable:
    it is written into the pending record, so it must never hold conversation text."""

    provider: str
    session_id: str
    cwd: str
    title: str = ""
    paths: list[Path] = field(default_factory=list)
    first_ts: "float | None" = None
    last_ts: "float | None" = None
    extra: dict = field(default_factory=dict)


# kinds of ContentItem, in the order they usually appear in a turn
CONTENT_KINDS = ("prompt", "assistant", "tool_use", "tool_result", "summary")


@dataclass
class ContentItem:
    """One piece of conversation text read from a provider store, for the side-effect scan, the
    old-run attribution (prompts) and the residue scan (distinctive strings)."""

    kind: str
    text: str
    ts: "float | None" = None
    tool: "str | None" = None
    tool_input: "dict | None" = None
    uuid: "str | None" = None


@runtime_checkable
class Adapter(Protocol):
    name: str
    can_delete_session: bool
    can_forget_turn: bool

    def locate(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
               session_id: str) -> "SessionArtifacts | None": ...

    def busy_reason(self, ctx: Mapping[str, Any], project: Mapping[str, Any],
                    session_id: str) -> "str | None": ...

    def read_content(self, art: SessionArtifacts) -> Iterator[ContentItem]: ...

    def delete(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> dict: ...

    def verify_gone(self, ctx: Mapping[str, Any], art: SessionArtifacts) -> list[str]: ...

    def residue_roots(self, ctx: Mapping[str, Any]) -> list[Path]: ...


_ADAPTERS: "dict[str, Adapter]" = {}


def register_adapter(adapter: Adapter) -> Adapter:
    if adapter.name in _ADAPTERS:
        raise ValueError(f"forget adapter {adapter.name!r} is already registered")
    _ADAPTERS[adapter.name] = adapter
    return adapter


def adapter(name: str) -> "Adapter | None":
    return _ADAPTERS.get(name)


def capabilities(name: str) -> dict:
    """The forget capability flags of one provider, merged into its `capabilities()` row."""
    a = _ADAPTERS.get(name)
    on = forget_enabled() and a is not None
    return {
        "delete_session": bool(on and a.can_delete_session),
        "forget_turn": bool(on and a.can_forget_turn),
    }


# Vendor-side limits shown in the dialog (P0 research, docs/internal/specs/p0/retention.md).
VENDOR_LIMITS: "dict[str, str]" = {
    "claude": ("Anthropic keeps copies for 30 days (or up to 5 years if 'Help improve Claude' is on); "
               "chats flagged by safety systems can be kept up to 2 years."),
    "codex": ("OpenAI does not publish how long it keeps Codex turns made with a ChatGPT login; "
              "deleting here does not delete anything on OpenAI's side."),
    "grok": ("xAI does not publish how long it keeps prompts and code; with the coding data-retention "
             "opt-out on, xAI says it discards them, but deleting here does not remove anything on "
             "xAI's side."),
    "ollama": ("Turns that ran on the local Ollama backend never left your network; its server keeps "
               "no prompt history."),
}

ALWAYS_LIMITS: "tuple[str, ...]" = (
    "Offsite backups made before today keep the cockpit's logs for up to 6 months.",
    "Notifications already delivered to your devices are not recalled.",
    "What the agent did (files, commits, sent messages) is not undone.",
)


def _load_adapters() -> None:
    """Import the provider adapters so they register themselves. One line per provider."""
    for mod in ("forget_claude", "forget_codex", "forget_grok"):
        try:
            __import__(mod)
        except ImportError:
            pass
