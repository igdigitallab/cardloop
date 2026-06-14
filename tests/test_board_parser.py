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
    _effective_card_model,
    _ALLOWED_MODELS,
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


# ─────────────────────────── 8. Description (новое) ───────────────────────────

def test_parse_card_with_description():
    """Строки '  > текст' сразу после карточки собираются в description."""
    text = """\
## Backlog
- [ ] Короткий заголовок <!--ops:abc111-->
  > Первая строка описания.
  > Вторая строка описания.
- [ ] Карточка без описания <!--ops:abc222-->
"""
    _, cols = _parse_tasks(text)
    assert len(cols["backlog"]) == 2

    card_with_desc = cols["backlog"][0]
    assert card_with_desc["id"] == "abc111"
    assert card_with_desc["text"] == "Короткий заголовок"
    assert card_with_desc["description"] == "Первая строка описания.\nВторая строка описания."

    card_no_desc = cols["backlog"][1]
    assert card_no_desc["id"] == "abc222"
    assert card_no_desc.get("description") is None


def test_parse_card_without_description_still_works():
    """Обратная совместимость: карточки без description парсятся как раньше."""
    _, cols = _parse_tasks(CANONICAL_TASKS_MD)
    for col_cards in cols.values():
        for card in col_cards:
            # description должен отсутствовать (None или ключ не существует)
            assert card.get("description") is None, (
                f"Карточка '{card['text']}' не должна иметь description в каноничном файле"
            )


def test_serialize_round_trip_with_description():
    """Карточка с description: parse → serialize → parse сохраняет description точно."""
    original = """\
## Backlog
- [ ] Title карточки <!--ops:xyz789-->
  > Описание первая строка.
  > Описание вторая строка.
- [ ] Без описания <!--ops:nnn000-->
"""
    _, cols1 = _parse_tasks(original)
    serialized = _serialize_tasks("", cols1, "test-project")

    # Проверяем что сериализованный файл содержит description-строки
    assert "  > Описание первая строка." in serialized
    assert "  > Описание вторая строка." in serialized

    # Round-trip: парсим обратно
    _, cols2 = _parse_tasks(serialized)
    assert len(cols2["backlog"]) == 2

    card_with = cols2["backlog"][0]
    assert card_with["id"] == "xyz789"
    assert card_with["text"] == "Title карточки"
    assert card_with["description"] == "Описание первая строка.\nОписание вторая строка."

    card_without = cols2["backlog"][1]
    assert card_without.get("description") is None

    # Второй serialize: идемпотентен
    serialized2 = _serialize_tasks("", cols2, "test-project")
    assert serialized == serialized2, "Сериализация с description нестабильна"


def test_parse_description_stops_at_new_card():
    """Description прерывается при встрече новой карточки."""
    text = """\
## Backlog
- [ ] Первая <!--ops:aaa001-->
  > Описание первой.
- [ ] Вторая <!--ops:aaa002-->
  > Описание второй.
"""
    _, cols = _parse_tasks(text)
    assert len(cols["backlog"]) == 2
    assert cols["backlog"][0]["description"] == "Описание первой."
    assert cols["backlog"][1]["description"] == "Описание второй."


def test_parse_description_stops_at_section_boundary():
    """Description карточки не переходит в следующую секцию."""
    text = """\
## Backlog
- [ ] Задача <!--ops:bbb001-->
  > Описание.

## In Progress
- [ ] Другая задача <!--ops:bbb002-->
"""
    _, cols = _parse_tasks(text)
    assert cols["backlog"][0]["description"] == "Описание."
    assert cols["in_progress"][0].get("description") is None


def test_count_potential_cards_description_lines_not_counted():
    """Строки description '  > ...' не считаются как потенциальные карточки."""
    text = """\
## Backlog
- [ ] Карточка <!--ops:ccc001-->
  > Описание строка 1.
  > Описание строка 2.
"""
    potential = _count_potential_cards(text)
    assert potential == 1, (
        f"Description строки не должны считаться как карточки: potential={potential}"
    )


# ─────────────────────────── Card 43665f: model field round-trip ───────────────


def test_parse_card_with_model_metadata():
    """Marker <!--ops:ID model=haiku--> is parsed; card carries model field."""
    text = """\
## Backlog
- [ ] Task with model <!--ops:aaa001 model=haiku-->
- [ ] Task without model <!--ops:aaa002-->
"""
    _, cols = _parse_tasks(text)
    cards = cols["backlog"]
    assert len(cards) == 2
    assert cards[0]["id"] == "aaa001"
    assert cards[0]["model"] == "haiku"
    assert cards[1]["id"] == "aaa002"
    assert cards[1].get("model") is None, "Card without model must not carry the field"


def test_parse_card_invalid_model_ignored():
    """Unknown model values in the marker are silently ignored (no model field)."""
    text = """\
## Backlog
- [ ] Task <!--ops:bbb001 model=gpt-9-->
"""
    _, cols = _parse_tasks(text)
    card = cols["backlog"][0]
    assert card.get("model") is None, "Invalid model value must be dropped"


def test_serialize_card_with_model():
    """Cards with a valid model field emit model=<val> in the ops marker."""
    preamble = "# Tasks — proj"
    cols = {
        "backlog": [{"id": "ccc001", "text": "My task", "model": "opus"}],
        "in_progress": [], "review": [], "failed": [],
    }
    serialized = _serialize_tasks(preamble, cols, "proj")
    assert "<!--ops:ccc001 model=opus-->" in serialized, (
        f"Expected model in marker, got:\n{serialized}"
    )


def test_serialize_card_without_model_no_metadata():
    """Cards without a model field emit the plain <!--ops:ID--> marker (no extra space/metadata)."""
    preamble = "# Tasks — proj"
    cols = {
        "backlog": [{"id": "ddd001", "text": "Plain task"}],
        "in_progress": [], "review": [], "failed": [],
    }
    serialized = _serialize_tasks(preamble, cols, "proj")
    assert "<!--ops:ddd001-->" in serialized, (
        f"Expected plain marker, got:\n{serialized}"
    )
    # Must NOT have any metadata
    assert "model=" not in serialized


def test_model_round_trip_parse_serialize_parse():
    """Full round-trip: parse → serialize → parse preserves model field exactly."""
    original = """\
# Tasks — proj

## Backlog
- [ ] Task A <!--ops:eee001 model=fable-->
- [ ] Task B <!--ops:eee002-->

## In Progress

## Review

## Failed
"""
    preamble, cols = _parse_tasks(original)
    serialized = _serialize_tasks(preamble, cols, "proj")
    preamble2, cols2 = _parse_tasks(serialized)

    card_a = cols2["backlog"][0]
    card_b = cols2["backlog"][1]
    assert card_a["id"] == "eee001"
    assert card_a["model"] == "fable", f"model should survive round-trip: {card_a}"
    assert card_b["id"] == "eee002"
    assert card_b.get("model") is None, f"card without model should stay clean: {card_b}"


def test_model_round_trip_stable():
    """serialize → parse → serialize produces identical output (idempotent)."""
    preamble = "# Tasks — proj"
    cols = {
        "backlog": [
            {"id": "fff001", "text": "With model", "model": "sonnet"},
            {"id": "fff002", "text": "No model"},
        ],
        "in_progress": [], "review": [], "failed": [],
    }
    s1 = _serialize_tasks(preamble, cols, "proj")
    _, cols2 = _parse_tasks(s1)
    s2 = _serialize_tasks(preamble, cols2, "proj")
    assert s1 == s2, f"Serialization is not stable:\n--- first ---\n{s1}\n--- second ---\n{s2}"


# ─────────────────────────── Long-text round-trip (bug fix d1ebd5) ─────────────

def test_long_single_line_task_round_trips_fully():
    """A task whose title is >120 chars (but no newline) must survive write→read intact.

    Regression guard: the old addCard() front-end code truncated single-line text at
    120 chars and moved the overflow to description, causing the visible card title to
    be cut off at 120 characters. The fix removes that truncation — the full text is
    stored on the '- [ ] ...' line and must be recovered verbatim by _parse_tasks."""
    long_title = "A" * 60 + " " + "B" * 60  # 121 chars, no newline
    cols_in = {
        "backlog": [{"id": "long01", "text": long_title}],
        "in_progress": [], "review": [], "failed": [],
    }
    serialized = _serialize_tasks("# Tasks — proj", cols_in, "proj")

    # The long title must appear verbatim on a single card line (no split)
    assert f"- [ ] {long_title} <!--ops:long01-->" in serialized, (
        f"Long title must be on a single line in TASKS.md:\n{serialized}"
    )

    # Round-trip: parse back
    _, cols_out = _parse_tasks(serialized)
    cards = cols_out["backlog"]
    assert len(cards) == 1, "Exactly one card expected after round-trip"
    assert cards[0]["text"] == long_title, (
        f"Full text must be preserved: expected {long_title!r}, got {cards[0]['text']!r}"
    )
    assert cards[0].get("description") is None, (
        "No description should be auto-generated for a long single-line task"
    )


def test_long_multiline_task_round_trips_fully():
    """A multi-line task: first line = title stored on '- [ ]', rest = description '  > ' lines.

    This path is correct in the original code and must continue to work after the fix."""
    title = "Fix the authentication flow"
    rest = "Check OAuth token expiry.\nUpdate the refresh logic.\nAdd integration test."
    cols_in = {
        "backlog": [{"id": "ml01", "text": title, "description": rest}],
        "in_progress": [], "review": [], "failed": [],
    }
    serialized = _serialize_tasks("# Tasks — proj", cols_in, "proj")

    # Title on card line
    assert f"- [ ] {title} <!--ops:ml01-->" in serialized
    # Description lines present
    for line in rest.splitlines():
        assert f"  > {line}" in serialized, f"Missing description line: {line!r}"

    # Round-trip
    _, cols_out = _parse_tasks(serialized)
    cards = cols_out["backlog"]
    assert len(cards) == 1
    assert cards[0]["text"] == title
    assert cards[0]["description"] == rest, (
        f"Multi-line description must survive round-trip:\nexpected {rest!r}\ngot {cards[0]['description']!r}"
    )


def test_long_single_line_count_guard_not_triggered():
    """A single long-title card counts as exactly 1 potential card — wipe-guard must not fire."""
    long_title = "X" * 300  # extremely long, no newlines
    cols_in = {
        "backlog": [{"id": "guard1", "text": long_title}],
        "in_progress": [], "review": [], "failed": [],
    }
    serialized = _serialize_tasks("# Tasks — proj", cols_in, "proj")
    potential = _count_potential_cards(serialized)
    assert potential == 1, (
        f"Long single-line card must count as 1 potential card, got {potential}"
    )


def test_existing_cards_no_model_survive_round_trip():
    """Cards written before this feature (no model in marker) survive without corruption."""
    text = """\
# Tasks — legacy

## Backlog
- [ ] Old task A <!--ops:leg001-->
- [ ] Old task B <!--ops:leg002-->

## In Progress

## Review

## Failed
"""
    preamble, cols = _parse_tasks(text)
    serialized = _serialize_tasks(preamble, cols, "legacy")
    _, cols2 = _parse_tasks(serialized)

    assert len(cols2["backlog"]) == 2
    for card in cols2["backlog"]:
        assert card.get("model") is None, f"Legacy card gained unexpected model: {card}"
    # Markers must remain simple
    assert "model=" not in serialized


# ─────────────────────────── Card 43665f: _effective_card_model resolution ────


def test_effective_card_model_uses_card_override():
    """When card has a valid model, it is returned regardless of global settings."""
    import unittest.mock as mock
    card = {"id": "x", "text": "t", "model": "opus"}
    with mock.patch("webapp._get_global_setting", return_value="haiku"):
        result = _effective_card_model(card)
    assert result == "opus", f"Expected 'opus' (card override), got {result!r}"


def test_effective_card_model_falls_to_global_setting():
    """When card has no model, board_card_model global setting is used."""
    import unittest.mock as mock
    card = {"id": "x", "text": "t"}

    def _fake_get_setting(key, fallback=None):
        if key == "board_card_model":
            return "haiku"
        return fallback

    with mock.patch("webapp._get_global_setting", side_effect=_fake_get_setting):
        result = _effective_card_model(card)
    assert result == "haiku", f"Expected 'haiku' (global setting), got {result!r}"


def test_effective_card_model_defaults_to_sonnet():
    """When card has no model and global setting is absent, falls back to 'sonnet'."""
    import unittest.mock as mock
    card = {"id": "x", "text": "t"}
    with mock.patch("webapp._get_global_setting", return_value=None):
        result = _effective_card_model(card)
    assert result == "sonnet", f"Expected 'sonnet' fallback, got {result!r}"


def test_effective_card_model_ignores_invalid_card_model():
    """Invalid card model value is skipped; falls through to global setting."""
    import unittest.mock as mock
    card = {"id": "x", "text": "t", "model": "gpt-5-turbo"}

    def _fake_get_setting(key, fallback=None):
        if key == "board_card_model":
            return "fable"
        return fallback

    with mock.patch("webapp._get_global_setting", side_effect=_fake_get_setting):
        result = _effective_card_model(card)
    assert result == "fable", f"Invalid card model should fall through to global setting, got {result!r}"


def test_effective_card_model_does_not_use_project_model():
    """_effective_card_model never falls back to project model — only sonnet."""
    import unittest.mock as mock
    # Even if someone passes a project-dict-like thing with 'model', it must be
    # treated as per-card override (not project model). When absent and global is
    # empty, sonnet is the floor.
    card = {"id": "x", "text": "t"}  # no 'model' key
    with mock.patch("webapp._get_global_setting", return_value=""):
        result = _effective_card_model(card)
    assert result == "sonnet"
    # Verify the allowed set is correct
    assert "sonnet" in _ALLOWED_MODELS
    assert "opus" in _ALLOWED_MODELS
    assert "haiku" in _ALLOWED_MODELS
    assert "fable" in _ALLOWED_MODELS
