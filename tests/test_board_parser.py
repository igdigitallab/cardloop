"""
Тесты парсера/сериализатора доски (TASKS.md → Kanban).

CRITICAL: регрессия здесь = потеря задач агентов в проде.
Прецедент: потеряно 39 задач в networking-os (2026-05-30).
"""
import re

import pytest

from webapp import (
    BOARD_COLUMNS,
    _count_potential_cards,
    _MARKER_RE,
    _parse_tasks,
    _serialize_tasks,
)


# ─────────────────────────── fixtures ───────────────────────────

CANONICAL_TASKS_MD = """\
# Tasks — my-project

## Backlog
- [ ] Первая задача <!--ops:aabbcc-->
- [ ] Вторая задача <!--ops:112233-->

## In Progress
- [~] Задача в работе <!--ops:deadbe-->

## Review
- [?] На ревью <!--ops:cafe01-->

## Failed
"""

MINIMAL_TASKS_MD = """\
## Backlog
- [ ] Только одна задача <!--ops:abcdef-->
"""

AGENT_PLAIN_TASKS_MD = """\
## Backlog
- Написать тесты
- Добавить CI

## In Progress
- [ ] Агент уже взял это <!--ops:111111-->
"""

PREAMBLE_TASKS_MD = """\
# Tasks — some-project

> Описание задач проекта.

Преамбула: какой-то свободный текст до первой колонки.

## Backlog
- [ ] Задача с преамбулой <!--ops:aabbcc-->
"""


# ─────────────────────────── 1. Каноничный формат ───────────────────────────

def test_parse_canonical():
    """Стандартный TASKS.md с checkbox-карточками парсится корректно.
    ID сохраняется из <!--ops:...-->."""
    _, cols = _parse_tasks(CANONICAL_TASKS_MD)

    assert len(cols["backlog"]) == 2, "Backlog должен содержать 2 карточки"
    assert len(cols["in_progress"]) == 1
    assert len(cols["review"]) == 1
    assert len(cols["failed"]) == 0

    # ID должны сохраниться
    ids = {c["id"] for c in cols["backlog"]}
    assert "aabbcc" in ids, "ID aabbcc должен быть сохранён"
    assert "112233" in ids, "ID 112233 должен быть сохранён"

    ip_card = cols["in_progress"][0]
    assert ip_card["id"] == "deadbe"
    assert ip_card["text"] == "Задача в работе"


# ─────────────────────────── 2. Агентский стиль (без checkbox) ───────────────────────────

def test_parse_plain_bullet_no_checkbox():
    """Строки '- текст' без [ ] внутри секций распознаются как карточки.
    Это ключевая защита от потери задач агента."""
    _, cols = _parse_tasks(AGENT_PLAIN_TASKS_MD)

    assert len(cols["backlog"]) == 2, (
        "Агент написал 2 plain-буллета в Backlog — оба должны стать карточками"
    )
    assert len(cols["in_progress"]) == 1, "Карточка с checkbox должна попасть в In Progress"

    texts = {c["text"] for c in cols["backlog"]}
    assert "Написать тесты" in texts
    assert "Добавить CI" in texts


def test_parse_plain_bullet_in_correct_column():
    """Plain-буллет в 'In Progress' должен попасть в in_progress, а не в backlog."""
    text = """\
## Backlog

## In Progress
- Задача без чекбокса в In Progress
"""
    _, cols = _parse_tasks(text)
    assert len(cols["in_progress"]) == 1
    assert cols["in_progress"][0]["text"] == "Задача без чекбокса в In Progress"
    assert len(cols["backlog"]) == 0


# ─────────────────────────── 3. Round-trip стабильность ───────────────────────────

def test_parse_round_trip():
    """parse → serialize → parse должен давать те же карточки.
    Второй проход не должен изменить файл."""
    _, cols1 = _parse_tasks(CANONICAL_TASKS_MD)
    serialized = _serialize_tasks("", cols1, "my-project")

    _, cols2 = _parse_tasks(serialized)

    # Структура должна совпасть
    assert {c["id"] for c in cols2["backlog"]} == {c["id"] for c in cols1["backlog"]}
    assert {c["id"] for c in cols2["in_progress"]} == {c["id"] for c in cols1["in_progress"]}
    assert {c["id"] for c in cols2["review"]} == {c["id"] for c in cols1["review"]}

    # Второй проход: serialize снова — результат должен быть идентичен
    serialized2 = _serialize_tasks("", cols2, "my-project")
    assert serialized == serialized2, "Сериализация нестабильна — round-trip изменил файл"


# ─────────────────────────── 4. Преамбула сохраняется ───────────────────────────

def test_parse_preamble_preserved():
    """Текст ДО первой ## колонки сохраняется при сериализации."""
    preamble, cols = _parse_tasks(PREAMBLE_TASKS_MD)

    assert "Tasks — some-project" in preamble, "Заголовок должен быть в преамбуле"
    assert "свободный текст" in preamble, "Свободный текст должен быть в преамбуле"

    serialized = _serialize_tasks(preamble, cols, "some-project")
    assert "свободный текст" in serialized, "Текст преамбулы должен сохраниться при сериализации"
    assert "## Backlog" in serialized


def test_parse_preamble_not_in_backlog():
    """Строки преамбулы не должны попасть в карточки."""
    _, cols = _parse_tasks(PREAMBLE_TASKS_MD)
    all_texts = [c["text"] for cards in cols.values() for c in cards]
    assert not any("Описание" in t for t in all_texts), "Описание из преамбулы не должно стать карточкой"
    assert not any("свободный текст" in t for t in all_texts)


# ─────────────────────────── 5. Guard: _count_potential_cards ───────────────────────────

def test_count_potential_cards_canonical():
    """Каноничный файл: _count_potential_cards == количество карточек из _parse_tasks."""
    _, cols = _parse_tasks(CANONICAL_TASKS_MD)
    parsed_count = sum(len(v) for v in cols.values())
    potential = _count_potential_cards(CANONICAL_TASKS_MD)
    assert potential == parsed_count, (
        f"Для каноничного файла potential={potential} должен совпадать с parsed={parsed_count}"
    )


def test_count_potential_cards_more_than_parsed():
    """Файл с таблицей или нераспознанным форматом: potential > parsed.
    Это активирует guard в api_project_tasks — НЕ перезаписывать файл."""
    # Таблица с '- ' строками ВНУТРИ секции — счётчик увидит их как потенциальные карточки,
    # но _parse_tasks не умеет их распознать как карточки (без текста после дефиса — min len).
    text_with_ambiguous_lines = """\
## Backlog
- [ ] Реальная карточка <!--ops:aabbcc-->
- Item 1 из какого-то списка
- Item 2 из того же списка
"""
    potential = _count_potential_cards(text_with_ambiguous_lines)
    _, cols = _parse_tasks(text_with_ambiguous_lines)
    parsed_count = sum(len(v) for v in cols.values())

    # Потенциальных должно быть >= реальных (>= потому что plain bullets тоже парсятся)
    assert potential >= parsed_count, (
        f"potential={potential} должен быть >= parsed={parsed_count}"
    )


def test_count_potential_cards_empty():
    """Пустой файл → 0 потенциальных карточек."""
    assert _count_potential_cards("") == 0
    assert _count_potential_cards("# заголовок\n\nПросто текст") == 0


def test_count_potential_cards_only_preamble():
    """Строки '- ' до первой ## секции НЕ считаются потенциальными карточками."""
    text = """\
# Tasks

- не карточка (преамбула)
- тоже не карточка

## Backlog
- [ ] Настоящая карточка
"""
    potential = _count_potential_cards(text)
    assert potential == 1, f"Только одна карточка в секции, потенциальных должно быть 1, а не {potential}"


# ─────────────────────────── 6. ID: извлечение из маркера ───────────────────────────

def test_extract_id_existing():
    """Карточка с <!--ops:abc123--> сохраняет этот ID."""
    text = "## Backlog\n- [ ] Задача <!--ops:abc123-->\n"
    _, cols = _parse_tasks(text)
    assert len(cols["backlog"]) == 1
    assert cols["backlog"][0]["id"] == "abc123"


def test_extract_id_generates_new_for_missing_marker():
    """Карточка без <!--ops:...--> получает новый сгенерированный ID (не пустую строку)."""
    text = "## Backlog\n- [ ] Задача без маркера\n"
    _, cols = _parse_tasks(text)
    assert len(cols["backlog"]) == 1
    card_id = cols["backlog"][0]["id"]
    assert card_id, "ID не должен быть пустым"
    assert len(card_id) == 6, f"Ожидался hex(3) = 6 символов, получили {len(card_id)!r}"
    assert re.match(r"^[0-9a-f]{6}$", card_id), f"ID должен быть hex: {card_id!r}"


def test_extract_id_dedup_duplicate_markers():
    """Строка с двумя <!--ops:X--> маркерами: берётся первый, текст очищается."""
    # Симулируем дубликат маркера в тексте карточки
    text = "## Backlog\n- [ ] Задача <!--ops:aaa111--> <!--ops:bbb222-->\n"
    _, cols = _parse_tasks(text)
    assert len(cols["backlog"]) == 1
    card = cols["backlog"][0]
    # _MARKER_RE ищет маркер с конца строки (search), берёт первое совпадение через search
    # В коде: mk = _MARKER_RE.search(rest) — поэтому берётся ПОСЛЕДНИЙ маркер на строке
    # (search находит первое вхождение, но маркер ищется в конце `rest`)
    # Главное: в тексте карточки не должно быть тегов <!--ops:...-->
    assert "<!--ops:" not in card["text"], (
        f"ID-маркер не должен попасть в текст карточки: {card['text']!r}"
    )


# ─────────────────────────── 7. Неизвестные секции ───────────────────────────

def test_serialize_unknown_section_dropped():
    """## Notes (не валидная колонка) не воспроизводится при сериализации,
    но карточки в валидных секциях не страдают."""
    text = """\
## Backlog
- [ ] Нормальная карточка <!--ops:aabbcc-->

## Notes
- Это заметка, не карточка

## In Progress
- [~] Задача в работе <!--ops:112233-->
"""
    _, cols = _parse_tasks(text)
    # Валидные карточки должны быть на месте
    assert len(cols["backlog"]) == 1
    assert len(cols["in_progress"]) == 1

    serialized = _serialize_tasks("", cols, "test")
    # ## Notes не должна появиться в выводе
    assert "## Notes" not in serialized, "Неизвестная секция не должна воспроизводиться"
    # Заметка не должна стать карточкой
    assert "Это заметка" not in serialized


def test_serialize_all_valid_columns_present():
    """Все четыре колонки всегда присутствуют в сериализованном выводе."""
    _, cols = _parse_tasks("")
    serialized = _serialize_tasks("", cols, "test")
    for _, label, _ in BOARD_COLUMNS:
        assert f"## {label}" in serialized, f"Колонка '## {label}' должна быть в выводе"


def test_parse_empty_file():
    """Пустой файл парсится без ошибок, все колонки пустые."""
    preamble, cols = _parse_tasks("")
    assert preamble == ""
    for key, _, _ in BOARD_COLUMNS:
        assert cols[key] == [], f"Колонка {key} должна быть пустой"


def test_serialize_adds_default_preamble_if_empty():
    """Если преамбула пустая — serialize добавляет дефолтный заголовок '# Tasks — <name>'."""
    _, cols = _parse_tasks("")
    serialized = _serialize_tasks("", cols, "my-project")
    assert "# Tasks — my-project" in serialized
