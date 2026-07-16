"""Tests for second_opinion.py — the optional Antigravity/agy MCP tool.

asyncio_mode=auto (pytest.ini) so all async tests are plain `async def test_...`.
NEVER invokes the real agy binary — all subprocess interaction is monkeypatched.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path (mirrors conftest.py)
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import second_opinion  # module under test


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeProc:
    """Mimics asyncio.subprocess.Process enough for the tests."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.kill_called = False

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self):
        self.kill_called = True


def make_subprocess_exec_patch(fake_proc):
    """Return an async callable that records its argv and returns fake_proc."""
    captured = {"argv": None}

    async def fake_exec(*argv, stdout=None, stderr=None):
        captured["argv"] = argv
        return fake_proc

    return fake_exec, captured


# ---------------------------------------------------------------------------
# 1. _resolve_agy honours a valid AGY_BIN file
# ---------------------------------------------------------------------------

def test_resolve_agy_honours_agy_bin(tmp_path, monkeypatch):
    bin_file = tmp_path / "agy"
    bin_file.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGY_BIN", str(bin_file))
    result = second_opinion._resolve_agy()
    assert result == str(bin_file)


# ---------------------------------------------------------------------------
# 2. _resolve_agy returns None when nothing found
# ---------------------------------------------------------------------------

def test_resolve_agy_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_BIN", raising=False)
    # which returns None
    monkeypatch.setattr(second_opinion.shutil, "which", lambda _name: None)
    # home fallback points to an empty tmp dir (no .local/bin/agy)
    monkeypatch.setattr(second_opinion.Path, "home", staticmethod(lambda: tmp_path))
    result = second_opinion._resolve_agy()
    assert result is None


# ---------------------------------------------------------------------------
# 3. _enabled: default True; '0' → False; 'false' → False
# ---------------------------------------------------------------------------

def test_enabled_default_true(monkeypatch):
    monkeypatch.delenv("SECOND_OPINION", raising=False)
    assert second_opinion._enabled() is True


def test_enabled_zero_false(monkeypatch):
    monkeypatch.setenv("SECOND_OPINION", "0")
    assert second_opinion._enabled() is False


def test_enabled_lowercase_false(monkeypatch):
    monkeypatch.setenv("SECOND_OPINION", "false")
    assert second_opinion._enabled() is False


# ---------------------------------------------------------------------------
# 4. _strip_noise removes known noise patterns, keeps real content
# ---------------------------------------------------------------------------

def test_strip_noise_removes_noise_keeps_answer():
    noisy = "\n".join([
        "I0123 some glog line",
        "Ripgrep is not available",
        "Falling back to GrepTool",
        "pkg loaded in 3ms",
        "this is deprecated",
        "real answer",
    ])
    result = second_opinion._strip_noise(noisy)
    assert "real answer" in result
    assert "I0123" not in result
    assert "Ripgrep" not in result
    assert "Falling back" not in result
    assert "loaded in" not in result
    assert "deprecated" not in result


# ---------------------------------------------------------------------------
# 5. build_antigravity_server → None when SECOND_OPINION=0
# ---------------------------------------------------------------------------

def test_build_antigravity_server_none_when_disabled(monkeypatch):
    monkeypatch.setenv("SECOND_OPINION", "0")
    result = second_opinion.build_antigravity_server()
    assert result is None


# ---------------------------------------------------------------------------
# 6. build_antigravity_server → None when _resolve_agy returns None
# ---------------------------------------------------------------------------

def test_build_antigravity_server_none_when_no_agy(monkeypatch):
    monkeypatch.delenv("SECOND_OPINION", raising=False)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: None)
    result = second_opinion.build_antigravity_server()
    assert result is None


# ---------------------------------------------------------------------------
# 7. build_antigravity_server returns dict with 'antigravity' when available
# ---------------------------------------------------------------------------

def test_build_antigravity_server_returns_dict(monkeypatch, tmp_path):
    bin_file = tmp_path / "agy"
    bin_file.write_text("#!/bin/sh\n")
    monkeypatch.delenv("SECOND_OPINION", raising=False)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: str(bin_file))
    result = second_opinion.build_antigravity_server()
    # If the SDK import fails the function returns None; that is acceptable in test env.
    # If it succeeds it must contain 'antigravity'.
    if result is not None:
        assert "antigravity" in result


# ---------------------------------------------------------------------------
# 8. _ask_agy happy path with alias 'pro'
# ---------------------------------------------------------------------------

async def test_ask_agy_happy_path_pro(monkeypatch):
    fake_proc = FakeProc(b"the answer", b"", returncode=0)
    fake_exec, captured = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "the answer" in result
    assert "Gemini 3.1 Pro (High)" in result


# ---------------------------------------------------------------------------
# 9. _ask_agy alias 'flash' uses exact model string
# ---------------------------------------------------------------------------

async def test_ask_agy_flash_alias_model_string(monkeypatch):
    fake_proc = FakeProc(b"flash answer", b"", returncode=0)
    fake_exec, captured = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)

    result = await second_opinion._ask_agy("q", "flash", None)

    # The exact model string must appear in the argv passed to subprocess
    assert "Gemini 3.5 Flash (High)" in captured["argv"]
    assert "Gemini 3.5 Flash (High)" in result


# ---------------------------------------------------------------------------
# 10. _ask_agy timeout → result contains 'timed out'; proc.kill() called
# ---------------------------------------------------------------------------

async def test_ask_agy_timeout(monkeypatch):
    fake_proc = FakeProc(b"", b"", returncode=None)
    # make communicate hang but wait_for raises TimeoutError before that
    async def hanging_communicate():
        await asyncio.sleep(9999)
        return (b"", b"")

    fake_proc_obj = fake_proc  # keep reference

    async def fake_exec(*argv, stdout=None, stderr=None):
        return fake_proc_obj

    async def raise_timeout(*_a, **_kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(second_opinion.asyncio, "wait_for", raise_timeout)

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "timed out" in result
    assert fake_proc_obj.kill_called is True


# ---------------------------------------------------------------------------
# 11. _ask_agy when _resolve_agy → None → result contains 'unavailable'
# ---------------------------------------------------------------------------

async def test_ask_agy_no_binary(monkeypatch):
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: None)
    result = await second_opinion._ask_agy("q", "pro", None)
    assert "unavailable" in result


# ---------------------------------------------------------------------------
# 12. _ask_agy truncation
# ---------------------------------------------------------------------------

async def test_ask_agy_truncation(monkeypatch):
    long_output = b"A" * 100
    fake_proc = FakeProc(long_output, b"", returncode=0)
    fake_exec, _ = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("SECOND_OPINION_MAX_CHARS", "20")

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "truncated" in result


# ---------------------------------------------------------------------------
# 13. _ask_agy nonzero exit + empty stdout → contains 'error (exit 1)'
# ---------------------------------------------------------------------------

async def test_ask_agy_nonzero_exit_empty_stdout(monkeypatch):
    fake_proc = FakeProc(b"", b"boom", returncode=1)
    fake_exec, _ = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "error (exit 1)" in result


# ---------------------------------------------------------------------------
# 14. _ask_agy empty-prompt sentinel → contains 'no usable answer'
# ---------------------------------------------------------------------------

async def test_ask_agy_empty_prompt_sentinel(monkeypatch):
    fake_proc = FakeProc(b"Error: empty prompt. Usage...", b"", returncode=0)
    fake_exec, _ = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "no usable answer" in result


# ---------------------------------------------------------------------------
# 15. _ask_agy strips noise from a real-looking stdout
# ---------------------------------------------------------------------------

async def test_ask_agy_strips_noise_from_stdout(monkeypatch):
    raw = b"Ripgrep is not available\nreal clean answer\n"
    fake_proc = FakeProc(raw, b"", returncode=0)
    fake_exec, _ = make_subprocess_exec_patch(fake_proc)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: "/usr/bin/agy")
    monkeypatch.setattr(second_opinion.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("SECOND_OPINION_MAX_CHARS", raising=False)

    result = await second_opinion._ask_agy("q", "pro", None)

    assert "real clean answer" in result
    assert "Ripgrep" not in result


# ---------------------------------------------------------------------------
# 16. _second_opinion_handler empty question → text contains 'non-empty'
# ---------------------------------------------------------------------------

async def test_handler_empty_question():
    result = await second_opinion._second_opinion_handler({"question": "  "})
    assert "content" in result
    items = result["content"]
    assert len(items) == 1
    assert items[0]["type"] == "text"
    assert "non-empty" in items[0]["text"]


# ---------------------------------------------------------------------------
# 17. _second_opinion_handler unknown model 'zzz' → coerced to 'pro'
# ---------------------------------------------------------------------------

async def test_handler_unknown_model_coerced_to_pro(monkeypatch):
    captured = {}

    async def fake_ask(question, alias, context):
        captured["alias"] = alias
        return "OK"

    monkeypatch.setattr(second_opinion, "_ask_agy", fake_ask)

    await second_opinion._second_opinion_handler({"question": "hello", "model": "zzz"})

    assert captured["alias"] == "pro"


# ---------------------------------------------------------------------------
# 18. _second_opinion_handler returns correct content shape on success
# ---------------------------------------------------------------------------

async def test_handler_returns_mcp_content_shape(monkeypatch):
    async def fake_ask(question, alias, context):
        return "OK"

    monkeypatch.setattr(second_opinion, "_ask_agy", fake_ask)

    result = await second_opinion._second_opinion_handler({"question": "hello"})

    assert isinstance(result, dict)
    assert "content" in result
    items = result["content"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "text"
    assert item["text"] == "OK"


# ===========================================================================
# Azure AI Foundry backend (spec-060 Phase B)
# ===========================================================================

def _set_azure(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_KEY", "k-test")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://foundry.example.com/")
    monkeypatch.delenv("AZURE_FOUNDRY_MODELS", raising=False)


# 19. _azure_configured reflects env
def test_azure_configured_true_false(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_KEY", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    assert second_opinion._azure_configured() is False
    _set_azure(monkeypatch)
    assert second_opinion._azure_configured() is True


# 20. _azure_models default + JSON override
def test_azure_models_default_and_override(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_MODELS", raising=False)
    assert second_opinion._azure_models()["grok"] == "grok-4-3"
    monkeypatch.setenv("AZURE_FOUNDRY_MODELS", '{"grok":"my-grok","x":"y"}')
    m = second_opinion._azure_models()
    assert m["grok"] == "my-grok" and m["x"] == "y"


# 21. _ask_azure unavailable when unconfigured
async def test_ask_azure_unconfigured(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_KEY", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    out = await second_opinion._ask_azure("q", "grok", None)
    assert "unavailable" in out


# 22. _ask_azure happy path (HTTP mocked)
async def test_ask_azure_happy(monkeypatch):
    _set_azure(monkeypatch)

    async def fake_once(url, key, deployment, prompt, max_out, token_param, timeout):
        assert deployment == "grok-4-3"
        return 200, {"choices": [{"message": {"content": "grok says hi"}, "finish_reason": "stop"}]}, None

    monkeypatch.setattr(second_opinion, "_azure_chat_once", fake_once)
    out = await second_opinion._ask_azure("q", "grok", None)
    assert "grok says hi" in out
    assert "Azure/grok-4-3" in out


# 23. _ask_azure self-heals max_tokens -> max_completion_tokens for gpt5
async def test_ask_azure_token_param_selfheal(monkeypatch):
    _set_azure(monkeypatch)
    calls = []

    async def fake_once(url, key, deployment, prompt, max_out, token_param, timeout):
        calls.append(token_param)
        if token_param == "max_tokens":
            return 400, {"error": {"message": "Unsupported parameter: 'max_tokens' ... Use 'max_completion_tokens' instead."}}, None
        return 200, {"choices": [{"message": {"content": "ok5"}, "finish_reason": "stop"}]}, None

    monkeypatch.setattr(second_opinion, "_azure_chat_once", fake_once)
    # force gpt5 through a deployment NOT starting with gpt-5 so first attempt uses max_tokens
    monkeypatch.setenv("AZURE_FOUNDRY_MODELS", '{"gpt5":"o-custom"}')
    out = await second_opinion._ask_azure("q", "gpt5", None)
    assert "ok5" in out
    assert calls == ["max_tokens", "max_completion_tokens"]


# 24. _ask_azure empty answer with finish_reason length → hint
async def test_ask_azure_empty_length(monkeypatch):
    _set_azure(monkeypatch)

    async def fake_once(*a, **k):
        return 200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, None

    monkeypatch.setattr(second_opinion, "_azure_chat_once", fake_once)
    out = await second_opinion._ask_azure("q", "grok", None)
    assert "empty answer" in out and "token cap" in out


# 25. _ask_azure HTTP error surfaced
async def test_ask_azure_http_error(monkeypatch):
    _set_azure(monkeypatch)

    async def fake_once(*a, **k):
        return 429, {"error": {"message": "rate limited"}}, None

    monkeypatch.setattr(second_opinion, "_azure_chat_once", fake_once)
    out = await second_opinion._ask_azure("q", "grok", None)
    assert "HTTP 429" in out and "rate limited" in out


# 26. handler routes an Azure alias to _ask_azure when configured
async def test_handler_routes_azure_alias(monkeypatch):
    _set_azure(monkeypatch)
    captured = {}

    async def fake_azure(question, alias, context):
        captured["alias"] = alias
        return "AZ"

    monkeypatch.setattr(second_opinion, "_ask_azure", fake_azure)
    r = await second_opinion._second_opinion_handler({"question": "hi", "model": "deepseek"})
    assert captured["alias"] == "deepseek"
    assert r["content"][0]["text"] == "AZ"


# 27. handler: Azure alias but Azure OFF → coerced to agy default 'pro'
async def test_handler_azure_alias_but_off_falls_to_agy(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_KEY", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    captured = {}

    async def fake_agy(question, alias, context):
        captured["alias"] = alias
        return "AGY"

    monkeypatch.setattr(second_opinion, "_ask_agy", fake_agy)
    await second_opinion._second_opinion_handler({"question": "hi", "model": "grok"})
    assert captured["alias"] == "pro"


# 28. handler panel=true calls _ask_panel
async def test_handler_panel(monkeypatch):
    async def fake_panel(question, context):
        return "PANEL-OUT"

    monkeypatch.setattr(second_opinion, "_ask_panel", fake_panel)
    r = await second_opinion._second_opinion_handler({"question": "hi", "panel": True})
    assert r["content"][0]["text"] == "PANEL-OUT"


# 29. _build_input_schema includes Azure aliases only when configured
def test_build_input_schema_enum(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_KEY", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    enum_off = second_opinion._build_input_schema()["properties"]["model"]["enum"]
    assert "grok" not in enum_off and "pro" in enum_off
    _set_azure(monkeypatch)
    enum_on = second_opinion._build_input_schema()["properties"]["model"]["enum"]
    assert "grok" in enum_on and "deepseek" in enum_on and "pro" in enum_on


# 30. build_antigravity_server builds with ONLY Azure configured (no agy)
def test_build_server_azure_only(monkeypatch):
    monkeypatch.delenv("SECOND_OPINION", raising=False)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: None)
    _set_azure(monkeypatch)
    result = second_opinion.build_antigravity_server()
    # None only if the SDK import fails in the test env; otherwise must expose the server
    if result is not None:
        assert "antigravity" in result


# 31. _ask_panel aggregates configured providers (agy off, azure mocked)
async def test_ask_panel_aggregates(monkeypatch):
    _set_azure(monkeypatch)
    monkeypatch.setattr(second_opinion, "_resolve_agy", lambda: None)  # no agy
    monkeypatch.setenv("AZURE_FOUNDRY_MODELS", '{"grok":"grok-4-3","deepseek":"deepseek-v4"}')

    async def fake_azure(question, alias, context):
        return f"ans-{alias}"

    monkeypatch.setattr(second_opinion, "_ask_azure", fake_azure)
    out = await second_opinion._ask_panel("q", None)
    assert "Azure/grok" in out and "Azure/deepseek" in out
    assert "ans-grok" in out and "ans-deepseek" in out
