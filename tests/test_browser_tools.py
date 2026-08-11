"""_run_with_retry: browser_navigate/click/type/snapshot must survive exactly the
scenario that used to loop forever — a session whose CDP connection died mid-run.
BrowserSession self-retires on that (see test_browser_pane.py); this is the other
half: the caller must notice it got a genuinely fresh session and use it, not just
fail the same way twice.
"""
import asyncio

import browser_tools


class _FakeSession:
    def __init__(self, name):
        self.name = name


def test_run_with_retry_returns_result_on_success(monkeypatch):
    sess = _FakeSession("only")

    async def fake_get_or_create(cwd):
        return sess
    monkeypatch.setattr(browser_tools._browser_pane, "get_or_create", fake_get_or_create)

    async def op(s):
        return f"ok:{s.name}"

    assert asyncio.run(browser_tools._run_with_retry("cwd", op)) == "ok:only"


def test_run_with_retry_retries_once_on_dead_connection(monkeypatch):
    sess1, sess2 = _FakeSession("dead"), _FakeSession("fresh")
    calls = {"n": 0}

    async def fake_get_or_create(cwd):
        calls["n"] += 1
        return sess1 if calls["n"] == 1 else sess2
    monkeypatch.setattr(browser_tools._browser_pane, "get_or_create", fake_get_or_create)

    async def op(s):
        if s is sess1:
            raise RuntimeError("Connection closed while reading from the driver")
        return f"ok:{s.name}"

    assert asyncio.run(browser_tools._run_with_retry("cwd", op)) == "ok:fresh"
    assert calls["n"] == 2


def test_run_with_retry_does_not_retry_on_a_normal_usage_error(monkeypatch):
    calls = {"n": 0}

    async def fake_get_or_create(cwd):
        calls["n"] += 1
        return _FakeSession("only")
    monkeypatch.setattr(browser_tools._browser_pane, "get_or_create", fake_get_or_create)

    async def op(s):
        raise Exception('Timeout 10000ms exceeded waiting for selector "#nope"')

    try:
        asyncio.run(browser_tools._run_with_retry("cwd", op))
        assert False, "must re-raise"
    except Exception as e:
        assert "Timeout" in str(e)
    assert calls["n"] == 1, "a bad selector must not spend a second get_or_create/retry"


def test_run_with_retry_gives_up_if_the_session_did_not_actually_change(monkeypatch):
    sess = _FakeSession("stuck")

    async def fake_get_or_create(cwd):
        return sess  # same object every time — retirement did not actually happen
    monkeypatch.setattr(browser_tools._browser_pane, "get_or_create", fake_get_or_create)

    async def op(s):
        raise RuntimeError("Connection closed while reading from the driver")

    try:
        asyncio.run(browser_tools._run_with_retry("cwd", op))
        assert False, "must re-raise instead of looping"
    except RuntimeError:
        pass
