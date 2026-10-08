"""spec-096 P2a: every secret file is private FROM CREATION, not after a chmod.

The old pattern was `write_text(...)` then `chmod(0o600)`: under umask 022 the file exists as
0644 between the two calls. The window is the bug — the final mode was always right — so these
tests run under umask 022 and look at the TEMP file at the moments the old code repaired or
published it: just before `os.replace`, and just before any chmod/fchmod (a chmod that finds a
group/other-readable file is a repair after the fact, which is exactly the window).
"""
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import accounts
import fsutil
import grok_engine
import roles
import secretstore
import webapp as _webapp


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def umask022():
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def mode_spy(monkeypatch, umask022):
    """Record the mode of the file being renamed/chmod-ed at the moment of the call."""
    seen: list = []
    real_replace, real_chmod, real_fchmod = os.replace, os.chmod, os.fchmod

    def replace(src, dst, *a, **k):
        seen.append(("replace", os.fspath(src), _mode(src)))
        return real_replace(src, dst, *a, **k)

    def chmod(path, mode, *a, **k):
        try:
            seen.append(("chmod", os.fspath(path), _mode(path)))
        except OSError:
            pass
        return real_chmod(path, mode, *a, **k)

    def fchmod(fd, mode):
        seen.append(("fchmod", fd, stat.S_IMODE(os.fstat(fd).st_mode)))
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fchmod", fchmod)
    return seen


def assert_private_throughout(seen, final_path, final_mode=0o600):
    """The temp file was never group/other-accessible when touched, and the result is `final_mode`."""
    assert seen, "the writer made no replace/chmod call at all — the spy saw nothing"
    assert any(kind == "replace" for kind, _, _ in seen), "nothing was published with os.replace"
    for kind, what, mode in seen:
        assert mode & 0o077 == 0, (
            f"{kind} on {what} saw mode {mode:04o} — the file was group/other-accessible "
            "before it was repaired (that readable window is the bug)")
    assert _mode(final_path) == final_mode


# ─────────────────────────── the helper ───────────────────────────────────────

def test_atomic_write_str_and_bytes_are_private_from_creation(tmp_path, mode_spy):
    fsutil.atomic_write(tmp_path / "a.txt", "héllo\r\nline\n")
    assert (tmp_path / "a.txt").read_bytes() == "héllo\r\nline\n".encode()   # no newline translation
    assert_private_throughout(mode_spy, tmp_path / "a.txt")

    mode_spy.clear()
    fsutil.atomic_write(tmp_path / "b.bin", b"\x00\xffkey")
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\xffkey"
    assert_private_throughout(mode_spy, tmp_path / "b.bin")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.bin"]   # no temp litter


def test_atomic_write_mode_argument_is_applied_before_any_byte(tmp_path, umask022):
    fsutil.atomic_write(tmp_path / "pub", "x", 0o644)
    assert _mode(tmp_path / "pub") == 0o644
    fsutil.atomic_write(tmp_path / "grp", "x", 0o640)
    assert _mode(tmp_path / "grp") == 0o640


def test_atomic_write_overwrite_narrows_a_wide_existing_file(tmp_path, mode_spy):
    target = tmp_path / "old.env"
    target.write_text("old")
    os.chmod(target, 0o644)
    mode_spy.clear()
    fsutil.atomic_write(target, "new")
    assert target.read_text() == "new"
    assert _mode(target) == 0o600


def test_atomic_write_creates_missing_parent(tmp_path, umask022):
    fsutil.atomic_write(tmp_path / "deep" / "er" / "f", "x")
    assert (tmp_path / "deep" / "er" / "f").read_text() == "x"


def test_atomic_write_failure_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch, umask022):
    target = tmp_path / "keep"
    target.write_text("original")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write(target, "new")
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]


def test_atomic_write_replaces_a_symlink_instead_of_writing_through_it(tmp_path, umask022):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    link = tmp_path / "link"
    link.symlink_to(victim)
    fsutil.atomic_write(link, "secret")
    assert victim.read_text() == "untouched"
    assert not link.is_symlink() and link.read_text() == "secret"


def test_tighten_only_narrows(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(f, 0o644)
    assert fsutil.tighten(f) is True and _mode(f) == 0o600
    assert fsutil.tighten(f) is False and _mode(f) == 0o600
    os.chmod(f, 0o400)
    assert fsutil.tighten(f) is False and _mode(f) == 0o400      # narrower is left alone
    assert fsutil.tighten(tmp_path / "missing") is False


def test_roles_atomic_write_delegates_and_keeps_its_temp_names(tmp_path, mode_spy):
    # spec-096 P9: a role is not a secret - it keeps the readable 0644 the old write_text gave it (a role
    # shared through group read must stay readable), so unlike the secret files it is NOT private-throughout.
    roles._atomic_write(str(tmp_path / "r.md"), "---\nname: r\n---\n")
    assert _mode(tmp_path / "r.md") == 0o644
    assert any(os.path.basename(w).startswith(".tmp-role-") and w.endswith(".md")
               for k, w, _ in mode_spy if k == "replace")


# ─────────────────────────── one test per call site ───────────────────────────

def test_site_project_secrets_env(tmp_path, mode_spy):
    _webapp._secrets_write(str(tmp_path), {"B": "2", "A": "1"})
    path = tmp_path / ".claude-ops" / "secrets" / "secrets.env"
    assert path.read_text().splitlines()[1:] == ["A=1", "B=2"]
    assert_private_throughout(mode_spy, path)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(tmp_path / "vault" / "secrets.enc"))
    monkeypatch.delenv("CLAUDE_OPS_SECRET_KEY", raising=False)
    return tmp_path


def test_site_vault_key_and_store(vault, mode_spy):
    secretstore.init_key()
    assert_private_throughout(mode_spy, vault / "keys" / "secret.key")
    assert (vault / "keys").is_dir() and _mode(vault / "keys") == 0o700

    mode_spy.clear()
    secretstore.set("TOKEN_X", "s3cr3t")
    assert secretstore.get("TOKEN_X") == "s3cr3t"
    assert_private_throughout(mode_spy, vault / "vault" / "secrets.enc")
    assert [p.name for p in (vault / "vault").iterdir()] == ["secrets.enc"]


def test_site_vault_key_force_rewrite_stays_private(vault, mode_spy):
    secretstore.init_key()
    mode_spy.clear()
    secretstore.init_key(force=True)
    assert_private_throughout(mode_spy, vault / "keys" / "secret.key")


def test_vault_key_in_an_existing_wide_directory_warns(vault, caplog, umask022):
    wide = vault / "keys"
    wide.mkdir()
    os.chmod(wide, 0o755)
    with caplog.at_level(logging.WARNING, logger="secretstore"):
        secretstore.init_key()
    assert any("accessible to group/other" in r.getMessage() and str(wide) in r.getMessage()
               for r in caplog.records)
    assert _mode(wide) == 0o755, "the helper must warn, never chmod a directory it did not create"
    assert _mode(wide / "secret.key") == 0o600


def test_vault_key_in_a_private_directory_does_not_warn(vault, caplog, umask022):
    private = vault / "keys"
    private.mkdir()
    os.chmod(private, 0o700)
    with caplog.at_level(logging.WARNING, logger="secretstore"):
        secretstore.init_key()
    assert not [r for r in caplog.records if r.name == "secretstore"]


def test_site_accounts_merge_key(tmp_path, mode_spy):
    dst = tmp_path / ".claude.json"
    dst.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}, "keep": 1}))
    os.chmod(dst, 0o644)
    mode_spy.clear()
    assert accounts._merge_key(dst, {"mcpServers": {"new": {"command": "y"}}}, "mcpServers", mirror=False)
    data = json.loads(dst.read_text())
    assert set(data["mcpServers"]) == {"old", "new"} and data["keep"] == 1
    assert_private_throughout(mode_spy, dst)
    assert [p.name for p in tmp_path.iterdir()] == [".claude.json"]


def test_site_grok_atomic_write(tmp_path, mode_spy):
    target = tmp_path / "config.toml"
    assert grok_engine._atomic_write(target, "a = 1\n") is True
    assert target.read_text() == "a = 1\n"
    assert_private_throughout(mode_spy, target)

    # unchanged content: reported False and the file is not even re-created
    inode = os.stat(target).st_ino
    mode_spy.clear()
    assert grok_engine._atomic_write(target, "a = 1\n") is False
    assert os.stat(target).st_ino == inode and mode_spy == []

    # changed content is rewritten; an explicit mode is honoured
    assert grok_engine._atomic_write(target, "a = 2\n", 0o640) is True
    assert target.read_text() == "a = 2\n" and _mode(target) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


@pytest.fixture
def vapid_globals(monkeypatch):
    monkeypatch.setattr(_webapp, "_PUSH_PRIV_KEY", None)
    monkeypatch.setattr(_webapp, "_PUSH_PUB_KEY", None)


def test_site_web_push_vapid_private_key_is_private_from_creation(tmp_path, monkeypatch, vapid_globals, mode_spy):
    path = tmp_path / "push-vapid.json"
    monkeypatch.setattr(_webapp, "_PUSH_VAPID_FILE", path)
    _webapp._push_ensure_vapid_keys()
    assert json.loads(path.read_text())["private_key"] == _webapp._PUSH_PRIV_KEY
    assert_private_throughout(mode_spy, path)


def test_existing_world_readable_vapid_file_is_tightened_at_startup(tmp_path, monkeypatch, vapid_globals, umask022):
    path = tmp_path / "push-vapid.json"
    path.write_text(json.dumps({"private_key": "p" * 43, "public_key": "q" * 87}))
    os.chmod(path, 0o644)                                    # what every pre-spec-096 install has
    for name in ("_PUSH_VAPID_FILE", "_PUSH_SUBS_FILE", "_PUSH_CTX", "_PUSH_LOCK"):
        monkeypatch.setattr(_webapp, name, None)             # restored after the test
    _webapp._push_init({"DATA": tmp_path})
    assert _webapp._PUSH_PRIV_KEY == "p" * 43, "the key was kept, not regenerated"
    assert _mode(path) == 0o600
