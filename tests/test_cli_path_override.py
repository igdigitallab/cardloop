"""CLAUDE_CLI_PATH — the escape hatch for a model that ships before the SDK bundles a CLI
new enough to run it (Opus 5.5, 2026-09-21: the API rejected claude-opus-5-5 with
"Claude Code 2.1.276 does not support this model; version 2.1.280 or newer is required",
by alias AND by explicit id, and no claude-agent-sdk release carried 2.1.280 yet).

Two properties matter here:
  * a MISCONFIGURED override degrades to the bundled CLI instead of taking the cockpit down —
    a typo in .env would otherwise make every run fail to spawn;
  * the three places that resolve it independently (runtime.py for the cockpit, the alias
    verifier and the journal cron, both of which must run standalone from cron) agree — a
    drifting copy would probe or bill one binary while the service runs another.
"""
import importlib.util
import os
import stat
import sys

import engine
import runtime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses in the loaded module resolve their annotations via
    # sys.modules[cls.__module__], which is None for an unregistered ad-hoc module.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _verifier():
    return _load(os.path.join(_ROOT, "tools", "verify_model_aliases.py"), "verify_model_aliases")


def _journal():
    return _load(os.path.join(_ROOT, "tools", "daily-journal.py"), "daily_journal")


def _make_exe(tmp_path, name="claude"):
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho hi\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_unset_means_bundled_cli(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    assert runtime.resolve_cli_path("") is None


def test_executable_path_is_used(monkeypatch, tmp_path):
    exe = _make_exe(tmp_path)
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(exe))
    assert runtime.resolve_cli_path() == str(exe)


def test_quotes_are_tolerated(tmp_path):
    """bot.py's .env loader does not strip quotes, so a quoted value reaches us verbatim."""
    exe = _make_exe(tmp_path)
    assert runtime.resolve_cli_path(f'"{exe}"') == str(exe)


def test_tilde_is_expanded(monkeypatch, tmp_path):
    exe = _make_exe(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert runtime.resolve_cli_path("~/claude") == str(exe)


def test_missing_file_falls_back_to_the_bundle(tmp_path):
    assert runtime.resolve_cli_path(str(tmp_path / "nope")) is None


def test_non_executable_file_falls_back_to_the_bundle(tmp_path):
    plain = tmp_path / "claude"
    plain.write_text("not executable")
    plain.chmod(0o644)
    assert runtime.resolve_cli_path(str(plain)) is None


def test_directory_is_not_accepted(tmp_path):
    assert runtime.resolve_cli_path(str(tmp_path)) is None


def test_engine_exposes_the_resolved_path():
    """engine builds every ClaudeAgentOptions with this value; it must be runtime's, not a
    second opinion computed from a different rule."""
    assert engine.CLI_PATH == runtime.CLI_PATH


def test_verifier_probes_the_active_cli_not_the_bundle(monkeypatch, tmp_path):
    """The ground-truth check must probe whatever actually serves runs, or it verifies a
    binary nobody uses — reporting a mismatch the operator already fixed, or missing one."""
    v = _verifier()
    exe = _make_exe(tmp_path)
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(exe))
    assert v._active_cli() == str(exe)

    monkeypatch.setenv("CLAUDE_CLI_PATH", str(tmp_path / "nope"))
    assert v._cli_path_override() is None
    assert v._active_cli() == v._bundled_cli()


def test_verifier_reads_dotenv_when_the_process_env_is_empty(monkeypatch, tmp_path):
    """cron and a bare shell do not load .env the way bot.py does, so the tool reads it
    itself — otherwise the daily watch probes the bundle while the service runs an external
    CLI, and pages on a mismatch that does not exist."""
    v = _verifier()
    exe = _make_exe(tmp_path)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.delenv("COPS_NO_DOTENV", raising=False)
    monkeypatch.setattr(v, "_REPO_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text(f'FOO=bar\nCLAUDE_CLI_PATH="{exe}"\n')
    assert v._cli_path_raw() == str(exe)
    assert v._cli_path_override() == str(exe)


def test_real_env_wins_over_dotenv(monkeypatch, tmp_path):
    v = _verifier()
    exe = _make_exe(tmp_path)
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(exe))
    monkeypatch.setattr(v, "_REPO_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("CLAUDE_CLI_PATH=/dev/null/from-dotenv\n")
    assert v._cli_path_raw() == str(exe)


def test_all_three_resolvers_agree(monkeypatch, tmp_path):
    """runtime.py (cockpit), verify_model_aliases (cron probe) and daily-journal (cron digest)
    each resolve this independently. If they drift, one of them silently uses a different
    binary than the one the operator configured."""
    v, j = _verifier(), _journal()
    exe = _make_exe(tmp_path)
    plain = tmp_path / "plain"
    plain.write_text("x")
    plain.chmod(0o644)

    for raw, expected in [
        (str(exe), str(exe)),
        (f'"{exe}"', str(exe)),
        (f"  {exe}  ", str(exe)),
        (str(tmp_path / "missing"), None),
        (str(plain), None),
        (str(tmp_path), None),
        ("", None),
    ]:
        monkeypatch.setenv("CLAUDE_CLI_PATH", raw)
        monkeypatch.setattr(v, "_REPO_ROOT", str(tmp_path))  # no stray .env fallback
        assert runtime.resolve_cli_path() == expected, raw
        assert v._cli_path_override() == expected, raw
        assert j._cli_path() == expected, raw
