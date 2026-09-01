"""spec-078 Phase 2 — per-project skills/plugins ("each project pulls only its own brains").

Covers: agents_config → run_engine kwarg mapping, plugin-id → install-path resolution,
and that the live-client fingerprint changes when a project's skill/plugin set changes
(so a stale subprocess is evicted and the new skill set actually loads).
"""
from __future__ import annotations

import engine
from claude_agent_sdk import ClaudeAgentOptions


# ── agents_config → kwargs mapping ────────────────────────────────────────────

def test_build_agents_kwargs_maps_skills_and_plugins():
    kw = engine._build_agents_kwargs({"skills": ["coolify-deploy"], "plugins": ["marketing-skills"]})
    assert kw["project_skills"] == ["coolify-deploy"]
    assert kw["project_plugins"] == ["marketing-skills"]


def test_build_agents_kwargs_skills_all():
    kw = engine._build_agents_kwargs({"skills": "all"})
    assert kw["project_skills"] == "all"
    assert "project_plugins" not in kw


def test_build_agents_kwargs_absent_keys_untouched():
    kw = engine._build_agents_kwargs({"executor_model": "sonnet"})
    assert "project_skills" not in kw and "project_plugins" not in kw


# ── plugin id → install path ──────────────────────────────────────────────────

def test_plugin_install_path_bogus_is_none():
    assert engine._plugin_install_path("definitely-not-a-plugin") is None


# ── fingerprint sensitivity ───────────────────────────────────────────────────

def _opts(skills=None, plugins=None):
    return ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        cwd="/tmp/x",
        permission_mode="bypassPermissions",
        setting_sources=["user", "project", "local"],
        skills=skills,
        plugins=plugins or [],
    )


def test_fingerprint_changes_with_skills():
    fp_none = engine._compute_fingerprint(_opts(skills=None))
    fp_lean = engine._compute_fingerprint(_opts(skills=["coolify-deploy"]))
    fp_all = engine._compute_fingerprint(_opts(skills="all"))
    assert fp_none != fp_lean != fp_all and fp_none != fp_all


def test_fingerprint_changes_with_plugins():
    fp_no = engine._compute_fingerprint(_opts(plugins=[]))
    fp_mkt = engine._compute_fingerprint(_opts(plugins=[{"type": "local", "path": "/x/marketing"}]))
    assert fp_no != fp_mkt


def test_fingerprint_stable_when_skills_identical():
    assert engine._compute_fingerprint(_opts(skills=["a", "b"])) == \
           engine._compute_fingerprint(_opts(skills=["a", "b"]))


# ── additive per-project filter ("+name" keeps the global core) ─────────────────────────────────

CORE = ["tdd", "docker-scan", "research"]


def test_plain_list_still_replaces_the_default():
    """Unchanged spec-078 behaviour: an explicit list is the whole answer."""
    assert engine._merge_project_skills(["meta-ads"], CORE) == ["meta-ads"]


def test_plus_entries_extend_the_default():
    out = engine._merge_project_skills(["+hyperframes", "+media-use"], CORE)
    assert out == ["tdd", "docker-scan", "research", "hyperframes", "media-use"]


def test_plus_and_plain_entries_mix_without_duplicates():
    out = engine._merge_project_skills(["+docker-scan", "+meta-ads", "song"], CORE)
    assert out == ["tdd", "docker-scan", "research", "meta-ads", "song"]


def test_plus_degrades_to_the_default_when_there_is_no_allowlist():
    """Default None = every skill loads; narrowing to the project's own few would be a downgrade."""
    assert engine._merge_project_skills(["+hyperframes"], None) is None


def test_all_and_none_are_passed_through():
    assert engine._merge_project_skills("all", CORE) == "all"
    assert engine._merge_project_skills(None, CORE) == CORE


def test_a_bare_plus_is_ignored():
    assert engine._merge_project_skills(["+", "+song"], CORE) == [*CORE, "song"]
