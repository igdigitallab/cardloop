"""
Grok-enabled cockpit for the E2E suite (spec-095 P5b).

The REAL `grok_engine` runs inside the booted cockpit — real provider probe, real ACP client,
real process group, real usage ledger. Only the binary it spawns is fake: `GROK_BIN` is a tiny
wrapper that execs `tests/fake_grok_acp.py`, which replays a recorded wire fixture (no network,
no tokens). Nothing in the fail-closed code is stubbed:

  * `GROK_ENABLED=true` + a seeded oidc login (retention opted out) in the cockpit's own GROK_HOME;
  * a stub `bwrap` on PATH (the engine only checks it exists — the sandbox itself is the real
    CLI's job, and the fake CLI has none);
  * the sandbox-denial verdict, which on a real host costs one model turn, is written through the
    engine's OWN cache writer (`_ensure_sandbox_probe`) with the verdict function replaced by a
    constant, so the on-disk fingerprint is exactly what the production code computes.

The wrapper picks the recorded fixture from the NAME of the project directory it is spawned in
(the engine spawns the CLI with cwd = the project), so one cockpit can serve a plain text turn, a
tool turn, a failing turn, ... side by side.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_ACP = REPO_ROOT / "tests" / "fake_grok_acp.py"

# project id -> (recorded fixture, extra FAKE_GROK_* switches). Anything not listed: synthetic_text.
FIXTURE_BY_PROJECT = {
    "g-tool": ("synthetic_tool", {}),
    "g-fail": ("synthetic_text", {"PROMPT_ERROR": json.dumps({"code": -32603, "message": "e2e scripted grok failure"})}),
}

OIDC_EMAIL = "user@example.invalid"   # the email recorded in the wire fixtures' authenticate reply


def write_login(home: Path) -> None:
    """A syntactically real login: oidc, coding-data retention opted out (what `_auth_problem` checks)."""
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    body = {"auth_mode": "oidc", "coding_data_retention_opt_out": True, "email": OIDC_EMAIL,
            "key": "e2e-not-a-token", "refresh_token": "e2e-not-a-token", "expires_at": "2099-01-01T00:00:00Z"}
    path = home / "auth.json"
    path.write_text(json.dumps({"https://auth.x.ai::e2e-client": body}))
    os.chmod(path, 0o600)


def write_fake_cli(bindir: Path) -> Path:
    """`bindir/grok` (the GROK_BIN) and `bindir/bwrap` (a no-op stub). Returns the grok path."""
    bindir.mkdir(parents=True, exist_ok=True)
    bwrap = bindir / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n")
    bwrap.chmod(bwrap.stat().st_mode | stat.S_IXUSR)
    wrapper = bindir / "grok"
    # A python wrapper, not sh (matches tests/test_grok_engine.py): the child env stays exactly
    # what the engine handed over plus the FAKE_GROK_* switches baked in here.
    body = (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"TABLE = {FIXTURE_BY_PROJECT!r}\n"
        "name = os.path.basename(os.getcwd())\n"
        "fixture, extra = TABLE.get(name, ('synthetic_text', {}))\n"
        "os.environ['FAKE_GROK_FIXTURE'] = fixture\n"
        "for k, v in extra.items():\n"
        "    os.environ['FAKE_GROK_' + k] = v\n"
        # The fake issues "fake-session_1-<nonce>" ids; the history reader (like the real CLI's session
        # store) only accepts UUIDs, so this wrapper — NOT the shared fake — hands out UUID ids.
        "import importlib.util, re, uuid\n"
        f"spec = importlib.util.spec_from_file_location('fake_grok_acp', {str(FAKE_ACP)!r})\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "def rewrite(self, raw):\n"
        "    return re.sub(r'SESSION_\\d+', lambda m: self.sid_map.setdefault(m.group(0), str(uuid.uuid4())), raw)\n"
        "mod.Fake.rewrite = rewrite\n"
        "mod.main()\n"
    )
    wrapper.write_text(body)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper


def grok_env(app_dir: Path, bindir: Path, wrapper: Path, denied: Path) -> dict:
    """Extra environment for the Grok-enabled cockpit (merged over the base e2e env)."""
    denied.mkdir(parents=True, exist_ok=True)
    return {
        "GROK_ENABLED": "true",
        "GROK_BIN": str(wrapper),
        "GROK_SANDBOX_DENY": str(denied),
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
    }


_SEED_PROBE = r"""
import asyncio, json, sys
import grok_engine as g

async def ok(info):
    return "ok", "e2e: verdict written through the production cache writer"

g._probe_sandbox_denial = ok
info = asyncio.run(g.provider_info(force=True))
print(json.dumps({"available": info.get("available"), "error": info.get("error"),
                  "models": [m["value"] for m in info.get("models", [])]}))
"""


def seed_availability(app_dir: Path, env: dict) -> dict:
    """Runs the real provider probe once in a subprocess with the SERVER's env, so the sandbox
    verdict lands in `data/grok_sandbox_probe.json` under the fingerprint the server will compute.
    Raises if the fake CLI did not come out `available` (nothing downstream would mean anything)."""
    proc = subprocess.run([sys.executable, "-c", _SEED_PROBE], cwd=str(app_dir), env=env,
                          capture_output=True, text=True, timeout=60)
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        info = json.loads(last)
    except ValueError:
        raise RuntimeError(f"grok availability seed failed: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}") from None
    if not info.get("available"):
        raise RuntimeError(f"fake Grok CLI is not available to the engine: {info.get('error')}")
    return info


def seed_usage_rows(data_dir: Path, rows: list[dict]) -> None:
    """Pre-seeded `data/grok_usage.jsonl` rows (the same file the engine appends to)."""
    with (data_dir / "grok_usage.jsonl").open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def usage_row(*, session_id: str, project: str, model: str = "grok-4.7", input: int = 1000,
              output: int = 200, cached: int = 400, reasoning: int = 50, notional_usd: float | None = 0.0123,
              age_sec: float = 60.0) -> dict:
    return {"ts": time.time() - age_sec, "provider": "grok", "session_id": session_id, "project": project,
            "session_key": f"{project}:1", "entrypoint": "chat", "model": model, "input": input,
            "output": output, "cached": cached, "reasoning": reasoning, "total": input + output,
            "duration_ms": 1500, "notional_usd": notional_usd}


def webapp_serves(*names: str) -> bool:
    """Has the cockpit's API layer been wired to these Grok readers yet (spec-095 P3/P4 wiring)?
    The tests that need them skip with a marker until it has; they lift on their own after."""
    text = (REPO_ROOT / "webapp.py").read_text(encoding="utf-8")
    return all(n in text for n in names)


def wait_until_available(server_log: Path, timeout: float = 40.0) -> None:
    """The startup probe is a BACKGROUND task (bot.py); the registry row only turns `available` once
    it has journaled its verdict. Wait for that line so no test races the first registry read."""
    end = time.time() + timeout
    text = ""
    while time.time() < end:
        text = server_log.read_text(errors="replace") if server_log.exists() else ""
        if "[grok] ready via" in text:
            return
        for line in text.splitlines():
            if line.startswith("[grok] unavailable"):
                raise RuntimeError(f"the Grok-enabled e2e cockpit came up with Grok unavailable: {line}")
        time.sleep(0.25)
    raise RuntimeError("the Grok startup probe never reported a verdict:\n" + text[-1500:])
