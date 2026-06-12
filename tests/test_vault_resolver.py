"""
Tests for the vault: secret reference resolver (Spec 026, Phase 3a).

Covers:
- Plain values pass through unchanged (no vault: prefix).
- vault:<name> with a single exact match resolves to the password.
- vault:<name> with multiple exact-name matches raises RuntimeError.
- vault:<name> with zero exact-name matches raises RuntimeError.
- vw non-zero exit raises RuntimeError.
- vw timeout raises RuntimeError.
- Exact-name matching: vault:crm must NOT match an item named 'crm.coscore.us'.
- In-memory TTL cache: second resolve within TTL does not spawn vw again.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import _resolve_secret_refs, _resolve_one_vault_ref, _vault_cache, _VW_PATH


# ─────────────────────────── helpers ──────────────────────────────────────────


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    """Return a mock asyncio subprocess that completes with given output."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ─────────────────────────── plain-value pass-through ─────────────────────────


@pytest.mark.asyncio
async def test_plain_values_pass_through_unchanged():
    """Dict with no vault: refs is returned with identical content."""
    secrets = {"API_KEY": "plain_key", "DB_PASS": "hunter2", "COUNT": "42"}
    result = await _resolve_secret_refs(secrets)
    assert result == secrets


@pytest.mark.asyncio
async def test_empty_dict_returns_empty():
    """Empty secrets dict returns empty dict."""
    result = await _resolve_secret_refs({})
    assert result == {}


@pytest.mark.asyncio
async def test_non_vault_prefix_not_resolved():
    """Values that don't start with 'vault:' are never touched."""
    secrets = {"KEY": "vault_prefix_but_no_colon", "OTHER": "totally_plain"}
    result = await _resolve_secret_refs(secrets)
    assert result == secrets


# ─────────────────────────── single exact match → resolves ─────────────────────


@pytest.mark.asyncio
async def test_vault_ref_single_exact_match_resolves(monkeypatch):
    """vault:<name> with exactly one exact-name match resolves to the password."""
    # vw pass output: one line matching exactly
    vw_output = b"My VaultItem: s3cr3tp@ss\n"
    monkeypatch.delitem(_vault_cache, "My VaultItem", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        secrets = {"MY_KEY": "vault:My VaultItem"}
        result = await _resolve_secret_refs(secrets)

    assert result["MY_KEY"] == "s3cr3tp@ss"
    mock_exec.assert_called_once_with(
        _VW_PATH, "pass", "My VaultItem",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# ─────────────────────────── multi-match → raises ─────────────────────────────


@pytest.mark.asyncio
async def test_vault_ref_multi_exact_match_raises(monkeypatch):
    """vault:<name> where vw returns two lines with the same exact name → RuntimeError."""
    # Simulate duplicate item names in the vault
    vw_output = b"dup-item: password_one\ndup-item: password_two\n"
    monkeypatch.delitem(_vault_cache, "dup-item", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        with pytest.raises(RuntimeError, match="2 items"):
            await _resolve_secret_refs({"DUPE": "vault:dup-item"})


# ─────────────────────────── zero-match → raises ──────────────────────────────


@pytest.mark.asyncio
async def test_vault_ref_zero_match_raises(monkeypatch):
    """vault:<name> with no exact-name match in vw output → RuntimeError."""
    # vw returns a different item, not an exact match
    vw_output = b"other-item: somepass\n"
    monkeypatch.delitem(_vault_cache, "missing-item", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        with pytest.raises(RuntimeError, match="no item with exact name"):
            await _resolve_secret_refs({"KEY": "vault:missing-item"})


# ─────────────────────────── vw non-zero exit → raises ────────────────────────


@pytest.mark.asyncio
async def test_vault_ref_vw_nonzero_exit_raises(monkeypatch):
    """vw returns non-zero exit code → RuntimeError naming the key."""
    monkeypatch.delitem(_vault_cache, "some-item", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(1, b"", b"ERR: vault unreachable\n")
        with pytest.raises(RuntimeError, match="exited with code 1"):
            await _resolve_secret_refs({"KEY": "vault:some-item"})


# ─────────────────────────── vw timeout → raises ──────────────────────────────


@pytest.mark.asyncio
async def test_vault_ref_vw_timeout_raises(monkeypatch):
    """asyncio.wait_for timeout → RuntimeError naming the key."""
    monkeypatch.delitem(_vault_cache, "slow-item", raising=False)

    proc = MagicMock()
    proc.returncode = None
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = proc
        with pytest.raises(RuntimeError, match="timed out"):
            await _resolve_secret_refs({"KEY": "vault:slow-item"})


# ─────────────────────────── exact-name: substring must NOT match ─────────────


@pytest.mark.asyncio
async def test_vault_ref_exact_name_no_substring_match(monkeypatch):
    """vault:crm must NOT match an item named 'crm.coscore.us'."""
    # vw does substring search, so querying "crm" returns "crm.coscore.us"
    vw_output = b"crm.coscore.us: somepassword\n"
    monkeypatch.delitem(_vault_cache, "crm", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        with pytest.raises(RuntimeError, match="no item with exact name"):
            await _resolve_secret_refs({"KEY": "vault:crm"})


@pytest.mark.asyncio
async def test_vault_ref_exact_name_matches_not_superset(monkeypatch):
    """vault:crm.coscore.us matches 'crm.coscore.us' exactly, not 'crm'."""
    vw_output = b"crm.coscore.us: therealpass\n"
    monkeypatch.delitem(_vault_cache, "crm.coscore.us", raising=False)

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        result = await _resolve_secret_refs({"KEY": "vault:crm.coscore.us"})

    assert result["KEY"] == "therealpass"


# ─────────────────────────── TTL cache: second call uses cache ─────────────────


@pytest.mark.asyncio
async def test_vault_ref_cache_hit_does_not_call_vw_again(monkeypatch):
    """Second resolve within TTL does NOT spawn vw again (subprocess called once)."""
    item = "cached-item-unique-xyz"
    monkeypatch.delitem(_vault_cache, item, raising=False)

    vw_output = f"{item}: cached_password\n".encode()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)

        # First call — hits vw
        r1 = await _resolve_secret_refs({"K": f"vault:{item}"})
        # Second call — should hit cache, not vw
        r2 = await _resolve_secret_refs({"K": f"vault:{item}"})

    assert r1["K"] == "cached_password"
    assert r2["K"] == "cached_password"
    # vw was called exactly once
    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_vault_ref_expired_cache_calls_vw_again(monkeypatch):
    """After TTL expires the cache entry is ignored and vw is called again."""
    item = "expired-cache-item-unique"
    # Pre-seed the cache with an already-expired entry
    _vault_cache[item] = ("stale_password", time.monotonic() - 1)

    vw_output = f"{item}: fresh_password\n".encode()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        result = await _resolve_secret_refs({"K": f"vault:{item}"})

    assert result["K"] == "fresh_password"
    assert mock_exec.call_count == 1

    # Cleanup
    monkeypatch.delitem(_vault_cache, item, raising=False)


# ─────────────────────────── mixed dict: plain + vault: ───────────────────────


@pytest.mark.asyncio
async def test_mixed_dict_plain_and_vault_refs(monkeypatch):
    """Dict with both plain and vault: values: plain pass-through, vault resolved."""
    item = "mix-test-item-unique"
    monkeypatch.delitem(_vault_cache, item, raising=False)

    vw_output = f"{item}: resolved_val\n".encode()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        mock_exec.return_value = _make_proc(0, vw_output)
        result = await _resolve_secret_refs({
            "PLAIN_KEY": "plaintext_value",
            "VAULT_KEY": f"vault:{item}",
        })

    assert result["PLAIN_KEY"] == "plaintext_value"
    assert result["VAULT_KEY"] == "resolved_val"
