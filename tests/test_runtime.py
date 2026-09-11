"""runtime.py — the single provider×account×model decision point (spec-092).

The invariants worth protecting, in order of how badly a silent version of each bites:

1. An unknown/unavailable provider must never resolve to Claude by accident — that fail-
   OPEN bug is exactly what shipped as `_chat_provider` before this module existed.
2. A legacy chat record with no `provider` key at all is a real, separate rule (always
   Claude) — not the same code path as "unknown provider", and must keep working forever.
3. Inheritance (chat → project → global) must apply per-field, and an EXPLICIT account pin
   must never quietly degrade to a different subscription — only a soft *default* may.
4. Two tabs racing a PATCH must not clobber each other — compare-and-swap on a revision.
5. A capability gap must surface as an error the caller can act on, never a silent
   downgrade of the option that was asked for.
6. A model that belongs to a different provider must be rejected at validation time, before
   it ever reaches a run.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime as rt


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CLAUDE = rt.ProviderInfo(
    provider="claude", available=True,
    models=("opus", "sonnet", "haiku"),
    capabilities={"ask_mode": True, "plan_mode": True, "multi_agent": True},
)
CODEX = rt.ProviderInfo(
    provider="codex", available=True,
    models=("gpt-5.6-sol",),
    capabilities={"plan_mode": True, "multi_agent": True},  # no ask_mode: no can_use_tool hook
)
OLLAMA_UNAVAILABLE = rt.ProviderInfo(provider="ollama", available=False, models=("qwen3.8",))

PROVIDERS = {"claude": CLAUDE, "codex": CODEX, "ollama": OLLAMA_UNAVAILABLE}


@pytest.fixture
def acct(tmp_path, monkeypatch):
    """Real accounts.py bound to an isolated tmp registry (same pattern as test_accounts.py)."""
    import accounts as mod

    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "accts"
    root.mkdir()
    home_claude = tmp_path / "home" / ".claude"
    (home_claude / "projects").mkdir(parents=True)
    (home_claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-main",
                                       "subscriptionType": "max", "expiresAt": 1900000000000}})
    )
    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ACCOUNTS_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home_claude))
    monkeypatch.delenv("CLAUDE_CREDENTIALS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return mod


def _register_working_account(mod, aid="work"):
    """Register + 'log in' an extra account so accounts.validate(aid) == (True, '')."""
    cdir, _ = mod.scaffold(aid)
    mod.register(aid, "Work", str(cdir))
    (cdir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-work",
                                       "subscriptionType": "max", "expiresAt": 1900000000000}})
    )
    return cdir


# ---------------------------------------------------------------------------
# 1 + 2. chat_provider: fail-closed vs the legacy no-key path
# ---------------------------------------------------------------------------

def test_chat_provider_fails_closed_on_unknown_value():
    chat = {"provider": "ollama"}  # registered but NOT available in this registry snapshot
    known = rt.available_providers(PROVIDERS)
    assert rt.chat_provider(chat, known_providers=known) is None


def test_chat_provider_fails_closed_on_never_registered_value():
    chat = {"provider": "gemini"}  # never existed in the registry at all
    known = rt.available_providers(PROVIDERS)
    assert rt.chat_provider(chat, known_providers=known) is None


def test_chat_provider_legacy_no_key_is_claude_even_if_claude_marked_unavailable():
    """The legacy path is a SEPARATE rule from availability — a record with no `provider`
    key predates the registry entirely and must not be re-judged against it."""
    chat = {"name": "Main", "session_id": "sess-1"}  # no "provider" key at all
    known = {"claude": False, "codex": True}  # claude marked unavailable on purpose
    assert rt.chat_provider(chat, known_providers=known) == "claude"


def test_chat_provider_none_chat_is_legacy_claude():
    assert rt.chat_provider(None, known_providers={}) == "claude"


def test_chat_provider_known_and_available_passes_through():
    chat = {"provider": "codex"}
    known = rt.available_providers(PROVIDERS)
    assert rt.chat_provider(chat, known_providers=known) == "codex"


# ---------------------------------------------------------------------------
# 3. resolve_runtime: chat → project → global inheritance
# ---------------------------------------------------------------------------

def test_resolve_runtime_unknown_chat_provider_raises_not_degrades():
    chat = {"provider": "gemini", "session_id": "s1"}
    with pytest.raises(rt.RuntimeResolutionError):
        rt.resolve_runtime(chat_id="c1", chat=chat, providers=PROVIDERS)


def test_resolve_runtime_model_inherits_project_then_global():
    # No chat model, project sets its own — must win over the global default.
    rc = rt.resolve_runtime(
        chat_id="c1",
        chat={"provider": "claude"},
        project={"model": "opus"},
        global_defaults={"models": {"claude": "haiku"}},
        providers=PROVIDERS,
    )
    assert rc.model == "opus"

    # No chat, no project → falls through to the global default.
    rc2 = rt.resolve_runtime(
        chat_id="c1",
        chat={"provider": "claude"},
        project=None,
        global_defaults={"models": {"claude": "haiku"}},
        providers=PROVIDERS,
    )
    assert rc2.model == "haiku"


def test_resolve_runtime_explicit_account_never_degrades(acct):
    """An explicit, broken chat-level account pin must raise — NOT fall back to `main`,
    unlike accounts.resolve()'s own (correct, but different-purpose) soft-degrade policy."""
    chat = {"provider": "claude", "account": "ghost-account"}  # never registered
    with pytest.raises(rt.RuntimeResolutionError):
        rt.resolve_runtime(chat_id="c1", chat=chat, providers=PROVIDERS, accounts_mod=acct)


def test_resolve_runtime_explicit_account_resolves_when_usable(acct):
    _register_working_account(acct, "work")
    chat = {"provider": "claude", "account": "work", "model": "sonnet"}
    rc = rt.resolve_runtime(chat_id="c1", chat=chat, providers=PROVIDERS, accounts_mod=acct)
    assert rc.account == "work"


def test_resolve_runtime_project_account_default_may_degrade(acct):
    """A PROJECT-level account override that is broken is a soft default — it degrades to
    the global active account instead of failing the run (accounts.py's documented rule)."""
    chat = {"provider": "claude", "model": "sonnet"}  # no chat-level account pin
    project = {"account": "ghost-account"}  # broken project default
    rc = rt.resolve_runtime(
        chat_id="c1", chat=chat, project=project, providers=PROVIDERS, accounts_mod=acct,
    )
    assert rc.account == acct.MAIN_ID  # degraded, did not raise


def test_resolve_runtime_backend_defaults_and_overrides():
    rc = rt.resolve_runtime(chat_id="c1", chat={"provider": "claude", "model": "sonnet"},
                             providers=PROVIDERS)
    assert rc.backend == "anthropic"

    rc2 = rt.resolve_runtime(
        chat_id="c1", chat={"provider": "claude", "model": "sonnet", "backend": "ollama"},
        providers=PROVIDERS,
    )
    assert rc2.backend == "ollama" and rc2.provider == "claude"


def test_resolve_runtime_carries_turn_options_unchanged():
    """ask_mode must survive resolve_runtime() untouched even for Codex — the old bug
    cleared it silently at the run-dispatch site; that decision belongs to
    capability_conflicts(), not to resolve_runtime()."""
    rc = rt.resolve_runtime(
        chat_id="c1",
        chat={"provider": "codex", "model": "gpt-5.6-sol"},
        providers=PROVIDERS,
        turn_options={"ask_mode": True, "effort": "high"},
    )
    assert rc.ask_mode is True
    assert rc.effort == "high"


def test_resolve_runtime_carries_both_session_ids_regardless_of_active_provider():
    chat = {"provider": "codex", "model": "gpt-5.6-sol",
            "session_id": "sess-claude-1", "codex_thread_id": "thread-1"}
    rc = rt.resolve_runtime(chat_id="c1", chat=chat, providers=PROVIDERS)
    assert rc.session_id == "sess-claude-1"
    assert rc.codex_thread_id == "thread-1"


# ---------------------------------------------------------------------------
# 5. capability_conflicts: error, never downgrade
# ---------------------------------------------------------------------------

def test_capability_conflict_on_ask_mode_without_hook():
    rc = rt.RunContext(chat_id="c1", provider="codex", backend="chatgpt",
                        model="gpt-5.6-sol", account="main", revision=0, ask_mode=True)
    conflicts = rt.capability_conflicts(rc, CODEX.capabilities)
    assert conflicts, "ask_mode on a hookless runtime must be reported as a conflict"
    assert "ask_mode" in conflicts[0]
    # And crucially: the flag itself is untouched — capability_conflicts must not mutate.
    assert rc.ask_mode is True


def test_capability_no_conflict_when_supported():
    rc = rt.RunContext(chat_id="c1", provider="claude", backend="anthropic",
                        model="sonnet", account="main", revision=0, ask_mode=True)
    assert rt.capability_conflicts(rc, CLAUDE.capabilities) == []


def test_capability_conflict_absent_key_is_treated_as_unsupported():
    """A capabilities map missing the key entirely must fail closed (unsupported), not be
    read as 'no opinion → assume yes'."""
    rc = rt.RunContext(chat_id="c1", provider="claude", backend="anthropic",
                        model="sonnet", account="main", revision=0, plan_mode=True)
    assert rt.capability_conflicts(rc, {}) != []


# ---------------------------------------------------------------------------
# 6. validate_runtime_change: model must belong to its provider
# ---------------------------------------------------------------------------

def test_validate_rejects_model_from_a_different_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "claude", "model": "gpt-5.6-sol"},  # a Codex model name
        providers=PROVIDERS,
        accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert "gpt-5.6-sol" in reason


def test_validate_accepts_model_that_belongs_to_its_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "claude", "model": "opus"},
        providers=PROVIDERS,
        accounts_list=[{"id": "main"}],
    )
    assert ok is True and reason == ""


def test_validate_rejects_unavailable_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "ollama"},  # registered but available=False
        providers=PROVIDERS,
        accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert "ollama" in reason


def test_validate_rejects_unknown_account():
    ok, reason = rt.validate_runtime_change(
        {"account": "nope"},
        providers=PROVIDERS,
        accounts_list=[{"id": "main"}, {"id": "work"}],
    )
    assert ok is False
    assert "nope" in reason


def test_validate_model_without_a_provider_in_scope_is_rejected():
    """Model given, but no provider anywhere (no patch value, no current chat provider)."""
    ok, reason = rt.validate_runtime_change(
        {"model": "opus"}, providers=PROVIDERS, accounts_list=[], current_provider=None,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# 4. apply_change: compare-and-swap
# ---------------------------------------------------------------------------

def test_apply_change_rejects_stale_revision():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=1)
    assert ok is False
    assert "stale" in reason.lower()
    # The chat must be untouched — a rejected CAS is not a partial write.
    assert chat["model"] == "sonnet"
    assert chat["runtime_revision"] == 3


def test_apply_change_accepts_matching_revision_and_bumps_it():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=3)
    assert ok is True and reason == ""
    assert chat["model"] == "opus"
    assert chat["runtime_revision"] == 4
    assert snapshot["model"] == "opus"


def test_apply_change_defaults_revision_to_zero_for_a_brand_new_chat():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet"}  # no runtime_revision key yet
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=0)
    assert ok is True
    assert chat["runtime_revision"] == 1


def test_apply_change_validates_the_patch_when_given_a_registry():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 0}
    ok, reason, snapshot = rt.apply_change(
        chat, {"model": "gpt-5.6-sol"}, expected_revision=0,
        providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert chat["model"] == "sonnet"  # invalid patch must not be applied even at the right revision
    assert chat["runtime_revision"] == 0


# ---------------------------------------------------------------------------
# Ollama leak guard
# ---------------------------------------------------------------------------

def test_ollama_env_never_leaks_into_claude_run():
    rc_claude = rt.RunContext(chat_id="c1", provider="claude", backend="anthropic",
                               model="sonnet", account="main", revision=0)
    assert rt.ollama_env_overlay(rc_claude, base_url="http://shim:11434") == {}

    rc_codex = rt.RunContext(chat_id="c1", provider="codex", backend="chatgpt",
                              model="gpt-5.6-sol", account="main", revision=0)
    assert rt.ollama_env_overlay(rc_codex, base_url="http://shim:11434") == {}


def test_ollama_env_overlay_applies_only_for_claude_plus_ollama_backend():
    rc = rt.RunContext(chat_id="c1", provider="claude", backend="ollama",
                        model="qwen3.8", account="main", revision=0)
    env = rt.ollama_env_overlay(rc, base_url="http://shim:11434")
    assert env["ANTHROPIC_BASE_URL"] == "http://shim:11434"
    assert env["ANTHROPIC_MODEL"] == "qwen3.8"
