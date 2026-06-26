"""
Tests for webapp._collect_projects — building the project list from ctx["topics"].

Regression log_cmd: 2026-05-31 the field was lost during collection — the Logs tab
stopped working even with a correct topics.json. The test below fixes the contract
"log_cmd is passed through".
"""
from webapp import _collect_projects


def _make_ctx(topics: dict, default_model: str = "sonnet") -> dict:
    """Minimal ctx for _collect_projects."""
    return {
        "topics": topics,
        "DATA": None,  # _load_free_chats returns {} when file is absent — that's fine
        "DEFAULT_MODEL": default_model,
    }


# ─────────────────────────── basic behaviour ───────────────────────────

def test_empty_topics(tmp_path):
    ctx = {"topics": {}, "DATA": tmp_path, "DEFAULT_MODEL": "sonnet"}
    assert _collect_projects(ctx) == []


def test_single_project(tmp_path):
    ctx = {
        "topics": {"-100:42": {"project": "my-proj", "cwd": "/tmp/test-project/my-proj", "model": "opus"}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert len(out) == 1
    p = out[0]
    assert p["id"] == "my-proj"
    assert p["name"] == "my-proj"
    assert p["cwd"] == "/tmp/test-project/my-proj"
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
    """Two topics with the same cwd — only the first is kept (dedup by cwd)."""
    ctx = {
        "topics": {
            "-100:1": {"project": "p", "cwd": "/tmp/dup", "model": "opus"},
            "-100:2": {"project": "p-alt", "cwd": "/tmp/dup", "model": "haiku"},
        },
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert len(out) == 1, "Dedup by cwd must leave exactly one entry"


def test_empty_cwd_skipped(tmp_path):
    """Topic without cwd is not included in the result."""
    ctx = {
        "topics": {"-100:1": {"project": "broken", "cwd": ""}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    assert _collect_projects(ctx) == []


def test_sorted_by_name_lowercase(tmp_path):
    """Case-insensitive sort by name."""
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
    """log_cmd from topics.json must appear in the output — otherwise the Logs tab silently breaks.
    Regression: before 2026-05-31 the field was lost during collection (gotcha in Cardloop)."""
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
    """Without log_cmd in topics → value is None (not a KeyError downstream)."""
    ctx = {
        "topics": {"-100:1": {"project": "p", "cwd": "/tmp/p"}},
        "DATA": tmp_path,
        "DEFAULT_MODEL": "sonnet",
    }
    out = _collect_projects(ctx)
    assert "log_cmd" in out[0], "Key must be present (UI relies on it)"
    assert out[0]["log_cmd"] is None
