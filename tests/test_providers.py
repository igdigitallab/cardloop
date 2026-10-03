"""spec-095 D6: the provider table itself (providers.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import codex_engine
import providers
import runtime


def test_registry_holds_claude_first_then_codex():
    assert providers.names() == ("claude", "codex")
    assert providers.DEFAULT == runtime.DEFAULT_PROVIDER == "claude"
    assert providers.get("claude").is_default and not providers.get("codex").is_default
    assert [s.name for s in providers.adapters()] == ["codex"]


def test_unknown_provider_is_an_error_never_claude():
    with pytest.raises(KeyError, match="unknown provider 'vertex'"):
        providers.get("vertex")


@pytest.mark.parametrize("raw,expected", [
    ("codex", "codex"), ("claude", "claude"), ("vertex", "claude"), ("", "claude"),
    (None, "claude"), (7, "claude"), ("CODEX", "claude"),
])
def test_normalize_is_permissive_and_exact_case(raw, expected):
    assert providers.normalize(raw) == expected


def test_per_provider_wiring_facts():
    c, x = providers.get("claude"), providers.get("codex")
    assert (c.engine_key, c.continuity_field, c.resume_kwarg, c.result_key) == (
        "run_engine", "session_id", "resume_session_id", "session_id")
    assert (x.engine_key, x.continuity_field, x.resume_kwarg, x.result_key) == (
        "run_codex_engine", "codex_thread_id", "resume_thread_id", "thread_id")
    assert (c.model_field, x.model_field) == ("model", "codex_model")
    assert providers.continuity_fields() == ("session_id", "codex_thread_id")
    assert providers.adapter_model_fields() == ("codex_model",)


def test_engine_lookup_reads_the_ctx_key():
    def fake():
        pass
    assert providers.get("codex").engine({"run_codex_engine": fake}) is fake
    assert providers.get("codex").engine({"run_engine": fake}) is None
    assert providers.get("claude").engine({"run_engine": fake}) is fake


def test_resume_and_result_ids():
    c, x = providers.get("claude"), providers.get("codex")
    chat = {"session_id": "S", "codex_thread_id": "T"}
    assert c.resume_id(chat) == "S" and x.resume_id(chat) == "T"
    assert c.resume_id({"session_id": ""}) is None and x.resume_id(None) is None
    event = {"session_id": "S2", "thread_id": "T2"}
    assert c.result_id(event) == "S2" and x.result_id(event) == "T2"
    assert x.result_id({"session_id": "S2"}) is None


def test_project_model_prefers_the_projects_field_then_the_builtin_default():
    c, x = providers.get("claude"), providers.get("codex")
    assert x.project_model({"codex_model": "gpt-p"}) == "gpt-p"
    assert x.project_model({}) == codex_engine.DEFAULT_CODEX_MODEL
    assert c.project_model({"model": "opus"}, {"DEFAULT_MODEL": "haiku"}) == "opus"
    assert c.project_model({}, {"DEFAULT_MODEL": "haiku"}) == "haiku"
    assert c.project_model({}) == "sonnet"


@pytest.mark.parametrize("on", [True, False])
def test_known_map_follows_codex_enabled_live(monkeypatch, on):
    monkeypatch.setattr(codex_engine, "codex_enabled", lambda: on)
    assert providers.known_map() == {"claude": True, "codex": on}


def test_claude_capabilities_is_the_one_shared_dict():
    assert providers.get("claude").capabilities() is providers.CLAUDE_CAPABILITIES
    assert providers.CLAUDE_CAPABILITIES["ask_mode"] is True
    assert "ask_mode" not in providers.get("codex").capabilities()


def _spec(**over):
    base = dict(name="x", label="X", engine_key="run_x_engine", continuity_field="x_id",
                resume_kwarg="resume_id", result_key="provider_session_id",
                fallback_model=lambda ctx: "m", enabled=lambda: True, capabilities=lambda: {})
    base.update(over)
    return providers.ProviderSpec(**base)


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(providers, "_REGISTRY", dict(providers._REGISTRY))


@pytest.mark.parametrize("over,message", [
    (dict(name="codex"), "already registered"),
    (dict(engine_key="run_engine"), "reuses engine_key"),
    (dict(continuity_field="session_id"), "reuses continuity_field"),
])
def test_register_refuses_colliding_providers(clean_registry, over, message):
    with pytest.raises(ValueError, match=message):
        providers.register(_spec(**over))


def test_register_accepts_a_distinct_provider_and_it_flows_through_the_table(clean_registry):
    providers.register(_spec())
    assert providers.names() == ("claude", "codex", "x")
    assert providers.continuity_fields() == ("session_id", "codex_thread_id", "x_id")
    assert providers.adapter_model_fields() == ("codex_model", "x_model")
    assert providers.normalize("x") == "x" and providers.is_adapter("x")
