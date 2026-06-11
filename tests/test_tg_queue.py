"""
Unit tests for ops:b53401 — TG message queue per topic.

Covers:
- enqueue while running (busy): message is queued, ack returned (not rejected)
- drain after result: next message starts automatically
- FIFO order preserved
- /reset clears the queue
- queue limit (TG_QUEUE_MAX)
- persistence (write/read roundtrip across restart)
- commands are NOT queued (handled immediately regardless of running state)
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot


# ─────────────────────────── helpers ───────────────────────────


def _reset_queue(tmp_path: Path = None) -> None:
    """Reset in-memory _TG_QUEUE and point TG_QUEUE_F to a temp path for isolation."""
    bot._TG_QUEUE.clear()
    if tmp_path is not None:
        bot.TG_QUEUE_F = tmp_path / "tg_queue.json"


def _restore_queue_path() -> None:
    """Restore the original TG_QUEUE_F path (DATA / tg_queue.json)."""
    bot.TG_QUEUE_F = bot.DATA / "tg_queue.json"


def _make_update(chat_id: int = 1001, thread_id: int = 42, text: str = "hello",
                 msg_id: int = 100) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    msg = MagicMock()
    msg.message_thread_id = thread_id
    msg.text = text
    msg.caption = None
    msg.document = None
    msg.photo = None
    msg.forward_origin = None
    msg.message_id = msg_id
    update.effective_message = msg
    update.effective_user.id = next(iter(bot.ALLOWED_USERS), 0)
    return update


def _session_key(chat_id: int = 1001, thread_id: int = 42) -> str:
    return f"{chat_id}:{thread_id}"


# ─────────────────────────── Basic queue helpers ───────────────────────────


def test_tg_queue_enqueue_returns_position(tmp_path):
    """_tg_queue_enqueue returns 1-indexed position when message is added."""
    _reset_queue(tmp_path)
    k = "1001:42"
    pos1 = bot._tg_queue_enqueue(k, "first", 101)
    pos2 = bot._tg_queue_enqueue(k, "second", 102)
    assert pos1 == 1
    assert pos2 == 2


def test_tg_queue_fifo_order(tmp_path):
    """Messages are dequeued in FIFO order."""
    _reset_queue(tmp_path)
    k = "1001:42"
    bot._tg_queue_enqueue(k, "msg1", 101)
    bot._tg_queue_enqueue(k, "msg2", 102)
    bot._tg_queue_enqueue(k, "msg3", 103)

    first = bot._tg_queue_pop(k)
    second = bot._tg_queue_pop(k)
    third = bot._tg_queue_pop(k)

    assert first["prompt"] == "msg1"
    assert second["prompt"] == "msg2"
    assert third["prompt"] == "msg3"
    assert bot._tg_queue_pop(k) is None  # now empty


def test_tg_queue_pop_empty_returns_none(tmp_path):
    """_tg_queue_pop on empty queue returns None without error."""
    _reset_queue(tmp_path)
    assert bot._tg_queue_pop("1001:99") is None


def test_tg_queue_len(tmp_path):
    """_tg_queue_len returns correct count."""
    _reset_queue(tmp_path)
    k = "1001:42"
    assert bot._tg_queue_len(k) == 0
    bot._tg_queue_enqueue(k, "a", 1)
    bot._tg_queue_enqueue(k, "b", 2)
    assert bot._tg_queue_len(k) == 2
    bot._tg_queue_pop(k)
    assert bot._tg_queue_len(k) == 1


# ─────────────────────────── Limit ───────────────────────────────


def test_tg_queue_limit_returns_none_when_full(tmp_path):
    """_tg_queue_enqueue returns None when queue is at TG_QUEUE_MAX."""
    _reset_queue(tmp_path)
    k = "1001:42"
    original_max = bot.TG_QUEUE_MAX
    bot.TG_QUEUE_MAX = 3
    try:
        pos1 = bot._tg_queue_enqueue(k, "a", 1)
        pos2 = bot._tg_queue_enqueue(k, "b", 2)
        pos3 = bot._tg_queue_enqueue(k, "c", 3)
        pos4 = bot._tg_queue_enqueue(k, "d", 4)  # should be rejected

        assert pos1 == 1
        assert pos2 == 2
        assert pos3 == 3
        assert pos4 is None, f"Expected None (queue full), got {pos4}"
        assert bot._tg_queue_len(k) == 3, "Queue should stay at max"
    finally:
        bot.TG_QUEUE_MAX = original_max


# ─────────────────────────── Persistence ───────────────────────────────


def test_tg_queue_persistence_roundtrip(tmp_path):
    """Queue survives a restart: enqueue → flush → reload from disk."""
    _reset_queue(tmp_path)
    k = "1001:42"
    bot._tg_queue_enqueue(k, "persisted_msg", 101)
    bot._tg_queue_enqueue(k, "persisted_msg2", 102)

    # Simulate restart: clear in-memory and reload from file
    bot._TG_QUEUE.clear()
    data = json.loads(bot.TG_QUEUE_F.read_text(encoding="utf-8"))
    bot._TG_QUEUE.update(data)

    assert bot._tg_queue_len(k) == 2
    first = bot._tg_queue_pop(k)
    assert first["prompt"] == "persisted_msg"
    assert first["msg_id"] == 101


def test_tg_queue_flush_atomic(tmp_path):
    """_tg_queue_flush uses atomic write (tmp file then rename)."""
    _reset_queue(tmp_path)
    k = "1001:1"
    bot._tg_queue_enqueue(k, "test", 1)
    # After flush, the file must exist and be valid JSON
    assert bot.TG_QUEUE_F.exists()
    data = json.loads(bot.TG_QUEUE_F.read_text())
    assert k in data
    assert data[k][0]["prompt"] == "test"


# ─────────────────────────── /reset clears queue ─────────────────────────


def test_tg_queue_clear(tmp_path):
    """/reset calls _tg_queue_clear which removes all messages for the topic."""
    _reset_queue(tmp_path)
    k = "1001:42"
    bot._tg_queue_enqueue(k, "a", 1)
    bot._tg_queue_enqueue(k, "b", 2)
    assert bot._tg_queue_len(k) == 2

    count = bot._tg_queue_clear(k)
    assert count == 2
    assert bot._tg_queue_len(k) == 0


def test_tg_queue_clear_empty(tmp_path):
    """_tg_queue_clear on empty queue returns 0 without error."""
    _reset_queue(tmp_path)
    count = bot._tg_queue_clear("1001:99")
    assert count == 0


def test_tg_queue_clear_persists(tmp_path):
    """_tg_queue_clear flushes the cleared state to disk."""
    _reset_queue(tmp_path)
    k = "1001:42"
    bot._tg_queue_enqueue(k, "a", 1)
    bot._tg_queue_clear(k)

    # File should reflect the cleared state
    data = json.loads(bot.TG_QUEUE_F.read_text())
    assert data.get(k, []) == []


# ─────────────────────────── Enqueue while running ───────────────────────


async def test_on_message_enqueues_when_busy(tmp_path):
    """on_message: when running[k] is set, the message is queued (not rejected)."""
    _reset_queue(tmp_path)
    k = _session_key()

    # Simulate a binding and running state
    bot.topics[k] = {"project": "testproj", "cwd": str(tmp_path), "model": "sonnet"}
    bot.running[k] = True  # busy

    # send() passes text as 2nd positional arg: send_message(chat, text, ...)
    sent_texts = []
    context = MagicMock()
    context.bot.send_message = AsyncMock(
        side_effect=lambda chat, text, **kw: sent_texts.append(text) or MagicMock()
    )

    update = _make_update(text="second message", msg_id=200)

    try:
        await bot.on_message(update, context)
    finally:
        bot.topics.pop(k, None)
        bot.running.pop(k, None)

    # Queue should contain the message
    assert bot._tg_queue_len(k) == 1
    queued = bot._TG_QUEUE.get(k, [])
    assert queued[0]["prompt"] == "second message"

    # ACK should mention "queued"
    assert any("queued" in str(t).lower() or "Queued" in str(t) for t in sent_texts), (
        f"Expected 'queued' ack, got: {sent_texts}"
    )


async def test_on_message_queue_full_sends_notice(tmp_path):
    """on_message: when queue is full, sends a 'queue full' notice."""
    _reset_queue(tmp_path)
    k = _session_key()
    original_max = bot.TG_QUEUE_MAX
    bot.TG_QUEUE_MAX = 2

    bot.topics[k] = {"project": "testproj", "cwd": str(tmp_path), "model": "sonnet"}
    bot.running[k] = True

    # Pre-fill the queue
    bot._tg_queue_enqueue(k, "x", 1)
    bot._tg_queue_enqueue(k, "y", 2)

    sent_texts = []
    context = MagicMock()
    context.bot.send_message = AsyncMock(
        side_effect=lambda chat, text, **kw: sent_texts.append(text) or MagicMock()
    )
    update = _make_update(text="overflow", msg_id=300)

    try:
        await bot.on_message(update, context)
    finally:
        bot.topics.pop(k, None)
        bot.running.pop(k, None)
        bot.TG_QUEUE_MAX = original_max

    # Queue should remain at max (not grow)
    assert bot._tg_queue_len(k) == 2
    assert any("full" in str(t).lower() for t in sent_texts), (
        f"Expected 'full' notice, got: {sent_texts}"
    )


# ─────────────────────────── Drain after result ──────────────────────────


async def test_drain_tg_queue_runs_next_message(tmp_path):
    """_drain_tg_queue starts the next queued message when the slot is free."""
    _reset_queue(tmp_path)
    k = _session_key()

    bot.topics[k] = {"project": "testproj", "cwd": str(tmp_path), "model": "sonnet"}
    # Slot is free (no entry in running)
    bot._tg_queue_enqueue(k, "queued prompt", 201)

    spawned_prompts = []
    update = _make_update(text="initial", msg_id=100)

    async def fake_safe_run_queued(ctx, upd, prompt):
        spawned_prompts.append(prompt)

    context = MagicMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock())
    context.bot.send_chat_action = AsyncMock()

    try:
        with patch.object(bot, "_safe_run_queued", side_effect=fake_safe_run_queued):
            with patch.object(bot.asyncio, "create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
                await bot._drain_tg_queue(context, update)
                # Allow the event loop to run the task
                await asyncio.sleep(0)
    finally:
        bot.topics.pop(k, None)
        bot.running.pop(k, None)

    assert spawned_prompts == ["queued prompt"], (
        f"Expected queued prompt to be started, got: {spawned_prompts}"
    )
    assert bot._tg_queue_len(k) == 0, "Queue should be empty after drain"


async def test_drain_tg_queue_no_op_when_empty(tmp_path):
    """_drain_tg_queue is a no-op when queue is empty."""
    _reset_queue(tmp_path)
    k = _session_key()
    bot.topics[k] = {"project": "testproj", "cwd": str(tmp_path), "model": "sonnet"}

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    update = _make_update()

    try:
        await bot._drain_tg_queue(context, update)
    finally:
        bot.topics.pop(k, None)
        bot.running.pop(k, None)

    context.bot.send_message.assert_not_called()


# ─────────────────────────── Commands are NOT queued ──────────────────────


def test_commands_not_filtered_by_on_message():
    """The MessageHandler for on_message uses ~filters.COMMAND, so command messages
    are routed to CommandHandlers — they never reach on_message at all.

    This is enforced by bot._amain() registering:
        MessageHandler(filters.TEXT | ... & ~filters.COMMAND, on_message)

    We verify the filter expression is present in the source."""
    import inspect
    src = inspect.getsource(bot._amain)
    assert "~filters.COMMAND" in src, (
        "on_message MessageHandler must use ~filters.COMMAND to exclude commands from queuing"
    )


async def test_cmd_reset_clears_queue(tmp_path):
    """/reset (cmd_reset) clears the queue for the topic."""
    _reset_queue(tmp_path)
    k = _session_key()

    bot.topics[k] = {"project": "testproj", "cwd": str(tmp_path), "model": "sonnet"}
    bot.sessions[k] = "old-session"
    bot._tg_queue_enqueue(k, "pending1", 1)
    bot._tg_queue_enqueue(k, "pending2", 2)
    assert bot._tg_queue_len(k) == 2

    sent_texts = []
    context = MagicMock()
    context.bot.send_message = AsyncMock(
        side_effect=lambda chat, text, **kw: sent_texts.append(text) or MagicMock()
    )
    update = _make_update()

    try:
        await bot.cmd_reset(update, context)
    finally:
        bot.topics.pop(k, None)
        bot.sessions.pop(k, None)

    assert bot._tg_queue_len(k) == 0, "Queue should be cleared after /reset"
    assert bot.sessions.get(k) is None, "Session should be cleared"
    # Reply should mention cleared messages
    assert any("cleared" in str(t).lower() or "queue" in str(t).lower() for t in sent_texts), (
        f"Reset reply should mention queue clear, got: {sent_texts}"
    )
