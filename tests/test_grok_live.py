"""spec-095 P1: the Grok engine against the REAL `grok` binary (opt-in, spends model turns).

Markers (both excluded from the default run, like `e2e`; select them explicitly):

    venv/bin/python -m pytest tests/test_grok_live.py -m grok_live      # isolation + sandbox + a real turn
    venv/bin/python -m pytest tests/test_grok_live.py -m grok_canary    # egress canary (spec §6.10)

Run the canary by hand after EVERY `grok` update and before enabling a new project: it is the only
control that notices a CLI build that starts uploading repositories.

They skip cleanly (never fail) when the binary, the login or bubblewrap is missing. The login comes
from the cockpit's own GROK_HOME (env GROK_HOME, else <data>/grok-home) — sign it in once with
`tools/grok-acct login`. Everything else is isolated: the canary dir, the probe cache and every
scratch project live under a pytest tmp dir, and the sandbox deny list is the engine's default one
plus ~/.config.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import grok_engine
from grok_engine import GrokTurn, child_env, ensure_home, read_auth_facts

EGRESS_LIMIT = 1024 * 1024
FORBIDDEN_TOOLS = {"ask_user_question", "workflow"}                       # verified absent (hermetic env)
RESIDUAL_TOOLS = {"enter_plan_mode", "exit_plan_mode", "monitor", "scheduler_create",
                  "scheduler_delete", "scheduler_list"}                  # verified STILL present: spec H5 was wrong


# ------------------------------------------------------------------------------------------
# prerequisites -> clean skips
# ------------------------------------------------------------------------------------------

def _skip_reason() -> str | None:
    if not grok_engine.grok_bin():
        return "grok binary not found (GROK_BIN / PATH / ~/.grok/bin)"
    if not shutil.which("bwrap"):
        return "bubblewrap (bwrap) not installed"
    home = grok_engine.grok_home()
    problem = grok_engine._auth_problem(read_auth_facts(home))
    if problem:
        return f"no usable Grok login in {home}: {problem}"
    return None


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)
    tmp = tmp_path_factory.mktemp("grok-live")
    data = tmp / "data"
    data.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setenv("GROK_ENABLED", "true")
    mp.setenv("_CARDLOOP_DATA_DIR", str(data))
    deny = list(grok_engine.DEFAULT_DENY) + ["~/.config", "~/.grok/auth.json"]
    mp.setenv("GROK_SANDBOX_DENY", ",".join(deny))
    grok_engine.reset_cache()
    info = SimpleNamespace(
        tmp=tmp, data=data, home=grok_engine.grok_home(), binary=grok_engine.grok_bin(),
        ctx={"DATA": data, "running": {}})
    yield info
    mp.undo()
    grok_engine.reset_cache()


def _project(live, name: str) -> Path:
    d = live.tmp / name
    d.mkdir()
    return d


async def real_turn(live, cwd: Path, prompt: str, *, key: str = "live:1", resume: str | None = None,
                    interrupt_after_tool: bool = False, timeout: float = 180.0):
    """One real turn through the engine. -> (events, tapped wire haystack, tools list)."""
    events: list[dict] = []
    hay: list[str] = []
    tools: list[str] = []

    def tap(msg: dict) -> None:
        hay.append(grok_engine._haystack(msg))
        upd = (msg.get("params") or {}).get("update") or {}
        if upd.get("sessionUpdate") == "available_commands_update":
            tools.extend((upd.get("_meta") or {}).get("tools") or [])

    async def drive():
        async for ev in grok_engine._run_turn(
                project_name="live", cwd=str(cwd), prompt=prompt, session_key=key, model=None,
                resume_session_id=resume, ctx=live.ctx, effort="low", tap=tap, _gate=False):
            events.append(ev)
            if interrupt_after_tool and ev["type"] == "tool":
                await asyncio.sleep(2.0)
                await live.ctx["running"][key].interrupt()
    await asyncio.wait_for(drive(), timeout)
    return events, "\n".join(hay), tools


def grok_agent_pids() -> set[int]:
    out = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{entry.name}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"agent" in cmd and b"--no-leader" in cmd and b"stdio" in cmd:
            out.add(int(entry.name))
    return out


# ------------------------------------------------------------------------------------------
# §6.4 isolation
# ------------------------------------------------------------------------------------------

@pytest.mark.grok_live
def test_inspect_under_the_engine_env_shows_no_claude_or_cursor_surface(live):
    project = _project(live, "inspect")
    ensure_home(live.ctx, bin_path=live.binary)
    env = child_env(live.home, sandbox=False)
    out = subprocess.run([live.binary, "inspect", "--json"], cwd=project, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-300:]
    doc = json.loads(out.stdout)
    active_mcp = [m for m in doc.get("mcpServers", []) if not m.get("disabled")]
    assert active_mcp == [], f"MCP servers active under the engine env: {[m.get('name') for m in active_mcp]}"
    active_hooks = [h for h in doc.get("hooks", [])
                    if not h.get("disabled") and h.get("vendor") in ("claude", "cursor")]
    assert active_hooks == []
    for kind in ("skills", "agents"):
        leaked = [r.get("name") for r in doc.get(kind, [])
                  if not r.get("disabled") and (r.get("vendor") in ("claude", "cursor")
                                                or (r.get("source") or {}).get("type") in ("claude", "claudeJson"))]
        assert leaked == [], f"{kind} imported from Claude/Cursor: {leaked}"
    cells = (doc.get("externalCompat") or {}).get("cells") or []
    on = [c for c in cells if c.get("vendor") in ("claude", "cursor") and c.get("enabled")
          and c.get("surface") in ("skills", "rules", "hooks", "mcps", "agents", "mcp")]
    assert on == [], f"compat surfaces still on: {on}"
    assert doc.get("projectInstructions") == []


@pytest.mark.grok_live
async def test_a_real_turn_exposes_no_interactive_tools(live):
    project = _project(live, "tools")
    events, _, tools = await real_turn(live, project, "Reply with the single word OK. Use no tools.")
    assert events[-1]["type"] == "result", events[-1]
    assert tools, "the agent never announced its tool list"
    assert not (set(tools) & FORBIDDEN_TOOLS), set(tools) & FORBIDDEN_TOOLS
    # What the spec expected gone (H5) but is NOT: still callable under a yolo sandboxed turn.
    # If this list ever shrinks the spec/§10 can be tightened; if it grows, review the new tool.
    print("residual tools present:", sorted(set(tools) & RESIDUAL_TOOLS))
    unexpected = {t for t in tools if re.match(r"^(scheduler_|monitor|workflow|ask_)", t)} - RESIDUAL_TOOLS
    assert not unexpected, unexpected


# ------------------------------------------------------------------------------------------
# §6.5 sandbox
# ------------------------------------------------------------------------------------------

@pytest.mark.grok_live
async def test_sandbox_denies_listed_paths_allows_cwd_write_and_git(live, monkeypatch):
    project = _project(live, "sandbox")
    denied = live.tmp / "denied-dir"
    denied.mkdir()
    token = "DENIED-" + os.urandom(8).hex()
    (denied / "secret.txt").write_text(token)
    monkeypatch.setenv("GROK_SANDBOX_DENY", os.environ["GROK_SANDBOX_DENY"] + f",{denied}")
    outside = Path.home() / f".grok-live-should-not-exist-{os.getpid()}"
    real_dirs = []
    for d in (".claude", ".claude-accounts", ".ssh"):
        p = Path.home() / d
        if p.is_dir():
            real_dirs.append((p, sorted(x.name for x in p.iterdir())[:15]))
    ls_cmd = "; ".join(f"echo LS-{p.name}:; ls -A {p} 2>&1" for p, _ in real_dirs)
    prompt = (
        "Security self-test. Run exactly this ONE shell command with your terminal tool and then "
        "reply DONE:\n"
        f"cat {denied}/secret.txt 2>&1; echo '--W--'; echo cwd-write-ok > w.txt && cat w.txt; "
        "echo '--G--'; git init -q . && git add -A && git -c user.email=t@example.invalid "
        "-c user.name=t commit -qm live-test-commit && git log --oneline | head -1; "
        f"echo '--H--'; echo x > {outside} 2>&1; echo \"home-write-exit=$?\"; echo '--L--'; {ls_cmd or 'true'}")
    try:
        events, hay, _ = await real_turn(live, project, prompt, key="live:sb")
        assert events[-1]["type"] == "result", events[-1]
        assert token not in hay, "the denied file's content reached the model"
        assert "cwd-write-ok" in hay, "writing inside the project directory failed"
        assert "live-test-commit" in hay, "git did not work inside the sandbox"
        assert "home-write-exit=0" not in hay and not outside.exists(), "a write to $HOME succeeded"
        for p, names in real_dirs:   # bind-over deny: the real directory listing must not show through
            assert not any(n in hay.split(f"LS-{p.name}:")[-1].split("LS-")[0] for n in names if len(n) > 3), p
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.grok_live
async def test_missing_bwrap_means_unavailable_not_unsandboxed(live, monkeypatch):
    monkeypatch.setenv("PATH", str(live.tmp / "no-bwrap-here"))
    grok_engine.reset_cache()
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is False and "bwrap" in info["error"]
    events = [e async for e in grok_engine.run_grok_engine(
        project_name="live", cwd=str(_project(live, "nobwrap")), prompt="hi", session_key="live:nb", ctx=live.ctx)]
    assert [e["type"] for e in events] == ["error"] and "bwrap" in str(events[0]["exc"])
    grok_engine.reset_cache()


@pytest.mark.grok_live
async def test_provider_info_and_the_real_sandbox_probe(live):
    grok_engine.reset_cache()
    grok_engine.reset_sandbox_probe(live.ctx)
    info = await grok_engine.provider_info(force=True)
    assert info["available"] is True, info["error"]
    assert info["sandbox"]["probe"] == "ok"
    assert "grok-4.7" in [m["value"] for m in info["models"]]
    assert info["version"], info
    print("warnings:", info["warnings"], "version:", info["version"])


# ------------------------------------------------------------------------------------------
# one real end-to-end session: streams, stops, resumes, leaves nothing behind
# ------------------------------------------------------------------------------------------

@pytest.mark.grok_live
async def test_real_turn_streams_interrupts_and_resumes(live):
    project = _project(live, "e2e")
    before = grok_agent_pids()
    events, _, _ = await real_turn(
        live, project, "Run the shell command `echo marker-one > m.txt && cat m.txt` and tell me what it printed.",
        key="live:e2e")
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "result" and "error" not in kinds, kinds
    assert "tool" in kinds and ("text_delta" in kinds or "text" in kinds)
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["name"] == "Bash" and "marker-one" in tool["input"]["command"]
    assert (project / "m.txt").read_text().strip() == "marker-one"
    sid = events[-1]["provider_session_id"]
    assert sid and events[-1]["usage"]["total_tokens"] > 0 and events[-1]["context_tokens"] > 0
    assert live.ctx["running"]["live:e2e"] is True

    # an operator stop in the middle of a long tool
    t0 = time.monotonic()
    events2, _, _ = await real_turn(
        live, project, "Run the shell command `sleep 90` and then say done.", key="live:e2e2",
        interrupt_after_tool=True)
    assert events2[-1]["type"] == "result" and not [e for e in events2 if e["type"] == "error"]
    assert time.monotonic() - t0 < 60, "interrupt() did not stop the turn promptly"

    # a second call resumes the FIRST session with its context
    events3, _, _ = await real_turn(
        live, project, "What word did the earlier shell command print? Answer with just that word.",
        key="live:e2e3", resume=sid)
    assert events3[-1]["type"] == "result", events3[-1]
    assert events3[-1]["provider_session_id"] == sid
    answer = "".join(e["text"] for e in events3 if e["type"] == "text").lower()
    assert "marker-one" in answer or "marker" in answer, answer

    await asyncio.sleep(0.5)
    assert grok_agent_pids() - before == set(), "a grok agent process outlived its turn"
    leftovers = [p.name for p in live.home.iterdir() if p.name.startswith("sandbox-blocked")]
    assert leftovers == [], leftovers
    rows = [json.loads(x) for x in (live.data / "grok_usage.jsonl").read_text().splitlines()]
    assert len(rows) >= 3 and all(r["provider"] == "grok" for r in rows)


# ------------------------------------------------------------------------------------------
# §6.10 egress canary
# ------------------------------------------------------------------------------------------

def _group_pids(pgid: int) -> set[int]:
    out = set()
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            st = grok_engine._proc_stat(int(entry.name))
            if st and st[1] == pgid:
                out.add(int(entry.name))
    return out


class _Egress:
    """Samples `ss -tinp` and keeps the max bytes_sent seen per connection of the Grok group."""

    def __init__(self):
        self.pgid: int | None = None
        self.max_sent: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            if self.pgid:
                self._sample()
            time.sleep(0.15)

    def _sample(self):
        pids = _group_pids(self.pgid)
        out = subprocess.run(["ss", "-tinpH", "state", "established"], capture_output=True, text=True).stdout
        lines = out.splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"pid=(\d+)", line)
            if not m or int(m.group(1)) not in pids:
                continue
            ident = " ".join(line.split()[3:5])
            detail = " ".join(lines[i + 1:i + 2])
            sent = re.search(r"bytes_sent:(\d+)", detail)
            if sent:
                self.max_sent[ident] = max(self.max_sent.get(ident, 0), int(sent.group(1)))


@pytest.mark.grok_canary
async def test_a_repo_with_a_big_history_blob_is_not_uploaded(live):
    """spec §6.10 / M15: a repo with a multi-MB blob in git history, prompt 'reply OK, open
    nothing'. No single connection may send more than 1 MiB (the system prompt + schemas are
    ~90 KiB). One turn on one CLI version proves nothing about the next release — re-run it."""
    if not shutil.which("ss"):
        pytest.skip("`ss` (iproute2) not installed")
    repo = _project(live, "canary-repo")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    git("init", "-q")
    (repo / "blob.bin").write_bytes(os.urandom(8 * 1024 * 1024))   # incompressible: ~8 MB if it ever leaves
    (repo / "README.md").write_text("canary repo\n")
    git("add", "-A")
    git("commit", "-qm", "add blob")
    git("rm", "-q", "blob.bin")
    git("commit", "-qm", "remove blob (it stays in history)")

    egress = _Egress()
    egress.start()

    async def watch_pgid():
        for _ in range(600):
            turn = live.ctx["running"].get("live:canary")
            acp = getattr(turn, "_acp", None)
            if acp is not None:
                egress.pgid = acp.pgid
                return
            await asyncio.sleep(0.05)
    watcher = asyncio.ensure_future(watch_pgid())
    try:
        events, _, _ = await real_turn(live, repo, "Reply with exactly OK. Open no files and use no tools.",
                                       key="live:canary")
    finally:
        watcher.cancel()
        egress.stop()
    assert events[-1]["type"] == "result", events[-1]
    sent = egress.max_sent
    print("egress per connection (bytes_sent max):", sent)
    assert sent, "the sampler saw no connection of the Grok process group — the canary measured nothing"
    worst = max(sent.values())
    assert worst < EGRESS_LIMIT, f"a single connection sent {worst} bytes (> {EGRESS_LIMIT}): the repo may be leaving the box"
    assert GrokTurn  # (the turn handle type is part of what was exercised)
