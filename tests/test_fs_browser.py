"""Explorer policy (fs_browser.py) and its /api/fs/* routes.

The policy is the security boundary of the Files tab now that it can leave a project's cwd,
so the tests pin the refusals, not just the happy path.
"""
import os
import sys
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
