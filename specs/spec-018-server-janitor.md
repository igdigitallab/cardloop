---
created: 2026-06-10
updated: 2026-06-11
status: draft
note: spec-025 (2026-06-11) added a trash-purge sweep to webapp.py (_janitor_trash_purge_loop / _run_janitor_trash_purge) that runs hourly and purges data/trash/ entries older than TRASH_RETENTION_DAYS (default 7). This is separate from spec-018's ~/.janitor-trash quarantine and lives in webapp.py, not in server-janitor/janitor.py.
---

# Spec 018 — Server Janitor: on-demand audit and safe cleanup

## Goal

Provide an operator-triggered audit of the home server that categorises clutter,
produces a structured report with verdicts, and converts each verdict into a Kanban
card requiring explicit human approval before anything is touched. Nothing is deleted
automatically; the only safe automation is a 30-day quarantine purge.

## Context / Motivation

### Why now

An inventory run on 2026-06-10 revealed the following on `docker-core` (`$HOME`):

- **33 `CLAUDE.md` files** spread across the home tree; at least 5 are stale (last
  modified >20 days ago, abandoned project scaffolding).
- **Duplicate / mergeable project directories**: `line_vpn_bot` + `linevpn-portal`;
  `navidrome` + `navidrome-coolify`; `song-maker` + `music-bot` + `music` (all three
  dead since 2024-05-24); `cloakbrowser-env` + `cloakbrowser-manager`.
- **~2.5 GB of node_modules**: npm-global 752 MB, npx-cache ~850 MB,
  `teleprompter` 234 MB, `pyrogram_bot/scripts/finance` 185 MB.
- **~1.3 GB of Python venvs**; 141 MB of `__pycache__` directories.
- **138 MB of loose SQL dumps** from the `khronika` project scattered in `$HOME`
  (dated 2024-05-22 – 2024-05-25, never moved to a proper archive location).
- **`pdf-tools-env`**: 144 MB venv with no active project referencing it.
- **n8n container**: running for 2 weeks with 0 workflows configured.

This is a routine hygiene problem that compounds over time. Manual cleanup is
error-prone (no audit trail) and easy to defer. An automated audit with human-approved
execution is the right shape.

### Why a system project, not a code module

Janitor needs no new engine code. It is a **system project** registered in ClaudeOps
with `cwd = $HOME`. A conductor (Fable, spec-017) fans out read-only researcher
sub-agents by category, synthesises their findings, and writes verdicts as Kanban
cards. The operator approves by moving a card; a separate executor agent then performs
the action. This is the conductor pattern (spec-017) applied to infra housekeeping.

### The self-heal lesson

Spec-010 (self-healing) was removed from this project on 2026-06-10 precisely because
it took autonomous actions over other sessions without operator approval. Janitor takes
that lesson as its primary design constraint: **no autonomous destructive action, ever**.
The quarantine is a safe holding area, not a deletion. Purging quarantine after 30 days
is the only timer-triggered action, and it is scoped to files the operator already
approved for removal.

---

## Design

### Architecture: system project `server-janitor`

Janitor is registered in `data/topics.json` / ClaudeOps registry as a project named
`server-janitor` with `cwd = $HOME` (or the operator's home directory via `$HOME`
expansion — no hardcoded path, per spec-014 OSS rules).

It has:
- A `playbook CLAUDE.md` at `$HOME/server-janitor/CLAUDE.md` describing audit
  categories, verdict grammar, quarantine protocol, and the conductor's instructions.
- A prompt template in `data/prompts.json` (key `janitor_audit`) that the operator
  can trigger from the cockpit or via `/janitor` in Telegram.
- A `TASKS.md` (standard ClaudeOps board) where verdict cards accumulate in Backlog.
- A `data/janitor_manifest.json` (gitignored) tracking quarantine moves.

No new Python modules in `bot.py` / `webapp.py` in Phase A.

### Conductor + researcher fan-out (spec-017 pattern)

When the operator triggers an audit, the conductor (Fable) receives the
`janitor_audit` prompt and delegates **one researcher sub-agent per category**:

| Category | Sub-agent task |
|---|---|
| `claude_md_sprawl` | Find all `CLAUDE.md` files; report mtime, project status (git activity, systemd unit), stale candidates |
| `duplicate_dirs` | Identify mergeable / redundant project directories; report last commit, disk size, systemd/docker references |
| `tmp_log_litter` | Find `*.log`, `*.tmp`, loose SQL/CSV/JSON dumps in `$HOME` and `/tmp`; report size, age |
| `dead_venvs` | Find all `venv/`, `.venv/`, `*-env/` directories; cross-reference with active project presence |
| `node_modules` | Find all `node_modules/` and npm/npx caches; report size, project activity |
| `docker_debris` | Find stopped/exited containers, untagged images, unused volumes; report age and size |

All sub-agents are `researcher` role (spec-017): read-only, no `Write`/`Edit`.
Bash is available for `find`, `du`, `git log`, `docker ps -a`, `systemctl list-units`.

### Verdict grammar

The conductor synthesises findings into a report with one verdict per item:

| Verdict | Meaning |
|---|---|
| `delete` | Safe to remove; no active references found |
| `quarantine` | Move to `~/.janitor-trash/<date>/`; manifest entry; auto-purge in 30 days |
| `merge X into Y` | Migrate content + git history of X into Y; requires a dedicated merge agent |
| `archive` | Move to a designated archive path; no deletion |
| `keep` | Active, referenced, or uncertain; no action |

Each `delete` verdict is translated to a `quarantine` action at execution time — the
executor never calls `rm`. "Delete" in the verdict means "operator has approved removal;
executor moves to quarantine."

### Kanban cards (one per verdict)

Each non-`keep` verdict becomes a Backlog card in `server-janitor/TASKS.md` with:
- `title`: `[janitor] <verdict>: <target>`
- `description`: what was found, why this verdict, exact paths, size/age data.
- Category tag so cards can be processed in batches.

The operator approves by moving the card to In Progress (or clicking the cockpit
"Apply" button on a card — standard C2-gate, spec-005). The executor agent then
performs the quarantine move and writes a manifest entry.

**Cards are never auto-executed.** `_run_card` for janitor cards runs only after
explicit operator action on the board.

### Quarantine protocol

```
~/.janitor-trash/
  2026-06-10/
    manifest.json        # [{item, from, why, verdict, ts}]
    line_vpn_bot/        # moved directory
    khronika_dump_*.sql  # moved files
```

- Executor uses `mv`, not `rm`.
- `manifest.json` is appended atomically (read–modify–write with file lock).
- Auto-purge: a cron entry (added in Phase B) runs `janitor purge --older-than 30d`
  which calls `rm -rf ~/.janitor-trash/<date>/` for dates >30 days old and removes the
  corresponding manifest entries. This is the **only** timer-triggered deletion in the
  entire system.
- If the operator wants to recover an item before the 30-day window, they move it back
  manually; the manifest provides the original path.

### Merge verdict (project consolidation)

When the conductor issues a `merge X into Y` verdict, the card description includes:
1. The exact migration plan (copy files, rebase git history or squash-merge, update
   any systemd/docker references, archive X).
2. A note that this card spawns a **dedicated merge agent** (separate executor run)
   after approval — it is not executed by the generic quarantine helper.

Merge cards are labelled `type:merge` and processed individually; batching is
prohibited for merges.

---

## Phases

### Phase A — Playbook + prompt template, no engine changes (S: ~2–3 h)

**Scope:** Register the system project; write the CLAUDE.md playbook and the
`janitor_audit` prompt template. Operator can trigger an audit manually by sending
the prompt from the cockpit. Conductor + researcher agents work via existing
spec-017 machinery. No new Python code.

Deliverables:
- `$HOME/server-janitor/CLAUDE.md` — playbook: audit categories, verdict grammar,
  quarantine protocol, conductor instructions, merge-verdict protocol.
- `$HOME/server-janitor/TASKS.md` — empty board (Backlog / In Progress / Done).
- `data/prompts.json` entry `janitor_audit` — self-contained prompt for the conductor,
  listing all six researcher tasks with their exact bash discovery commands.
- `data/topics.json` entry for `server-janitor` with `cwd=$HOME/server-janitor`,
  `model=fable` (conductor), `log_cmd` pointing to audit output.
- No changes to `bot.py`, `webapp.py`, or any test file.

Acceptance (Phase A):
- Operator sends `/janitor` (or triggers prompt from cockpit) → Fable receives the
  `janitor_audit` prompt, delegates six researcher sub-agents, synthesises findings,
  and writes verdict cards to `server-janitor/TASKS.md`.
- At least one card per non-empty category from the 2026-06-10 inventory appears.
- No file is moved or deleted during the audit run.
- All verdict cards are in Backlog, not In Progress / Done.

### Phase B — Quarantine helper + Kanban integration (M: ~4–6 h)

**Scope:** A small executor helper (`server-janitor/janitor.py`) that the executor
sub-agent calls to perform quarantine moves. Kanban card execution via standard C2-gate
(spec-005) — operator moves card → `_run_card` → executor calls helper.

Deliverables:
- `$HOME/server-janitor/janitor.py` — CLI tool:
  - `janitor quarantine <path> --reason <verdict_text>` — moves to
    `~/.janitor-trash/<today>/`, appends to `manifest.json`.
  - `janitor purge --older-than <days>` — removes dated subdirs older than threshold,
    cleans manifest; logs to stdout.
  - `janitor list` — prints current quarantine manifest as a table.
  - No `rm` anywhere except inside `purge`.
- Cron entry for auto-purge (added to user crontab): `0 4 * * * janitor purge --older-than 30d >> ~/.janitor-trash/purge.log 2>&1`.
- `server-janitor/CLAUDE.md` updated: executor instructions for using `janitor.py`.
- Integration with C2-gate: executor card prompt includes the exact `janitor quarantine`
  command; the standard worktree + apply flow from spec-005 handles the board update.

Acceptance (Phase B):
- Operator approves a `quarantine` card → executor runs `janitor quarantine <path>`
  → file/directory appears in `~/.janitor-trash/<date>/` → `manifest.json` updated.
- Original location is empty after the move.
- Running `janitor list` shows the moved item with original path and reason.
- `janitor purge --older-than 0` (test mode) removes the quarantine entry and its
  directory; `manifest.json` entry removed.
- `janitor quarantine /some/nonexistent/path` exits non-zero with a clear error message.
- Auto-purge cron entry visible in `crontab -l` output.

### Phase C — Scheduled dry-run + cockpit Audit button (M: ~3–5 h)

**Scope:** Monthly scheduled audit that produces a report without any card creation
(pure observation). Audit button in the cockpit Project overview for `server-janitor`.

Deliverables:
- Claude Code scheduled job (`~/.claude/jobs`) or systemd timer: runs `janitor_audit`
  in `--dry-run` mode on the first of each month; sends a summary Telegram message to
  the operator with a link to the full report (written to
  `server-janitor/reports/audit-<date>.md`). **No cards created; no actions taken.**
- `janitor_dry_run` prompt variant in `data/prompts.json` — identical to
  `janitor_audit` but instructs the conductor to write a report file instead of board
  cards.
- Cockpit `ProjectView`: "Audit" button in the overview section for the
  `server-janitor` project (standard project action button, no new UI component needed
  if a generic "run prompt" button is already available from spec-017 Phase C).

Acceptance (Phase C):
- Scheduled dry-run fires → report written to `server-janitor/reports/audit-<date>.md`
  → Telegram message sent → no new TASKS.md cards.
- "Audit" button in cockpit triggers the same report-only flow interactively.
- Manual `/janitor` command still creates cards as before (Phase A/B behaviour
  unchanged).

---

## Test plan

Phase A and B do not touch `bot.py` / `webapp.py`, so the existing `pytest -q` baseline
(748 tests) is unaffected. Phase B adds unit tests for `janitor.py`:

- `test_janitor_quarantine_moves_file` — moves a temp file; asserts target exists,
  source gone, manifest entry present.
- `test_janitor_quarantine_moves_directory` — same for a directory tree.
- `test_janitor_quarantine_missing_path_exits_nonzero` — subprocess call; assert exit ≠ 0.
- `test_janitor_purge_removes_old_entries` — creates a manifest entry dated 31 days ago;
  `purge --older-than 30` removes it.
- `test_janitor_purge_keeps_recent_entries` — entry dated yesterday; `purge --older-than 30`
  leaves it.
- `test_janitor_list_output_includes_original_path` — quarantine an item; `list` output
  contains the original path string.
- `test_manifest_append_is_atomic` — two concurrent quarantine calls; both entries appear,
  no JSON corruption.

Phase C: no automated tests for the scheduled job; acceptance is manual (verify report
file created + TG message received).

---

## Risks

### Researcher sub-agent has Bash access
`researcher` (spec-017) can run arbitrary shell commands despite `Write`/`Edit` being
disallowed. An overzealous prompt could trigger destructive bash. **Mitigation:** the
`janitor_audit` prompt explicitly states "read-only discovery only; do not move, delete,
or modify any files." The playbook CLAUDE.md reinforces this with a dedicated rule.
Phase A acceptance requires verifying no mutations occur during the audit run.

### `cwd = $HOME` is very broad
Running a Fable session with `cwd = $HOME` means the agent has access to the entire
home tree, including secrets and credentials. **Mitigation:** the janitor project does
not expose secrets; `bypassPermissions` already applies project-wide. The researcher
sub-agents receive scoped `find` commands with explicit path filters; the playbook
forbids reading `~/.claude/`, `~/vault/_system/`, and `.env*` files.

### Quarantine fills disk before 30-day purge
If the operator approves many large items (e.g., all node_modules at once), the
quarantine directory could temporarily double disk usage. **Mitigation:** `janitor.py`
prints the size of the item before moving and warns if free disk space would drop below
a configurable threshold (default: 10 GB). Operator can also run `janitor purge
--older-than 0` to immediately clear already-approved items.

### Merge verdict complexity
Merging two projects with conflicting git history is non-trivial and failure-prone.
**Mitigation:** merge cards have a detailed checklist in their description; the merge
executor agent is separate and must be triggered explicitly. Failed merges leave both
directories intact; the merge card moves back to Backlog.

---

## Non-goals

- Automatic execution of any verdict without operator approval.
- Monitoring or continuous scanning (this is on-demand only until Phase C's monthly
  dry-run, which is also observation-only).
- Cleaning up Docker images or volumes directly — docker debris verdicts go through the
  same quarantine / approve flow (quarantine is not applicable to Docker objects;
  verdict for those is `remove` executed as `docker rmi` / `docker volume rm` after
  approve; the helper will support this in Phase B as a separate subcommand).
- Integration with Coolify cleanup (separate concern; Coolify manages its own registry).
- Automated project merges without human review of the migration plan.

---

## Related

- [[spec-017-fable-orchestrator]] — conductor + researcher sub-agents that power the
  audit fan-out. Phase A requires spec-017 Phase B to be deployed (sub-agent events
  forwarded so the operator can watch researcher progress).
- [[spec-005-c2-gate]] — worktree-based card execution that handles the approve →
  execute flow for quarantine cards.
- [[spec-010-self-healing]] — removed 2026-06-10; its removal is the direct motivation
  for janitor's strict no-autonomous-action design.
- [[spec-012-incidents-realtime-push]] — incidents pipeline that janitor does NOT
  extend; broken-cron detection belongs in spec-019, not here.
- [[spec-019-schedules-registry]] — companion spec; janitor may generate `keep` verdicts
  for crons that spec-019 marks as active, providing cross-validation.
