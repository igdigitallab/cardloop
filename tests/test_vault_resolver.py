"""
Tests for the secret: reference resolver (Spec 026, Phase 3).

Replaces the previous vw-based vault: resolver tests.

Covers:
- Plain values pass through unchanged (no secret: prefix).
- secret:<name> where the name exists in the store → resolved to value.
- secret:<name> where the name is absent from the store → RuntimeError (fail loud).
- Mixed dict: plain values pass through, secret: refs are resolved.
- Empty dict returns empty dict.
- Non-secret: prefix (e.g. "vault:") is treated as plain (pass-through).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import secretstore
from webapp import _resolve_secret_refs


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch):
    """Fresh temp store/key for every test."""
    key_path = tmp_path / "secret.key"
    store_path = tmp_path / "vault.enc"
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(key_path))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(store_path))
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    secretstore.init_key()
    yield tmp_path


# ─────────────────────────── plain pass-through ───────────────────────────────


@pytest.mark.asyncio
async def test_plain_values_pass_through_unchanged():
    """Dict with no secret: refs is returned with identical content."""
    secrets = {"API_KEY": "plain_key", "DB_PASS": "hunter2", "COUNT": "42"}
    result = await _resolve_secret_refs(secrets)
    assert result == secrets


@pytest.mark.asyncio
async def test_empty_dict_returns_empty():
    """Empty secrets dict returns empty dict."""
    result = await _resolve_secret_refs({})
    assert result == {}


@pytest.mark.asyncio
async def test_non_secret_prefix_not_resolved():
    """Values that don't start with 'secret:' are never looked up."""
    secrets = {
        "KEY1": "vault:old-format",  # old vw prefix — now just plain text
        "KEY2": "totally_plain",
        "KEY3": "secret_but_no_colon",
    }
    result = await _resolve_secret_refs(secrets)
    assert result == secrets


# ─────────────────────────── secret: resolves ─────────────────────────────────


@pytest.mark.asyncio
async def test_secret_ref_resolves_to_stored_value():
    """secret:<name> resolves to the value stored in the built-in store."""
    secretstore.set("my-api-key", "resolved_value_xyz")
    result = await _resolve_secret_refs({"KEY": "secret:my-api-key"})
    assert result["KEY"] == "resolved_value_xyz"


@pytest.mark.asyncio
async def test_secret_ref_missing_raises_runtime_error():
    """secret:<name> for an absent key raises RuntimeError (fail loud, never silent empty)."""
    with pytest.raises(RuntimeError, match="not found in the built-in secret store"):
        await _resolve_secret_refs({"KEY": "secret:nonexistent-key"})


@pytest.mark.asyncio
async def test_secret_ref_error_mentions_key_name():
    """RuntimeError for a missing secret mentions the secret name."""
    with pytest.raises(RuntimeError, match="special-missing-key"):
        await _resolve_secret_refs({"MYENV": "secret:special-missing-key"})


# ─────────────────────────── mixed dict ───────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_dict_plain_and_secret_refs():
    """Dict with both plain and secret: values: plain pass-through, secret: resolved."""
    secretstore.set("the-token", "resolved_token_value")
    result = await _resolve_secret_refs({
        "PLAIN_KEY": "plaintext_value",
        "VAULT_KEY": "secret:the-token",
    })
    assert result["PLAIN_KEY"] == "plaintext_value"
    assert result["VAULT_KEY"] == "resolved_token_value"


@pytest.mark.asyncio
async def test_mixed_dict_one_missing_raises():
    """If any secret: ref is missing, the whole resolve raises (do not partially resolve)."""
    secretstore.set("present-key", "some_value")
    with pytest.raises(RuntimeError):
        await _resolve_secret_refs({
            "A": "secret:present-key",
            "B": "secret:missing-key",  # this one is absent
        })


# ─────────────────────────── non-string values ────────────────────────────────


@pytest.mark.asyncio
async def test_non_string_values_pass_through():
    """Non-string values (int, None, bool) are never processed as secret: refs."""
    secrets = {"NUM": 42, "FLAG": True, "EMPTY": None}
    result = await _resolve_secret_refs(secrets)
    assert result == secrets
