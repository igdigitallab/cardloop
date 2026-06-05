"""
Тесты cross-device UI-state sync (раскладка кокпита между устройствами).

Покрывают:
- персистентность data/ui_state.json: load/save/namespace-изоляция;
- устойчивость к битому файлу (не роняем кокпит);
- HTTP-хендлеры GET/PUT: roundtrip, валидация тела, лимит размера,
  сохранность чужих namespace при записи в свой.
"""
import sys
import json
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


@pytest.fixture
def ui_tmp(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _webapp._ui_state_init({"DATA": data})
    yield data
    _webapp._UI_STATE_PATH = None


class _FakeReq:
    """Минимальный stand-in для web.Request — хендлерам нужен только .json()
    (а _ui_state_ns игнорирует req, отдавая "default")."""
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _run(coro):
    return asyncio.run(coro)


# ─────────────── персистентность ───────────────

def test_load_all_empty(ui_tmp):
    assert _webapp._ui_state_load_all() == {}


def test_save_and_load_roundtrip(ui_tmp):
    _webapp._ui_state_save_all({"default": {"open": ["a", "b"], "active": "a"}})
    assert _webapp._ui_state_load_all()["default"]["open"] == ["a", "b"]


def test_namespace_isolation(ui_tmp):
    _webapp._ui_state_save_all({"alice": {"active": "x"}, "bob": {"active": "y"}})
    data = _webapp._ui_state_load_all()
    assert data["alice"]["active"] == "x"
    assert data["bob"]["active"] == "y"


def test_broken_file_returns_empty(ui_tmp):
    (ui_tmp / "ui_state.json").write_text("{не json", encoding="utf-8")
    assert _webapp._ui_state_load_all() == {}


def test_ns_is_default_single_tenant(ui_tmp):
    # ЕДИНСТВЕННАЯ точка смены на user_id при мульти-юзере (см. spec-013)
    assert _webapp._ui_state_ns(None) == "default"


# ─────────────── HTTP-хендлеры ───────────────

def test_get_returns_empty_state_initially(ui_tmp):
    resp = _run(_webapp.api_ui_state_get(_FakeReq(None)))
    assert resp.status == 200
    assert json.loads(resp.body.decode())["state"] == {}


def test_put_then_get_roundtrip(ui_tmp):
    state = {"open": ["p1", "p2"], "active": "p1", "splitWidth": 55}
    r1 = _run(_webapp.api_ui_state_put(_FakeReq({"state": state})))
    assert r1.status == 200
    r2 = _run(_webapp.api_ui_state_get(_FakeReq(None)))
    got = json.loads(r2.body.decode())["state"]
    assert got["open"] == ["p1", "p2"]
    assert got["active"] == "p1"
    assert got["splitWidth"] == 55


def test_put_rejects_non_object_state(ui_tmp):
    r = _run(_webapp.api_ui_state_put(_FakeReq({"state": "nope"})))
    assert r.status == 400


def test_put_rejects_missing_state(ui_tmp):
    r = _run(_webapp.api_ui_state_put(_FakeReq({})))
    assert r.status == 400


def test_put_rejects_bad_json(ui_tmp):
    r = _run(_webapp.api_ui_state_put(_FakeReq(ValueError("bad json"))))
    assert r.status == 400


def test_put_rejects_oversize(ui_tmp):
    big = {"junk": "x" * (65 * 1024)}
    r = _run(_webapp.api_ui_state_put(_FakeReq({"state": big})))
    assert r.status == 413


def test_put_preserves_other_namespaces(ui_tmp):
    # запись в "default" не должна затирать соседний namespace (важно для мульти-юзера)
    _webapp._ui_state_save_all({"other": {"active": "keep"}})
    _run(_webapp.api_ui_state_put(_FakeReq({"state": {"active": "new"}})))
    data = _webapp._ui_state_load_all()
    assert data["other"]["active"] == "keep"
    assert data["default"]["active"] == "new"
