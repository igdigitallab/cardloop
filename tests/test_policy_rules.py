"""
Tests for policy_rules.py — declarative Markdown policy rules (PreToolUse), docs/RULES.md.

Layers: parsing -> operators/fields -> tiers & override -> trust of version-controlled project
files (real throw-away repos) -> safety limits (size, count, ReDoS, timeouts) -> exact hook
output shapes -> caching -> engine wiring -> HTTP API. Nothing here touches the real
~/.claude-ops/rules: CARDLOOP_RULES_DIR is pointed at a tmp dir per test (global_dir() reads the
env live) and every project is a tmp dir.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import policy_rules as pr

NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


# ─────────────────────────── helpers ───────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    g = tmp_path / "global-rules"
    cwd = tmp_path / "myproject"          # the HTTP project id is derived from this name
    cwd.mkdir()
    monkeypatch.setenv("CARDLOOP_RULES_DIR", str(g))
    pr.clear_caches()
    yield SimpleNamespace(g=g, cwd=str(cwd), p=cwd / ".claude-ops" / "rules", tmp=tmp_path)
    pr.clear_caches()


def rule_text(front: str, body: str = "Do not do that.") -> str:
    return f"---\n{front.strip()}\n---\n{body}\n"


def put(directory: Path, stem: str, front: str, body: str = "Do not do that.") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.md"
    path.write_text(rule_text(front, body), encoding="utf-8")
    return path


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=GIT_ENV)


def fire(rs, tool, tool_input):
    matches, problems = pr.evaluate(rs, tool, tool_input)
    return matches


def names(matches):
    return sorted(m.name for m in matches)


BLOCK_BASH = "event: bash\naction: block\n"


# ══════════════════════════ A. parsing ══════════════════════════

def test_a1_minimal_rule_defaults():
    rule, err = pr.parse_rule(rule_text("name: r1\npattern: rm"), stem="r1")
    assert err is None
    assert (rule.enabled, rule.event, rule.action) == (True, "all", "warn")
    assert rule.message == "Do not do that."
    assert rule.shortcut is not None and rule.conditions == ()


def test_a2_name_defaults_to_file_stem_and_mismatch_warns():
    rule, err = pr.parse_rule(rule_text("pattern: rm"), stem="from-stem")
    assert err is None and rule.name == "from-stem"
    rule, err = pr.parse_rule(rule_text("name: other\npattern: rm"), stem="from-stem")
    assert err is None and rule.name == "other" and "from-stem.md" in rule.warnings[0]


def test_a3_conditions_block_list_of_mappings():
    text = rule_text(
        "name: c\nevent: file\naction: block\nconditions:\n"
        "  - field: file_path\n    operator: regex_match\n    pattern: \\.env$\n"
        "  - field: content\n    operator: contains\n    pattern: API_KEY\n")
    rule, err = pr.parse_rule(text, stem="c")
    assert err is None
    assert [(c.field, c.operator, c.pattern) for c in rule.conditions] == [
        ("file_path", "regex_match", r"\.env$"), ("content", "contains", "API_KEY")]
    assert rule.conditions[0].regex is not None and rule.conditions[1].regex is None


def test_a4_operator_defaults_to_regex_match_and_empty_conditions_list_is_fine():
    rule, err = pr.parse_rule(rule_text("name: c\nconditions:\n  - field: command\n    pattern: rm"), stem="c")
    assert err is None and rule.conditions[0].operator == "regex_match"
    rule, err = pr.parse_rule(rule_text("name: c\nconditions: []\npattern: rm"), stem="c")
    assert err is None and rule.shortcut is not None


def test_a5_quoting_rules():
    # single quotes: verbatim, '' is one quote; double quotes: JSON escapes; bare: verbatim
    assert pr.parse_frontmatter(["pattern: 'rm\\s+it''s'"])[0]["pattern"] == "rm\\s+it's"
    assert pr.parse_frontmatter(['pattern: "a\\\\sb"'])[0]["pattern"] == "a\\sb"
    assert pr.parse_frontmatter(["pattern: rm\\s+-rf"])[0]["pattern"] == "rm\\s+-rf"
    raw, err = pr.parse_frontmatter(['pattern: "rm\\s"'])
    assert err and "double-quoted" in err                      # \s is not a valid JSON escape
    raw, err = pr.parse_frontmatter(["pattern: 'unterminated"])
    assert err and "unterminated" in err


def test_a6_regex_keys_keep_hash_but_other_keys_drop_comments():
    raw, err = pr.parse_frontmatter(["pattern: foo #bar", "action: block   # strict", "enabled: true # on"])
    assert err is None
    assert raw == {"pattern": "foo #bar", "action": "block", "enabled": True}
    raw, _ = pr.parse_frontmatter(["pattern: 'foo'   # trailing comment after quotes"])
    assert raw["pattern"] == "foo"


def test_a7_bom_and_crlf_files_parse():
    text = "\ufeff---\r\nname: crlf\r\npattern: rm\r\n---\r\nMessage line.\r\n"
    rule, err = pr.parse_rule(text, stem="crlf")
    assert err is None and rule.name == "crlf" and rule.message == "Message line."


@pytest.mark.parametrize("front,fragment", [
    ("name: r\npattern: rm\nevnt: bash", "unsupported frontmatter key 'evnt'"),
    ("name: r\nname: r2\npattern: rm", "duplicate key 'name'"),
    ("name: r\nevent: stop\npattern: rm", "'event' must be one of"),
    ("name: r\naction: ask\npattern: rm", "'action' must be one of"),
    ("name: r\nenabled: maybe\npattern: rm", "'enabled' must be true or false"),
    ("name: bad name!\npattern: rm", "invalid rule name"),
    ("name: r", "needs at least one of"),
    ("name: r\npattern: rm\nconditions:\n  - field: command\n    pattern: x", "not both"),
    ("name: r\nconditions:\n  - field: nope\n    pattern: x", "'field' must be one of"),
    ("name: r\nconditions:\n  - field: command\n    operator: like\n    pattern: x", "'operator' must be one of"),
    ("name: r\nconditions:\n  - field: command", "'pattern' must be a non-empty string"),
    ("name: r\nconditions:\n  - field: command\n    pattern: x\n    extra: 1", "unsupported key 'extra'"),
    ("name: r\nconditions:\n  - field: tool_input.a..b\n    pattern: x", "'field' must be one of"),
    ("name: r\npattern: |", "block scalars"),
    ("name: r\n  indented: 1\npattern: rm", "indented"),
])
def test_a8_bad_frontmatter_is_rejected_with_a_reason(front, fragment):
    rule, err = pr.parse_rule(rule_text(front), stem="r")
    assert rule is None and fragment in err, err


def test_a9_missing_fence_empty_body_and_too_many_conditions():
    assert pr.parse_rule("name: r\npattern: x\n", stem="r")[1].startswith("line 1")
    assert "message body" in pr.parse_rule("---\nname: r\npattern: x\n---\n  \n", stem="r")[1]
    many = "name: r\nconditions:\n" + "".join(
        f"  - field: command\n    operator: contains\n    pattern: p{i}\n" for i in range(17))
    assert "at most 16" in pr.parse_rule(rule_text(many), stem="r")[1]


def test_a10_message_is_sanitised_and_capped():
    body = "\x1b[31mred\x1b[0m\x00 text\x07" + "x" * 9000
    rule, _ = pr.parse_rule(rule_text("name: r\npattern: x", body), stem="r")
    assert "\x1b" not in rule.message and "\x00" not in rule.message and "\x07" not in rule.message
    assert rule.message.startswith("red text")
    assert len(rule.message) <= pr.MAX_MESSAGE_CHARS


# ══════════════════════════ B. operators, fields, events ══════════════════════════

@pytest.mark.parametrize("op,pattern,value,expected", [
    ("regex_match", r"rm\s+-rf", "RM   -RF /x", True),            # case-insensitive by default
    ("regex_match", r"^rm", "echo hi\nrm x", True),               # ^ is per line
    ("regex_match", r"(?-i:RM)", "rm x", False),                  # opt back into case sensitivity
    ("regex_match", r"rm\s+-rf", "rmdir x", False),
    ("contains", "foo", "a foo b", True),
    ("contains", "foo", "a Foo b", False),                        # non-regex operators are exact
    ("equals", "ls", "ls", True),
    ("equals", "ls", "ls -la", False),
    ("not_contains", "sudo", "ls -la", True),
    ("not_contains", "sudo", "sudo ls", False),
    ("starts_with", "git ", "git push", True),
    ("starts_with", "git ", "x git push", False),
    ("ends_with", ".env", "cat a/.env", True),
    ("ends_with", ".env", "cat a/.env.bak", False),
])
def test_b1_every_operator(env, op, pattern, value, expected):
    put(env.g, "op", f"{BLOCK_BASH}conditions:\n  - field: command\n    operator: {op}\n    pattern: '{pattern}'")
    rs = pr.load_ruleset(None)
    assert bool(fire(rs, "Bash", {"command": value})) is expected


def test_b2_all_conditions_must_match(env):
    put(env.g, "both", f"{BLOCK_BASH}conditions:\n"
        "  - field: command\n    operator: contains\n    pattern: rm\n"
        "  - field: command\n    operator: contains\n    pattern: /srv")
    rs = pr.load_ruleset(None)
    assert fire(rs, "Bash", {"command": "rm /srv/x"})
    assert not fire(rs, "Bash", {"command": "rm /tmp/x"})
    assert not fire(rs, "Bash", {"command": "ls /srv"})


def test_b3_absent_field_matches_nothing_even_not_contains(env):
    put(env.g, "neg", "event: all\naction: block\nconditions:\n"
        "  - field: command\n    operator: not_contains\n    pattern: ok")
    rs = pr.load_ruleset(None)
    assert not fire(rs, "Write", {"file_path": "/x", "content": "y"})     # Write has no command
    assert fire(rs, "Bash", {"command": "ls"})


def test_b4_fields_resolve_per_tool(env):
    f = pr.field_value
    assert f("tool_name", "Bash", {}) == "Bash"
    assert f("command", "Bash", {"command": "ls"}) == "ls"
    assert f("file_path", "Write", {"file_path": "/a"}) == "/a"
    assert f("file_path", "Read", {"path": "/b"}) == "/b"
    assert f("file_path", "NotebookEdit", {"notebook_path": "/c.ipynb"}) == "/c.ipynb"
    assert f("content", "Write", {"content": "w"}) == "w"
    assert f("content", "Edit", {"old_string": "o", "new_string": "n"}) == "n"      # not the removed text
    assert f("content", "NotebookEdit", {"new_source": "s"}) == "s"
    assert f("command", "Write", {"file_path": "/a"}) is None


def test_b5_tool_input_dotted_paths_lists_and_json(env):
    ti = {"to": ["a@x.com", "b@evil.com"], "opts": {"cc": {"n": 3}}, "flag": True}
    assert pr.field_value("tool_input.to.1", "mcp__m__send", ti) == "b@evil.com"
    assert pr.field_value("tool_input.opts.cc.n", "mcp__m__send", ti) == "3"
    assert pr.field_value("tool_input.to", "mcp__m__send", ti) == '["a@x.com", "b@evil.com"]'
    assert pr.field_value("tool_input.flag", "mcp__m__send", ti) == "true"
    assert pr.field_value("tool_input.nope", "mcp__m__send", ti) is None
    assert pr.field_value("tool_input.to.9", "mcp__m__send", ti) is None
    put(env.g, "evil-domain", "event: mcp\naction: block\nconditions:\n"
        "  - field: tool_input.to\n    operator: contains\n    pattern: '@evil.com'")
    rs = pr.load_ruleset(None)
    assert fire(rs, "mcp__m__send", ti)
    assert not fire(rs, "mcp__m__send", {"to": ["ok@x.com"]})


def test_b6_multiedit_conditions_must_hold_on_the_same_edit(env):
    put(env.g, "same-edit", "event: file\naction: block\nconditions:\n"
        "  - field: file_path\n    operator: ends_with\n    pattern: .env\n"
        "  - field: content\n    operator: contains\n    pattern: SECRET")
    rs = pr.load_ruleset(None)
    hit = {"file_path": "/a/.env", "edits": [{"old_string": "a", "new_string": "SECRET=1"}]}
    assert fire(rs, "MultiEdit", hit)
    apart = {"file_path": "/a/.env", "edits": [{"old_string": "a", "new_string": "ok"}]}
    assert not fire(rs, "MultiEdit", apart)
    # per-edit file_path overrides the call's: SECRET sits in an edit of a different file
    split = {"file_path": "/a/.env", "edits": [{"file_path": "/b/x.py", "new_string": "SECRET"}]}
    assert not fire(rs, "MultiEdit", split)


@pytest.mark.parametrize("event,tool,expected", [
    ("bash", "Bash", True), ("bash", "PowerShell", True), ("bash", "Write", False),
    ("file", "Write", True), ("file", "Edit", True), ("file", "MultiEdit", True),
    ("file", "NotebookEdit", True), ("file", "Bash", False), ("file", "Read", False),
    ("mcp", "mcp__mail__send", True), ("mcp", "Bash", False),
    ("all", "Bash", True), ("all", "Read", True), ("all", "mcp__x__y", True),
])
def test_b7_event_scoping(env, event, tool, expected):
    put(env.g, "ev", f"event: {event}\naction: block\ntool_matcher: .")
    rs = pr.load_ruleset(None)
    assert bool(fire(rs, tool, {"command": "x", "file_path": "/x"})) is expected


def test_b8_tool_matcher_is_a_regex_on_the_tool_name(env):
    put(env.g, "mail", "event: mcp\naction: block\ntool_matcher: ^mcp__mail__")
    rs = pr.load_ruleset(None)
    assert fire(rs, "mcp__mail__send", {})
    assert fire(rs, "mcp__mail__reply", {})
    assert not fire(rs, "mcp__webmail__send", {})
    assert not fire(rs, "Bash", {})


def test_b9_pattern_shortcut_targets_the_tools_primary_text(env):
    put(env.g, "sc-bash", "event: all\naction: warn\npattern: DROP\\s+TABLE")
    rs = pr.load_ruleset(None)
    assert fire(rs, "Bash", {"command": "psql -c 'drop table x'"})
    assert fire(rs, "Write", {"file_path": "/a.sql", "content": "DROP TABLE y"})
    assert fire(rs, "mcp__db__query", {"sql": "drop   table z"})          # JSON of the input
    assert not fire(rs, "Bash", {"command": "ls"})
    pr.clear_caches()
    os.remove(env.g / "sc-bash.md")
    put(env.g, "sc-path", "event: file\naction: block\npattern: \\.env$")
    rs = pr.load_ruleset(None)
    assert fire(rs, "Write", {"file_path": "/x/.env", "content": "a"})
    assert not fire(rs, "Write", {"file_path": "/x/a.py", "content": "a"})


def test_b10_tool_matcher_only_rule_matches_every_call_of_that_tool(env):
    put(env.g, "no-send", "event: mcp\naction: block\ntool_matcher: ^mcp__mail__send$")
    rs = pr.load_ruleset(None)
    assert fire(rs, "mcp__mail__send", {})
    assert fire(rs, "mcp__mail__send", {"to": "a@b.c", "body": "hi"})


def test_b11_enabled_false_rule_never_fires(env):
    put(env.g, "off", f"enabled: false\n{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(None)
    assert not rs.active and not fire(rs, "Bash", {"command": "rm x"})
    assert rs.entries[0].status == "disabled"


# ══════════════════════════ C. tiers and override ══════════════════════════

def test_c1_project_overrides_global_by_name_and_global_is_marked_shadowed(env):
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm", "global message")
    put(env.p, "no-rm", f"{BLOCK_BASH}pattern: rm", "project message")
    rs = pr.load_ruleset(env.cwd)
    assert [e.tier for e in rs.active] == ["project"]
    assert fire(rs, "Bash", {"command": "rm x"})[0].message == "project message"
    shadowed = [e for e in rs.entries if e.tier == "global"][0]
    assert shadowed.status == "shadowed" and shadowed.shadowed_by == "project"


def test_c2_disabled_project_file_switches_off_the_global_rule_of_that_name(env):
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm")
    put(env.p, "no-rm", f"enabled: false\n{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(env.cwd)
    assert not rs.active and not fire(rs, "Bash", {"command": "rm x"})


def test_c3_an_invalid_override_does_not_shadow_the_lower_tier(env):
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm")
    (env.p).mkdir(parents=True)
    (env.p / "no-rm.md").write_text("not a rule at all", encoding="utf-8")
    rs = pr.load_ruleset(env.cwd)
    assert [e.tier for e in rs.active] == ["global"]
    assert any(e.status == "invalid" for e in rs.entries)


def test_c4_pack_dirs_are_the_lowest_tier_and_load_via_extra_dirs(env):
    pack = env.tmp / "pack-a"
    put(pack, "from-pack", f"{BLOCK_BASH}pattern: curl", "pack message")
    put(pack, "no-rm", f"{BLOCK_BASH}pattern: rm", "pack rm")
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm", "global rm")
    rs = pr.load_ruleset(env.cwd, extra_dirs=[str(pack)])
    by_name = {e.name: e for e in rs.active}
    assert set(by_name) == {"from-pack", "no-rm"}
    assert by_name["from-pack"].tier == "pack" and by_name["no-rm"].tier == "global"


def test_c5_duplicate_names_inside_one_tier_keep_the_first_file(env):
    put(env.g, "a-first", f"name: dup\n{BLOCK_BASH}pattern: rm", "first")
    put(env.g, "b-second", f"name: dup\n{BLOCK_BASH}pattern: rm", "second")
    rs = pr.load_ruleset(None)
    assert len(rs.active) == 1 and rs.active[0].rule.message == "first"


def test_c6_cwd_equal_to_the_global_dir_is_loaded_once_as_global(env, monkeypatch):
    home = env.tmp / "home"
    monkeypatch.setenv("CARDLOOP_RULES_DIR", str(home / ".claude-ops" / "rules"))
    put(home / ".claude-ops" / "rules", "r", f"{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(str(home))
    assert [(e.tier, e.name) for e in rs.entries] == [("global", "r")]


def test_c7_missing_dirs_are_not_errors_and_not_created(env):
    rs = pr.load_ruleset(env.cwd)
    assert rs.entries == [] and rs.diagnostics == []
    assert not env.g.exists() and not env.p.exists()


def test_c8_global_dir_honours_the_env_var_and_defaults_under_home(monkeypatch):
    monkeypatch.setenv("CARDLOOP_RULES_DIR", "/x/y")
    assert pr.global_dir() == "/x/y"
    monkeypatch.delenv("CARDLOOP_RULES_DIR")
    assert pr.global_dir() == os.path.expanduser("~/.claude-ops/rules")


# ══════════════════════════ D. trust: version-controlled project files ══════════════════════════

@NEEDS_GIT
def test_d1_a_tracked_project_rule_is_disabled_and_untracked_siblings_load(env):
    git(env.cwd, "init", "-q")
    put(env.p, "tracked", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", ".claude-ops/rules/tracked.md")
    git(env.cwd, "commit", "-qm", "rule")
    put(env.p, "local", f"{BLOCK_BASH}pattern: curl")        # never added
    rs = pr.load_ruleset(env.cwd)
    status = {e.name: e.status for e in rs.entries}
    assert status == {"local": "active", "tracked": "untrusted"}
    assert not fire(rs, "Bash", {"command": "rm x"})
    assert fire(rs, "Bash", {"command": "curl x"})
    untrusted = [e for e in rs.entries if e.name == "tracked"][0]
    assert not untrusted.trusted and "rules_trust_tracked" in untrusted.untrusted_reason


@NEEDS_GIT
def test_d2_the_per_project_opt_in_loads_tracked_files(env):
    git(env.cwd, "init", "-q")
    put(env.p, "tracked", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    git(env.cwd, "commit", "-qm", "rule")
    rs = pr.load_ruleset(env.cwd, trust_tracked=True)
    assert [e.status for e in rs.entries] == ["active"] and fire(rs, "Bash", {"command": "rm x"})


@NEEDS_GIT
def test_d3_a_staged_but_uncommitted_file_counts_as_tracked(env):
    git(env.cwd, "init", "-q")
    put(env.p, "staged", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    assert pr.load_ruleset(env.cwd).entries[0].status == "untrusted"


@NEEDS_GIT
def test_d4_gitignored_rule_files_load_normally(env):
    git(env.cwd, "init", "-q")
    (Path(env.cwd) / ".gitignore").write_text(".claude-ops/\n", encoding="utf-8")
    put(env.p, "private", f"{BLOCK_BASH}pattern: rm")
    assert pr.load_ruleset(env.cwd).entries[0].status == "active"


@NEEDS_GIT
def test_d5_an_untrusted_tracked_file_cannot_disable_a_global_rule(env):
    """A cloned repo ships `enabled: false` under the name of an operator's global rule."""
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm", "global")
    git(env.cwd, "init", "-q")
    put(env.p, "no-rm", f"enabled: false\n{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    git(env.cwd, "commit", "-qm", "evil")
    rs = pr.load_ruleset(env.cwd)
    assert [e.tier for e in rs.active] == ["global"]
    assert fire(rs, "Bash", {"command": "rm x"})
    # ...and once the operator opts in, the repo's file IS the override
    rs = pr.load_ruleset(env.cwd, trust_tracked=True)
    assert not rs.active


@NEEDS_GIT
def test_d6_untrusted_files_are_not_even_read(env):
    git(env.cwd, "init", "-q")
    path = put(env.p, "tracked", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    git(env.cwd, "commit", "-qm", "rule")
    with patch.object(pr, "_read_rule_text", side_effect=AssertionError("must not read")):
        rs = pr.load_ruleset(env.cwd)
    assert rs.entries[0].status == "untrusted" and rs.entries[0].path == str(path)


@NEEDS_GIT
def test_d7_when_git_cannot_answer_project_rules_are_disabled(env):
    (Path(env.cwd) / ".git").mkdir()                           # looks like a repo, git is gone
    put(env.p, "r", f"{BLOCK_BASH}pattern: rm")
    with patch.object(pr.subprocess, "run", side_effect=FileNotFoundError("git")):
        rs = pr.load_ruleset(env.cwd)
    assert rs.entries[0].status == "untrusted" and not rs.active
    assert "git is unavailable" in rs.entries[0].untrusted_reason
    assert any("project rules disabled" in d for d in rs.diagnostics)


@NEEDS_GIT
def test_d8_a_failing_git_is_treated_like_a_missing_git(env):
    (Path(env.cwd) / ".git").mkdir()                           # not a valid repo: ls-files exits 128
    put(env.p, "r", f"{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(env.cwd)
    assert rs.entries[0].status == "untrusted" and "git ls-files failed" in rs.entries[0].untrusted_reason


def test_d9_a_directory_outside_any_repository_loads_project_rules(env):
    put(env.p, "r", f"{BLOCK_BASH}pattern: rm")
    assert pr.load_ruleset(env.cwd).entries[0].status == "active"


def test_d10_global_and_pack_tiers_are_never_subject_to_the_tracking_check(env):
    put(env.g, "g", f"{BLOCK_BASH}pattern: rm")
    with patch.object(pr, "tracked_rule_files", side_effect=AssertionError("must not run")):
        rs = pr.load_ruleset(env.cwd)
    assert rs.entries[0].status == "active"


def test_d11_symlinked_rule_file_in_the_project_tier_is_refused(env):
    real = env.tmp / "elsewhere.md"
    real.write_text(rule_text(f"name: sl\n{BLOCK_BASH}pattern: rm"), encoding="utf-8")
    env.p.mkdir(parents=True)
    os.symlink(real, env.p / "sl.md")
    e = pr.load_ruleset(env.cwd).entries[0]
    assert e.status == "invalid" and "symbolic link" in e.error


def test_d12_symlinked_project_rules_dir_is_refused(env):
    target = env.tmp / "other-dir"
    put(target, "r", f"{BLOCK_BASH}pattern: rm")
    (Path(env.cwd) / ".claude-ops").mkdir()
    os.symlink(target, env.p)
    rs = pr.load_ruleset(env.cwd)
    assert rs.entries == [] and any("symbolic link" in d for d in rs.diagnostics)


def test_d13_symlinked_global_rule_file_is_allowed(env):
    real = env.tmp / "vault-rule.md"
    real.write_text(rule_text(f"name: lnk\n{BLOCK_BASH}pattern: rm"), encoding="utf-8")
    env.g.mkdir()
    os.symlink(real, env.g / "lnk.md")
    assert pr.load_ruleset(None).entries[0].status == "active"


def test_d14_trust_opt_in_is_read_live_from_the_topics_records():
    ctx = {"topics": {"1:1": {"cwd": "/p"}, "1:2": {"cwd": "/q", "rules_trust_tracked": True}}}
    assert pr.trust_tracked_for(ctx, "/q") is True
    assert pr.trust_tracked_for(ctx, "/p") is False
    assert pr.trust_tracked_for(None, "/p") is False and pr.trust_tracked_for({}, "/p") is False
    ctx["topics"]["1:1"]["rules_trust_tracked"] = "yes"           # strictly the boolean true
    assert pr.trust_tracked_for(ctx, "/p") is False


@NEEDS_GIT
def test_d15_the_tracked_answer_is_refreshed_after_its_ttl(env, monkeypatch):
    git(env.cwd, "init", "-q")
    put(env.p, "r", f"{BLOCK_BASH}pattern: rm")
    assert pr.get_ruleset(env.cwd).entries[0].status == "active"      # untracked
    git(env.cwd, "add", "-A")
    assert pr.get_ruleset(env.cwd).entries[0].status == "active"      # cached: file unchanged, TTL not over
    monkeypatch.setattr(pr, "TRACKED_TTL_S", 0.0)
    assert pr.get_ruleset(env.cwd).entries[0].status == "untrusted"   # now tracked


# ══════════════════════════ E. safety limits ══════════════════════════

def test_e1_oversized_file_is_skipped_with_a_diagnostic(env):
    env.g.mkdir()
    (env.g / "big.md").write_text(rule_text("name: big\npattern: x", "m" * (pr.MAX_FILE_BYTES + 10)), encoding="utf-8")
    e = pr.load_ruleset(None).entries[0]
    assert e.status == "invalid" and "larger than 64 KB" in e.error


def test_e2_a_file_at_the_limit_is_still_read(env):
    env.g.mkdir()
    head = "---\nname: edge\npattern: x\n---\n"
    body = "m" * (pr.MAX_FILE_BYTES - len(head))
    (env.g / "edge.md").write_text(head + body, encoding="utf-8")
    assert (env.g / "edge.md").stat().st_size == pr.MAX_FILE_BYTES
    assert pr.load_ruleset(None).entries[0].error is None


def test_e3_at_most_100_rules_load_and_the_project_tier_wins_the_slots(env):
    for i in range(pr.MAX_RULES):
        put(env.g, f"g{i:03d}", f"{BLOCK_BASH}pattern: g{i}")
    put(env.p, "mine", f"{BLOCK_BASH}pattern: mine")
    rs = pr.load_ruleset(env.cwd)
    assert len(rs.active) == pr.MAX_RULES
    assert "mine" in {e.name for e in rs.active}
    dropped = [e for e in rs.entries if e.error and "rule limit" in e.error]
    assert len(dropped) == 1 and dropped[0].tier == "global"


def test_e4_overlong_patterns_are_rejected_everywhere(env):
    long = "a" * (pr.MAX_PATTERN_CHARS + 1)
    assert "longer than 512" in pr.parse_rule(rule_text(f"name: r\npattern: {long}"), stem="r")[1]
    assert "longer than 512" in pr.parse_rule(rule_text(f"name: r\ntool_matcher: {long}"), stem="r")[1]
    cond = f"name: r\nconditions:\n  - field: command\n    operator: contains\n    pattern: {long}"
    assert "longer than 512" in pr.parse_rule(rule_text(cond), stem="r")[1]
    ok = "a" * pr.MAX_PATTERN_CHARS
    assert pr.parse_rule(rule_text(f"name: r\npattern: {ok}"), stem="r")[1] is None


@pytest.mark.parametrize("pattern", [
    "(a+)+", "(.*)*", "(a*)*b", "(a+)*", "([a-z]+)+$", "(\\w+\\s*)*x", "(?:a+){2,}", "(a|aa)+", "(a|a?)*b",
    "(a+){3}", "((a+)b)+", "(x+x+)+y", "(?:a|)+",
])
def test_e5_catastrophic_shapes_are_rejected(pattern):
    why = pr.unsafe_regex_reason(pattern)
    assert why and ("nested quantifiers" in why or "alternation" in why), (pattern, why)


@pytest.mark.parametrize("pattern", [r"(a)\1", r"(?P<x>a)(?P=x)", r"(a)(?(1)b|c)"])
def test_e6_backreferences_are_rejected(pattern):
    assert "backreferences" in pr.unsafe_regex_reason(pattern)


@pytest.mark.parametrize("pattern", [
    r"rm\s+-rf", r"\.env$", r"^mcp__mail__", r"(foo|bar)+x", r"(?:ab)*c", r"(\d{1,3}\.){3}\d{1,3}",
    r"(a{2})*", r"(a?)*b", r"(\d|[a-z])+x", r"password\s*=\s*\S+", r"AKIA[0-9A-Z]{16}", r"(?i:select)\s.*\sfrom",
    r"rm\s+(-[a-z]+\s+)?/", r"^(Write|Edit)$",
])
def test_e7_ordinary_patterns_are_accepted(pattern):
    assert pr.unsafe_regex_reason(pattern) is None, pattern


def test_e8_syntax_errors_are_diagnosed_without_echoing_the_pattern():
    why = pr.unsafe_regex_reason("(unclosed")
    assert why.startswith("invalid regex")
    assert pr.unsafe_regex_reason("") and pr.unsafe_regex_reason(None)


def test_e9_a_rejected_pattern_skips_only_that_rule(env):
    put(env.g, "bad", f"{BLOCK_BASH}pattern: (a+)+")
    put(env.g, "good", f"{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(None)
    assert [e.name for e in rs.active] == ["good"]
    bad = [e for e in rs.entries if e.name == "bad"][0]
    assert bad.status == "invalid" and "nested quantifiers" in bad.error


def test_e10_a_match_beyond_64kb_is_still_found(env):
    put(env.g, "late", f"{BLOCK_BASH}pattern: 'FORBIDDEN_TOKEN'")
    rs = pr.load_ruleset(None)
    pad = "x" * (pr.MATCH_WINDOW_CHARS + 5000)
    assert fire(rs, "Bash", {"command": pad + " FORBIDDEN_TOKEN"})
    assert fire(rs, "Bash", {"command": "FORBIDDEN_TOKEN " + pad})
    assert not fire(rs, "Bash", {"command": pad})


def test_e11_a_match_straddling_a_window_boundary_is_found(env):
    put(env.g, "straddle", f"{BLOCK_BASH}pattern: 'FORBIDDEN_TOKEN'")
    rs = pr.load_ruleset(None)
    cut = pr.MATCH_WINDOW_CHARS - 6                          # token starts 6 chars before the window end
    cmd = "x" * cut + "FORBIDDEN_TOKEN" + "y" * 200_000
    assert fire(rs, "Bash", {"command": cmd})


def test_e12_input_beyond_the_scan_cap_fails_closed_for_block_and_open_for_warn(env):
    put(env.g, "blk", f"{BLOCK_BASH}pattern: 'NEVER_PRESENT'")
    put(env.g, "wrn", "event: bash\naction: warn\npattern: 'NEVER_PRESENT_EITHER'")
    rs = pr.load_ruleset(None)
    huge = "x" * (pr.MAX_SCAN_CHARS + 1000)
    matches = fire(rs, "Bash", {"command": huge})
    assert [(m.name, m.unverified) for m in matches] == [("blk", True)]


def test_e13_non_regex_operators_see_the_whole_input_regardless_of_size(env):
    put(env.g, "tail", f"{BLOCK_BASH}conditions:\n  - field: command\n    operator: ends_with\n    pattern: THE_END")
    rs = pr.load_ruleset(None)
    assert fire(rs, "Bash", {"command": "x" * (pr.MAX_SCAN_CHARS + 1000) + "THE_END"})


QUADRATIC = r"(ab)*c"        # passes the static check; quadratic under search() on 'abab...'


def test_e14_a_runaway_regex_is_interrupted_and_the_block_rule_fails_closed(env):
    put(env.g, "slow-block", f"{BLOCK_BASH}pattern: '{QUADRATIC}'")
    put(env.g, "slow-warn", f"event: bash\naction: warn\npattern: '{QUADRATIC}'")
    rs = pr.load_ruleset(None)
    cmd = "ab" * 30_000
    t0 = time.monotonic()
    matches, problems = pr.evaluate(rs, "Bash", {"command": cmd})
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, elapsed                             # un-interrupted this runs for minutes
    assert [(m.name, m.unverified) for m in matches] == [("slow-block", True)]
    assert ("slow-block", "regex timed out") in problems and ("slow-warn", "regex timed out") in problems


def test_e15_the_watchdog_restores_the_previous_signal_handler_and_timer():
    import signal
    before = signal.getsignal(signal.SIGALRM)
    with pr._watchdog(5.0) as armed:
        assert armed is True
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_e16_the_watchdog_stands_down_when_a_timer_is_already_running():
    import signal
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        with pr._watchdog(1.0) as armed:
            assert armed is False
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0       # the foreign timer was left alone
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_e17_off_the_main_thread_the_windows_shrink_instead(env):
    put(env.g, "late", f"{BLOCK_BASH}pattern: 'FORBIDDEN_TOKEN'")
    rs = pr.load_ruleset(None)
    out = {}

    def work():
        with pr._watchdog(1.0) as armed:
            out["armed"] = armed
        out["small"] = pr.evaluate(rs, "Bash", {"command": "x" * 100_000 + "FORBIDDEN_TOKEN"})

    th = threading.Thread(target=work)
    th.start()
    th.join(30)
    assert out["armed"] is False
    matches, _ = out["small"]
    # 100 KB > the unarmed scan cap: undecidable, so the block rule fails closed
    assert [(m.name, m.unverified) for m in matches] == [("late", True)]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_e18_a_fifo_with_a_rule_name_is_rejected_without_blocking(env):
    env.g.mkdir()
    os.mkfifo(env.g / "pipe.md")
    t0 = time.monotonic()
    e = pr.load_ruleset(None).entries[0]
    assert time.monotonic() - t0 < 2 and e.status == "invalid" and "not a regular file" in e.error


def test_e19_non_utf8_and_unreadable_files_do_not_break_loading(env):
    env.g.mkdir()
    (env.g / "bin.md").write_bytes(b"---\nname: bin\n\xff\xfe\n---\nx")
    put(env.g, "ok", f"{BLOCK_BASH}pattern: rm")
    rs = pr.load_ruleset(None)
    assert {e.name: e.status for e in rs.entries} == {"bin": "invalid", "ok": "active"}


# ══════════════════════════ F. hook: exact output shapes, counters, audit ══════════════════════════

def make(env, calls=None, ctx=None):
    audit = (lambda *a: calls.append(a)) if calls is not None else None
    return pr.make_hook("proj", env.cwd, ctx or {}, audit_fn=audit)


async def call(hook, tool, tool_input):
    return await hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}, "tu1", None)


async def test_f1_block_output_is_the_exact_deny_shape(env):
    put(env.g, "no-send", "event: mcp\naction: block\ntool_matcher: ^mcp__mail__send$", "Mail goes through the operator.")
    out = await call(make(env), "mcp__mail__send", {"to": "a@b.c"})
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Blocked by policy rule:\n[no-send] Mail goes through the operator.",
    }}


async def test_f2_warn_output_is_context_only_and_never_carries_a_permission_decision(env):
    put(env.g, "care", "event: bash\naction: warn\npattern: 'rm\\s'", "You are removing files; double-check the path.")
    out = await call(make(env), "Bash", {"command": "rm x"})
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "Policy warning (the call is allowed to proceed):\n"
                             "[care] You are removing files; double-check the path.",
    }}
    # "allow" would skip the permission prompt in ask mode: a warning must not approve anything
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "decision" not in out


async def test_f3_block_wins_over_warn_and_every_match_is_counted_and_audited(env):
    put(env.g, "w", "event: bash\naction: warn\npattern: rm", "warn text")
    put(env.g, "b", f"{BLOCK_BASH}pattern: rm", "block text")
    calls = []
    out = await call(make(env, calls), "Bash", {"command": "rm x"})
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "[b] block text" in spec["permissionDecisionReason"] and "warn text" not in spec["permissionDecisionReason"]
    assert sorted(c[2].split(":")[0] for c in calls) == ["b block", "w warn"]
    assert pr.hit_stats(env.cwd)["w"]["hits"] == 1 and pr.hit_stats(env.cwd)["b"]["hits"] == 1


async def test_f4_several_blockers_are_listed_together(env):
    put(env.g, "b1", f"{BLOCK_BASH}pattern: rm", "first")
    put(env.g, "b2", f"{BLOCK_BASH}pattern: rm", "second")
    out = await call(make(env), "Bash", {"command": "rm x"})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Blocked by policy rules:") and "[b1] first" in reason and "[b2] second" in reason


async def test_f5_no_match_and_no_rules_return_an_empty_dict(env):
    assert await call(make(env), "Bash", {"command": "ls"}) == {}
    put(env.g, "b", f"{BLOCK_BASH}pattern: rm")
    assert await call(make(env), "Bash", {"command": "ls"}) == {}
    assert await call(make(env), "Write", {"file_path": "/x"}) == {}


async def test_f6_zero_rules_never_reaches_the_evaluator(env):
    hook = make(env)
    with patch.object(pr, "evaluate", side_effect=AssertionError("must not run")):
        assert await call(hook, "Bash", {"command": "rm -rf /"}) == {}


async def test_f7_garbage_input_never_raises(env):
    put(env.g, "b", f"{BLOCK_BASH}pattern: rm")
    hook = make(env)
    for bad in ({}, {"tool_input": None}, "not a dict", None, {"tool_name": None, "tool_input": "x"},
                {"tool_name": "Bash", "tool_input": {"command": 5}}, {"tool_name": "Bash", "tool_input": []}):
        assert isinstance(await hook(bad, None, None), dict)


async def test_f8_an_internal_error_fails_open_and_is_logged(env, capsys):
    put(env.g, "b", f"{BLOCK_BASH}pattern: rm")
    hook = make(env)
    with patch.object(pr, "evaluate", side_effect=RuntimeError("boom")):
        assert await call(hook, "Bash", {"command": "rm x"}) == {}
    assert "hook error (RuntimeError)" in capsys.readouterr().out


async def test_f9_audit_line_format_and_200_char_cap(env):
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm")
    calls = []
    await call(make(env, calls), "Bash", {"command": "rm " + "x" * 500})
    project, kind, text = calls[0]
    assert (project, kind) == ("proj", "RULE")
    assert text.startswith("no-rm block: rm xxx") and len(text) == 200 and text.endswith("...")


async def test_f10_audit_never_contains_file_content_or_mcp_argument_values(env):
    put(env.g, "w", "event: file\naction: warn\npattern: SECRET")
    put(env.g, "m", "event: mcp\naction: warn\ntool_matcher: .")
    calls = []
    hook = make(env, calls)
    await call(hook, "Write", {"file_path": "/a/.env", "content": "SECRET=hunter2"})
    await call(hook, "mcp__mail__send", {"to": "a@b.c", "body": "hunter2"})
    text = " ".join(c[2] for c in calls)
    assert "hunter2" not in text and "a@b.c" not in text
    assert "w warn: Write /a/.env" in text and "m warn: mcp__mail__send {body,to}" in text


async def test_f11_audit_line_cannot_be_broken_by_newlines_or_escapes_in_the_command(env):
    put(env.g, "no-rm", f"{BLOCK_BASH}pattern: rm")
    calls = []
    await call(make(env, calls), "Bash", {"command": "rm a\nFAKE 2026 [x] TASK: pwned\x1b[31m"})
    assert "\n" not in calls[0][2] and "\x1b" not in calls[0][2]


async def test_f12_an_unverified_block_says_so_in_both_the_reason_and_the_audit_line(env):
    put(env.g, "blk", f"{BLOCK_BASH}pattern: 'NEVER_PRESENT'")
    calls = []
    out = await call(make(env, calls), "Bash", {"command": "x" * (pr.MAX_SCAN_CHARS + 10)})
    assert "failed closed" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "[unverified]" in calls[0][2]


async def test_f13_combined_output_is_capped(env):
    for i in range(20):
        put(env.g, f"b{i:02d}", f"{BLOCK_BASH}pattern: rm", "m" * 7000)
    out = await call(make(env), "Bash", {"command": "rm x"})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) <= pr.MAX_MESSAGE_CHARS and reason.endswith("[output truncated]")


async def test_f14_extra_dirs_callable_is_consulted_per_call(env):
    pack = env.tmp / "pack"
    hook = pr.make_hook("proj", env.cwd, {}, extra_dirs=lambda: [str(pack)])
    assert await call(hook, "Bash", {"command": "curl x"}) == {}
    put(pack, "pk", f"{BLOCK_BASH}pattern: curl", "no curl")
    out = await call(hook, "Bash", {"command": "curl x"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_f15_ask_and_plan_modes_do_not_change_anything_the_hook_is_mode_blind(env):
    put(env.g, "b", f"{BLOCK_BASH}pattern: rm")
    out = await make(env)({"tool_name": "Bash", "tool_input": {"command": "rm x"}, "permission_mode": "plan"}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ══════════════════════════ G. performance ══════════════════════════

def test_g1_100_rules_against_a_64kb_input_stay_under_100ms(env):
    pats = [r"rm\s+-rf\s+/data%d", r"curl\s+.*\|\s*sh%d", r"\.env%d$", r"password%d\s*=", r"AKIA%d[0-9A-Z]{16}"]
    for i in range(100):
        put(env.g, f"r{i:03d}", f"{BLOCK_BASH}pattern: '{pats[i % 5] % i}'")
    rs = pr.load_ruleset(None)
    assert len(rs.active) == 100
    cmd = ("echo hello world; " * 4000)[:65536]
    best = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        matches, problems = pr.evaluate(rs, "Bash", {"command": cmd})
        best = min(best, time.perf_counter() - t0)
    assert not matches and not problems
    assert best < 0.1, f"{best * 1000:.1f} ms"


async def test_g2_the_per_call_overhead_with_unchanged_files_is_small(env):
    for i in range(20):
        put(env.g, f"r{i}", f"{BLOCK_BASH}pattern: 'zzz{i}'")
    hook = make(env)
    await call(hook, "Bash", {"command": "ls"})            # warm: loads + caches
    t0 = time.perf_counter()
    for _ in range(50):
        await call(hook, "Bash", {"command": "ls"})
    assert (time.perf_counter() - t0) / 50 < 0.01


# ══════════════════════════ H. caching and live reload ══════════════════════════

async def test_h1_an_edited_rule_applies_to_the_next_call_without_a_restart(env):
    path = put(env.g, "r", f"{BLOCK_BASH}pattern: rm", "v1")
    hook = make(env)
    assert "v1" in (await call(hook, "Bash", {"command": "rm x"}))["hookSpecificOutput"]["permissionDecisionReason"]
    path.write_text(rule_text(f"{BLOCK_BASH}pattern: rm", "version two"), encoding="utf-8")
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
    assert "version two" in (await call(hook, "Bash", {"command": "rm x"}))["hookSpecificOutput"]["permissionDecisionReason"]
    path.unlink()
    assert await call(hook, "Bash", {"command": "rm x"}) == {}


async def test_h2_new_files_are_picked_up_and_files_are_parsed_once_per_change(env):
    parses = []
    real = pr.parse_rule
    with patch.object(pr, "parse_rule", side_effect=lambda *a, **k: (parses.append(1), real(*a, **k))[1]):
        put(env.g, "r1", f"{BLOCK_BASH}pattern: rm")
        hook = make(env)
        for _ in range(5):
            await call(hook, "Bash", {"command": "ls"})
        assert len(parses) == 1
        put(env.g, "r2", f"{BLOCK_BASH}pattern: curl")
        out = await call(hook, "Bash", {"command": "curl x"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert len(parses) == 2                                # r1 came from the cache


async def test_h3_flipping_the_trust_opt_in_applies_on_the_next_call(env):
    ctx = {"topics": {"1:1": {"cwd": env.cwd}}}
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    git(env.cwd, "init", "-q")
    put(env.p, "tracked", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    git(env.cwd, "commit", "-qm", "r")
    hook = make(env, ctx=ctx)
    assert await call(hook, "Bash", {"command": "rm x"}) == {}
    ctx["topics"]["1:1"]["rules_trust_tracked"] = True
    assert (await call(hook, "Bash", {"command": "rm x"}))["hookSpecificOutput"]["permissionDecision"] == "deny"
    ctx["topics"]["1:1"].pop("rules_trust_tracked")
    assert await call(hook, "Bash", {"command": "rm x"}) == {}


def test_h4_problems_are_logged_once_per_change(env, capsys):
    env.g.mkdir()
    (env.g / "bad.md").write_text("garbage", encoding="utf-8")
    pr.get_ruleset(None)
    pr.get_ruleset(None)
    assert capsys.readouterr().out.count("bad.md") == 1


# ══════════════════════════ I. engine wiring ══════════════════════════

class _FakeClient:
    captured = None

    def __init__(self, options):
        _FakeClient.captured = options

    async def query(self, prompt):
        pass

    async def receive_response(self):
        return
        yield  # pragma: no cover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _captured_options(cwd):
    import bot
    import engine
    with patch.object(engine, "ClaudeSDKClient", _FakeClient), \
            patch.object(engine, "running", {}), \
            patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(project_name="t", cwd=cwd, prompt="hi", session_key="pr:t", model="sonnet"):
            pass
    return _FakeClient.captured


async def test_i1_run_engine_registers_the_rules_hook_for_every_tool_next_to_the_bash_guards(env):
    import engine
    put(env.p, "no-rm", f"{BLOCK_BASH}pattern: rm", "no rm here")
    opts = await _captured_options(env.cwd)
    matchers = opts.hooks["PreToolUse"]
    bash, every = matchers
    assert bash.matcher == "Bash"
    assert bash.hooks == [engine._bundle_grep_guard_hook, engine._dangerous_command_guard_hook]
    assert every.matcher is None and len(every.hooks) == 1
    out = await every.hooks[0]({"tool_name": "Bash", "tool_input": {"command": "rm x"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await every.hooks[0]({"tool_name": "mcp__x__y", "tool_input": {}}, None, None) == {}


async def test_i2_the_wired_hook_audits_through_the_engines_audit_function(env):
    import bot
    import engine
    put(env.p, "no-rm", f"{BLOCK_BASH}pattern: rm", "no rm here")
    audited = []
    with patch.object(engine, "ClaudeSDKClient", _FakeClient), \
            patch.object(engine, "running", {}), \
            patch.object(engine, "audit", lambda *a: audited.append(a)):
        async for _ in bot.run_engine(project_name="t", cwd=env.cwd, prompt="hi", session_key="pr:t2", model="sonnet"):
            pass
        hook = _FakeClient.captured.hooks["PreToolUse"][1].hooks[0]
        await hook({"tool_name": "Bash", "tool_input": {"command": "rm x"}}, None, None)
    assert ("t", "RULE", "no-rm block: rm x") in audited


# ══════════════════════════ J. HTTP API ══════════════════════════

@pytest.fixture
def api_ctx(env):
    from webapp import _derive_token
    data_dir = env.tmp / "data"
    data_dir.mkdir()
    ctx = {
        "topics": {"1001:42": {"project": "myproject", "cwd": env.cwd, "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": "testpass", "DATA": data_dir, "HERE": ROOT,
        "VAULT_PROJECTS": env.tmp / "vault" / "01-Projects", "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None, "run_engine": None,
        "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token("testpass")
    return ctx


@pytest.fixture
def api_app(api_ctx):
    from aiohttp import web
    import webapp
    app = web.Application(middlewares=[webapp.auth_middleware])
    app["ctx"] = api_ctx
    app.router.add_get("/api/projects/{id}/rules", webapp.api_project_rules)
    app.router.add_get("/api/projects/{id}/settings", webapp.api_project_settings_get)
    app.router.add_post("/api/projects/{id}/settings", webapp.api_project_settings_post)
    return app


def _h(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_j1_rules_endpoint_lists_rules_with_hits_and_diagnostics(aiohttp_client, api_app, api_ctx, env):
    put(env.g, "no-send", "event: mcp\naction: block\ntool_matcher: ^mcp__mail__send$", "no mail")
    put(env.p, "care", "event: bash\naction: warn\npattern: rm", "careful")
    (env.p / "broken.md").write_text("no frontmatter", encoding="utf-8")
    hook = pr.make_hook("myproject", env.cwd, api_ctx)
    await hook({"tool_name": "mcp__mail__send", "tool_input": {}}, None, None)
    await hook({"tool_name": "mcp__mail__send", "tool_input": {}}, None, None)

    client = await aiohttp_client(api_app)
    resp = await client.get("/api/projects/myproject/rules", headers=_h(api_ctx))
    assert resp.status == 200
    data = await resp.json()
    rows = {r["name"]: r for r in data["rules"]}
    assert set(rows) == {"no-send", "care", "broken"}
    assert rows["no-send"] | {"hits": 2} == rows["no-send"]
    assert rows["no-send"]["tier"] == "global" and rows["no-send"]["action"] == "block"
    assert rows["no-send"]["event"] == "mcp" and rows["no-send"]["enabled"] is True
    assert rows["no-send"]["trusted"] is True and rows["no-send"]["status"] == "active"
    assert rows["no-send"]["path"] == str(env.g / "no-send.md") and rows["no-send"]["last_hit"] > 0
    assert rows["care"]["tier"] == "project" and rows["care"]["hits"] == 0
    assert rows["broken"]["status"] == "invalid" and rows["broken"]["diagnostics"]
    assert data["trust_tracked"] is False and data["limits"]["max_rules"] == 100
    assert data["global_dir"] == str(env.g) and data["project_dir"] == str(env.p)


@NEEDS_GIT
async def test_j2_untrusted_rows_and_the_opt_in_flow(aiohttp_client, api_app, api_ctx, env):
    git(env.cwd, "init", "-q")
    put(env.p, "tracked", f"{BLOCK_BASH}pattern: rm")
    git(env.cwd, "add", "-A")
    git(env.cwd, "commit", "-qm", "r")
    client = await aiohttp_client(api_app)
    h = _h(api_ctx)
    row = (await (await client.get("/api/projects/myproject/rules", headers=h)).json())["rules"][0]
    assert row["status"] == "untrusted" and row["trusted"] is False and row["enabled"] is False
    assert "rules_trust_tracked" in row["diagnostics"][0]

    resp = await client.post("/api/projects/myproject/settings", json={"rules_trust_tracked": True}, headers=h)
    assert resp.status == 200 and (await resp.json())["settings"]["rules_trust_tracked"] is True
    assert api_ctx["topics"]["1001:42"]["rules_trust_tracked"] is True
    data = await (await client.get("/api/projects/myproject/rules", headers=h)).json()
    assert data["trust_tracked"] is True and data["rules"][0]["status"] == "active"

    resp = await client.post("/api/projects/myproject/settings", json={"rules_trust_tracked": False}, headers=h)
    assert (await resp.json())["settings"]["rules_trust_tracked"] is False
    assert "rules_trust_tracked" not in api_ctx["topics"]["1001:42"]


async def test_j3_trust_setting_is_strictly_boolean(aiohttp_client, api_app, api_ctx):
    client = await aiohttp_client(api_app)
    h = _h(api_ctx)
    for bad in ("true", 1, None, "yes"):
        resp = await client.post("/api/projects/myproject/settings", json={"rules_trust_tracked": bad}, headers=h)
        assert resp.status == 400, bad
    assert (await (await client.get("/api/projects/myproject/settings", headers=h)).json())["rules_trust_tracked"] is False


async def test_j4_auth_and_unknown_project(aiohttp_client, api_app, api_ctx):
    client = await aiohttp_client(api_app)
    assert (await client.get("/api/projects/myproject/rules")).status == 401
    assert (await client.get("/api/projects/nope/rules", headers=_h(api_ctx))).status == 404


async def test_j5_empty_state(aiohttp_client, api_app, api_ctx):
    client = await aiohttp_client(api_app)
    data = await (await client.get("/api/projects/myproject/rules", headers=_h(api_ctx))).json()
    assert data["rules"] == [] and data["diagnostics"] == []


# ══════════════════════════ K. source hygiene ══════════════════════════

def test_k1_module_imports_nothing_from_engine_or_webapp():
    src = (ROOT / "policy_rules.py").read_text(encoding="utf-8")
    for banned in ("import engine", "from engine", "import webapp", "from webapp"):
        assert banned not in src


def test_k2_no_personal_path_or_new_dependency():
    src = (ROOT / "policy_rules.py").read_text(encoding="utf-8")
    assert os.path.expanduser("~") not in src or os.path.expanduser("~") == "/"
    assert "/home/" not in src
    assert "yaml" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()


def test_k3_docs_name_the_same_limits_as_the_code():
    doc = (ROOT / "docs" / "RULES.md").read_text(encoding="utf-8")
    for needle in (str(pr.MAX_FILE_BYTES // 1024) + " KB", str(pr.MAX_RULES), str(pr.MAX_PATTERN_CHARS),
                   "rules_trust_tracked", "mcp__mail__send", "CARDLOOP_RULES_DIR", "permissionDecision"):
        assert needle in doc, needle


def test_k4_the_worked_examples_in_the_docs_are_real_rules(env):
    import re as _re
    doc = (ROOT / "docs" / "RULES.md").read_text(encoding="utf-8")
    blocks = [b for b in _re.findall(r"```markdown\n(.*?)```", doc, _re.S)
              if "name: block-mail-send" in b or "name: warn-rm-client-files" in b]
    assert len(blocks) == 2
    env.g.mkdir()
    for idx, text in enumerate(blocks):
        (env.g / f"example-{idx}.md").write_text(text, encoding="utf-8")
    rs = pr.load_ruleset(None)
    assert {e.name: e.status for e in rs.entries} == {"block-mail-send": "active", "warn-rm-client-files": "active"}
    m = fire(rs, "mcp__mail__send", {"to": "x@y.z"})
    assert [(x.name, x.action) for x in m] == [("block-mail-send", "block")]
    assert not fire(rs, "mcp__mail__read", {})
    m = fire(rs, "Bash", {"command": "rm -rf /srv/client-files/old"})
    assert [(x.name, x.action) for x in m] == [("warn-rm-client-files", "warn")]
    assert not fire(rs, "Bash", {"command": "rm -rf /tmp/scratch"})
    assert not fire(rs, "Bash", {"command": "ls /srv/client-files"})
