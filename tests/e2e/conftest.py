"""
Shared fixtures for the E2E Playwright suite (spec-072).

Boots a REAL cockpit subprocess — its own private copy of the app under a fresh tmp
"data/" dir, a random port, a random password, E2E_FAKE_ENGINE=1 (see
e2e_fake_engine.py) — and drives it with a real (headless) browser via Playwright.
Never touches the prod service or prod data/: the subprocess's HERE/DATA resolve
entirely inside a tmp_path_factory directory (see _build_app_copy for why a plain
symlink would NOT achieve this).

Run only via:  venv/bin/python -m pytest tests/e2e -m e2e   (see CLAUDE.md Operations)
The default `pytest tests/` run excludes this suite (pytest.ini: addopts = -m "not e2e").
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Reuse the interpreter that is running pytest right now, rather than assuming
# REPO_ROOT/venv exists: a git worktree checkout (as used by this project's agent
# workflow) shares one venv from the main checkout and does not have its own
# venv/ directory. Whoever invokes `.../venv/bin/python -m pytest ...` already
# picked the right (shared) venv — sys.executable is exactly that interpreter.
VENV_PYTHON = Path(sys.executable)

# One scripted "e2e-<name>" project per scenario keeps each Playwright test's
# transcript pristine — no bubbles bleeding in from another scenario's run.
E2E_PROJECT_IDS = ["e2e-text", "e2e-tool", "e2e-slow", "e2e-busy", "e2e-multiblock",
                   "e2e-plan", "e2e-plan-reload", "e2e-hold", "e2e-hold-queue",
                   "e2e-popout-a", "e2e-popout-b", "e2e-files", "e2e-mermaid"]

# The project-health pill scenarios (test_project_health.py) own one project.
E2E_PROJECT_IDS.append("e2e-health")


def _free_port() -> int:
    """Binds to port 0 to let the OS pick a free one, then releases it. Small
    TOCTOU race (another process could grab it before we launch) — acceptable for
    a local test harness; retried implicitly by pytest on the rare flake."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_app_copy(dest: Path) -> None:
    """Populates `dest` with a private, runnable copy of the app.

    Every top-level *.py module computes its own module directory via
    Path(__file__).resolve().parent — and .resolve() FOLLOWS symlinks back to the
    real file. So a symlinked bot.py would still compute HERE (and therefore
    DATA = HERE/"data") as the real worktree, defeating the whole point of this
    harness (it must never touch the developer's real data/). Real copies of the
    small .py files are the only way to get an isolated HERE/DATA for a full
    subprocess boot; web/dist and other static asset dirs are only ever joined
    onto an already-resolved HERE (no further .resolve() on them), so symlinking
    those is safe and avoids duplicating the ~5MB frontend bundle per test run.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for py in REPO_ROOT.glob("*.py"):
        shutil.copy2(py, dest / py.name)
    for name in ("templates", "features", "tools", "roles"):
        src = REPO_ROOT / name
        if src.is_dir():
            (dest / name).symlink_to(src, target_is_directory=True)

    dist_src = REPO_ROOT / "web" / "dist"
    if not (dist_src / "index.html").exists():
        pytest.fail(
            f"web/dist is missing or unbuilt at {dist_src} — the e2e harness serves the "
            "built cockpit UI and cannot run without it. Build it first: "
            "(cd web && npm run build), or copy an existing build into web/dist "
            "(it is gitignored, never committed).",
            pytrace=False,
        )
    (dest / "web").mkdir(exist_ok=True)
    (dest / "web" / "dist").symlink_to(dist_src, target_is_directory=True)


def _seed_data(dest: Path, project_cwds: dict, extras: "dict | None" = None) -> None:
    """Writes data/topics.json directly — this (not data/registry.json) is what
    _collect_projects()/api_projects reads to populate the sidebar and resolve a
    project id to a cwd+session_key (see webapp.py:_collect_projects). registry.json
    only matters for free-text alias resolution (resolve_project), which the
    Playwright scenarios below never exercise (they click a project row, they don't
    type its name), so it's intentionally left unwritten here."""
    data_dir = dest / "data"
    data_dir.mkdir(exist_ok=True)
    topics = {
        pid: {"project": pid, "cwd": str(cwd), "model": "sonnet", "git_enabled": False,
              **((extras or {}).get(pid) or {})}
        for pid, cwd in project_cwds.items()
    }
    (data_dir / "topics.json").write_text(json.dumps(topics, indent=2))
    (data_dir / "sessions.json").write_text("{}")


def _wait_for_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
        time.sleep(0.2)
    raise RuntimeError(f"e2e cockpit never became healthy at {base_url}: {last_err!r}")


def _cockpit_process(app_dir: Path, env: dict, port: int, info: dict):
    """Starts `bot.py` from `app_dir`, waits for /api/health, yields `info`, then stops it.
    Shared by the plain cockpit (`e2e_server`) and the Grok-enabled one (`e2e_grok_server`)."""
    log_path = app_dir / "server.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), str(app_dir / "bot.py")],
        cwd=str(app_dir),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base_url)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_f.close()
        log_text = log_path.read_text(errors="replace")
        pytest.fail(f"e2e cockpit failed to start.\n--- server.log ---\n{log_text}", pytrace=False)

    try:
        yield info() if callable(info) else info
    finally:
        _stop(proc, log_f)


def _stop(proc, log_f) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log_f.close()


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory):
    """Boots one cockpit subprocess for the whole e2e session. All scenarios share
    it (fast — no per-test process boot) but use distinct pre-seeded projects so
    transcripts never mix between tests."""
    app_dir = tmp_path_factory.mktemp("e2e-app")
    _build_app_copy(app_dir)

    # Isolated $HOME for the subprocess: webapp.py:_sdk_sessions_dir reads/writes SDK
    # conversation transcripts under Path.home()/".claude"/"projects"/<slug> — and
    # e2e_fake_engine.py writes a minimal transcript there too (see its module
    # docstring), so post-turn hydrates don't wipe the chat. Without overriding HOME,
    # both the real app code and the fake engine would touch the OPERATOR'S REAL
    # ~/.claude/projects/ on whatever machine runs this suite.
    fake_home = tmp_path_factory.mktemp("e2e-home")

    project_cwds = {pid: tmp_path_factory.mktemp(f"e2e-proj-{pid.replace('e2e-', '')}") for pid in E2E_PROJECT_IDS}
    # The Files explorer scenarios need a project BELOW $HOME, so its parent folder is
    # reachable ("go above the project") — see test_files_explorer.py.
    files_cwd = fake_home / "work" / "e2e-files"
    files_cwd.mkdir(parents=True)
    project_cwds["e2e-files"] = files_cwd
    _seed_data(app_dir, project_cwds)
    # A scratch root standing in for /tmp (FILES_EXTRA_ROOTS defaults to the real /tmp, where
    # pytest's own tmp dirs live, which would make every other project reachable).
    scratch = tmp_path_factory.mktemp("e2e-scratch")

    port = _free_port()
    password = "e2e-" + os.urandom(8).hex()
    sentinel = "sentinel-" + os.urandom(8).hex()
    env = dict(os.environ)
    env.update({
        "COPS_NO_DOTENV": "1",
        "WEB_PORT": str(port),
        "WEB_PASSWORD": password,
        "E2E_FAKE_ENGINE": "1",
        "CLAUDE_AUTH_MODE": "subscription",
        "HOME": str(fake_home),
        "FILES_EXTRA_ROOTS": str(scratch),
        # spec-096 P3b: a secret-shaped variable the cockpit must NOT hand to its terminal/agents.
        "E2E_SENTINEL_TOKEN": sentinel,
    })
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("WEB_COOKIE_SALT", None)   # a developer's real salt must not leak in (spec-096 P3.1 test)
    # This cockpit is the "Grok switched OFF" half of the suite: a developer's own GROK_* must not turn it on.
    for k in [k for k in env if k.startswith("GROK_")]:
        env.pop(k)

    yield from _cockpit_process(app_dir, env, port, {
        "base_url": f"http://127.0.0.1:{port}",
        "password": password,
        "project_ids": E2E_PROJECT_IDS,
        "app_dir": app_dir,
        "home": fake_home,
        "scratch": scratch,
        "sentinel": sentinel,
    })


# Projects of the Grok-enabled cockpit. `g-*` ids double as the key the fake `grok` wrapper uses to
# pick a recorded wire fixture (tests/e2e/grok_support.py:FIXTURE_BY_PROJECT).
GROK_PROJECTS = ["g-text", "g-tool", "g-fail", "g-board", "g-handoff", "g-hand-desktop", "g-hand-360",
                 "g-hist", "g-new", "g-plain", "g-settings", "g-claude"]


@pytest.fixture(scope="session")
def e2e_grok_server(tmp_path_factory):
    """A SECOND cockpit with GROK_ENABLED=true (spec-095 P5b): the real grok_engine against the
    fake `grok` CLI (tests/fake_grok_acp.py). The plain `e2e_server` keeps GROK_ENABLED unset on
    purpose — it is the "nothing changes when the feature is off" half of the suite."""
    from . import grok_support as gs

    app_dir = tmp_path_factory.mktemp("e2e-grok-app")
    _build_app_copy(app_dir)
    fake_home = tmp_path_factory.mktemp("e2e-grok-home")
    projects_root = tmp_path_factory.mktemp("e2e-grok-projects")
    ids = list(GROK_PROJECTS)
    project_cwds = {}
    for pid in ids:
        cwd = projects_root / pid
        cwd.mkdir()
        project_cwds[pid] = cwd
    _seed_data(app_dir, project_cwds)          # no per-project Grok flag: choosing the provider IS the consent

    bindir = tmp_path_factory.mktemp("e2e-grok-bin")
    wrapper = gs.write_fake_cli(bindir)
    gs.write_login(app_dir / "data-grok-home")      # next to data/, never inside it

    port = _free_port()
    password = "e2e-" + os.urandom(8).hex()
    env = dict(os.environ)
    env.update({
        "COPS_NO_DOTENV": "1",
        "WEB_PORT": str(port),
        "WEB_PASSWORD": password,
        "E2E_FAKE_ENGINE": "1",
        "CLAUDE_AUTH_MODE": "subscription",
        "HOME": str(fake_home),
        "PYTHONUNBUFFERED": "1",   # server.log is read live (the Grok startup verdict) and on a failed boot
    })
    env.update(gs.grok_env(app_dir, bindir, wrapper, tmp_path_factory.mktemp("e2e-grok-denied")))
    env.pop("ANTHROPIC_API_KEY", None)
    for k in [k for k in env if k.startswith("GROK_") and k not in ("GROK_ENABLED", "GROK_BIN", "GROK_SANDBOX_DENY")]:
        env.pop(k)  # a developer's own GROK_HOME / GROK_ENABLED must not leak in
    # `~/.claude` exists on every real host and the sandbox deny list names it when it does — the
    # cockpit creates it on first run. Create it BEFORE the verdict is seeded, or the seed and the
    # server would hash two different deny lists (the cache is keyed by that fingerprint).
    (fake_home / ".claude").mkdir(exist_ok=True)
    gs.seed_availability(app_dir, env)
    gs.seed_usage_rows(app_dir / "data", [
        gs.usage_row(session_id="seed-1", project="g-text", input=12000, output=3000, cached=4000,
                     reasoning=900, age_sec=3600),
        gs.usage_row(session_id="seed-2", project="g-tool", model="grok-4.6", input=8000, output=1500,
                     cached=2000, reasoning=300, age_sec=7200),
    ])

    def info_factory():
        gs.wait_until_available(app_dir / "server.log")
        return {
        "base_url": f"http://127.0.0.1:{port}",
        "password": password,
        "project_ids": ids,
        "app_dir": app_dir,
        "home": fake_home,
        "cwds": project_cwds,
        }

    yield from _cockpit_process(app_dir, env, port, info_factory)


def _login(page, server: dict):
    ui_state_path = server["app_dir"] / "data" / "ui_state.json"
    ui_state_path.unlink(missing_ok=True)
    page.goto(server["base_url"])
    page.fill("#password", server["password"])
    page.click("button.btn-primary[type=submit]")
    page.wait_for_selector(".project-item", timeout=10_000)
    return page


@pytest.fixture
def grok_page(e2e_grok_server, page):
    """A Playwright page logged in to the Grok-enabled cockpit."""
    page.set_default_timeout(10_000)
    return _login(page, e2e_grok_server)


@pytest.fixture
def logged_in_page(e2e_server, page):
    """A Playwright `page` already navigated to the e2e cockpit and authenticated.

    Cross-test isolation note: `e2e_server` is ONE shared subprocess for the whole
    session (fast — no per-test process boot), and each test uses its own project
    id so per-project state (queue/timeline/sessions) never mixes. But the cockpit's
    open-tabs layout (data/ui_state.json) is a single GLOBAL "default" namespace
    (see webapp.py:_ui_state_ns — single-tenant, not per-project) synced across
    devices/browsers. Without a reset, a fresh Playwright context still fetches the
    PREVIOUS test's open-tab list from the server and ends up with N tabs mounted
    (all ProjectViews stay mounted while open, just hidden — see App.tsx "All open
    ProjectViews — always mounted, inactive ones are hidden"), so `.chat-textarea`
    matches more than one element. Deleting the file before each test keeps every
    scenario's DOM to exactly the one project it opened.
    """
    ui_state_path = e2e_server["app_dir"] / "data" / "ui_state.json"
    ui_state_path.unlink(missing_ok=True)

    page.goto(e2e_server["base_url"])
    page.fill("#password", e2e_server["password"])
    page.click("button.btn-primary[type=submit]")
    page.wait_for_selector(".project-item", timeout=10_000)
    return page


def open_project(page, project_id: str) -> None:
    """Clicks the sidebar row for `project_id` and waits for its chat composer."""
    page.click(f".project-item:has-text('{project_id}')")
    page.wait_for_selector(".chat-textarea", timeout=10_000)


def send_chat(page, text: str) -> None:
    """Fills the composer and sends via Enter (handleKeyDown: Enter w/o Shift sends)."""
    ta = page.locator(".chat-textarea")
    ta.fill(text)
    ta.press("Enter")
