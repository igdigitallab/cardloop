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

    def cgroup(self, current, limit="max", inactive=0, oom=0):
        (self.cg / "memory.current").write_text(str(current))
        (self.cg / "memory.max").write_text(str(limit))
        (self.cg / "memory.stat").write_text(f"anon 1\ninactive_file {inactive}\n")
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
    assert ag["value"] == "12/8"
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
