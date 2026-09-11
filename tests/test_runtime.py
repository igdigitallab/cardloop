"""runtime.py -- the single provider x account x model decision point (spec-092).

The invariants worth protecting, in order of how badly a silent version of each bites:

1. RunContext must be constructible for every real integration point (chat/card/director/
   wake), not just chats -- a bare `chat_id` requirement crashes on first contact for three
   of the four.
2. chat_provider() is the ONLY place a chat record's own provider signal is judged --
   resolve_runtime() must agree with it on every input, including the legacy no-key case,
   with no second, independently re-derived availability check layered on top.
3. The Ollama env overlay is gated on `backend`, never `provider` -- and a non-ollama run
   must actively CLEAR the overlay vars, not just skip adding them.
4. Two tabs racing a PATCH must not clobber each other (CAS), a no-op patch must not mint a
   revision, a corrupt/mistyped revision is a reported error not a crash or a silent
   coercion, and the input dict is never mutated in place.
5. A change must be validated by its RESULT, not by which keys happen to appear in the
   patch -- switching provider without a compatible model must be rejected at PATCH time.
6. Every required RunContext field is validated, model included.
"""
import sys
import types
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
    backends=("", "ollama"),
    capabilities={"ask_mode": True, "plan_mode": True, "multi_agent": True},
)
CODEX = rt.ProviderInfo(
    provider="codex", available=True,
    models=("gpt-5.6-sol",),
    capabilities={"plan_mode": True, "multi_agent": True},  # no ask_mode: no can_use_tool hook
)
# A provider that IS registered but currently down -- distinct from one never registered.
GEMINI_DOWN = rt.ProviderInfo(provider="gemini", available=False, models=("gemini-3",))

PROVIDERS = {"claude": CLAUDE, "codex": CODEX, "gemini": GEMINI_DOWN}


@pytest.fixture
def acct(tmp_path, monkeypatch):
    """Real accounts.py bound to an isolated tmp registry (same pattern as test_accounts.py)."""
    import json
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
    import json
    cdir, _ = mod.scaffold(aid)
    mod.register(aid, "Work", str(cdir))
    (cdir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-work",
                                       "subscriptionType": "max", "expiresAt": 1900000000000}})
    )
    return cdir


# ---------------------------------------------------------------------------
# 1. RunContext: discriminated origin, not a bare chat_id
# ---------------------------------------------------------------------------

def test_runcontext_constructs_for_every_origin_kind():
    for kind in rt.ORIGIN_KINDS:
        rc = rt.RunContext(origin_kind=kind, origin_id=f"{kind}-1", provider="claude",
                            backend="", model="sonnet", account="main", revision=0)
        assert rc.origin_kind == kind


def test_runcontext_rejects_unknown_origin_kind():
    with pytest.raises(rt.RuntimeResolutionError):
        rt.RunContext(origin_kind="ghost-kind", origin_id="x", provider="claude",
                       backend="", model="sonnet", account="main", revision=0)


def test_runcontext_rejects_empty_origin_id():
    with pytest.raises(rt.RuntimeResolutionError):
        rt.RunContext(origin_kind="card", origin_id="", provider="claude",
                       backend="", model="sonnet", account="main", revision=0)


def test_runcontext_invalid_construction_is_the_one_exception_type():
    """A wake/card/director construction failure must raise the SAME exception type as
    resolve_runtime() -- not a bare ValueError a caller's `except RuntimeResolutionError`
    would miss."""
    with pytest.raises(rt.RuntimeResolutionError):
        rt.RunContext(origin_kind="wake", origin_id="", provider="claude",
                       backend="", model="sonnet", account="main", revision=0)


def test_card_and_director_origins_build_without_a_chat_entity():
    """The three chat-less integration points named in the review must not crash."""
    card_rc = rt.RunContext(origin_kind="card", origin_id="card-42", provider="claude",
                             backend="", model="sonnet", account="main", revision=0)
    director_rc = rt.RunContext(origin_kind="director", origin_id="director-run-7",
                                 provider="claude", backend="", model="opus",
                                 account="main", revision=0)
    assert card_rc.origin_id == "card-42"
    assert director_rc.origin_id == "director-run-7"


def test_wake_context_from_inherits_parent_runtime_unchanged():
    parent = rt.RunContext(origin_kind="chat", origin_id="chat-1", provider="codex",
                            backend="", model="gpt-5.6-sol", account="work", revision=3,
                            session_id="s1", codex_thread_id="t1", ask_mode=True)
    wake = rt.wake_context_from(parent, "run-99")
    assert wake.origin_kind == "wake"
    assert wake.origin_id == "run-99"
    # Everything else must be byte-identical -- a wake is not a re-resolution.
    assert wake.provider == parent.provider
    assert wake.backend == parent.backend
    assert wake.model == parent.model
    assert wake.account == parent.account
    assert wake.session_id == parent.session_id
    assert wake.ask_mode == parent.ask_mode


def test_wake_context_from_rejects_empty_wake_id():
    parent = rt.RunContext(origin_kind="chat", origin_id="chat-1", provider="claude",
                            backend="", model="sonnet", account="main", revision=0)
    with pytest.raises(rt.RuntimeResolutionError):
        rt.wake_context_from(parent, "")


# ---------------------------------------------------------------------------
# 6. RunContext: every required field validated, model included
# ---------------------------------------------------------------------------

def test_runcontext_rejects_empty_model():
    with pytest.raises(rt.RuntimeResolutionError):
        rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude",
                       backend="", model="", account="main", revision=0)


def test_runcontext_accepts_empty_string_backend_as_native():
    """backend="" is the NATIVE case (matches engine.py's own default), not an error."""
    rc = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude",
                        backend="", model="sonnet", account="main", revision=0)
    assert rc.backend == ""


# ---------------------------------------------------------------------------
# 2. chat_provider(): single source of truth, three states
# ---------------------------------------------------------------------------

def test_chat_provider_unavailable_vs_unknown_are_distinct_states():
    known = rt.available_providers(PROVIDERS)  # gemini: False, claude/codex: True
    unavailable = rt.chat_provider({"provider": "gemini"}, known_providers=known)
    unknown = rt.chat_provider({"provider": "vertex"}, known_providers=known)
    assert unavailable.status is rt.ProviderStatus.UNAVAILABLE
    assert unknown.status is rt.ProviderStatus.UNKNOWN
    assert unavailable.status != unknown.status


def test_chat_provider_legacy_no_key_is_claude_even_if_claude_marked_unavailable():
    """The legacy path is a SEPARATE rule from availability -- a record with no `provider`
    key predates the registry entirely and must not be re-judged against it."""
    chat = {"name": "Main", "session_id": "sess-1"}  # no "provider" key at all
    known = {"claude": False, "codex": True}  # claude marked unavailable on purpose
    result = rt.chat_provider(chat, known_providers=known)
    assert result.status is rt.ProviderStatus.OK
    assert result.value == "claude"


def test_chat_provider_none_chat_is_legacy_claude():
    result = rt.chat_provider(None, known_providers={})
    assert result.status is rt.ProviderStatus.OK and result.value == "claude"


def test_chat_provider_known_and_available_passes_through():
    known = rt.available_providers(PROVIDERS)
    result = rt.chat_provider({"provider": "codex"}, known_providers=known)
    assert result.status is rt.ProviderStatus.OK and result.value == "codex"


def test_resolve_runtime_agrees_with_chat_provider_on_the_legacy_path():
    """The exact repro from the review: a legacy no-key chat resolves to claude even when
    the registry marks claude unavailable -- resolve_runtime() must NOT re-derive its own,
    disagreeing answer via a second availability check."""
    chat = {"name": "Main"}  # no provider key
    providers_with_claude_down = {
        "claude": rt.ProviderInfo(provider="claude", available=False, models=("sonnet",)),
    }
    standalone = rt.chat_provider(chat, known_providers=rt.available_providers(providers_with_claude_down))
    assert standalone.status is rt.ProviderStatus.OK and standalone.value == "claude"

    rc = rt.resolve_runtime(
        origin_kind="chat", origin_id="c1", chat=chat,
        providers=providers_with_claude_down,
        global_defaults={"models": {"claude": "sonnet"}},
    )
    assert rc.provider == "claude"  # must NOT raise, must match chat_provider()'s own answer


def test_resolve_runtime_unavailable_chat_provider_raises_and_is_retryable_language():
    chat = {"provider": "gemini", "model": "gemini-3"}
    with pytest.raises(rt.RuntimeResolutionError, match="temporarily unavailable"):
        rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, providers=PROVIDERS)


def test_resolve_runtime_unknown_chat_provider_raises_and_is_permanent_language():
    chat = {"provider": "vertex", "session_id": "s1"}
    with pytest.raises(rt.RuntimeResolutionError, match="not a registered provider"):
        rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, providers=PROVIDERS)


# ---------------------------------------------------------------------------
# resolve_runtime: model/backend/account inheritance + turn options passthrough
# ---------------------------------------------------------------------------

def test_resolve_runtime_model_inherits_project_then_global():
    rc = rt.resolve_runtime(
        origin_kind="chat", origin_id="c1",
        chat={"provider": "claude"},
        project={"model": "opus"},
        global_defaults={"models": {"claude": "haiku"}},
        providers=PROVIDERS,
    )
    assert rc.model == "opus"

    rc2 = rt.resolve_runtime(
        origin_kind="chat", origin_id="c1",
        chat={"provider": "claude"},
        project=None,
        global_defaults={"models": {"claude": "haiku"}},
        providers=PROVIDERS,
    )
    assert rc2.model == "haiku"


def test_resolve_runtime_explicit_account_never_degrades(acct):
    chat = {"provider": "claude", "account": "ghost-account", "model": "sonnet"}
    with pytest.raises(rt.RuntimeResolutionError):
        rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, providers=PROVIDERS,
                            accounts_mod=acct)


def test_resolve_runtime_explicit_account_resolves_when_usable(acct):
    _register_working_account(acct, "work")
    chat = {"provider": "claude", "account": "work", "model": "sonnet"}
    rc = rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, providers=PROVIDERS,
                             accounts_mod=acct)
    assert rc.account == "work"


def test_resolve_runtime_project_account_default_may_degrade(acct):
    chat = {"provider": "claude", "model": "sonnet"}
    project = {"account": "ghost-account"}
    rc = rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, project=project,
                             providers=PROVIDERS, accounts_mod=acct)
    assert rc.account == acct.MAIN_ID


def test_resolve_runtime_backend_defaults_to_native_empty_string():
    rc = rt.resolve_runtime(origin_kind="chat", origin_id="c1",
                             chat={"provider": "claude", "model": "sonnet"},
                             providers=PROVIDERS)
    assert rc.backend == rt.DEFAULT_BACKEND == ""


def test_resolve_runtime_backend_override_to_ollama():
    rc = rt.resolve_runtime(
        origin_kind="chat", origin_id="c1",
        chat={"provider": "claude", "model": "sonnet", "backend": "ollama"},
        providers=PROVIDERS,
    )
    assert rc.backend == "ollama" and rc.provider == "claude"


def test_resolve_runtime_carries_turn_options_unchanged():
    rc = rt.resolve_runtime(
        origin_kind="chat", origin_id="c1",
        chat={"provider": "codex", "model": "gpt-5.6-sol"},
        providers=PROVIDERS,
        turn_options={"ask_mode": True, "effort": "high"},
    )
    assert rc.ask_mode is True
    assert rc.effort == "high"


def test_resolve_runtime_carries_both_session_ids_regardless_of_active_provider():
    chat = {"provider": "codex", "model": "gpt-5.6-sol",
            "session_id": "sess-claude-1", "codex_thread_id": "thread-1"}
    rc = rt.resolve_runtime(origin_kind="chat", origin_id="c1", chat=chat, providers=PROVIDERS)
    assert rc.session_id == "sess-claude-1"
    assert rc.codex_thread_id == "thread-1"


# ---------------------------------------------------------------------------
# capability_conflicts: error, never downgrade (unaffected by the review, still covered)
# ---------------------------------------------------------------------------

def test_capability_conflict_on_ask_mode_without_hook():
    rc = rt.RunContext(origin_kind="chat", origin_id="c1", provider="codex", backend="",
                        model="gpt-5.6-sol", account="main", revision=0, ask_mode=True)
    conflicts = rt.capability_conflicts(rc, CODEX.capabilities)
    assert conflicts
    assert "ask_mode" in conflicts[0]
    assert rc.ask_mode is True  # untouched -- no silent downgrade


def test_capability_no_conflict_when_supported():
    rc = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude", backend="",
                        model="sonnet", account="main", revision=0, ask_mode=True)
    assert rt.capability_conflicts(rc, CLAUDE.capabilities) == []


def test_capability_conflict_absent_key_is_treated_as_unsupported():
    rc = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude", backend="",
                        model="sonnet", account="main", revision=0, plan_mode=True)
    assert rt.capability_conflicts(rc, {}) != []


# ---------------------------------------------------------------------------
# 3. ollama_env_overlay: gated on backend, actively clears when not ollama
# ---------------------------------------------------------------------------

def test_ollama_overlay_ignores_provider_and_gates_on_backend_alone():
    """The review's exact concern: an operator selection may arrive as provider='ollama'.
    The overlay must key off `backend`, not `provider`, so mislabeling provider does not
    silently produce {} and route to Anthropic."""
    rc_claude_ollama = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude",
                                      backend="ollama", model="qwen3.8", account="main",
                                      revision=0)
    overlay = rt.ollama_env_overlay(rc_claude_ollama, base_url="http://shim:11434")
    assert overlay.to_set["ANTHROPIC_BASE_URL"] == "http://shim:11434"
    assert overlay.to_unset == ()

    # Even an (invalid-in-practice) provider="codex" runcontext with backend="ollama" must
    # still get the overlay -- provider plays no part in this function's gate.
    rc_codex_ollama = rt.RunContext(origin_kind="chat", origin_id="c1", provider="codex",
                                     backend="ollama", model="qwen3.8", account="main",
                                     revision=0)
    overlay2 = rt.ollama_env_overlay(rc_codex_ollama, base_url="http://shim:11434")
    assert overlay2.to_set["ANTHROPIC_BASE_URL"] == "http://shim:11434"


def test_ollama_env_never_leaks_into_claude_run():
    """A native-backend Claude run must actively CLEAR the overlay vars, not merely omit
    them -- an empty `to_set` alone does not guarantee the CLI never sees a leftover
    ANTHROPIC_BASE_URL from the service's own environment."""
    rc_claude = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude",
                               backend="", model="sonnet", account="main", revision=0)
    overlay = rt.ollama_env_overlay(rc_claude, base_url="http://shim:11434")
    assert overlay.to_set == {}
    assert set(overlay.to_unset) == set(rt.OLLAMA_ENV_VAR_NAMES)

    rc_codex = rt.RunContext(origin_kind="chat", origin_id="c1", provider="codex",
                              backend="", model="gpt-5.6-sol", account="main", revision=0)
    overlay_codex = rt.ollama_env_overlay(rc_codex, base_url="http://shim:11434")
    assert overlay_codex.to_set == {}
    assert set(overlay_codex.to_unset) == set(rt.OLLAMA_ENV_VAR_NAMES)


def test_ollama_overlay_requires_base_url():
    rc = rt.RunContext(origin_kind="chat", origin_id="c1", provider="claude", backend="ollama",
                        model="qwen3.8", account="main", revision=0)
    with pytest.raises(rt.RuntimeResolutionError):
        rt.ollama_env_overlay(rc, base_url=None)


def test_validate_rejects_ollama_as_a_provider_value():
    ok, reason = rt.validate_runtime_change(
        {"provider": "ollama"}, providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert "ollama" in reason and "backend" in reason


# ---------------------------------------------------------------------------
# 5. validate_runtime_change: validates the RESULTING state
# ---------------------------------------------------------------------------

def test_validate_rejects_provider_switch_that_leaves_an_incompatible_model_behind():
    """The review's exact repro: {"provider": "codex"} alone used to pass validation because
    only `"model" in patch` was checked. The chat's CURRENT model belongs to claude, not
    codex -- the resulting state is unrunnable and must be rejected here."""
    current = {"provider": "claude", "model": "claude-sonnet-5"}
    ok, reason = rt.validate_runtime_change(
        {"provider": "codex"}, providers=PROVIDERS, accounts_list=[{"id": "main"}],
        current=current,
    )
    assert ok is False
    # Specifically the RESULTING-model mismatch message, not the separate "no model at all"
    # guard -- proves the inherited current model was actually consulted, not just presence
    # of a "model" key in the patch.
    assert "does not belong to provider" in reason
    assert "claude-sonnet-5" in reason


def test_validate_rejects_provider_switch_with_no_current_state_at_all():
    """Same defect, worst case: no `current` supplied either, so there is no way to know
    what model would result -- must still be rejected, not silently accepted."""
    ok, reason = rt.validate_runtime_change(
        {"provider": "codex"}, providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False


def test_validate_accepts_provider_switch_with_a_compatible_model_in_the_same_patch():
    current = {"provider": "claude", "model": "claude-sonnet-5"}
    ok, reason = rt.validate_runtime_change(
        {"provider": "codex", "model": "gpt-5.6-sol"},
        providers=PROVIDERS, accounts_list=[{"id": "main"}], current=current,
    )
    assert ok is True and reason == ""


def test_validate_rejects_model_from_a_different_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "claude", "model": "gpt-5.6-sol"},
        providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert "gpt-5.6-sol" in reason


def test_validate_accepts_model_that_belongs_to_its_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "claude", "model": "opus"},
        providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is True and reason == ""


def test_validate_rejects_unavailable_provider():
    ok, reason = rt.validate_runtime_change(
        {"provider": "gemini"}, providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False and "gemini" in reason


def test_validate_rejects_unknown_account():
    ok, reason = rt.validate_runtime_change(
        {"account": "nope"}, providers=PROVIDERS,
        accounts_list=[{"id": "main"}, {"id": "work"}],
    )
    assert ok is False and "nope" in reason


def test_validate_model_without_a_provider_in_scope_is_rejected():
    ok, reason = rt.validate_runtime_change(
        {"model": "opus"}, providers=PROVIDERS, accounts_list=[], current=None,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# 4. apply_change: CAS -- no mutation, no spurious bump, coerced/validated revisions
# ---------------------------------------------------------------------------

def test_apply_change_rejects_stale_revision():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=1)
    assert ok is False
    assert "stale" in reason.lower()
    assert chat == {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}


def test_apply_change_never_mutates_the_input_dict():
    """Passing an actually-immutable mapping proves apply_change never attempts a write to
    `chat` -- a MappingProxyType would raise TypeError on any assignment."""
    chat = types.MappingProxyType({
        "id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3,
    })
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=3)
    assert ok is True
    assert snapshot["model"] == "opus"
    assert snapshot["runtime_revision"] == 4
    assert chat["model"] == "sonnet"  # original untouched
    assert chat["runtime_revision"] == 3


def test_apply_change_expected_revision_string_matches_int():
    """A JSON body's revision arrives as whatever the client serialised -- a `"3"` string
    must compare equal to the stored `3` int, not false-reject a legitimate CAS."""
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision="3")
    assert ok is True, reason
    assert snapshot["runtime_revision"] == 4


def test_apply_change_no_op_patch_does_not_bump_revision():
    """A patch that changes nothing (same value, or only unrecognised keys) must not mint a
    new revision -- doing so would false-reject the NEXT legitimate concurrent writer."""
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 3}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "sonnet"}, expected_revision=3)
    assert ok is True
    assert snapshot["runtime_revision"] == 3

    ok2, reason2, snapshot2 = rt.apply_change(chat, {"unrelated_field": "x"}, expected_revision=3)
    assert ok2 is True
    assert snapshot2["runtime_revision"] == 3

    ok3, reason3, snapshot3 = rt.apply_change(chat, {}, expected_revision=3)
    assert ok3 is True
    assert snapshot3["runtime_revision"] == 3


def test_apply_change_rejects_corrupt_stored_revision_without_raising():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": -1}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=-1)
    assert ok is False
    assert "runtime_revision" in reason or "revision" in reason.lower()
    # And crucially: no exception escaped, and nothing was mutated or coerced forward.
    assert chat["runtime_revision"] == -1


def test_apply_change_rejects_corrupt_expected_revision_without_raising():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 0}
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision="not-a-number")
    assert ok is False
    assert chat["runtime_revision"] == 0


def test_apply_change_defaults_revision_to_zero_for_a_brand_new_chat():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet"}  # no runtime_revision key yet
    ok, reason, snapshot = rt.apply_change(chat, {"model": "opus"}, expected_revision=0)
    assert ok is True
    assert snapshot["runtime_revision"] == 1
    assert "runtime_revision" not in chat  # original untouched


def test_apply_change_validates_the_resulting_state_when_given_a_registry():
    chat = {"id": "c1", "provider": "claude", "model": "sonnet", "runtime_revision": 0}
    ok, reason, snapshot = rt.apply_change(
        chat, {"model": "gpt-5.6-sol"}, expected_revision=0,
        providers=PROVIDERS, accounts_list=[{"id": "main"}],
    )
    assert ok is False
    assert chat["model"] == "sonnet"
    assert chat["runtime_revision"] == 0


def test_validate_accepts_a_model_only_patch_on_a_pre_provider_field_chat():
    """A chat created before the `provider` field existed must still be able to change model.

    Those records have NO `provider` key, and this module's legacy rule says such a record is
    Claude. `validate_runtime_change` used to merge `current` and `patch` as raw dicts, which
    does not know that rule, so a model-only PATCH on the operator's oldest chats was rejected
    with "cannot validate a model without a resolvable provider" — they could not change model
    at all. Found in review before any frontend could hit it.
    """
    providers = {
        "claude": rt.ProviderInfo(provider="claude", available=True, models=["opus", "sonnet"]),
        "codex": rt.ProviderInfo(provider="codex", available=True, models=["gpt-5.6-sol"]),
    }
    legacy_chat = {"id": "c1", "model": "opus"}  # no provider key at all

    ok, reason = rt.validate_runtime_change(
        {"model": "sonnet"}, providers=providers, accounts_list=[{"id": "main"}],
        current=legacy_chat,
    )
    assert ok, reason

    # The legacy default must be a real resolution, not a bypass: a model belonging to
    # ANOTHER provider is still rejected, judged against Claude.
    ok, reason = rt.validate_runtime_change(
        {"model": "gpt-5.6-sol"}, providers=providers, accounts_list=[{"id": "main"}],
        current=legacy_chat,
    )
    assert not ok
    assert "claude" in reason


def test_validate_still_refuses_an_explicitly_null_provider():
    """Key absent (legacy) and key present-but-null are different states, on purpose.

    The absent key is a documented pre-field record; an explicit null is a malformed one and
    must not silently inherit the legacy Claude default.
    """
    providers = {"claude": rt.ProviderInfo(provider="claude", available=True, models=["opus"])}
    ok, reason = rt.validate_runtime_change(
        {"model": "opus"}, providers=providers, accounts_list=[{"id": "main"}],
        current={"id": "c1", "provider": None, "model": "opus"},
    )
    assert not ok
    assert "resolvable provider" in reason
