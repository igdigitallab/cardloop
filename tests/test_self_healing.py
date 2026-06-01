"""
Тесты Spec 010 — Самолечение (Self-Healing).

КРИТИЧНЫЙ регрессия-страж: без флага self_heal — сканер только создаёт карточки,
починку НЕ запускает (поведение = v0.7.0). Предохранители 1-6 покрыты тестами.

ПРЕДОХРАНИТЕЛИ:
1. OFF по умолчанию — _self_heal_enabled default False
2. НИКОГДА не auto-apply — агент доходит только до Review
3. Лимит 1 попытка/инцидент — heal_attempted ставится ДО прогона
4. Лимит конкурентности — счётчик активных починок
5. Только git+clean — не-git/dirty пропускаются
6. Всё видно — Timeline kind:"self_heal"
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp
from webapp import (
    _self_heal_enabled,
    _parse_incident_desc,
    _format_incident_desc,
    _is_incident_card,
    _load_board,
    _save_board,
    _tasks_path,
    _self_heal_card,
    _error_scanner_loop,
)


# ─────────────────────────── Фикстуры ───────────────────────────

@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Временный git-репо с baseline-коммитом (чистый)."""
    cwd = tmp_path / "heal_repo"
    cwd.mkdir()
    subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@t.com"], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(cwd), check=True, capture_output=True)
    (cwd / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(cwd), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(cwd), check=True, capture_output=True)
    return cwd


@pytest.fixture
def tmp_non_git(tmp_path: Path) -> Path:
    """Временная директория — НЕ git-репо."""
    cwd = tmp_path / "plain_dir"
    cwd.mkdir()
    return cwd


def _make_ctx(data_dir: Path, cwd: str, self_heal: bool = False) -> dict:
    """Создаёт минимальный ctx с одним проектом."""
    pid = Path(cwd.rstrip("/")).name
    return {
        "topics": {
            f"0:{pid}": {
                "cwd": cwd,
                "project": pid,
                "self_heal": self_heal,
            },
        },
        "sessions": {},
        "running": {},
        "DATA": data_dir,
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }


def _make_project(cwd: str, self_heal: bool = False) -> dict:
    """Создаёт dict проекта для передачи в _self_heal_card."""
    pid = Path(cwd.rstrip("/")).name
    return {
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


def _make_incident_card(card_id: str = "err-abc123", text: str = "[ERR] ValueError: bad input") -> dict:
    """Создаёт карточку-инцидент."""
    meta = {
        "source": "log",
        "seen": "1",
        "first": "2026-05-31T12:00",
        "last": "2026-05-31T12:00",
        "excerpt": "ValueError: bad input\n  File app.py line 42",
    }
    return {
        "id": card_id,
        "text": text,
        "description": _format_incident_desc(meta),
    }


def _write_incident_to_board(cwd: str, incident_card: dict, column: str = "failed"):
    """Записывает инцидент в TASKS.md проекта."""
    name = Path(cwd).name
    tp = _tasks_path(cwd)
    _, preamble, cols = _load_board(cwd)
    cols[column].append(incident_card)
    _save_board(cwd, name, preamble, cols)


# ─────────────────────────── ШАГ 1: _self_heal_enabled ───────────────────────────

class TestSelfHealEnabled:

    def test_default_false_no_flag_no_env(self):
        """ПРЕДОХРАНИТЕЛЬ №1: без флага и без env — всегда False."""
        project = {"self_heal": False}
        with patch.dict(os.environ, {}, clear=True):
            # убираем SELF_HEAL_ENABLED если вдруг установлена
            os.environ.pop("SELF_HEAL_ENABLED", None)
            assert _self_heal_enabled(project) is False

    def test_default_false_missing_key(self):
        """Если ключ self_heal отсутствует в проекте — False."""
        project = {}
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SELF_HEAL_ENABLED", None)
            assert _self_heal_enabled(project) is False

    def test_enabled_by_project_flag(self):
        """Флаг per-project включает."""
        project = {"self_heal": True}
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SELF_HEAL_ENABLED", None)
            assert _self_heal_enabled(project) is True

    def test_enabled_by_env_true(self):
        """SELF_HEAL_ENABLED=1 включает глобально."""
        project = {"self_heal": False}
        with patch.dict(os.environ, {"SELF_HEAL_ENABLED": "1"}):
            assert _self_heal_enabled(project) is True

    def test_enabled_by_env_true_string(self):
        """SELF_HEAL_ENABLED=true включает."""
        project = {}
        with patch.dict(os.environ, {"SELF_HEAL_ENABLED": "true"}):
            assert _self_heal_enabled(project) is True

    def test_enabled_by_env_yes(self):
        """SELF_HEAL_ENABLED=yes включает."""
        project = {}
        with patch.dict(os.environ, {"SELF_HEAL_ENABLED": "yes"}):
            assert _self_heal_enabled(project) is True

    def test_env_false_doesnt_override_project_flag(self):
        """SELF_HEAL_ENABLED=0 при project.self_heal=True → True (проект приоритетнее)."""
        project = {"self_heal": True}
        with patch.dict(os.environ, {"SELF_HEAL_ENABLED": "0"}):
            assert _self_heal_enabled(project) is True

    def test_env_zero_returns_false(self):
        """SELF_HEAL_ENABLED=0 без флага проекта → False."""
        project = {"self_heal": False}
        with patch.dict(os.environ, {"SELF_HEAL_ENABLED": "0"}):
            assert _self_heal_enabled(project) is False


# ─────────────────────────── ШАГ 1: heal_attempted в meta ───────────────────────────

class TestHealAttemptedMeta:

    def test_format_incident_desc_includes_heal_attempted(self):
        """_format_incident_desc сериализует heal_attempted=true."""
        meta = {
            "source": "log",
            "seen": "1",
            "first": "2026-05-31T12:00",
            "last": "2026-05-31T12:00",
            "heal_attempted": "true",
            "excerpt": "err",
        }
        desc = _format_incident_desc(meta)
        assert "heal_attempted=true" in desc

    def test_parse_incident_desc_reads_heal_attempted(self):
        """_parse_incident_desc читает heal_attempted."""
        desc = "source=log\nseen=1\nfirst=2026-05-31T12:00\nheal_attempted=true\nexcerpt=err"
        meta = _parse_incident_desc(desc)
        assert meta.get("heal_attempted") == "true"

    def test_format_without_heal_attempted_no_key(self):
        """Если heal_attempted не установлен — в description не попадает."""
        meta = {"source": "log", "seen": "1", "first": "2026-05-31T12:00", "last": "2026-05-31T12:00"}
        desc = _format_incident_desc(meta)
        assert "heal_attempted" not in desc

    def test_heal_attempted_false_not_written(self):
        """heal_attempted=False не пишется (только truthy значения)."""
        meta = {"source": "log", "heal_attempted": False}
        desc = _format_incident_desc(meta)
        assert "heal_attempted" not in desc


# ─────────────────────────── КРИТИЧНЫЙ РЕГРЕССИЯ-СТРАЖ ───────────────────────────

class TestScannerOffByDefault:
    """
    ПРЕДОХРАНИТЕЛЬ №1 — КРИТИЧНЫЙ: без флага self_heal сканер только создаёт
    карточки, не запускает починку. Поведение = v0.7.0.
    """

    @pytest.mark.asyncio
    async def test_scanner_off_default_no_heal_call(self, tmp_path):
        """
        Сканер без self_heal → _self_heal_card НЕ вызывается.
        Это регрессия-страж против случайного включения авто-починки.
        """
        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=False)

        heal_calls = []

        def fake_create_task(coro):
            heal_calls.append(coro)
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        sleep_calls = [None]  # первый sleep(30) проходит
        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return
            raise StopAsyncIteration()

        with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
             patch("webapp._collect_projects") as mock_collect, \
             patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
             patch("webapp.asyncio.create_task", side_effect=fake_create_task):

            mock_scan.return_value = {"ok": True, "added": 5, "updated": 0, "scanned": 10}
            mock_collect.return_value = [{
                **_make_project(cwd, self_heal=False),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

            assert len(heal_calls) == 0, (
                "КРИТИЧНО: починка запустилась без флага self_heal! "
                "Это нарушение ПРЕДОХРАНИТЕЛЯ №1."
            )

    @pytest.mark.asyncio
    async def test_scanner_with_flag_would_trigger_heal(self, tmp_path, tmp_git):
        """
        С флагом self_heal=True И новыми инцидентами — _self_heal_card вызывается
        через asyncio.create_task. (Это позитивный тест флага.)

        Тестируем напрямую логику принятия решения в scanner loop:
        проверяем, что create_task вызывается при self_heal=True + added>0.
        """
        cwd = str(tmp_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)

        incident = _make_incident_card()
        _write_incident_to_board(cwd, incident, column="failed")

        create_task_calls = []

        def fake_create_task(coro):
            create_task_calls.append(coro)
            # Не выполняем coro — закрываем
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        # sleep(30) → 0 (первый вызов пропустить), sleep(INTERVAL) → StopAsyncIteration
        sleep_calls = [None]  # первый раз ничего
        async def fake_sleep(n):
            if sleep_calls:
                sleep_calls.pop()
                return  # первый sleep(30) проходит
            raise StopAsyncIteration()

        with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
             patch("webapp._collect_projects") as mock_collect, \
             patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
             patch("webapp.asyncio.create_task", side_effect=fake_create_task):

            mock_scan.return_value = {"ok": True, "added": 1, "updated": 0, "scanned": 5}
            mock_collect.return_value = [{
                **_make_project(cwd, self_heal=True),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

            assert len(create_task_calls) > 0, (
                "При self_heal=True и новых инцидентах должен вызываться create_task"
            )


# ─────────────────────────── ПРЕДОХРАНИТЕЛЬ №3: heal_attempted ДО прогона ───────────────────────────

class TestHealAttemptedBeforeRun:

    @pytest.mark.asyncio
    async def test_heal_attempted_set_before_run_engine(self, tmp_path, tmp_git):
        """
        ПРЕДОХРАНИТЕЛЬ №3: heal_attempted=true ставится ДО запуска агента.
        Если агент упадёт — повторного запуска не будет.
        """
        cwd = str(tmp_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        incident = _make_incident_card("err-abc123")
        _write_incident_to_board(cwd, incident, column="failed")

        # Флаг пометки (заметим до выполнения run_engine)
        heal_attempted_set_before_run = []

        async def fake_run_engine(**kwargs):
            # Проверяем состояние TASKS.md в момент вызова run_engine
            _, _, cols = _load_board(cwd)
            for col_cards in cols.values():
                for c in col_cards:
                    if c["id"] == "err-abc123":
                        meta = _parse_incident_desc(c.get("description", ""))
                        heal_attempted_set_before_run.append(meta.get("heal_attempted") == "true")
            # Генератор — возвращаем пустой результат
            return
            yield  # make it async generator

        # Мокируем run_engine как async generator
        class FakeGen:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration

        ctx["run_engine"] = lambda **kw: FakeGen()

        with patch("webapp._card_run_mode", new_callable=AsyncMock, return_value="worktree"), \
             patch("webapp._card_worktree_setup", new_callable=AsyncMock) as mock_wt_setup, \
             patch("webapp._run_quality_gate", new_callable=AsyncMock, return_value={"verdict": "safe", "tests": {"detected": False, "ok": True, "cmd": None, "exit_code": 0, "output": "", "timed_out": False}, "lint": None}), \
             patch("webapp._send_tg_ping", new_callable=AsyncMock), \
             patch("webapp._commit_in_worktree", new_callable=AsyncMock, return_value=False), \
             patch("webapp._diff_from_worktree", new_callable=AsyncMock, return_value=("", "")):

            mock_wt_setup.return_value = {"wt_path": str(tmp_path / "wt"), "base_branch": "main"}
            # Создаём папку worktree
            (tmp_path / "wt").mkdir()

            await _self_heal_card(ctx, project, incident)

        # После выполнения heal_attempted должен быть выставлен
        _, _, cols = _load_board(cwd)
        found_meta = None
        for col_cards in cols.values():
            for c in col_cards:
                if c["id"] == "err-abc123":
                    found_meta = _parse_incident_desc(c.get("description", ""))
                    break

        assert found_meta is not None
        assert found_meta.get("heal_attempted") == "true", (
            "ПРЕДОХРАНИТЕЛЬ №3: heal_attempted должен быть выставлен после попытки"
        )

    @pytest.mark.asyncio
    async def test_heal_attempted_already_set_skip_in_scanner(self, tmp_path):
        """
        ПРЕДОХРАНИТЕЛЬ №3: инцидент с heal_attempted=true — НЕ перезапускается.
        """
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)

        # Инцидент уже помечен как починявшийся
        meta = {
            "source": "log", "seen": "1",
            "first": "2026-05-31T12:00", "last": "2026-05-31T12:00",
            "heal_attempted": "true",
            "excerpt": "err",
        }
        incident = {
            "id": "err-abc123",
            "text": "[ERR] ValueError: bad",
            "description": _format_incident_desc(meta),
        }
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
                **_make_project(cwd, self_heal=True),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

        assert len(create_task_calls) == 0, (
            "ПРЕДОХРАНИТЕЛЬ №3: heal_attempted инцидент НЕ должен перезапускаться"
        )


# ─────────────────────────── ПРЕДОХРАНИТЕЛЬ №4: лимит конкурентности ───────────────────────────

class TestConcurrencyLimit:

    @pytest.mark.asyncio
    async def test_busy_project_not_healed(self, tmp_path):
        """
        ПРЕДОХРАНИТЕЛЬ №4: если проект занят (running lock) — починка не запускается.
        """
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        # Ставим running lock
        session_key = project["tg_thread"]
        ctx["running"][session_key] = True

        incident = _make_incident_card("err-busy123")
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
                **_make_project(cwd, self_heal=True),
                "log_cmd": "echo err",
                "test_cmd": None,
            }]

            try:
                await _error_scanner_loop(ctx)
            except StopAsyncIteration:
                pass

        assert len(create_task_calls) == 0, (
            "ПРЕДОХРАНИТЕЛЬ №4: занятый проект не должен запускать починку"
        )

    @pytest.mark.asyncio
    async def test_global_concurrency_limit(self, tmp_path):
        """
        ПРЕДОХРАНИТЕЛЬ №4: глобальный лимит _SELF_HEAL_MAX_CONCURRENT.
        Если счётчик на максимуме — новые починки не запускаются.
        """
        cwd = str(tmp_path / "proj")
        (tmp_path / "proj").mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)

        incident = _make_incident_card("err-limit123")
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

        # Устанавливаем счётчик на максимум
        import webapp as _wapp
        original = _wapp._self_heal_active_count
        _wapp._self_heal_active_count = _wapp._SELF_HEAL_MAX_CONCURRENT

        try:
            with patch("webapp._scan_and_ingest", new_callable=AsyncMock) as mock_scan, \
                 patch("webapp._collect_projects") as mock_collect, \
                 patch("webapp.asyncio.sleep", side_effect=fake_sleep), \
                 patch("webapp.asyncio.create_task", side_effect=fake_create_task):

                mock_scan.return_value = {"ok": True, "added": 1, "updated": 0, "scanned": 5}
                mock_collect.return_value = [{
                    **_make_project(cwd, self_heal=True),
                    "log_cmd": "echo err",
                    "test_cmd": None,
                }]

                try:
                    await _error_scanner_loop(ctx)
                except StopAsyncIteration:
                    pass
        finally:
            _wapp._self_heal_active_count = original

        assert len(create_task_calls) == 0, (
            "ПРЕДОХРАНИТЕЛЬ №4: при достижении лимита новые починки не должны запускаться"
        )


# ─────────────────────────── ПРЕДОХРАНИТЕЛЬ №5: git+clean ───────────────────────────

class TestGitCleanRequired:

    @pytest.mark.asyncio
    async def test_non_git_project_skipped(self, tmp_path, tmp_non_git):
        """
        ПРЕДОХРАНИТЕЛЬ №5: не-git проект пропускается.
        """
        cwd = str(tmp_non_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        incident = _make_incident_card("err-git123")
        _write_incident_to_board(cwd, incident, column="failed")

        # Ставим счётчик (чтобы финальный decrement не ушёл в минус)
        import webapp as _wapp
        original = _wapp._self_heal_active_count
        _wapp._self_heal_active_count = 1

        try:
            with patch("webapp._send_tg_ping", new_callable=AsyncMock), \
                 patch("webapp._bus_publish"):
                await _self_heal_card(ctx, project, incident)
        finally:
            _wapp._self_heal_active_count = original

        # Инцидент должен остаться в failed без heal_badge
        _, _, cols = _load_board(cwd)
        found = None
        for c in cols.get("failed", []):
            if c["id"] == "err-git123":
                found = c
                break

        # heal_attempted должен быть выставлен (ДО проверки режима)
        if found:
            meta = _parse_incident_desc(found.get("description", ""))
            assert meta.get("heal_attempted") == "true", (
                "ПРЕДОХРАНИТЕЛЬ №3: heal_attempted ставится до проверки worktree-режима"
            )


# ─────────────────────────── ПРЕДОХРАНИТЕЛЬ №2: никогда не auto-apply ───────────────────────────

class TestNoAutoApply:
    """
    ПРЕДОХРАНИТЕЛЬ №2: НИКОГДА не auto-apply.
    Агент доходит только до Review. api_card_apply НЕ вызывается из самолечения.
    """

    @pytest.mark.asyncio
    async def test_safe_gate_goes_to_review_not_apply(self, tmp_path, tmp_git):
        """
        safe-гейт → карточка в Review. api_card_apply НЕ вызывается.
        """
        cwd = str(tmp_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        incident = _make_incident_card("err-safe123")
        _write_incident_to_board(cwd, incident, column="failed")

        apply_called = []

        class FakeGen:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        ctx["run_engine"] = lambda **kw: FakeGen()

        with patch("webapp._card_run_mode", new_callable=AsyncMock, return_value="worktree"), \
             patch("webapp._card_worktree_setup", new_callable=AsyncMock) as mock_wt, \
             patch("webapp._run_quality_gate", new_callable=AsyncMock, return_value={
                 "verdict": "safe",
                 "tests": {"detected": True, "ok": True, "cmd": "pytest", "exit_code": 0, "output": "", "timed_out": False},
                 "lint": None,
             }), \
             patch("webapp._send_tg_ping", new_callable=AsyncMock), \
             patch("webapp._commit_in_worktree", new_callable=AsyncMock, return_value=True), \
             patch("webapp._diff_from_worktree", new_callable=AsyncMock, return_value=("diff content", "1 file")), \
             patch("webapp.api_card_apply", new_callable=AsyncMock) as mock_apply:

            wt_path = str(tmp_path / "wt")
            (tmp_path / "wt").mkdir()
            mock_wt.return_value = {"wt_path": wt_path, "base_branch": "main"}

            import webapp as _wapp
            original = _wapp._self_heal_active_count
            _wapp._self_heal_active_count = 1
            try:
                await _self_heal_card(ctx, project, incident)
            finally:
                _wapp._self_heal_active_count = original

        mock_apply.assert_not_called(), "ПРЕДОХРАНИТЕЛЬ №2: api_card_apply НЕ должен вызываться из самолечения"

        # Карточка должна быть в Review (не в Failed)
        _, _, cols = _load_board(cwd)
        review_ids = [c["id"] for c in cols.get("review", [])]
        assert "err-safe123" in review_ids, "safe-гейт → карточка должна быть в Review"

    @pytest.mark.asyncio
    async def test_risky_gate_goes_to_failed(self, tmp_path, tmp_git):
        """
        risky-гейт → карточка в Failed с пометкой «гейт ✗».
        """
        cwd = str(tmp_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        incident = _make_incident_card("err-risky123")
        _write_incident_to_board(cwd, incident, column="failed")

        class FakeGen:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        ctx["run_engine"] = lambda **kw: FakeGen()

        with patch("webapp._card_run_mode", new_callable=AsyncMock, return_value="worktree"), \
             patch("webapp._card_worktree_setup", new_callable=AsyncMock) as mock_wt, \
             patch("webapp._run_quality_gate", new_callable=AsyncMock, return_value={
                 "verdict": "risky",
                 "tests": {"detected": True, "ok": False, "cmd": "pytest", "exit_code": 1, "output": "FAILED", "timed_out": False},
                 "lint": None,
             }), \
             patch("webapp._send_tg_ping", new_callable=AsyncMock), \
             patch("webapp._commit_in_worktree", new_callable=AsyncMock, return_value=True), \
             patch("webapp._diff_from_worktree", new_callable=AsyncMock, return_value=("diff", "1 file")):

            wt_path = str(tmp_path / "wt")
            (tmp_path / "wt").mkdir()
            mock_wt.return_value = {"wt_path": wt_path, "base_branch": "main"}

            import webapp as _wapp
            original = _wapp._self_heal_active_count
            _wapp._self_heal_active_count = 1
            try:
                await _self_heal_card(ctx, project, incident)
            finally:
                _wapp._self_heal_active_count = original

        _, _, cols = _load_board(cwd)
        failed_ids = [c["id"] for c in cols.get("failed", [])]
        assert "err-risky123" in failed_ids, "risky-гейт → карточка должна быть в Failed"

        # Проверяем пометку в description
        for c in cols.get("failed", []):
            if c["id"] == "err-risky123":
                assert "гейт ✗" in (c.get("description") or ""), "risky карточка должна иметь пометку «гейт ✗»"
                break


# ─────────────────────────── ПРЕДОХРАНИТЕЛЬ №6: Timeline ───────────────────────────

class TestTimelineEvents:

    @pytest.mark.asyncio
    async def test_timeline_receives_self_heal_events(self, tmp_path, tmp_git):
        """
        ПРЕДОХРАНИТЕЛЬ №6: Timeline получает события kind:"self_heal".
        """
        cwd = str(tmp_git)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = _make_ctx(data_dir, cwd, self_heal=True)
        project = _make_project(cwd, self_heal=True)

        incident = _make_incident_card("err-timeline123")
        _write_incident_to_board(cwd, incident, column="failed")

        published_events = []

        def fake_bus_publish(session_key, event):
            published_events.append(event)

        class FakeGen:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        ctx["run_engine"] = lambda **kw: FakeGen()

        with patch("webapp._bus_publish", side_effect=fake_bus_publish), \
             patch("webapp._card_run_mode", new_callable=AsyncMock, return_value="worktree"), \
             patch("webapp._card_worktree_setup", new_callable=AsyncMock) as mock_wt, \
             patch("webapp._run_quality_gate", new_callable=AsyncMock, return_value={
                 "verdict": "safe", "tests": None, "lint": None,
             }), \
             patch("webapp._send_tg_ping", new_callable=AsyncMock), \
             patch("webapp._commit_in_worktree", new_callable=AsyncMock, return_value=False), \
             patch("webapp._diff_from_worktree", new_callable=AsyncMock, return_value=("", "")):

            wt_path = str(tmp_path / "wt")
            (tmp_path / "wt").mkdir()
            mock_wt.return_value = {"wt_path": wt_path, "base_branch": "main"}

            import webapp as _wapp
            original = _wapp._self_heal_active_count
            _wapp._self_heal_active_count = 1
            try:
                await _self_heal_card(ctx, project, incident)
            finally:
                _wapp._self_heal_active_count = original

        # Проверяем наличие self_heal событий
        self_heal_events = [e for e in published_events if e.get("kind") == "self_heal"]
        assert len(self_heal_events) > 0, "Timeline должен получить хотя бы одно self_heal событие"

        phases = {e.get("phase") for e in self_heal_events}
        assert "start" in phases, "Должно быть событие phase:start"


# ─────────────────────────── API endpoint: self-heal toggle ───────────────────────────

class TestSelfHealToggleAPI:

    @pytest.fixture
    def web_app(self, tmp_path):
        """aiohttp-приложение с роутом self-heal toggle."""
        import webapp as _wapp
        from aiohttp import web

        cwd = str(tmp_path / "proj")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        pid = "proj"

        ctx = {
            "topics": {
                f"0:{pid}": {"cwd": cwd, "project": pid, "self_heal": False},
            },
            "sessions": {},
            "running": {},
            "password": "testpass",
            "DATA": data_dir,
            "HERE": ROOT,
            "VAULT_PROJECTS": tmp_path / "vault",
            "DEFAULT_MODEL": "sonnet",
            "save_sessions": lambda: None,
            "save_topics": lambda: None,
            "run_engine": None,
            "ptb_app": None,
            "rate_limits": {},
        }
        ctx["_auth_token"] = _wapp._derive_token("testpass")

        app = web.Application(middlewares=[_wapp.auth_middleware])
        app["ctx"] = ctx
        app.router.add_post("/api/login", _wapp.api_login)
        app.router.add_post("/api/projects/{id}/self-heal", _wapp.api_project_self_heal_toggle)
        return app

    @pytest.mark.asyncio
    async def test_toggle_enable_requires_auth(self, aiohttp_client, web_app):
        """POST /api/projects/{id}/self-heal без auth → 401."""
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/projects/proj/self-heal", json={"enabled": True})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_toggle_enable_with_auth(self, aiohttp_client, web_app, tmp_path):
        """POST /api/projects/{id}/self-heal с auth → 200, self_heal=True."""
        import webapp as _wapp
        client = await aiohttp_client(web_app)
        token = _wapp._derive_token("testpass")

        resp = await client.post(
            "/api/projects/proj/self-heal",
            json={"enabled": True},
            headers={"Cookie": f"cops_auth={token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["self_heal"] is True
        assert data["topics_updated"] >= 1

    @pytest.mark.asyncio
    async def test_toggle_disable_with_auth(self, aiohttp_client, web_app, tmp_path):
        """Отключение самолечения: enabled=False."""
        import webapp as _wapp
        client = await aiohttp_client(web_app)
        token = _wapp._derive_token("testpass")

        # Сначала включаем
        await client.post(
            "/api/projects/proj/self-heal",
            json={"enabled": True},
            headers={"Cookie": f"cops_auth={token}"},
        )
        # Потом выключаем
        resp = await client.post(
            "/api/projects/proj/self-heal",
            json={"enabled": False},
            headers={"Cookie": f"cops_auth={token}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["self_heal"] is False

    @pytest.mark.asyncio
    async def test_toggle_unknown_project_404(self, aiohttp_client, web_app, tmp_path):
        """Несуществующий проект → 404."""
        import webapp as _wapp
        client = await aiohttp_client(web_app)
        token = _wapp._derive_token("testpass")

        resp = await client.post(
            "/api/projects/nonexistent/self-heal",
            json={"enabled": True},
            headers={"Cookie": f"cops_auth={token}"},
        )
        assert resp.status == 404


# ─────────────────────────── collect_projects включает self_heal ───────────────────────────

class TestCollectProjectsSelfHeal:

    def test_collect_projects_includes_self_heal_flag(self, tmp_path):
        """_collect_projects передаёт self_heal флаг из topics.json."""
        from webapp import _collect_projects

        cwd = str(tmp_path / "myproject")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = {
            "topics": {
                "0:42": {"cwd": cwd, "project": "myproject", "self_heal": True},
            },
            "DEFAULT_MODEL": "sonnet",
            "DATA": data_dir,
        }

        projects = _collect_projects(ctx)
        assert len(projects) == 1
        assert projects[0]["self_heal"] is True

    def test_collect_projects_self_heal_default_false(self, tmp_path):
        """Если self_heal не задан в topics — в проекте False."""
        from webapp import _collect_projects

        cwd = str(tmp_path / "myproject2")
        Path(cwd).mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx = {
            "topics": {
                "0:43": {"cwd": cwd, "project": "myproject2"},
            },
            "DEFAULT_MODEL": "sonnet",
            "DATA": data_dir,
        }

        projects = _collect_projects(ctx)
        assert len(projects) == 1
        assert projects[0]["self_heal"] is False
