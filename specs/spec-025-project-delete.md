---
created: 2026-06-11
updated: 2026-06-11
phases_shipped: all (1-4, 2026-06-11)
status: shipped
phases_shipped:
card: ops:spec025
depends_on: spec-023 (archive), spec-018 (janitor)
---

# Spec 025 — Project Hard-Delete (trash + grace period, guard-railed)

## Goal

Let the operator **permanently remove a project** — its folder on disk and all related
cockpit state and its Telegram topic — from the cockpit. Because this is the single most
destructive action in the system (`rm`-ing user data), it is built as **delete-to-trash
with a 7-day grace period** (recoverable within the window; the server-janitor purges it
for real afterward), wrapped in mandatory guardrails so the wrong thing can never be
deleted.

Operator decisions (2026-06-11): **mechanism = trash + grace** (not immediate rm);
**Telegram topic = also deleted**.

---

## Threat model — why the guardrails exist (READ THIS)

A project's `cwd` can be **any path**. A bad id→path resolution, a symlink, or a project
whose `cwd` is `$HOME` / an ancestor of `claude-ops-bot` would let a single click delete
the wrong tree — including the bot itself or the entire home directory. Unpushed/uncommitted
work has no remote copy and is lost forever. Every guardrail below is non-negotiable.

---

## Guardrails (ALL mandatory)

1. **Archive-first.** Delete is only offered for **already-archived** projects, from the
   "Archived" section. You cannot delete an active project. Backend rejects delete of a
   non-archived id → 409.
2. **Type-to-confirm.** The confirm modal requires typing the **exact project name**. The
   `POST` body must carry `confirm_name`; backend returns 400 if it doesn't match the
   project's name (defense in depth, not just FE).
3. **Path allowlist (the critical one).** Resolve `real = realpath(cwd)` and
   `home = realpath(~)`. Proceed ONLY if ALL hold, else **400, delete nothing**:
   - `real` is strictly under `home` (`real.startswith(home + os.sep)`) and `real != home`;
   - `real` is **not** the claude-ops-bot directory and **not an ancestor** of it
     (`claude_ops_dir` must not be `real` nor start with `real + os.sep`);
   - depth ≥ 1 below home (no `~/x` that's actually a mount root; require a real subdir);
   - the realpath resolution didn't escape `home` via symlink (compare resolved vs raw).
4. **Not busy.** A running turn for the project → **409**. (Also refuse if a card worktree
   run is active for that cwd.)
5. **Unpushed-work warning (pre-delete check).** `GET /api/projects/{id}/delete-precheck`
   returns `{is_git, uncommitted_count, unpushed_count, branch, has_remote}`. The modal
   surfaces a red warning if `uncommitted_count > 0` or `unpushed_count > 0`
   ("N files / M commits will be lost forever"). Non-git dir → warn "not a git repo —
   nothing is backed up." This **informs**, does not block.
6. **Audit.** Write a `DELETE⚠️` audit line: project id, original cwd, trash path, timestamp.

---

## Design

### Delete flow (order matters — reversible core first)

`POST /api/projects/{id}/delete` body `{confirm_name}`:
1. Run all guardrails (archived? name match? path allowlist? not busy?). Any fail → 4xx,
   touch nothing.
2. **Move** `cwd` → `data/trash/<id>-<unixts>/` via `shutil.move` (atomic rename on same
   filesystem; cross-device falls back to copy+unlink — note the slow path in a comment).
   Write a sidecar `data/trash/<id>-<unixts>.json` = `{id, name, original_cwd, deleted_at,
   tg_chat, tg_thread}` for listing/restore/purge.
3. Clean cockpit state: remove the `topics.json` binding(s) for that project, the
   `sessions.json` entry, `archived.json` entry, `project_groups.json` assignment, and the
   `data/timeline/<slug>.jsonl` file. Each wrapped in try/except — partial-cleanup failure
   must not abort an already-moved folder (log and continue).
4. **Delete the Telegram topic**: via bot `deleteForumTopic(chat_id, message_thread_id)`
   for the bound topic. This is the one irreversible step (a deleted TG topic can't be
   restored even within the grace window) — done last, try/except, logged. (Implement in
   `bot.py`; webapp triggers it through `ctx` like other bot calls, or webapp calls the
   Telegram HTTP API directly — agent picks the consistent path. Do NOT make webapp import
   bot.py.)
5. Return `{deleted:true, trash_path, purge_at}`.

### Trash + janitor purge (ties into spec-018)

- `TRASH_DIR` default `data/trash` (gitignored). `TRASH_RETENTION_DAYS` default **7**.
- The **server-janitor (spec-018)** gets a new sweep: scan `data/trash/*.json`, and for any
  whose `deleted_at` is older than `TRASH_RETENTION_DAYS`, `rm -rf` the folder + sidecar
  (this is the only real `rm` — on a path already proven to be under `data/trash`, so it's
  safe). Log each purge. If the janitor has a fixed cadence, this rides it; otherwise add a
  lightweight periodic task.

### Restore (within grace)

- `GET /api/trash` → list trashed projects (id, name, original_cwd, deleted_at, days_left).
- `POST /api/trash/{entry}/restore` → if `original_cwd` is free (not re-occupied), `shutil.move`
  the folder back and re-add the `topics.json` binding. **Caveat (state honestly in UI):**
  files + cockpit binding are restored, but the **Telegram topic is NOT** (it was deleted) —
  the operator re-creates it with `/newtopic` if needed. Path collision → 409 with message.

### Frontend (web/src)

- In the **Archived section** (spec-023), each project gets a **"Delete permanently…"**
  action (danger styling, clearly separated from "Restore").
- Click → opens the delete modal: calls the precheck endpoint, shows the unpushed/uncommitted
  warning, shows "moved to trash, auto-purged in 7 days, recoverable until then (topic not
  recoverable)", and a **type-the-name** field that gates the confirm button.
- A **Trash** view (small, e.g. below Archived or in settings) listing trashed projects with
  days-left and a Restore button.
- Danger toasts for 400/409 (name mismatch, path rejected, busy, collision).

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: guardrails + delete→trash + cockpit-state cleanup + precheck endpoint | shipped 2026-06-11 |
| 2 | Backend: TG topic deletion + trash list/restore endpoints | shipped 2026-06-11 |
| 3 | Janitor (spec-018) trash-purge sweep with `TRASH_RETENTION_DAYS` | shipped 2026-06-11 |
| 4 | Frontend: delete modal (precheck warning + type-to-confirm) + Trash view + restore | shipped 2026-06-11 |

---

## Acceptance

- [ ] Delete of a **non-archived** project → 409 (archive-first enforced).
- [ ] `confirm_name` mismatch → 400, nothing deleted.
- [ ] Path-allowlist rejects: `cwd == ~`, `cwd` == claude-ops-bot dir or an ancestor of it,
      symlink escaping `~`, shallow/root paths → 400, **folder untouched**.
- [ ] Busy project (or active card run on that cwd) → 409.
- [ ] On success, `cwd` is moved under `data/trash/`, original path no longer exists, and a
      sidecar json records `original_cwd` + `deleted_at`.
- [ ] `topics.json` / `sessions.json` / `archived.json` / `project_groups.json` /
      `timeline/<slug>.jsonl` entries for the project are removed.
- [ ] The bound Telegram topic is deleted (deleteForumTopic called).
- [ ] Partial cleanup failure (e.g. timeline file missing) does not leave the operation
      half-aborted — folder still trashed, error logged.
- [ ] `delete-precheck` reports uncommitted/unpushed counts (or non-git).
- [ ] Janitor purges trash entries older than `TRASH_RETENTION_DAYS`; newer ones survive.
      Purge only ever `rm`s paths under `data/trash`.
- [ ] `GET /api/trash` lists; `POST /api/trash/{entry}/restore` moves folder back + rebinds;
      path collision → 409.
- [ ] A `DELETE⚠️` audit line is written with id + cwd + trash path.
- [ ] UI: delete lives in Archived, shows the warning, type-to-confirm gates the button.

---

## Tests

`tests/test_project_delete.py` (backend) — **heavy on the guardrails**:
- non-archived → 409; name mismatch → 400.
- path allowlist: build fixtures where cwd == home, cwd == bot dir, cwd is symlink escaping
  home, shallow path → each 400 and assert the fixture dir still exists (NOT deleted).
- happy path: archived project with cwd under a temp "home" → moved to trash, sidecar
  written, original gone, cockpit state cleaned. Use tmp dirs only — never touch real `~`.
- busy → 409.
- precheck: git repo with dirty/unpushed state → correct counts; non-git → flagged.
- janitor purge: sidecar older than retention → purged; newer → kept; purge refuses any
  path not under `data/trash`.
- restore: moves back + rebinds; collision → 409.
- Mock the TG topic deletion (no real Telegram call in tests).

Run the full suite green; do not regress the ~892+ existing tests.

---

## Constraints (do not violate)

- **The ONLY real `rm -rf` is the janitor purge, and only on paths under `data/trash`.**
  The delete endpoint itself never `rm`s — it `move`s. Belt and suspenders.
- Webapp must **not import bot.py**; TG topic deletion goes through `ctx`/bot or a direct
  Telegram HTTP call.
- **English-only** code/comments/UI strings.
- Don't break spec-023 archive, spec-024 groups, rotation, or cost UI.
- Tests must use temp directories exclusively — never operate on the real home or repo.

---

## Non-goals

- Deleting the GitHub remote (out of scope — that lives off-server).
- Recovering a deleted Telegram topic (not possible; operator re-creates it).
- Configurable per-project retention (single global `TRASH_RETENTION_DAYS`).

---

## Related

- Spec 023 — Archive (delete is offered only for archived projects, from its UI).
- Spec 024 — Groups (clean the group assignment on delete).
- Spec 018 — Server janitor (hosts the trash-purge sweep).
- CLAUDE.md — destructive-operation discipline; `topics.json`/`sessions.json`/`timeline` layout.
