---
created: 2026-06-10
status: draft
---

# Spec 019 — Schedules Registry: unified view of all scheduled jobs

## Goal

Provide a single, read-only registry of every scheduled task running on the server —
cron, systemd timers, Claude Code jobs, Coolify scheduled tasks, n8n workflows, and
in-process schedulers — surfaced as a global **Schedules** tab in the cockpit.
Detect jobs that should have run but did not. Close the backlog card
"Единый Schedules UI".

## Context / Motivation

### The real bug this spec was born from

On 2026-06-10 an audit revealed that `~/logs/` did not exist on the server. Five cron
jobs had been silently swallowing their entire output for an unknown period:
- Two backup scripts (`backup-volumes.sh` and a second backup)
- `g2-community-watch`
- `meta-analyst`
- `rsync` → NAS

All five redirect stdout/stderr to a path under `~/logs/`. Because the directory was
missing, every invocation exited with a redirect error after doing nothing — yet cron
recorded exit 0 for the redirect itself. No alert was raised; no backup was verifiably
happening.

The fix (creating the directory) is trivial. **Catching this class of bug is the primary
acceptance criterion for this spec.** A cron entry whose redirect target does not exist
must appear as `status: broken` in the registry.

### Current state (2026-06-10 inventory)

- **User crontab**: 11 entries (backup scripts, g2-community-watch, meta-analyst,
  rsync → NAS, and others).
- **Root crontab / `/etc/cron.d`**: `backup-volumes.sh` scheduled under root.
- **Systemd timers**: 9 active timers, all in the `networking-crm*` family (e.g.
  `networking-crm-sync.timer`, `networking-crm-health.timer`); all currently live per
  `systemctl list-timers`.
- **In-process schedulers** (statically known, registered in `data/schedules.json`):
  - `pyrogram_bot`: finance job + `claude_health` via `JobQueue`
  - `proxmon-bot`: health-check job via `JobQueue`
  - `line_vpn_bot`: expiry-check job via `aioschedule`
  - `content-editor`: digest + publish jobs via `APScheduler`
- **n8n**: container running, 0 workflows configured.
- **Coolify**: scheduled tasks API available; no active tasks currently known.
- **Claude Code jobs** (`~/.claude/jobs`): present; content to be scanned at audit time.

### Why read-only MVP

The registry is a **second source of truth risk**. Cron is already the source of truth
for cron; systemd for timers; etc. Any write capability in the cockpit creates a
divergence hazard. Phase A is strictly read-only: scan, normalise, display. Edit
capabilities are explicitly a non-goal for this spec.

### Why a global tab, not per-project

Scheduled tasks span the server. A cron entry that backs up a project's database is
not "owned" by that project's topic. The global view is the primary surface. Per-project
filtering (showing only schedules whose resolved `project` field matches the current
project) is available as a secondary view in the project's overview panel — it is a
client-side filter of the global registry, not a separate data store.

---

## Design

### Normalised record schema

Every schedule entry from any source is normalised into a single record type:

```json
{
  "id": "<stable hash of source+schedule+command>",
  "source": "cron" | "systemd" | "claude_jobs" | "coolify" | "n8n" | "in_process",
  "schedule": "0 4 * * *",
  "command": "bash ~/scripts/backup-volumes.sh >> ~/logs/backup.log 2>&1",
  "project": "networking-os",
  "last_run": "2026-06-10T04:00:01Z",
  "next_run": "2026-06-11T04:00:00Z",
  "status": "ok" | "stale" | "broken" | "unknown",
  "purpose": "Daily backup of Docker volumes to NAS",
  "annotations": {}
}
```

`project` is resolved by matching the command path or working directory against known
project directories (`data/topics.json` + `data/registry.json`). Unresolvable → `null`.

`last_run` / `next_run`: populated from systemd's `list-timers` output (accurate) or
estimated from log file mtime heuristic (cron). Unknown → `null`.

`annotations`: a stable overlay layer written by the `investigate` action (Phase B);
survives re-scans. Stored in `data/schedules_annotations.json` (gitignored), keyed by
`id`. The collector merges annotations onto records after scanning.

### Collector: 6 sources

**Source 1: cron (user + root + `/etc/cron.d`)**

```bash
crontab -l                                     # user crontab
sudo crontab -l                                # root crontab (if accessible)
cat /etc/cron.d/*                              # system drops
```

Parse each line: skip comments and `CRON_TZ`/variable lines. Extract schedule (5 fields)
and command. Detect `status: broken` if:
- The command redirects to a path (`>> /path/` or `2> /path/`) and that path does not
  exist or is not writable.
- The command references a script path that does not exist.

This is the acceptance-critical detection: `>> ~/logs/foo.log` with `~/logs/` absent
→ `status: broken`.

**Source 2: systemd timers**

```bash
systemctl list-timers --all --output=json
systemctl show <unit> --property=ExecStart,Description,ActiveState
```

`last_run` / `next_run` come directly from `list-timers` (`LAST`, `NEXT` columns).
`status`: `ok` if `ActiveState=active`; `stale` if timer is active but `next_run` is
in the past and `last_run` is null; `broken` if `ActiveState=failed`; `unknown` otherwise.

**Source 3: Claude Code jobs (`~/.claude/jobs`)**

Read all job files (JSON). Extract schedule, command, enabled flag. `status: unknown`
if enabled but no `last_run` recorded. This source is trusted to exist; empty directory
→ zero records, not an error.

**Source 4: Coolify scheduled tasks**

```
GET http://localhost:8000/api/v1/servers/{server_uuid}/scheduled-tasks
Authorization: Bearer <COOLIFY_API_KEY>
```

Parse response; map `schedule`, `command`, `last_run_at`, `next_run_at` to normalised
fields. `status`: derive from `last_run_status` field if present. Source omitted
gracefully (with a collector warning) if the API is unreachable.

**Source 5: n8n workflows**

```
GET http://<n8n_host>/api/v1/workflows?active=true
X-N8N-API-KEY: <N8N_API_KEY>
```

For each active workflow with a Schedule Trigger node: extract cron expression and
workflow name. `last_run` from `updatedAt` (heuristic only). Currently 0 active
workflows → 0 records; this path is exercised once n8n is configured. Source omitted
gracefully if unreachable.

**Source 6: in-process schedulers**

These cannot be auto-scanned reliably without instrumenting each application.
**MVP approach (Phase A)**: a static registry in `data/schedules.json` (gitignored),
manually populated by the operator or by the `investigate` action. Each entry has
`source: in_process` plus a human-written `purpose`. The collector reads this file and
merges its entries into the full registry.

**Phase C (future)**: a code-scanning sub-agent (researcher, spec-017) that searches
project directories for `APScheduler`, `aioschedule`, `JobQueue`, `schedule.every` and
extracts declared intervals. Writes results back to `data/schedules.json` for human
review before inclusion.

### Status derivation

| Condition | Status |
|---|---|
| systemd `ActiveState=failed` | `broken` |
| cron command redirects to non-existent path | `broken` |
| cron command references non-existent script | `broken` |
| `last_run` is >2× the interval in the past (heuristic) | `stale` |
| systemd: `NextElapseUSecRealtime` in the past, `LastTriggerUSec` null | `stale` |
| n8n/Coolify: active and `last_run` within expected window | `ok` |
| no `last_run` data available | `unknown` |

### "Should run but didn't" detection and incident routing

When a record transitions to `broken` or `stale`, the collector emits an event through
the **existing incidents pipeline** (spec-012):
- `_report_incident(ctx, project, err)` with `exc_class="ScheduleMissed"` (for `stale`)
  or `"ScheduleBroken"` (for `broken`).
- This creates an err-card on the project's board (or the global `server-janitor` board
  if `project` is null) and sends a Telegram ping to the operator.
- **No auto-healing.** The incident card describes the problem; the operator investigates
  and fixes manually. Self-heal is not triggered (spec-010 removed; spec-012 safety
  layer applies).

### `purpose` and the `investigate` action

Schedules with `purpose: null` and `status: unknown` display an "Investigate" button
in the cockpit Schedules tab. Clicking it:
1. Creates a Backlog card in the relevant project's board (or `server-janitor` if no
   project) with title `[schedules] investigate: <command>`.
2. The executor sub-agent reads the script file + `git log --follow` for it, writes a
   one-paragraph annotation to `data/schedules_annotations.json` keyed by the record's
   `id`.
3. On the next collector scan, the annotation is merged into the record's `purpose`
   field and `status` is updated if the script analysis reveals a broken condition.

The annotations layer survives re-scans. It is the only write path the schedules
feature exercises on persistent storage.

### API

`GET /api/schedules` — returns the full normalised list, sorted by `next_run` ascending
(nulls last). Query params:
- `?project=<id>` — filter by resolved project.
- `?status=broken,stale` — filter by status (comma-separated).
- `?source=cron,systemd` — filter by source.

`POST /api/schedules/scan` — triggers an immediate re-scan; returns `{"queued": true}`.
The scan runs in the background; clients poll `GET /api/schedules` or watch the SSE
stream for a `schedules_updated` event.

`POST /api/schedules/{id}/investigate` — creates the investigate Backlog card described
above; returns `{"card_id": "..."}`.

No `PUT` / `PATCH` / `DELETE` endpoints. The registry is read-only from the API layer.

### Cockpit UI: global Schedules tab

A new top-level tab `schedules` added to the cockpit sidebar (alongside the existing
project tabs). It is a **global tab** — not scoped to a project.

Layout:
- Header: "Schedules" + "Scan now" button + last-scan timestamp.
- Filter bar: source selector, status selector, project selector.
- Table: `Schedule | Command (truncated) | Project | Last run | Next run | Status | Purpose | Actions`.
- Status badge colours: `ok` → green, `stale` → yellow, `broken` → red, `unknown` → grey.
- Actions column: "Investigate" button (only for `purpose: null` entries).
- Clicking a row expands it to show the full command, all fields, and the annotation
  history if present.

Per-project filter: the project overview panel gains a "Schedules" section showing
records where `project === currentProject.id`. This is a client-side filter of the
global API response; no new backend endpoint needed.

---

## Phases

### Phase A — Collector + JSON API + minimal table (S: ~3–4 h)

**Scope:** Implement the collector for sources 1–4 (cron, systemd, Claude jobs,
Coolify); expose `GET /api/schedules` and `POST /api/schedules/scan`; render a minimal
read-only table in the cockpit as a new global tab. The broken-cron detection (the
acceptance-critical `~/logs/` case) is included in this phase.

Deliverables:
- `bot.py` or a new `schedules.py` module: `collect_schedules(ctx)` async function
  scanning sources 1–4 + in-process static file; writes result to
  `data/schedules_cache.json` (gitignored).
- `webapp.py`: `GET /api/schedules`, `POST /api/schedules/scan` endpoints.
- Background task: re-scan on startup + every `SCHEDULES_SCAN_INTERVAL` (default 300s,
  env-configurable). Scan result replaces `data/schedules_cache.json` atomically (write
  to `.tmp` then `rename`).
- Frontend `SchedulesTab.tsx`: global tab with the table described above; status badges;
  filter bar (source + status only in Phase A; project filter in Phase B).
- `SchedulesTab` registered in `App.tsx` sidebar alongside other global views.
- `npm run build` passes with no type errors.

Acceptance (Phase A):
- `GET /api/schedules` returns a JSON array; each item has `id`, `source`, `schedule`,
  `command`, `status`, `project` (may be null), `purpose` (may be null).
- The 11 user-cron entries from the 2026-06-10 inventory are present.
- The 9 `networking-crm*` systemd timers are present with `status: ok`.
- **The acceptance-critical case:** add a test cron entry that redirects to a
  non-existent path → `status: broken` in the registry. Verify in the cockpit table.
- `POST /api/schedules/scan` → 200 `{"queued":true}` → after scan `GET /api/schedules`
  returns updated data.
- No write to any file outside `data/schedules_cache.json` during a scan.
- `pytest -q` — all existing tests green (748 baseline); new collector unit tests added
  (see Test plan).

### Phase B — Purpose annotations + Investigate action (M: ~3–5 h)

**Scope:** The `investigate` flow; annotations layer; per-project filter in UI; n8n
source (source 5); `purpose` field populated for in-process entries from static file.

Deliverables:
- `POST /api/schedules/{id}/investigate` endpoint.
- Annotations layer: `data/schedules_annotations.json`; collector merges on each scan.
- Conductor prompt template `schedules_investigate` in `data/prompts.json`: brief for
  the executor sub-agent (read script + git log → write annotation).
- `SchedulesTab.tsx`: "Investigate" button visible for `purpose: null` entries; project
  filter in filter bar.
- n8n collector (source 5); graceful skip if unreachable.
- `data/schedules.json` (static in-process registry) pre-populated with the 5 known
  in-process schedulers from the 2026-06-10 inventory.

Acceptance (Phase B):
- Click "Investigate" on an unknown-purpose entry → Backlog card created in the
  relevant project → executor agent runs → annotation written to
  `data/schedules_annotations.json` → next scan merges the annotation into the record →
  cockpit shows the annotation as `purpose`.
- Annotation survives a full re-scan (not overwritten by the collector).
- n8n source: 0 records (currently 0 workflows); no error in collector log.
- Project filter: selecting `networking-os` in the filter bar shows only records whose
  `project === "networking-os"`.

### Phase C — Stale/broken detection → incidents pipeline (M: ~3–5 h)

**Scope:** Wire the `broken` / `stale` status transitions into the incidents pipeline
(spec-012 `_report_incident`). Coolify source (source 4 polish). Incident dedup so a
persistently broken cron does not flood the board.

Deliverables:
- Collector: on each scan, compare new status against previous cache; emit
  `_report_incident` for transitions to `broken` or `stale` (not on every scan for
  already-known broken items — dedup by `id` in `dismissed_incidents.json`, same
  mechanism as spec-012).
- Telegram message format for `ScheduleBroken`: include the schedule expression, the
  broken command, and the reason (e.g. "redirect target `~/logs/backup.log` path does
  not exist").
- Coolify source: polish retry logic; add support for per-resource scheduled tasks
  (not just server-level).
- `data/schedules.json` auto-update path for in-process scanners (Phase C code scan
  sub-agent — see Design section).

Acceptance (Phase C):
- Introduce a broken cron entry (redirect to non-existent path) → collector scan →
  `err-card` appears on relevant project board within one scan interval → Telegram
  notification received.
- Same broken entry on second scan → no duplicate card (dedup).
- Fix the cron entry → next scan → status transitions to `ok` → no new incident.
- The acceptance-critical real-world case: the 5 crons that previously redirected to
  the missing `~/logs/` directory would have been caught. Reproduce by temporarily
  removing the directory, running a scan, and verifying `status: broken` on those 5
  entries.

---

## Test plan

All phases gate on `pytest -q` green (748 baseline).

### Phase A tests
- `test_collector_parses_crontab_lines` — feed a mock crontab string; assert N records
  with correct schedule/command fields.
- `test_collector_detects_broken_redirect_to_missing_dir` — cron entry with
  `>> /nonexistent/path/file.log`; assert `status=="broken"`.
- `test_collector_detects_broken_missing_script` — cron entry calling a non-existent
  script path; assert `status=="broken"`.
- `test_collector_parses_systemd_timers` — mock `systemctl list-timers` JSON output;
  assert correct `last_run`, `next_run`, `status`.
- `test_collector_systemd_failed_state_is_broken` — timer with `ActiveState=failed`;
  assert `status=="broken"`.
- `test_collector_skips_coolify_on_connection_error` — mock requests to raise
  `ConnectionError`; assert collector returns partial results without raising.
- `test_api_schedules_get_returns_array` — GET `/api/schedules` → 200, body is list.
- `test_api_schedules_filter_by_source` — GET `?source=cron` → only cron records.
- `test_api_schedules_filter_by_status` — GET `?status=broken` → only broken records.
- `test_api_schedules_scan_post` — POST `/api/schedules/scan` → 200 `{"queued":true}`.
- `test_schedules_cache_write_is_atomic` — concurrent scan calls do not corrupt the
  cache file (test with `asyncio.gather`).
- `test_record_id_is_stable` — same source+schedule+command on two scans → same `id`.

### Phase B tests
- `test_investigate_creates_backlog_card` — POST `/api/schedules/{id}/investigate` →
  card appears in relevant TASKS.md.
- `test_investigate_nonexistent_id_returns_404` — assert 404.
- `test_annotations_survive_rescan` — write to `schedules_annotations.json`; run
  collector; assert annotation merged into record.
- `test_annotations_not_overwritten_by_scan` — annotation manually set; scan runs;
  annotation unchanged.
- `test_n8n_collector_zero_workflows` — mock n8n API returning empty list; assert 0
  records, no error.

### Phase C tests
- `test_broken_status_triggers_report_incident` — collector transition to `broken` →
  mock `_report_incident` called once.
- `test_broken_status_deduped_on_second_scan` — same broken entry on second scan →
  `_report_incident` NOT called again (already in dismissed map).
- `test_ok_status_after_fix_no_new_incident` — fix the entry; transition to `ok` →
  no call.
- `test_stale_status_triggers_report_incident` — entry overdue by >2× interval →
  `_report_incident` called with `exc_class="ScheduleMissed"`.

---

## Risks

### Cron broken-detection false positives
The redirect-path detection uses string matching on the command text. Edge cases:
- Variable-expanded paths: `LOG=$HOME/logs/foo.log; cmd >> $LOG` — the path is not
  statically visible. **Mitigation:** Phase A only detects literal `>> ~/path` or
  `>> /abs/path` patterns. Variable-expanded redirects → `status: unknown`, not
  `broken`. Documented as a known limitation; Phase B can add shell-expansion evaluation
  via `bash -n` on the script.
- Commands that create the log directory themselves (e.g. `mkdir -p ~/logs && cmd >>
  ~/logs/foo.log`) would be incorrectly flagged. **Mitigation:** if the command
  contains `mkdir`, skip redirect-broken detection for that entry and leave
  `status: unknown`.

### systemd timer parsing changes across OS versions
`systemctl list-timers --output=json` format may vary. **Mitigation:** parse the tabular
text output (not JSON) as a fallback; both parsers maintained with a feature-detect on
the `--output=json` flag.

### n8n / Coolify API availability
Both are optional sources. If unavailable at scan time, the collector logs a warning and
returns partial results. No scan failure; no incident raised for collector errors.

### Stale/broken flood from large crontab on first scan
On first deployment, if many entries have `last_run: null` or point to now-broken paths,
the incidents pipeline could receive many `_report_incident` calls at once. **Mitigation:**
Phase C adds a bootstrap flag: the first scan after deployment is treated as a baseline
and does not emit incidents. Only transitions on subsequent scans trigger incidents. The
baseline is recorded in `data/schedules_cache.json` with a `bootstrapped: true` flag.

---

## Non-goals

- Creating, editing, or deleting cron entries, systemd timers, or any scheduled task
  from the cockpit UI. The registry is read-only. Edit capabilities are a future spec.
- A second source of truth: `data/schedules_cache.json` is a cache, not an authority.
  The authority is always the original source (crontab, systemd, etc.).
- Auto-fixing broken schedules (no self-heal; the incident card describes the problem).
- Full cron expression parsing for next-run calculation (use `croniter` if already
  available in the venv; otherwise leave `next_run: null` for cron source in Phase A
  and add `croniter` as a dependency in Phase B).
- Monitoring the schedules registry itself as a ClaudeOps project (it is a feature of
  the `claude-ops-bot` system project, not a standalone project).

---

## Related

- [[spec-012-incidents-realtime-push]] — the existing incidents pipeline that Phase C
  wires into. `_report_incident` is the integration point; no new incident mechanism
  is created.
- [[spec-017-fable-orchestrator]] — the `investigate` action (Phase B) spawns an
  executor sub-agent via the conductor pattern.
- [[spec-018-server-janitor]] — janitor audits may produce `keep` verdicts for cron
  jobs that the schedules registry marks as `ok`, providing cross-validation.
- [[spec-014-oss-hardening]] — all new endpoints, paths, and env vars follow OSS
  hardening rules: no hardcoded usernames, paths via `$HOME`, API keys via env.
- [[spec-015-oss-runtime]] — all new UI strings and API responses are in English.
