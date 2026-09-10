"""T3 final-gate re-probe: A2-fix-audit.md's N1 probe re-run against the round-2 tree.
Not part of the permanent suite contract — scratch verification only, safe to delete after
the gate report is filed. Mirrors /tmp/cardloop-scratch/rev/tests/test_a2_probe*.py exactly,
against the LIVE (round-2-fixed) roles.py/engine.py instead of the pre-round-2 copy.

The N2 probes (main-role provenance/append-ordering) and the N3 probe (main.md not counting
as "role files exist") were removed with the `main` role concept (FX6) — the mechanisms they
guarded no longer exist.
"""
import os
import sys
from pathlib import Path

import pytest

from tests.test_spec091_wiring import isolated_dirs, _run_engine_capturing, _write  # noqa: F401

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
import roles as R  # noqa: E402


@pytest.mark.asyncio
async def test_reprobe_n1_all_disabled_denies_spawn_tools_not_silent_cli_fallback(isolated_dirs):
    cwd, _ = isolated_dirs
    for name in R.load_roles(None):
        R.set_enabled(cwd, name, "builtin", False)
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="reprobe-n1:t", model="sonnet")
    assert opts.agents == {}  # still an unset-equivalent dict at the wire (SDK drops it) —
    # BUT now the turn denies the tools that could exploit that collision:
    assert "Agent" in (opts.disallowed_tools or [])
    assert "Task" in (opts.disallowed_tools or [])
    assert "Workflow" in (opts.disallowed_tools or [])


def test_reprobe_i1l_unreadable_roles_dir_degrades_instead_of_raising(isolated_dirs):
    cwd, _ = isolated_dirs
    d = Path(cwd) / ".claude-ops" / "roles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text("---\nname: x\ndescription: d\n---\nb\n", encoding="utf-8")
    os.chmod(d, 0o000)
    try:
        roles_list, errors = R.list_roles_report(cwd)
        assert isinstance(roles_list, list)
        assert any(e.get("scope") == "project" for e in errors)
    finally:
        os.chmod(d, 0o755)


def test_reprobe_i1j_on_disk_invalid_enum_now_rejected_at_parse_not_compiled(isolated_dirs):
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "bad.md",
           "---\nname: bad\ndescription: d\neffort: banana\npermissionMode: yolo\n"
           "memory: nonsense\nmaxTurns: -5\n---\nbody\n")
    compiled = R.compile_agents(R.load_roles(cwd))
    assert "bad" not in compiled, "invalid-enum file must be rejected at parse time, not compiled"
    roles_list, errors = R.list_roles_report(cwd)
    assert any("bad" in str(e.get("path", "")) for e in errors)
