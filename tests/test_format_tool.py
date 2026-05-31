"""
Тесты для webapp._format_tool — единый форматтер tool-событий.

Используется в трёх точках: chat SSE, bus publish, session-history.
Регрессия = ломает live-чат, history-парсинг и шину активности одновременно.
"""
from webapp import _format_tool


# ─────────────────────────── Bash ───────────────────────────

def test_bash_basic():
    out = _format_tool("Bash", {"command": "ls -la", "description": "list files"})
    assert out == {"name": "Bash", "kind": "bash", "cmd": "ls -la", "desc": "list files"}


def test_bash_no_description():
    out = _format_tool("Bash", {"command": "pwd"})
    assert out["cmd"] == "pwd"
    assert out["desc"] == ""


def test_bash_empty_input():
    out = _format_tool("Bash", {})
    assert out["kind"] == "bash"
    assert out["cmd"] == ""


# ─────────────────────────── Edit / MultiEdit / NotebookEdit ───────────────────────────

def test_edit_basic():
    out = _format_tool("Edit", {
        "file_path": "/tmp/a.py", "old_string": "x = 1", "new_string": "x = 2",
    })
    assert out == {
        "name": "Edit", "kind": "edit", "file": "/tmp/a.py",
        "old": "x = 1", "new": "x = 2",
    }


def test_edit_truncates_long_strings():
    big = "A" * 1000
    out = _format_tool("Edit", {"file_path": "x", "old_string": big, "new_string": big})
    assert len(out["old"]) == 401  # 400 + ellipsis '…'
    assert out["old"].endswith("…")
    assert len(out["new"]) == 401
    assert out["new"].endswith("…")


def test_multiedit_counts_edits():
    out = _format_tool("MultiEdit", {
        "file_path": "/tmp/b.py",
        "edits": [{"old_string": "a", "new_string": "b"}] * 3,
    })
    assert out == {"name": "MultiEdit", "kind": "edit", "file": "/tmp/b.py", "count": 3}


def test_multiedit_empty_edits():
    out = _format_tool("MultiEdit", {"file_path": "x"})
    assert out["count"] == 0


def test_multiedit_non_list_edits():
    """Если edits не список (битый input) — count=0, не падаем."""
    out = _format_tool("MultiEdit", {"file_path": "x", "edits": "garbage"})
    assert out["count"] == 0


def test_notebook_edit_cell_type():
    out = _format_tool("NotebookEdit", {"file_path": "/tmp/n.ipynb", "cell_type": "code"})
    assert out == {
        "name": "NotebookEdit", "kind": "edit", "file": "/tmp/n.ipynb", "cell_type": "code",
    }


# ─────────────────────────── Write ───────────────────────────

def test_write_short_content():
    out = _format_tool("Write", {"file_path": "/tmp/x.txt", "content": "hello"})
    assert out == {"name": "Write", "kind": "write", "file": "/tmp/x.txt", "preview": "hello"}


def test_write_truncates_long_content():
    big = "X" * 1000
    out = _format_tool("Write", {"file_path": "/tmp/y.txt", "content": big})
    assert len(out["preview"]) == 601  # 600 + '…'
    assert out["preview"].endswith("…")


def test_write_non_string_content():
    """content не строка — preview пустой, не падаем."""
    out = _format_tool("Write", {"file_path": "x", "content": ["a", "b"]})
    assert out["preview"] == ""


# ─────────────────────────── Read ───────────────────────────

def test_read():
    out = _format_tool("Read", {"file_path": "/etc/passwd"})
    assert out == {"name": "Read", "kind": "read", "file": "/etc/passwd"}


def test_read_no_file_path():
    out = _format_tool("Read", {})
    assert out["kind"] == "read"
    assert out["file"] == ""


# ─────────────────────────── Glob / Grep ───────────────────────────

def test_glob():
    out = _format_tool("Glob", {"pattern": "*.py", "path": "/tmp"})
    assert out == {"name": "Glob", "kind": "search", "pattern": "*.py", "path": "/tmp"}


def test_grep():
    out = _format_tool("Grep", {"pattern": "TODO", "path": "src/"})
    assert out["kind"] == "search"
    assert out["pattern"] == "TODO"


# ─────────────────────────── other / fallback ───────────────────────────

def test_unknown_tool_uses_first_value():
    out = _format_tool("WeirdTool", {"some_arg": "hello world"})
    assert out == {"name": "WeirdTool", "kind": "other", "summary": "hello world"}


def test_unknown_tool_empty_input():
    out = _format_tool("WeirdTool", {})
    assert out["kind"] == "other"
    assert out["summary"] == ""


def test_unknown_tool_truncates_summary():
    big = "Z" * 500
    out = _format_tool("WeirdTool", {"q": big})
    assert len(out["summary"]) == 201  # 200 + '…'
    assert out["summary"].endswith("…")


def test_unknown_tool_non_string_first():
    """Первый аргумент не строка — str() его (число, dict)."""
    out = _format_tool("WeirdTool", {"q": 42})
    assert out["summary"] == "42"


# ─────────────────────────── защита от мусора ───────────────────────────

def test_non_dict_input_treated_as_empty():
    """Если inp не dict (битый input от SDK) — обрабатываем как {}, не крашимся."""
    out = _format_tool("Bash", "not a dict")  # type: ignore[arg-type]
    assert out["kind"] == "bash"
    assert out["cmd"] == ""


def test_all_tools_return_name_and_kind():
    """Контракт: все варианты возвращают как минимум 'name' и 'kind' — UI на них опирается."""
    for tool_name in ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit",
                      "NotebookEdit", "RandomThing"):
        out = _format_tool(tool_name, {})
        assert "name" in out, f"{tool_name}: нет 'name'"
        assert "kind" in out, f"{tool_name}: нет 'kind'"
        assert out["name"] == tool_name
