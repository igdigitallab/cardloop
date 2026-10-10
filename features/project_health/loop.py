"""features/project_health/loop.py — run the checks: on demand, cached, and once a day.

Import rule: feature -> core is safe (spec-068 IRON RULE); core never imports this.

Every filesystem/git read runs in the default executor, never on the event loop.  The sweep is
read-only: it writes ONE digest to data/inbox/ and pushes at most once per calendar day, and
only for findings it has not announced before (a standing finding must not nag every morning).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from webapp import (
    _collect_projects,
    _detect_test_cmd,
    _global_claude_md_path,
    _project_memory_dir,
    _sdk_sessions_dir,
)

from features.project_health import logic as _logic

CACHE_TTL_SEC = 300                  # on-demand results are reused this long (?fresh=1 bypasses)
STATE_FILE = "project_health_state.json"

_cache: dict = {}                    # project id -> (expires_at, result)
_last_sweep: dict = {"at": None, "results": {}}


def build_env(ctx: dict) -> "_logic.Env":
    """The injectable environment, wired to the cockpit's own path/test-detection helpers."""
    return _logic.Env(
        native_memory_dir=lambda cwd: _sdk_sessions_dir(cwd) / "memory",
        curated_memory_dir=_project_memory_dir,
        global_claude_md=_global_claude_md_path(),
        detect_test_cmd=_detect_test_cmd,
        acks=_logic.load_acks(ctx["DATA"]),
    )


def _run_sync(ctx: dict, record: dict) -> dict:
    return _logic.run_checks(_logic.Project.from_record(record), build_env(ctx))


async def check_project(ctx: dict, record: dict, fresh: bool = False) -> dict:
    """Findings for one project.  Cached for CACHE_TTL_SEC unless *fresh*."""
    pid = record["id"]
    if not fresh:
        hit = _cache.get(pid)
        if hit and hit[0] > time.time():
            return hit[1]
    result = await asyncio.get_running_loop().run_in_executor(None, _run_sync, ctx, record)
    _cache[pid] = (time.time() + CACHE_TTL_SEC, result)
    return result


def cached_result(pid: str) -> "dict | None":
    """Last known result without running anything: the cache (even if stale), else the sweep."""
    hit = _cache.get(pid)
    if hit:
        return hit[1]
    return _last_sweep["results"].get(pid)


def forget(pid: str) -> None:
    _cache.pop(pid, None)


def empty_result(record: dict) -> dict:
    return {"project_id": record["id"], "name": record.get("name") or record["id"], "findings": [],
            "checked_at": None, "took_ms": 0, "errors": [], "skipped": []}


def fleet_snapshot() -> dict:
    """What the last sweep found (only projects with findings)."""
    sick = [r for r in _last_sweep["results"].values() if r.get("findings")]
    sick.sort(key=lambda r: str(r.get("name", "")).lower())
    flat = [f for r in sick for f in r["findings"]]
    return {
        "mode": _logic.MODE, "interval_sec": _logic.INTERVAL_SEC,
        "last_sweep_at": _last_sweep["at"], "projects": sick,
        "counts": {"projects": len(sick), "crit": sum(1 for f in flat if f["severity"] == "crit"),
                   "warn": sum(1 for f in flat if f["severity"] == "warn")},
    }


async def sweep_once(ctx: dict) -> dict:
    """Check every registered project, store the results, write the digest.  Never raises."""
    results = []
    for record in _collect_projects(ctx):
        if record.get("is_free") or not record.get("cwd"):
            continue   # a free chat's cwd is $HOME: not a project
        try:
            results.append(await check_project(ctx, record, fresh=True))
        except Exception as exc:  # noqa: BLE001 - one bad project must not stop the sweep
            print(f"[project-health] {record.get('name')}: {exc}")
    _last_sweep["at"] = int(time.time())
    _last_sweep["results"] = {r["project_id"]: r for r in results}
    await _write_digest(ctx, results)
    return fleet_snapshot()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state(data: Path) -> dict:
    try:
        raw = json.loads((data / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


async def _write_digest(ctx: dict, results: list) -> None:
    """Digest to data/inbox/ (rewritten each sweep); push at most once a day, only for NEW findings."""
    try:
        data: Path = ctx["DATA"]
        sick = [r for r in results if r.get("findings")]
        day = _today()
        if sick:
            inbox = data / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"project-health-{day}.md").write_text(_logic.build_digest(results), encoding="utf-8")

        state = _load_state(data)
        current = {_logic.finding_key(r["project_id"], f) for r in results for f in r["findings"]}
        # Keys that went away are forgotten, so a finding that comes back is announced again.
        notified = set(state.get("notified") or []) & current
        new = current - notified
        last_push_day = state.get("last_push_day") or ""
        if new and last_push_day != day:
            notified |= new
            last_push_day = day
            crit = sum(1 for r in sick for f in r["findings"] if f["severity"] == "crit")
            try:
                from webapp import _push_broadcast
                await _push_broadcast(json.dumps({
                    "title": "Project health",
                    "body": f"{len(new)} new finding(s) across {len(sick)} project(s)"
                            + (f", {crit} critical" if crit else ""),
                    "icon": "/icons/icon-192.png",
                    "tag": "project-health",
                    "data": {"url": "/"},
                }))
            except Exception:  # noqa: BLE001 - a failed push must not lose the state update
                pass
        from fsutil import atomic_write
        atomic_write(data / STATE_FILE,
                     json.dumps({"last_push_day": last_push_day, "notified": sorted(notified)}, indent=2),
                     mode=0o600)
    except Exception as exc:  # noqa: BLE001
        print(f"[project-health] digest write failed: {exc}")


async def _project_health_loop(ctx: dict) -> None:
    """Background sweep.  Never raises out - a health check that dies silently is worse than none."""
    await asyncio.sleep(120)   # let the service settle first
    while True:
        try:
            snap = await sweep_once(ctx)
            c = snap["counts"]
            if c["projects"]:
                print(f"[project-health] {c['crit']} critical, {c['warn']} warning(s) in {c['projects']} project(s)")
        except Exception as exc:  # noqa: BLE001
            print(f"[project-health] sweep failed: {exc}")
        await asyncio.sleep(max(60, _logic.INTERVAL_SEC))
