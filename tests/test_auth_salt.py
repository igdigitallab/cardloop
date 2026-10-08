"""spec-096 P3.1 — the cookie salt must never be a value published in the repository.

A blank or `CHANGE_ME...` WEB_COOKIE_SALT resolves to a private, persisted, never-printed salt in
the data dir; an explicitly configured salt keeps producing byte-identical cookies.
"""
import hashlib
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import auth_salt  # noqa: E402
import webapp  # noqa: E402

PLACEHOLDERS = ["", "   ", "CHANGE_ME_RANDOM", "change_me", "  Change_Me_Please  ", "CHANGE_ME"]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("value", PLACEHOLDERS + [None])
def test_blank_and_placeholder_are_recognised(value):
    assert auth_salt.is_placeholder(value)


@pytest.mark.parametrize("value", ["a" * 64, "my-own-salt", "x", "CHANGEME-not-the-prefix"])
def test_real_values_are_not_placeholders(value):
    assert not auth_salt.is_placeholder(value)


@pytest.mark.parametrize("value", PLACEHOLDERS)
def test_placeholder_resolves_to_private_persisted_salt(tmp_path, value):
    salt = auth_salt.resolve(value, tmp_path)
    assert salt and salt != value.encode()
    assert b"change_me" not in salt.lower()
    f = tmp_path / auth_salt.SALT_FILENAME
    assert f.is_file()
    assert _mode(f) == 0o600
    assert f.read_bytes().strip() == salt
    assert len(salt) >= 32


def test_resolution_is_stable_across_calls_and_restarts(tmp_path):
    first = auth_salt.resolve("CHANGE_ME_RANDOM", tmp_path)
    again = auth_salt.resolve("", tmp_path)
    assert first == again                        # same file read back, not a fresh salt each start
    assert auth_salt.resolve(None, tmp_path) == first


def test_two_installs_do_not_share_a_salt(tmp_path):
    a = auth_salt.resolve("", tmp_path / "a")
    b = auth_salt.resolve("", tmp_path / "b")
    assert a != b


def test_explicit_salt_is_used_byte_for_byte_and_nothing_is_written(tmp_path):
    explicit = " my own salt with spaces "      # verbatim: no strip, no re-encoding
    assert auth_salt.resolve(explicit, tmp_path) == explicit.encode()
    assert not (tmp_path / auth_salt.SALT_FILENAME).exists()


def test_existing_wide_file_is_tightened(tmp_path):
    f = tmp_path / auth_salt.SALT_FILENAME
    f.write_text("k" * 64)
    os.chmod(f, 0o644)
    assert auth_salt.resolve("", tmp_path) == b"k" * 64
    assert _mode(f) == 0o600


@pytest.mark.parametrize("junk", ["", "short", "\n\n"])
def test_damaged_file_is_replaced_not_used(tmp_path, junk):
    f = tmp_path / auth_salt.SALT_FILENAME
    f.write_text(junk)
    salt = auth_salt.resolve("", tmp_path)
    assert len(salt) >= 32 and salt != junk.strip().encode()
    assert f.read_bytes().strip() == salt


def test_unusable_data_dir_falls_back_to_per_process_salt_without_leaking(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")                       # a FILE where the data dir should be
    with caplog.at_level(logging.DEBUG):
        salt = auth_salt.resolve("", blocker)
        no_dir = auth_salt.resolve("", None)
    assert len(salt) >= 32 and len(no_dir) >= 32 and salt != no_dir
    assert salt.decode() not in caplog.text and no_dir.decode() not in caplog.text


def test_salt_value_never_reaches_stdout_stderr_or_logs(tmp_path, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        salt = auth_salt.resolve("CHANGE_ME_RANDOM", tmp_path)          # generated + persisted
        again = auth_salt.resolve("", tmp_path)                         # read back
    out = capsys.readouterr()
    assert salt == again
    for stream in (out.out, out.err, caplog.text):
        assert salt.decode() not in stream


def test_env_example_ships_no_usable_salt():
    """Whatever `.env.example` carries for WEB_COOKIE_SALT must be blank or a placeholder."""
    line = next(ln for ln in (ROOT / ".env.example").read_text().splitlines()
                if ln.startswith("WEB_COOKIE_SALT="))
    assert auth_salt.is_placeholder(line.split("=", 1)[1])


# ── wiring: webapp._init_auth_salt (what start() runs before the first token) ──────────────

def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=1 << 14, r=8, p=1, dklen=32).hex()


@pytest.fixture
def restore_salt():
    saved = webapp.AUTH_SALT
    yield
    webapp.AUTH_SALT = saved


def test_start_wiring_placeholder_env_uses_the_persisted_salt(tmp_path, monkeypatch, restore_salt, capsys):
    monkeypatch.setenv("WEB_COOKIE_SALT", "CHANGE_ME_RANDOM")
    webapp._init_auth_salt({"DATA": tmp_path})
    persisted = (tmp_path / auth_salt.SALT_FILENAME).read_bytes().strip()
    assert webapp.AUTH_SALT == persisted != b"CHANGE_ME_RANDOM"
    # the cookie is NOT derivable from the published placeholder
    assert webapp._derive_token("pw") != _scrypt("pw", b"CHANGE_ME_RANDOM")
    assert webapp._derive_token("pw") == _scrypt("pw", persisted)
    # a second "start" (restart) lands on the same salt, hence the same cookie
    before = webapp._derive_token("pw")
    webapp.AUTH_SALT = b"something else"
    webapp._init_auth_salt({"DATA": tmp_path})
    assert webapp._derive_token("pw") == before
    assert persisted.decode() not in capsys.readouterr().out


def test_start_wiring_explicit_salt_keeps_existing_cookies_valid(tmp_path, monkeypatch, restore_salt):
    monkeypatch.setenv("WEB_COOKIE_SALT", "ops-long-random-salt-0123456789")
    webapp._init_auth_salt({"DATA": tmp_path})
    assert webapp.AUTH_SALT == b"ops-long-random-salt-0123456789"
    assert webapp._derive_token("pw") == _scrypt("pw", b"ops-long-random-salt-0123456789")
    assert not (tmp_path / auth_salt.SALT_FILENAME).exists()


def test_start_wiring_picks_up_a_salt_loaded_after_import(tmp_path, monkeypatch, restore_salt):
    """.env may be loaded after webapp was imported: start() must read the env at start time."""
    monkeypatch.delenv("WEB_COOKIE_SALT", raising=False)
    monkeypatch.setenv("WEB_COOKIE_SALT", "loaded-late-by-dotenv-0123456789")
    webapp._init_auth_salt({"DATA": tmp_path})
    assert webapp.AUTH_SALT == b"loaded-late-by-dotenv-0123456789"
