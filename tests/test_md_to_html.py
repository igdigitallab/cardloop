"""
Тесты для bot.md_to_html — markdown ответа модели → безопасный HTML для Telegram.

CRITICAL: регрессия = крах TG-отправки.
Из CLAUDE.md: «Ответ модели часто содержит <title>/<div> и пр. → Telegram BadRequest».
Стратегия: код/ссылки в плейсхолдеры ДО html.escape, потом markdown, потом возврат.
"""
from bot import md_to_html, CODE_MAX_LINES, CODE_PREVIEW_LINES


# ─────────────────────────── 1. Escape сырого HTML ───────────────────────────

def test_escape_raw_html_tag():
    """Сырые <title>/<div> из ответа модели становятся &lt;title&gt; — главная защита от BadRequest."""
    out = md_to_html("Found <title>foo</title> tag")
    assert "<title>" not in out, "Сырой <title> должен быть экранирован"
    assert "&lt;title&gt;" in out


def test_escape_ampersand():
    out = md_to_html("a & b")
    assert "&amp;" in out
    assert " & " not in out


def test_escape_lt_gt():
    out = md_to_html("if x < 5 and y > 3")
    assert "&lt;" in out and "&gt;" in out


# ─────────────────────────── 2. Жирный / курсив ───────────────────────────

def test_bold():
    assert md_to_html("hello **world**") == "hello <b>world</b>"


def test_italic():
    assert md_to_html("hello *world*") == "hello <i>world</i>"


def test_italic_not_confused_with_multiplication():
    """*курсив* консервативный — не должен ловить '2 * 3' или остатки списков."""
    # Между числами с пробелами — не курсив
    out = md_to_html("2 * 3 = 6")
    assert "<i>" not in out, f"'2 * 3' не должен стать курсивом: {out!r}"


def test_bold_and_italic_combined():
    out = md_to_html("**bold** and *italic*")
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out


# ─────────────────────────── 3. Заголовки ───────────────────────────

def test_h1_to_bold():
    out = md_to_html("# Заголовок")
    assert out.strip() == "<b>Заголовок</b>"


def test_h6_to_bold():
    out = md_to_html("###### малый заголовок")
    assert out.strip() == "<b>малый заголовок</b>"


def test_seven_hashes_not_header():
    """####### (7 решёток) — не заголовок (только 1-6)."""
    out = md_to_html("####### foo")
    assert "<b>" not in out


# ─────────────────────────── 4. Списки → bullet ───────────────────────────

def test_dash_list_becomes_bullet():
    out = md_to_html("- item1\n- item2")
    assert "• item1" in out and "• item2" in out
    assert "- item" not in out


def test_asterisk_list_becomes_bullet():
    out = md_to_html("* foo\n* bar")
    assert "• foo" in out and "• bar" in out


def test_plus_list_becomes_bullet():
    out = md_to_html("+ alpha\n+ beta")
    assert "• alpha" in out


def test_indented_list_preserves_indent():
    out = md_to_html("  - nested")
    assert "  • nested" in out


# ─────────────────────────── 5. Инлайн-код ───────────────────────────

def test_inline_code():
    out = md_to_html("use `foo()` here")
    assert "<code>foo()</code>" in out


def test_inline_code_escapes_html_inside():
    """Сырой HTML внутри `...` должен быть экранирован."""
    out = md_to_html("`<div>html</div>`")
    assert "<code>&lt;div&gt;html&lt;/div&gt;</code>" in out


def test_inline_code_not_eaten_by_escape():
    """Плейсхолдер код подставляется ПОСЛЕ html.escape, тег <code> жив."""
    out = md_to_html("text `inline` more")
    assert "<code>inline</code>" in out
    assert "&lt;code&gt;" not in out


# ─────────────────────────── 6. Блоки кода ```...``` ───────────────────────────

def test_code_block_short():
    """Короткий блок кода (≤ CODE_MAX_LINES) рендерится целиком в <pre>."""
    code = "line1\nline2\nline3"
    out = md_to_html(f"```\n{code}\n```")
    assert "<pre>" in out and "</pre>" in out
    assert "line1" in out and "line3" in out


def test_code_block_with_language():
    out = md_to_html("```python\nx = 1\n```")
    assert "<pre>" in out
    assert "x = 1" in out


def test_code_block_long_collapses():
    """Длинный блок (> CODE_MAX_LINES) сворачивается в превью + маркер строк."""
    lines = [f"line{i}" for i in range(CODE_MAX_LINES + 5)]
    code = "\n".join(lines)
    out = md_to_html(f"```\n{code}\n```")
    # Первые CODE_PREVIEW_LINES должны быть в выводе
    assert f"line{CODE_PREVIEW_LINES - 1}" in out
    # Последняя строка свёрнута — должна отсутствовать
    assert f"line{CODE_MAX_LINES + 4}" not in out
    # Должен быть маркер сворачивания
    assert "collapsed" in out


def test_code_block_escapes_html_inside():
    """HTML внутри ```...``` экранируется."""
    out = md_to_html("```\n<script>alert(1)</script>\n```")
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_code_block_with_markdown_inside_not_processed():
    """**bold** внутри ```...``` не должен стать <b> — это код, не markdown."""
    out = md_to_html("```\n**not bold**\n```")
    assert "<b>" not in out
    assert "**not bold**" in out


# ─────────────────────────── 7. Ссылки ───────────────────────────

def test_link_basic():
    out = md_to_html("[click](https://example.com)")
    assert '<a href="https://example.com">click</a>' in out


def test_link_with_special_chars_in_text():
    out = md_to_html("[a & b](https://example.com)")
    assert ">a &amp; b</a>" in out


def test_link_url_with_query():
    out = md_to_html("[search](https://example.com/?q=foo&bar=baz)")
    # Кавычки в href должны быть экранированы; & в URL → &amp;
    assert "https://example.com/?q=foo&amp;bar=baz" in out


def test_non_http_link_not_processed():
    """Только http(s):// — javascript:/data: остаются как текст."""
    out = md_to_html("[xss](javascript:alert(1))")
    assert "<a " not in out
    # Текст экранирован как обычный markdown
    assert "javascript:" in out


# ─────────────────────────── 8. Комбинации ───────────────────────────

def test_code_protects_dangerous_html():
    """Главный security-кейс: `<script>` внутри инлайн-кода не должно стать тегом."""
    out = md_to_html("use `<script>alert(1)</script>` carefully")
    assert "<code>&lt;script&gt;alert(1)&lt;/script&gt;</code>" in out
    assert "<script>" not in out


def test_empty_input():
    assert md_to_html("") == ""


def test_only_whitespace():
    out = md_to_html("   \n\n  ")
    # Не должно крашиться, должно вернуть нечто (пробелы/переводы строк)
    assert isinstance(out, str)


def test_newlines_preserved():
    """Переводы строк не теряются."""
    out = md_to_html("line1\nline2\nline3")
    assert out.count("\n") >= 2
