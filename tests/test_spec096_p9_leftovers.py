"""spec-096 P9 item H: four small leftovers of the P2/P3 review.

F1  two racing starts must agree on ONE cookie salt (the file is published with an exclusive
    create and the winner is read back).
F4  role files are not secrets: they keep a 0644 mode (the old `write_text` followed the umask),
    and `fsutil.atomic_write` documents that a symlinked target is REPLACED, not followed.
F5  the `2fa_state_unreadable` journal line names the way out when the KEY is lost, where
    `secret rm` cannot work: move the store aside.
F6  Grok's `_atomic_write` skips an unchanged file but must still tighten a loose mode.
"""
import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import auth_salt
import fsutil
import grok_engine
import roles
import secretstore
import webapp


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def umask022():
    old = os.umask(0o022)
    yield
    os.umask(old)


# ─────────────────────────── F1: the salt race ───────────────────────────


def test_a_start_that_missed_the_file_adopts_the_winner_instead_of_replacing_it(tmp_path, monkeypatch):
    first = auth_salt.resolve("", tmp_path)                     # start A published its salt
    real = auth_salt._read_persisted
    calls = {"n": 0}

    def stale_first_read(path):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(path)          # start B looked BEFORE A's file existed

    monkeypatch.setattr(auth_salt, "_read_persisted", stale_first_read)
    second = auth_salt.resolve("", tmp_path)
    assert second == first, "B overwrote A's salt: A signs cookies with a value that is no longer on disk"
    assert (tmp_path / auth_salt.SALT_FILENAME).read_bytes().strip() == first


def test_racing_starts_all_agree_on_one_salt(tmp_path):
    results: list = []
    barrier = threading.Barrier(8)

    def start():
        barrier.wait()
        results.append(auth_salt.resolve("", tmp_path))

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = (tmp_path / auth_salt.SALT_FILENAME).read_bytes().strip()
    assert len(results) == 8 and set(results) == {on_disk}


def test_a_damaged_file_is_still_replaced_exactly_once(tmp_path):
    f = tmp_path / auth_salt.SALT_FILENAME
    f.write_text("short")
    salt = auth_salt.resolve("", tmp_path)
    assert len(salt) >= 32 and f.read_bytes().strip() == salt
    assert auth_salt.resolve("", tmp_path) == salt


def test_create_exclusive_publishes_a_complete_private_file_once(tmp_path, umask022):
    target = tmp_path / "salt"
    assert fsutil.create_exclusive(target, "first", 0o600) is True
    assert target.read_text() == "first" and _mode(target) == 0o600
    assert fsutil.create_exclusive(target, "second", 0o600) is False
    assert target.read_text() == "first", "the loser must not touch the winner's file"
    assert [p.name for p in tmp_path.iterdir()] == ["salt"], "no temp file may be left behind"


def test_create_exclusive_never_follows_a_symlink_at_the_target(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    target = tmp_path / "salt"
    target.symlink_to(victim)
    assert fsutil.create_exclusive(target, "x" * 40, 0o600) is False
    assert victim.read_text() == "keep"


# ─────────────────────────── F4: roles stay readable, symlinks are replaced ───────────────────────────


def test_a_role_file_keeps_the_readable_mode_the_old_writer_gave_it(tmp_path, umask022):
    roles._atomic_write(str(tmp_path / "r.md"), "---\nname: r\n---\n")
    assert _mode(tmp_path / "r.md") == 0o644, "roles are not secrets: a group-shared role must stay readable"


def test_write_role_through_the_public_api_is_0644_too(tmp_path, umask022):
    text = "---\nname: probe\ndescription: d\n---\nbody\n"
    role = roles.write_role(str(tmp_path), "probe", "project", text)
    assert _mode(role.path) == 0o644


def test_fsutil_documents_that_a_symlinked_target_is_replaced_not_followed():
    doc = fsutil.atomic_write.__doc__ or ""
    assert "symlink" in doc.lower() and "replaced" in doc.lower() and "not followed" in doc.lower()


# ─────────────────────────── F5: the journal line names the way out ───────────────────────────


def test_the_2fa_journal_line_names_the_store_to_move_aside(tmp_path, monkeypatch, caplog):
    store = tmp_path / "vault" / "secrets.enc"
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(store))
    assert secretstore._store_path() == store, "the test must steer the store path the line reports"
    with caplog.at_level(logging.ERROR):
        resp = webapp._twofa_state_unreadable("203.0.113.9", "secret", ValueError("x"))
    assert resp.status == 503
    line = caplog.text
    assert str(store) in line, "the operator reads the journal while locked out: name the file"
    assert "move" in line.lower() and "aside" in line.lower()
    assert "secret rm __totp_secret__" in line, "the readable-vault way out stays named too"
    assert "ValueError" in line


# ─────────────────────────── F6: Grok's writer tightens an unchanged file ───────────────────────────


def test_grok_atomic_write_tightens_an_unchanged_loose_file(tmp_path, umask022):
    target = tmp_path / "config.toml"
    target.write_text("a = 1\n")
    os.chmod(target, 0o644)
    assert grok_engine._atomic_write(target, "a = 1\n") is False          # content unchanged: not rewritten
    assert _mode(target) == 0o600, "a pre-existing 0644 file must not stay world-readable"


def test_grok_atomic_write_tighten_respects_an_explicit_mode(tmp_path, umask022):
    target = tmp_path / "config.toml"
    target.write_text("a = 1\n")
    os.chmod(target, 0o666)
    assert grok_engine._atomic_write(target, "a = 1\n", 0o640) is False
    assert _mode(target) == 0o640
