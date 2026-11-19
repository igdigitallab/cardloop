"""spec-098: which timeline/audit lines belong to a session (the review's F1, F2, F3, F9, F10).

The failure that matters most is deleting ANOTHER session's lines. These tests build the exact
shapes that used to misattribute: two chats of one project typing the same short prompt, a run that
rotated into `timeline.jsonl.1`, an audit month shared by every project, a prompt without a time.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import forget

T = 1_795_000_000.0
SK = "1001:42"
SID = "target-0000-4000-8000-000000000001"


def _prompts(*pairs):
    """[(ts, text)] -> the Held.prompts shape."""
    out = []
    for ts, text in pairs:
        c = forget.collapse(text)
        out.append((ts, frozenset({forget.digest(c), forget.digest(forget.short(c, 300))})))
    return out


def _write(path: Path, recs) -> None:
    path.write_bytes(b"".join((json.dumps(r) + "\n").encode() for r in recs))


def _kept(path: Path, digests) -> "list[dict]":
    want = set(digests)
    return [json.loads(raw) for raw in path.read_bytes().splitlines(keepends=True)
            if forget.digest(raw) not in want]


def test_same_short_prompt_in_another_chat_is_not_adopted(tmp_path):
    """F1: the target's "ok" is consumed by its own run; the other chat's "ok" a minute later is
    not the target's, even though it is within the 120 s window."""
    tl = tmp_path / "t.jsonl"
    _write(tl, [
        {"ts": T + 0.2, "session_key": SK, "kind": "run_start", "prompt": "ok", "run_id": "rT", "chat_id": "c1"},
        {"ts": T + 1, "session_key": SK, "kind": "tool_result", "run_id": "rT", "output": "target out"},
        {"ts": T + 2, "session_key": SK, "kind": "run_end", "run_id": "rT"},
        {"ts": T + 60, "session_key": SK, "kind": "run_start", "prompt": "ok", "run_id": "rX", "chat_id": "c2"},
        {"ts": T + 61, "session_key": SK, "kind": "tool_result", "run_id": "rX", "output": "OTHER out"},
        {"ts": T + 62, "session_key": SK, "kind": "run_end", "run_id": "rX"},
    ])
    known: set = set()
    sel = forget.select_timeline_lines(tl, session_id=SID, known_runs=known, prompts=_prompts((T, "ok")))
    kept = _kept(tl, sel["digests"])
    assert known == {"rT"}
    assert [r.get("output") for r in kept if r["kind"] == "tool_result"] == ["OTHER out"]
    assert len(kept) == 3


def test_a_run_whose_end_names_another_session_never_matches_by_prompt(tmp_path):
    tl = tmp_path / "t.jsonl"
    _write(tl, [
        {"ts": T + 0.2, "session_key": SK, "kind": "run_start", "prompt": "continue", "run_id": "rX"},
        {"ts": T + 1, "session_key": SK, "kind": "run_end", "run_id": "rX", "session_id": "someone-else"},
    ])
    sel = forget.select_timeline_lines(tl, session_id=SID, known_runs=set(),
                                       prompts=_prompts((T, "continue")))
    assert sel["digests"] == []


def test_a_run_that_rotated_into_dot1_still_tags_its_lines_in_the_live_file(tmp_path):
    """F2 + F3: pass 1 spans every file, and the latest run_start's verdict carries across files."""
    old, live = tmp_path / "t.jsonl.1", tmp_path / "t.jsonl"
    _write(old, [
        {"ts": T + 0.2, "session_key": SK, "kind": "run_start", "prompt": "build it", "run_id": "rA"},
        {"ts": T + 1, "session_key": SK, "kind": "run_start", "prompt": "legacy no id"},
    ])
    _write(live, [
        {"ts": T + 2, "session_key": SK, "kind": "tool_result", "output": "id-less, legacy run"},
        {"ts": T + 3, "session_key": SK, "kind": "tool_result", "run_id": "rA", "output": "stamped"},
        {"ts": T + 4, "session_key": SK, "kind": "run_end", "run_id": "rA", "session_id": SID},
    ])
    ordered = [old, live]
    shared = {"run_sessions": forget.timeline_run_sessions(ordered)}
    known: set = set()
    prompts = _prompts((T + 1, "legacy no id"))           # "build it" is NOT a prompt match: rA is known by id
    sels = [forget.select_timeline_lines(p, session_id=SID, known_runs=known, prompts=prompts, shared=shared)
            for p in ordered]
    assert "rA" in known
    assert len(sels[0]["digests"]) == 2                  # both run_starts
    assert len(sels[1]["digests"]) == 3 and sels[1]["unattributed"] == 0


def test_audit_lines_of_another_project_are_never_touched(tmp_path):
    """F9: the month file is shared by every project; the same TASK text in project B within the
    window must not adopt B's lines."""
    af = tmp_path / "audit-2026-11.log"
    af.write_text(
        "2026-11-18T10:00:05 [A] TASK: continue\n"
        "2026-11-18T10:00:06 [A] RESULT: Bash ok target-output\n"
        "2026-11-18T10:00:30 [B] TASK: continue\n"
        "2026-11-18T10:00:31 [B] RESULT: Bash ok OTHER-PROJECT-output\n", encoding="utf-8")
    ts = forget._audit_ts("2026-11-18T10:00:05")
    sel = forget.select_audit_lines(af, known_runs=set(), prompts=_prompts((ts, "continue"), (ts + 25, "continue")),
                                    project_names={"A"})
    kept = [raw for raw in af.read_bytes().splitlines(keepends=True) if forget.digest(raw) not in set(sel["digests"])]
    assert [k.decode().split(" ", 1)[1].strip() for k in kept] == [
        "[B] TASK: continue", "[B] RESULT: Bash ok OTHER-PROJECT-output"]


def test_a_prompt_without_a_time_matches_nothing(tmp_path):
    """F10: Grok prompts carry no ts; "any time" used to adopt every run that typed the same words."""
    tl = tmp_path / "t.jsonl"
    _write(tl, [{"ts": T - 86_400 * 30, "session_key": SK, "kind": "run_start", "prompt": "yes", "run_id": "rOld"}])
    sel = forget.select_timeline_lines(tl, session_id=SID, known_runs=set(), prompts=[(None, _prompts((0, "yes"))[0][1])])
    assert sel["digests"] == []
    af = tmp_path / "audit-2026-10.log"
    af.write_text("2026-10-01T09:00:00 [A] TASK: yes\n", encoding="utf-8")
    assert forget.select_audit_lines(af, known_runs=set(), prompts=[(None, _prompts((0, "yes"))[0][1])],
                                     project_names={"A"})["digests"] == []


# ── Claude history.jsonl (the review's F4, F5) ─────────────────────────────────────────────────
import forget_claude  # noqa: E402


def test_history_line_that_only_quotes_the_id_does_not_block_verify(tmp_path):
    """F4: another session's prompt "claude --resume <sid>" must neither be deleted nor make
    verify report the session as still present (that left the purge stuck half-done)."""
    h = tmp_path / "history.jsonl"
    other = {"display": f"claude --resume {SID}", "sessionId": "another-session"}
    h.write_text(json.dumps({"display": "mine", "sessionId": SID}) + "\n" + json.dumps(other) + "\n")
    A = forget_claude.ClaudeAdapter
    assert A._history_has(h, SID) is True
    assert A._rewrite_history(h, SID) == 1
    assert A._history_has(h, SID) is False
    assert [json.loads(x) for x in h.read_text().splitlines()] == [other]


def test_a_line_appended_during_the_rewrite_survives(tmp_path, monkeypatch):
    """F5: an interactive `claude` appending between our read and the rename keeps its line."""
    h = tmp_path / "history.jsonl"
    h.write_text(json.dumps({"display": "mine", "sessionId": SID}) + "\n")
    late = {"display": "typed meanwhile", "sessionId": "busy-elsewhere"}
    real_replace = forget_claude.os.replace

    def racing_replace(src, dst):
        with open(dst, "a") as f:                       # the other CLI writes to the OLD inode
            f.write(json.dumps(late) + "\n")
        real_replace(src, dst)

    monkeypatch.setattr(forget_claude.os, "replace", racing_replace)
    assert forget_claude.ClaudeAdapter._rewrite_history(h, SID) == 1
    assert [json.loads(x) for x in h.read_text().splitlines()] == [late]


# ── audit writers outside the cockpit process (the review's F11) ───────────────────────────────
def test_grok_hook_append_waiting_on_a_rewrite_lands_in_the_new_file(tmp_path):
    """The Grok guard hook is a separate process: it cannot take engine.AUDIT_LOCK. It takes flock
    on the month file and, if the purge replaced the file meanwhile, appends to the new one."""
    import fcntl
    import threading
    import time as _t
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import grok_guard_hook

    month = tmp_path / f"audit-{_t.strftime('%Y-%m')}.log"
    month.write_text("2026-11-18T10:00:00 [A] TASK: doomed\n", encoding="utf-8")
    old = open(month, "rb")
    fcntl.flock(old.fileno(), fcntl.LOCK_EX)              # the purge holds the old inode
    t = threading.Thread(target=grok_guard_hook._audit, args=(str(tmp_path), "/work/b", "rule", "rm -rf x"))
    t.start()
    _t.sleep(0.3)
    tmp = tmp_path / "new"
    tmp.write_text("", encoding="utf-8")
    import os as _os
    _os.replace(tmp, month)                               # the rewrite lands
    fcntl.flock(old.fileno(), fcntl.LOCK_UN)
    old.close()
    t.join(5)
    assert "DENY: rule: rm -rf x (grok)" in month.read_text(encoding="utf-8")


# ── residue scan honesty (the review's G6) ─────────────────────────────────────────────────────
async def test_residue_scan_with_nothing_to_look_for_is_not_clean(tmp_path):
    res = await forget.residue_scan(tmp_path, [], [tmp_path])
    assert res["clean"] is False and res["checked"] is False and res["hits"] == []


def test_fts_phrases_cover_cyrillic_text():
    w = ["ия первое второе третье четвёртое пятое шестое"]
    assert forget._fts_phrases(w) == ["первое второе третье четвёртое"]
