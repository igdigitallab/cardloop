---
created: 2026-06-12
status: draft
phases_shipped: none
---

# Spec 032 — Move "Failed" out of the column row into a collapsible tray

## Goal
On the project board (`web/src/tabs/BoardTab.tsx`), the **Failed** column clutters the working
columns. Pull it out of the `backlog · in_progress · review · failed` row and render it as a
**separate collapsible "Failed" tray pinned at the bottom of the board**, per project. Default
**collapsed**. All existing failed-card actions (retry/run, move to backlog/review, edit,
description, delete, batch-select) keep working, and drag still moves cards into/out of Failed.

Frontend-only. No backend change.

## Current state
- `ORDER = ['backlog', 'in_progress', 'review', 'failed']`; columns render side by side from
  `visibleOrder = ORDER.filter(k => visibleCols.has(k))`. `DEFAULT_VISIBLE = ['backlog']`.
- A `board-col-toggles` row lets the user show/hide each column (incl. `failed`).
- `PARK_ORDER = ['backlog', 'review', 'failed']` drives the per-card move arrows (in_progress has
  no arrows). `canShowResult = key === 'review' || key === 'failed'`.
- Each card is a large JSX block inside the `col.cards.map(...)` loop (incl. incident ⚠ icon,
  checkbox, queued badge, inline edit, description btn, move arrows, run/retry, delete).

## Changes

1. **Drop `failed` from the column row.** The columns row renders only
   `['backlog', 'in_progress', 'review']` (still subject to the existing visibility toggle). The
   `board-col-toggles` row also lists only those three (Failed is no longer a toggleable column —
   it has its own tray collapse).

2. **Refactor: extract a `renderCard` helper.** Factor the per-card JSX out of the columns loop
   into one local function `renderCard(card, { columnKey, parkIdx, isInProgress, canShowResult })`
   so the SAME markup/handlers are reused by both the columns and the tray (no duplication). The
   columns loop calls it; the tray calls it with `columnKey: 'failed'`,
   `parkIdx: PARK_ORDER.indexOf('failed')`, `isInProgress: false`, `canShowResult: true`.

3. **Failed tray.** Below `.board-columns`, render a `.board-failed-tray` ONLY when the failed
   column has ≥1 card:
   - Collapsible header: `▶/▼ 🔴 {failed label} ({count})`. Default **collapsed**. Persist collapse
     in localStorage key `cops.board.failedCollapsed` (default `true` / collapsed).
   - When expanded: render the failed column's cards via `renderCard(...)`. Lay them out as a
     horizontal wrap/grid (full board width), not a single tall narrow column.
   - The tray body is a **drop target**: dragging any card onto it moves that card to `failed`
     (`onDragOver` preventDefault + `dropEffect='move'`; `onDrop` → `move(dragCardId, 'failed')`),
     mirroring the column drop handler. Failed cards stay `draggable` so they can be dragged back
     to a visible column. Keep the drag-over highlight class behaviour consistent with columns.

4. **Styling (`web/src/styles/board.css`).** `.board-failed-tray` — full width, sits under the
   columns with a top divider; muted/desaturated red accent (NOT the loud alarm red) so it reads
   as "parked failures", not a blaring alert. Collapsed header is a slim bar. Expanded cards wrap
   horizontally. Reuse `.board-card` styling; you may tone down `.board-card-incident` redness
   inside the tray if it looks alarming. No layout shift between collapsed/expanded beyond the
   tray's own height.

5. **i18n.** Any new user-facing string (e.g. tray header if not reusing the backend `failed`
   column label) goes in BOTH `web/src/i18n/en.ts` and `ru.ts` under the `board.*` namespace.
   English-only in code/components.

## Acceptance
- [ ] Columns row shows only Backlog / In progress / Review; Failed is gone from that row and from
      the column-toggle bar.
- [ ] A `🔴 Failed (N)` tray appears at the bottom only when N>0, collapsed by default, toggles
      open/closed, and the collapse state persists across reloads.
- [ ] Expanded tray shows failed cards with ALL existing actions working (retry/run, move arrows
      to backlog/review, edit, description, delete, batch checkbox).
- [ ] Dragging a card onto the tray moves it to Failed; dragging a failed card to a column moves
      it out. Live updates without a manual refresh (board already polls/refreshes on run_end).
- [ ] `cd web && npm run build` clean (no TS errors). No backend change.

## Non-goals
- Touching backend / board model. Global cross-project failures view (that was the rejected
  alternative). Changing other columns' behaviour.
