"""
Tests for spec-091 Phase 1 — declarative, cockpit-editable sub-agent roles (`roles.py`).

Covers CHECKLIST.md sections A (parser), B (registry/precedence/compilation) and H
(builtin role set). Every test name embeds the checklist line it proves.

Nothing here touches the real ~/.claude-ops/roles — CARDLOOP_ROLES_DIR is monkeypatched
to a tmp_path per test (roles.global_dir() reads the env var live, so no module reload
is needed), and the project tier uses tmp_path/"proj" as the cwd.
"""
import os
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import roles as R
import engine


# ─────────────────────────── fixtures ───────────────────────────

@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Isolate global_dir() and give each test its own project cwd. Returns (cwd, global_dir)."""
    global_d = tmp_path / "global-roles"
    proj_cwd = tmp_path / "proj"
    proj_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(global_d))
    return str(proj_cwd), str(global_d)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


MINIMAL = """---
name: {name}
description: Use this when testing {name}.
---
Body for {name}.
"""


# ══════════════════════════ A. Role file format and parser ══════════════════════════

def test_a1_roles_module_imports_nothing_from_engine_or_webapp():
    src = (ROOT / "roles.py").read_text(encoding="utf-8")
    for banned in ("import engine", "from engine", "import webapp", "from webapp"):
        assert banned not in src, f"roles.py must not import engine/webapp ({banned!r} found)"


def test_a2_no_new_third_party_dependency():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pyyaml" not in req
    assert "yaml" not in req


@pytest.mark.parametrize("value_line,expected", [
    ("model: opus", "opus"),
    ('description: "a: b"', "a: b"),
])
def test_a3_scalar_and_quoted_scalar_forms(value_line, expected):
    key = value_line.split(":", 1)[0]
    text = f"---\nname: rr\ndescription: Use this when x.\n{value_line}\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    field = {"model": role.model, "description": role.description}[key]
    assert field == expected


def test_a3_bool_form():
    text = "---\nname: rr\ndescription: Use this when x.\nenabled: false\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.enabled is False


def test_a3_int_form():
    text = "---\nname: rr\ndescription: Use this when x.\nmaxTurns: 25\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.max_turns == 25
    assert isinstance(role.max_turns, int)


def test_a3_inline_list_form():
    text = "---\nname: rr\ndescription: Use this when x.\ntools: [Read, Bash]\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.tools == ["Read", "Bash"]


def test_a3_empty_inline_list_form():
    text = "---\nname: rr\ndescription: Use this when x.\nmcpServers: []\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.mcp_servers == []


def test_a3_block_list_form():
    text = "---\nname: rr\ndescription: Use this when x.\ntools:\n  - Read\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.tools == ["Read"]


def test_a3_folded_scalar_form():
    text = "---\nname: rr\ndescription: >\n  two lines\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.description == "two lines"


def test_a4_missing_description_fails_naming_the_field():
    text = "---\nname: rr\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert role is None
    assert "description" in err


def test_a4_empty_description_fails_naming_the_field():
    text = "---\nname: rr\ndescription:\n---\nBody.\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert role is None
    assert "description" in err


def test_a5_empty_body_fails_naming_the_body():
    text = "---\nname: rr\ndescription: Use this when x.\n---\n"
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert role is None
    assert "body" in err.lower() or "prompt" in err.lower()


@pytest.mark.parametrize("bad_name", ["Foo", "a", "x" * 40, "../x", "with/slash", "with space"])
def test_a6_invalid_name_rejected_by_parse_role(bad_name):
    text = MINIMAL.format(name="valid-name")
    role, err = R.parse_role(text, name=bad_name, scope="project", path="/x")
    assert role is None
    assert err is not None


@pytest.mark.parametrize("bad_name", ["Foo", "a", "x" * 40, "../x"])
def test_a6_invalid_name_rejected_via_write_path(isolated_dirs, bad_name):
    cwd, _ = isolated_dirs
    content = MINIMAL.format(name="valid-name")
    with pytest.raises(ValueError):
        R.write_role(cwd, bad_name, "project", content)
    # And no file was created anywhere under the project roles dir.
    proj_roles_dir = Path(cwd) / R.PROJECT_SUBDIR
    if proj_roles_dir.is_dir():
        assert list(proj_roles_dir.iterdir()) == []


def test_a7_unknown_frontmatter_keys_round_trip_via_extras():
    text = (
        "---\nname: rr\ndescription: Use this when x.\ncustomField: hello\n"
        "anotherList: [a, b]\n---\nBody.\n"
    )
    role, err = R.parse_role(text, name="rr", scope="project", path="/x")
    assert err is None, err
    assert role.extras == {"customField": "hello", "anotherList": ["a", "b"]}
    # extras never reach the SDK.
    compiled = R.compile_agents({"rr": role})["rr"]
    assert not hasattr(compiled, "customField")


def test_a7_unknown_frontmatter_keys_round_trip_write_read(isolated_dirs):
    cwd, _ = isolated_dirs
    content = (
        "---\nname: rr\ndescription: Use this when x.\ncustomField: hello\n---\nBody.\n"
    )
    R.write_role(cwd, "rr", "project", content)
    # write_role stores the raw content byte-for-byte; reading it back parses the same extras.
    on_disk = (Path(cwd) / R.PROJECT_SUBDIR / "rr.md").read_text(encoding="utf-8")
    assert on_disk == content
    role2, err2 = R.parse_role(on_disk, name="rr", scope="project", path="/x")
    assert err2 is None
    assert role2.extras == {"customField": "hello"}


def test_a8_name_disagreeing_with_filename_filename_wins_with_warning():
    text = "---\nname: other-name\ndescription: Use this when x.\n---\nBody.\n"
    role, err = R.parse_role(text, name="filename-name", scope="project", path="/x")
    assert err is None, err
    assert role.name == "filename-name"
    assert any("other-name" in w and "filename" in w for w in role.warnings)


def test_a9_unparseable_file_never_raises_out_of_list_roles(isolated_dirs):
    cwd, _ = isolated_dirs
    _write(Path(cwd) / R.PROJECT_SUBDIR / "broken.md", "not frontmatter at all\n")
    # Must not raise.
    all_roles = R.list_roles_report(cwd)[0]
    assert isinstance(all_roles, list)
    merged = R.load_roles(cwd)
    assert isinstance(merged, dict)


def test_a9_unparseable_file_surfaces_in_list_roles_report(isolated_dirs):
    cwd, _ = isolated_dirs
    _write(Path(cwd) / R.PROJECT_SUBDIR / "broken.md", "not frontmatter at all\n")
    all_roles, errors = R.list_roles_report(cwd)
    assert any(e["name"] == "broken" and e["scope"] == "project" for e in errors)
    assert not any(r.name == "broken" for r in all_roles)


def test_a10_parse_role_is_pure_no_filesystem_access():
    text = MINIMAL.format(name="pure-test")
    with mock.patch("builtins.open", side_effect=AssertionError("parse_role touched the filesystem")):
        role, err = R.parse_role(text, name="pure-test", scope="project", path="/does/not/matter")
    assert err is None
    assert role is not None


# ══════════════════════════ B. Registry, precedence, compilation ══════════════════════════

def test_b1_three_tiers_resolve(isolated_dirs):
    cwd, global_d = isolated_dirs
    _write(Path(global_d) / "only-global.md", MINIMAL.format(name="only-global"))
    _write(Path(cwd) / R.PROJECT_SUBDIR / "only-project.md", MINIMAL.format(name="only-project"))

    merged = R.load_roles(cwd)
    assert "executor" in merged  # builtin tier
    assert "only-global" in merged  # global tier
    assert "only-project" in merged  # project tier


def test_b2_project_file_fully_replaces_same_named_role_whole_file(isolated_dirs):
    cwd, _ = isolated_dirs
    override = (
        "---\nname: executor\ndescription: Use this when overridden.\n"
        "tools: [Read]\nmodel: haiku\n---\nCompletely different body.\n"
    )
    R.write_role(cwd, "executor", "project", override)
    merged = R.load_roles(cwd)
    r = merged["executor"]
    assert r.scope == "project"
    assert r.model == "haiku"
    assert r.tools == ["Read"]
    assert r.prompt == "Completely different body."
    # No field merge with the builtin executor's maxTurns/disallowedTools/etc.
    assert r.max_turns is None
    assert r.disallowed_tools is None


def test_b3_disabled_excluded_from_load_roles_but_present_in_list_roles(isolated_dirs):
    cwd, _ = isolated_dirs
    content = (
        "---\nname: disabled-role\ndescription: Use this when disabled.\n"
        "enabled: false\n---\nBody.\n"
    )
    R.write_role(cwd, "disabled-role", "project", content)
    assert "disabled-role" not in R.load_roles(cwd)
    all_roles = R.list_roles_report(cwd)[0]
    found = [r for r in all_roles if r.name == "disabled-role"]
    assert len(found) == 1
    assert found[0].scope == "project"
    assert found[0].enabled is False


def test_b5_compile_agents_maps_every_field_and_defaults_permission_mode(isolated_dirs):
    cwd, _ = isolated_dirs
    content = (
        "---\nname: full\ndescription: Use this when full.\n"
        "tools: [Read, Bash]\ndisallowedTools: [Write]\nmodel: opus\neffort: high\n"
        "maxTurns: 12\nskills: [code-review]\nmcpServers: [srv1]\nmemory: project\n"
        "---\nFull body.\n"
    )
    role = R.write_role(cwd, "full", "project", content)
    compiled = R.compile_agents({"full": role})["full"]
    assert compiled.description == role.description
    assert compiled.prompt == role.prompt
    assert compiled.tools == ["Read", "Bash"]
    assert compiled.disallowedTools == ["Write"]
    assert compiled.model == "opus"
    assert compiled.effort == "high"
    assert compiled.maxTurns == 12
    assert compiled.skills == ["code-review"]
    assert compiled.mcpServers == ["srv1"]
    assert compiled.memory == "project"
    # permissionMode was omitted in the file -> defaults to bypassPermissions.
    assert compiled.permissionMode == "bypassPermissions"


def test_b5_compile_agents_honours_explicit_permission_mode(isolated_dirs):
    cwd, _ = isolated_dirs
    content = (
        "---\nname: rr\ndescription: Use this when x.\npermissionMode: plan\n---\nBody.\n"
    )
    role = R.write_role(cwd, "rr", "project", content)
    compiled = R.compile_agents({"rr": role})["rr"]
    assert compiled.permissionMode == "plan"


def test_b6_registry_fingerprint_stable_when_nothing_changes(isolated_dirs):
    cwd, _ = isolated_dirs
    fp1 = R.registry_fingerprint(R.load_roles(cwd))
    fp2 = R.registry_fingerprint(R.load_roles(cwd))
    assert fp1 == fp2


@pytest.mark.parametrize("field,mutate", [
    ("prompt", lambda c: c.replace("Body.", "Different body.")),
    ("model", lambda c: c.replace("---\nBody.", "model: haiku\n---\nBody.")),
    ("tools", lambda c: c.replace("---\nBody.", "tools: [Read]\n---\nBody.")),
    ("enabled", lambda c: c.replace("---\nBody.", "enabled: false\n---\nBody.")),
])
def test_b6_registry_fingerprint_changes_on_field_edit(isolated_dirs, field, mutate):
    cwd, _ = isolated_dirs
    base = "---\nname: fp-role\ndescription: Use this when fp.\n---\nBody.\n"
    R.write_role(cwd, "fp-role", "project", base)
    fp_before = R.registry_fingerprint(R.load_roles(cwd))

    # I1k: write_role now refuses to clobber an existing file unless overwrite=True is passed.
    R.write_role(cwd, "fp-role", "project", mutate(base), overwrite=True)
    fp_after = R.registry_fingerprint(R.load_roles(cwd))
    assert fp_before != fp_after, f"fingerprint did not change when {field} changed"


def test_b7_missing_directories_are_not_an_error_and_not_created(tmp_path, monkeypatch):
    missing_global = tmp_path / "does-not-exist-global"
    missing_cwd = tmp_path / "does-not-exist-proj"
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(missing_global))

    # Neither directory exists on disk before the call.
    assert not missing_global.exists()
    assert not (missing_cwd / R.PROJECT_SUBDIR).exists()

    roles_list, errors = R.list_roles_report(str(missing_cwd))
    assert errors == []
    assert any(r.name == "executor" for r in roles_list)  # builtin tier still works

    # Still not created after the read.
    assert not missing_global.exists()
    assert not (missing_cwd / R.PROJECT_SUBDIR).exists()


def test_b7_global_dir_alone_not_auto_created_on_read(tmp_path, monkeypatch):
    missing_global = tmp_path / "ghost-global-dir"
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(missing_global))
    R.load_roles(None)
    assert not missing_global.exists()


def test_b8_global_dir_honours_cardloop_roles_dir_env(tmp_path, monkeypatch):
    custom = tmp_path / "custom-roles-dir"
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(custom))
    assert R.global_dir() == str(custom)


def test_b8_global_dir_falls_back_to_home_claude_ops_roles(monkeypatch):
    monkeypatch.delenv("CARDLOOP_ROLES_DIR", raising=False)
    assert R.global_dir() == os.path.expanduser("~/.claude-ops/roles")


def test_b8_no_personal_path_hardcoded_in_module_source():
    src = (ROOT / "roles.py").read_text(encoding="utf-8")
    home = os.path.expanduser("~")
    # The real, resolved home directory string must never appear literally in the source
    # (only the portable os.path.expanduser("~/...") form is allowed).
    assert home not in src


# ══════════════════════════ H. Builtin role set ══════════════════════════

_ELEVEN = [
    "executor", "researcher", "skeptic", "quick",
    "reviewer-logic", "reviewer-security", "reviewer-quality",
    "architect", "debugger", "test-writer", "docs-writer",
]

_NEW_SEVEN = [
    "reviewer-logic", "reviewer-security", "reviewer-quality",
    "architect", "debugger", "test-writer", "docs-writer",
]

_PROGRESS_ON_DISK_PARAGRAPH = (
    "PROGRESS ON DISK — every ~15 tool calls, append what you have done and learned so far "
    "to /tmp/cardloop-scratch/<task-slug>.md (mkdir -p first). If you hit your turn limit, that "
    "file IS your deliverable; your final message must name its path."
)
_FINAL_ANSWER_RULE = (
    "FINAL ANSWER = the path of your report file on disk + at most 5 lines of summary. Never "
    "paste the report into the final answer — the orchestrator opens the file when it needs "
    "detail."
)


@pytest.fixture()
def builtin_roles():
    roles_list, errors = R.list_roles_report(None)
    assert errors == [], f"builtin roles failed to parse: {errors}"
    return {r.name: r for r in roles_list if r.scope == "builtin"}


def test_h1_all_eleven_builtin_roles_exist_and_parse(builtin_roles):
    assert set(builtin_roles.keys()) == set(_ELEVEN)
    for name in _ELEVEN:
        assert (Path(R.BUILTIN_DIR) / f"{name}.md").is_file()


@pytest.mark.parametrize("name", _NEW_SEVEN)
def test_h2_new_role_prompt_contains_progress_on_disk_paragraph_verbatim(builtin_roles, name):
    assert _PROGRESS_ON_DISK_PARAGRAPH in builtin_roles[name].prompt


@pytest.mark.parametrize("name", _NEW_SEVEN)
def test_h3_new_role_prompt_contains_final_answer_rule_verbatim(builtin_roles, name):
    assert _FINAL_ANSWER_RULE in builtin_roles[name].prompt


def test_h4_three_reviewers_on_three_different_model_tiers(builtin_roles):
    # F3 fix (A1-audit.md, binding IMPLEMENTATION.md §2b.3): shipped roles pin explicit ids,
    # not bare aliases — a bare alias can silently resolve to a previous-generation model on
    # an older bundled CLI (memory: opus5-alias-staleness-2026-07-24).
    models = {
        "reviewer-logic": builtin_roles["reviewer-logic"].model,
        "reviewer-security": builtin_roles["reviewer-security"].model,
        "reviewer-quality": builtin_roles["reviewer-quality"].model,
    }
    assert models["reviewer-logic"] == "claude-opus-5"
    assert models["reviewer-security"] == "claude-fable-5-1"
    assert models["reviewer-quality"] == "claude-sonnet-5"
    assert len(set(models.values())) == 3


def test_h4b_remaining_four_new_roles_also_pin_explicit_model_ids(builtin_roles):
    # N6 fix (A2-audit.md): the guard above covered only 3 of the 7 new roles — reverting
    # architect/debugger/test-writer/docs-writer to bare aliases left the whole suite green
    # (proven live by the audit). Pin all four explicitly too, against a documented live
    # incident (memory: opus5-alias-staleness-2026-07-24).
    models = {
        "architect": builtin_roles["architect"].model,
        "debugger": builtin_roles["debugger"].model,
        "test-writer": builtin_roles["test-writer"].model,
        "docs-writer": builtin_roles["docs-writer"].model,
    }
    assert models["architect"] == "claude-opus-5"
    assert models["debugger"] == "claude-sonnet-5"
    assert models["test-writer"] == "claude-sonnet-5"
    assert models["docs-writer"] == "claude-sonnet-5"
    for name, model in models.items():
        assert model.startswith("claude-"), (name, model)


_READ_ONLY_ROLES = ["researcher", "skeptic", "reviewer-logic", "reviewer-security", "reviewer-quality", "architect"]


@pytest.mark.parametrize("name", _READ_ONLY_ROLES)
def test_h5_every_read_only_role_disallows_write_edit_notebookedit(builtin_roles, name):
    disallowed = builtin_roles[name].disallowed_tools or []
    assert "Write" in disallowed
    assert "Edit" in disallowed
    assert "NotebookEdit" in disallowed


def test_h6_ops_and_judge_are_absent(builtin_roles):
    # F13 (A1-audit.md leanness): dropped the doc-text grep that used to close this test — it
    # asserted `"... NOT shipped" in impl or "NOT shipped" in impl`, which is true for almost
    # any IMPLEMENTATION.md and can never go red. §7's own prose is read by a human during
    # review, not re-derived here. The falsifiable half (the roster fact itself) stays.
    assert "ops" not in builtin_roles
    assert "judge" not in builtin_roles


def test_h7_every_description_is_use_this_when_routing_logic(builtin_roles):
    for name, role in builtin_roles.items():
        assert "use this when" in role.description.lower(), (
            f"{name}: description must read as routing logic ('use this when...'), "
            f"got: {role.description!r}"
        )


# ══════════════════════════ Extra: builtin roles ship byte-identical to DEFAULT_AGENTS ══════════════════════════
# (Not a standalone checklist line, but the load-bearing claim H1-H4 build on — verified here
# so a future edit to engine.py's DEFAULT_AGENTS or the builtin files trips a red test.)

# IMPLEMENTATION.md §0.4, amended 2026-09-09 after the audit: `description` is deliberately
# NOT byte-identical to DEFAULT_AGENTS — it was rewritten into "use this when…" routing
# language (H7). Pinned here to the ROLE FILES' literal text (not derived from DEFAULT_AGENTS)
# so a future accidental edit to either is caught as a decision, not silently skipped — this
# is the exact field the audit's F5 finding named as the one nobody was comparing.
_EXPECTED_DESCRIPTIONS = {
    "executor": (
        "General code and infra execution agent. Use this when a task needs the repo or "
        "system actually changed: write files, edit code, run bash commands, install dependencies."
    ),
    "researcher": (
        "Read-only research agent. Use this when you need facts gathered (web lookups, file "
        "reads, grep) before deciding anything, without touching project files."
    ),
    "skeptic": (
        "Adversarial verifier, read-only. Use this when a claim or finding needs an "
        "independent attempt to REFUTE it with evidence before it is trusted."
    ),
    "quick": (
        "Fast lookup and simple transform agent. Use this when a question is cheap and needs "
        "a low-latency answer, not a full investigation."
    ),
}

# N4/I1m fix (A2-audit.md): `engine.DEFAULT_AGENTS[name].model` reads EXECUTOR_MODEL/
# RESEARCHER_MODEL/QUICK_MODEL at import time — an operator's env (still legitimately read as
# DEFAULT_AGENTS' own broken-install-fallback knob) made this comparison red with no code bug
# involved. The role FILES pin a fixed id regardless of env; compare against that literal
# baseline instead of the env-sensitive DEFAULT_AGENTS value.
_DEFAULT_MODEL_BY_ROLE = {
    "executor": "claude-sonnet-5",
    "researcher": "claude-sonnet-5",
    "skeptic": "claude-sonnet-5",
    "quick": "haiku",
}


@pytest.mark.parametrize("name", ["executor", "researcher", "skeptic", "quick"])
def test_copied_roles_match_default_agents_except_improved_description(builtin_roles, name):
    """Renamed from test_copied_roles_byte_identical_to_default_agents (F5, A1-audit.md): the
    old name claimed byte-identity while silently never comparing `description` — the one
    field IMPLEMENTATION.md §1 calls "the routing logic" and the one field that actually
    changed. Six fields stay byte-identical to DEFAULT_AGENTS; `description` is asserted
    against the role file's own pinned text instead."""
    default = engine.DEFAULT_AGENTS[name]
    role = builtin_roles[name]
    assert role.prompt == default.prompt
    assert role.tools == default.tools
    assert role.disallowed_tools == default.disallowedTools
    # N4/I1m fix (A2-audit.md): env-independent — see _DEFAULT_MODEL_BY_ROLE above.
    assert role.model == _DEFAULT_MODEL_BY_ROLE[name]
    assert role.effort == default.effort
    assert role.max_turns == default.maxTurns
    assert role.description == _EXPECTED_DESCRIPTIONS[name]
    assert role.description != default.description


# ══════════════════════════ I. Safety and regressions (roles.py half) ══════════════════════════

def test_i1c_toggling_builtin_with_existing_project_override_preserves_it(isolated_dirs):
    """I1c/F2 (A1-audit.md): set_enabled(scope="builtin") must NOT clobber an existing,
    customized project role file with the builtin's text — project role files are
    gitignored, so that loss is unrecoverable. It must toggle the EXISTING project file
    instead."""
    cwd, _ = isolated_dirs
    custom = (
        "---\nname: executor\ndescription: use this when custom work is needed\n"
        "model: claude-opus-5\n---\nCustom hand-tuned executor body.\n"
    )
    R.write_role(cwd, "executor", "project", custom)

    role = R.set_enabled(cwd, "executor", "builtin", False)

    assert role.scope == "project"
    assert role.enabled is False
    assert role.prompt == "Custom hand-tuned executor body."
    assert role.model == "claude-opus-5"
    project_path = R.role_path(cwd, "executor", "project")
    with open(project_path, encoding="utf-8") as f:
        text = f.read()
    assert "Custom hand-tuned executor body." in text
    assert "enabled: false" in text


def test_i1c_toggling_builtin_with_no_project_override_still_copies_it(isolated_dirs):
    """Companion to the test above: the FIRST toggle of a builtin (no project file yet) must
    still copy the builtin's own text into the project scope — only a pre-existing project
    file changes the behavior."""
    cwd, _ = isolated_dirs
    role = R.set_enabled(cwd, "quick", "builtin", False)
    assert role.scope == "project"
    assert role.enabled is False
    builtin_role = R.parse_role(
        open(R.role_path(cwd, "quick", "builtin"), encoding="utf-8").read(),
        name="quick", scope="builtin", path="",
    )[0]
    assert role.prompt == builtin_role.prompt


# ══════════════════════════ I1j: enum validation moves into parse_role ══════════════════════════

@pytest.mark.parametrize("bad_field,bad_line", [
    ("effort", "effort: banana"),
    ("permissionMode", "permissionMode: yolo"),
    ("memory", "memory: nonsense"),
    ("maxTurns", "maxTurns: -5"),
])
def test_i1j_invalid_enum_rejected_by_parse_role_itself(bad_field, bad_line):
    """I1j fix (A2-audit.md/F7 residual): validation used to live ONLY in webapp.py's HTTP
    write path — an already-on-disk file (hand-written, or written by any agent with Bash)
    still reached AgentDefinition verbatim. parse_role is the one choke point every READ goes
    through too, so a bad value must fail to parse here directly, with no HTTP involved."""
    text = f"---\nname: bad\ndescription: use this when bad\n{bad_line}\n---\nBody.\n"
    role, err = R.parse_role(text, name="bad", scope="project", path="/x/bad.md")
    assert role is None
    assert bad_field.lower() in err.lower()


def test_i1j_valid_enum_values_still_parse(isolated_dirs):
    text = (
        "---\nname: ok\ndescription: use this when ok\neffort: xhigh\n"
        "permissionMode: acceptEdits\nmemory: project\nmaxTurns: 10\n---\nBody.\n"
    )
    role, err = R.parse_role(text, name="ok", scope="project", path="/x/ok.md")
    assert err is None
    assert role.effort == "xhigh"
    assert role.permission_mode == "acceptEdits"
    assert role.memory == "project"
    assert role.max_turns == 10


def test_i1j_bad_enum_on_disk_never_reaches_compile_agents(isolated_dirs):
    """Companion to the HTTP-path test (tests/test_spec091_wiring.py::test_e5c...): a file
    written DIRECTLY to disk (no HTTP) with a garbage enum must never compile into an
    AgentDefinition — it must be skipped and reported, like any other unparseable file."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "bad.md",
           "---\nname: bad\ndescription: use this when bad\neffort: banana\n"
           "permissionMode: yolo\nmemory: nonsense\nmaxTurns: -5\n---\nbody\n")
    all_roles, errors = R.list_roles_report(cwd)
    assert not any(r.name == "bad" for r in all_roles)
    assert any(e["name"] == "bad" for e in errors)
    compiled = R.compile_agents(R.load_roles(cwd, all_roles))
    assert "bad" not in compiled


# ══════════════════════════ I1k: write_role overwrite guard + set_enabled TOCTOU/CRLF ══════════

def test_i1k_write_role_refuses_to_clobber_an_existing_file_by_default(isolated_dirs):
    cwd, _ = isolated_dirs
    R.write_role(cwd, "helper", "project", MINIMAL.format(name="helper"))
    with pytest.raises(FileExistsError):
        R.write_role(cwd, "helper", "project", MINIMAL.format(name="helper") + "\nmore.\n")
    # The original file must be untouched.
    path = R.role_path(cwd, "helper", "project")
    assert "more." not in Path(path).read_text(encoding="utf-8")


def test_i1k_write_role_overwrite_true_replaces_it(isolated_dirs):
    cwd, _ = isolated_dirs
    R.write_role(cwd, "helper", "project", MINIMAL.format(name="helper"))
    new_content = "---\nname: helper\ndescription: Use this when replaced.\n---\nReplaced.\n"
    role = R.write_role(cwd, "helper", "project", new_content, overwrite=True)
    assert role.prompt == "Replaced."


def test_i1k_write_role_new_file_needs_no_overwrite_flag(isolated_dirs):
    cwd, _ = isolated_dirs
    role = R.write_role(cwd, "brand-new", "project", MINIMAL.format(name="brand-new"))
    assert role.name == "brand-new"


def test_i1k_set_enabled_toggles_via_try_open_not_check_then_open(isolated_dirs, monkeypatch):
    """I1k fix (A2-audit.md): the old code called `os.path.isfile(dest_path)` and THEN a
    separate `open(src_path)` — a window between the two. The new code must not call
    `os.path.isfile` on the project path at all (it tries to open it directly and falls back
    to builtin on FileNotFoundError)."""
    cwd, _ = isolated_dirs
    calls = []
    real_isfile = os.path.isfile

    def _spy_isfile(path):
        calls.append(path)
        return real_isfile(path)

    monkeypatch.setattr(os.path, "isfile", _spy_isfile)
    role = R.set_enabled(cwd, "quick", "builtin", False)
    assert role.scope == "project"
    project_path = R.role_path(cwd, "quick", "project")
    assert project_path not in calls


def test_i1k_set_enabled_preserves_crlf_line_endings(isolated_dirs):
    """I1k fix (A2-audit.md): the docstring used to claim byte-for-byte preservation, which
    was false for a CRLF file — the two hardcoded fence lines (and a rewritten `enabled:`
    line) were joined with a bare "\\n", so a CRLF file came back with mixed endings. Every
    line this function writes must now use the file's own line ending consistently."""
    cwd, _ = isolated_dirs
    crlf_text = "---\r\nname: crlf-role\r\ndescription: Use this when crlf.\r\n---\r\nBody.\r\n"
    path = Path(R.role_path(cwd, "crlf-role", "project"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(crlf_text.encode("utf-8"))

    R.set_enabled(cwd, "crlf-role", "project", False)
    new_bytes = path.read_bytes()
    # Every line ending is "\r\n" — no bare "\n" anywhere (mixed endings is the old bug).
    assert new_bytes.count(b"\n") == new_bytes.count(b"\r\n")
    assert b"enabled: false" in new_bytes


# ══════════════════════════ I1l: an unreadable roles dir degrades, never raises ══════════════════

def test_i1l_unreadable_roles_dir_degrades_instead_of_raising(isolated_dirs):
    """I1l fix (A2-audit.md, pre-existing defect found by the second audit, item 5):
    `list_roles_report`'s own docstring already promised "never raises on a bad file" — an
    unreadable DIRECTORY (not a file) broke that promise via a bare `os.listdir` inside
    `_iter_role_files`, propagating PermissionError out of this function and, through
    engine.py's run_engine, killing every turn in the project."""
    cwd, _ = isolated_dirs
    d = Path(cwd) / ".claude-ops" / "roles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text("---\nname: x\ndescription: use this when x\n---\nb\n", encoding="utf-8")
    os.chmod(d, 0o000)
    try:
        all_roles, errors = R.list_roles_report(cwd)
        assert any(e["scope"] == "project" and e["path"] == str(d) for e in errors)
        assert not any(r.scope == "project" for r in all_roles)
        # The other tiers (builtin) are unaffected.
        assert any(r.name == "executor" for r in all_roles)
    finally:
        os.chmod(d, 0o755)
