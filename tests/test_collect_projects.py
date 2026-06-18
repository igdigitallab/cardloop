"""
Тесты для webapp._collect_projects — сборка списка проектов из ctx["topics"].

Регрессия log_cmd: 2026-05-31 поле терялось при сборке — вкладка «Логи» не работала
даже при правильном topics.json. Тест ниже фиксирует контракт «log_cmd пробрасывается».
"""
from webapp import _collect_projects


def _make_ctx(topics: dict, default_model: str = "sonnet") -> dict:
    """Минимальный ctx для _collect_projects."""
    return {
        "topics": topics,
        "DATA": None,  # _load_free_chats вернёт {} при отсутствии файла — нам ок
        "DEFAULT_MODEL": default_model,
    }


# ─────────────────────────── базовое поведение ───────────────────────────

def test_empty_topics(tmp_path):
    ctx = {"topics": {}, "DATA": tmp_path, "DEFAULT_MODEL": "sonnet"}
    assert _collect_projects(ctx) == []


def test_single_project(tmp_path):
    ctx = {
        "topics": {"-100:42": {"project": "my-proj", "cwd": "/home/igor/my-proj", "model": "opus"}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert len(out) == 1
    p = out[0]
    assert p["id"] == "my-proj"
    assert p["name"] == "my-proj"
    assert p["cwd"] == "/home/igor/my-proj"
    assert p["model"] == "opus"
    assert p["session_key"] == "-100:42"
    assert p["is_free"] is False


def test_default_model_used_when_topic_missing_model(tmp_path):
    ctx = {
        "topics": {"-100:1": {"project": "p", "cwd": "/tmp/p"}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert out[0]["model"] == "sonnet"


def test_dedup_by_cwd(tmp_path):
    """Два топика с одним cwd — берётся только первый (дедуп по cwd)."""
    ctx = {
        "topics": {
            "-100:1": {"project": "p", "cwd": "/tmp/dup", "model": "opus"},
            "-100:2": {"project": "p-alt", "cwd": "/tmp/dup", "model": "haiku"},
        },
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert len(out) == 1, "Дедуп по cwd должен оставить одну запись"


def test_empty_cwd_skipped(tmp_path):
    """Топик без cwd не попадает в выдачу."""
    ctx = {
        "topics": {"-100:1": {"project": "broken", "cwd": ""}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    assert _collect_projects(ctx) == []


def test_sorted_by_name_lowercase(tmp_path):
    """Регистронезависимая сортировка по имени."""
    ctx = {
        "topics": {
            "-100:1": {"project": "Zeta", "cwd": "/tmp/z"},
            "-100:2": {"project": "alpha", "cwd": "/tmp/a"},
            "-100:3": {"project": "Beta", "cwd": "/tmp/b"},
        },
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    names = [p["name"] for p in out]
    assert names == ["alpha", "Beta", "Zeta"]


# ─────────────────────────── log_cmd (regression fix 2026-05-31) ───────────────────────────

def test_log_cmd_propagated_when_set(tmp_path):
    """log_cmd из topics.json должен попадать в выдачу — иначе вкладка «Логи» молча не работает.
    Регрессия: до 2026-05-31 поле терялось при сборке (gotcha в claude-ops-bot)."""
    ctx = {
        "topics": {
            "-100:1": {
                "project": "p",
                "cwd": "/tmp/p",
                "log_cmd": "journalctl -u my-service -n 300 --no-pager",
            }
        },
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert out[0]["log_cmd"] == "journalctl -u my-service -n 300 --no-pager"


def test_log_cmd_none_when_not_set(tmp_path):
    """Без log_cmd в topics → значение None (а не KeyError ниже по стеку)."""
    ctx = {
        "topics": {"-100:1": {"project": "p", "cwd": "/tmp/p"}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert "log_cmd" in out[0], "Ключ должен присутствовать (UI на него опирается)"
    assert out[0]["log_cmd"] is None
