"""Explorer policy (fs_browser.py) and its /api/fs/* routes.

The policy is the security boundary of the Files tab now that it can leave a project's cwd,
so the tests pin the refusals, not just the happy path.
"""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fs_browser as fb  # noqa: E402


@pytest.fixture
def world(tmp_path):
    """A fake $HOME with a project, a secret dotdir, a scratch root and a foreign dir."""
    home = tmp_path / "home"
    (home / "proj" / "docs").mkdir(parents=True)
    (home / "proj" / "docs" / "a.md").write_text("# a\n")
    (home / "proj" / "node_modules").mkdir()
    (home / "proj" / ".git").mkdir()
    (home / "notes.txt").write_text("hi\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAA\n")
    (home / ".aws").mkdir()
    (home / ".bashrc").write_text("export X=1\n")
    (home / ".claude" / "projects" / "-home-x" / "memory").mkdir(parents=True)
    (home / ".claude" / "projects" / "-home-x" / "memory" / "m.md").write_text("mem\n")
    (home / ".claude" / "projects" / "-home-x" / "sess.jsonl").write_text("{}\n")
    (home / ".claude" / ".credentials.json").write_text("{}\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "report.md").write_text("r\n")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "x.txt").write_text("x\n")
    return {"tmp": tmp_path, "home": home, "scratch": scratch, "foreign": foreign}


def roots_for(w, cwd=None, extras=True):
    return fb.Roots(w["home"], [w["scratch"]] if extras else [], Path(cwd) if cwd else None)


def status_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except fb.FsError as e:
        return e.status
    return 200


# ── reach ─────────────────────────────────────────────────────────────────────

def test_home_and_scratch_are_reachable_foreign_is_not(world):
    r = roots_for(world)
    assert fb.list_dir(str(world["home"]), r)["path"] == str(world["home"])
    assert fb.read_file(str(world["scratch"] / "report.md"), r)["content"] == "r\n"
    assert status_of(fb.list_dir, str(world["foreign"]), r) == 403
    assert status_of(fb.read_file, str(world["foreign"] / "x.txt"), r) == 403


def test_home_listing_hides_every_top_level_dot_entry(world):
    names = {e["name"] for e in fb.list_dir(str(world["home"]), roots_for(world))["entries"]}
    assert names == {"proj", "notes.txt"}


@pytest.mark.parametrize("rel", [".ssh/authorized_keys", ".bashrc", ".aws", ".claude/.credentials.json"])
def test_dot_entries_under_home_are_denied_directly(world, rel):
    r = roots_for(world)
    p = str(world["home"] / rel)
    assert status_of(fb.resolve_checked, p, r) == 403


def test_native_agent_memory_is_the_one_dot_exception(world):
    r = roots_for(world)
    mem = world["home"] / ".claude" / "projects" / "-home-x" / "memory" / "m.md"
    assert fb.read_file(str(mem), r)["content"] == "mem\n"
    assert status_of(fb.read_file, str(mem.parent.parent / "sess.jsonl"), r) == 403


def test_secret_names_and_excluded_dirs_denied_at_any_depth(world):
    r = roots_for(world)
    for name in (".env", ".env.production", "id_rsa", "deploy.pem", "credentials.json"):
        (world["home"] / "proj" / name).write_text("s\n")
        assert status_of(fb.read_file, str(world["home"] / "proj" / name), r) == 403, name
    (world["home"] / "proj" / ".env.example").write_text("K=\n")
    assert fb.read_file(str(world["home"] / "proj" / ".env.example"), r)["content"] == "K=\n"
    assert status_of(fb.list_dir, str(world["home"] / "proj" / "node_modules"), r) == 403
    assert status_of(fb.list_dir, str(world["home"] / "proj" / ".git"), r) == 403
    listing = {e["name"] for e in fb.list_dir(str(world["home"] / "proj"), r)["entries"]}
    assert "node_modules" not in listing and ".git" not in listing and ".env" not in listing


def test_traversal_relative_and_nul_are_refused(world):
    r = roots_for(world)
    assert status_of(fb.list_dir, str(world["home"]) + "/../foreign", r) == 403
    assert status_of(fb.list_dir, "proj", r) == 400
    assert status_of(fb.list_dir, str(world["home"]) + "/proj\x00/x", r) == 400
    assert status_of(fb.list_dir, "", r) == 400


def test_symlinks_cannot_leave_the_roots(world):
    r = roots_for(world)
    proj = world["home"] / "proj"
    (proj / "out_dir").symlink_to(world["foreign"])
    (proj / "out_file").symlink_to(world["foreign"] / "x.txt")
    (proj / "to_ssh").symlink_to(world["home"] / ".ssh")
    (proj / "in_link").symlink_to(world["home"] / "notes.txt")
    names = {e["name"] for e in fb.list_dir(str(proj), r)["entries"]}
    assert {"out_dir", "out_file", "to_ssh"}.isdisjoint(names)
    assert "in_link" in names
    assert status_of(fb.read_file, str(proj / "out_file"), r) == 403
    assert status_of(fb.list_dir, str(proj / "to_ssh"), r) == 403
    assert status_of(fb.write_file, str(proj / "to_ssh" / "authorized_keys"), "x", "0:0", r, True) == 403


def test_fifos_and_sockets_are_not_listed(world):
    r = roots_for(world)
    os.mkfifo(world["scratch"] / "pipe")
    names = {e["name"] for e in fb.list_dir(str(world["scratch"]), r)["entries"]}
    assert names == {"report.md"}
    assert status_of(fb.read_file, str(world["scratch"] / "pipe"), r) == 404


def test_cwd_equal_to_home_or_above_grants_nothing(world):
    for cwd in (world["home"], world["home"].parent):
        r = roots_for(world, cwd=cwd)
        assert status_of(fb.resolve_checked, str(world["home"] / ".ssh" / "authorized_keys"), r) == 403
        assert status_of(fb.list_dir, str(world["foreign"]), r) == 403


def test_project_cwd_below_home_in_a_dotdir_or_outside_home_is_its_own_root(world):
    hidden = world["home"] / ".hidden" / "app"
    hidden.mkdir(parents=True)
    (hidden / "f.txt").write_text("f\n")
    r = roots_for(world, cwd=hidden)
    assert fb.read_file(str(hidden / "f.txt"), r)["content"] == "f\n"
    assert status_of(fb.resolve_checked, str(world["home"] / ".ssh"), r) == 403
    r2 = roots_for(world, cwd=world["foreign"])
    assert fb.read_file(str(world["foreign"] / "x.txt"), r2)["content"] == "x\n"


def test_shortcut_list_has_no_duplicate_or_dead_entries(world):
    def paths(cwd):
        return [r["path"] for r in roots_for(world, cwd=cwd).as_list()]
    assert paths(world["home"] / "proj") == [str(world["home"] / "proj"), str(world["home"]), str(world["scratch"])]
    assert paths(world["home"]) == [str(world["home"]), str(world["scratch"])]
    assert paths(world["home"].parent) == [str(world["home"]), str(world["scratch"])]


def test_parent_and_crumbs_stop_at_the_ceiling(world):
    r = roots_for(world)
    up = fb.list_dir(str(world["home"] / "proj"), r)
    assert up["parent"] == str(world["home"])
    top = fb.list_dir(str(world["home"]), r)
    assert top["parent"] is None
    ok = {c["path"]: c["ok"] for c in top["crumbs"]}
    assert ok[str(world["home"])] is True and ok["/"] is False


def test_listing_sorts_dirs_first_and_caps_huge_directories(world, monkeypatch):
    r = roots_for(world)
    ents = fb.list_dir(str(world["home"]), r)["entries"]
    assert [e["type"] for e in ents] == ["dir", "file"]
    monkeypatch.setattr(fb, "MAX_ENTRIES", 3)
    for i in range(6):
        (world["scratch"] / f"f{i}.txt").write_text("x")
    out = fb.list_dir(str(world["scratch"]), r)
    assert len(out["entries"]) == 3 and out["truncated"] is True


# ── pasted paths ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pasted,expect", [
    ("`/tmp/a.md`", "/tmp/a.md"),
    ('"/tmp/a b.md"', "/tmp/a b.md"),
    ("(/tmp/a.md)", "/tmp/a.md"),
    ("[report](/tmp/a.md)", "/tmp/a.md"),
    ("/tmp/a.md.", "/tmp/a.md"),
    ("/tmp/a.md:12", "/tmp/a.md"),
    ("/tmp/a.md:12:5", "/tmp/a.md"),
    ("/tmp/a.md#L10-L20", "/tmp/a.md"),
    ("file:///tmp/a%20b.md", "/tmp/a b.md"),
    ("  \n  /tmp/a.md  \nnext line", "/tmp/a.md"),
    ("~/x/y.md", "/h/x/y.md"),
    ("$HOME/x", "/h/x"),
    ("~", "/h"),
    ("", ""),
])
def test_normalise_input(pasted, expect):
    assert fb.normalise_input(pasted, Path("/h")) == expect


def test_stat_classifies_and_finds_the_nearest_folder(world):
    r = roots_for(world)
    f = str(world["home"] / "proj" / "docs" / "a.md")
    assert fb.stat_input(f"`{f}:3`", r)["kind"] == "file"
    assert fb.stat_input(str(world["home"] / "proj"), r)["kind"] == "dir"
    miss = fb.stat_input(str(world["home"] / "proj" / "docs" / "typo.md"), r)
    assert miss["kind"] == "missing" and miss["nearest"] == str(world["home"] / "proj" / "docs")
    assert fb.stat_input(str(world["home"] / ".ssh" / "authorized_keys"), r)["kind"] == "denied"
    assert fb.stat_input(str(world["foreign"] / "x.txt"), r)["kind"] == "denied"


def test_stat_tries_the_literal_text_before_the_cleaned_one(world):
    r = roots_for(world)
    odd = world["scratch"] / "weird:12"
    odd.write_text("w\n")
    assert fb.stat_input(str(odd), r) == {"input": str(odd), "path": str(odd), "kind": "file"}


def test_stat_resolves_relative_against_base_then_project_then_home(world):
    r = roots_for(world, cwd=world["home"] / "proj")
    assert fb.stat_input("a.md", r, base=str(world["home"] / "proj" / "docs"))["path"] == \
        str(world["home"] / "proj" / "docs" / "a.md")
    assert fb.stat_input("docs/a.md", r, base=str(world["scratch"]))["path"] == \
        str(world["home"] / "proj" / "docs" / "a.md")
    assert fb.stat_input("notes.txt", r, base=str(world["scratch"]))["kind"] == "file"


# ── read ──────────────────────────────────────────────────────────────────────

def test_read_returns_string_revision_and_normalises_crlf(world):
    r = roots_for(world)
    p = world["scratch"] / "win.txt"
    p.write_bytes(b"a\r\nb\r\n")
    out = fb.read_file(str(p), r)
    assert out["content"] == "a\nb\n" and out["editable"] is True
    assert isinstance(out["rev"], str)


def test_read_refuses_binary_and_large_and_flags_non_utf8_readonly(world, monkeypatch):
    r = roots_for(world)
    b = world["scratch"] / "b.bin"
    b.write_bytes(b"\x00\x01")
    assert fb.read_file(str(b), r)["error"] == "binary file"
    monkeypatch.setattr(fb, "MAX_TEXT_BYTES", 10)
    big = world["scratch"] / "big.txt"
    big.write_text("x" * 50)
    assert fb.read_file(str(big), r)["error"] == "file too large"
    monkeypatch.undo()
    latin = world["scratch"] / "latin.txt"
    latin.write_bytes("caf\xe9\n".encode("latin-1"))
    out = fb.read_file(str(latin), r)
    assert out["editable"] is False and "caf" in out["content"]


# ── write ─────────────────────────────────────────────────────────────────────

def test_write_saves_atomically_keeps_mode_and_returns_the_new_rev(world):
    r = roots_for(world)
    p = world["scratch"] / "report.md"
    os.chmod(p, 0o640)
    rev = fb.read_file(str(p), r)["rev"]
    out = fb.write_file(str(p), "new\n", rev, r)
    assert p.read_text() == "new\n" and out["ok"] is True
    assert (p.stat().st_mode & 0o777) == 0o640
    assert out["rev"] == fb.read_file(str(p), r)["rev"] != rev
    assert [x.name for x in world["scratch"].iterdir() if x.name.startswith(".cardloop-save-")] == []


def test_write_conflict_when_the_file_changed_on_disk(world):
    r = roots_for(world)
    p = world["scratch"] / "report.md"
    rev = fb.read_file(str(p), r)["rev"]
    p.write_text("the agent wrote this, and it is longer\n")
    assert status_of(fb.write_file, str(p), "mine\n", rev, r) == 409
    assert p.read_text().startswith("the agent")
    assert status_of(fb.write_file, str(p), "mine\n", None, r) == 400
    assert fb.write_file(str(p), "mine\n", None, r, force=True)["ok"] is True
    assert p.read_text() == "mine\n"


def test_write_keeps_crlf_files_crlf(world):
    r = roots_for(world)
    p = world["scratch"] / "win.txt"
    p.write_bytes(b"a\r\nb\r\n")
    fb.write_file(str(p), "a\nb\nc\n", fb.read_file(str(p), r)["rev"], r)
    assert p.read_bytes() == b"a\r\nb\r\nc\r\n"


def test_write_refuses_binary_invalid_utf8_missing_and_denied_targets(world):
    r = roots_for(world)
    b = world["scratch"] / "b.bin"
    b.write_bytes(b"\x00\x01")
    assert status_of(fb.write_file, str(b), "x", None, r, True) == 415
    latin = world["scratch"] / "latin.txt"
    latin.write_bytes(b"caf\xe9\n")
    assert status_of(fb.write_file, str(latin), "x", None, r, True) == 415
    assert latin.read_bytes() == b"caf\xe9\n"
    assert status_of(fb.write_file, str(world["scratch"] / "new.md"), "x", None, r, True) == 404
    for target in (world["home"] / ".ssh" / "authorized_keys", world["home"] / ".bashrc",
                   world["foreign"] / "x.txt"):
        assert status_of(fb.write_file, str(target), "x", None, r, True) == 403
    assert (world["home"] / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAA\n"


def test_write_caps_size(world, monkeypatch):
    r = roots_for(world)
    monkeypatch.setattr(fb, "MAX_TEXT_BYTES", 10)
    assert status_of(fb.write_file, str(world["scratch"] / "report.md"), "x" * 50, None, r, True) == 413


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_read_only_files_are_shown_but_never_rewritten(world):
    r = roots_for(world)
    p = world["scratch"] / "ro.txt"
    p.write_text("keep\n")
    os.chmod(p, 0o444)
    doc = fb.read_file(str(p), r)
    assert doc["editable"] is False and doc["content"] == "keep\n"
    assert status_of(fb.write_file, str(p), "x", doc["rev"], r) == 403
    assert p.read_text() == "keep\n" and (p.stat().st_mode & 0o777) == 0o444


def test_a_failed_save_leaves_the_original_and_no_temp_file(world, monkeypatch):
    """Regression: the in-place fallback used to catch a failed temp write too, so a full disk
    truncated the operator's file."""
    r = roots_for(world)
    p = world["scratch"] / "report.md"
    rev = fb.read_file(str(p), r)["rev"]

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(fb.os, "replace", boom)
    assert status_of(fb.write_file, str(p), "new\n", rev, r) == 500
    monkeypatch.undo()
    assert p.read_text() == "r\n"
    assert [x.name for x in world["scratch"].iterdir() if x.name.startswith(".cardloop-save-")] == []


def test_unwritable_folder_falls_back_to_in_place_only_then(world, monkeypatch):
    r = roots_for(world)
    p = world["scratch"] / "report.md"
    rev = fb.read_file(str(p), r)["rev"]

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(fb.tempfile, "mkstemp", denied)
    out = fb.write_file(str(p), "in place\n", rev, r)
    assert p.read_text() == "in place\n"
    assert out["rev"] == fb.read_file(str(p), r)["rev"]


def test_a_same_size_atomic_rewrite_in_the_same_tick_is_still_a_conflict(world):
    r = roots_for(world)
    p = world["scratch"] / "report.md"
    st = p.stat()
    rev = fb.read_file(str(p), r)["rev"]
    other = world["scratch"] / "swap.tmp"
    other.write_text("x\n")  # same size as "r\n"
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, p)  # what an agent's atomic save does: new inode, same size, same mtime
    assert p.stat().st_size == st.st_size and p.stat().st_mtime_ns == st.st_mtime_ns
    assert status_of(fb.write_file, str(p), "mine\n", rev, r) == 409


def test_an_extra_root_covering_home_is_ignored(world):
    for extra in (world["home"], world["home"].parent):
        r = fb.Roots(world["home"], [extra, world["scratch"]])
        assert status_of(fb.resolve_checked, str(world["home"] / ".ssh" / "authorized_keys"), r) == 403
        assert status_of(fb.write_file, str(world["home"] / ".bashrc"), "x", None, r, True) == 403
        assert fb.read_file(str(world["scratch"] / "report.md"), r)["content"] == "r\n"


# ── raw bytes: previews and downloads ─────────────────────────────────────────

def _bytes_file(dirpath, name, data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16):
    p = dirpath / name
    p.write_bytes(data)
    return p


def test_read_reports_the_preview_kind_and_never_pushes_bytes_through_the_text_path(world):
    r = roots_for(world)
    for name, kind in (("a.png", "image"), ("a.SVG", "image"), ("a.pdf", "pdf"), ("a.mp4", "video"), ("a.mp3", "audio")):
        _bytes_file(world["scratch"], name)
        doc = fb.read_file(str(world["scratch"] / name), r)
        assert doc["kind"] == kind and doc["content"] == "" and doc["editable"] is False, name
        assert "error" not in doc


def test_raw_serves_previews_inline_with_the_headers_that_keep_them_harmless(world):
    r = roots_for(world)
    png = _bytes_file(world["scratch"], "a.png")
    _, mime, h = fb.open_raw(str(png), r)
    assert mime == "image/png" and h["Content-Disposition"] == "inline"
    assert h["Content-Security-Policy"].startswith("sandbox")

    svg = _bytes_file(world["scratch"], "a.svg", b"<svg onload='alert(1)'/>")
    _, mime, h = fb.open_raw(str(svg), r)
    assert mime == "image/svg+xml" and "sandbox" in h["Content-Security-Policy"]

    pdf = _bytes_file(world["scratch"], "a.pdf", b"%PDF-1.4")
    _, mime, h = fb.open_raw(str(pdf), r)
    # A PDF cannot carry a sandbox CSP (Chrome will not render it) but must be frameable by us.
    assert mime == "application/pdf" and h["X-Frame-Options"] == "SAMEORIGIN"
    assert "Content-Security-Policy" not in h


def test_raw_forces_a_download_for_everything_it_does_not_preview(world):
    r = roots_for(world)
    for name in ("page.html", "notes.md", "script.py", "blob.bin", "evil.svgz"):
        _bytes_file(world["scratch"], name, b"<script>alert(1)</script>")
        _, mime, h = fb.open_raw(str(world["scratch"] / name), r)
        assert mime == "application/octet-stream", name
        assert h["Content-Disposition"].startswith("attachment; filename*=UTF-8''"), name
        assert "sandbox" in h["Content-Security-Policy"]
    png = _bytes_file(world["scratch"], "a.png")
    _, mime, h = fb.open_raw(str(png), r, download=True)
    assert mime == "application/octet-stream" and h["Content-Disposition"].startswith("attachment")


def test_raw_filenames_are_quoted_and_cannot_inject_headers(world):
    r = roots_for(world)
    p = _bytes_file(world["scratch"], 'my "report"\n; x=1.txt', b"x")
    _, _, h = fb.open_raw(str(p), r)
    assert "\n" not in h["Content-Disposition"] and '"' not in h["Content-Disposition"].split("''", 1)[1]


def test_raw_follows_the_same_policy_as_everything_else(world, monkeypatch):
    r = roots_for(world)
    assert status_of(fb.open_raw, str(world["home"] / ".ssh" / "authorized_keys"), r) == 403
    assert status_of(fb.open_raw, str(world["foreign"] / "x.txt"), r) == 403
    (world["home"] / "proj" / "deploy.pem").write_bytes(b"k")
    assert status_of(fb.open_raw, str(world["home"] / "proj" / "deploy.pem"), r) == 403
    os.mkfifo(world["scratch"] / "pipe")
    assert status_of(fb.open_raw, str(world["scratch"] / "pipe"), r) == 404
    (world["scratch"] / "out").symlink_to(world["foreign"] / "x.txt")
    assert status_of(fb.open_raw, str(world["scratch"] / "out"), r) == 403
    monkeypatch.setattr(fb, "RAW_MAX_BYTES", 4)
    _bytes_file(world["scratch"], "big.png", b"0123456789")
    assert status_of(fb.open_raw, str(world["scratch"] / "big.png"), r) == 413


# ── recent: what the agent just wrote ─────────────────────────────────────────

def test_touched_path_only_for_write_style_tools():
    assert fb.touched_path("Write", {"file_path": "/a/b.md"}, "/cwd") == "/a/b.md"
    assert fb.touched_path("Edit", {"file_path": "x.py"}, "/cwd") == "/cwd/x.py"
    assert fb.touched_path("MultiEdit", {"file_path": "/a/b"}, "/cwd") == "/a/b"
    assert fb.touched_path("NotebookEdit", {"notebook_path": "/n.ipynb"}, "/cwd") == "/n.ipynb"
    assert fb.touched_path("Read", {"file_path": "/a"}, "/cwd") is None
    assert fb.touched_path("Bash", {"command": "cat > /a"}, "/cwd") is None
    for junk in (None, [], "x", {}, {"file_path": ""}, {"file_path": 5}, {"file_path": "/a\x00b"}):
        assert fb.touched_path("Write", junk, "/cwd") is None


def _touch(data, cwd, path, tool="Write"):
    fb.record_touched(data, str(cwd), tool, {"file_path": str(path)})


def test_recent_lists_agent_writes_newest_first_one_row_per_file(world):
    data = world["tmp"] / "data"
    r = roots_for(world, cwd=world["home"] / "proj")
    proj = world["home"] / "proj"
    _touch(data, proj, proj / "docs" / "a.md")
    _touch(data, proj, world["scratch"] / "report.md")
    _touch(data, proj, proj / "docs" / "a.md", "Edit")  # same file again: still one row, now newest
    rows = fb.recent_files(data, str(proj), r, disk_cap=0)
    assert [x["name"] for x in rows] == ["a.md", "report.md"]
    assert rows[0]["src"] == "agent" and rows[0]["tool"] == "Edit" and rows[0]["size"] > 0


def test_recent_drops_missing_denied_and_outside_paths(world):
    data = world["tmp"] / "data"
    proj = world["home"] / "proj"
    r = roots_for(world, cwd=proj)
    for path in (proj / "gone.md", world["home"] / ".ssh" / "authorized_keys", world["foreign"] / "x.txt",
                 proj / "node_modules" / "m.js", proj / "docs" / "a.md"):
        (proj / "node_modules").mkdir(exist_ok=True)
        (proj / "node_modules" / "m.js").write_text("x")
        _touch(data, proj, path)
    assert [x["name"] for x in fb.recent_files(data, str(proj), r, disk_cap=0)] == ["a.md"]


def test_recent_adds_shell_written_files_from_the_disk_but_agent_rows_win(world):
    data = world["tmp"] / "data"
    proj = world["home"] / "proj"
    r = roots_for(world, cwd=proj)
    old = proj / "old.txt"
    old.write_text("x")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    (proj / "made_by_shell.log").write_text("x")
    (proj / "node_modules" / "junk.js").write_text("x")
    (proj / ".env").write_text("SECRET=1")
    _touch(data, proj, proj / "docs" / "a.md")
    rows = {x["name"]: x for x in fb.recent_files(data, str(proj), r)}
    assert rows["made_by_shell.log"]["src"] == "disk"
    assert rows["a.md"]["src"] == "agent"
    assert "old.txt" not in rows          # outside the window
    assert "junk.js" not in rows          # pruned dir
    assert ".env" not in rows             # secret name
    # touched AND changed on disk: reported once, as the agent's
    _touch(data, proj, proj / "made_by_shell.log")
    again = {x["name"]: x for x in fb.recent_files(data, str(proj), r)}
    assert again["made_by_shell.log"]["src"] == "agent"
    assert [x["name"] for x in fb.recent_files(data, str(proj), r)].count("made_by_shell.log") == 1


def test_recent_disk_scan_is_capped_and_the_log_is_trimmed(world, monkeypatch):
    data = world["tmp"] / "data"
    proj = world["home"] / "proj"
    r = roots_for(world, cwd=proj)
    for i in range(8):
        (proj / f"f{i}.txt").write_text("x")
    assert len(fb.recent_files(data, str(proj), r, disk_cap=3)) == 3
    monkeypatch.setattr(fb, "_TOUCH_KEEP", 5)
    monkeypatch.setattr(fb.time, "time", lambda: 1000.0)  # noqa: keep ordering deterministic
    log = fb._touch_file(data, str(proj))
    for i in range(40):
        _touch(data, proj, proj / f"f{i % 8}.txt")
    monkeypatch.undo()
    assert log.exists()


# ── the cockpit's own state ───────────────────────────────────────────────────

def test_the_cockpit_data_dir_is_private_except_uploads(world, monkeypatch):
    """Regression (security review): data/ sits inside $HOME with no dot component, so the
    Web Push private key, the encrypted safe and the touched-file log were all reachable."""
    data = world["home"] / "cardloop" / "data"
    for rel in ("vault/secrets.enc", "push-vapid.json", "touched/abc.jsonl", "inbox/pic.png", "accounts.json"):
        f = data / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    monkeypatch.setattr(fb, "DATA_DIR", data.resolve())
    for roots in (roots_for(world), roots_for(world, cwd=world["home"] / "cardloop")):
        for rel in ("vault/secrets.enc", "push-vapid.json", "touched/abc.jsonl", "accounts.json"):
            assert status_of(fb.open_raw, str(data / rel), roots) == 403, rel
            assert status_of(fb.read_file, str(data / rel), roots) == 403, rel
        assert status_of(fb.list_dir, str(data), roots) == 403
        assert fb.open_raw(str(data / "inbox" / "pic.png"), roots)[1] == "image/png"   # uploads stay
        names = {e["name"] for e in fb.list_dir(str(world["home"] / "cardloop"), roots)["entries"]}
        assert "data" not in names


def test_the_grok_home_beside_the_data_dir_is_private_and_unlisted(world, monkeypatch):
    """`<data>-grok-home` (grok_engine.grok_home) is a sibling of data/ inside the repo checkout: without this
    the project Files tab listed it and `raw` served its auth.json token."""
    repo = world["home"] / "cardloop"
    data = repo / "data"
    grok = repo / "data-grok-home"
    for rel in ("auth.json", "sessions/x/chat_history.jsonl", "sandbox.toml"):
        f = grok / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("TOKEN")
    (repo / "data-grok-homework.md").write_text("an unrelated sibling with a similar prefix")
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(fb, "DATA_DIR", data.resolve())
    monkeypatch.delenv("GROK_HOME", raising=False)
    for roots in (roots_for(world), roots_for(world, cwd=repo)):
        for rel in ("auth.json", "sessions/x/chat_history.jsonl", "sandbox.toml"):
            assert status_of(fb.open_raw, str(grok / rel), roots) == 403, rel
            assert status_of(fb.read_file, str(grok / rel), roots) == 403, rel
        assert status_of(fb.list_dir, str(grok), roots) == 403
        names = {e["name"] for e in fb.list_dir(str(repo), roots)["entries"]}
        assert "data-grok-home" not in names and "data-grok-homework.md" in names


def test_a_relocated_grok_home_is_private_too(world, monkeypatch):
    home = world["home"] / "elsewhere" / "grok-state"
    home.mkdir(parents=True)
    (home / "auth.json").write_text("TOKEN")
    monkeypatch.setattr(fb, "DATA_DIR", (world["home"] / "cardloop" / "data").resolve())
    monkeypatch.setenv("GROK_HOME", str(home))
    r = roots_for(world)
    assert status_of(fb.read_file, str(home / "auth.json"), r) == 403
    assert status_of(fb.open_raw, str(home / "auth.json"), r) == 403
    monkeypatch.delenv("GROK_HOME")
    assert fb.read_file(str(home / "auth.json"), r)["content"] == "TOKEN"        # the rule follows the variable


def test_a_relocated_secret_store_or_key_is_private_too(world, monkeypatch):
    store = world["home"] / "elsewhere" / "safe.bin"
    key = world["home"] / "elsewhere" / "safe.key"
    store.parent.mkdir()
    store.write_bytes(b"x")
    key.write_bytes(b"k")
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(store))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(key))
    r = roots_for(world)
    assert status_of(fb.read_file, str(store), r) == 403
    assert status_of(fb.open_raw, str(key), r) == 403


def test_recent_does_not_list_a_symlink_whose_target_is_denied(world):
    data = world["tmp"] / "data"
    proj = world["home"] / "proj"
    r = roots_for(world, cwd=proj)
    (proj / "innocent-name.txt").symlink_to(world["home"] / ".ssh" / "authorized_keys")
    (proj / "fine-link.txt").symlink_to(world["home"] / "notes.txt")
    names = {x["name"] for x in fb.recent_files(data, str(proj), r)}
    assert "innocent-name.txt" not in names
    assert "notes.txt" in names           # a link to something reachable shows up as its target


# ── routes ────────────────────────────────────────────────────────────────────

def _routes_app(ctx):
    from aiohttp import web
    import webapp as W
    app = web.Application(middlewares=[W.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/fs/info", W.api_fs_info)
    app.router.add_get("/api/fs/list", W.api_fs_list)
    app.router.add_get("/api/fs/stat", W.api_fs_stat)
    app.router.add_get("/api/fs/file", W.api_fs_file)
    app.router.add_put("/api/fs/file", W.api_fs_file_write)
    app.router.add_post("/api/global/file", W.api_global_file_write)
    return app


@pytest.fixture
def http(world, monkeypatch):
    import webapp as W
    monkeypatch.setattr(Path, "home", staticmethod(lambda: world["home"]))
    monkeypatch.setenv("FILES_EXTRA_ROOTS", str(world["scratch"]))
    ctx = {
        "topics": {"0:1": {"project": "proj", "cwd": str(world["home"] / "proj"), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": "pw", "DATA": world["tmp"] / "data",
        "HERE": ROOT, "save_sessions": lambda: None, "save_topics": lambda: None,
    }
    ctx["_auth_token"] = W._derive_token("pw")
    return ctx, {"Cookie": f"cops_auth={ctx['_auth_token']}"}


async def test_routes_require_auth(aiohttp_client, http):
    ctx, _ = http
    c = await aiohttp_client(_routes_app(ctx))
    for path in ("/api/fs/info", "/api/fs/list?path=/", "/api/fs/file?path=/x", "/api/fs/stat?path=x"):
        assert (await c.get(path)).status == 401, path
    assert (await c.put("/api/fs/file?path=/x", json={"content": ""})).status == 401


async def test_info_starts_in_the_project_and_lists_the_roots(aiohttp_client, http, world):
    ctx, auth = http
    c = await aiohttp_client(_routes_app(ctx))
    data = await (await c.get("/api/fs/info?project=proj", headers=auth)).json()
    assert data["start"] == str(world["home"] / "proj")
    assert {r["path"] for r in data["roots"]} == {str(world["home"]), str(world["home"] / "proj"), str(world["scratch"])}
    assert (await c.get("/api/fs/info?project=nope", headers=auth)).status == 404
    assert (await (await c.get("/api/fs/info", headers=auth)).json())["start"] == str(world["home"])


async def test_list_read_write_round_trip_and_conflict_over_http(aiohttp_client, http, world):
    ctx, auth = http
    c = await aiohttp_client(_routes_app(ctx))
    p = str(world["scratch"] / "report.md")
    lst = await c.get(f"/api/fs/list?path={world['scratch']}", headers=auth)
    assert [e["name"] for e in (await lst.json())["entries"]] == ["report.md"]
    doc = await (await c.get(f"/api/fs/file?path={p}", headers=auth)).json()
    ok = await c.put(f"/api/fs/file?path={p}", json={"content": "v2\n", "base_rev": doc["rev"]}, headers=auth)
    assert ok.status == 200 and (await ok.json())["rev"] != doc["rev"]
    stale = await c.put(f"/api/fs/file?path={p}", json={"content": "v3\n", "base_rev": doc["rev"]}, headers=auth)
    assert stale.status == 409
    assert (world["scratch"] / "report.md").read_text() == "v2\n"
    assert (await c.put(f"/api/fs/file?path={p}", json={"content": 5}, headers=auth)).status == 400
    assert (await c.put(f"/api/fs/file?path={p}", json=[1], headers=auth)).status == 400


async def test_stat_route_and_refusals(aiohttp_client, http, world):
    ctx, auth = http
    c = await aiohttp_client(_routes_app(ctx))
    got = await (await c.get("/api/fs/stat", params={"path": f"`{world['scratch']}/report.md:4`"}, headers=auth)).json()
    assert got["kind"] == "file"
    denied = await c.get(f"/api/fs/file?path={world['home']}/.ssh/authorized_keys", headers=auth)
    assert denied.status == 403
    assert (await c.get(f"/api/fs/list?path={world['foreign']}", headers=auth)).status == 403


async def test_legacy_global_write_can_no_longer_touch_sensitive_dirs(aiohttp_client, http, world):
    """Regression: POST /api/global/file had no sensitive-dir gate, unlike its GET twin."""
    ctx, auth = http
    c = await aiohttp_client(_routes_app(ctx))
    r = await c.post("/api/global/file?path=.ssh/authorized_keys", json={"content": "evil"}, headers=auth)
    assert r.status == 403
    assert (world["home"] / ".ssh" / "authorized_keys").read_text() == "ssh-ed25519 AAA\n"


async def test_raw_route_streams_with_range_and_the_right_headers(aiohttp_client, http, world):
    ctx, auth = http
    from aiohttp import web
    import webapp as W
    app = _routes_app(ctx)
    app.middlewares.append(W.security_headers_middleware)
    app.router.add_get("/api/fs/raw", W.api_fs_raw)
    c = await aiohttp_client(app)
    (world["scratch"] / "a.png").write_bytes(b"\x89PNG" + bytes(range(200)))
    r = await c.get(f"/api/fs/raw?path={world['scratch']}/a.png", headers=auth)
    assert r.status == 200 and r.headers["Content-Type"] == "image/png"
    assert r.headers["Content-Disposition"] == "inline" and "sandbox" in r.headers["Content-Security-Policy"]
    assert (await r.read()).startswith(b"\x89PNG")
    part = await c.get(f"/api/fs/raw?path={world['scratch']}/a.png", headers={**auth, "Range": "bytes=4-9"})
    assert part.status == 206 and await part.read() == bytes(range(6))

    (world["scratch"] / "page.html").write_text("<script>alert(1)</script>")
    h = await c.get(f"/api/fs/raw?path={world['scratch']}/page.html", headers=auth)
    assert h.headers["Content-Type"].startswith("application/octet-stream")
    assert h.headers["Content-Disposition"].startswith("attachment")

    # The blanket X-Frame-Options: DENY would stop the PDF preview being framed; the route
    # must win over it, and only for PDFs.
    (world["scratch"] / "a.pdf").write_bytes(b"%PDF-1.4 x")
    pdf = await c.get(f"/api/fs/raw?path={world['scratch']}/a.pdf", headers=auth)
    assert pdf.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["X-Frame-Options"] == "DENY"

    dl = await c.get(f"/api/fs/raw?path={world['scratch']}/a.png&download=1", headers=auth)
    assert dl.headers["Content-Disposition"].startswith("attachment")

    assert (await c.get(f"/api/fs/raw?path={world['home']}/.ssh/authorized_keys", headers=auth)).status == 403
    assert (await c.get(f"/api/fs/raw?path={world['scratch']}/a.png")).status == 401


async def test_recent_route_needs_a_project(aiohttp_client, http, world):
    ctx, auth = http
    import webapp as W
    app = _routes_app(ctx)
    app.router.add_get("/api/fs/recent", W.api_fs_recent)
    c = await aiohttp_client(app)
    ctx["DATA"].mkdir(parents=True, exist_ok=True)
    proj = world["home"] / "proj"
    fb.record_touched(ctx["DATA"], str(proj), "Write", {"file_path": str(proj / "docs" / "a.md")})
    assert (await c.get("/api/fs/recent", headers=auth)).status == 400
    data = await (await c.get("/api/fs/recent?project=proj", headers=auth)).json()
    assert [i["name"] for i in data["items"]][0] == "a.md"
    assert (await c.get("/api/fs/recent?project=proj")).status == 401


async def test_raw_never_serves_a_gzip_sibling_that_the_policy_did_not_check(aiohttp_client, http, world):
    """Regression (security review): aiohttp answers Accept-Encoding: gzip with `<file>.gz` when it
    exists. `.env.example` is readable, `.env.example.gz` is denied by name — it must not leak."""
    ctx, auth = http
    from aiohttp import web
    import webapp as W
    app = _routes_app(ctx)
    app.router.add_get("/api/fs/raw", W.api_fs_raw)
    c = await aiohttp_client(app)
    import gzip
    (world["scratch"] / "a.png").write_bytes(b"\x89PNG-plain")
    (world["scratch"] / "a.png.gz").write_bytes(gzip.compress(b"SECRET-IN-THE-SIBLING"))
    r = await c.get(f"/api/fs/raw?path={world['scratch']}/a.png", headers={**auth, "Accept-Encoding": "gzip"},
                    auto_decompress=False)
    assert r.status == 200
    assert r.headers.get("Content-Encoding") is None
    assert await r.read() == b"\x89PNG-plain"
    part = await c.get(f"/api/fs/raw?path={world['scratch']}/a.png", headers={**auth, "Accept-Encoding": "gzip", "Range": "bytes=1-3"})
    assert part.status == 206 and await part.read() == b"PNG"
