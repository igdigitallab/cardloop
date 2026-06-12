---
created: 2026-06-11
updated: 2026-06-11
status: shipped
phases_shipped:
  - phase: 1
    description: "Backend: project_groups.json store + /api/project-groups + /api/projects/{id}/group endpoints + group field on _collect_projects"
    shipped: 2026-06-11
  - phase: 2
    description: "Frontend: collapsible group sections + per-project assign menu + manage groups + Ungrouped section + i18n keys"
    shipped: 2026-06-11
card: ops:spec024
---

# Spec 024 — Project Groups (virtual navigation folders, no disk change)

## Goal

Organize the cockpit project list into named, collapsible **groups** for navigation —
purely a UI/organizational layer. **No filesystem change**: folders on disk are never
moved or renamed. A project belongs to at most one group; ungrouped projects appear under
a default heading. This is the navigation companion to spec-023 (archive): together they
turn a flat, noisy project list into an organized, tidy one without touching any code.

Built together with spec-023 by a single agent — both features live in the same project
switcher / `_collect_projects` surface, so one owner designs the list layout once
(group sections + an "Archived" section at the bottom).

---

## Hard invariant (non-negotiable)

**Grouping NEVER touches the filesystem.** No moving `cwd`, no renaming dirs, no symlinks.
A group is a label stored in cockpit config only. Removing/renaming a group never affects
the project's code, memory, board, binding, or disk path.

---

## Design

### Data model

`data/project_groups.json` (gitignored, mirrors `data/` idioms):

```json
{
  "groups": ["Khronika", "IGGO", "Personal"],      // ordered list of group labels
  "assignments": { "<project_id>": "Khronika" }    // project_id -> group label
}
```

- `project_id` = `_project_id(cwd)` (basename) — same id the API already uses everywhere.
- `groups` is an explicit ordered list so groups can exist before any project is assigned
  and so section order is stable/user-controlled.
- A project whose id is not in `assignments` (or whose label is not in `groups`) →
  rendered under **"Ungrouped"** (implicit, not stored).
- Load/save with try/except like other `data/` stores; missing file → empty structure.

### Backend (webapp.py)

- `GET /api/project-groups` → `{groups:[...], assignments:{...}}`.
- `POST /api/projects/{id}/group` body `{"group": "<label>" | null}` → assign a project to
  a group (auto-creates the label in `groups` if new) or clear it (`null` → ungrouped).
- `POST /api/project-groups` body `{groups:[...]}` → manage the group list: create, rename
  (agent decides representation — simplest: client sends the full desired ordered list;
  renames map old→new and update `assignments`; deletes drop the label and unassign its
  members to Ungrouped). Reorder = order of the array.
- `GET /api/projects` (`_collect_projects`) includes each project's `group` label (or null).
  The FE can render purely from the project list + the groups endpoint for order.
- Validate labels: trim, non-empty, reasonable length cap, no control chars. `project_id`
  reuses existing validation (no path injection).

### Frontend (web/src)

Project switcher / list:
- Render **collapsible group sections** in `groups` order; **"Ungrouped"** section for the
  rest; **"Archived"** section (from spec-023) last.
- Collapse/expand state persisted in `localStorage` per group label.
- **Assign a project to a group**: a small menu on each project ("Move to group →") listing
  existing groups + "New group…" (prompt for a label) + "Remove from group". Calls
  `POST /api/projects/{id}/group`.
- **Manage groups** (light, can be a small section in settings or a menu): rename, reorder
  (up/down or drag — up/down is fine for v1), delete (members fall back to Ungrouped).
- Empty groups are allowed (a created group with no members still shows, collapsed).

Keep v1 pragmatic: assign + auto-create-on-new-label + collapsible sections + persisted
collapse state are the must-haves; rename/reorder/delete-group are cheap and included but
secondary — don't gold-plate (no drag-drop if it balloons scope).

---

## Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Backend: `project_groups.json` store + groups/assign endpoints + `group` on project list | pending |
| 2 | Frontend: collapsible group sections + assign menu + manage (rename/reorder/delete) | pending |

---

## Acceptance

- [ ] `POST /api/projects/{id}/group {"group":"X"}` assigns; auto-creates "X" if new.
- [ ] `POST /api/projects/{id}/group {"group":null}` clears assignment → project Ungrouped.
- [ ] `GET /api/project-groups` returns ordered groups + assignments.
- [ ] `GET /api/projects` exposes each project's `group` (or null).
- [ ] Reordering groups changes section order; renaming a group preserves its members;
      deleting a group moves its members to Ungrouped.
- [ ] **No filesystem change** to any project dir on any group operation (assert only
      `data/project_groups.json` changes).
- [ ] Switcher renders collapsible group sections + Ungrouped + Archived (spec-023).
- [ ] Collapse/expand state persists across reloads (localStorage).
- [ ] Invalid/empty label → 400; unknown project id → 404.

---

## Tests

`tests/test_project_groups.py` (backend):
- assign / auto-create / clear; groups endpoint shape.
- rename preserves members; delete → members ungrouped; reorder persists.
- group ops touch only `data/project_groups.json` — no writes under any project cwd.
- label/id validation (empty, control chars, path-injection id → 400/404).

Frontend: extend switcher test if a harness exists; else document manual check
(assign → appears under group → collapse persists → rename/reorder/delete behaves).

---

## Constraints (do not violate)

- **Never touch any project's filesystem** — labels only.
- **English-only** code/comments/UI strings.
- Reuse existing id validation, `data/` save idioms, Modal/menu/toast patterns.
- Don't break `topics.json` layers, hot-reload, `/reset`, or the archive feature (023).
- Coexists with archive: archived projects are hidden from group sections and shown only
  under "Archived"; group assignment is preserved through archive→unarchive.

---

## Non-goals

- Moving/renaming folders on disk (explicitly excluded).
- Nested groups / sub-sub-groups (one level only in v1).
- Multi-group membership (a project is in at most one group).
- Auto-grouping by path/heuristics (could be a later card).

---

## Related

- Spec 023 — Project archive (same project-switcher surface; build together).
- topics.json (SLOY 1) + `_collect_projects` discovery (CLAUDE.md "Что где").
