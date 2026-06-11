"""
schedules.py — Spec 019: Unified Schedules Registry.

Collector: scans 6 sources (cron, systemd, Claude jobs, Coolify, n8n, in-process),
normalises records, writes atomically to data/schedules_cache.json.

Broken-cron detection (acceptance-critical): a cron entry that redirects stdout/stderr
to a path whose parent directory does not exist is marked status="broken".

Phase C: stale/broken transitions emit _report_incident (anti-flood dedup + bootstrap flag).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Module-level state (set by _schedules_init) ───────────────────────────────
_CACHE_PATH: "Path | None" = None          # data/schedules_cache.json
_ANNOTATIONS_PATH: "Path | None" = None    # data/schedules_annotations.json
_STATIC_PATH: "Path | None" = None         # data/schedules.json  (in-process static registry)
_LAST_SCAN_TS: float = 0.0                 # unix timestamp of last successful scan
_SCAN_LOCK = asyncio.Lock()                 # one scan at a time
_SCAN_INTERVAL_SEC = int(os.environ.get("SCHEDULES_SCAN_INTERVAL", "300"))

# Bootstrap flag: first scan after process start does not emit incidents (Phase C dedup).
_BOOTSTRAPPED = False


# ── Init ─────────────────────────────────────────────────────────────────────

def _schedules_init(ctx: dict) -> None:
    """Called from webapp.start() — sets file paths and clears bootstrap flag."""
    global _CACHE_PATH, _ANNOTATIONS_PATH, _STATIC_PATH, _BOOTSTRAPPED
    data: Path = ctx["DATA"]
    _CACHE_PATH = data / "schedules_cache.json"
    _ANNOTATIONS_PATH = data / "schedules_annotations.json"
    _STATIC_PATH = data / "schedules.json"
    _BOOTSTRAPPED = False   # will be set True after first scan


# ── Stable record ID ─────────────────────────────────────────────────────────

def _record_id(source: str, schedule: str, command: str) -> str:
    """SHA-1 of source+schedule+command → 12-char hex. Stable across scans."""
    raw = f"{source}\x00{schedule}\x00{command}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


# ── Project resolver ─────────────────────────────────────────────────────────

def _resolve_project(ctx: dict, command: str, cwd: str | None = None) -> str | None:
    """
    Resolve a project id from the command string or working directory.
    Matches against project cwd basenames + topics.json.
    Returns basename of matching cwd, or None.
    """
    # Import lazily to avoid circular dependency
    try:
        import webapp as _wa
        projects = _wa._collect_projects(ctx)
        home = str(Path.home())
        for p in projects:
            pcwd = p.get("cwd", "")
            if not pcwd:
                continue
            # Normalise paths: expand ~ and $HOME
            norm_cwd = pcwd.replace("$HOME", home).replace("~", home)
            norm_cwd_path = str(Path(norm_cwd).resolve())
            # Check if command mentions the project path or cwd
            cmd_norm = command.replace("$HOME", home).replace("~", home)
            if norm_cwd_path in cmd_norm:
                return p["id"]
            # cwd match for systemd/in-process
            if cwd:
                cwd_norm = cwd.replace("$HOME", home).replace("~", home)
                if norm_cwd_path == str(Path(cwd_norm).resolve()):
                    return p["id"]
        return None
    except Exception:
        return None


# ── Broken detection helpers ──────────────────────────────────────────────────

_REDIRECT_RE = re.compile(
    r"""(?:>>?|2>)\s*([^\s&;|]+)""",
    re.VERBOSE,
)
_SCRIPT_RE = re.compile(r"""(?:^|\s)(~?/[^\s]+\.sh)""")


def _expand_home(path: str) -> str:
    """Expand ~ and $HOME to actual home directory."""
    home = str(Path.home())
    return path.replace("~", home).replace("$HOME", home)


def _check_cron_command_status(command: str) -> str:
    """
    Return 'broken', 'ok', or 'unknown' based on static command analysis.

    Rules (from spec):
    - command contains 'mkdir' → skip redirect check → 'unknown' (may self-create)
    - redirect to path whose PARENT dir does not exist → 'broken'
    - command calls a script path that does not exist → 'broken'
    - otherwise → 'unknown' (can't determine without runtime)
    """
    if "mkdir" in command:
        return "unknown"

    # Check redirects: >> /path/file.log, 2> /path/file.log, 2>&1 (skip &1)
    broken_reason = None
    for m in _REDIRECT_RE.finditer(command):
        raw_path = m.group(1).strip()
        # Skip &1, &2 (fd redirects)
        if raw_path.startswith("&"):
            continue
        expanded = _expand_home(raw_path)
        parent = Path(expanded).parent
        if not parent.exists():
            broken_reason = f"redirect target parent does not exist: {parent}"
            break

    if broken_reason:
        return "broken"

    # Check script paths
    for m in _SCRIPT_RE.finditer(command):
        raw = m.group(1).strip()
        expanded = _expand_home(raw)
        if not Path(expanded).exists():
            return "broken"

    return "unknown"


# ── Source 1: cron ────────────────────────────────────────────────────────────

_CRON_COMMENT_RE = re.compile(r"^\s*#")
_CRON_VAR_RE = re.compile(r"^\s*[A-Z_]+=")
_CRON_SPECIAL_RE = re.compile(r"^\s*@")


def _parse_crontab_text(text: str, ctx: dict) -> list[dict]:
    """Parse crontab text into normalised schedule records."""
    records: list[dict] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if _CRON_COMMENT_RE.match(line):
            continue
        if _CRON_VAR_RE.match(line):
            continue

        # @reboot / @daily etc — treat as special schedule
        if _CRON_SPECIAL_RE.match(line):
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            schedule = parts[0].strip()
            command = parts[1].strip()
        else:
            parts = line.split(None, 6)
            if len(parts) < 6:
                continue
            schedule = " ".join(parts[:5])
            command = parts[5]

        status = _check_cron_command_status(command)
        rec_id = _record_id("cron", schedule, command)
        records.append({
            "id": rec_id,
            "source": "cron",
            "schedule": schedule,
            "command": command,
            "project": _resolve_project(ctx, command),
            "last_run": None,
            "next_run": None,
            "status": status,
            "purpose": None,
            "annotations": {},
        })
    return records


async def _collect_cron(ctx: dict) -> list[dict]:
    """
    Collect cron entries from:
    - user crontab (crontab -l)
    - root crontab (sudo -n crontab -l -u root) — unavailable → source skipped
    - /etc/cron.d/* files
    """
    records: list[dict] = []

    # User crontab
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "crontab", "-l",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=8.0,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            text = out.decode("utf-8", errors="replace")
            records.extend(_parse_crontab_text(text, ctx))
    except Exception as e:
        log.warning("[schedules] user crontab unavailable: %s", e)

    # Root crontab (no interactive sudo — if it fails, skip gracefully)
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "sudo", "-n", "crontab", "-l", "-u", "root",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=8.0,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            text = out.decode("utf-8", errors="replace")
            for r in _parse_crontab_text(text, ctx):
                r["id"] = _record_id("cron-root", r["schedule"], r["command"])
                records.append(r)
    except Exception as e:
        log.debug("[schedules] root crontab unavailable: %s", e)

    # /etc/cron.d/*
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "sudo", "-n", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=5.0,
        )
        # Actually just read the files directly — may work without sudo
    except Exception:
        pass

    crond = Path("/etc/cron.d")
    if crond.is_dir():
        try:
            for f in crond.iterdir():
                if f.is_file():
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        # cron.d format: user field between schedule and command
                        for r in _parse_crontab_d_text(text, ctx, str(f)):
                            records.append(r)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("[schedules] /etc/cron.d unavailable: %s", e)

    return records


def _parse_crontab_d_text(text: str, ctx: dict, source_file: str) -> list[dict]:
    """Parse /etc/cron.d file format: <schedule 5 fields> <user> <command>."""
    records: list[dict] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if _CRON_COMMENT_RE.match(line):
            continue
        if _CRON_VAR_RE.match(line):
            continue
        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        schedule = " ".join(parts[:5])
        # parts[5] = user, parts[6] = command
        command = parts[6]
        status = _check_cron_command_status(command)
        rec_id = _record_id(f"cron-d:{source_file}", schedule, command)
        records.append({
            "id": rec_id,
            "source": "cron",
            "schedule": schedule,
            "command": command,
            "project": _resolve_project(ctx, command),
            "last_run": None,
            "next_run": None,
            "status": status,
            "purpose": None,
            "annotations": {},
        })
    return records


# ── Source 2: systemd timers ─────────────────────────────────────────────────

def _parse_systemd_timers_text(text: str) -> list[dict]:
    """
    Parse tabular output of `systemctl list-timers --all`.
    Columns: NEXT  LEFT  LAST  PASSED  UNIT  ACTIVATES
    The columns are fixed-width in the tabular output; header detection is used.
    Returns a list of {unit, next_raw, last_raw} dicts.
    """
    results: list[dict] = []
    lines = text.splitlines()
    if not lines:
        return results

    # Find header line
    header_idx = -1
    for i, line in enumerate(lines):
        if "UNIT" in line and "NEXT" in line:
            header_idx = i
            break
    if header_idx < 0:
        return results

    header = lines[header_idx]
    # Column positions by header keyword
    unit_pos = header.find("UNIT")
    next_pos = header.find("NEXT")
    last_pos = header.find("LAST")

    for line in lines[header_idx + 1:]:
        if not line.strip() or line.strip().startswith("timers") or "listed" in line.lower():
            break
        unit = ""
        next_raw = ""
        last_raw = ""
        if unit_pos >= 0 and unit_pos < len(line):
            unit_part = line[unit_pos:].split()
            unit = unit_part[0] if unit_part else ""
        if next_pos >= 0 and next_pos < len(line):
            next_raw = line[next_pos:unit_pos].strip() if unit_pos > next_pos else line[next_pos:].split("  ")[0].strip()
        if last_pos >= 0 and last_pos < len(line):
            last_end = next_pos if next_pos > last_pos else len(line)
            last_raw = line[last_pos:last_end].strip()
        if unit and unit.endswith(".timer"):
            results.append({"unit": unit, "next_raw": next_raw, "last_raw": last_raw})
    return results


def _parse_systemd_timers_json(data: list[dict]) -> list[dict]:
    """
    Parse JSON output of `systemctl list-timers --all --output=json`.
    Fields: unit, next, last, activates, ...
    """
    results: list[dict] = []
    for entry in data:
        unit = entry.get("unit", "")
        if not unit.endswith(".timer"):
            continue
        results.append({
            "unit": unit,
            "next_raw": str(entry.get("next", "") or ""),
            "last_raw": str(entry.get("last", "") or ""),
        })
    return results


def _iso_from_systemd_ts(raw: str) -> str | None:
    """Convert systemd timestamp string to ISO 8601 UTC string, or None."""
    if not raw or raw in ("n/a", "-", ""):
        return None
    # systemd format: "Mon 2026-06-10 04:00:01 UTC"
    try:
        # Strip weekday
        parts = raw.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) <= 4:
            raw = parts[1]
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S %Z")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


async def _get_systemd_unit_details(unit: str) -> dict:
    """Get ExecStart and ActiveState for a systemd unit."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "systemctl", "show", unit,
                "--property=ExecStart,Description,ActiveState",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=5.0,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", errors="replace")
        result: dict = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        return result
    except Exception:
        return {}


async def _collect_systemd(ctx: dict) -> list[dict]:
    """Collect systemd timer records."""
    records: list[dict] = []

    # Try JSON output first; fall back to tabular
    timer_list: list[dict] = []
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "systemctl", "list-timers", "--all", "--output=json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=10.0,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            raw = out.decode("utf-8", errors="replace").strip()
            data = json.loads(raw)
            if isinstance(data, list):
                timer_list = _parse_systemd_timers_json(data)
    except Exception:
        timer_list = []

    if not timer_list:
        # Fall back to tabular
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "systemctl", "list-timers", "--all",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=10.0,
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                text = out.decode("utf-8", errors="replace")
                timer_list = _parse_systemd_timers_text(text)
        except Exception as e:
            log.warning("[schedules] systemd timers unavailable: %s", e)
            return records

    # For each timer, get details
    for entry in timer_list:
        unit = entry["unit"]
        details = await _get_systemd_unit_details(unit)
        active_state = details.get("ActiveState", "unknown")
        description = details.get("Description", "")
        exec_start = details.get("ExecStart", "")
        # Extract actual command from ExecStart (format: "{ path=... ; argv[]=... ; }")
        command = description or unit
        if exec_start:
            m = re.search(r"path=([^;]+)", exec_start)
            if m:
                command = m.group(1).strip()

        last_run = _iso_from_systemd_ts(entry.get("last_raw", ""))
        next_run = _iso_from_systemd_ts(entry.get("next_raw", ""))

        # Status derivation
        if active_state == "failed":
            status = "broken"
        elif active_state == "active":
            if next_run is None and last_run is None:
                status = "stale"
            else:
                status = "ok"
        else:
            status = "unknown"

        schedule = unit  # systemd timers don't expose cron string easily
        rec_id = _record_id("systemd", unit, command)
        records.append({
            "id": rec_id,
            "source": "systemd",
            "schedule": schedule,
            "command": command,
            "project": _resolve_project(ctx, command),
            "last_run": last_run,
            "next_run": next_run,
            "status": status,
            "purpose": description or None,
            "annotations": {},
        })

    return records


# ── Source 3: Claude Code jobs ────────────────────────────────────────────────

async def _collect_claude_jobs(ctx: dict) -> list[dict]:
    """Read ~/.claude/jobs/*.json files."""
    records: list[dict] = []
    jobs_dir = Path.home() / ".claude" / "jobs"
    if not jobs_dir.is_dir():
        return records
    try:
        for f in jobs_dir.iterdir():
            if not f.suffix == ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                schedule = str(data.get("schedule", "")).strip()
                command = str(data.get("command", data.get("prompt", str(f.name)))).strip()
                enabled = bool(data.get("enabled", True))
                last_run_raw = data.get("last_run") or data.get("lastRun")
                last_run = str(last_run_raw) if last_run_raw else None

                if not enabled:
                    status = "unknown"
                elif last_run:
                    status = "ok"
                else:
                    status = "unknown"

                rec_id = _record_id("claude_jobs", schedule, command)
                records.append({
                    "id": rec_id,
                    "source": "claude_jobs",
                    "schedule": schedule,
                    "command": command,
                    "project": _resolve_project(ctx, command),
                    "last_run": last_run,
                    "next_run": None,
                    "status": status,
                    "purpose": data.get("description") or None,
                    "annotations": {},
                })
            except Exception:
                pass
    except Exception as e:
        log.warning("[schedules] claude jobs unavailable: %s", e)
    return records


# ── Source 4: Coolify ────────────────────────────────────────────────────────

async def _collect_coolify(ctx: dict) -> list[dict]:
    """
    GET http://localhost:8000/api/v1/servers/{server_uuid}/scheduled-tasks
    COOLIFY_API_TOKEN must be set; server UUID read from env (COOLIFY_SERVER_UUID
    or the constant from CLAUDE.md).
    """
    records: list[dict] = []
    token = os.environ.get("COOLIFY_API_TOKEN", "").strip()
    if not token:
        log.debug("[schedules] COOLIFY_API_TOKEN not set — coolify source skipped")
        return records

    server_uuid = os.environ.get("COOLIFY_SERVER_UUID", "f0kgss8ccgksokkscgc0sk4s")
    base_url = os.environ.get("COOLIFY_API_BASE", "http://localhost:8000/api/v1")
    url = f"{base_url}/servers/{server_uuid}/scheduled-tasks"

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            async with session.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tasks = data if isinstance(data, list) else data.get("data", [])
                    for task in tasks:
                        schedule = str(task.get("frequency") or task.get("schedule", "")).strip()
                        command = str(task.get("command", "")).strip()
                        last_run = task.get("last_run_at") or task.get("lastRunAt")
                        next_run = task.get("next_run_at") or task.get("nextRunAt")
                        last_status = task.get("last_run_status", "")
                        if last_status == "success":
                            status = "ok"
                        elif last_status in ("failed", "error"):
                            status = "broken"
                        else:
                            status = "unknown"
                        rec_id = _record_id("coolify", schedule, command)
                        records.append({
                            "id": rec_id,
                            "source": "coolify",
                            "schedule": schedule,
                            "command": command,
                            "project": _resolve_project(ctx, command),
                            "last_run": str(last_run) if last_run else None,
                            "next_run": str(next_run) if next_run else None,
                            "status": status,
                            "purpose": task.get("description") or None,
                            "annotations": {},
                        })
                elif resp.status == 404:
                    log.debug("[schedules] Coolify server-level scheduled tasks not found (404)")
                else:
                    log.warning("[schedules] Coolify API returned %s", resp.status)
    except Exception as e:
        log.warning("[schedules] Coolify source unavailable: %s", e)

    return records


# ── Source 5: n8n ─────────────────────────────────────────────────────────────

async def _collect_n8n(ctx: dict) -> list[dict]:
    """
    GET http://<n8n_host>/api/v1/workflows?active=true
    N8N_API_KEY must be set; N8N_HOST defaults to localhost:5678.
    0 active workflows → 0 records (correct, not an error).
    """
    records: list[dict] = []
    api_key = os.environ.get("N8N_API_KEY", "").strip()
    host = os.environ.get("N8N_HOST", "localhost:5678")
    # Determine scheme
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    url = f"{host}/api/v1/workflows?active=true"

    if not api_key:
        log.debug("[schedules] N8N_API_KEY not set — n8n source skipped")
        return records

    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
            async with session.get(url, headers=headers, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    workflows = data.get("data", data) if isinstance(data, dict) else data
                    if not isinstance(workflows, list):
                        return records
                    for wf in workflows:
                        nodes = wf.get("nodes", [])
                        schedule_nodes = [n for n in nodes if "schedule" in str(n.get("type", "")).lower()]
                        if not schedule_nodes:
                            continue
                        for node in schedule_nodes:
                            params = node.get("parameters", {})
                            rule = params.get("rule", {})
                            cron_expr = rule.get("cronExpression", "") or ""
                            name = wf.get("name", "")
                            rec_id = _record_id("n8n", cron_expr, name)
                            records.append({
                                "id": rec_id,
                                "source": "n8n",
                                "schedule": cron_expr,
                                "command": name,
                                "project": None,
                                "last_run": wf.get("updatedAt") or None,
                                "next_run": None,
                                "status": "ok" if wf.get("active") else "unknown",
                                "purpose": name or None,
                                "annotations": {},
                            })
                else:
                    log.warning("[schedules] n8n API returned %s", resp.status)
    except Exception as e:
        log.warning("[schedules] n8n source unavailable: %s", e)

    return records


# ── Source 6: in-process static registry ─────────────────────────────────────

def _collect_in_process() -> list[dict]:
    """
    Read data/schedules.json (static, operator-maintained registry for in-process
    schedulers). Returns entries as-is (source="in_process"). Missing file → [].
    """
    if _STATIC_PATH is None or not _STATIC_PATH.exists():
        return []
    try:
        data = json.loads(_STATIC_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        records: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            schedule = str(entry.get("schedule", "")).strip()
            command = str(entry.get("command", "")).strip()
            if not command:
                continue
            rec_id = entry.get("id") or _record_id("in_process", schedule, command)
            records.append({
                "id": rec_id,
                "source": "in_process",
                "schedule": schedule,
                "command": command,
                "project": entry.get("project"),
                "last_run": entry.get("last_run"),
                "next_run": entry.get("next_run"),
                "status": entry.get("status", "unknown"),
                "purpose": entry.get("purpose"),
                "annotations": entry.get("annotations", {}),
            })
        return records
    except Exception as e:
        log.warning("[schedules] in-process static registry unavailable: %s", e)
        return []


# ── Annotations overlay ───────────────────────────────────────────────────────

def _load_annotations() -> dict[str, dict]:
    """Read data/schedules_annotations.json → {id: {purpose, updated_at, ...}}."""
    if _ANNOTATIONS_PATH is None or not _ANNOTATIONS_PATH.exists():
        return {}
    try:
        data = json.loads(_ANNOTATIONS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_annotations(annotations: dict) -> None:
    """Write annotations dict atomically to data/schedules_annotations.json."""
    if _ANNOTATIONS_PATH is None:
        return
    try:
        _ANNOTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ANNOTATIONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(annotations, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(_ANNOTATIONS_PATH)
    except Exception as e:
        log.warning("[schedules] failed to save annotations: %s", e)


# ── Stale / broken detection & incident emission ──────────────────────────────

def _load_cache_raw() -> dict | None:
    """Read raw cache (for previous-state comparison). None on error."""
    if _CACHE_PATH is None or not _CACHE_PATH.exists():
        return None
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


async def _emit_schedule_incident(
    ctx: dict,
    record: dict,
    exc_class: str,
) -> None:
    """
    Emit an incident via webapp._report_incident for a broken/stale schedule.
    Uses the record's project or falls back to 'claude-ops-bot' (server-level).
    Anti-flood: dedup via _report_incident's own debounce + 24h dismissed_incidents.
    """
    try:
        import webapp as _wa
        project_id = record.get("project") or "claude-ops-bot"
        cmd_short = record.get("command", "unknown")[:80]
        where = f"schedule:{record['id']}:{cmd_short}"
        await _wa._report_incident(ctx, exc_class, where, project_id=project_id)
    except Exception as e:
        log.debug("[schedules] incident emit failed: %s", e)


async def _check_incidents(ctx: dict, new_records: list[dict], previous: dict | None) -> None:
    """
    Phase C: compare new statuses against previous scan.
    Emit incidents only for NEW transitions to broken/stale (not already known).
    First scan after bootstrap → no incidents.
    Confirmed = status bad in both current and previous scan.
    """
    global _BOOTSTRAPPED

    if not _BOOTSTRAPPED:
        _BOOTSTRAPPED = True
        return  # First scan is baseline — no incidents

    if previous is None:
        return  # No previous data to compare

    prev_by_id: dict[str, str] = {}
    for r in previous.get("records", []):
        prev_by_id[r["id"]] = r.get("status", "unknown")

    # Track what we've already emitted in this scan (per-run dedup)
    emitted: set[str] = set()

    for record in new_records:
        rid = record["id"]
        new_status = record.get("status", "unknown")
        old_status = prev_by_id.get(rid, "unknown")

        if new_status not in ("broken", "stale"):
            continue

        # Confirm: bad in previous scan too? (two consecutive bad scans)
        if old_status not in ("broken", "stale"):
            # First time bad — update prev tracking but don't emit yet
            continue

        if rid in emitted:
            continue
        emitted.add(rid)

        exc_class = "ScheduleBroken" if new_status == "broken" else "ScheduleMissed"
        await _emit_schedule_incident(ctx, record, exc_class)


# ── Main collector ─────────────────────────────────────────────────────────────

async def collect_schedules(ctx: dict) -> list[dict]:
    """
    Full scan: collect from all 6 sources, merge annotations, return normalised list.
    Each source is wrapped in try/except with timeout — one failing source never
    blocks the others.
    """
    records: list[dict] = []
    source_statuses: list[dict] = []

    async def safe_collect(name: str, coro) -> list[dict]:
        try:
            result = await asyncio.wait_for(coro, timeout=15.0)
            source_statuses.append({"source": name, "status": "ok", "count": len(result)})
            return result
        except asyncio.TimeoutError:
            source_statuses.append({"source": name, "status": "timeout"})
            log.warning("[schedules] %s timed out", name)
            return []
        except Exception as e:
            source_statuses.append({"source": name, "status": "unavailable", "error": str(e)})
            log.warning("[schedules] %s unavailable: %s", name, e)
            return []

    # Run all async sources
    results = await asyncio.gather(
        safe_collect("cron", _collect_cron(ctx)),
        safe_collect("systemd", _collect_systemd(ctx)),
        safe_collect("claude_jobs", _collect_claude_jobs(ctx)),
        safe_collect("coolify", _collect_coolify(ctx)),
        safe_collect("n8n", _collect_n8n(ctx)),
        return_exceptions=False,
    )

    for r in results:
        records.extend(r)

    # Source 6: in-process (sync)
    try:
        in_proc = _collect_in_process()
        source_statuses.append({"source": "in_process", "status": "ok", "count": len(in_proc)})
        records.extend(in_proc)
    except Exception as e:
        source_statuses.append({"source": "in_process", "status": "unavailable", "error": str(e)})

    # Merge annotations overlay (annotations survive re-scans)
    annotations = _load_annotations()
    for rec in records:
        ann = annotations.get(rec["id"])
        if ann:
            if ann.get("purpose"):
                rec["purpose"] = ann["purpose"]
            rec["annotations"] = ann

    return records, source_statuses


async def _write_cache(records: list[dict], source_statuses: list[dict]) -> None:
    """Atomically write scan result to data/schedules_cache.json."""
    if _CACHE_PATH is None:
        return
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "source_statuses": source_statuses,
        "records": records,
    }
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.rename(_CACHE_PATH)
    except Exception as e:
        log.error("[schedules] failed to write cache: %s", e)


def _read_cache() -> dict:
    """Read current cache from disk. Returns empty structure on missing/corrupt file."""
    if _CACHE_PATH is None or not _CACHE_PATH.exists():
        return {"scanned_at": None, "record_count": 0, "source_statuses": [], "records": []}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"scanned_at": None, "record_count": 0, "source_statuses": [], "records": []}


async def run_scan(ctx: dict) -> dict:
    """
    Run a full scan (protected by lock — concurrent calls share one scan).
    Returns the cache payload dict.
    Phase C: emit incidents for newly confirmed broken/stale transitions.
    """
    global _LAST_SCAN_TS
    async with _SCAN_LOCK:
        previous = _load_cache_raw()
        records, source_statuses = await collect_schedules(ctx)
        await _write_cache(records, source_statuses)
        _LAST_SCAN_TS = time.time()
        # Phase C: incident emission
        try:
            await _check_incidents(ctx, records, previous)
        except Exception as e:
            log.warning("[schedules] incident check failed: %s", e)
        return _read_cache()


async def _schedules_scan_loop(ctx: dict) -> None:
    """Background task: scan on startup, then every SCHEDULES_SCAN_INTERVAL seconds."""
    # Initial scan
    try:
        await run_scan(ctx)
        log.info("[schedules] initial scan complete")
    except Exception as e:
        log.warning("[schedules] initial scan failed: %s", e)

    while True:
        interval = int(os.environ.get("SCHEDULES_SCAN_INTERVAL", str(_SCAN_INTERVAL_SEC)))
        await asyncio.sleep(interval)
        try:
            await run_scan(ctx)
        except Exception as e:
            log.warning("[schedules] periodic scan failed: %s", e)


# ── Investigate action ─────────────────────────────────────────────────────────

async def investigate_schedule(ctx: dict, record_id: str) -> dict:
    """
    Phase B: create a Backlog card for an investigate action on a schedule entry.
    The card is created in the relevant project (or 'claude-ops-bot' if no project).
    Returns {"card_id": "..."}.
    """
    import webapp as _wa

    cache = _read_cache()
    record = None
    for r in cache.get("records", []):
        if r["id"] == record_id:
            record = r
            break

    if record is None:
        return None  # caller should return 404

    project_id = record.get("project") or "claude-ops-bot"
    project = _wa._find_project_by_id(ctx, project_id)
    if project is None:
        # Fall back to any registered project
        projects = _wa._collect_projects(ctx)
        project = projects[0] if projects else None
    if project is None:
        return None

    cwd = project["cwd"]
    name = project["name"]
    cmd = record.get("command", record_id)
    text = f"[schedules] investigate: {cmd[:100]}"
    description = (
        f"Investigate schedule entry `{record_id}`.\n"
        f"Source: {record.get('source')}\n"
        f"Schedule: {record.get('schedule')}\n"
        f"Command: {record.get('command')}\n"
        f"\n"
        f"Tasks:\n"
        f"1. Read the script or service definition.\n"
        f"2. Check git log for recent changes.\n"
        f"3. Write a one-paragraph annotation to "
        f"`data/schedules_annotations.json` keyed by `{record_id}` "
        f"with fields: `purpose` (string), `updated_at` (ISO timestamp).\n"
        f"4. If the script is broken, also set `status: broken` in the annotation.\n"
    )

    async with _wa._get_board_lock(cwd):
        _, preamble, cols = _wa._load_board(cwd)
        card_id = _wa._new_card_id()
        new_card: dict = {"id": card_id, "text": text, "description": description}
        cols["backlog"].insert(0, new_card)
        _wa._save_board(cwd, name, preamble, cols)

    return {"card_id": card_id}
