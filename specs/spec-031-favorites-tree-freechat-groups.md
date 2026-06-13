---
created: 2026-06-12
status: draft
phases_shipped: none
builds_on: spec-030
---

# Spec 031 — Favorites, tree-indented groups, free-chats in groups

Polish pass on top of spec-030 (Obsidian-style project list). Three additions, all in
`web/src/components/Sidebar.tsx` + `web/src/styles/sidebar.css` + a small backend touch in
`webapp.py`. Keep everything spec-030 already does (drag-to-group, right-click context menus,
inline rename, archive/trash, collapsible groups).

## Goals

1. **Tree look.** Projects inside a group are visually indented to the right under their group
   header, with a subtle vertical guide line — like a file tree / Obsidian folder.
2. **Favorites (⭐).** A project (or free chat) can be starred. Starred items show in a
   collapsible **⭐ Favorites** section pinned at the very top of the list — AND they ALSO stay
   in their normal group/ungrouped position (dual display, not moved out of the group). A small
   ⭐ marks starred rows in their group too.
3. **Free chats in groups.** Free chats currently render in a flat list at the bottom and cannot
   be grouped. Keep showing them, but allow assigning a free chat to a group (drag + context
   menu), same as a project. Ungrouped free chats keep their current dedicated spot.

---

## Backend (`webapp.py`)

### Favorites store (new, mirrors the groups store)
- New file `data/project_favorites.json` = `{"favorites": ["<pid>", ...]}` (list of project ids;
  for free chats the id is the `free-…` id). Helpers `_load_favorites(ctx)` /
  `_save_favorites(ctx, data)` next to `_load_groups`/`_save_groups`. `_load_favorites` returns
  `{"favorites": []}` on missing/corrupt file.
- In `_collect_projects` add `"favorite": pid in fav_set` to **every** project dict — both the
  real-project branch (~line 921) and the free-chat branch (~line 941). Load the set once at the
  top of the function.
- New endpoint `POST /api/projects/{id}/favorite` body `{favorite: bool}`:
  - 404 if `_find_project_by_id` is None; 400 on bad json / non-bool.
  - Add/remove `pid` in the favorites list; `_save_favorites`; return `{"ok": True}`.
  - Register the route next to the other `/api/projects/{id}/...` routes (~line 8428).

### Free-chat group field (bug fix)
- In `_collect_projects`, the free-chat branch hardcodes `"group": None` (~line 942). Change it
  to read the assignment like real projects do:
  `assignments.get(fid) if assignments.get(fid) in valid_groups else None`.
  (`api_project_group_set` already resolves free chats via `_find_project_by_id`, so assignment
  already persists — only the read-back was broken.)

### Tests
- Add unit tests mirroring the existing group tests (no aiohttp_client): favorite toggle
  round-trips through the store and surfaces as `favorite: True` in `_collect_projects`; a free
  chat assigned to a group surfaces with that `group`. Keep `venv/bin/python -m pytest tests/`
  green. (Run tests via **`venv/bin/python -m pytest tests/`** — system python lacks
  pytest-aiohttp and gives false errors.)

---

## Frontend

### types.ts
- Add `favorite?: boolean` to `Project`.

### api.ts
- Add `setFavorite(id: string, favorite: boolean)` → `POST /api/projects/{id}/favorite`.

### Sidebar.tsx

**Favorites section (top):**
- Derive `const favorites = projects.filter(p => p.favorite && p.name.matches(search))`.
- Render a collapsible section **above the named groups**, only when `!hasSearch` and
  `favorites.length > 0`. Header: `⭐ Favorites` + count, same markup/behaviour as a group header
  but it is NOT a drop target and NOT draggable and NOT renamable/deletable (it is virtual).
  Collapse state in localStorage key `cops.group.collapsed.__favorites__`.
- Render its rows with the existing `renderProjectItem` (so free chats keep 🏠 + delete there too).
- Items here are NOT indented as a tree (favorites is a flat pinned list).

**Favorite toggle:**
- In BOTH the project and free-chat context menus add an item:
  `⭐ Add to favorites` / `☆ Remove from favorites` (toggle based on `proj.favorite`).
  On click: `await api.setFavorite(id, !proj.favorite); onProjectsReload?.()`.
- Add a small star affordance on each row: a `⭐`/`☆` button that appears on hover (and is
  always visible if already starred), click toggles favorite. Put it before the unread/incident
  badges; `stopPropagation` on pointer/click so it doesn't start a drag or select. Starred rows
  show a persistent ⭐ even when not hovered.

**Tree indentation:**
- Wrap a group's child project items in a `<div className="sidebar-group-body">` container
  (replacing the current bare `groupProjects.map(...)` + drop-hint). The container carries the
  indent + vertical guide line via CSS. Do the same wrapper for the ungrouped drop zone if it
  helps consistency, but ungrouped/favorites items stay at base indent (only *grouped* children
  are indented).

**Free chats in groups:**
- Today: `filtered = nonFreeProjects` and grouping iterates only `filtered`; free chats render
  separately at the bottom. Change so grouping considers **all** projects (real + free):
  - Build `grouped`/`ungrouped` over `projects.filter(name matches search)` (both kinds).
  - A free chat WITH a valid group renders inside that group (indented, with 🏠 + delete btn).
  - Free chats WITHOUT a group keep rendering in their current dedicated bottom spot
    (the `filteredFree.filter(p => !p.group)` list) so nothing regresses.
- Enable the context menu for free chats: `renderProjectItem`'s `onContextMenu` currently bails
  with `if (!p.is_free)`. Allow it for free chats too, but open a **free-chat-flavored** menu:
  Open · Move to group ▶ · Remove from group (if grouped) · ⭐ favorite toggle · separator ·
  🗑 Delete free chat (→ existing `setConfirmDelete`). Do NOT show Archive or ⚙ Settings for
  free chats. (Drag already works for free chats — the generic pointer handlers fire regardless
  of `is_free`; only the menu was gated.)

### sidebar.css
- `.sidebar-group-body` — `margin-left: 9px; padding-left: 9px; border-left: 1px solid var(--border)`
  (a thin tree guide). Tune so child `.project-item`s sit ~16–18px right of the group toggle and
  the guide line reads as a tree branch. On group hover the guide can brighten slightly. Keep the
  empty-group drop hint inside the body so a fresh group still shows the indented "drop here".
- `.sidebar-favorites-section` header may reuse `.sidebar-group-header` styling.
- Star button `.fav-star-btn` — muted/transparent until hover or starred; ⭐ gold-ish when active,
  ☆ faint when not. No layout shift between states.

---

## Acceptance
- [ ] Projects under a group are indented with a visible vertical tree guide; ungrouped &
      favorites rows are not indented.
- [ ] Starring a project/free chat puts it in a top **⭐ Favorites** section while it also stays
      in its group; unstarring removes it from Favorites. Star toggle works from row hover-button
      and from the context menu. Persists across reload (backend store).
- [ ] A free chat can be dragged onto a group and via context-menu "Move to group", renders
      inside that group, and the assignment survives reload. Ungrouped free chats still appear.
- [ ] Free-chat context menu has favorite + Move to group + Remove from group + Delete free chat,
      and no Archive/Settings.
- [ ] `venv/bin/python -m pytest tests/` green; `cd web && npm run build` clean (no TS errors).

## Non-goals
- Reordering inside the Favorites section. Nested groups. Per-group color. Favoriting archived/
  trashed items.
