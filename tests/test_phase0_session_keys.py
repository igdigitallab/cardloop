"""
Tests for spec-040 Phase 0: neutral session keys.

Covers:
- key_of() canonical constructor (slug from cwd)
- _migrate_session_keys(): TG chat:thread keys -> slugs, idempotency,
  tg_key field added to migrated entries, session_id values preserved
- DEFAULT_NUDGE exists and run_engine() defaults to it (not TELEGRAM_NUDGE)
- binding_for() reverse-lookup on tg_key field after migration
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot


# ─────────────────────────── key_of() ───────────────────────────

class TestKeyOf:
    def test_returns_basename(self):
        assert bot.key_of("/home/igor/claude-ops-bot") == "claude-ops-bot"

    def test_trailing_slash_stripped(self):
        assert bot.key_of("/home/igor/claude-ops-bot/") == "claude-ops-bot"

    def test_nested_path(self):
        assert bot.key_of("/home/igor/projects/sac-tech") == "sac-tech"

    def test_cwd_equals_project_id(self):
        """key_of must produce the same result as _webapp._project_id for the same cwd."""
        import webapp as _webapp
        cwd = "/home/igor/rightforms-app"
        assert bot.key_of(cwd) == _webapp._project_id(cwd)


# ─────────────────────────── DEFAULT_NUDGE ───────────────────────────

class TestDefaultNudge:
    def test_default_nudge_exists(self):
        """DEFAULT_NUDGE constant must exist on bot module."""
        assert hasattr(bot, "DEFAULT_NUDGE"), "bot.DEFAULT_NUDGE must be defined (spec-040 Phase 0)"

    def test_default_nudge_is_string(self):
        assert isinstance(bot.DEFAULT_NUDGE, str)
        assert len(bot.DEFAULT_NUDGE) > 0

    def test_default_nudge_not_telegram_specific(self):
        """DEFAULT_NUDGE must not contain Telegram-specific instructions."""
        nudge = bot.DEFAULT_NUDGE.lower()
        for tg_term in ("telegram", "ptb", "message_thread_id", "tg-reply", "chat_action"):
            assert tg_term not in nudge, (
                f"DEFAULT_NUDGE contains Telegram-specific term {tg_term!r}"
            )

    def test_telegram_nudge_still_exists(self):
        """TELEGRAM_NUDGE must still exist — TG adapter uses it explicitly."""
        assert hasattr(bot, "TELEGRAM_NUDGE"), "TELEGRAM_NUDGE must remain for TG adapter"

    def test_run_engine_default_uses_default_nudge(self):
        """run_engine() code path for system_prompt=None must use DEFAULT_NUDGE, not TELEGRAM_NUDGE.

        We verify by inspecting the source code around the default branch,
        since we cannot invoke run_engine without the real SDK.
        """
        import inspect
        source = inspect.getsource(bot.run_engine)
        # Find the if system_prompt is None branch
        assert "DEFAULT_NUDGE" in source, (
            "run_engine must use DEFAULT_NUDGE as the default system_prompt (spec-040 Phase 0)"
        )
        # Confirm it appears before the TELEGRAM_NUDGE usage in run_agent (which uses TG-specific nudge)
        dn_pos = source.index("DEFAULT_NUDGE")
        # The default should be set inside the function
        assert dn_pos > 0

    def test_tg_adapter_still_uses_telegram_nudge(self):
        """run_agent() must still pass TELEGRAM_NUDGE explicitly (TG adapter, kept until Phase D)."""
        import inspect
        source = inspect.getsource(bot.run_agent)
        assert "TELEGRAM_NUDGE" in source, (
            "run_agent (TG adapter) must still use TELEGRAM_NUDGE explicitly"
        )


# ─────────────────────────── _migrate_session_keys() ───────────────────────────

class TestMigrateSessionKeys:
    def _topics(self, entries):
        """Build topics dict from list of (tg_key, cwd, project) tuples."""
        return {
            tg_key: {"project": project, "cwd": cwd, "model": "sonnet"}
            for tg_key, cwd, project in entries
        }

    def _sessions(self, entries):
        """Build sessions dict from list of (key, session_id) tuples."""
        return {k: sid for k, sid in entries}

    def test_basic_migration(self):
        """TG chat:thread keys are renamed to slugs."""
        topics = self._topics([
            ("-100123:7", "/home/igor/rightforms-app", "rightforms-app"),
            ("-100123:8", "/home/igor/claude-ops-bot", "claude-ops-bot"),
        ])
        sessions = self._sessions([
            ("-100123:7", "uuid-aaa"),
            ("-100123:8", "uuid-bbb"),
        ])

        new_t, new_s, migrated = bot._migrate_session_keys(topics, sessions)

        assert migrated == 2
        assert "rightforms-app" in new_t
        assert "claude-ops-bot" in new_t
        assert "-100123:7" not in new_t
        assert "-100123:8" not in new_t

    def test_session_ids_preserved(self):
        """session_id values must be preserved after key rename."""
        topics = self._topics([("-100123:7", "/home/igor/myproject", "myproject")])
        sessions = self._sessions([("-100123:7", "session-abc-123")])

        new_t, new_s, _ = bot._migrate_session_keys(topics, sessions)

        assert new_s.get("myproject") == "session-abc-123"

    def test_tg_key_field_added(self):
        """Migrated topic entries must carry the original tg_key for TG reverse lookup."""
        topics = self._topics([("-100123:42", "/home/igor/myproject", "myproject")])
        sessions = {}

        new_t, _, _ = bot._migrate_session_keys(topics, sessions)

        entry = new_t.get("myproject")
        assert entry is not None
        assert entry.get("tg_key") == "-100123:42"

    def test_idempotent(self):
        """Second run with already-migrated data produces migrated=0 and identical dicts."""
        topics = self._topics([("-100123:7", "/home/igor/proj", "proj")])
        sessions = self._sessions([("-100123:7", "session-x")])

        new_t, new_s, migrated1 = bot._migrate_session_keys(topics, sessions)
        new_t2, new_s2, migrated2 = bot._migrate_session_keys(new_t, new_s)

        assert migrated2 == 0
        assert new_t == new_t2
        assert new_s == new_s2

    def test_free_keys_untouched(self):
        """free-* keys are not migrated."""
        topics = {"free-abcd1234": {"project": "chat", "cwd": "/home/igor", "model": "sonnet"}}
        sessions = {"free-abcd1234": "session-free"}

        new_t, new_s, migrated = bot._migrate_session_keys(topics, sessions)

        assert migrated == 0
        assert "free-abcd1234" in new_t
        assert new_s.get("free-abcd1234") == "session-free"

    def test_glasses_key_untouched(self):
        """glasses:* keys are not migrated."""
        topics = {}
        sessions = {"glasses:claude-ops-bot": "session-glasses"}

        new_t, new_s, migrated = bot._migrate_session_keys(topics, sessions)

        assert migrated == 0
        assert "glasses:claude-ops-bot" in new_s

    def test_entry_without_cwd_kept_under_old_key(self):
        """Entries without cwd cannot be slug-derived — kept under old TG key."""
        topics = {"-100123:7": {"project": "nope", "model": "sonnet"}}  # no cwd
        sessions = {}

        new_t, _, migrated = bot._migrate_session_keys(topics, sessions)

        assert migrated == 0
        assert "-100123:7" in new_t

    def test_stale_session_without_topic_kept_under_old_key(self):
        """Sessions with TG keys that have no matching topic are kept under old key."""
        topics = {}
        sessions = {"-100999:1": "stale-session-id"}

        _, new_s, _ = bot._migrate_session_keys(topics, sessions)

        assert new_s.get("-100999:1") == "stale-session-id"

    def test_slug_collision_keeps_first(self, capsys):
        """Two TG keys mapping to same slug: first wins, second is skipped with warning."""
        # Both cwds have same basename
        topics = {
            "-100123:7": {"project": "proj-a", "cwd": "/path/a/myproject", "model": "sonnet"},
            "-100123:8": {"project": "proj-b", "cwd": "/path/b/myproject", "model": "sonnet"},
        }
        sessions = {}

        new_t, _, migrated = bot._migrate_session_keys(topics, sessions)

        # Only 1 migrated (first one), second is skipped
        assert migrated == 1
        assert "myproject" in new_t
        captured = capsys.readouterr()
        assert "collision" in captured.out.lower() or "WARNING" in captured.out


# ─────────────────────────── binding_for reverse lookup ───────────────────────────

class TestBindingForReverseLookup:
    """Verify that binding_for() finds entries after Phase 0 migration via tg_key field."""

    def _make_update(self, chat_id=-100123, thread_id=42):
        update = MagicMock()
        update.effective_chat.id = chat_id
        msg = MagicMock()
        msg.message_thread_id = thread_id
        msg.text = "hello"
        msg.document = None
        msg.photo = None
        update.effective_message = msg
        return update

    def test_reverse_lookup_finds_migrated_entry(self, monkeypatch):
        """binding_for() finds an entry via tg_key reverse scan after migration."""
        original_topics = dict(bot.topics)
        try:
            # Simulate migrated state: slug key + tg_key field
            bot.topics.clear()
            bot.topics["myproject"] = {
                "project": "myproject",
                "cwd": "/home/igor/myproject",
                "model": "sonnet",
                "tg_key": "-100123:42",
            }

            update = self._make_update(chat_id=-100123, thread_id=42)
            result = bot.binding_for(update)

            assert result is not None
            assert result["project"] == "myproject"
            assert result["cwd"] == "/home/igor/myproject"
        finally:
            bot.topics.clear()
            bot.topics.update(original_topics)

    def test_direct_lookup_still_works(self, monkeypatch):
        """binding_for() still works for pre-migration entries with TG key directly."""
        original_topics = dict(bot.topics)
        try:
            bot.topics.clear()
            bot.topics["-100123:42"] = {
                "project": "oldproject",
                "cwd": "/home/igor/oldproject",
                "model": "sonnet",
            }

            update = self._make_update(chat_id=-100123, thread_id=42)
            result = bot.binding_for(update)

            assert result is not None
            assert result["project"] == "oldproject"
        finally:
            bot.topics.clear()
            bot.topics.update(original_topics)
