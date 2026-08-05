"""
Regression: an Agent tool call made with run_in_background=False can finish and deliver its
FULL result inline, in the SAME PostToolUse event — the tool_response's own `status` is already
terminal ('completed'/'failed'/...), not 'async_launched'. No <task-notification> is ever
emitted for it afterwards (there is nothing left to defer): the SDK only emits that message for
tasks that are still outstanding when a turn's result frame closes (see
claude_agent_sdk.types.TaskNotificationMessage's own docstring: "not every terminal task emits
this message").

engine._monitor_delta used to hardcode status="running" for every Agent PostToolUse event,
ignoring the tool_response's own status. That left same-turn (inline) completions registered as
"running" forever — RC#3's transcript scan (webapp._scan_transcript_for_notifications) never
finds a <task-notification> for them because the SDK never wrote one, so the ONLY thing that
could ever move them was the sweeper's 900s staleness fallback, which reports the wrong terminal
status ("stopped"/stale) long after the agent actually finished successfully.

Live incident (2026-08-05, session 'even-g2', parent transcript
cb50b07b-2b4b-460b-85b5-98541af6b12d.jsonl): three Agent calls — "Deep dive g2flash firmware"
(agentId a979720de343f0c4d), "Deep dive BLE stack repos" (a21c5d0a5959121b8), "Agent bridge +
community context" (ab07e4c0c7b06575a) — were all launched with run_in_background=False and
returned a fully completed toolUseResult (status="completed", usage stats, final report text)
inline, in the same turn. None of the three ever produced a <task-notification> block anywhere
in the 639 KB parent transcript (confirmed: the literal substring "task-notification" occurs
exactly twice in that file, both for an unrelated fourth agent that WAS later resumed via
SendMessage and re-notified on its next stop). All three monitors sat "running" with zero tail
updates for exactly ~900s before the sweeper's staleness fallback flipped them to "stopped".

_AGENT_TOOL_RESULT below is that real toolUseResult dict (agentId a979720de343f0c4d), captured
from the live transcript with the large report body replaced by a placeholder — every field the
code path reads (status, agentId, resolvedModel, description absence) is untouched.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import engine
import webapp as _webapp


# Real tool_response captured from ~/.claude/projects/-home-igor-even-g2/
# cb50b07b-2b4b-460b-85b5-98541af6b12d.jsonl (line 29, agentId a979720de343f0c4d) — the report
# body is replaced with a placeholder, every other field is verbatim.
_AGENT_TOOL_RESULT = {
    "status": "completed",
    "prompt": "Изучи локально склонированные репозитории...",
    "agentId": "a979720de343f0c4d",
    "agentType": "researcher",
    "content": [{"type": "text", "text": "(real report text, trimmed for the fixture)"}],
    "resolvedModel": "claude-sonnet-5",
    "totalDurationMs": 308430,
    "totalTokens": 94563,
    "totalToolUseCount": 27,
}

# The tool_input for that same Agent call (description drives the monitor label).
_AGENT_TOOL_INPUT = {
    "description": "Deep dive g2flash firmware",
    "subagent_type": "researcher",
    "model": "sonnet",
    "name": "g2-firmware",
    "run_in_background": False,
}


@pytest.fixture(autouse=True)
def _clean_state():
    _webapp._monitors.clear()
    _webapp._monitors_dismissed.clear()
    yield
    _webapp._monitors.clear()
    _webapp._monitors_dismissed.clear()


# ─────────────────────── _monitor_delta: status passthrough ───────────────────────


def test_agent_delta_maps_completed_status_to_done_real_payload():
    """The exact real payload that produced a zombie monitor must now register as done."""
    d = engine._monitor_delta("Agent", _AGENT_TOOL_INPUT, _AGENT_TOOL_RESULT, "orch")
    assert d is not None
    assert d["id"] == "a979720de343f0c4d"
    assert d["kind"] == "agent"
    assert d["status"] == "done", (
        "a synchronous Agent call that already reports status='completed' inline must not be "
        "registered as 'running' — there is no later <task-notification> to rescue it"
    )
    assert d["label"] == "Deep dive g2flash firmware"


def test_agent_delta_maps_failed_status_to_failed():
    """Documented SDK vocabulary (TaskUpdatedStatus): 'failed' must map to a terminal failure,
    not linger as 'running' either."""
    tr = {"status": "failed", "agentId": "aidFail1", "description": "Probe"}
    d = engine._monitor_delta("Agent", {"description": "Probe"}, tr, "orch")
    assert d is not None
    assert d["status"] == "failed"


def test_agent_delta_still_running_for_async_launched():
    """No regression: a genuinely backgrounded launch (isAsync=True, status='async_launched',
    the shape 456/545 real Agent responses across this operator's transcripts actually use)
    must still register as 'running' — only inline-completed calls should skip it."""
    tr = {"isAsync": True, "status": "async_launched", "agentId": "aidBg1",
          "description": "Long task"}
    d = engine._monitor_delta("Agent", {"description": "Long task"}, tr, "orch")
    assert d is not None
    assert d["status"] == "running"


def test_agent_delta_missing_status_still_running():
    """Defensive default: an unrecognized/missing status must fall through to 'running' —
    never spuriously mark an agent done."""
    tr = {"agentId": "aidNoStatus"}
    d = engine._monitor_delta("Agent", {"description": "x"}, tr, "orch")
    assert d is not None
    assert d["status"] == "running"


# ─────────────────────── webapp._monitor_update: end-to-end flip ──────────────────


def test_monitor_update_flips_immediately_for_inline_completion():
    """Feeding the real payload's delta into the registry must land as 'done' on FIRST sight —
    never a 'running' row that only the 900s staleness fallback would eventually clear."""
    sk = "even-g2"
    d = engine._monitor_delta("Agent", _AGENT_TOOL_INPUT, _AGENT_TOOL_RESULT, "orch")
    _webapp._monitor_update(sk, d)
    rec = _webapp._monitors[sk]["a979720de343f0c4d"]
    assert rec["status"] == "done"
    assert rec["kind"] == "agent"

    # And therefore it is excluded from the agent-activity sweeper's "still running" set —
    # the exact filter _agent_activity_sweep_loop uses to decide what to poll/stale-flip.
    still_running = [r for r in _webapp._monitors[sk].values()
                     if r.get("kind") == "agent" and r.get("status") == "running"]
    assert still_running == []


def test_monitor_update_still_running_for_async_launched_end_to_end():
    """No regression at the registry layer either: a real async launch stays 'running' and
    IS picked up by the sweeper's filter, same as before this fix."""
    sk = "even-g2-bg"
    tr = {"isAsync": True, "status": "async_launched", "agentId": "aidBg2",
          "description": "Long task"}
    d = engine._monitor_delta("Agent", {"description": "Long task"}, tr, "orch")
    _webapp._monitor_update(sk, d)
    rec = _webapp._monitors[sk]["aidBg2"]
    assert rec["status"] == "running"
    still_running = [r for r in _webapp._monitors[sk].values()
                     if r.get("kind") == "agent" and r.get("status") == "running"]
    assert len(still_running) == 1


# ─────────── proof that the OLD safety net (task-notification transcript scan) ───────────
# ─────────── could never have caught this class of completion ────────────────────────────


def test_real_transcript_excerpt_has_no_task_notification_for_inline_completions(tmp_path):
    """Ground the diagnosis in the real transcript, not just the payload: three real
    tool_use/tool_result lines (agentIds a979720de343f0c4d / a21c5d0a5959121b8 /
    ab07e4c0c7b06575a — copied verbatim in shape from cb50b07b-2b4b-460b-85b5-98541af6b12d.jsonl,
    report bodies trimmed) never contain the literal 'task-notification' substring, so
    webapp._scan_transcript_for_notifications skips every line outright (its own marker
    pre-filter) and none of the three monitors ever flip through that path — confirming the
    transcript-scan reconcile was never going to save them, no matter how long the sweeper ran."""
    import json

    def _user_tool_result_line(agent_id: str) -> str:
        return json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{agent_id}",
                 "content": [{"type": "text",
                             "text": f"agentId: {agent_id} (use SendMessage with to: "
                                     f"'{agent_id}', summary: '...' to continue this agent)"}]},
            ]},
            "toolUseResult": {"status": "completed", "agentId": agent_id,
                              "agentType": "researcher", "resolvedModel": "claude-sonnet-5",
                              "totalDurationMs": 300000, "totalTokens": 90000,
                              "content": [{"type": "text", "text": "(trimmed real report)"}]},
        }, ensure_ascii=False)

    agent_ids = ["a979720de343f0c4d", "a21c5d0a5959121b8", "ab07e4c0c7b06575a"]
    transcript = tmp_path / "cb50b07b-2b4b-460b-85b5-98541af6b12d.jsonl"
    transcript.write_text("\n".join(_user_tool_result_line(a) for a in agent_ids) + "\n")

    # Same marker pre-filter webapp._scan_transcript_for_notifications applies.
    raw = transcript.read_text()
    assert _webapp._TASK_NOTIFICATION_MARKER not in raw, (
        "sanity: the fixture must not accidentally contain a real task-notification block"
    )

    sk = "even-g2"
    for aid in agent_ids:
        _webapp._monitor_update(sk, {"id": aid, "kind": "agent", "status": "running",
                                     "label": "probe", "agent": "researcher"})

    _webapp._scan_transcript_for_notifications(sk, transcript)

    for aid in agent_ids:
        assert _webapp._monitors[sk][aid]["status"] == "running", (
            "the transcript-scan reconcile must NOT have flipped these — proving it is not "
            "the mechanism that rescues inline (run_in_background=False) completions"
        )
