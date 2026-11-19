"""spec-098: the Claude adapter on the measured on-disk shapes."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import forget
import forget_claude
from _forget_world import K_SID, PID, SENT, T_SID, TEXT_PROMPT, World, j


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def adapter():
    return forget_claude.ClaudeAdapter()


def test_it_is_registered_with_the_right_capabilities():
    a = forget.adapter("claude")
    assert a is not None and a.name == "claude" and a.can_delete_session and a.can_forget_turn


def test_locate_finds_every_artifact_and_skips_unknown_ids(world, adapter):
    ctx = world.ctx()
    art = adapter.locate(ctx, world.project(), T_SID)
    h = world.home
    got = {str(p) for p in art.paths}
    assert str(world.proj_dir / f"{T_SID}.jsonl") in got and str(world.proj_dir / T_SID) in got
    for cfg in (h / ".claude", h / ".claude-accounts" / "work"):
        assert str(cfg / "file-history" / T_SID) in got and str(cfg / "session-env" / T_SID) in got
    assert str(h / ".claude" / "debug" / f"{T_SID}.txt") in got
    assert K_SID not in " ".join(got)
    assert {Path(p).name for p in art.extra["history_files"]} == {"history.jsonl"} and len(art.extra["history_files"]) == 2
    assert art.title == "target label" and art.first_ts and art.last_ts >= art.first_ts
    assert art.extra["prompts"] == 1 and art.extra["subagents"] == 1
    assert SENT not in repr(art.extra), "extra goes into the pending record: no conversation text"
    assert adapter.locate(ctx, world.project(), "33333333-cccc-4ccc-8ccc-333333333333") is None
    assert adapter.locate(ctx, world.project(), "../../etc") is None
    assert adapter.locate(ctx, {"cwd": "/nowhere"}, T_SID) is None


def test_an_account_whose_file_history_is_a_symlink_is_not_listed_twice(world, adapter):
    work = world.home / ".claude-accounts" / "work" / "file-history"
    import shutil
    shutil.rmtree(work)
    work.symlink_to(world.home / ".claude" / "file-history")
    art = adapter.locate(world.ctx(), world.project(), T_SID)
    assert sum(1 for p in art.paths if p.parts[-2] == "file-history") == 1


def test_read_content_streams_prompts_replies_tools_subagents_and_tool_results(world, adapter):
    art = adapter.locate(world.ctx(), world.project(), T_SID)
    gen = adapter.read_content(art)
    assert iter(gen) is gen                                   # a generator, never a list
    items = list(gen)
    kinds = {i.kind for i in items}
    assert {"prompt", "assistant", "tool_use", "tool_result", "summary"} <= kinds
    texts = " ".join(i.text for i in items)
    assert "subagent found" in texts and "big output with" in texts and "hook said" in texts
    prompts = [i for i in items if i.kind == "prompt"]
    assert any(p.uuid is None for p in prompts), "queue-operation prompts carry no uuid"
    assert [p for p in prompts if p.uuid] and all(p.ts for p in prompts if p.uuid)
    tu = [i for i in items if i.kind == "tool_use" and i.tool == "Bash"][0]
    assert tu.tool_input == {"command": "git commit -m wip"}


def test_an_oversized_line_is_skipped_not_parsed(world, adapter, monkeypatch):
    monkeypatch.setattr(forget_claude, "_MAX_LINE", 200)
    big = j({"type": "assistant", "uuid": "big", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "x" * 500}]}})
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as f:
        f.write(big)
    art = adapter.locate(world.ctx(), world.project(), T_SID)
    assert not any(i.uuid == "big" for i in adapter.read_content(art))


def test_delete_removes_everything_and_is_idempotent(world, adapter):
    ctx = world.ctx()
    art = adapter.locate(ctx, world.project(), T_SID)
    res = adapter.delete(ctx, art)
    assert res["files"] == 7 and res["history_lines"] == 2
    assert adapter.verify_gone(ctx, art) == []
    again = adapter.delete(ctx, art)
    assert again["files"] == 0 and again["history_lines"] == 0
    # the kept session and its stores are untouched
    assert (world.proj_dir / f"{K_SID}.jsonl").exists()
    assert (world.home / ".claude" / "file-history" / K_SID / "abc@v1").exists()


def test_delete_does_not_follow_a_symlink_out_of_the_session_dir(world, adapter):
    outside = world.tmp / "outside.txt"
    outside.write_text("precious")
    (world.proj_dir / T_SID / "tool-results" / "link").symlink_to(outside)
    outdir = world.tmp / "outdir"
    outdir.mkdir()
    (outdir / "f").write_text("precious")
    (world.proj_dir / T_SID / "dirlink").symlink_to(outdir)
    ctx = world.ctx()
    art = adapter.locate(ctx, world.project(), T_SID)
    adapter.delete(ctx, art)
    assert outside.read_text() == "precious" and (outdir / "f").read_text() == "precious"


def test_a_session_path_that_is_itself_a_symlink_is_unlinked_not_followed(world, adapter):
    real = world.tmp / "real-env"
    real.mkdir()
    (real / "keep").write_text("keep")
    env_dir = world.home / ".claude" / "session-env" / T_SID
    env_dir.rmdir()
    env_dir.symlink_to(real)
    ctx = world.ctx()
    art = adapter.locate(ctx, world.project(), T_SID)
    adapter.delete(ctx, art)
    assert not env_dir.exists() and (real / "keep").read_text() == "keep"


def test_history_rewrite_keeps_other_lines_byte_for_byte(world, adapter):
    h = world.home / ".claude" / "history.jsonl"
    weird = b'{"display": "no session"}\nnot json at all\n'
    h.write_bytes(h.read_bytes() + weird)
    k_line = [ln for ln in h.read_bytes().splitlines(keepends=True) if K_SID.encode() in ln][0]
    assert adapter._rewrite_history(h, T_SID) == 1
    assert h.read_bytes() == k_line + weird


def test_history_rewrite_retries_when_another_cli_appends_meanwhile(world, adapter, monkeypatch):
    h = world.home / ".claude" / "history.jsonl"
    real_replace = os.replace
    calls = {"n": 0}

    def racing_replace(src, dst):
        calls["n"] += 1
        real_replace(src, dst)
    appended = {"done": False}
    real_stat = Path.stat

    def stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == h and not appended["done"] and calls["n"] == 0:
            appended["done"] = True
            with open(h, "ab") as f:                                  # a concurrent CLI append
                f.write(j({"display": "late", "sessionId": K_SID}))
        return st
    monkeypatch.setattr(os, "replace", racing_replace)
    monkeypatch.setattr(Path, "stat", stat)
    assert adapter._rewrite_history(h, T_SID) == 1
    monkeypatch.undo()
    assert b"late" in h.read_bytes() and T_SID.encode() not in h.read_bytes()


def test_busy_reason_is_none_when_nobody_holds_the_session(world, adapter):
    assert adapter.busy_reason(world.ctx(), world.project(), T_SID) is None


def test_fingerprint_moves_with_prompts_not_with_housekeeping_lines(world, adapter):
    ctx = world.ctx()
    fp = adapter.fingerprint(adapter.locate(ctx, world.project(), T_SID))
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as f:
        f.write(j({"type": "ai-title", "aiTitle": "again"}))
    assert adapter.fingerprint(adapter.locate(ctx, world.project(), T_SID)) == fp
    with open(world.proj_dir / f"{T_SID}.jsonl", "ab") as f:
        f.write(j({"type": "user", "uuid": "u9", "timestamp": "2026-11-18T10:00:00.000Z",
                   "message": {"role": "user", "content": "hi"}}))
    assert adapter.fingerprint(adapter.locate(ctx, world.project(), T_SID)) != fp


def test_residue_roots_and_sibling_roots(world, adapter):
    roots = adapter.residue_roots(world.ctx())
    assert world.home / ".claude" / "file-history" in roots
    art = adapter.locate(world.ctx(), world.project(), T_SID)
    assert adapter.sibling_roots(world.ctx(), art) == [world.proj_dir]


def test_the_prompt_text_is_what_the_attribution_matches(world, adapter):
    held = forget.collect_held(adapter.read_content(adapter.locate(world.ctx(), world.project(), T_SID)), seed="s")
    key = forget.digest(forget.collapse(TEXT_PROMPT))
    assert any(key in keys for _ts, keys in held.prompts)
    assert (PID, "shot-1.png") in held.media and (PID, "shared.png") in held.media


def test_busy_reason_sees_a_claude_cli_on_the_session_not_a_shell_that_names_it(world, adapter, tmp_path):
    """A process counts only when its PROGRAM is claude and the id is one of its arguments. The old
    substring test refused live deletes because of the deleting agent's own Bash command line."""
    import os
    import subprocess
    import time
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    os.symlink("/bin/sh", fake)
    shell = subprocess.Popen(["/bin/bash", "-c", f"sleep 5 # ~/.claude/projects/x/{T_SID}.jsonl"])
    try:
        time.sleep(0.2)
        assert adapter.busy_reason(world.ctx(), world.project(), T_SID) is None
    finally:
        shell.kill()
        shell.wait()
    cli = subprocess.Popen([str(fake), "-c", "sleep 5", "--resume", T_SID])
    try:
        time.sleep(0.2)
        reason = adapter.busy_reason(world.ctx(), world.project(), T_SID)
        assert reason and str(cli.pid) in reason
    finally:
        cli.kill()
        cli.wait()
