#!/usr/bin/env python3
"""Record real Grok Build ACP wire fixtures for spec-095 (tests/fixtures/grok/*.jsonl).

Spawns the real `grok agent --no-leader stdio` under the hermetic child env the cockpit
engine uses, speaks ACP over stdio, and writes EVERY line in both directions as

    {"dir": "c2a" | "a2c", "msg": <json>, "t": <seconds since spawn, float>}

after scrubbing: session ids -> SESSION_n, uuids -> UUID_n, the account email ->
user@example.invalid, absolute paths -> $CWD / $GROK_HOME / $HOME, token-looking strings and
the values of the login file -> REDACTED. A fixture that still contains a secret-looking
value is refused (verify pass) and deleted.

Re-run after any `grok` CLI bump to refresh the fixtures:

    python3 tools/grok_record_fixtures.py --list
    python3 tools/grok_record_fixtures.py --scenario all
    python3 tools/grok_record_fixtures.py --scenario simple_text --scenario tool_read

Everything is parameterised by argument or environment (nothing machine specific):

    --grok-bin / $GROK_BIN            the grok binary          (default: `grok` on PATH, else ~/.grok/bin/grok)
    --auth-src / $GROK_RECORD_AUTH    auth.json to borrow      (default: $GROK_HOME/auth.json, else ~/.grok/auth.json)
    --scratch-dir                     parent for the throwaway GROK_HOME + project (default: a mkdtemp under $TMPDIR)
    --out-dir                         fixture directory        (default: <repo>/tests/fixtures/grok)
    --deny                            extra sandbox deny paths (default: the cockpit's D4 list, only entries that exist)

The login file is COPIED (mode 600) into a scratch GROK_HOME and the whole scratch tree is
deleted at the end (also on error). Nothing is read from the login file except key NAMES and
the retention flag; secret values are loaded only to be scrubbed from the output.

Every turn runs under a custom sandbox profile (`extends = "workspace"` + a deny list) and
the prompts only touch the scratch project. Never point this at a real repository.
"""
from __future__ import annotations

import argparse
import collections
import getpass
import json
import os
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "tests" / "fixtures" / "grok"
PROFILE = "cardloop"
HANDSHAKE_TIMEOUT = 25.0

# D3 hermetic env (spec-095 §4): everything that is NOT an allowlisted parent variable.
HERMETIC = {
    "GROK_TELEMETRY_ENABLED": "0", "GROK_TELEMETRY_TRACE_UPLOAD": "0", "GROK_MEMORY": "0",
    "GROK_ASK_USER_QUESTION": "0", "GROK_AUTO_WAKE": "0", "GROK_WORKFLOWS": "0",
    "GROK_DISABLE_AUTOUPDATER": "1",
}
for _v in ("AGENTS", "HOOKS", "MCPS", "RULES", "SKILLS"):
    HERMETIC[f"GROK_CLAUDE_{_v}_ENABLED"] = "0"
    HERMETIC[f"GROK_CURSOR_{_v}_ENABLED"] = "0"
ENV_ALLOW = ("PATH", "HOME", "LANG", "TERM", "TMPDIR")

# Default sandbox deny entries, HOME-relative; only those that exist are used (a missing deny
# path is materialised as an empty 0444 file by Grok, spec §10-C7). `.grok` itself is NOT here:
# the grok binary lives in ~/.grok/downloads, denying the directory stops bwrap from exec'ing it.
DEFAULT_DENY = (".claude", ".claude-accounts", ".ssh", ".aws", ".gnupg", ".config", ".grok/auth.json")


# --------------------------------------------------------------------------- scrubbing

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")
_PREFIXED = re.compile(r"\b(?:xai|sk|pk|ghp|gho|ghu|ghs|github_pat|AKIA|ASIA|AIza|ya29|Bearer)[-_ ]?[A-Za-z0-9_./+-]{16,}")
_BLOB = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-])")


def _looks_secret_blob(tok: str) -> bool:
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    # "a/b/c"-style path remnants and long snake_case identifiers are not secrets
    return has_digit and has_alpha and not tok.startswith(("SESSION_", "UUID_"))


def _safe(fn):
    try:
        return fn()
    except Exception:
        return ""


class Scrubber:
    def __init__(self, *, cwd: str, grok_home: str, home: str, secrets: list[str], email: Optional[str]):
        self.paths = []  # (needle, replacement), longest needle first
        for needle, repl in ((cwd, "$CWD"), (grok_home, "$GROK_HOME"), (home, "$HOME")):
            for n in {needle, os.path.realpath(needle)}:
                if n and n != "/":
                    self.paths.append((n, repl))
        self.paths.sort(key=lambda p: -len(p[0]))
        self.secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 8}, key=len, reverse=True)
        self.email = email
        # host-identifying words that show up in agent output (`ls -l`, `hostname`, `_meta.hostname`)
        self.words = []
        for word, repl in ((_safe(getpass.getuser), "user"), (_safe(socket.gethostname), "host")):
            if word and len(word) >= 3:
                self.words.append((re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(word) + r"(?![A-Za-z0-9_-])"), repl))
        self.sessions: dict[str, str] = {}
        self.uuids: dict[str, str] = {}
        self.blobs: dict[str, str] = {}

    def register_session(self, sid: str) -> str:
        if sid not in self.sessions:
            self.sessions[sid] = f"SESSION_{len(self.sessions) + 1}"
        return self.sessions[sid]

    def text(self, s: str) -> str:
        if self.email:  # before the generic secrets: the email is a login-file value too
            s = s.replace(self.email, "user@example.invalid")
        for sec in self.secrets:
            s = s.replace(sec, "REDACTED")
        for needle, repl in self.paths:
            s = s.replace(needle, repl)
        for rx, repl in self.words:
            s = rx.sub(repl, s)
        for sid, name in self.sessions.items():
            s = s.replace(sid, name)

        def uuid_sub(m: re.Match) -> str:
            u = m.group(0).lower()
            if u not in self.uuids:
                self.uuids[u] = f"UUID_{len(self.uuids) + 1}"
            return self.uuids[u]

        s = _UUID.sub(uuid_sub, s)
        s = _EMAIL.sub(lambda m: m.group(0) if m.group(0).endswith("@example.invalid") else "user@example.invalid", s)
        s = _JWT.sub("REDACTED", s)
        s = _PREFIXED.sub("REDACTED", s)
        s = _BLOB.sub(self._blob, s)
        return s

    def _blob(self, m: re.Match) -> str:
        tok = m.group(0)
        if not _looks_secret_blob(tok):
            return tok
        # consistent per token, so ids that appear in a request and its updates still correlate
        if tok not in self.blobs:
            self.blobs[tok] = f"REDACTED_{len(self.blobs) + 1}"
        return self.blobs[tok]

    def obj(self, x: Any) -> Any:
        if isinstance(x, str):
            return self.text(x)
        if isinstance(x, list):
            return [self.obj(i) for i in x]
        if isinstance(x, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in x.items()}
        return x


def verify_clean(path: Path, *, home: str, secrets: list[str], email: Optional[str]) -> list[str]:
    """Return a list of problems found in a written fixture (empty = clean)."""
    raw = path.read_text(encoding="utf-8")
    problems = []
    if home and home != "/" and home in raw:
        problems.append("real HOME path present")
    for sec in secrets:
        if isinstance(sec, str) and len(sec) >= 8 and sec in raw:
            problems.append("a login-file value is present")
            break
    if email and email in raw:
        problems.append("real email present")
    for word in (_safe(getpass.getuser), _safe(socket.gethostname)):
        if word and len(word) >= 3 and re.search(r"(?<![A-Za-z0-9_-])" + re.escape(word) + r"(?![A-Za-z0-9_-])", raw):
            problems.append("local user/host name present")
            break
    for m in _EMAIL.finditer(raw):
        if not m.group(0).endswith("@example.invalid"):
            problems.append(f"foreign email-like string {m.group(0)[:3]}***")
            break
    if _JWT.search(raw):
        problems.append("JWT-like token present")
    if _PREFIXED.search(raw):
        problems.append("token-prefix string present")
    return problems


# --------------------------------------------------------------------------- environment

def find_grok_bin(arg: Optional[str]) -> str:
    cand = arg or os.environ.get("GROK_BIN") or shutil.which("grok") or os.path.expanduser("~/.grok/bin/grok")
    if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
        sys.exit(f"grok binary not found/executable: {cand} (use --grok-bin or $GROK_BIN)")
    return cand


def build_env(grok_bin: str, grok_home: str, *, sandbox: Optional[str] = PROFILE, extra: Optional[dict] = None) -> dict:
    """The child env: allowlisted parent vars + D3 switches. XAI_API_KEY is never forwarded."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    for k, v in os.environ.items():
        if k.startswith("LC_"):
            env[k] = v
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TERM", "dumb")
    env.setdefault("TMPDIR", tempfile.gettempdir())
    env["PATH"] = os.pathsep.join([os.path.dirname(os.path.realpath(grok_bin)), os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")])
    env["GROK_HOME"] = grok_home
    env.update(HERMETIC)
    if sandbox:
        env["GROK_SANDBOX"] = sandbox
    env.pop("XAI_API_KEY", None)
    env.update(extra or {})
    return env


def write_sandbox_profile(grok_home: str, deny_extra: list[str]) -> list[str]:
    """Write $GROK_HOME/sandbox.toml; returns the deny paths used (relative to HOME where possible)."""
    home = os.path.expanduser("~")
    entries = [os.path.join(home, d) for d in DEFAULT_DENY]
    entries += [os.path.expanduser(d) for d in deny_extra]
    deny = [p for p in dict.fromkeys(entries) if os.path.lexists(p)]  # lexists: see spec §10-C7
    body = f'[profiles.{PROFILE}]\nextends = "workspace"\ndeny = [\n' + "".join(f'  "{p}",\n' for p in deny) + "]\n"
    Path(grok_home, "sandbox.toml").write_text(body)
    return [p.replace(home, "~") for p in deny]


def rmtree_force(path: str) -> None:
    """rm -rf that survives Grok's mode-000 `sandbox-blocked-dir.<pid>` litter."""
    def onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IRWXU)
            os.chmod(os.path.dirname(p), stat.S_IRWXU)
            func(p)
        except OSError:
            pass
    if not os.path.lexists(path):
        return
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(path, onerror=onerr)


def load_auth_values(auth_path: str) -> tuple[list[str], Optional[str], Optional[bool]]:
    """-> (every string value in the login entry, account email, retention opt-out flag). Values are only used to scrub."""
    d = json.loads(Path(auth_path).read_text())
    entry = next((v for k, v in d.items() if str(k).startswith("https://auth.x.ai::")), None) or next(iter(d.values()))
    # only the identifying / secret fields; public constants (issuer URL, "oidc", "User") stay readable
    secret_keys = ("key", "refresh_token", "user_id", "principal_id", "team_id", "oidc_client_id", "first_name", "last_name")
    vals = [entry[k] for k in secret_keys if isinstance(entry.get(k), str)]
    return vals, entry.get("email"), entry.get("coding_data_retention_opt_out")


# --------------------------------------------------------------------------- ACP client

class AcpError(RuntimeError):
    pass


class Acp:
    """Minimal synchronous ACP client that logs every line (scrubbed) to a fixture file."""

    def __init__(self, *, grok_bin: str, env: dict, cwd: str, scrub: Scrubber, out_path: Path,
                 sandbox_env_names: Optional[list] = None):
        self.cwd = cwd
        self.scrub = scrub
        self.out_path = out_path
        self.t0 = time.monotonic()
        self.argv = [grok_bin, "agent", "--no-leader", "stdio"]
        self.proc = subprocess.Popen(self.argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        self.q: "queue.Queue[Optional[tuple]]" = queue.Queue()  # (arrival monotonic, raw line) | None
        self.stderr_tail: collections.deque = collections.deque(maxlen=80)
        self.rows: list[dict] = []
        self._id = 0
        self.session_id: Optional[str] = None
        self.sessions_seen: list[str] = []
        self.requests_from_agent: list[dict] = []
        self.updates: list[dict] = []          # raw (unscrubbed) session/update `update` objects, for scenario logic
        self.on_update: Optional[Callable[[dict], None]] = None
        self.permission_policy: Callable[[dict], Optional[dict]] = lambda params: None  # None = leave unanswered
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # -- plumbing
    def _read_out(self):
        for line in self.proc.stdout:
            self.q.put((time.monotonic(), line))  # stamp on arrival, not when the pump gets to it
        self.q.put(None)

    def _read_err(self):
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    def _log(self, direction: str, msg: Any, at: Optional[float] = None):
        self.rows.append({"dir": direction, "msg": msg, "t": round((at or time.monotonic()) - self.t0, 4)})

    def mark(self, **info):
        """A non-wire row (`dir: marker`) explaining what the client did, e.g. a timeout; fake servers skip it."""
        self._log("marker", info)

    def register_session(self, sid: str):
        self.session_id = sid
        if sid not in self.sessions_seen:
            self.sessions_seen.append(sid)
        self.scrub.register_session(sid)

    def _write(self, msg: dict):
        self._log("c2a", msg)
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def send_request(self, method: str, params: dict) -> int:
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        return self._id

    def pump(self, want_id: int, timeout: float) -> dict:
        """Read lines until the response to `want_id` arrives; dispatch everything else. -> the raw response."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                self.mark(client_timeout_s=timeout, waiting_for_id=want_id)
                return {"_timeout": True}
            try:
                line = self.q.get(timeout=min(left, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None and self.q.empty():
                    self.mark(agent_exited=self.proc.returncode)
                    return {"_exit": self.proc.returncode}
                continue
            if line is None:
                self.mark(agent_exited=self.proc.poll())
                return {"_exit": self.proc.poll()}
            arrived, line = line
            try:
                m = json.loads(line)
            except ValueError:
                self._log("a2c", {"_nonjson": line.decode("utf-8", "replace")[:500]}, arrived)
                continue
            # learn session ids BEFORE logging so they are scrubbed from the very first line
            res = m.get("result")
            if isinstance(res, dict) and isinstance(res.get("sessionId"), str):
                self.register_session(res["sessionId"])
            params = m.get("params")
            if isinstance(params, dict) and isinstance(params.get("sessionId"), str):
                self.scrub.register_session(params["sessionId"])
            self._log("a2c", m, arrived)
            if "method" in m and "id" in m:
                self._answer_agent_request(m)
            elif m.get("method") == "session/update":
                upd = (m.get("params") or {}).get("update") or {}
                self.updates.append(upd)
                if self.on_update:
                    self.on_update(upd)
            elif m.get("id") == want_id and ("result" in m or "error" in m):
                return m

    def _answer_agent_request(self, m: dict):
        self.requests_from_agent.append(m)
        if m["method"] == "session/request_permission":
            reply = self.permission_policy(m.get("params") or {})
            if reply is None:
                return  # leave unanswered (recorded as such)
            self._write({"jsonrpc": "2.0", "id": m["id"], "result": reply})
        else:
            self._write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "method not supported by client"}})

    def call(self, method: str, params: dict, timeout: float = HANDSHAKE_TIMEOUT) -> dict:
        return self.pump(self.send_request(method, params), timeout)

    # -- protocol helpers
    def handshake(self) -> dict:
        r = self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        if "result" not in r:
            raise AcpError(f"initialize failed: {r}")
        a = self.call("authenticate", {"methodId": "cached_token"})
        if "result" not in a:
            raise AcpError(f"authenticate failed (sign-in expired or hang): {a}")
        return r

    def new_session(self, *, yolo: bool = True, rules: Optional[str] = None, extra_meta: Optional[dict] = None) -> dict:
        meta: dict = {"yoloMode": bool(yolo)}
        if rules:
            meta["rules"] = rules
        meta.update(extra_meta or {})
        r = self.call("session/new", {"cwd": self.cwd, "mcpServers": [], "_meta": meta})
        if "result" not in r:
            raise AcpError(f"session/new failed: {r}")
        return r

    def resume_session(self, sid: str) -> dict:
        self.scrub.register_session(sid)
        r = self.call("session/resume", {"sessionId": sid, "cwd": self.cwd, "mcpServers": []})
        if "result" in r:
            self.session_id = sid
        return r

    def prompt(self, text: str, *, timeout: float = 180.0, sid: Optional[str] = None) -> dict:
        return self.call("session/prompt", {"sessionId": sid or self.session_id,
                                            "prompt": [{"type": "text", "text": text}]}, timeout)

    def agent_text(self, since: int = 0) -> str:
        return "".join(((u.get("content") or {}).get("text") or "") for u in self.updates[since:]
                       if u.get("sessionUpdate") == "agent_message_chunk")

    # -- teardown
    def close(self):
        """killpg the group (spec §M9: SIGTERM to the leader alone leaves the agent alive)."""
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.05)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def write_fixture(self, *, home: str, secrets: list[str], email: Optional[str]) -> list[str]:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_path, "w", encoding="utf-8") as f:
            for row in self.rows:
                f.write(json.dumps(self.scrub.obj(row), ensure_ascii=False, separators=(",", ":")) + "\n")
        problems = verify_clean(self.out_path, home=home, secrets=secrets, email=email)
        if problems:
            self.out_path.unlink()
        return problems


# --------------------------------------------------------------------------- scratch project + scenarios

class Ctx:
    """What a scenario gets: how to open a recorded process against the scratch project."""

    def __init__(self, args, grok_bin, grok_home, project, secrets, email, out_dir, results):
        self.args, self.grok_bin, self.grok_home, self.project = args, grok_bin, grok_home, project
        self.secrets, self.email, self.out_dir, self.results = secrets, email, out_dir, results

    def open(self, fixture: str, *, extra_env: Optional[dict] = None) -> Acp:
        scrub = Scrubber(cwd=self.project, grok_home=self.grok_home, home=os.path.expanduser("~"),
                         secrets=self.secrets, email=self.email)
        env = build_env(self.grok_bin, self.grok_home, extra=extra_env)
        return Acp(grok_bin=self.grok_bin, env=env, cwd=self.project, scrub=scrub, out_path=self.out_dir / f"{fixture}.jsonl")

    def finish(self, acp: Acp, fixture: str, note: str = "") -> None:
        acp.close()
        problems = acp.write_fixture(home=os.path.expanduser("~"), secrets=self.secrets, email=self.email)
        stderr = " | ".join(list(acp.stderr_tail)[-4:])
        self.results.append({"fixture": fixture, "lines": len(acp.rows), "problems": problems, "note": note,
                             "stderr_tail": acp.scrub.text(stderr)[:300]})
        status = "REFUSED " + ",".join(problems) if problems else f"{len(acp.rows)} lines"
        print(f"[{fixture}] {status} {note}".rstrip(), flush=True)


def seed_project(project: str) -> None:
    rmtree_force(project)
    os.makedirs(os.path.join(project, "src"))
    Path(project, "notes.txt").write_text("alpha\nbeta\ngamma\n")
    Path(project, "src", "hello.py").write_text('def hello():\n    return "hello"\n')
    Path(project, "README.md").write_text("# Scratch project\n\nA throwaway directory for protocol recordings.\n")


ONLY_HERE = "Work ONLY inside the current directory. Do not read or write anything outside it. "


def allow_policy(params: dict) -> dict:
    opts = params.get("options") or []
    pick = next((o for o in opts if str(o.get("kind", "")).startswith("allow")), opts[0] if opts else {})
    return {"outcome": {"outcome": "selected", "optionId": pick.get("optionId")}}


def sc_simple_text(c: Ctx):
    a = c.open("simple_text"); a.handshake(); a.new_session()
    a.prompt("Reply with exactly the single word PONG and nothing else. Use no tools.")
    c.finish(a, "simple_text")


def sc_tool_read(c: Ctx):
    a = c.open("tool_read"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "List the files in the current directory, then read notes.txt and tell me its second line. Be brief.")
    c.finish(a, "tool_read")


def sc_tool_write_yolo(c: Ctx):
    a = c.open("tool_write_yolo"); a.handshake(); a.new_session(yolo=True)
    a.prompt(ONLY_HERE + "Create a file named out.txt containing exactly the text hello-yolo (no trailing newline needed). Then reply DONE.")
    ok = Path(c.project, "out.txt").exists()
    c.finish(a, "tool_write_yolo", f"file created: {ok}")


def reject_policy(params: dict) -> dict:
    opts = params.get("options") or []
    pick = next((o for o in opts if str(o.get("kind", "")).startswith("reject")), opts[-1] if opts else {})
    return {"outcome": {"outcome": "selected", "optionId": pick.get("optionId")}}


def _perm_variant(c: Ctx, fixture: str, filename: str, *, policy, timeout: float, cancel_after: Optional[float] = None):
    """One no-yolo write turn; `policy` answers (or not) the session/request_permission."""
    a = c.open(fixture); a.handshake(); a.new_session(yolo=False)
    a.permission_policy = policy
    if cancel_after is not None:
        orig = a._answer_agent_request  # fire the cancel from a timer thread once the permission request is pending

        def answer(m):
            orig(m)
            if m["method"] == "session/request_permission":
                threading.Timer(cancel_after, lambda: a.notify("session/cancel", {"sessionId": a.session_id})).start()
        a._answer_agent_request = answer
    t = time.monotonic()
    r = a.prompt(ONLY_HERE + f"Create a file named {filename} containing the text hello-perm. Then reply DONE.", timeout=timeout)
    ok = Path(c.project, filename).exists()
    stop = (r.get("result") or {}).get("stopReason")
    err = r.get("error")
    c.finish(a, fixture, f"requests={len(a.requests_from_agent)} stopReason={stop} error={json.dumps(err)[:160] if err else None} "
                         f"timeout={'_timeout' in r} turn_s={time.monotonic()-t:.1f} file_created={ok}")


def sc_permission_unanswered(c: Ctx):
    _perm_variant(c, "permission_request_no_yolo_unanswered", "perm.txt", policy=lambda p: None, timeout=75)


def sc_permission_allow(c: Ctx):
    _perm_variant(c, "permission_request_no_yolo_allow", "perm2.txt", policy=allow_policy, timeout=90)


def sc_permission_reject(c: Ctx):
    _perm_variant(c, "permission_request_no_yolo_reject", "perm3.txt", policy=reject_policy, timeout=90)


def sc_permission_error(c: Ctx):
    """JSON-RPC -32601 reply to the permission request (what a client that does not implement it sends)."""
    a = c.open("permission_request_no_yolo_error32601"); a.handshake(); a.new_session(yolo=False)
    orig = a._answer_agent_request

    def answer(m):  # the generic unknown-method branch already replies -32601; make that explicit for this method too
        a.requests_from_agent.append(m)
        a._write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "method not supported by client"}})
    a._answer_agent_request = answer
    t = time.monotonic()
    r = a.prompt(ONLY_HERE + "Create a file named perm4.txt containing the text hello-perm. Then reply DONE.", timeout=90)
    c.finish(a, "permission_request_no_yolo_error32601",
             f"stopReason={(r.get('result') or {}).get('stopReason')} timeout={'_timeout' in r} turn_s={time.monotonic()-t:.1f} "
             f"file_created={Path(c.project, 'perm4.txt').exists()}")


def sc_permission_outcome_cancelled(c: Ctx):
    _perm_variant(c, "permission_request_no_yolo_outcome_cancelled", "perm5.txt",
                  policy=lambda p: {"outcome": {"outcome": "cancelled"}}, timeout=90)


def sc_permission_cancel_pending(c: Ctx):
    """Request left unanswered, then session/cancel: does the pending turn end, and how?"""
    _perm_variant(c, "permission_request_no_yolo_cancel_pending", "perm6.txt", policy=lambda p: None, timeout=60, cancel_after=6.0)


def sc_permission_bash(c: Ctx):
    a = c.open("permission_bash_no_yolo"); a.handshake(); a.new_session(yolo=False)
    a.permission_policy = allow_policy
    a.prompt(ONLY_HERE + "Run these shell commands one at a time: `ls`, then `touch touched.txt`, then `rm touched.txt`, then `git init -q`. Then reply DONE.", timeout=120)
    c.finish(a, "permission_bash_no_yolo",
             f"requests={len(a.requests_from_agent)} titles={[ (r['params'].get('toolCall') or {}).get('title') for r in a.requests_from_agent]} "
             f"git_init_done={Path(c.project, '.git').exists()}")


def sc_cancel_mid_tool(c: Ctx):
    a = c.open("cancel_mid_tool"); a.handshake(); a.new_session()
    state = {"cancelled": False, "t_cancel": None}

    def fire():
        state["t_cancel"] = time.monotonic()
        a.notify("session/cancel", {"sessionId": a.session_id})

    def on_update(u):
        if state["cancelled"] or u.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            return
        if "sleep" in json.dumps(u) and u.get("status") in ("in_progress", None):
            state["cancelled"] = True
            threading.Timer(1.5, fire).start()  # let the command really start; never sleep in the reader

    a.on_update = on_update
    r = a.prompt(ONLY_HERE + "Run the shell command `sleep 40` and then reply DONE.", timeout=90)
    dt = time.monotonic() - state["t_cancel"] if state["t_cancel"] else None
    # is the sleep still running right after the cancel? (grandchild of the sandboxed agent)
    alive = subprocess.run(["pgrep", "-fc", "sleep 40"], capture_output=True, text=True).stdout.strip()
    # the session must stay usable after a cancel
    a.mark(note="follow-up prompt on the same session after the cancel")
    r2 = a.prompt("Reply with exactly the single word OK. Use no tools.", timeout=90)
    c.finish(a, "cancel_mid_tool", f"cancel_sent={state['cancelled']} stopReason={(r.get('result') or {}).get('stopReason')} "
                                   f"cancel_to_response_s={dt if dt is None else round(dt,3)} sleeps_alive_after_cancel(pgrep -fc incl. self)={alive} "
                                   f"followup_stopReason={(r2.get('result') or {}).get('stopReason')} followup_text={a.agent_text()[-20:]!r}")


def sc_resume_turn(c: Ctx):
    a = c.open("resume_turn"); a.handshake(); a.new_session()
    a.prompt("Remember the secret word MANGO-7. Reply only: STORED.")
    sid = a.session_id
    a.close()
    # a SECOND process resumes the session: this is what the cockpit does every turn (one process per turn)
    b = Acp(grok_bin=c.grok_bin, env=build_env(c.grok_bin, c.grok_home), cwd=c.project, scrub=a.scrub, out_path=a.out_path)
    b.rows = a.rows  # one continuous fixture; a `marker` row flags the second spawn (its `t` restarts at 0)
    b.mark(second_process_spawn=True)
    b.handshake()
    r = b.resume_session(sid)
    b.prompt("What was the secret word I asked you to remember? Reply with just the word.")
    txt = b.agent_text()
    c.finish(b, "resume_turn", f"resume_ok={'result' in r} remembered={'MANGO-7' in txt}")


def sc_session_load(c: Ctx):
    """History replay: session/load re-streams the stored conversation (spec D7 reads the disk instead)."""
    a = c.open("session_load"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "Read notes.txt with a tool and reply with its first line only.")
    sid = a.session_id
    a.close()
    b = Acp(grok_bin=c.grok_bin, env=build_env(c.grok_bin, c.grok_home), cwd=c.project, scrub=a.scrub, out_path=a.out_path)
    b.rows = a.rows
    b.mark(second_process_spawn=True)
    b.handshake()
    r = b.call("session/load", {"sessionId": sid, "cwd": c.project, "mcpServers": []}, 30)
    kinds = collections.Counter(u.get("sessionUpdate") for u in b.updates)
    c.finish(b, "session_load", f"load_ok={'result' in r} replay_kinds={dict(kinds)}")


def sc_subagent_turn(c: Ctx):
    a = c.open("subagent_turn"); a.handshake(); a.new_session()
    a.permission_policy = allow_policy
    a.prompt(ONLY_HERE + "Use your sub-agent (task/explore agent) tool to have a sub-agent read notes.txt and report its first line to you. "
             "You must delegate this to the sub-agent rather than reading the file yourself. Then tell me the line.", timeout=180)
    c.finish(a, "subagent_turn", "kinds=" + json.dumps(dict(collections.Counter(u.get("sessionUpdate") for u in a.updates))))


def sc_multi_message_turn(c: Ctx):
    a = c.open("multi_message_turn"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "Do these in order. First write one sentence announcing you will check the notes file. "
             "Then read notes.txt with the read tool. Then write one sentence saying what the first line is. "
             "Then list the directory with a tool. Then write a final one-sentence summary.", timeout=180)
    c.finish(a, "multi_message_turn")


def sc_model_effort_switch(c: Ctx):
    a = c.open("model_effort_switch"); a.handshake(); r = a.new_session()
    opts = (r.get("result") or {}).get("configOptions") or []
    models = next((o for o in opts if o.get("id") == "model"), {})
    efforts = next((o for o in opts if o.get("id") in ("reasoning_effort", "effort")), {})
    sid = a.session_id
    eff_id = efforts.get("id", "reasoning_effort")

    def first_other(o):
        cur = o.get("currentValue")
        return next((x.get("value") for x in (o.get("options") or []) if x.get("value") != cur), None)

    other_model, other_effort = first_other(models), first_other(efforts)
    if other_model:
        a.call("session/set_config_option", {"sessionId": sid, "configId": "model", "value": other_model})
    a.call("session/set_config_option", {"sessionId": sid, "configId": eff_id, "value": "low"})
    a.call("session/set_config_option", {"sessionId": sid, "configId": "model", "value": "grok-does-not-exist"})
    a.call("session/set_config_option", {"sessionId": sid, "configId": eff_id, "value": "ultra"})
    a.call("session/set_config_option", {"sessionId": sid, "configId": "no_such_option", "value": "x"})
    a.call("session/set_config_option", {"sessionId": "00000000-0000-0000-0000-000000000000", "configId": "model", "value": other_model or "x"})
    # the turn runs under the switched model + effort: the response must name them
    a.prompt("Reply with exactly the single word OK. Use no tools.", timeout=120)
    c.finish(a, "model_effort_switch", f"model_options={[x.get('value') for x in models.get('options', [])]} "
                                       f"effort_options={[x.get('value') for x in efforts.get('options', [])]} ran_on={other_model}/low")


def sc_session_list(c: Ctx):
    a = c.open("session_list"); a.handshake(); a.new_session()
    a.prompt("Reply with exactly the single word OK. Use no tools.", timeout=120)
    a.call("session/list", {"cwd": c.project})
    a.call("session/list", {})
    a.call("session/list", {"cwd": os.path.join(c.project, "does-not-exist")})
    a.call("session/close", {"sessionId": a.session_id})
    c.finish(a, "session_list")


def sc_error_shapes(c: Ctx):
    a = c.open("error_shapes"); a.handshake()
    ghost = "11111111-2222-3333-4444-555555555555"
    a.call("session/prompt", {"sessionId": ghost, "prompt": [{"type": "text", "text": "hi"}]})
    a.call("session/resume", {"sessionId": ghost, "cwd": c.project, "mcpServers": []})
    a.call("session/load", {"sessionId": ghost, "cwd": c.project, "mcpServers": []})
    a.call("session/new", {"cwd": os.path.join(c.project, "no-such-dir"), "mcpServers": []})
    a.call("session/new", {"mcpServers": []})  # missing cwd
    a.call("no/such_method", {})
    a.notify("session/cancel", {"sessionId": ghost})
    a.call("session/close", {"sessionId": ghost})
    a.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})  # second initialize
    c.finish(a, "error_shapes")


def sc_rules_meta(c: Ctx):
    a = c.open("rules_meta"); a.handshake()
    a.new_session(rules="Always answer in UPPER CASE letters only, whatever you are asked.")
    a.prompt("Say hello in one short sentence. Use no tools.", timeout=90)
    c.finish(a, "rules_meta", f"text={a.agent_text()[:80]!r}")


def sc_tool_grep_edit(c: Ctx):
    a = c.open("tool_grep_edit"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "Use the grep tool to search for the word hello in src/. Then use the edit (search and replace) tool to change "
             'the string "hello" in src/hello.py to "hi" (both the quotes content only). Then reply DONE.', timeout=150)
    c.finish(a, "tool_grep_edit", f"hello.py={Path(c.project, 'src', 'hello.py').read_text()!r}")


def sc_tool_error(c: Ctx):
    a = c.open("tool_error"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "Read the file does-not-exist.txt with the read tool, then run the shell command `ls no-such-dir` "
             "and `exit 3`, then tell me in one sentence what happened.", timeout=150)
    c.finish(a, "tool_error")


def sc_plan_mode_probe(c: Ctx):
    """H5: does a turn that calls enter_plan_mode / scheduler_* hang or ask the client under the hermetic env?"""
    a = c.open("plan_mode_probe"); a.handshake(); a.new_session(yolo=True)
    a.permission_policy = lambda p: None
    r = a.prompt(ONLY_HERE + "Call your enter_plan_mode tool right now (do not ask me anything first), then reply READY.", timeout=60)
    c.finish(a, "plan_mode_probe", f"requests={[x['method'] for x in a.requests_from_agent]} stopReason={(r.get('result') or {}).get('stopReason')} timeout={'_timeout' in r}")


def sc_todo_write(c: Ctx):
    a = c.open("todo_write"); a.handshake(); a.new_session()
    a.prompt(ONLY_HERE + "Use your todo-list tool to record a plan with exactly three items: read notes.txt, summarise it, report. "
             "Mark the first one completed. Then reply DONE. Do not do the tasks themselves.", timeout=120)
    c.finish(a, "todo_write")


def sc_web_tools(c: Ctx):
    a = c.open("web_tools"); a.handshake(); a.new_session()
    a.prompt("Fetch the page https://example.com with your web fetch tool and tell me its title in one short sentence. "
             "Then use your web search tool to search for 'example domain iana' and name one result in one sentence.", timeout=180)
    c.finish(a, "web_tools")


def sc_auth_missing(c: Ctx):
    """spec §10-C2: an empty GROK_HOME makes `authenticate` hang instead of erroring."""
    empty = os.path.join(os.path.dirname(c.grok_home), "empty-home")
    os.makedirs(empty, mode=0o700, exist_ok=True)
    shutil.copyfile(os.path.join(c.grok_home, "sandbox.toml"), os.path.join(empty, "sandbox.toml"))
    scrub = Scrubber(cwd=c.project, grok_home=empty, home=os.path.expanduser("~"), secrets=c.secrets, email=c.email)
    a = Acp(grok_bin=c.grok_bin, env=build_env(c.grok_bin, empty), cwd=c.project, scrub=scrub, out_path=c.out_dir / "auth_missing.jsonl")
    r = a.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    t = time.monotonic()
    r2 = a.call("authenticate", {"methodId": "cached_token"}, timeout=20)
    note = f"authenticate_after_s={time.monotonic()-t:.1f} resp={'timeout' if '_timeout' in r2 else json.dumps(r2)[:200]}"
    c.finish(a, "auth_missing", note)
    rmtree_force(empty)


SCENARIOS: dict[str, Callable[[Ctx], None]] = {
    "simple_text": sc_simple_text,
    "tool_read": sc_tool_read,
    "tool_write_yolo": sc_tool_write_yolo,
    "tool_grep_edit": sc_tool_grep_edit,
    "tool_error": sc_tool_error,
    "permission_request_no_yolo_unanswered": sc_permission_unanswered,
    "permission_request_no_yolo_allow": sc_permission_allow,
    "permission_request_no_yolo_reject": sc_permission_reject,
    "permission_request_no_yolo_error32601": sc_permission_error,
    "permission_request_no_yolo_outcome_cancelled": sc_permission_outcome_cancelled,
    "permission_request_no_yolo_cancel_pending": sc_permission_cancel_pending,
    "permission_bash_no_yolo": sc_permission_bash,
    "cancel_mid_tool": sc_cancel_mid_tool,
    "resume_turn": sc_resume_turn,
    "session_load": sc_session_load,
    "subagent_turn": sc_subagent_turn,
    "multi_message_turn": sc_multi_message_turn,
    "model_effort_switch": sc_model_effort_switch,
    "session_list": sc_session_list,
    "error_shapes": sc_error_shapes,
    "rules_meta": sc_rules_meta,
    "plan_mode_probe": sc_plan_mode_probe,
    "todo_write": sc_todo_write,
    "web_tools": sc_web_tools,
    "auth_missing": sc_auth_missing,
}


# --------------------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenario", action="append", default=[], help="scenario name or 'all' (repeatable)")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--grok-bin", default=None)
    ap.add_argument("--auth-src", default=os.environ.get("GROK_RECORD_AUTH"))
    ap.add_argument("--scratch-dir", default=None, help="parent dir for the throwaway GROK_HOME + project")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--deny", action="append", default=[], help="extra sandbox deny path (repeatable)")
    ap.add_argument("--keep-scratch", action="store_true", help="debug only: leave the scratch tree (it holds a login copy!)")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(SCENARIOS))
        return 0
    names = list(SCENARIOS) if (not args.scenario or "all" in args.scenario) else args.scenario
    for n in names:
        if n not in SCENARIOS:
            sys.exit(f"unknown scenario {n!r}; --list shows the names")

    grok_bin = find_grok_bin(args.grok_bin)
    real_home = os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok")
    auth_src = args.auth_src or os.path.join(real_home, "auth.json")
    if not os.path.isfile(auth_src):
        sys.exit(f"login file not found: {auth_src} (use --auth-src or $GROK_RECORD_AUTH)")
    secrets, email, optout = load_auth_values(auth_src)
    if optout is not True:
        sys.exit("refusing to record: the account's coding_data_retention_opt_out is not true (spec-095 D2)")

    scratch = tempfile.mkdtemp(prefix="grok-record-", dir=args.scratch_dir)
    grok_home = os.path.join(scratch, "home")
    project = os.path.join(scratch, "proj")
    os.makedirs(grok_home, mode=0o700)
    if os.path.realpath(grok_home) == os.path.realpath(real_home):
        sys.exit("scratch GROK_HOME resolves to the real one; refusing")
    results: list[dict] = []
    try:
        shutil.copyfile(auth_src, os.path.join(grok_home, "auth.json"))
        os.chmod(os.path.join(grok_home, "auth.json"), 0o600)
        deny = write_sandbox_profile(grok_home, args.deny)
        print(f"scratch={scratch} sandbox deny entries: {len(deny)}", flush=True)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            seed_project(project)
            ctx = Ctx(args, grok_bin, grok_home, project, secrets, email, out_dir, results)
            try:
                SCENARIOS[name](ctx)
            except Exception as e:  # a failed scenario must not abort the rest
                print(f"[{name}] FAILED: {type(e).__name__}: {str(e)[:300]}", flush=True)
                results.append({"fixture": name, "error": str(e)[:300]})
        # litter measurement (spec §10-C7): what a run left in GROK_HOME
        litter = [n for n in os.listdir(grok_home) if n.startswith("sandbox-blocked")]
        print(f"sandbox-blocked* litter entries in scratch GROK_HOME: {len(litter)}", flush=True)
    finally:
        if args.keep_scratch:
            print(f"KEPT scratch (contains a login copy): {scratch}", flush=True)
        else:
            rmtree_force(scratch)
        left = subprocess.run(["pgrep", "-x", "grok"], capture_output=True, text=True).stdout.split()
        if left:
            print(f"WARNING: grok processes still alive: {left}", flush=True)
    bad = [r for r in results if r.get("problems") or r.get("error")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
