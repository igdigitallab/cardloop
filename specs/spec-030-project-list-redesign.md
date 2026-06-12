---
created: 2026-06-12
status: draft
phases_shipped: none
card: ops:dc5ee0
---

# Spec 030 — Project List Redesign (Obsidian-style groups)

## Goal

Make the cockpit project list (`web/src/components/Sidebar.tsx`) clean and convenient:
kill the per-project `⋮` kebab menu, move group assignment to **drag-and-drop** (Obsidian
style), and put all actions behind **right-click context menus** (project + group). Add a
proper **＋ New group** button (no more `window.prompt`), inline group rename, collapsible
groups (already exist), and group reordering. Archive stays reachable from the project context
menu AND the project Settings tab.

Source: board card `ops:dc5ee0`. Builds on the existing group backend (`project_groups.json`).

---

## Current state (build on it, don't rewrite)

- **Backend groups exist:** `project_groups.json` = `{groups: [name…], assignments: {pid: group}}`.
  - `GET /api/project-groups` → `{groups, assignments}` (keep).
  - `POST /api/projects/{id}/group` body `{group: string|null}` → assign/unassign; auto-adds a
    new group name (keep — used by drag-to-group + context-menu move).
  - `POST /api/project-groups/manage` body `{groups: [...]}` → full-list replace; unassigns
    projects of dropped groups; list order = group order. **Gap: a list-edit rename loses
    assignments** (old name dropped → its projects unassigned).
- **Frontend (`Sidebar.tsx`):** collapsible groups (▶/▼, localStorage `cops.group.collapsed.*`)
  already work. Pointer-events drag exists but only **reorders the flat list**, never assigns to
  a group. Group create/manage is via `window.prompt` (ugly). Per-project `⋮` kebab holds
  "Move to group ▶" + "Archive". Archived + Trash sections (collapse, restore, hard-delete with
  git precheck) already work and stay.
- The **"точки" to remove = the `⋮` kebab** menu. `HealthDot` (status circle, left of name) stays.

---

## Design — interactions

| Action | Gesture | Effect |
|---|---|---|
| Open project | left click | select/open (unchanged) |
| Assign to group | **drag** project onto a group header/body | `POST /projects/{id}/group {group}` |
| Remove from group | **drag** onto the *Ungrouped* zone | `POST /projects/{id}/group {group:null}` |
| Reorder | drag between projects in same zone | existing `onReorder` |
| Reorder groups | drag a group header | `POST /project-groups/reorder` |
| Project menu | **right-click** (long-press on touch) | Open · Move to group ▶ · Remove from group · ⚙ Settings · 🗄 Archive |
| Group menu | **right-click** on group header | ✏ Rename (inline) · ➕ New project in group · Collapse/expand · 🗑 Delete group |

- **Drag UX:** highlight the drop-target group (tinted bg) + insertion line for position. Pure
  pointer-events (mouse + touch), extending the existing handlers. Drop target type is detected
  from the element under the pointer (`data-group` on group zones, `data-project-id` on items,
  a dedicated `data-ungrouped-zone`).
- **Custom context menu:** own styled menu (not the browser default — `preventDefault` on
  `contextmenu`), positioned at the cursor, dismissed on outside-click / Escape. Long-press
  (~500 ms) opens it on touch.
- **Delete group** → confirm modal → its projects become ungrouped (never deletes projects).
- **Rename group** → inline editable label (Enter confirm, Esc cancel), not `window.prompt`.

## Design — layout

```
＋ New project   ＋ New group        ← both buttons at top, next to each other
▼ Work            3
  🟢 rightforms
  🟡 khronika-portal
▶ Personal        2                  ← collapsed
▼ Experiments     0
  ⌁ drop projects here               ← empty group = drop hint
── Ungrouped ──                      ← drop here = leave group
  🟢 claude-ops-bot
  🏠 free chat
▶ 🗄 Archived     4
▶ 🗑 Trash (7d)   1
```

- **＋ New group** creates an empty group with an inline-editable name (auto-focus, Enter/Esc).
- Empty groups render with a muted "drop projects here" hint (so a fresh group is a visible
  drop target).
- **Decisions (accepted 2026-06-12):** Ungrouped sits **below** the groups · `HealthDot` **kept**
  · context menu is **custom-styled** + long-press on touch.

---

## Backend — REST contract (the frontend codes against this exactly)

Keep: `GET /api/project-groups`, `POST /api/projects/{id}/group`.
Add (each returns the full updated `{groups, assignments}` so the client refreshes from the response):

| Method · Path | Body | Behaviour |
|---|---|---|
| `POST /api/project-groups/create` | `{name}` | append an **empty** group (idempotent if it exists); trim; reject empty |
| `POST /api/project-groups/rename` | `{from, to}` | rename in `groups` **and remap every assignment** `from→to`; reject if `from` missing or `to` empty/collides |
| `POST /api/project-groups/delete` | `{name}` | remove from `groups` + **unassign** its projects (become ungrouped) |
| `POST /api/project-groups/reorder` | `{order: [...]}` | set group order; `order` must be a permutation of the current groups (reject otherwise) |

- `rename` is the genuinely new logic; `create/delete/reorder` may reuse the existing
  `_load_groups`/`_save_groups` + manage logic but as explicit atomic endpoints.
- Validation mirrors existing handlers (non-empty trimmed strings; JSON errors → 400).
- Concurrency: single-operator cockpit — last-write-wins on `project_groups.json` is acceptable
  (same as today's `manage`).

---

## Phases

| Phase | Description | Status |
|---|---|---|
| 1 | Backend: `create` / `rename` (remap) / `delete` (unassign) / `reorder` endpoints + tests | planned |
| 2 | Frontend: remove kebab; drag-to-group + ungrouped drop zone; group reorder | planned |
| 3 | Frontend: custom right-click context menus (project + group) + long-press touch | planned |
| 4 | Frontend: ＋ New group button + inline rename; empty-group drop hint | planned |
| 5 | Archive entry in project Settings tab (in addition to context menu) | planned |

Phases 2–4 land together in one `Sidebar.tsx` pass; Phase 1 is parallel (backend). Phase 5 is a
small separate touch (Settings tab).

## Acceptance

- [ ] No `⋮` kebab on project items; all actions via right-click / drag / Settings.
- [ ] Dragging a project onto a group assigns it; onto Ungrouped removes it; visible drop target.
- [ ] Right-click project → context menu (Open / Move to group / Remove / Settings / Archive).
- [ ] Right-click group → Rename (inline) / New project / Collapse / Delete (→ projects ungrouped).
- [ ] ＋ New group creates an empty, inline-named group; groups reorderable; collapse persists.
- [ ] Archive reachable from project context menu and Settings tab; archived/trash sections intact.
- [ ] Backend: create/rename(remap)/delete(unassign)/reorder endpoints + tests green.
- [ ] `venv/bin/python -m pytest tests/` green; `cd web && npm run build` clean.

## Non-goals
- Nested groups / sub-groups (flat groups only).
- Per-group color theming (optional future polish).
- Multi-select drag (one project at a time).

## Related
- Card `ops:dc5ee0`. Existing group backend in `webapp.py` (`_load_groups`/`api_project_group_set`/
  `api_project_groups_manage`). Spec-025 (archive/trash/hard-delete) — untouched, stays.
