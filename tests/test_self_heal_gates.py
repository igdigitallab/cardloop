"""
Тесты spec-012 Ф2 — Self-Heal Safety Layer (gates B–E).

Покрывают НОВЫЕ предохранители, добавленные поверх spec-010:
B. Дебаунс (seen >= _HEAL_MIN_SEEN)
C. Benign / ignore-list (heal_skip метка + round-trip)
D. Per-project rate-limit (_heal_rate_ok / _heal_record)
E. Порядок gates (_heal_decision чистая функция)

spec-010 предохранители проверяются в test_self_healing.py (регрессия-страж).
Здесь — только новые ворота.
"""
import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp
from webapp import (
    _heal_rate_ok,
    _heal_record,
    _heal_history,
    _HEAL_MIN_SEEN,
    _HEAL_MAX_PER_WINDOW,
    _HEAL_WINDOW_SEC,
    _HEAL_BENIGN_DEFAULT,
    _heal_decision,
    _parse_incident_desc,
    _format_incident_desc,
    _ERR_DESC_RE,
    _is_incident_card,
    _load_board,
    _save_board,
    _tasks_path,
    _error_scanner_loop,
    _self_heal_active_count,
    _SELF_HEAL_MAX_CONCURRENT,
)


# ─────────────────────────── Helpers ───────────────────────────

def _make_project(cwd: str, self_heal: bool = True, heal_ignore=None) -> dict:
    pid = Path(cwd.rstrip("/")).name
    proj = {
        "id": pid,
        "name": pid,
        "cwd": cwd,
        "tg_thread": f"0:{pid}",
        "model": "sonnet",
        "is_free": False,
        "self_heal": self_heal,
        "log_cmd": None,
        "test_cmd": None,
        "notify_on_error": False,
    }
    if heal_ignore is not None:
        proj["heal_ignore"] = heal_ignore
    return proj


def _make_incident_card(
    card_id: str = "err-aabbcc",
    text: str = "[ERR] ValueError: something broke",
    seen: int = 2,
    excerpt: str = "ValueError: something broke\n  File app.py line 10",
) -> dict:
    meta = {
        "source": "log",
        "seen": str(seen),
        "first": "2026-06-04T10:00",
        "last": "2026-06-04T10:00",
        "excerpt": excerpt,
    }
    return {
        "id": card_id,
        "text": text,
        "description": _format_incident_desc(meta),
    }


def _write_incident_to_board(cwd: str, card: dict, column: str = "failed") -> None:
    name = Path(cwd).name
    _, preamble, cols = _load_board(cwd)
    cols[column].append(card)
    _save_board(cwd, name, preamble, cols)


def _make_ctx(data_dir: Path, cwd: str, self_heal: bool = True) -> dict:
    pid = Path(cwd.rstrip("/")).name
    return {
        "topics": {f"0:{pid}": {"cwd": cwd, "project": pid, "self_heal": self_heal}},
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }


# ─────────────────────────── C. heal_skip round-trip ───────────────────────────

class TestHealSkipMeta:
    """heal_skip=benign записывается, парсится, round-trip через _format/_parse."""

    def test_heal_skip_written_to_desc(self):
        meta = {"source": "log", "seen": "1", "heal_skip": "benign"}
        desc = _format_incident_desc(meta)
        assert "heal_skip=benign" in desc

    def test_heal_skip_parsed_from_desc(self):
        desc = "source=log\nseen=1\nheal_skip=benign\nexcerpt=err"
        meta = _parse_incident_desc(desc)
        assert meta.get("heal_skip") == "benign"

    def test_heal_skip_round_trip(self):
        """Полный round-trip: format → parse → heal_skip сохранён."""
        original = {
            "source": "log",
            "seen": "3",
            "first": "2026-06-04T10:00",
            "last": "2026-06-04T10:05",
            "heal_skip": "benign",
            "excerpt": "ConnectionResetError",
        }
        desc = _format_incident_desc(original)
        parsed = _parse_incident_desc(desc)
        assert parsed["heal_skip"] == "benign"
        assert parsed["seen"] == "3"
        assert parsed["source"] == "log"

    def test_err_desc_re_matches_heal_skip(self):
        """_ERR_DESC_RE разрешает heal_skip (добавлен явно в whitelist)."""
        m = _ERR_DESC_RE.match("heal_skip=benign")
        assert m is not None
        assert m.group(1) == "heal_skip"
        assert m.group(2) == "benign"

    def test_heal_skip_not_written_when_absent(self):
        meta = {"source": "log", "seen": "1"}
        desc = _format_incident_desc(meta)
        assert "heal_skip" not in desc

    def test_err_desc_re_matches_heal_attempted(self):
        """Старый ключ heal_attempted тоже разрешён (регрессия)."""
        m = _ERR_DESC_RE.match("heal_attempted=true")
        assert m is not None


# ─────────────────────────── B. Debounce (seen) ───────────────────────────

class TestDebounce:
    """Карточка с seen < _HEAL_MIN_SEEN не лечится."""

    def _card(self, seen: int) -> dict:
        return _make_incident_card(seen=seen)

    def _proj(self, tmp_path) -> dict:
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir(exist_ok=True)
        return _make_project(cwd)

    def test_seen_1_not_healed(self, tmp_path):
        """seen=1 → skip (too_young), даже если все остальные гейты OK."""
        card = self._card(seen=1)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "skip"
        assert "too_young" in reason

    def test_seen_at_min_heals(self, tmp_path):
        """seen == _HEAL_MIN_SEEN → heal (если прочие гейты OK)."""
        card = self._card(seen=_HEAL_MIN_SEEN)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "heal"

    def test_seen_above_min_heals(self, tmp_path):
        """seen > _HEAL_MIN_SEEN → heal."""
        card = self._card(seen=_HEAL_MIN_SEEN + 5)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "heal"

    def test_seen_missing_defaults_to_1(self, tmp_path):
        """Если seen не задан в meta — считается 1 (не лечим)."""
        meta = {"source": "log", "excerpt": "err"}  # нет seen
        card = {
            "id": "err-aabbcc",
            "text": "[ERR] ValueError",
            "description": _format_incident_desc(meta),
        }
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        # seen по умолчанию 1, что < _HEAL_MIN_SEEN (2) → skip или heal зависит от MIN_SEEN
        if _HEAL_MIN_SEEN > 1:
            assert action == "skip"
        else:
            # если MIN_SEEN=1, то seen=1 уже достаточно
            assert action == "heal"

    @pytest.mark.asyncio
    async def test_scanner_young_incident_not_healed(self, tmp_path):
        """Интеграция: сканер не запускает heal для seen=1 карточки."""
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd)

        incident = _make_incident_card(seen=1)
        _write_incident_to_board(cwd, incident, column="failed")

        create_task_calls = []

        def fake_create_task(coro):
            create_task_calls.append(coro)
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        sleep_calls = [None]

        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return
            raise StopAsyncIteration()

        with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
             patch("webapp._collect_projects") as mock_collect, \
             patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
             patch("webapp.asyncio.create_task", side_effect=fake_create_task):

            mock_scan.return_value = {"ok": True, "added": 1, "updated": 0, "scanned": 5}
            mock_collect.return_value = [{
                **_make_project(cwd),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

        # seen=1 → дебаунс → не должно быть задач
        if _HEAL_MIN_SEEN > 1:
            assert len(create_task_calls) == 0, (
                "Карточка с seen=1 не должна запускать heal (дебаунс)"
            )

    @pytest.mark.asyncio
    async def test_scanner_mature_incident_healed(self, tmp_path):
        """Интеграция: сканер запускает heal для seen>=MIN_SEEN карточки."""
        cwd = str(tmp_path / "proj2")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd)

        incident = _make_incident_card(seen=_HEAL_MIN_SEEN)
        _write_incident_to_board(cwd, incident, column="failed")

        create_task_calls = []

        def fake_create_task(coro):
            create_task_calls.append(coro)
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        sleep_calls = [None]

        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return
            raise StopAsyncIteration()

        with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
             patch("webapp._collect_projects") as mock_collect, \
             patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
             patch("webapp.asyncio.create_task", side_effect=fake_create_task):

            mock_scan.return_value = {"ok": True, "added": 1, "updated": 0, "scanned": 5}
            mock_collect.return_value = [{
                **_make_project(cwd),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

        assert len(create_task_calls) > 0, (
            f"Карточка с seen={_HEAL_MIN_SEEN} должна запускать heal"
        )


# ─────────────────────────── C. Benign filter ───────────────────────────

class TestBenignFilter:
    """Benign-классы из default списка и per-project override."""

    def _proj(self, tmp_path, heal_ignore=None) -> dict:
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir(exist_ok=True)
        return _make_project(cwd, heal_ignore=heal_ignore)

    def test_connection_reset_in_title_is_benign(self, tmp_path):
        """ConnectionResetError в заголовке → benign.
        Один из benign-классов присутствует в тексте — возвращается первый совпавший."""
        card = _make_incident_card(
            text="[ERR] ClientConnectionResetError: Cannot write to closing transport",
            seen=3,
        )
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "benign"
        # Первый совпавший substring из benign-листа: "ConnectionResetError" или
        # "ClientConnectionResetError" — любой из них достаточен
        assert any(b in reason for b in _HEAL_BENIGN_DEFAULT)

    def test_connection_reset_in_excerpt_is_benign(self, tmp_path):
        """ConnectionResetError в excerpt → benign."""
        card = _make_incident_card(
            text="[ERR] SomeWrapper",
            excerpt="ClientConnectionResetError: broken pipe",
            seen=3,
        )
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "benign"

    def test_cancelled_error_is_benign(self, tmp_path):
        card = _make_incident_card(text="[ERR] CancelledError: task cancelled", seen=5)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "benign"

    def test_timeout_error_is_benign(self, tmp_path):
        card = _make_incident_card(text="[ERR] TimeoutError: timed out", seen=5)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "benign"

    def test_value_error_not_benign(self, tmp_path):
        """ValueError — не benign, heal OK."""
        card = _make_incident_card(text="[ERR] ValueError: bad data", seen=3)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "heal"

    def test_per_project_override_adds_class(self, tmp_path):
        """per-project heal_ignore добавляет класс к benign-листу."""
        card = _make_incident_card(text="[ERR] MyCustomTransientError: retry", seen=5)
        proj = self._proj(tmp_path, heal_ignore=["MyCustomTransientError"])
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "benign"
        assert "MyCustomTransientError" in reason

    def test_per_project_override_substring_match(self, tmp_path):
        """per-project heal_ignore: подстрока из excerpt тоже матчит."""
        card = _make_incident_card(
            text="[ERR] SomeError",
            excerpt="caused by IgnoreMe: transient",
            seen=5,
        )
        proj = self._proj(tmp_path, heal_ignore=["IgnoreMe"])
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "benign"

    def test_heal_skip_already_set_returns_skip(self, tmp_path):
        """Карточка с heal_skip уже выставленным → skip (не benign повторно)."""
        meta = {"source": "log", "seen": "5", "heal_skip": "benign", "excerpt": "ConnectionResetError"}
        card = {
            "id": "err-aabbcc",
            "text": "[ERR] ConnectionResetError",
            "description": _format_incident_desc(meta),
        }
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "skip"
        assert "heal_skip" in reason

    @pytest.mark.asyncio
    async def test_scanner_sets_heal_skip_for_benign(self, tmp_path):
        """Интеграция: сканер помечает heal_skip=benign на ConnectionResetError."""
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd)

        incident = _make_incident_card(
            text="[ERR] ClientConnectionResetError: Cannot write",
            seen=5,
        )
        _write_incident_to_board(cwd, incident, column="failed")

        sleep_calls = [None]

        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return
            raise StopAsyncIteration()

        with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
             patch("webapp._collect_projects") as mock_collect, \
             patch("webapp.asyncio.sleep", side_effect=fake_sleep):

            mock_scan.return_value = {"ok": True, "added": 0, "updated": 0, "scanned": 5}
            mock_collect.return_value = [{
                **_make_project(cwd),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

        # Проверяем что heal_skip=benign выставлен в description
        _, _, cols = _load_board(cwd)
        found = None
        for c in cols.get("failed", []):
            if c["id"] == incident["id"]:
                found = c
                break
        assert found is not None
        meta = _parse_incident_desc(found.get("description", ""))
        assert meta.get("heal_skip") == "benign", (
            "Бенигн-инцидент должен получить heal_skip=benign в description"
        )


# ─────────────────────────── D. Rate-limit helpers ───────────────────────────

class TestRateLimit:
    """_heal_rate_ok и _heal_record."""

    def setup_method(self):
        """Очищаем _heal_history перед каждым тестом."""
        _heal_history.clear()

    def test_rate_ok_empty_history(self):
        """Без истории — всегда OK."""
        assert _heal_rate_ok("proj:0", time.time()) is True

    def test_rate_ok_below_max(self):
        """Меньше MAX записей → OK."""
        now = time.time()
        for _ in range(_HEAL_MAX_PER_WINDOW - 1):
            _heal_record("proj:0", now)
        assert _heal_rate_ok("proj:0", now) is True

    def test_rate_not_ok_at_max(self):
        """Ровно MAX записей в окне → NOT OK."""
        now = time.time()
        for _ in range(_HEAL_MAX_PER_WINDOW):
            _heal_record("proj:0", now)
        assert _heal_rate_ok("proj:0", now) is False

    def test_rate_ok_old_entries_pruned(self):
        """Записи старше WINDOW_SEC игнорируются."""
        now = time.time()
        old = now - _HEAL_WINDOW_SEC - 1  # старше окна
        for _ in range(_HEAL_MAX_PER_WINDOW):
            _heal_record("proj:0", old)
        # Старые записи → pruned при rate_ok/record → OK
        assert _heal_rate_ok("proj:0", now) is True

    def test_rate_partial_old_partial_new(self):
        """Часть записей старые, часть новые — считаем только новые."""
        now = time.time()
        old = now - _HEAL_WINDOW_SEC - 1
        for _ in range(_HEAL_MAX_PER_WINDOW - 1):
            _heal_record("proj:0", old)  # старые, будут отброшены
        _heal_record("proj:0", now)  # одна новая
        # Новых: 1 < MAX → OK
        assert _heal_rate_ok("proj:0", now) is True

    def test_rate_keys_isolated(self):
        """Разные ключи независимы."""
        now = time.time()
        for _ in range(_HEAL_MAX_PER_WINDOW):
            _heal_record("proj:A", now)
        # proj:A исчерпан, proj:B свободен
        assert _heal_rate_ok("proj:A", now) is False
        assert _heal_rate_ok("proj:B", now) is True

    def test_heal_record_appends(self):
        """_heal_record добавляет временную метку в историю."""
        now = time.time()
        _heal_record("proj:0", now)
        assert len(_heal_history["proj:0"]) == 1

    def test_heal_record_prunes_old(self):
        """_heal_record при добавлении прореживает старые записи."""
        now = time.time()
        old = now - _HEAL_WINDOW_SEC - 1
        _heal_history["proj:0"] = [old, old, old]
        _heal_record("proj:0", now)
        # Только одна новая запись должна остаться (старые обрезаны)
        assert len(_heal_history["proj:0"]) == 1

    def test_rate_limit_decision_returns_stop(self, tmp_path):
        """_heal_decision: rate_ok=False → stop/rate_limit."""
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir(exist_ok=True)
        card = _make_incident_card(seen=_HEAL_MIN_SEEN)
        proj = _make_project(cwd)
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2,
            running_busy=False, rate_ok=False,
            now=time.time(),
        )
        assert action == "stop"
        assert reason == "rate_limit"

    @pytest.mark.asyncio
    async def test_scanner_rate_limit_fires_timeline_once(self, tmp_path):
        """Интеграция: при rate-limit сканер публикует Timeline событие один раз."""
        _heal_history.clear()
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd)

        # Заполняем историю до лимита
        now = time.time()
        key = f"0:{Path(cwd).name}"
        for _ in range(_HEAL_MAX_PER_WINDOW):
            _heal_record(key, now)

        # Два инцидента с seen>=MIN (оба должны упереться в rate-limit)
        for i in range(2):
            inc = _make_incident_card(card_id=f"err-r{i:04d}", seen=_HEAL_MIN_SEEN)
            _write_incident_to_board(cwd, inc, column="failed")

        published = []

        def fake_publish(sk, ev):
            published.append(ev)

        sleep_calls = [None]

        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return
            raise StopAsyncIteration()

        try:
            with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
                 patch("webapp._collect_projects") as mock_collect, \
                 patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
                 patch("webapp._bus_publish", side_effect=fake_publish):

                mock_scan.return_value = {"ok": True, "added": 0, "updated": 0, "scanned": 0}
                mock_collect.return_value = [{
                    **_make_project(cwd),
                    "log_cmd": "echo err",
                    "test_cmd": None,
                }]

                try:
                    await _error_scanner_loop(ctx)
                except StopAsyncIteration:
                    pass
        finally:
            _heal_history.clear()

        rl_events = [e for e in published if e.get("kind") == "self_heal" and e.get("reason") == "rate_limit"]
        assert len(rl_events) == 1, (
            "При rate-limit должно быть ровно ОДНО Timeline-событие (не per-card)"
        )


# ─────────────────────────── E. _heal_decision: gate ordering ───────────────────────────

class TestHealDecision:
    """Проверяем порядок gates в _heal_decision (cheapest/safest first)."""

    def _proj(self, tmp_path) -> dict:
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir(exist_ok=True)
        return _make_project(cwd)

    def test_non_incident_card_returns_skip(self, tmp_path):
        """Карточка без err- префикса → skip/not_incident."""
        card = {"id": "task-001", "text": "Do something", "description": ""}
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "skip"
        assert reason == "not_incident"

    def test_heal_attempted_takes_priority_over_benign(self, tmp_path):
        """heal_attempted проверяется ДО benign (spec-010 предохранитель важнее)."""
        meta = {
            "source": "log", "seen": "5",
            "heal_attempted": "true",
            # benign-класс тоже присутствует
            "excerpt": "ConnectionResetError",
        }
        card = {
            "id": "err-aabbcc",
            "text": "[ERR] ConnectionResetError: closed",
            "description": _format_incident_desc(meta),
        }
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        # heal_attempted → skip (не benign, который проверяется позже)
        assert action == "skip"
        assert reason == "heal_attempted"

    def test_benign_before_debounce(self, tmp_path):
        """Benign-класс обнаруживается до проверки seen (benign дешевле чем heal)."""
        # seen=1 (тоже должен был бы фильтроваться дебаунсом),
        # но benign проверяется раньше и возвращает 'benign'
        card = _make_incident_card(
            text="[ERR] ConnectionResetError: closed",
            seen=1,
        )
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        # benign проверяется после heal_skip но до seen — возвращает benign
        assert action == "benign"

    def test_concurrency_stop_after_debounce(self, tmp_path):
        """Лимит конкурентности применяется ПОСЛЕ дебаунс (только для зрелых карточек)."""
        card = _make_incident_card(seen=_HEAL_MIN_SEEN)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj,
            active_count=_SELF_HEAL_MAX_CONCURRENT, max_conc=_SELF_HEAL_MAX_CONCURRENT,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "stop"
        assert reason == "concurrency_limit"

    def test_running_busy_stop(self, tmp_path):
        """running_busy=True → stop/project_busy."""
        card = _make_incident_card(seen=_HEAL_MIN_SEEN)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=True, rate_ok=True, now=time.time(),
        )
        assert action == "stop"
        assert reason == "project_busy"

    def test_all_gates_pass_returns_heal(self, tmp_path):
        """Все гейты пройдены → heal."""
        card = _make_incident_card(seen=_HEAL_MIN_SEEN)
        proj = self._proj(tmp_path)
        action, reason = _heal_decision(
            card, proj, active_count=0, max_conc=2,
            running_busy=False, rate_ok=True, now=time.time(),
        )
        assert action == "heal"
        assert reason == "ok"

    def test_spec010_safeguard1_off_by_default(self):
        """spec-010 ПРЕДОХРАНИТЕЛЬ №1: _self_heal_enabled = False по умолчанию."""
        from webapp import _self_heal_enabled
        import os
        project = {}
        os.environ.pop("SELF_HEAL_ENABLED", None)
        assert _self_heal_enabled(project) is False, (
            "КРИТИЧНО: _self_heal_enabled должен быть False по умолчанию"
        )


class TestPhase2ReviewFixes:
    """Доводки по ревью spec-012 Ф2: benign доминирует над seen/rate,
    case-insensitive, per-project heal_ignore (теперь wired), битый seen."""

    def _proj(self, tmp_path, **kw) -> dict:
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir(exist_ok=True)
        return _make_project(cwd, **kw)

    def test_benign_dominates_high_seen_and_free_rate(self, tmp_path):
        """benign-класс с seen=высоким и свободным rate-limit ВСЁ РАВНО не лечится."""
        card = _make_incident_card(
            text="[ERR] ClientConnectionResetError: Cannot write to closing transport",
            seen=_HEAL_MIN_SEEN + 100,
            excerpt="UNHANDLED exc_class=ClientConnectionResetError path=/api/x",
        )
        action, reason = _heal_decision(
            card, self._proj(tmp_path),
            active_count=0, max_conc=2, running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "benign", f"benign должен доминировать, получили {action}/{reason}"

    def test_benign_case_insensitive(self, tmp_path):
        """Строчные/нестандартный регистр имени класса тоже ловится (флуд-защита)."""
        card = _make_incident_card(
            text="[ERR] clientconnectionreseterror: cannot write",
            seen=_HEAL_MIN_SEEN + 1,
            excerpt="connectionreseterror lowercase variant",
        )
        action, reason = _heal_decision(
            card, self._proj(tmp_path),
            active_count=0, max_conc=2, running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "benign", f"case-insensitive benign, получили {action}/{reason}"

    def test_heal_ignore_per_project_applies(self, tmp_path):
        """Per-project heal_ignore (теперь wired в _collect_projects) расширяет benign-лист."""
        card = _make_incident_card(
            text="[ERR] MyFlakyError: transient blip",
            seen=_HEAL_MIN_SEEN + 1,
            excerpt="MyFlakyError: transient blip",
        )
        proj = self._proj(tmp_path, heal_ignore=["MyFlakyError"])
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2, running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "benign", f"per-project heal_ignore должен сработать, получили {action}/{reason}"

    def test_heal_ignore_not_list_is_ignored(self, tmp_path):
        """Битый heal_ignore (не list) → не крашит, просто игнорируется."""
        card = _make_incident_card(seen=_HEAL_MIN_SEEN + 1)
        proj = self._proj(tmp_path, heal_ignore="not-a-list")
        action, reason = _heal_decision(
            card, proj,
            active_count=0, max_conc=2, running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "heal", f"битый heal_ignore не должен ломать решение, получили {action}/{reason}"

    def test_malformed_seen_treated_as_young(self, tmp_path):
        """Нечисловой seen → трактуется как молодой (skip), а не ValueError-краш."""
        card = _make_incident_card(seen="abc")  # type: ignore[arg-type]
        action, reason = _heal_decision(
            card, self._proj(tmp_path),
            active_count=0, max_conc=2, running_busy=False, rate_ok=True,
            now=time.time(),
        )
        assert action == "skip", f"битый seen → skip, получили {action}/{reason}"
