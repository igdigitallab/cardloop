"""
Tests for the built-in encrypted secret store (Spec 026, Phase 3).

All tests use isolated temp dirs via env-var overrides — they never touch the
real keyfile (~/.config/claude-ops/secret.key) or real store (data/vault/secrets.enc).
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import secretstore


# ─────────────────────────── fixtures ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point secretstore at a fresh temp directory for every test."""
    key_path = tmp_path / "secret.key"
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(key_path))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(store_path))
    # Remove the env-key override so we use the keyfile path
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    yield tmp_path


@pytest.fixture
def initialized_store(tmp_path):
    """A store that has been initialised with a key."""
    secretstore.init_key()
    return tmp_path


# ─────────────────────────── init_key ─────────────────────────────────────────


def test_init_key_creates_keyfile(tmp_path):
    """init_key() writes a valid Fernet key to the keyfile."""
    path = secretstore.init_key()
    kf = Path(path)
    assert kf.exists()
    key_bytes = kf.read_bytes().strip()
    # A Fernet key is 44 base64-url characters encoded to bytes
    assert len(key_bytes) == 44


def test_init_key_mode_0600(tmp_path):
    """init_key() sets the keyfile mode to 0600 (owner-only r/w)."""
    path = secretstore.init_key()
    kf = Path(path)
    mode = kf.stat().st_mode
    assert not (mode & stat.S_IRGRP), "group read must be off"
    assert not (mode & stat.S_IWGRP), "group write must be off"
    assert not (mode & stat.S_IROTH), "other read must be off"
    assert not (mode & stat.S_IWOTH), "other write must be off"


def test_init_key_refuses_overwrite_without_force():
    """init_key() raises FileExistsError if keyfile already exists."""
    secretstore.init_key()
    with pytest.raises(FileExistsError):
        secretstore.init_key()


def test_init_key_force_overwrites():
    """init_key(force=True) replaces an existing keyfile."""
    path1 = secretstore.init_key()
    key1 = Path(path1).read_bytes()
    # force=True must succeed
    path2 = secretstore.init_key(force=True)
    key2 = Path(path2).read_bytes()
    # The key is regenerated (may differ; at minimum no error)
    assert path1 == path2  # same path


def test_init_key_returns_path_string():
    """init_key() returns the keyfile path as a string."""
    result = secretstore.init_key()
    assert isinstance(result, str)
    assert Path(result).exists()


# ─────────────────────────── missing key error ────────────────────────────────


def test_missing_key_raises_runtime_error_with_guidance(tmp_path, monkeypatch):
    """If no key is found, set() raises a RuntimeError naming 'secret init'.

    Note: get() on a non-existent store returns None without needing the key
    (the store file is absent so there is nothing to decrypt). The key is only
    required when writing (set) or decrypting an existing store.
    """
    # Ensure neither env-key nor keyfile exists
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    missing_kf = tmp_path / "nonexistent" / "secret.key"
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(missing_kf))

    with pytest.raises(RuntimeError, match="secret init"):
        secretstore.set("any-name", "any-value")


# ─────────────────────────── set / get round-trip ─────────────────────────────


def test_set_get_roundtrip(initialized_store):
    """set() then get() returns the exact value stored."""
    secretstore.set("my-key", "s3cr3t_value")
    assert secretstore.get("my-key") == "s3cr3t_value"


def test_set_updates_existing(initialized_store):
    """set() on an existing name replaces the value."""
    secretstore.set("key", "v1")
    secretstore.set("key", "v2")
    assert secretstore.get("key") == "v2"


def test_get_absent_returns_none(initialized_store):
    """get() returns None for a name that has not been stored."""
    assert secretstore.get("never-stored") is None


def test_set_unicode_value(initialized_store):
    """Unicode values survive the round-trip correctly."""
    secretstore.set("uni", "pâss-🔐-émoji")
    assert secretstore.get("uni") == "pâss-🔐-émoji"


# ─────────────────────────── encryption at rest ───────────────────────────────


def test_store_file_is_not_plaintext(initialized_store, monkeypatch):
    """The on-disk store file does NOT contain the plaintext secret value."""
    secretstore.set("hidden-key", "VERY_SECRET_VALUE_XYZ_9999")

    store_path = Path(os.environ["CLAUDE_OPS_SECRET_STORE"])
    raw_bytes = store_path.read_bytes()

    # The plaintext value must NOT appear in the raw encrypted blob
    assert b"VERY_SECRET_VALUE_XYZ_9999" not in raw_bytes, \
        "Secret value found as plaintext in the encrypted store file!"


def test_store_file_is_not_plaintext_name_either(initialized_store):
    """Even the secret name should not appear as a recognisable plaintext token."""
    secretstore.set("UNIQUE_NAME_CANARY_TEST", "some_value")
    store_path = Path(os.environ["CLAUDE_OPS_SECRET_STORE"])
    raw_bytes = store_path.read_bytes()
    assert b"UNIQUE_NAME_CANARY_TEST" not in raw_bytes, \
        "Secret name found as plaintext in the encrypted store file!"


# ─────────────────────────── list_meta ───────────────────────────────────────


def test_list_meta_returns_names_and_categories(initialized_store):
    """list_meta() returns name and category for each entry."""
    secretstore.set("alpha", "va1", category="api")
    secretstore.set("beta", "vb1", category="db")

    metas = secretstore.list_meta()
    names = [m["name"] for m in metas]
    assert "alpha" in names
    assert "beta" in names

    for m in metas:
        assert "name" in m
        assert "category" in m


def test_list_meta_no_values(initialized_store):
    """list_meta() NEVER includes 'value' in any entry."""
    secretstore.set("my-secret", "DO_NOT_LEAK", category="api")
    metas = secretstore.list_meta()
    for m in metas:
        assert "value" not in m, f"Value leaked in list_meta() entry: {m}"
        # Also check it's not embedded in any string field
        for field in ("name", "category", "notes", "updated_at"):
            assert "DO_NOT_LEAK" not in str(m.get(field, "")), \
                f"Secret value leaked in field '{field}' of list_meta()!"


def test_list_meta_empty_store(initialized_store):
    """list_meta() returns an empty list when no secrets are stored."""
    assert secretstore.list_meta() == []


# ─────────────────────────── delete ───────────────────────────────────────────


def test_delete_existing(initialized_store):
    """delete() removes the entry and returns True."""
    secretstore.set("to-rm", "val")
    assert secretstore.delete("to-rm") is True
    assert secretstore.get("to-rm") is None


def test_delete_nonexistent_returns_false(initialized_store):
    """delete() returns False when the name is not in the store."""
    assert secretstore.delete("ghost") is False


def test_delete_leaves_others_intact(initialized_store):
    """delete() does not disturb other entries."""
    secretstore.set("keep", "keeper_value")
    secretstore.set("remove", "goner_value")
    secretstore.delete("remove")
    assert secretstore.get("keep") == "keeper_value"
    assert secretstore.get("remove") is None


# ─────────────────────────── bad name validation ──────────────────────────────


@pytest.mark.parametrize("bad_name", [
    "",
    "../etc/passwd",
    "has space",
    "has/slash",
    "has\\back",
    "A" * 129,            # too long
    "\x00null",
    "newline\n",
])
def test_set_bad_name_raises_value_error(initialized_store, bad_name):
    """set() with an invalid name raises ValueError."""
    with pytest.raises(ValueError, match="invalid secret name"):
        secretstore.set(bad_name, "value")


@pytest.mark.parametrize("bad_name", [
    "",
    "../etc/passwd",
    "has space",
])
def test_get_bad_name_raises_value_error(initialized_store, bad_name):
    """get() with an invalid name raises ValueError."""
    with pytest.raises(ValueError, match="invalid secret name"):
        secretstore.get(bad_name)


@pytest.mark.parametrize("bad_name", [
    "",
    "has/slash",
])
def test_delete_bad_name_raises_value_error(initialized_store, bad_name):
    """delete() with an invalid name raises ValueError."""
    with pytest.raises(ValueError, match="invalid secret name"):
        secretstore.delete(bad_name)


@pytest.mark.parametrize("good_name", [
    "simple",
    "MY_KEY",
    "key.with.dots",
    "key-with-dashes",
    "Mixed123",
    "a",
    "A" * 128,
])
def test_set_good_names_accepted(initialized_store, good_name):
    """Valid names are accepted without error."""
    secretstore.set(good_name, "v")  # should not raise


# ─────────────────────────── get_full ─────────────────────────────────────────


def test_get_full_returns_value_and_meta(initialized_store):
    """get_full() returns value, category, notes, and updated_at."""
    secretstore.set("full-key", "full-value", category="api", notes="used by X")
    entry = secretstore.get_full("full-key")
    assert entry is not None
    assert entry["value"] == "full-value"
    assert entry["category"] == "api"
    assert entry["notes"] == "used by X"
    assert "updated_at" in entry


def test_get_full_absent_returns_none(initialized_store):
    """get_full() returns None for absent secrets."""
    assert secretstore.get_full("never-set") is None


# ─────────────────────────── import_env ───────────────────────────────────────


def test_import_env_dotenv_style(initialized_store, tmp_path):
    """import_env() parses KEY=value lines and returns the count."""
    env_file = tmp_path / "creds.env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "API_KEY=abc123\n"
        "DB_PASS=hunter2\n"
        "  SPACED = also works  \n"
    )
    count = secretstore.import_env(str(env_file))
    assert count == 3
    assert secretstore.get("API_KEY") == "abc123"
    assert secretstore.get("DB_PASS") == "hunter2"
    assert secretstore.get("SPACED") == "also works"


def test_import_env_json_style(initialized_store, tmp_path):
    """import_env() parses a JSON object and returns the count."""
    json_file = tmp_path / "creds.json"
    json_file.write_text(json.dumps({
        "stripe-key": "sk_test_abc",
        "openai-key": "sk-openai-xyz",
    }))
    count = secretstore.import_env(str(json_file))
    assert count == 2
    assert secretstore.get("stripe-key") == "sk_test_abc"
    assert secretstore.get("openai-key") == "sk-openai-xyz"


def test_import_env_skips_invalid_names(initialized_store, tmp_path):
    """import_env() skips names that fail validation rather than aborting."""
    env_file = tmp_path / "mixed.env"
    env_file.write_text(
        "valid-key=good_value\n"
        "bad name=skipped\n"        # space in name — invalid
        "has/slash=skipped\n"       # slash — invalid
    )
    count = secretstore.import_env(str(env_file))
    assert count == 1
    assert secretstore.get("valid-key") == "good_value"


def test_import_env_overwrites_existing(initialized_store, tmp_path):
    """import_env() updates existing entries."""
    secretstore.set("my-key", "old-value")
    env_file = tmp_path / "update.env"
    env_file.write_text("my-key=new-value\n")
    count = secretstore.import_env(str(env_file))
    assert count == 1
    assert secretstore.get("my-key") == "new-value"
