#!/usr/bin/env python3
"""A fake `grok` binary for tests (spec-095 §6.2): speaks the ACP subset the engine uses by
replaying recorded wire fixtures, and misbehaves on request.

`GROK_BIN` points at a tiny wrapper that execs this script, so the engine spawns a REAL
subprocess (real pids, real process groups) with no network and no tokens. The wrapper bakes in the
`FAKE_GROK_*` switches — the engine's hermetic child environment deliberately does not carry them.

Fixture contract (shared with tools/grok_record_fixtures.py): `tests/fixtures/grok/<name>.jsonl`,
one JSON object per line `{"dir": "c2a"|"a2c", "msg": {JSON-RPC}, "t": <float, optional>}`.
  c2a = client -> agent: the fake WAITS for a request/notification with the same `method` (ids and
        params are ignored). A c2a entry without a `method` is a recorded client reply to a server
        request; it is consumed implicitly.
  a2c = agent -> client: replayed in order. Each response `id` is rewritten to the id of the request
        it answers; every `SESSION_<n>` token becomes this process's real session id (the one the
        client resumed, or a fresh one), so the engine's own-session filter sees consistent ids.

Switches (env):
  FAKE_GROK_FIXTURE         fixture name (tests/fixtures/grok/<name>.jsonl) or absolute path
  FAKE_GROK_VERSION         text printed for `--version` (default "grok 1.0.46 (fake) [stable]")
  FAKE_GROK_MODELS          "loggedout" | "empty" | raw text for `models` (default: the 4 real models)
  FAKE_GROK_HANG_ON=<m>     never answer method <m> (a missing login hangs `authenticate` for real)
  FAKE_GROK_EXIT_ON=<m>     exit (stderr text + FAKE_GROK_EXIT_CODE) when method <m> arrives
  FAKE_GROK_GIANT_LINE=<n>  before the prompt response emit ONE line of ~n bytes
  FAKE_GROK_STOP_REASON=<s> override the prompt response's stopReason
  FAKE_GROK_PROMPT_ERROR=<json>  answer session/prompt with this JSON-RPC error object
  FAKE_GROK_IGNORE_CANCEL=1 never act on session/cancel (the engine must killpg)
  FAKE_GROK_IGNORE_CLOSE=1  never answer session/close
  FAKE_GROK_PERMISSION_WAIT seconds to wait for a client reply to a server request (default 3)
  FAKE_GROK_STDERR_FLOOD=<n> write n bytes to stderr at start (proves stderr never blocks)
  FAKE_GROK_STDERR_TEXT     last line written to stderr
  FAKE_GROK_SPAWN_CHILD=1   start a `sleep 300` grandchild in the same process group
  FAKE_GROK_PID_FILE        json {"leader": pid, "child": pid|null} written at start
  FAKE_GROK_LITTER=1        leave sandbox-blocked(-dir).<pid> placeholders in $GROK_HOME like the real one
  FAKE_GROK_AUTH_META       json merged into the authenticate result `_meta`
  FAKE_GROK_ENV_DUMP / FAKE_GROK_ARGV_DUMP / FAKE_GROK_CWD_DUMP  where to record what the engine handed us
  FAKE_GROK_LOG             append every received message (jsonl)
  FAKE_GROK_LEAK            a value written to stderr verbatim (the redaction test)
  FAKE_GROK_REALTIME=1      honour the fixture's `t` deltas
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
ENV = os.environ


def out(obj):
    sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))) + "\n")
    sys.stdout.flush()


def errw(text):
    sys.stderr.write(text)
    sys.stderr.flush()


def dump(path_env, payload):
    path = ENV.get(path_env)
    if path:
        Path(path).write_text(json.dumps(payload))


def run_subcommand(argv):
    if argv == ["--version"]:
        print(ENV.get("FAKE_GROK_VERSION", "grok 1.0.46 (fake) [stable]"))
        return 0
    if argv == ["models"]:
        mode = ENV.get("FAKE_GROK_MODELS", "")
        if mode == "loggedout":
            print("You are not logged in. Run `grok login`.")
        elif mode == "empty":
            print("You are logged in with grok.com.\n\nDefault model: none\n\nAvailable models:")
        elif mode:
            print(mode)
        else:
            print("You are logged in with grok.com.\n\nDefault model: grok-4.7\n\nAvailable models:\n"
                  "  * grok-4.7 (default)\n  - grok-4.7-build-fast\n  - grok-4.6\n  - grok-4.5")
        return 0
    return 2


class Fake:
    def __init__(self):
        name = ENV.get("FAKE_GROK_FIXTURE", "simple_text")
        path = Path(name) if name.startswith("/") else FIXTURES / (name + ".jsonl")
        self.entries = []
        for line in path.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                if e.get("dir") in ("c2a", "a2c"):
                    self.entries.append(e)
        self.inbox = queue.Queue()
        self.idmap = {}
        self.sid_map = {}
        self.nonce = uuid.uuid4().hex[:8]
        self.prompt_id = None
        self.log_path = ENV.get("FAKE_GROK_LOG")
        threading.Thread(target=self._read_stdin, daemon=True).start()

    # --- io ---------------------------------------------------------------------------
    def _read_stdin(self):
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if self.log_path:
                with open(self.log_path, "a") as fh:
                    fh.write(json.dumps(msg) + "\n")
            self.inbox.put(msg)
        self.inbox.put(None)  # EOF

    def recv(self, timeout=None):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return "timeout"

    def rewrite(self, raw):
        def sub(m):
            tok = m.group(0)
            return self.sid_map.setdefault(tok, f"fake-{tok.lower()}-{self.nonce}")
        return re.sub(r"SESSION_\d+", sub, raw)

    def emit(self, entry):
        msg = json.loads(self.rewrite(json.dumps(entry["msg"])))
        if "method" not in msg and ("result" in msg or "error" in msg):
            msg["id"] = self.idmap.get(entry["msg"].get("id"), msg.get("id"))
            stop = ENV.get("FAKE_GROK_STOP_REASON")
            if stop and isinstance(msg.get("result"), dict) and "stopReason" in msg["result"]:
                msg["result"]["stopReason"] = stop
            if ENV.get("FAKE_GROK_PROMPT_ERROR") and msg["id"] == self.prompt_id:
                msg = {"jsonrpc": "2.0", "id": msg["id"], "error": json.loads(ENV["FAKE_GROK_PROMPT_ERROR"])}
            meta_over = ENV.get("FAKE_GROK_AUTH_META")
            if meta_over and isinstance(msg.get("result"), dict) and "email" in (msg["result"].get("_meta") or {}):
                msg["result"]["_meta"].update(json.loads(meta_over))
        out(msg)

    # --- generic replies ----------------------------------------------------------------
    def generic(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method == "session/cancel":
            return  # a notification nobody scripted: ignore
        if method == "session/set_config_option":
            p = msg.get("params") or {}
            if str(p.get("value", "")).startswith("bad-"):
                out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Invalid params",
                                                            "data": "unknown model id"}})
            else:
                out({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": [
                    {"id": p.get("configId"), "currentValue": p.get("value")}]}})
        elif method == "session/close":
            if not ENV.get("FAKE_GROK_IGNORE_CLOSE"):
                out({"jsonrpc": "2.0", "id": mid, "result": {"_meta": {"x.ai/closeOutcome": "closed"}}})
        elif mid is not None:
            out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unsupported {method}"}})

    # --- replay -------------------------------------------------------------------------
    def wait_for_method(self, method):
        """Block until the client sends `method`, answering anything else generically."""
        while True:
            msg = self.recv()
            if msg is None:
                sys.exit(0)  # stdin EOF = graceful shutdown, like the real agent
            if ENV.get("FAKE_GROK_EXIT_ON") and msg.get("method") == ENV["FAKE_GROK_EXIT_ON"]:
                self.die()
            if msg.get("method") == method:
                if method == "session/cancel" and ENV.get("FAKE_GROK_IGNORE_CANCEL"):
                    continue
                return msg
            self.generic(msg)

    def die(self):
        errw(ENV.get("FAKE_GROK_STDERR_TEXT", "fake grok: fatal error") + "\n")
        sys.exit(int(ENV.get("FAKE_GROK_EXIT_CODE", "3")))

    def run(self):
        pos = 0
        n = len(self.entries)
        while pos < n:
            e = self.entries[pos]
            msg = e["msg"]
            if e["dir"] == "a2c":
                if ENV.get("FAKE_GROK_REALTIME") and e.get("t") and pos:
                    prev = self.entries[pos - 1].get("t") or 0
                    time.sleep(max(0.0, min(2.0, e["t"] - prev)))
                if "method" in msg and "id" in msg:  # a server -> client REQUEST (permission)
                    self.emit(e)
                    reply = self.recv(timeout=float(ENV.get("FAKE_GROK_PERMISSION_WAIT", "3")))
                    if isinstance(reply, dict) and reply.get("method") is None:
                        pass  # answered; the recorded client reply entry is skipped below
                    pos += 1
                    continue
                self.emit(e)
                pos += 1
                continue
            # c2a
            if "method" not in msg:
                pos += 1
                continue
            method = msg["method"]
            got = self.wait_for_method(method)
            if "id" in got and "id" in msg:
                self.idmap[msg["id"]] = got["id"]
            if method == "session/prompt":
                self.prompt_id = got.get("id")
                giant = int(ENV.get("FAKE_GROK_GIANT_LINE", "0") or 0)
                if giant:
                    out({"jsonrpc": "2.0", "method": "session/update", "params": {
                        "sessionId": got["params"]["sessionId"], "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "x" * giant}}}})
            if method in ("session/resume", "session/load"):
                self.sid_map["SESSION_1"] = (got.get("params") or {}).get("sessionId") or "fake-resumed"
            if ENV.get("FAKE_GROK_HANG_ON") == method:
                while True:
                    time.sleep(3600)
            pos += 1
        # fixture exhausted: stay alive like the real agent until stdin closes
        while True:
            msg = self.recv()
            if msg is None:
                return
            self.generic(msg)


def main():
    argv = sys.argv[1:]
    dump("FAKE_GROK_ARGV_DUMP", argv)
    if not (argv[:3] == ["agent", "--no-leader", "stdio"]):
        sys.exit(run_subcommand(argv))
    dump("FAKE_GROK_ENV_DUMP", {k: v for k, v in ENV.items() if not k.startswith("FAKE_")})
    dump("FAKE_GROK_CWD_DUMP", os.getcwd())
    child = None
    if ENV.get("FAKE_GROK_SPAWN_CHILD"):
        child = subprocess.Popen(["sleep", "300"])  # same session + process group as us
    dump("FAKE_GROK_PID_FILE", {"leader": os.getpid(), "child": child.pid if child else None})
    if ENV.get("FAKE_GROK_LITTER") and ENV.get("GROK_HOME"):
        home = Path(ENV["GROK_HOME"])
        d = home / f"sandbox-blocked-dir.{os.getpid()}"
        d.mkdir(exist_ok=True)
        os.chmod(d, 0)
        f = home / f"sandbox-blocked.{os.getpid()}"
        f.write_text("")
        os.chmod(f, 0)
    if ENV.get("FAKE_GROK_LEAK"):
        errw(f"debug: {ENV['FAKE_GROK_LEAK']}\n")
    flood = int(ENV.get("FAKE_GROK_STDERR_FLOOD", "0") or 0)
    if flood:
        def pump():
            chunk = "e" * 65535 + "\n"
            sent = 0
            while sent < flood:
                errw(chunk)
                sent += len(chunk)
            if ENV.get("FAKE_GROK_STDERR_TEXT"):
                errw(ENV["FAKE_GROK_STDERR_TEXT"] + "\n")
        threading.Thread(target=pump, daemon=True).start()
    elif ENV.get("FAKE_GROK_STDERR_TEXT") and not ENV.get("FAKE_GROK_EXIT_ON"):
        errw(ENV["FAKE_GROK_STDERR_TEXT"] + "\n")
    Fake().run()


if __name__ == "__main__":
    main()
