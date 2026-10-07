"""spec-094: host-load monitor — readers, thresholds, debounce and the snapshot, all against
fixture /proc + /sys trees so no test depends on the machine it runs on."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import load_monitor as lm

MB = 1024 * 1024
GB = 1024 * MB


class Tree:
    """A fake machine: tmp/proc + tmp/sys/fs/cgroup."""

    def __init__(self, root: Path):
        self.proc = root / "proc"
        self.sysr = root / "sys"
        self.cg = self.sysr / "system.slice" / "svc.service"
        self.cg.mkdir(parents=True)
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "self" / "cgroup").write_text("0::/system.slice/svc.service\n")
        self.fs = lm.Fs(proc=self.proc, sys=self.sysr)

    def w(self, rel: str, text: str):
        p = (self.proc if not rel.startswith("sys/") else self.sysr) / rel.removeprefix("sys/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def cgroup(self, current, limit="max", inactive=0, oom=0, slab=0):
        (self.cg / "memory.current").write_text(str(current))
        (self.cg / "memory.max").write_text(str(limit))
        (self.cg / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\nslab_reclaimable {slab}\n")
        (self.cg / "memory.events").write_text(f"low 0\nmax 0\noom_kill {oom}\n")

    def meminfo(self, total_gb=16, avail_gb=8, swap_total_gb=0, swap_free_gb=0):
        self.w("meminfo", f"MemTotal: {total_gb * 1024 * 1024} kB\nMemAvailable: {avail_gb * 1024 * 1024} kB\n"
                          f"SwapTotal: {swap_total_gb * 1024 * 1024} kB\nSwapFree: {swap_free_gb * 1024 * 1024} kB\n")

    def proc_entry(self, pid, ppid, argv0, rss_kb=1000, cwd=None):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(f"{pid} (x y) S {ppid} 0 0\n")
        (d / "status").write_text(f"Name: x\nVmRSS: {rss_kb} kB\n")
        (d / "cmdline").write_bytes((argv0 + "\0--flag\0").encode())
        if cwd:
            os.symlink(cwd, d / "cwd")
        with open(self.cg / "cgroup.procs", "a") as fh:
            fh.write(f"{pid}\n")


@pytest.fixture
def t(tmp_path):
    return Tree(tmp_path)


# ───────────────────────────── memory measure ─────────────────────────────

def test_working_set_excludes_reclaimable_page_cache(t):
    t.meminfo(total_gb=64)
    t.cgroup(current=9 * GB, limit=10 * GB, inactive=7 * GB)     # raw 90 %, real 20 %
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["limited"] and m["source"] == "cgroup"
    assert m["frac"] == pytest.approx(0.2)
    assert lm.cgroup_working_set_fraction(t.fs) == pytest.approx(0.2)


def test_ceiling_is_the_smaller_of_cgroup_limit_and_ram(t):
    t.meminfo(total_gb=8)
    t.cgroup(current=4 * GB, limit=64 * GB)                      # limit above physical RAM
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["ceiling"] == 8 * GB and m["frac"] == pytest.approx(0.5)


def test_unlimited_cgroup_is_not_a_guard_signal_but_still_measured(t):
    t.meminfo(total_gb=16)
    t.cgroup(current=8 * GB, limit="max")
    assert lm.cgroup_working_set_fraction(t.fs) is None          # guard stays inactive, as before
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert not m["limited"] and m["frac"] == pytest.approx(0.5)


def test_no_cgroup_falls_back_to_host_meminfo(tmp_path):
    t = Tree(tmp_path)
    (t.proc / "self" / "cgroup").write_text("")                  # cgroup v1 / macOS-like
    t.meminfo(total_gb=10, avail_gb=2)
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["source"] == "host" and m["frac"] == pytest.approx(0.8)
    assert lm.cgroup_working_set_fraction(t.fs) is None


def test_psi_parse(tmp_path):
    p = tmp_path / "memory.pressure"
    p.write_text("some avg10=1.50 avg60=0.00 avg300=0.00 total=9\nfull avg10=0.25 avg60=0.00 avg300=0.00 total=3\n")
    d = lm.read_psi(p)
    assert d["some"]["avg10"] == 1.5 and d["full"]["avg10"] == 0.25
    assert lm.read_psi(tmp_path / "missing") is None


# ───────────────────────────── thresholds ─────────────────────────────

def _lvl(signals, sid):
    return next((s["level"] for s in signals if s["id"] == sid), None)


def test_mem_levels_follow_the_guard_and_scale_with_it():
    mk = lambda f, g=0.75: {"guard": g, "mem": {"frac": f, "ws": f * 10 * GB, "ceiling": 10 * GB, "limited": True}}
    assert _lvl(lm.evaluate(mk(0.50)), "mem") == "ok"
    assert _lvl(lm.evaluate(mk(0.76)), "mem") == "warn"
    assert _lvl(lm.evaluate(mk(0.91)), "mem") == "crit"
    # an operator who raised the guard to 0.92 must not be told 0.91 is a warning
    assert _lvl(lm.evaluate(mk(0.91, g=0.92)), "mem") == "ok"


def test_missing_readings_emit_no_signal():
    assert lm.evaluate({"guard": 0.75}) == []


def test_disk_is_relative_with_an_absolute_floor():
    big = lambda free: {"disk": {"free": free, "total": 2000 * GB}}
    assert _lvl(lm.evaluate(big(250 * GB)), "disk") == "ok"       # 12.5 % free on 2 TB is fine
    assert _lvl(lm.evaluate({"disk": {"free": 8 * GB, "total": 100 * GB}}), "disk") == "warn"
    assert _lvl(lm.evaluate({"disk": {"free": 3 * GB, "total": 100 * GB}}), "disk") == "crit"
    assert _lvl(lm.evaluate({"disk": {"free": 0.5 * GB, "total": 2000 * GB}}), "disk") == "crit"


def test_oom_and_evictions_and_lag_and_fds():
    s = lm.evaluate({"oom": 1, "evictions": 2, "loop_lag": 6.0, "fds": {"used": 950, "limit": 1000}})
    assert _lvl(s, "oom") == "crit" and _lvl(s, "evictions") == "warn"
    assert _lvl(s, "loop_lag") == "crit" and _lvl(s, "fds") == "crit"
    assert _lvl(lm.evaluate({"oom": 0, "evictions": 0, "loop_lag": 0.05}), "oom") == "ok"


def test_full_swap_without_swap_in_is_not_thrashing():
    s = lm.evaluate({"swap": {"in_mb_min": 0.0, "occupancy": 0.999}})
    assert _lvl(s, "swap") == "ok"
    assert "not thrashing" in next(x for x in s if x["id"] == "swap")["text"]
    assert _lvl(lm.evaluate({"swap": {"in_mb_min": 800.0, "occupancy": 0.5}}), "swap") == "crit"


def test_pressure_is_half_at_warn_and_full_at_crit():
    assert lm._pressure(0.75, 0.75, 0.90) == pytest.approx(0.5)
    assert lm._pressure(0.90, 0.75, 0.90) == pytest.approx(1.0)
    assert lm._pressure(0.0, 0.75, 0.90) == 0.0


# ───────────────────────────── debounce ─────────────────────────────

def test_tracker_sustain_then_clear_hysteresis():
    tr = lm._Tracker()
    assert tr.update("warn", 0, (10, 0)) == "ok"                # not sustained yet
    assert tr.update("warn", 10, (10, 0)) == "warn"
    assert tr.update("crit", 11, (10, 0)) == "crit"             # crit escalates at once
    # one clean sample must not drop it, three must
    assert tr.update("ok", 12, (10, 0)) == "crit"
    assert tr.update("ok", 17, (10, 0)) == "crit"
    assert tr.update("ok", 22, (10, 0)) == "ok"


def test_tracker_flap_resets_the_sustain_clock():
    tr = lm._Tracker()
    tr.update("warn", 0, (10, 0))
    tr.update("ok", 5, (10, 0))
    assert tr.update("warn", 8, (10, 0)) == "ok"                # the 0..5 s run does not count


# ───────────────────────────── the monitor ─────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _healthy(t, cur=2 * GB):
    t.meminfo(total_gb=16, avail_gb=12)
    t.cgroup(current=cur, limit=8 * GB)
    t.w("vmstat", "pswpin 0\n")
    t.w("self/limits", "Max open files            1024                 4096                 files\n")
    (t.proc / "self" / "fd").mkdir(exist_ok=True)


def test_snapshot_ok_and_not_unknown(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.note_loop_lag(0.01)
    snap = m.sample({"live_max": 8, "running": 0, "chats_live": 2}, t.fs)
    assert snap["level"] == "ok" and snap["score"] < 50
    assert {"mem", "loop_lag", "evictions"} <= {s["id"] for s in snap["signals"]}
    assert snap["chats"] == {"live": 2, "max": 8}


def test_nothing_measurable_is_unknown_not_green(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (_ for _ in ()).throw(OSError()))
    t = Tree(tmp_path)
    (t.proc / "self" / "cgroup").write_text("")
    clk = Clock()
    snap = lm.Monitor(now=clk).sample({}, t.fs)
    assert snap["level"] == "unknown" and snap["signals"] == []


def test_guard_evictions_are_windowed_to_15_minutes(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.note_guard_eviction()
    assert _lvl(m.sample({"live_max": 8}, t.fs)["signals"], "evictions") == "warn"
    clk.t += 16 * 60
    for _ in range(3):                                          # drops after 3 clean samples
        clk.t += 5
        snap = m.sample({"live_max": 8}, t.fs)
    assert _lvl(snap["signals"], "evictions") == "ok"


def test_oom_is_a_sliding_window_not_a_latch(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.sample({"live_max": 8}, t.fs)                             # baseline: 0 kills
    t.cgroup(current=2 * GB, limit=8 * GB, oom=1)
    clk.t += 5
    assert _lvl(m.sample({"live_max": 8}, t.fs)["signals"], "oom") == "crit"
    clk.t += 20 * 60                                            # 20 min of quiet
    m.sample({"live_max": 8}, t.fs)
    clk.t += 5
    for _ in range(3):                                          # de-escalation hysteresis
        clk.t += 5
        snap = m.sample({"live_max": 8}, t.fs)
    assert _lvl(snap["signals"], "oom") == "ok" and snap["level"] != "crit"


def test_loop_lag_uses_the_max_so_one_stall_is_not_averaged_away(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    clk = Clock()
    m = lm.Monitor(now=clk)
    for _ in range(59):
        m.note_loop_lag(0.001)
    m.note_loop_lag(6.0)
    assert _lvl(m.sample({"live_max": 8}, t.fs)["signals"], "loop_lag") == "crit"


def test_leaked_agents_are_caught_by_headcount_after_the_sustain_time(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    COCKPIT = 100
    t.proc_entry(COCKPIT, 1, "/venv/bin/python")
    for i in range(12):                                         # 12 CLIs against a cap of 8
        pid = 200 + i
        t.proc_entry(pid, COCKPIT, "/x/_bundled/claude", rss_kb=300 * 1024, cwd=f"/work/proj{i % 3}")
        t.proc_entry(500 + i, pid, "/usr/bin/python3", rss_kb=50 * 1024)   # its MCP child
    clk = Clock()
    m = lm.Monitor(now=clk)
    inp = {"live_max": 8, "running": 0, "bg_agents": 0, "cockpit_pid": COCKPIT}
    first = m.sample(inp, t.fs)
    assert _lvl(first["signals"], "agents") == "ok"             # raw warn, not yet sustained
    clk.t += 125
    snap = m.sample(inp, t.fs)
    assert _lvl(snap["signals"], "agents") == "warn"
    ag = next(s for s in snap["signals"] if s["id"] == "agents")
    assert ag["value"] == "12"                                  # headcount only: the limit lives in the text
    assert "8 expected" in ag["text"]
    # footprint = CLI + its MCP child, named by project only
    assert snap["top"][0]["rss_mb"] == 350 and snap["top"][0]["project"].startswith("proj")


def test_chats_in_flight_and_card_runs_are_not_strays(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    t.proc_entry(100, 1, "/venv/bin/python")
    for i in range(11):
        t.proc_entry(200 + i, 100, "/x/_bundled/claude", cwd="/work/p")
    clk = Clock()
    m = lm.Monitor(now=clk)
    inp = {"live_max": 8, "running": 3, "bg_agents": 0, "cockpit_pid": 100}   # 8 + 3 in flight
    m.sample(inp, t.fs)
    clk.t += 125
    assert _lvl(m.sample(inp, t.fs)["signals"], "agents") == "ok"


def test_snapshot_never_leaks_command_lines_or_paths(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    t.proc_entry(100, 1, "/venv/bin/python")
    t.proc_entry(200, 100, "/secret/path/_bundled/claude", cwd="/home/someone/private-project")
    snap = lm.Monitor(now=Clock()).sample({"live_max": 8, "cockpit_pid": 100}, t.fs)
    blob = str(snap)
    assert "/secret" not in blob and "--flag" not in blob and "/home/someone" not in blob
    assert any(c["project"] == "private-project" for c in snap["top"])


def test_tmpfs_temp_dir_fullness_is_a_signal(t, monkeypatch, tmp_path):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    tmp = str(tmp_path)
    t.w("mounts", f"tmpfs {tmp} tmpfs rw 0 0\n/dev/sda1 / ext4 rw 0 0\n")
    monkeypatch.setattr("tempfile.gettempdir", lambda: tmp)
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("U", (), {"total": 12 * GB, "used": 0, "free": int(0.4 * GB)})())
    snap = lm.Monitor(now=Clock()).sample({"live_max": 8}, t.fs)
    assert _lvl(snap["signals"], "tmp") == "crit"
    # an ordinary (disk-backed) /tmp is not reported at all
    t.w("mounts", "/dev/sda1 / ext4 rw 0 0\n")
    snap2 = lm.Monitor(now=Clock()).sample({"live_max": 8}, t.fs)
    assert _lvl(snap2["signals"], "tmp") is None


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("LOAD_MONITOR", "0")
    assert lm.enabled() is False
    monkeypatch.setenv("LOAD_MONITOR", "1")
    assert lm.enabled() is True


def test_a_held_level_keeps_its_hint(t, monkeypatch):
    """After a debounce rewrite the row is non-ok but must still say what to do."""
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t, cur=int(6.5 * GB))                               # 81 % of the 8 GB limit -> warn
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.sample({"live_max": 8}, t.fs)
    clk.t += 11
    snap = m.sample({"live_max": 8}, t.fs)
    mem = next(s for s in snap["signals"] if s["id"] == "mem")
    assert mem["level"] == "warn" and mem["hint"]
    _healthy(t, cur=2 * GB)                                      # raw drops to ok, the level is held
    clk.t += 5
    held = next(s for s in m.sample({"live_max": 8}, t.fs)["signals"] if s["id"] == "mem")
    assert held["level"] == "warn" and held["hint"], "a held level lost its hint"


def test_feeding_from_the_loop_thread_while_sampling_never_raises(t, monkeypatch):
    """sample() runs in a worker thread while the event loop appends lag samples and guard
    evictions: iterating a deque another thread is appending to raises RuntimeError."""
    import threading
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    m = lm.Monitor()
    stop = threading.Event()

    def feeder():
        while not stop.is_set():
            m.note_loop_lag(0.001)
            m.note_guard_eviction()

    th = threading.Thread(target=feeder)
    th.start()
    try:
        for _ in range(25):
            m.sample({"live_max": 8}, t.fs)
    finally:
        stop.set()
        th.join()


# ───────────────────────── review round 2 regressions ─────────────────────────

def test_a_not_yet_sustained_spike_is_not_a_clear_sample():
    """crit -> ok, ok, then a fresh (unsustained) warn: the signal is reading warn right now, so
    the level may step down to warn but must never skip to ok."""
    tr = lm._Tracker()
    tr.update("crit", 0, (10, 0))
    tr.update("ok", 5, (10, 0))
    tr.update("ok", 10, (10, 0))
    assert tr.update("warn", 15, (10, 0)) == "warn"
    # the spike after only TWO clean samples does not even count as a third
    tr2 = lm._Tracker()
    tr2.update("crit", 0, (10, 0))
    tr2.update("ok", 5, (10, 0))
    assert tr2.update("crit", 10, (10, 0)) == "crit" and tr2._clear == 0


def test_a_pid_reuse_cycle_does_not_hang_the_process_walk(t):
    _healthy(t)
    t.proc_entry(100, 1, "/venv/bin/python")
    t.proc_entry(300, 301, "/x/_bundled/claude", cwd="/work/a")       # PID reuse mid-scan:
    t.proc_entry(301, 300, "/x/_bundled/claude", cwd="/work/b")       # 300 <-> 301 form a loop
    out = lm.scan_processes(t.fs, 100, lm.cgroup_dir(t.fs))
    assert out is not None and out["claude_total"] == 2


@pytest.mark.parametrize("argv,expected", [
    (["/x/_bundled/claude", "--flag"], True),
    (["claude"], True),
    (["node", "--max-old-space-size=4096", "/usr/local/bin/claude"], True),
    (["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], True),
    (["grep", "claude"], False),
    (["which", "claude"], False),
    (["node", "/srv/app/server.js"], False),
    ([], False),
])
def test_is_claude(argv, expected):
    assert lm._is_claude(argv) is expected


def test_orphaned_claude_outside_the_cockpits_children_still_counts(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    t.proc_entry(100, 1, "/venv/bin/python")
    for i in range(12):
        t.proc_entry(200 + i, 1, "/x/_bundled/claude", cwd="/work/p")      # re-parented to init
    clk = Clock()
    m = lm.Monitor(now=clk)
    inp = {"live_max": 8, "running": 0, "bg_agents": 0, "cockpit_pid": 100}
    m.sample(inp, t.fs)
    clk.t += 125
    assert _lvl(m.sample(inp, t.fs)["signals"], "agents") == "warn"


def test_a_stopped_heartbeat_is_itself_the_stall(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.note_loop_lag(0.001)
    clk.t += 90                                   # the loop froze: no beat for 90 s, window long gone
    sig = next(s for s in m.sample({"live_max": 8}, t.fs)["signals"] if s["id"] == "loop_lag")
    assert sig["level"] == "crit"


def test_the_guard_ceiling_is_the_smaller_of_limit_and_ram(t, monkeypatch):
    import load_monitor
    t.meminfo(total_gb=16)
    t.cgroup(current=14 * GB, limit=32 * GB)                     # MemoryMax above physical RAM
    monkeypatch.setattr(load_monitor, "DEFAULT_FS", t.fs)
    assert lm.cgroup_working_set_fraction() == pytest.approx(14 / 16)


def test_dirty_and_writeback_pages_are_not_reclaimable(t):
    t.meminfo(total_gb=64)
    t.cgroup(current=9 * GB, limit=10 * GB, inactive=8 * GB)
    (t.cg / "memory.stat").write_text(f"inactive_file {8 * GB}\nfile_dirty {5 * GB}\nfile_writeback {1 * GB}\n")
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["ws"] == 9 * GB - 2 * GB                             # only 2 GB can be dropped for free


def test_unlimited_cgroup_still_sees_a_loaded_host(t):
    t.meminfo(total_gb=16, avail_gb=1)                            # other tenants ate the box
    t.cgroup(current=1 * GB, limit="max")
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["source"] == "host" and m["frac"] == pytest.approx(15 / 16)


def test_one_malformed_reading_does_not_blind_the_other_signals():
    s = lm.evaluate({"guard": 0.75,
                     "mem": {"frac": 0.8, "ws": 8 * GB, "ceiling": 10 * GB, "limited": True},
                     "disk": {"free": 0, "total": 0},             # ramfs / some FUSE report total 0
                     "tmp": {"free": 0, "total": 0, "path": "/tmp"},
                     "fds": {"used": 1, "limit": 0}})
    assert _lvl(s, "mem") == "warn" and _lvl(s, "disk") is None and _lvl(s, "fds") is None


def test_a_tiny_mostly_empty_volume_is_not_a_permanent_emergency():
    assert _lvl(lm.evaluate({"disk": {"free": 0.45 * GB, "total": 0.5 * GB}}), "disk") == "ok"
    assert _lvl(lm.evaluate({"disk": {"free": 0.04 * GB, "total": 0.5 * GB}}), "disk") == "crit"


def test_swap_signal_needs_swap(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)                                                   # SwapTotal 0
    assert _lvl(lm.Monitor(now=Clock()).sample({"live_max": 8}, t.fs)["signals"], "swap") is None
    t.meminfo(swap_total_gb=8, swap_free_gb=8)
    assert _lvl(lm.Monitor(now=Clock()).sample({"live_max": 8}, t.fs)["signals"], "swap") == "ok"


def test_cpu_load_is_divided_by_the_cgroup_quota_not_the_host_core_count(t, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "getloadavg", lambda: (2.0, 0, 0))
    _healthy(t)
    (t.cg / "cpu.max").write_text("100000 100000\n")             # one core's worth of quota
    clk = Clock()
    m = lm.Monitor(now=clk)
    m.sample({"live_max": 8}, t.fs)
    clk.t += 11                                                   # warn must persist 10 s to count
    cpu = next(s for s in m.sample({"live_max": 8}, t.fs)["signals"] if s["id"] == "cpu")
    assert cpu["level"] == "warn"                                 # 2.0 load on 1 core, not 0.03 on 64


def test_windows_are_monotonic_but_the_snapshot_carries_wall_time(t, monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    _healthy(t)
    snap = lm.Monitor(now=Clock(), wall=lambda: 1234.5).sample({"live_max": 8}, t.fs)
    assert snap["at"] == 1234.5


def test_mem_hint_only_mentions_the_guard_where_a_guard_can_act(t):
    unl = lm.evaluate({"guard": 0.75, "mem": {"frac": 0.8, "ws": 8 * GB, "ceiling": 10 * GB, "limited": False}})
    lim = lm.evaluate({"guard": 0.75, "mem": {"frac": 0.8, "ws": 8 * GB, "ceiling": 10 * GB, "limited": True}})
    assert "guard" not in unl[0]["hint"] and "guard" in lim[0]["hint"]


# ───────────────────────────── v2: slab, units, disk runway ─────────────────────────────

def test_reclaimable_slab_is_not_working_set(t):
    t.meminfo(total_gb=64)
    t.cgroup(current=9 * GB, limit=10 * GB, inactive=2 * GB, slab=3 * GB)
    m = lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))
    assert m["ws"] == 4 * GB                                     # 9 - 2 (cache) - 3 (slab)
    t.cgroup(current=9 * GB, limit=10 * GB, inactive=2 * GB, slab=20 * GB)   # absurd stat never goes negative
    assert lm.read_memory(lm.cgroup_dir(t.fs), lm.read_meminfo(t.fs))["ws"] == 0


def test_sizes_are_labelled_in_binary_units():
    s = lm.evaluate({"disk": {"free": 22 * GB, "total": 197 * GB}, "swap": {"in_mb_min": 3.0, "occupancy": 0.4}})
    assert next(x for x in s if x["id"] == "disk")["text"].startswith("Data disk 89% full, 22.0 GiB free")
    assert next(x for x in s if x["id"] == "swap")["value"] == "3 MiB/min in"


DAY = 86400.0
H = 3600.0


def _hist(per_day_gib, days, free_now_gib, step=600.0, now=10 * DAY):
    """History of a volume losing `per_day_gib` a day, sampled every `step` s up to `now`."""
    pts, t = [], now - days * DAY
    while t <= now:
        pts.append((t, int((free_now_gib + per_day_gib * (now - t) / DAY) * GB)))
        t += step
    return pts, now


def test_trend_recovers_a_steady_fill_rate():
    pts, now = _hist(6.0, 3.2, free_now_gib=22)
    tr = lm.disk_trend(pts, now)
    assert tr["base_days"] == 3 and tr["per_day"] / GB == pytest.approx(6.0, rel=0.02)


def test_trend_needs_two_full_days_of_history_and_fresh_points():
    for days in (0.9, 1.5):                                      # the time of day / one baseline: not a trend yet
        pts, now = _hist(6.0, days, free_now_gib=22)
        assert lm.disk_trend(pts, now) is None
    pts, now = _hist(6.0, 2.1, free_now_gib=22)
    assert lm.disk_trend(pts, now)["base_days"] == 2
    pts, now = _hist(6.0, 3.0, free_now_gib=22)
    assert lm.disk_trend(pts, now + 3 * H) is None               # newest point is 3 h old: stale
    assert lm.disk_trend(pts[:3], now) is None


def test_trend_is_silent_for_a_disk_that_is_emptying_or_flat():
    pts, now = _hist(-5.0, 3.0, free_now_gib=22)                 # cleanup freed space
    assert lm.disk_trend(pts, now)["per_day"] == 0.0
    pts, now = _hist(0.0, 3.0, free_now_gib=22)
    assert lm.disk_trend(pts, now)["per_day"] == 0.0


def test_trend_ignores_one_odd_point_at_either_end():
    pts, now = _hist(0.0, 3.0, free_now_gib=22)
    odd = {i for i, (t, _f) in enumerate(pts) if abs(t - (now - 3 * DAY)) < 1} | {len(pts) - 1}
    pts = [(t, f - 15 * GB if i in odd else f) for i, (t, f) in enumerate(pts)]   # a 15 GiB spike at both ends
    assert lm.disk_trend(pts, now)["per_day"] / GB < 0.5


def test_trend_baseline_is_phase_aligned_for_a_nightly_job():
    # +10 GiB written at 02:00 every night and pruned at 03:00: no net growth, big intra-day swing.
    now = 10 * DAY + 14 * H                                      # 14:00
    pts = []
    t = now - 3.4 * DAY
    while t <= now:
        tod = t % DAY
        pts.append((t, int((22 - (10 if 2 * H <= tod < 3 * H else 0)) * GB)))
        t += 600.0
    assert lm.disk_trend(pts, now)["per_day"] / GB < 0.5


def test_trend_falls_back_to_a_shorter_baseline_when_the_long_one_has_a_hole():
    pts, now = _hist(6.0, 3.0, free_now_gib=22)
    t0 = now - 3 * DAY
    pts = [(t, f) for t, f in pts if abs(t - t0) > 4 * H]        # the host was down around the 3-day mark
    tr = lm.disk_trend(pts, now)
    assert tr["base_days"] == 2 and tr["per_day"] / GB == pytest.approx(6.0, rel=0.02)


def _disk_sig(free_gib, total_gib, per_day_gib, base_days=3):
    raw = {"disk": {"free": int(free_gib * GB), "total": int(total_gib * GB)}}
    if per_day_gib is not None:
        raw["disk"]["trend"] = {"per_day": per_day_gib * GB, "base_days": base_days}
    return next(s for s in lm.evaluate(raw) if s["id"] == "disk")


def test_runway_grades_the_days_left_not_the_percent_used():
    s = _disk_sig(22, 197, 6.0)                                  # 89 % used, 3.7 days left
    assert s["level"] == "warn" and "full in ~3.7 days" in s["text"] and s["value"] == "89% · 3.7d"
    assert 0.5 <= s["pressure"] < 1.0 and "Find what grows" in s["hint"]
    assert _disk_sig(22, 197, 15.0)["level"] == "crit"           # 1.5 days
    ok = _disk_sig(22, 197, 1.0)                                 # 22 days: fine, but say so
    assert ok["level"] == "ok" and ok["value"] == "89% · 22d" and ok["pressure"] < 0.5


def test_runway_stays_quiet_where_it_has_no_business():
    assert _disk_sig(22, 197, 0.1)["level"] == "ok"              # below the churn floor
    assert "filling" not in _disk_sig(22, 197, 0.1)["text"]
    assert _disk_sig(150, 400, 100.0)["level"] == "ok"           # 37 % free: not a runway problem yet
    assert _disk_sig(22, 197, None)["value"] == "89%"            # no history yet: plain reading
    assert _disk_sig(22, 197, 0.0)["level"] == "ok"              # shrinking


def test_a_static_crit_is_never_softened_by_a_slow_trend():
    s = _disk_sig(3, 100, 0.3)                                   # 97 % used: crit by the old rule
    assert s["level"] == "crit"


def _disk_monitor(tmp_path, monkeypatch, free_of, wall):
    import collections
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(197 * GB, 0, free_of()))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    t = Tree(tmp_path / "fx")
    m = lm.Monitor(now=Clock(), wall=wall)
    return m, t


def test_monitor_collects_history_and_reports_the_runway_after_a_day(tmp_path, monkeypatch):
    w = Clock()
    cur = {"free": 40 * GB}
    m, t = _disk_monitor(tmp_path, monkeypatch, lambda: int(cur["free"]), w)
    hist = tmp_path / "hist.json"
    m.attach_disk_history(hist)
    inp = {"data_dir": tmp_path, "live_max": 8}
    first = m.sample(inp, t.fs)
    assert "filling" not in next(s for s in first["signals"] if s["id"] == "disk")["text"]   # no history yet
    for _ in range(int(2.6 * DAY // 600)):                       # 2.6 days at 6 GiB/day
        w.t += 600
        cur["free"] -= 6 * GB * 600 / DAY
        snap = m.sample(inp, t.fs)
    d = next(s for s in snap["signals"] if s["id"] == "disk")
    import re
    assert re.search(r"filling (5\.9|6\.0) GiB/day \(held over the last 2 d\)", d["text"]) and "faster" not in d["text"]
    assert hist.exists()


def test_history_survives_a_restart_and_a_swapped_volume_resets_it(tmp_path, monkeypatch):
    w = Clock()
    cur = {"free": 30 * GB}
    m, t = _disk_monitor(tmp_path, monkeypatch, lambda: int(cur["free"]), w)
    hist = tmp_path / "hist.json"
    m.attach_disk_history(hist)
    inp = {"data_dir": tmp_path, "live_max": 8}
    for _ in range(5):
        m.sample(inp, t.fs)
        w.t += 700
    n = len(m._disk_hist)
    assert n == 5
    m2 = lm.Monitor(now=Clock(), wall=w)                          # "restart"
    m2.attach_disk_history(hist)
    assert len(m2._disk_hist) == n
    # same file, but the volume is now a different size: the old points describe another disk
    monkeypatch.setattr(lm.shutil, "disk_usage",
                        lambda p: __import__("collections").namedtuple("du", "total used free")(500 * GB, 0, 100 * GB))
    m2.sample(inp, t.fs)
    assert len(m2._disk_hist) == 1


def test_a_corrupt_or_foreign_history_file_is_ignored(tmp_path, capsys):
    p = tmp_path / "hist.json"
    p.write_text("{not json")
    m = lm.Monitor()
    m.attach_disk_history(p)
    assert len(m._disk_hist) == 0 and "unreadable" in capsys.readouterr().out
    p.write_text('{"v": 2, "dev": 1, "total": 1, "points": [[1, 2]]}')
    m.attach_disk_history(p)
    assert len(m._disk_hist) == 0


def test_a_wall_clock_step_back_drops_the_future_points(tmp_path, monkeypatch):
    w = Clock()
    m, t = _disk_monitor(tmp_path, monkeypatch, lambda: 30 * GB, w)
    inp = {"data_dir": tmp_path, "live_max": 8}
    for _ in range(4):
        m.sample(inp, t.fs)
        w.t += 700
    w.t -= 3 * 700 + 5000                                         # NTP / VM resume: the clock jumps back
    m.sample(inp, t.fs)
    assert all(ts <= w.t for ts, _f in m._disk_hist)


def test_the_static_disk_rule_is_not_debounced_by_the_runway_sustain(t, monkeypatch):
    # `make doctor` samples a fresh Monitor twice, one second apart: a full disk must show at once.
    import collections
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(100 * GB, 0, int(0.5 * GB)))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    clk = Clock()
    m = lm.Monitor(now=clk)
    snap = m.sample({"data_dir": "/tmp"}, t.fs)
    assert _lvl(snap["signals"], "disk") == "crit"
    assert all(not k.startswith("_") for s in snap["signals"] for k in s)     # the private hint never leaks to the API


def test_a_runway_only_level_waits_out_its_sustain_then_holds(t, monkeypatch):
    import collections
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(197 * GB, 0, 22 * GB))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    clk, wall = Clock(), Clock()
    m = lm.Monitor(now=clk, wall=wall)
    # three days of history at 6 GiB/day that ends at 22 GiB free: runway 3.7 d -> raw warn, 89 % -> static ok
    m._disk_dev, m._disk_total = os.stat("/tmp").st_dev, 197 * GB
    now = wall.t
    m._disk_hist.extend((now - DAY * 3.2 + i * 600.0, int((22 + 6 * (3.2 * DAY - i * 600.0) / DAY) * GB))
                        for i in range(int(3.2 * DAY / 600) + 1))
    inp = {"data_dir": "/tmp"}
    seen = []
    for _ in range(14):                                                  # 14 samples, 60 s apart
        seen.append(_lvl(m.sample(inp, t.fs)["signals"], "disk"))
        clk.t += 60.0
    assert seen[0] == "ok" and seen[9] == "ok"                           # < 600 s: debounced
    assert seen[11] == "warn" and seen[13] == "warn"                     # sustained: warn, and it holds
    snap = m.sample(inp, t.fs)
    assert all(not k.startswith("_") for s in snap["signals"] for k in s)   # the private hint never leaks to the API


def test_total_size_jitter_is_not_a_new_volume(tmp_path, monkeypatch):
    import collections
    du = collections.namedtuple("du", "total used free")
    w = Clock()
    n = {"i": 0}
    # ZFS/btrfs/NFS style: `total` moves by a few KiB between samples; the history must keep growing
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(197 * GB + (n["i"] % 7) * 128 * 1024, 0, 30 * GB))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    t = Tree(tmp_path / "fx")
    m = lm.Monitor(now=Clock(), wall=w)
    m.attach_disk_history(tmp_path / "hist.json")
    for i in range(6):
        n["i"] = i
        m.sample({"data_dir": tmp_path}, t.fs)
        w.t += 700
    assert len(m._disk_hist) == 6


def test_a_one_off_step_is_not_a_fill_rate():
    # 100 GiB copied 2 h ago onto a volume with 150 GiB left of 1000: the last day moved 100 GiB, the two
    # days before it moved nothing. That is a step, not a sustained rate -> 0, however it is windowed.
    now = 10 * DAY
    pts, t = [], now - 3.2 * DAY
    while t <= now:
        pts.append((t, int((250 if t < now - 2 * H else 150) * GB)))
        t += 600.0
    tr = lm.disk_trend(pts, now)
    assert tr["per_day"] == 0.0 and tr["recent_per_day"] / GB == pytest.approx(100, rel=0.05)
    two = [(t, f) for t, f in pts if t >= now - 2.1 * DAY]
    assert lm.disk_trend(two, now)["per_day"] == 0.0


def test_a_fresh_runaway_shows_the_faster_recent_rate_but_is_graded_on_the_sustained_one():
    s = _disk_sig(22, 197, 4.2)
    assert "last 24 h" not in s["text"]
    raw = {"disk": {"free": 22 * GB, "total": 197 * GB,
                    "trend": {"per_day": 4.2 * GB, "recent_per_day": 12.5 * GB, "base_days": 3}}}
    d = next(x for x in lm.evaluate(raw) if x["id"] == "disk")
    assert "the last 24 h were faster: 12.5 GiB/day" in d["text"]
    assert d["level"] == "warn"                                  # 22/4.2 = 5.2 days: graded on what has held


def test_the_runway_only_speaks_for_a_disk_that_is_already_tight():
    assert _disk_sig(100, 400, 40.0)["level"] == "ok"            # 25 % free, 2.5 days at that rate: plenty of room
    assert _disk_sig(55, 400, 10.0)["level"] == "warn"           # 14 % free, 5.5 days


# ───────────────────── spec-096 P8b: findings of the hostile review of load meter v2 ─────────────────────

def _step_history(free_before_gib, free_after_gib, step_age_h, hist_days, now=10 * DAY, every=600.0):
    """Flat history with ONE jump `step_age_h` hours ago (a copy / restore / download)."""
    pts, t = [], now - hist_days * DAY
    while t <= now:
        pts.append((t, int((free_before_gib if t < now - step_age_h * H else free_after_gib) * GB)))
        t += every
    return pts, now


@pytest.mark.parametrize("hist_days", [2.1, 3.2])
@pytest.mark.parametrize("age_h", [2, 12, 23, 25, 40, 60])
def test_a_one_off_step_of_any_age_is_never_a_runway(age_h, hist_days):
    # 1000 GiB volume flat at 250 GiB free, one 200 GiB copy `age_h` ago -> 50 GiB (5 %) free, static rule ok.
    pts, now = _step_history(250, 50, age_h, hist_days)
    tr = lm.disk_trend(pts, now)
    assert tr is None or tr["per_day"] == 0.0                       # a step is not a trend
    raw = {"disk": {"free": 50 * GB, "total": 1000 * GB}}
    if tr is not None:
        raw["disk"]["trend"] = tr
    s = next(x for x in lm.evaluate(raw) if x["id"] == "disk")
    assert s["level"] == "ok" and "filling" not in s["text"]


@pytest.mark.parametrize("direction", [-1, +1])
def test_a_step_never_reads_as_a_rate_at_any_moment_of_its_life(direction):
    # The step slides through every segment boundary as `now` advances (once a day, for an hour). A
    # boundary value that AVERAGES the two sides of the step used to split it into two half-size
    # segments and graded 100 GiB/day for ten minutes at the one-day mark. Sweep every sample time.
    import random
    rnd = random.Random(3 if direction < 0 else 4)
    t_step = 10 * DAY
    series, tt = [], t_step - 3.5 * DAY
    while tt <= t_step + 3.2 * DAY:
        series.append((tt, int((250 if (tt < t_step) == (direction < 0) else 50) * GB)))
        tt += 600.0 + rnd.uniform(0, 40)
    checked = worst = 0
    for i, (now, _f) in enumerate(series):
        if now < t_step + 600.0:
            continue
        pts = [(t, f) for t, f in series[:i + 1] if t >= now - 4 * DAY]
        tr = lm.disk_trend(pts, now)
        checked += 1
        worst = max(worst, (tr or {}).get("per_day", 0.0))
    assert checked > 400
    assert worst < lm._DISK_NOISE_PER_DAY   # -1: 200 GiB written once; +1: 200 GiB freed once. Neither is a fill


def test_two_lumps_on_different_days_are_graded_at_their_average_not_at_the_larger():
    # 100 GiB written 2.5 d ago and again 0.5 d ago, nothing in between: 200 GiB over 3 days = 67/day
    now = 10 * DAY
    pts, tt = [], now - 3.2 * DAY
    while tt <= now:
        pts.append((tt, int((340 - (100 if tt >= now - 2.5 * DAY else 0) - (100 if tt >= now - 0.5 * DAY else 0)) * GB)))
        tt += 600.0
    tr = lm.disk_trend(pts, now)
    assert tr["per_day"] / GB == pytest.approx(200 / 3, rel=0.05)
    s = next(x for x in lm.evaluate({"disk": {"free": 140 * GB, "total": 1000 * GB, "trend": tr}}) if x["id"] == "disk")
    assert s["level"] == "warn" and "full in ~2.1 days" in s["text"]          # real growth, but not a crit on a lump


def test_a_one_off_step_never_alerts_through_the_monitor(t, monkeypatch):
    import collections
    from features.load_monitor.alerts import AlertState, decide
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(1000 * GB, 0, 50 * GB))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    clk, wall = Clock(), Clock()
    m = lm.Monitor(now=clk, wall=wall)
    m._disk_dev, m._disk_total = os.stat("/tmp").st_dev, 1000 * GB
    pts, _ = _step_history(250, 50, 2, 3.2, now=wall.t)
    m._disk_hist.extend(pts)
    st, levels = AlertState(), set()
    for _ in range(40):                                              # 40 x 60 s: well past every sustain
        snap = m.sample({"data_dir": "/tmp"}, t.fs)
        levels.add(_lvl(snap["signals"], "disk"))
        assert decide(snap, st, wall.t) is None
        clk.t += 60.0
        wall.t += 60.0
    assert levels == {"ok"}


def test_a_steady_fill_is_still_graded_with_noise_and_a_nightly_job():
    import random
    rnd = random.Random(11)
    now = 10 * DAY + 14 * H
    pts, tt = [], now - 3.4 * DAY
    while tt <= now:
        tod = tt % DAY
        free = 85 + 30.0 * (now - tt) / DAY - (10 if 2 * H <= tod < 3 * H else 0) + rnd.uniform(-0.5, 0.5)
        pts.append((tt, int(free * GB)))
        tt += 600.0 + rnd.uniform(0, 40)
    tr = lm.disk_trend(pts, now)
    assert tr["per_day"] / GB == pytest.approx(30.0, rel=0.06) and tr["base_days"] == 3
    s = next(x for x in lm.evaluate({"disk": {"free": 85 * GB, "total": 1000 * GB, "trend": tr}}) if x["id"] == "disk")
    assert s["level"] == "warn" and ("full in ~2.8 days" in s["text"] or "full in ~2.9 days" in s["text"])   # 85 / 30


def test_a_cleanup_inside_the_window_does_not_mask_a_real_fill():
    # 100 GiB freed 2.5 d ago, then a steady 30 GiB/day: 85 GiB free of 1000 now -> ~2.8 days, WARN
    now = 10 * DAY

    def free_at(tt):
        return 60 * GB if tt < now - 2.5 * DAY else int(160 * GB - 30 * GB * (tt - (now - 2.5 * DAY)) / DAY)

    pts, tt = [], now - 3.5 * DAY
    while tt <= now:
        pts.append((tt, free_at(tt)))
        tt += 600.0
    tr = lm.disk_trend(pts, now)
    assert tr is not None and tr["per_day"] / GB == pytest.approx(30.0, rel=0.06)
    s = next(x for x in lm.evaluate({"disk": {"free": free_at(now), "total": 1000 * GB, "trend": tr}}) if x["id"] == "disk")
    assert s["level"] == "warn" and "filling" in s["text"]


def test_a_history_that_cannot_support_a_slope_is_unknown_not_zero():
    # only 2 days of history and the older of them contains a cleanup: one clean day cannot be a trend
    now = 10 * DAY
    cleanup_at = now - 1.5 * DAY

    def free_at(tt):
        return 60 * GB if tt < cleanup_at else int(160 * GB - 30 * GB * (tt - cleanup_at) / DAY)

    pts, tt = [], now - 2.1 * DAY
    while tt <= now:
        pts.append((tt, free_at(tt)))
        tt += 600.0
    assert lm.disk_trend(pts, now) is None                           # unknown: no "0 GiB/day", no green claim
    # ...whereas a disk that only ever emptied or stayed flat has an honest zero
    emptied, _ = _hist(-5.0, 3.0, free_now_gib=22)
    assert lm.disk_trend(emptied, 10 * DAY)["per_day"] == 0.0


def test_a_phantom_scratch_file_does_not_move_the_end_of_a_realistic_history():
    # production spacing is 600 s + sampler jitter, so a 30-minute end window holds only 2-3 points
    import random
    rnd = random.Random(5)
    now0 = 10 * DAY
    pts, tt = [], now0 - 3.5 * DAY
    while tt <= now0:
        pts.append((tt, 10 * GB))
        tt += 600.0 + rnd.uniform(0, 40)
    pts[-2] = (pts[-2][0], 2 * GB)                                    # an 8 GiB file seen at one sample, gone at the next
    worst = 0.0
    for dt in range(0, 600, 5):
        tr = lm.disk_trend(pts, pts[-1][0] + dt)
        worst = max(worst, (tr or {}).get("per_day", 0.0))
    assert worst < lm._DISK_NOISE_PER_DAY                              # never a phantom fill rate


@pytest.mark.parametrize("hh,mm", [(2, 10), (2, 40), (2, 59), (3, 5), (3, 20)])
def test_a_nightly_job_reads_as_zero_growth_even_when_the_sample_lands_inside_it(hh, mm):
    # +10 GiB written 02:00-03:00 every night: evaluated at 02:40 the newest hour lies INSIDE the job,
    # and so does the same hour of every earlier day, so it must cancel instead of reading as growth.
    import random
    rnd = random.Random(hh * 100 + mm)
    now = 10 * DAY + hh * H + mm * 60.0
    pts, tt = [], now - 3.4 * DAY
    while tt <= now:
        tod = tt % DAY
        pts.append((tt, int((22 - (10 if 2 * H <= tod < 3 * H else 0)) * GB)))
        tt += 600.0 + rnd.uniform(0, 30)
    tr = lm.disk_trend(pts, now)
    assert tr["per_day"] / GB < 0.5


def _static_warn_runway_crit_monitor(monkeypatch):
    import collections
    du = collections.namedtuple("du", "total used free")
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: du(200 * GB, 0, 15 * GB))   # 92.5 % used, 15 GiB free
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0, 0))
    clk, wall = Clock(), Clock()
    m = lm.Monitor(now=clk, wall=wall)
    m._disk_dev, m._disk_total = os.stat("/tmp").st_dev, 200 * GB
    # a genuine 15 GiB/day fill for 3.2 days that ends at 15 GiB free: runway 1 day -> raw crit
    m._disk_hist.extend((wall.t - 3.2 * DAY + i * 600.0, int((15 + 15 * (3.2 * DAY - i * 600.0) / DAY) * GB))
                        for i in range(int(3.2 * DAY / 600) + 1))
    return m, clk, wall


def test_a_static_warn_is_not_delayed_by_a_worse_runway_after_a_restart(t, monkeypatch):
    m, clk, wall = _static_warn_runway_crit_monitor(monkeypatch)
    seen = {}
    for i in range(30):                                              # 5 s samples, 150 s
        snap = m.sample({"data_dir": "/tmp"}, t.fs)
        seen[i * 5] = _lvl(snap["signals"], "disk")
        if seen[i * 5] == "warn":
            assert snap["score"] < 100                               # an amber meter is never a full bar
        clk.t += 5.0
        wall.t += 5.0
    assert seen[0] == "ok" and seen[10] == "warn"                    # the static rule: its own 10 s debounce
    assert all(seen[s] == "warn" for s in range(10, 115, 5))         # never green while the runway is still debouncing
    assert seen[120] == "crit" and seen[145] == "crit"               # the runway crit lands after ITS sustain
    assert all(not k.startswith("_") for s in snap["signals"] for k in s)


def test_a_runway_that_disappears_hands_back_to_the_static_rule(t, monkeypatch):
    m, clk, wall = _static_warn_runway_crit_monitor(monkeypatch)
    for _ in range(30):
        snap = m.sample({"data_dir": "/tmp"}, t.fs)
        clk.t += 5.0
        wall.t += 5.0
    assert _lvl(snap["signals"], "disk") == "crit"
    m._disk_hist.clear()                                              # history gone (volume swapped): no runway any more
    m._disk_dev = None                                                # (a device change is what clears it in production)
    for _ in range(6):
        snap = m.sample({"data_dir": "/tmp"}, t.fs)
        clk.t += 5.0
        wall.t += 5.0
    assert _lvl(snap["signals"], "disk") == "warn"                    # the static warn remains; the crit is not held
    assert not any(k.endswith(":runway") for k in m._trackers)
