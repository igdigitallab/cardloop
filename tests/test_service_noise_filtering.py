"""
Board card 822e08: "Service noise floods the chat feed".

Two kinds of cockpit-internal noise used to reach the chat feed as if the operator (or the
model) had written them:

  1. A synthetic auto-continue wake-up (`_BG_CONTINUE_PREFIX = "[auto-continue]"`, generated
     by `_completion_wake_fire`) drained through the ordinary chat queue lane and rendered as
     a full operator bubble.
  2. A raw `<task-notification>...</task-notification>` block (the SDK's own background-task
     report) landing inside a user-turn string and rendering as a giant XML dump.

Both are fixed at the single helper `_display_prompt` (and its run_start-event wrapper
`_clean_run_start_event`), reused at every boundary that turns a raw prompt/message string
into a displayed chat message: `_session_history` (session-history hydration / reload),
`_live_turn_create`/`_live_turn_append` (the GET /live snapshot + reconnect replay buffer),
and `_bus_publish` (the live SSE fan-out + timeline persistence).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


# ─────────────────────────── _display_prompt (unit) ───────────────────────────


def test_display_prompt_drops_auto_continue_prefix_whole():
    raw = (f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: "
           '"seo research" → done. Collect their results now.')
    assert _webapp._display_prompt(raw) == ""


def test_display_prompt_strips_task_notification_keeps_human_text():
    raw = ("please double-check the deploy\n\n"
           "<task-notification><task-id>xyz</task-id><result>done</result></task-notification>")
    assert _webapp._display_prompt(raw) == "please double-check the deploy"


def test_display_prompt_keeps_message_mentioning_auto_continue_mid_sentence():
    """The prefix check is a startswith, not a substring search — an operator asking about
    the feature by name must never be treated as harness noise."""
    raw = "can you explain how auto-continue decides when to wake the model back up?"
    assert _webapp._display_prompt(raw) == raw


def test_display_prompt_ordinary_text_untouched():
    assert _webapp._display_prompt("what's the deploy status?") == "what's the deploy status?"


def test_display_prompt_empty_and_none_safe():
    assert _webapp._display_prompt("") == ""
    assert _webapp._display_prompt(None) == ""


# ─────────────────────────── _session_history (a, b, c, d) ───────────────────────────


def test_session_history_drops_auto_continue_message(tmp_path):
    """(a) A queued auto-continue wake-up must not reach the feed at all — surrounding real
    turns survive."""
    jsonl = tmp_path / "auto-continue.jsonl"
    auto_text = (f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: "
                 '"seo research" → done. Collect their results now.')
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"role": "user", "content": "run the seo research task"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "launched it in the background"}]}},
        {"type": "user", "message": {"role": "user", "content": auto_text}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "collected the results, all green"}]}},
    ]) + "\n", encoding="utf-8")

    texts = [m["text"] for m in _webapp._session_history(jsonl)]
    assert texts == [
        "run the seo research task",
        "launched it in the background",
        "collected the results, all green",
    ], "the synthetic wake-up bubble must be dropped, everything else kept in order"


def test_session_history_strips_task_notification_block(tmp_path):
    """(b) A <task-notification> block embedded in a user turn is cut; the human text around
    it survives."""
    jsonl = tmp_path / "task-notification.jsonl"
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"role": "user", "content": (
            "one more thing before you continue\n\n"
            "<task-notification><task-id>abc</task-id>"
            "<result>Ran the full test suite, all green.</result></task-notification>"
        )}},
    ]) + "\n", encoding="utf-8")

    msgs = _webapp._session_history(jsonl)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "one more thing before you continue"
    assert "task-notification" not in msgs[0]["text"]


def test_session_history_keeps_operator_message_mentioning_auto_continue(tmp_path):
    """(c) A human message that merely contains the words "auto-continue" NOT at the start
    must stay visible — the filter is a prefix check, not a keyword ban."""
    jsonl = tmp_path / "mentions-word.jsonl"
    human_text = "why did auto-continue fire twice on that session?"
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"role": "user", "content": human_text}},
    ]) + "\n", encoding="utf-8")

    texts = [m["text"] for m in _webapp._session_history(jsonl)]
    assert texts == [human_text]


def test_session_history_assistant_replies_are_never_filtered(tmp_path):
    """(d) The noise filters apply only to user-role content — an assistant reply that quotes
    a task-notification-shaped string, or starts with the literal auto-continue prefix (e.g.
    echoing it back to explain the mechanism), must render verbatim."""
    jsonl = tmp_path / "assistant-untouched.jsonl"
    quoting_reply = (
        f"{_webapp._BG_CONTINUE_PREFIX} is the literal prefix the cockpit uses; "
        "it also strips <task-notification> blocks from the feed."
    )
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": quoting_reply}]}},
    ]) + "\n", encoding="utf-8")

    texts = [m["text"] for m in _webapp._session_history(jsonl)]
    assert texts == [quoting_reply], "assistant text must never be run through the noise filter"


def test_session_history_drops_auto_continue_steered_attachment(tmp_path):
    """Defense in depth: the queued_command/steered attachment branch (spec-086) routes
    through the same _display_prompt helper, so an auto-continue wake or a raw
    task-notification block delivered mid-turn is filtered exactly like an ordinary user line."""
    jsonl = tmp_path / "steered-noise.jsonl"
    auto_text = f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: x → done."
    jsonl.write_text("\n".join(json.dumps(o) for o in [
        {"type": "user", "message": {"role": "user", "content": "count to twenty"}},
        {"type": "attachment", "uuid": "att-1",
         "attachment": {"type": "queued_command", "prompt": auto_text}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "stopped at 9"}]}},
    ]) + "\n", encoding="utf-8")

    texts = [m["text"] for m in _webapp._session_history(jsonl)]
    assert texts == ["count to twenty", "stopped at 9"], \
        "the steered auto-continue wake must not render as its own bubble"


# ─────────────────── live-turn buffer (GET /live + reconnect replay) ───────────────────


def _cleanup_live_turn(session_key):
    _webapp._live_turns.pop(session_key, None)
    _webapp._live_seq.pop(session_key, None)


def test_live_turn_create_drops_auto_continue_prompt():
    session_key = "1001:noise-live-create"
    _cleanup_live_turn(session_key)
    raw = f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: x → done."
    try:
        turn = _webapp._live_turn_create(session_key, "sonnet", raw)
        assert turn["prompt"] == "", f"the top-level GET /live prompt field must be empty: {turn}"
    finally:
        _cleanup_live_turn(session_key)


def test_live_turn_append_stores_a_clean_run_start_event():
    """The event object STORED in the ring buffer (served by GET /live's "events" and the
    ?since= reconnect replay) must already be clean — not just the copy _bus_publish later
    fans out over SSE, which is a separate object once _clean_run_start_event runs."""
    session_key = "1001:noise-live-append"
    _cleanup_live_turn(session_key)
    try:
        _webapp._live_turn_create(session_key, "sonnet", "")
        raw = f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: x → done."
        tagged = _webapp._live_turn_append(session_key, {
            "kind": "run_start", "source": "chat", "prompt": raw, "run_id": "r1",
        })
        assert tagged["prompt"] == ""
        assert tagged.get("prompt_service_only") is True
        stored = list(_webapp._live_turns[session_key]["events"])
        assert len(stored) == 1
        assert stored[0]["prompt"] == "", f"raw auto-continue prompt leaked into the ring buffer: {stored[0]}"
    finally:
        _cleanup_live_turn(session_key)


def test_bus_publish_drops_auto_continue_prefix_prompt():
    """Same class of fix at the bus-publish boundary, mirroring the existing
    task-notification coverage in test_spec035_live_trace.py."""
    session_key = "1001:noise-bus"
    q = _webapp._bus_subscribe(session_key)
    try:
        raw = f"{_webapp._BG_CONTINUE_PREFIX} Background task(s) just finished: x → done."
        _webapp._bus_publish(session_key, {
            "kind": "run_start", "source": "chat", "prompt": raw, "run_id": "r1",
        }, persist=False)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        _webapp._bus_unsubscribe(session_key, q)

    assert len(events) == 1
    assert events[0]["prompt"] == ""
    assert events[0].get("prompt_service_only") is True
