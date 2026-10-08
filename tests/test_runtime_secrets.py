"""spec-096 P3b — the cockpit's secrets stay out of every child process and out of reach of other
same-user processes.

Two mechanisms (runtime_secrets.py): a startup scrub that moves secret variables out of
os.environ into a private snapshot (children never inherit them), and prctl(PR_SET_DUMPABLE, 0)
(/proc/<pid>/environ & co. become root-owned). The tests that matter run the REAL bot.py as the
program in a subprocess; the unit tests pin the name policy and every in-process reader.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import runtime_secrets as rs

ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")

# Every name the task lists explicitly, each with a recognisable value.
LISTED = {n: f"sekret-{n.lower()}-0123456789" for n in rs.EXPLICIT_NAMES}
# Secrets caught by the suffix patterns only (not in the explicit list).
PATTERN_ONLY = {
    "GITHUB_TOKEN": "sekret-gh-0123456789", "DB_PASSWORD": "sekret-db-0123456789",
    "HMAC_SALT": "sekret-salt-0123456789", "STRIPE_SECRET": "sekret-stripe-0123456789",
    "OPENAI_API_KEY": "sekret-oai-0123456789",
}
# Must survive: config that merely LOOKS secret-ish, and the credentials a child needs itself.
KEPT = {
    "CLAUDE_CLI_PATH": "/opt/claude", "GROK_ENABLED": "true", "WEB_PORT": "9999",
    "SECOND_OPINION_AZURE_MAX_TOKENS": "2500", "AUTOPILOT_DAILY_TOKEN_CAP": "2000000",
    "ANTHROPIC_API_KEY": "sk-ant-keepme", "ANTHROPIC_AUTH_TOKEN": "keep-anthropic-token",
    "CLAUDE_CODE_OAUTH_TOKEN": "keep-oauth-token", "CLAUDE_OPS_SECRET_KEYFILE": "/tmp/keyfile",
}


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    rs.reset_for_tests()
    yield
    rs.reset_for_tests()


# ─────────────────────────── name policy ───────────────────────────

def test_scrub_removes_every_listed_and_pattern_name_and_returns_names_only():
    env = {**LISTED, **PATTERN_ONLY, **KEPT}
    removed = rs.scrub(env)
    assert sorted(removed) == sorted([*LISTED, *PATTERN_ONLY])
    for name in (*LISTED, *PATTERN_ONLY):
        assert name not in env
    assert "sekret" not in " ".join(removed)          # the report is names, never values


def test_scrub_leaves_config_and_the_childs_own_credentials_alone():
    env = {**LISTED, **KEPT}
    rs.scrub(env)
    assert {k: env[k] for k in KEPT} == KEPT


def test_agent_env_passthrough_keeps_a_name_visible_to_children():
    env = {**PATTERN_ONLY, rs.PASSTHROUGH_VAR: " github_token , DB_PASSWORD ,"}
    rs.scrub(env)
    assert env["GITHUB_TOKEN"] == PATTERN_ONLY["GITHUB_TOKEN"]      # case-insensitive, trimmed
    assert env["DB_PASSWORD"] == PATTERN_ONLY["DB_PASSWORD"]
    assert "HMAC_SALT" not in env                                    # the rest is still scrubbed
    assert env[rs.PASSTHROUGH_VAR]                                   # the opt-out itself is not a secret


def test_passthrough_can_keep_even_an_explicitly_listed_name():
    env = {"OLLAMA_AUTH_TOKEN": "tok-0123456789", rs.PASSTHROUGH_VAR: "OLLAMA_AUTH_TOKEN"}
    rs.scrub(env)
    assert env["OLLAMA_AUTH_TOKEN"] == "tok-0123456789"


def test_scrub_is_idempotent_and_keeps_the_snapshot():
    env = {"WEB_PASSWORD": "pw-0123456789"}
    assert rs.scrub(env) == ["WEB_PASSWORD"]
    assert rs.scrub(env) == []
    assert rs.snapshot_names() == ["WEB_PASSWORD"]
    assert rs.secret_values() == ["pw-0123456789"]


def test_get_answers_from_the_snapshot_and_a_live_value_wins(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "from-startup")
    rs.scrub()
    assert "WEB_PASSWORD" not in os.environ
    assert rs.get("WEB_PASSWORD") == "from-startup"
    assert rs.get("NOT_A_SECRET_NAME", "dflt") == "dflt"
    monkeypatch.setenv("WEB_PASSWORD", "set-by-a-test")             # tools/tests that set env still work
    assert rs.get("WEB_PASSWORD") == "set-by-a-test"


def test_get_without_any_scrub_is_plain_environ(monkeypatch):
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "plain-0123456789")
    assert rs.get("TWOCAPTCHA_API_KEY") == "plain-0123456789"


# ─────────────────────── every in-process reader ───────────────────────

@pytest.fixture
def scrubbed(monkeypatch):
    """The real os.environ carrying a value for every secret, then scrubbed like bot.py does."""
    for name, value in LISTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(rs.PASSTHROUGH_VAR, raising=False)
    removed = rs.scrub()
    for name in LISTED:
        assert name not in os.environ, f"{name} survived the scrub"
    assert sorted(removed) == sorted(LISTED)
    return LISTED


def test_webapp_salt_resolution_reads_the_snapshot(scrubbed, tmp_path, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "AUTH_SALT", webapp.AUTH_SALT)      # restored on teardown
    webapp._init_auth_salt({"DATA": tmp_path})
    assert webapp.AUTH_SALT == scrubbed["WEB_COOKIE_SALT"].encode()


def test_engine_ctx_password_reads_the_snapshot(scrubbed):
    import engine
    assert engine._build_ctx(web_port=8787)["password"] == scrubbed["WEB_PASSWORD"]


def test_second_opinion_key_reads_the_snapshot(scrubbed):
    import second_opinion
    assert second_opinion._azure_key() == scrubbed["AZURE_FOUNDRY_KEY"]


def test_captcha_key_reads_the_snapshot(scrubbed, monkeypatch):
    import captcha_solver
    monkeypatch.setattr(captcha_solver, "_secretstore", None)       # never fall through to the vault
    assert captcha_solver.api_key() == scrubbed["TWOCAPTCHA_API_KEY"]
    assert captcha_solver.configured() is True


def test_ollama_overlay_still_carries_its_token_into_the_claude_child(scrubbed):
    """OLLAMA_AUTH_TOKEN is scrubbed from os.environ yet must still reach the per-run overlay the
    engine builds for a local-backend turn (engine.py: auth_token=_ollama_mod.auth_token())."""
    import ollama_backend
    import runtime
    assert ollama_backend.auth_token() == scrubbed["OLLAMA_AUTH_TOKEN"]
    rc = runtime.RunContext(origin_kind="chat", origin_id="c1", provider="claude", backend="ollama",
                            model="qwen", account="main", revision=0)
    overlay = runtime.ollama_env_overlay(rc, base_url="http://127.0.0.1:11434",
                                         auth_token=ollama_backend.auth_token())
    assert overlay.to_set["ANTHROPIC_AUTH_TOKEN"] == scrubbed["OLLAMA_AUTH_TOKEN"]


def test_secretstore_master_key_reads_the_snapshot(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    import secretstore
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEY", key)
    monkeypatch.setenv("CLAUDE_OPS_SECRET_STORE", str(tmp_path / "store.enc"))
    monkeypatch.setenv("CLAUDE_OPS_SECRET_KEYFILE", str(tmp_path / "no-such-keyfile"))
    rs.scrub()
    assert "CLAUDE_OPS_SECRET_KEY" not in os.environ
    secretstore.set("probe", "value-0123456789")
    assert secretstore.get("probe") == "value-0123456789"


@pytest.mark.parametrize("collector, name, header", [
    ("_collect_coolify", "COOLIFY_API_TOKEN", "Authorization"),
    ("_collect_n8n", "N8N_API_KEY", "X-N8N-API-KEY"),
])
async def test_schedule_sources_send_the_snapshot_credential(scrubbed, monkeypatch, collector, name, header):
    import aiohttp

    import schedules
    seen: list[dict] = []

    class _Resp:
        status = 500
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def get(self, url, headers=None, **kw):
            seen.append(dict(headers or {}))
            return _Resp()

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session())
    await getattr(schedules, collector)({})
    assert seen, f"{collector} skipped the request although {name} is configured"
    assert scrubbed[name] in seen[0][header]


def test_grok_log_redaction_still_masks_the_scrubbed_secrets(scrubbed):
    import grok_engine
    line = f"login failed for {scrubbed['WEB_PASSWORD']} and {scrubbed['AZURE_FOUNDRY_KEY']}"
    out = grok_engine._redact(line)
    assert scrubbed["WEB_PASSWORD"] not in out and scrubbed["AZURE_FOUNDRY_KEY"] not in out
    assert out.count("***") == 2


def test_grok_child_env_stays_hermetic(scrubbed, tmp_path):
    import grok_engine
    env = grok_engine.child_env(tmp_path / "home")
    assert not set(scrubbed) & set(env)
    assert not any(v == s for v in env.values() for s in scrubbed.values())


# ───────────── the real bot.py, started as the program, in a subprocess ─────────────

_RUN_BOT = textwrap.dedent('''
    import json, os, runpy, subprocess, sys
    sys.argv = ["bot.py", "--help"]          # parse_args() exits 0 right after the module-level startup
    try:
        runpy.run_path(os.environ["P3B_BOT"], run_name="__main__")
    except SystemExit:
        pass
    import ctypes, runtime_secrets
    child = subprocess.run(["env"], capture_output=True, text=True).stdout
    child_names = {ln.split("=", 1)[0] for ln in child.splitlines() if "=" in ln}
    names = json.loads(os.environ["P3B_NAMES"])
    out = {
        "environ": sorted(n for n in names if n in os.environ),
        "child_env": sorted(n for n in names if n in child_names),
        "child_env_text_has_a_value": any(v in child for v in json.loads(os.environ["P3B_VALUES"])),
        "get": {n: runtime_secrets.get(n) for n in names},
        "dumpable": ctypes.CDLL(None).prctl(3, 0, 0, 0, 0),
    }
    open(os.environ["P3B_OUT"], "w").write(json.dumps(out))
''')


def _start_cockpit_process(tmp_path: Path, extra_env: dict) -> dict:
    """Run bot.py as __main__ (stops at --help, after every module-level startup step) and report."""
    names = [*LISTED, *PATTERN_ONLY, *KEPT]
    values = [*LISTED.values(), *PATTERN_ONLY.values(), *KEPT.values()]
    out = tmp_path / "out.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT), "COPS_NO_DOTENV": "1", "CLAUDE_AUTH_MODE": "api_key",
        "P3B_BOT": str(ROOT / "bot.py"), "P3B_NAMES": json.dumps(names),
        "P3B_VALUES": json.dumps(values), "P3B_OUT": str(out),
        **LISTED, **PATTERN_ONLY, **KEPT, **extra_env,
    }
    done = subprocess.run([sys.executable, "-c", _RUN_BOT], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=180)
    assert out.exists(), f"cockpit start-up script died:\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}"
    result = json.loads(out.read_text())
    result["stdout"] = done.stdout
    return result


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_started_cockpit_hands_no_secret_to_any_child(tmp_path):
    r = _start_cockpit_process(tmp_path, {})
    hidden = [*LISTED, *PATTERN_ONLY]
    assert r["environ"] == sorted(KEPT), "only the kept names may remain in the cockpit's own os.environ"
    assert not set(r["child_env"]) & set(hidden), "an `env` child still sees a secret"
    assert r["child_env"] == sorted(KEPT)
    assert r["child_env_text_has_a_value"] is True          # ...but the KEPT values are there (control)
    for name, value in {**LISTED, **PATTERN_ONLY}.items():
        assert r["get"][name] == value, f"in-process readers lost {name}"
    # the startup line names the variables and never prints a value
    assert "[security]" in r["stdout"] and "secret variable(s) removed" in r["stdout"]
    assert "sekret-" not in r["stdout"]


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_started_cockpit_honours_agent_env_passthrough(tmp_path):
    r = _start_cockpit_process(tmp_path, {rs.PASSTHROUGH_VAR: "GITHUB_TOKEN,WEB_COOKIE_SALT"})
    assert "GITHUB_TOKEN" in r["child_env"] and "WEB_COOKIE_SALT" in r["child_env"]
    assert "WEB_PASSWORD" not in r["child_env"] and "DB_PASSWORD" not in r["child_env"]


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_started_cockpit_is_non_dumpable(tmp_path):
    assert _start_cockpit_process(tmp_path, {})["dumpable"] == 0


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_importing_bot_changes_nothing_about_the_process(tmp_path):
    """Tests and tools `import bot`; only running it AS the program may harden/scrub."""
    code = ("import os, ctypes, bot; "
            "print(ctypes.CDLL(None).prctl(3,0,0,0,0), 'WEB_PASSWORD' in os.environ)")
    env = {**os.environ, "WEB_PASSWORD": "pw-0123456789", "COPS_NO_DOTENV": "1", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True,
                         text=True, timeout=180).stdout.split()[-2:]
    assert out == ["1", "True"]


# ─────────────────── the non-dumpable process and its neighbours ───────────────────

_HARDENED_CHILD = textwrap.dedent('''
    import json, os, subprocess, sys, time
    import runtime_secrets
    applied = runtime_secrets.harden_process(force=True)
    print(json.dumps({"applied": applied, "pid": os.getpid()}), flush=True)
    # What the cockpit itself does from this state:
    from pathlib import Path
    import load_monitor, webapp
    kid = subprocess.Popen(["bash", "-c", "exec -a claude sleep 30"], cwd=sys.argv[1])
    time.sleep(0.5)
    cg = Path(sys.argv[2]); cg.mkdir(exist_ok=True)
    (cg / "cgroup.procs").write_text(f"{os.getpid()}\\n{kid.pid}\\n")
    changed = webapp._oom_raise_children(cg, os.getpid(), 700)           # the OOM shield tick
    scan = load_monitor.scan_processes(load_monitor.DEFAULT_FS, os.getpid(), cg)  # the load meter tick
    top = webapp._memory_top_offenders(cg)                                # the memory alert
    adj = int(Path(f"/proc/{kid.pid}/oom_score_adj").read_text())
    plain = subprocess.run([sys.executable, "-c", "print(open('/proc/self/environ','rb').read()[:1] != b'')"],
                           capture_output=True, text=True).stdout.strip()
    print(json.dumps({"kid": kid.pid, "shield_changed": changed, "kid_adj": adj,
                      "scan_total": scan["claude_total"] if scan else None,
                      "scan_project": scan["chats"][0]["project"] if scan and scan["chats"] else None,
                      "cockpit_mb": scan["cockpit_mb"] if scan else None,
                      "top_lines": len(top), "child_reads_own_environ": plain}), flush=True)
    sys.stdin.readline()                       # stay alive until the test has probed us
    kid.kill()
''')


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_non_dumpable_cockpit_hides_environ_but_nothing_the_cockpit_or_doctor_reads(tmp_path):
    """The experiment of the spec, kept as a regression test: a non-dumpable process, a second
    same-user process probing it, and the cockpit's OWN readers (OOM shield, load meter, memory
    alert) running from inside it against a child."""
    work = tmp_path / "proj"
    work.mkdir()
    proc = subprocess.Popen([sys.executable, "-c", _HARDENED_CHILD, str(work), str(tmp_path / "cg")],
                            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "COPS_NO_DOTENV": "1"},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        head = json.loads(proc.stdout.readline())
        assert head["applied"] is True
        pid = head["pid"]
        inside = json.loads(proc.stdout.readline())
        p = Path(f"/proc/{pid}")

        # Denied to a same-user process: the secret-bearing and ptrace-gated entries.
        for entry in ("environ", "maps", "mem", "io"):
            with pytest.raises(PermissionError):
                open(p / entry, "rb").read(1)
        with pytest.raises(PermissionError):
            os.listdir(p / "fd")
        with pytest.raises(PermissionError):
            os.readlink(p / "cwd")
        # Still readable: everything doctor / the load meter / the memory alert read from OUTSIDE.
        assert "VmRSS" in (p / "status").read_text()
        assert (p / "stat").read_text().startswith(f"{pid} ")
        assert b"-c" in (p / "cmdline").read_bytes()
        assert (p / "cgroup").read_text()

        # The cockpit's readers, run from inside the non-dumpable process, against its child.
        assert inside["shield_changed"] == 1 and inside["kid_adj"] == 700          # child is dumpable after exec
        assert inside["scan_total"] == 1 and inside["scan_project"] == "proj"      # cwd readlink of the child
        assert inside["cockpit_mb"] and inside["cockpit_mb"] > 0                   # its own VmRSS
        assert inside["top_lines"] >= 1
        assert inside["child_reads_own_environ"] == "True"                         # a child is NOT hardened

        # And the doctor line on top of it.
        sys.path.insert(0, str(ROOT / "tools"))
        import doctor
        facts = doctor.probe_process_hardening(pid)
        fact = next(f for f in facts if f.label == "Process hardening")
        assert fact.level == "ok" and fact.value.startswith("non-dumpable"), fact
    finally:
        try:
            proc.stdin.write("\n"); proc.stdin.flush()
        except Exception:
            pass
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(not LINUX, reason="procfs / prctl")
def test_doctor_reports_a_dumpable_process_and_never_greens_what_it_cannot_measure(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import doctor
    mine = doctor.probe_process_hardening(os.getpid())          # this very test process is dumpable
    fact = next(f for f in mine if f.label == "Process hardening")
    assert fact.level == "warn" and "DUMPABLE" in fact.value and "P3b" in fact.remedy
    gone = doctor.probe_process_hardening(2 ** 22 + 12345)       # no such pid
    fact = next(f for f in gone if f.label == "Process hardening")
    assert fact.level == "info" and "not measurable" in fact.value
    # a root-run cockpit cannot be judged from owners: say so, do not guess
    fake = tmp_path / "proc" / "77"
    fake.mkdir(parents=True)
    (fake / "environ").write_text("")
    root_owned = doctor.probe_process_hardening(77, proc_root=tmp_path / "proc")
    if os.getuid() == 0:                                         # only meaningful when the dir is uid 0
        assert next(f for f in root_owned if f.label == "Process hardening").level == "info"


def test_ptrace_scope_note_is_info_only(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import doctor
    y = tmp_path / "ptrace_scope"
    y.write_text("0\n")
    f = next(f for f in doctor.probe_process_hardening(2 ** 22 + 1, yama=y) if f.label == "kernel.yama.ptrace_scope")
    assert f.level == "info" and "ptrace_scope=1" in f.remedy
    y.write_text("1\n")
    f = next(f for f in doctor.probe_process_hardening(2 ** 22 + 1, yama=y) if f.label == "kernel.yama.ptrace_scope")
    assert f.level == "ok"
    assert not [f for f in doctor.probe_process_hardening(2 ** 22 + 1, yama=tmp_path / "absent")
                if f.label == "kernel.yama.ptrace_scope"]


# ─────────────────────────── harden_process itself ───────────────────────────

def test_harden_process_is_skipped_inside_pytest():
    assert "pytest" in sys.modules
    assert rs.harden_process() is False
    if LINUX:
        import ctypes
        assert ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 1      # still dumpable


def test_harden_process_failure_is_one_warning_and_the_cockpit_continues(monkeypatch, capsys):
    import ctypes

    def _boom(*a, **kw):
        raise OSError("no libc for you")
    monkeypatch.setattr(ctypes, "CDLL", _boom)
    assert rs.harden_process(force=True) is False
    out = capsys.readouterr().out
    assert out.count("[security] WARNING") == 1
    assert "stays readable" in out


def test_harden_process_is_a_no_op_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert rs.harden_process(force=True) is False


def test_harden_process_reports_a_prctl_refusal(monkeypatch, capsys):
    class _Libc:
        def prctl(self, *a):
            return -1
    import ctypes
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: _Libc())
    monkeypatch.setattr(ctypes, "get_errno", lambda: 1)
    assert rs.harden_process(force=True) is False
    assert "[security] WARNING" in capsys.readouterr().out
