"""Host-load monitor (spec-094): is THIS machine overloaded, judged against ITS OWN limits.

Pure readers + evaluators over injectable /proc and /sys roots (unit-testable against fixture
trees) and one small in-process state holder. Stdlib only.

Rules that shape every line below:
- Unknown is never green. A reader returns None when its source does not exist on this host
  and the signal is simply not reported; zero measurable signals -> level "unknown".
- Nothing is tied to one machine. Memory is a fraction of the detected ceiling, PSI is
  unitless, disk/tmp/fds are fractions; only loop lag is absolute (responsiveness).
- Core module: engine.py imports it (the memory guard shares `cgroup_working_set_fraction`).
  It must NOT import webapp or features.*.
"""
from __future__ import annotations

import collections
import os
import platform
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}

GUARD_DEFAULT = 0.75          # == engine.LIVE_CLIENT_MEM_GUARD default
MEM_CRIT = 0.90
_EVICTION_WINDOW_S = 15 * 60
_OOM_WINDOW_S = 15 * 60
_SWAP_WINDOW_S = 60
_LAG_WINDOW_S = 60
_PROC_SCAN_EVERY_S = 30
_CLEAR_AFTER = 3              # consecutive lower samples before a level drops
_GIB = 1024 ** 3


def enabled() -> bool:
    """LOAD_MONITOR=0 (or the module toggled off in the Modules panel) disables everything."""
    if os.getenv("LOAD_MONITOR", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import modules
        return bool(modules.is_enabled("load_monitor"))
    except Exception:
        return True


@dataclass(frozen=True)
class Fs:
    """Roots of the pseudo filesystems; tests point these at a tmp_path tree."""
    proc: Path = Path("/proc")
    sys: Path = Path("/sys/fs/cgroup")


DEFAULT_FS = Fs()


# ───────────────────────────── low-level readers (never raise) ─────────────────────────────

def _text(p: Path) -> "str | None":
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def _int(p: Path) -> "int | None":
    t = _text(p)
    try:
        return int(t) if t is not None else None
    except ValueError:
        return None


def _kv(p: Path) -> "dict[str, int]":
    out: "dict[str, int]" = {}
    for ln in (_text(p) or "").splitlines():
        parts = ln.split()
        if len(parts) >= 2:
            try:
                out[parts[0].rstrip(":")] = int(parts[1])    # /proc/*/status, meminfo use "Key:"
            except ValueError:
                pass
    return out


def cgroup_dir(fs: "Fs | None" = None) -> "Path | None":
    """This process's cgroup-v2 directory, or None (CI, macOS, WSL1, cgroup v1)."""
    fs = fs or DEFAULT_FS
    t = _text(fs.proc / "self" / "cgroup")
    for ln in (t or "").splitlines():
        if ln.startswith("0::"):
            base = fs.sys / ln.split("::", 1)[1].strip().lstrip("/")
            if (base / "memory.current").exists():
                return base
    return None


def read_meminfo(fs: Fs = DEFAULT_FS) -> "dict[str, int] | None":
    """/proc/meminfo in bytes."""
    d = _kv(fs.proc / "meminfo")
    return {k.rstrip(":"): v * 1024 for k, v in d.items()} or None


def read_psi(path: Path) -> "dict[str, dict[str, float]] | None":
    t = _text(path)
    if not t:
        return None
    out: "dict[str, dict[str, float]]" = {}
    for ln in t.splitlines():
        parts = ln.split()
        if not parts:
            continue
        vals: "dict[str, float]" = {}
        for tok in parts[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                try:
                    vals[k] = float(v)
                except ValueError:
                    pass
        out[parts[0]] = vals
    return out or None


def _working_set(cur: int, stat: "dict[str, int]") -> int:
    """memory.current minus what the kernel can drop for free: inactive file cache that is neither
    dirty nor under writeback (those must be flushed first, so they still count as pressure)."""
    reclaimable = max(0, stat.get("inactive_file", 0) - stat.get("file_dirty", 0) - stat.get("file_writeback", 0))
    return max(0, cur - reclaimable)


def read_memory(cg: "Path | None", meminfo: "dict[str, int] | None") -> "dict[str, Any] | None":
    """Working-set memory against the detected ceiling.

    cgroup: (memory.current - inactive_file) / min(memory.max, MemTotal). Reclaimable page cache
    is excluded on purpose — raw memory.current sits near the limit on any git-heavy host while
    nothing is wrong (same definition the kubelet uses). No cgroup: 1 - MemAvailable/MemTotal.
    """
    total = (meminfo or {}).get("MemTotal")
    if cg is not None:
        cur = _int(cg / "memory.current")
        if cur is not None:
            raw_max = _text(cg / "memory.max")
            limit = int(raw_max) if raw_max and raw_max.isdigit() else None
            ceiling = min([v for v in (limit, total) if v]) if (limit or total) else None
            if ceiling:
                ws = _working_set(cur, _kv(cg / "memory.stat"))
                out = {"frac": ws / ceiling, "ws": ws, "ceiling": ceiling,
                       "limited": limit is not None, "source": "cgroup"}
                # An unlimited cgroup says nothing about the other tenants of the machine: take the
                # worse of "our working set" and "the host as a whole", or the meter would stay
                # green while the box swaps.
                if limit is None and total and (meminfo or {}).get("MemAvailable") is not None:
                    used = total - meminfo["MemAvailable"]
                    if used / total > out["frac"]:
                        out.update(frac=used / total, ws=used, ceiling=total, source="host")
                return out
    if meminfo and total and meminfo.get("MemAvailable") is not None:
        used = total - meminfo["MemAvailable"]
        return {"frac": used / total, "ws": used, "ceiling": total,
                "limited": False, "source": "host"}
    return None


def cgroup_working_set_fraction(fs: "Fs | None" = None) -> "float | None":
    """For the memory guard: working set / memory.max of THIS cgroup, None when no limit is set
    (the guard stays inactive on unlimited hosts, exactly as before it shared this module)."""
    cg = cgroup_dir(fs or DEFAULT_FS)
    if cg is None:
        return None
    raw_max = _text(cg / "memory.max")
    if not raw_max or not raw_max.isdigit() or int(raw_max) <= 0:
        return None
    cur = _int(cg / "memory.current")
    if cur is None:
        return None
    ceiling = int(raw_max)
    total = (read_meminfo(fs or DEFAULT_FS) or {}).get("MemTotal")
    if total:                              # a limit above physical RAM protects nothing
        ceiling = min(ceiling, total)
    return _working_set(cur, _kv(cg / "memory.stat")) / ceiling


def _fd_limit(fs: Fs) -> "int | None":
    for ln in (_text(fs.proc / "self" / "limits") or "").splitlines():
        if ln.startswith("Max open files"):
            parts = ln.split()
            try:
                return int(parts[3])
            except (IndexError, ValueError):
                return None
    return None


def _fs_type(path: str, fs: Fs) -> "str | None":
    best, best_type = "", None
    for ln in (_text(fs.proc / "mounts") or "").splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            mnt = parts[1].replace("\\040", " ")
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/") or mnt == "/") and len(mnt) >= len(best):
                best, best_type = mnt, parts[2]
    return best_type


def _is_claude(argv: "list[str]") -> bool:
    """The Claude Code CLI: the bundled/standalone binary (argv[0] is `claude`), or a node shim
    `node [flags] .../claude` / `.../claude-code/cli.js`. An argument that merely says "claude"
    (`grep claude`, `which claude`) is not an agent."""
    if not argv or not argv[0]:
        return False
    first = Path(argv[0]).name
    if first == "claude":
        return True
    if first in ("node", "nodejs", "bun"):
        rest = [a for a in argv[1:4] if a and not a.startswith("-")]
        return any(Path(a).name == "claude" or (Path(a).name == "cli.js" and "claude" in a) for a in rest)
    return False


def scan_processes(fs: Fs, cockpit_pid: int, cg: "Path | None") -> "dict[str, Any] | None":
    """RSS footprint per `claude` CLI (itself + its MCP children) and the cockpit's own RSS.

    Walks cgroup.procs (so re-parented MCP children still count); no cgroup -> the descendants of
    the cockpit pid. Returns None when /proc is unusable. Only project names and sizes leave this
    function — never command lines or paths.
    """
    pids: "list[int]" = []
    if cg is not None:
        pids = [int(x) for x in (_text(cg / "cgroup.procs") or "").split() if x.isdigit()]
    if not pids:
        try:
            pids = [int(n) for n in os.listdir(fs.proc) if n.isdigit()]
        except Exception:
            return None
    info: "dict[int, tuple[int, int, list[str]]]" = {}      # pid -> (ppid, rss_kb, argv)
    for pid in pids:
        stat = _text(fs.proc / str(pid) / "stat")
        if not stat or ")" not in stat:
            continue
        try:
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            continue
        rss = _kv(fs.proc / str(pid) / "status").get("VmRSS", 0)
        try:
            argv = (fs.proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except Exception:
            argv = []
        info[pid] = (ppid, rss, argv)
    if cockpit_pid not in info and cg is None:
        return None
    # Descendants only when we walked all of /proc (no cgroup to bound the set).
    if cg is None:
        keep = {cockpit_pid}
        grew = True
        while grew:
            grew = False
            for pid, (pp, _r, _a) in info.items():
                if pid not in keep and pp in keep:
                    keep.add(pid)
                    grew = True
        info = {p: v for p, v in info.items() if p in keep}
    children: "dict[int, list[int]]" = collections.defaultdict(list)
    for pid, (pp, _r, _a) in info.items():
        children[pp].append(pid)

    def subtree_kb(root: int) -> int:
        tot, stack, seen = 0, [root], set()
        while stack:
            cur = stack.pop()
            if cur in seen:               # /proc is read non-atomically: PID reuse can close a loop
                continue
            seen.add(cur)
            tot += info[cur][1] if cur in info else 0
            stack.extend(children.get(cur, ()))
        return tot

    home = Path.home().name
    claude_direct = 0
    chats: "list[dict[str, Any]]" = []
    for pid, (pp, _rss, argv) in info.items():
        if not _is_claude(argv):
            continue
        if pp == cockpit_pid:
            claude_direct += 1
        try:
            proj = Path(os.readlink(fs.proc / str(pid) / "cwd")).name
        except Exception:
            proj = "?"
        chats.append({"kind": "chat", "project": "free chat" if proj == home else proj,
                      "rss_mb": subtree_kb(pid) // 1024})
    chats.sort(key=lambda c: c["rss_mb"], reverse=True)
    return {"claude_children": claude_direct, "claude_total": len(chats), "chats": chats,
            "cockpit_mb": (info.get(cockpit_pid, (0, 0, []))[1]) // 1024}


# ───────────────────────────────────── signal evaluation ─────────────────────────────────────

def _grade(v: float, warn: float, crit: float) -> str:
    return CRIT if v >= crit else WARN if v >= warn else OK


def _pressure(v: float, warn: float, crit: float) -> float:
    """0..1 bar fill: 0.5 at the warn line, 1.0 at the crit line, linear in between."""
    if v <= 0 or warn <= 0:
        return 0.0
    if v < warn:
        return 0.5 * v / warn
    return min(1.0, 0.5 + 0.5 * (v - warn) / max(crit - warn, 1e-9))


def _sig(sid: str, level: str, pressure: float, value: str, text: str, hint: str = "") -> "dict[str, Any]":
    return {"id": sid, "level": level, "pressure": round(max(0.0, min(1.0, pressure)), 3),
            "value": value, "text": text, "hint": hint}


def _gb(n: float) -> str:
    return f"{n / _GIB:.1f} GB"


def _ev_mem(raw):
    m = raw.get("mem")
    if not m:
        return []
    guard = min(float(raw.get("guard", GUARD_DEFAULT)), 0.93)
    crit = MEM_CRIT if guard < MEM_CRIT else min(0.98, guard + 0.05)
    lvl = _grade(m["frac"], guard, crit)
    scope = "service limit" if m.get("limited") else "this machine"
    hint = (f"The memory guard evicts idle chats above {guard:.0%}. Raise the service memory limit, "
            "lower LIVE_CLIENT_MAX / LIVE_CLIENT_TTL_SEC, or stop what holds the memory."
            if m.get("limited") else
            "Free memory: lower LIVE_CLIENT_MAX / LIVE_CLIENT_TTL_SEC or stop what holds it.")
    return [_sig("mem", lvl, _pressure(m["frac"], guard, crit), f"{m['frac']:.0%}",
                 f"Memory in use: {_gb(m['ws'])} of {_gb(m['ceiling'])} ({scope}, reclaimable cache excluded)", hint)]


def _ev_host_mem(raw):
    hm = raw.get("host_mem_avail_frac")
    if hm is None or not (raw.get("mem") or {}).get("limited"):
        return []
    used = 1.0 - hm
    return [_sig("host_mem", _grade(used, 0.90, 0.96), _pressure(used, 0.90, 0.96), f"{used:.0%}",
                 f"Whole machine: {used:.0%} of RAM in use (other services share it)",
                 "Something besides Cardloop is using the RAM; the service limit alone will not protect it.")]


def _ev_psi(raw):
    p = raw.get("mem_psi")
    if p is None:
        return []
    return [_sig("mem_psi", _grade(p, 1.0, 10.0), _pressure(p, 1.0, 10.0), f"{p:.1f}%",
                 f"Tasks stalled waiting for memory: {p:.1f}% of the last 10 s",
                 "The kernel is actively reclaiming/swapping while work waits — chats feel frozen.")]


def _ev_swap(raw):
    sw = raw.get("swap")
    if sw is None:
        return []
    rate, occ = sw["in_mb_min"], sw.get("occupancy")
    occ_t = f", swap {occ:.0%} full" if occ is not None else ""
    idle = " (idle pages parked, not thrashing)" if rate < 50 and (occ or 0) > 0.8 else ""
    return [_sig("swap", _grade(rate, 50.0, 500.0), _pressure(rate, 50.0, 500.0), f"{rate:.0f} MB/min in",
                 f"Swap-in rate {rate:.0f} MB/min{occ_t}{idle}",
                 "Pages are being read back from swap continuously (thrashing).")]


def _ev_evictions(raw):
    ev = raw.get("evictions")
    if ev is None:
        return []
    return [_sig("evictions", _grade(ev, 1, 5), _pressure(ev, 1, 5), str(ev),
                 f"Memory guard evicted {ev} idle chat(s) in the last 15 min",
                 "Each eviction makes that chat's next message a cold resume. Free memory or raise the limit.")]


def _ev_agents(raw):
    ag = raw.get("agents")
    if ag is None:
        return []
    ex = ag["excess"]
    return [_sig("agents", _grade(ex, 3, 6), _pressure(ex, 3, 6), f"{ag['observed']}/{ag['allowed']}",
                 f"{ag['observed']} agent processes running, {ag['allowed']} expected (live-client cap + chats in flight)",
                 "More agent processes than the cockpit tracks: some were orphaned. "
                 "A restart reaps them; the top consumers below show where the memory sits.")]


def _ev_oom(raw):
    oom = raw.get("oom")
    if oom is None:
        return []
    return [_sig("oom", CRIT if oom >= 1 else OK, 1.0 if oom >= 1 else 0.0, str(oom),
                 f"Kernel OOM kills in the last 15 min: {oom}",
                 "The kernel killed a process for lack of memory — one chat probably died mid-turn.")]


def _ev_lag(raw):
    lag = raw.get("loop_lag")
    if lag is None:
        return []
    return [_sig("loop_lag", _grade(lag, 1.0, 5.0), _pressure(lag, 1.0, 5.0), f"{lag:.2f}s",
                 f"Cockpit event loop stalled up to {lag:.2f}s in the last minute",
                 "The server is not keeping up — look at CPU, memory pressure and blocking calls.")]


def _ev_fds(raw):
    fd = raw.get("fds")
    if not fd or fd["limit"] <= 0:
        return []
    fr = fd["used"] / fd["limit"]
    return [_sig("fds", _grade(fr, 0.70, 0.90), _pressure(fr, 0.70, 0.90), f"{fr:.0%}",
                 f"Open file descriptors: {fd['used']} of {fd['limit']}",
                 "Leaked pipes/sockets. At the limit the server stops accepting connections.")]


def _ev_disk(raw):
    dk = raw.get("disk")
    if not dk or dk["total"] <= 0:                      # ramfs / some FUSE report a zero total
        return []
    used_f, free = 1.0 - dk["free"] / dk["total"], dk["free"]
    # The absolute floor only applies to a volume that is actually filling up: a tiny, mostly empty
    # volume (a 512 MB docker volume at 10 %) is not an emergency.
    crit = used_f >= 0.97 or (free < 1 * _GIB and used_f >= 0.90)
    warn = used_f >= 0.90 and free < 20 * _GIB
    lvl = CRIT if crit else WARN if warn else OK
    pr = _pressure(used_f, 0.90, 0.97) if lvl != OK else min(0.49, _pressure(used_f, 0.90, 0.97))
    return [_sig("disk", lvl, 1.0 if crit else pr, f"{used_f:.0%}",
                 f"Data disk {used_f:.0%} full, {_gb(free)} free",
                 "Free some space — a full disk breaks writes to chats and the board.")]


def _ev_tmp(raw):
    tp = raw.get("tmp")
    if not tp or tp["total"] <= 0:
        return []
    u = 1.0 - tp["free"] / tp["total"]
    return [_sig("tmp", _grade(u, 0.85, 0.95), _pressure(u, 0.85, 0.95), f"{u:.0%}",
                 f"Temp dir {tp['path']} is RAM-backed (tmpfs) and {u:.0%} full — it competes with agents for RAM/swap",
                 "Clear old scratch files in the temp dir; a full tmpfs also makes tools fail with 'No space left'.")]


def _ev_cpu(raw):
    cpu = raw.get("cpu")
    if not cpu:
        return []
    if cpu["source"] == "psi":
        v, w, c, val = cpu["value"], 40.0, 80.0, f"{cpu['value']:.0f}%"
        txt = f"CPU: tasks waited for a core {cpu['value']:.0f}% of the last 10 s"
    else:
        v, w, c, val = cpu["value"], 1.5, 3.0, f"{cpu['value']:.1f}x"
        txt = f"CPU load is {cpu['value']:.1f}x the core count (approximate)"
    return [_sig("cpu", _grade(v, w, c), _pressure(v, w, c), val, txt, "The machine is CPU-bound; chats and the UI will lag.")]


_EVALUATORS = (_ev_mem, _ev_host_mem, _ev_psi, _ev_swap, _ev_evictions, _ev_agents, _ev_oom,
               _ev_lag, _ev_fds, _ev_disk, _ev_tmp, _ev_cpu)


def evaluate(raw: "dict[str, Any]") -> "list[dict[str, Any]]":
    """raw readings -> signals. A missing key means "not measurable here": no signal is emitted.
    Each signal is isolated: one malformed reading (a zero total, a missing field) drops only itself
    instead of blinding every other signal."""
    out: "list[dict[str, Any]]" = []
    for fn in _EVALUATORS:
        try:
            out.extend(fn(raw))
        except Exception:
            continue
    return out


class _Tracker:
    """Debounce one signal: escalate after its sustain time, de-escalate after consecutive clears."""

    def __init__(self) -> None:
        self.level = OK
        self._since: "dict[str, float | None]" = {WARN: None, CRIT: None}
        self._clear = 0

    def update(self, raw_level: str, now: float, sustain: "tuple[float, float]") -> str:
        r = _RANK[raw_level]
        for lvl, rank in ((WARN, 1), (CRIT, 2)):
            if r >= rank:
                if self._since[lvl] is None:
                    self._since[lvl] = now
            else:
                self._since[lvl] = None
        target = OK
        if self._since[WARN] is not None and now - self._since[WARN] >= sustain[0]:
            target = WARN
        if self._since[CRIT] is not None and now - self._since[CRIT] >= sustain[1]:
            target = CRIT
        if _RANK[target] >= _RANK[self.level]:
            self.level, self._clear = target, 0
        elif r >= _RANK[self.level]:
            # The raw reading is still at/above the held level; the lower target is only the
            # sustain delay of a fresh episode, not a recovery — it must not count as "clear".
            self._clear = 0
        else:
            self._clear += 1
            if self._clear >= _CLEAR_AFTER:
                # Stepping DOWN needs no sustain, but never below what the signal is reading right
                # now: crit -> (ok, ok, fresh warn) lands on warn, not on ok.
                self.level = raw_level if _RANK[raw_level] > _RANK[target] else target
                self._clear = 0
        return self.level


# Seconds a raw level must persist before it counts (warn, crit). Default 10 s = two samples.
_SUSTAIN: "dict[str, tuple[float, float]]" = {
    "agents": (120.0, 120.0),     # teardown windows and short helper CLIs must not flap it
    "oom": (0.0, 0.0), "mem_psi": (10.0, 5.0), "evictions": (0.0, 0.0), "loop_lag": (0.0, 0.0),
}
_DEFAULT_SUSTAIN = (10.0, 0.0)


class Monitor:
    """Windows over the raw samples + per-signal trackers -> one snapshot per sample()."""

    def __init__(self, now: "Any" = time.monotonic, wall: "Any" = time.time) -> None:
        # Windows and debounce clocks are monotonic: an NTP step or a VM resume must neither fire a
        # "sustained" condition instantly nor read the suspend as an event-loop stall.
        self._now = now
        self._wall = wall
        self.error: "str | None" = None
        self._lock = threading.Lock()          # one sample() at a time
        self._feed = threading.Lock()          # loop thread appends, sampler thread windows/iterates
        self._evictions: "collections.deque[float]" = collections.deque(maxlen=1024)
        self._lag: "collections.deque[tuple[float, float]]" = collections.deque(maxlen=4096)
        self._last_beat: "float | None" = None
        self._oom: "collections.deque[tuple[float, int]]" = collections.deque(maxlen=4096)
        self._swapin: "collections.deque[tuple[float, int]]" = collections.deque(maxlen=512)
        self._trackers: "dict[str, _Tracker]" = {}
        self._procs: "dict[str, Any] | None" = None
        self._procs_at = 0.0
        self._snap: "dict[str, Any] | None" = None
        self.started_at = now()

    # -- event feeds (called from the event loop; deque appends are thread-safe) --
    def note_guard_eviction(self) -> None:
        with self._feed:
            self._evictions.append(self._now())

    def note_loop_lag(self, lag_s: float) -> None:
        with self._feed:
            self._last_beat = self._now()
            self._lag.append((self._now(), max(0.0, lag_s)))

    def snapshot(self) -> "dict[str, Any] | None":
        return self._snap

    def note_error(self, err: "str | None") -> None:
        """The sampler loop reports a failing tick here so the API can say so instead of "measuring"."""
        self.error = err

    # -- one sampling pass (blocking /proc reads: run it in a worker thread) --
    def sample(self, inputs: "dict[str, Any]", fs: Fs = DEFAULT_FS) -> "dict[str, Any]":
        now = self._now()
        with self._lock:
            raw = self._collect(inputs, fs, now)
            sigs = evaluate(raw)
            # A signal that vanished from this sample (reader failed once, window emptied) must not
            # keep stale debounce state: it would show an old crit for samples after it recovered.
            live_ids = {x["id"] for x in sigs}
            self._trackers = {k: v for k, v in self._trackers.items() if k in live_ids}
            level = OK
            score = 0.0
            for s in sigs:
                tr = self._trackers.setdefault(s["id"], _Tracker())
                eff = tr.update(s["level"], now, _SUSTAIN.get(s["id"], _DEFAULT_SUSTAIN))
                if eff != s["level"]:
                    s["level"] = eff
                    s["pressure"] = (min(s["pressure"], 0.49) if eff == OK
                                     else 1.0 if eff == CRIT else max(s["pressure"], 0.5))
                level = level if _RANK[level] >= _RANK[eff] else eff
                score = max(score, s["pressure"])
            sigs.sort(key=lambda s: (-_RANK[s["level"]], -s["pressure"], s["id"]))
            procs = self._procs or {}
            top = list(procs.get("chats", []))[:3]
            if procs.get("cockpit_mb"):
                top.append({"kind": "cockpit", "project": "cockpit process", "rss_mb": procs["cockpit_mb"]})
            top.sort(key=lambda c: c["rss_mb"], reverse=True)
            mi = raw.get("_meminfo") or {}
            snap = {
                "level": level if sigs else "unknown",
                "score": int(round(score * 100)) if sigs else 0,
                "at": self._wall(),
                "chats": {"live": inputs.get("chats_live", 0), "max": inputs.get("live_max", 0)},
                "signals": sigs,
                "top": top[:4],
                "host": {"os": platform.system(), "cpus": os.cpu_count() or 0,
                         "mem_gb": round(mi.get("MemTotal", 0) / _GIB, 1) if mi.get("MemTotal") else None},
            }
            self._snap = snap
            return snap

    def _window(self, dq: "collections.deque", horizon: float, now: float) -> None:
        while dq and now - (dq[0][0] if isinstance(dq[0], tuple) else dq[0]) > horizon:
            dq.popleft()

    def _collect(self, inputs: "dict[str, Any]", fs: Fs, now: float) -> "dict[str, Any]":
        raw: "dict[str, Any]" = {"guard": inputs.get("guard", GUARD_DEFAULT)}
        mi = read_meminfo(fs)
        raw["_meminfo"] = mi
        cg = cgroup_dir(fs)
        raw["mem"] = read_memory(cg, mi)
        if mi and mi.get("MemTotal") and mi.get("MemAvailable") is not None:
            raw["host_mem_avail_frac"] = mi["MemAvailable"] / mi["MemTotal"]
        psi = read_psi(cg / "memory.pressure") if cg else None
        psi = psi or read_psi(fs.proc / "pressure" / "memory")
        if psi and "full" in psi:
            raw["mem_psi"] = psi["full"].get("avg10", 0.0)
        # swap-in rate over the last minute (+ occupancy for the text)
        vm = _kv(fs.proc / "vmstat")
        if "pswpin" in vm and (mi or {}).get("SwapTotal", 0) > 0:
            self._swapin.append((now, vm["pswpin"]))
            self._window(self._swapin, _SWAP_WINDOW_S, now)
            t0, p0 = self._swapin[0]
            page = 4096
            try:
                page = os.sysconf("SC_PAGE_SIZE")
            except (ValueError, OSError, AttributeError):
                pass
            span = max(now - t0, 1.0)
            occ = None
            if mi and mi.get("SwapTotal"):
                occ = 1.0 - mi.get("SwapFree", 0) / mi["SwapTotal"]
            raw["swap"] = {"in_mb_min": (vm["pswpin"] - p0) * page / 1048576 * 60.0 / span, "occupancy": occ}
        with self._feed:
            self._window(self._evictions, _EVICTION_WINDOW_S, now)
            n_evictions = len(self._evictions)
            self._window(self._lag, _LAG_WINDOW_S, now)
            lag_max = max((v for _t, v in self._lag), default=0.0)
            have_lag = bool(self._lag)
            if self._last_beat is not None:
                # No heartbeat for longer than its period means the loop is stalled RIGHT NOW; the
                # window above would otherwise forget a long freeze once its samples expire.
                lag_max = max(lag_max, now - self._last_beat - 1.0)
                have_lag = True
        # Only where the guard can act at all (a limited cgroup); elsewhere a constant 0 would
        # read as evidence of health that does not exist.
        if (raw["mem"] or {}).get("limited") or n_evictions:
            raw["evictions"] = n_evictions
        if cg is not None:
            kills = _kv(cg / "memory.events").get("oom_kill")
            if kills is not None:
                self._oom.append((now, kills))
                self._window(self._oom, _OOM_WINDOW_S, now)
                raw["oom"] = max(0, kills - self._oom[0][1])
        if have_lag or now - self.started_at > 10:
            raw["loop_lag"] = lag_max
        used, lim = None, _fd_limit(fs)
        try:
            used = len(os.listdir(fs.proc / "self" / "fd"))
        except Exception:
            pass
        if used is not None and lim:
            raw["fds"] = {"used": used, "limit": lim}
        data_dir = inputs.get("data_dir")
        if data_dir:
            try:
                du = shutil.disk_usage(str(data_dir))
                raw["disk"] = {"free": du.free, "total": du.total}
            except Exception:
                pass
        tmp = tempfile.gettempdir()
        if _fs_type(tmp, fs) in ("tmpfs", "ramfs"):
            try:
                du = shutil.disk_usage(tmp)
                raw["tmp"] = {"free": du.free, "total": du.total, "path": tmp}
            except Exception:
                pass
        psi_c = read_psi(cg / "cpu.pressure") if cg else None
        psi_c = psi_c or read_psi(fs.proc / "pressure" / "cpu")
        if psi_c and "some" in psi_c:
            raw["cpu"] = {"source": "psi", "value": psi_c["some"].get("avg10", 0.0)}
        else:
            try:
                cores = float(os.cpu_count() or 1)
                quota = (_text(cg / "cpu.max") or "").split() if cg else []
                if len(quota) == 2 and quota[0].isdigit() and quota[1].isdigit() and int(quota[1]) > 0:
                    cores = min(cores, max(0.1, int(quota[0]) / int(quota[1])))
                raw["cpu"] = {"source": "load", "value": os.getloadavg()[0] / cores}
            except (OSError, AttributeError):
                pass
        pid = inputs.get("cockpit_pid") or os.getpid()
        if now - self._procs_at >= _PROC_SCAN_EVERY_S or self._procs is None:
            self._procs, self._procs_at = scan_processes(fs, pid, cg), now
        if self._procs is not None:
            observed = self._procs["claude_total"] if cg is not None else self._procs["claude_children"]
            allowed = int(inputs.get("live_max", 0)) + int(inputs.get("running", 0)) + int(inputs.get("bg_agents", 0))
            raw["agents"] = {"observed": observed, "allowed": allowed, "excess": max(0, observed - allowed)}
        return raw


MONITOR = Monitor()


def note_guard_eviction() -> None:
    MONITOR.note_guard_eviction()
