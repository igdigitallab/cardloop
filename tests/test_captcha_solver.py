"""2captcha bridge — unit tests for the network half (no real API calls).

The page-side half (detect widget → inject token) is covered in
test_browser_pane.py; this file covers key resolution, task building, the poll
loop, and the failure paths that must NOT silently return a bogus token.
"""
import asyncio

import pytest

import captcha_solver as cs


# ── key resolution: env wins, safe is the fallback ─────────────────────────────

def test_env_key_takes_priority_over_the_safe(monkeypatch):
    monkeypatch.setenv(cs.API_KEY_ENV, "from-env")
    monkeypatch.setattr(cs, "_secretstore", type("S", (), {"get": staticmethod(lambda n: "from-safe")}))
    assert cs.api_key() == "from-env"


def test_safe_is_used_when_env_is_unset(monkeypatch):
    monkeypatch.delenv(cs.API_KEY_ENV, raising=False)
    monkeypatch.setattr(cs, "_secretstore", type("S", (), {"get": staticmethod(lambda n: "from-safe")}))
    assert cs.api_key() == "from-safe"
    assert cs.configured() is True


def test_blank_env_key_falls_through_rather_than_masking_the_safe(monkeypatch):
    """An empty TWOCAPTCHA_API_KEY= line in .env must not shadow a working safe entry."""
    monkeypatch.setenv(cs.API_KEY_ENV, "   ")
    monkeypatch.setattr(cs, "_secretstore", type("S", (), {"get": staticmethod(lambda n: "from-safe")}))
    assert cs.api_key() == "from-safe"


def test_no_key_anywhere_is_not_configured(monkeypatch):
    monkeypatch.delenv(cs.API_KEY_ENV, raising=False)
    monkeypatch.setattr(cs, "_secretstore", None)
    assert cs.api_key() is None
    assert cs.configured() is False
    with pytest.raises(cs.CaptchaError, match="No 2captcha API key"):
        cs._require_key()


# ── task building ──────────────────────────────────────────────────────────────

def test_build_task_maps_kind_to_the_2captcha_task_type():
    task = cs.build_task("recaptcha_v2", "https://x.test/login", "6Lc-KEY")
    assert task == {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": "https://x.test/login",
        "websiteKey": "6Lc-KEY",
    }


def test_build_task_passes_extra_params_through():
    task = cs.build_task("recaptcha_v3", "https://x.test", "KEY", {"pageAction": "login"})
    assert task["type"] == "RecaptchaV3TaskProxyless"
    assert task["pageAction"] == "login"


def test_build_task_drops_none_extras_rather_than_sending_nulls():
    """2captcha rejects the whole task with ERROR_BAD_PARAMETERS on a null field,
    so an absent data-callback/data-action must be omitted, not sent as None."""
    task = cs.build_task("turnstile", "https://x.test", "0xKEY", {"action": None})
    assert "action" not in task


def test_image_task_carries_the_body_and_no_sitekey():
    task = cs.build_task("image", "https://x.test", "", {"body": "BASE64"})
    assert task == {"type": "ImageToTextTask", "body": "BASE64"}


def test_image_task_without_a_body_is_refused():
    with pytest.raises(cs.CaptchaError, match="base64 body"):
        cs.build_task("image", "https://x.test", "")


def test_unknown_kind_is_refused_with_the_known_list():
    with pytest.raises(cs.CaptchaError, match="Unsupported captcha kind"):
        cs.build_task("funcaptcha", "https://x.test", "KEY")


# ── token extraction ───────────────────────────────────────────────────────────

def test_token_of_handles_each_captcha_types_field_name():
    assert cs.token_of({"gRecaptchaResponse": "A"}) == "A"
    assert cs.token_of({"token": "B"}) == "B"
    assert cs.token_of({"text": "C"}) == "C"


def test_token_of_refuses_a_solution_with_no_token():
    with pytest.raises(cs.CaptchaError, match="no token field"):
        cs.token_of({"cookies": {}})


# ── the solve loop ─────────────────────────────────────────────────────────────

def _fake_post(script):
    """Build a _post stand-in from a list of canned responses, recording calls."""
    calls = []

    async def _post(path, payload):
        calls.append((path, payload))
        return script.pop(0)
    _post.calls = calls
    return _post


def _no_sleep(monkeypatch):
    async def _sleep(_s):
        return None
    monkeypatch.setattr(cs.asyncio, "sleep", _sleep)


def test_solve_polls_until_ready_and_reports_cost(monkeypatch):
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    _no_sleep(monkeypatch)
    post = _fake_post([
        {"errorId": 0, "taskId": 42},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "cost": "0.00299", "solution": {"gRecaptchaResponse": "TOK"}},
    ])
    monkeypatch.setattr(cs, "_post", post)

    sol = asyncio.run(cs.solve({"type": "RecaptchaV2TaskProxyless"}))
    assert cs.token_of(sol) == "TOK"
    assert sol["_cost"] == "0.00299"
    assert [c[0] for c in post.calls] == ["/createTask", "/getTaskResult", "/getTaskResult"]
    assert post.calls[1][1] == {"clientKey": "K", "taskId": 42}


def test_solve_raises_on_a_rejected_task_instead_of_polling_forever(monkeypatch):
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    _no_sleep(monkeypatch)
    monkeypatch.setattr(cs, "_post", _fake_post([
        {"errorId": 110, "errorCode": "ERROR_BAD_PARAMETERS", "errorDescription": "missing params"},
    ]))
    with pytest.raises(cs.CaptchaError, match="ERROR_BAD_PARAMETERS"):
        asyncio.run(cs.solve({"type": "TurnstileTaskProxyless"}))


def test_solve_raises_when_the_workers_cannot_solve_it(monkeypatch):
    """An errorId mid-poll is a terminal refusal ('unsolvable'), not a retry signal —
    it must surface, never be mistaken for 'still processing'."""
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    _no_sleep(monkeypatch)
    monkeypatch.setattr(cs, "_post", _fake_post([
        {"errorId": 0, "taskId": 7},
        {"errorId": 12, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE", "errorDescription": "unsolvable"},
    ]))
    with pytest.raises(cs.CaptchaError, match="ERROR_CAPTCHA_UNSOLVABLE"):
        asyncio.run(cs.solve({"type": "ImageToTextTask"}))


def test_solve_times_out_rather_than_hanging_the_whole_turn(monkeypatch):
    """A task stuck in 'processing' must end the call, not spin until the SDK's own
    turn ceiling kills the run. budget=0 makes the deadline bite on the first poll
    (patching time.monotonic would also skew asyncio's event loop clock)."""
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    _no_sleep(monkeypatch)
    polls = []

    async def _post(path, payload):
        if path == "/createTask":
            return {"errorId": 0, "taskId": 1}
        polls.append(payload)
        return {"errorId": 0, "status": "processing"}
    monkeypatch.setattr(cs, "_post", _post)

    with pytest.raises(cs.CaptchaError, match="did not solve it within"):
        asyncio.run(cs.solve({"type": "RecaptchaV2TaskProxyless"}, budget=0))
    assert len(polls) == 1  # gave up immediately, didn't keep hammering the API


def test_solve_raises_when_createtask_returns_no_task_id(monkeypatch):
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    _no_sleep(monkeypatch)
    monkeypatch.setattr(cs, "_post", _fake_post([{"errorId": 0}]))
    with pytest.raises(cs.CaptchaError, match="no taskId"):
        asyncio.run(cs.solve({"type": "RecaptchaV2TaskProxyless"}))


def test_balance_surfaces_an_api_error(monkeypatch):
    monkeypatch.setenv(cs.API_KEY_ENV, "K")
    monkeypatch.setattr(cs, "_post", _fake_post([
        {"errorId": 1, "errorDescription": "ERROR_KEY_DOES_NOT_EXIST"},
    ]))
    with pytest.raises(cs.CaptchaError, match="ERROR_KEY_DOES_NOT_EXIST"):
        asyncio.run(cs.balance())
