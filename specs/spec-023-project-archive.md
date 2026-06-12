---
created: 2026-06-11
updated: 2026-06-11
status: shipped
phases_shipped:
  - phase: 1
    description: "Backend: archived.json store + archive/unarchive/archived-list endpoints + filter in _collect_projects"
    shipped: 2026-06-11
  - phase: 2
    description: "Frontend: archive action + confirm modal + Archived section with Restore in Sidebar + i18n keys"
    shipped: 2026-06-11
card: ops:spec023
---

# Spec 023 — Project Archive (declutter cockpit, fully reversible)

## Goal

Let the operator hide a project from the active cockpit list to declutter, **without ever
touching the project's code on disk or its binding data**. Archiving is fully reversible:
archived projects live in an "Archived" section and restore with one click. This replaces
the never-shipped "delete project" backlog card with a safe, reversible affordance.

The operator accumulates many short-lived project dirs (scratch repos, `untitled-*`); the
active list gets noisy. Archive trims the list without losing anything.

---

## Hard safety invariant (non-negotiable)

**Archiving NEVER modifies the filesystem of the project.** No `rm`, no moving `cwd`, no
deleting `.claude-ops/`, memory, board, or git. Archive flips a visibility flag only. The
same goes for un-archive. There is deliberately **no hard-delete** of code anywhere in this
spec — that would be destructive and is out of scope (CLAUDE.md destructive discipline).

The TG topic (if any) is also left intact — archive is a **cockpit-visibility** concept,
not a teardown. (Deleting TG topics is explicitly out of scope.)

---

## Design

### Data model

Store archived state in a dedicated `data/archived.json` — a JSON list/set of archived
project ids (`_project_id(cwd)` = basename of cwd, the same id the API already uses).

- Rationale: keeps the binding store (`topics.json`, SLOY 1) clean and single-purpose;
  `data/` is already gitignored (chat ids/sessions/audit), so archive state stays local.
- **Verify against the actual project-discovery path** before committing to this: projects
  are assembled in `webapp.py` `_collect_projects` from the registry + `topics.json`. If a
  flag on the topics entry is genuinely cleaner given that code, the agent may use that
  instead — but `archived.json` is the recommended default. Either way, `/reset` must not
  affect archive state, and the existing `topics.json` hot-reload must not be broken.
- Load/save helpers mirror existing `data/` patterns (small read/write with try/except).

### Backend (webapp.py)

- `POST /api/projects/{id}/archive` → add id to archived set, persist. Returns
  `{"archived": true}`. If the project is currently **busy** (a turn is running for it),
  return **409** (consistent with the rotate endpoint's busy-guard) — archiving mid-run is
  confusing; require it idle.
- `POST /api/projects/{id}/unarchive` → remove id from archived set, persist. Returns
  `{"archived": false}`.
- `_collect_projects` (the default `GET /api/projects`) **filters out archived ids**.
- Archived projects are returned by either `GET /api/projects?archived=1` or a dedicated
  `GET /api/projects/archived` (agent picks the cleaner fit with existing routing) — id +
  name + cwd, enough to render the restore list.
- `_valid_project_id` / id validation reused — no path injection via id.

### Frontend (web/src)

- **Archive action** on a project (in the project view header or its Settings tab — place
  it next to other project-level actions; do not bury it). Click → confirmation modal
  (reuse `components/Modal.tsx`): *"Archive {name}? It will be hidden from the project
  list. Code, memory, board and history are kept — restore anytime from Archived."* →
  on confirm calls `POST .../archive`, then removes it from the active list / navigates away.
- **Archived section** in the project switcher / list: a collapsed "Archived ({n})" group
  (or a toggle) listing archived projects, each with a **"Restore"** button → `POST
  .../unarchive` → project returns to the active list.
- If the currently-open project is archived, route the user back to the project list.
- Busy-archive (409) → inline toast: *"Can't archive while a task is running."*

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: `archived.json` store + archive/unarchive endpoints + filter in `_collect_projects` + archived list endpoint | pending |
| 2 | Frontend: archive action + confirm modal + Archived section with Restore | pending |

---

## Acceptance

- [ ] `POST /api/projects/{id}/archive` adds the id to the archived store and persists it.
- [ ] After archive, the project is absent from default `GET /api/projects`.
- [ ] The archived project is returned by the archived-list endpoint (id, name, cwd).
- [ ] `POST /api/projects/{id}/unarchive` returns it to the default list.
- [ ] Archiving a **busy** project → 409; the project stays active.
- [ ] **No filesystem change** to the project dir on archive or unarchive (assert no writes
      under `cwd`; only `data/archived.json` changes).
- [ ] `topics.json` binding (project/cwd/model) is unchanged through archive→unarchive.
- [ ] Project memory, board (TASKS.md), and secrets are intact after archive→unarchive.
- [ ] `/reset` does not change archive state; `topics.json` hot-reload still works.
- [ ] UI: archive action shows a confirm modal; Archived section lists with working Restore.

---

## Tests

`tests/test_project_archive.py` (backend):
- archive adds id + filtered from default list; unarchive restores.
- archived-list endpoint returns archived only.
- busy project → 409.
- archive/unarchive touch only `data/archived.json` — no writes under the project cwd.
- invalid/unknown id → 404/400 (no path injection).
- archive state survives a simulated `topics.json` reload / `/reset` (no interference).

Frontend: extend existing list/switcher tests if a harness exists; otherwise document a
manual check (archive → gone from list → appears under Archived → Restore → back).

---

## Constraints (do not violate)

- **Never touch the project's filesystem** — flag-only. No hard-delete anywhere.
- **English-only** code/comments/UI strings.
- Don't break `topics.json` hot-reload, the layer-1/layer-2 split, or `/reset` semantics.
- Reuse existing id validation, Modal component, toast pattern, and `data/` save idioms.
- TG topic left intact (cockpit-visibility only).

---

## Non-goals

- Hard delete of project code / `cwd` (deliberately excluded — destructive).
- Deleting the Telegram topic.
- Bulk archive / auto-archive-by-age (could be a later card).
- Archiving carrying any data migration — it's a pure visibility flag.

---

## Related

- Replaces the never-shipped "delete project" backlog card (design-only, never built).
- topics.json (SLOY 1) binding + `_collect_projects` discovery (CLAUDE.md "Что где").
- CLAUDE.md destructive-operation discipline (why archive, not delete).
