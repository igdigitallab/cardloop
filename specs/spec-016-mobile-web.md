---
created: 2026-06-04
status: in-progress
---

# Spec 016 — Mobile / responsive web cockpit

## Goal
Make the cockpit comfortable on phones and tablets (touch-first), not just desktop.
Today it's a desktop IDE (fixed sidebar + tab bar + project view + side chat); on a
narrow screen it's cramped and drag interactions don't work on touch. Targets: 375px
(phone), 768px (tablet portrait), 1024px (tablet landscape). Desktop stays unchanged.

## Current state (grounding)
- `.app-layout` = flex: `.sidebar` (fixed `--sidebar-w`, always visible) + `.main-area`
  (tab bar + `.project-tab-slot`).
- Split-view (two free chats) = row layout with a `col-resize` divider.
- ProjectView = header (title/git/tabs) + tab content + a resizable side chat panel.
- Responsive is partial: one `@media (max-width: 900px)` in `layout.css`,
  `@media (pointer: coarse)` tweaks in `chat.css`. No systematic breakpoints, no mobile
  sidebar, no touch drag.
- Viewport meta present (`width=device-width, initial-scale=1`).
- Drag-to-reorder (sidebar projects, board cards) uses HTML5 DnD → broken on touch
  (board card **f78394**).

## Breakpoints (standardize)
- mobile: `max-width: 640px`
- tablet: `max-width: 1024px`
- desktop: `> 1024px` (current behavior, untouched)
Layer `max-width` overrides on top of the desktop base; keep queries consistent.

## Changes

### 1. Sidebar → off-canvas drawer (≤1024px)
- The sidebar becomes a slide-in drawer (`position: fixed`, off-screen `translateX`) with
  a backdrop overlay, toggled by a hamburger button in the tab bar. Tap backdrop or pick a
  project → close. Desktop (>1024px): unchanged (always visible).
- New `drawerOpen` state in `App.tsx` (not persisted; default closed). Close on project
  select and on resize-up to desktop.

### 2. Main area full-width on mobile
- `.main-area` takes full width; the drawer overlays it.

### 3. Split-view → single pane (≤640px)
- Two side-by-side free chats can't fit. On ≤640px collapse to a single (active) pane;
  keep the desktop row layout above the breakpoint.

### 4. ProjectView responsive
- **Header**: condense — keep title + git dot + sync; wrap or hide secondary chips on
  mobile; tighter padding.
- **Tab bar**: horizontally scrollable (`overflow-x:auto`, momentum), no wrap; tap targets
  ≥44px tall.
- **Chat panel**: desktop = resizable side panel; on ≤768px make it full-width/height
  (chat is the primary interaction) — hide the resizer; toggle between content tab and chat.

### 5. Touch interactions (closes f78394)
- Replace HTML5 drag-to-reorder with **Pointer Events** (works for mouse + touch) for the
  sidebar project list. Keep board card column moves on the existing arrow buttons (already
  touch-friendly). If pointer-reorder is too heavy, fall back to up/down move handles shown
  on `@media (pointer: coarse)`.
- All interactive controls ≥44×44px on coarse pointers.

### 6. Inputs / modals / safe areas
- Inputs ≥16px font on mobile (prevents iOS auto-zoom).
- Modals/pickers (PromptPicker, SkillPicker, SessionSelector, file-open) → full-screen or
  bottom-sheet on ≤640px.
- Viewport: add `viewport-fit=cover`; honor `env(safe-area-inset-*)` for notches; use
  dynamic viewport units (`100svh`/`100dvh`) so the mobile keyboard doesn't crop the chat.

### 7. Typography / spacing
- Comfortable spacing on mobile; audit for fixed widths so nothing overflows horizontally.

## Phases (implementation)
- **A** — breakpoint system + off-canvas sidebar drawer + hamburger + main-area full-width.
- **B** — ProjectView (tab scroll, header condense, chat full-width ≤768) + split→single.
- **C** — touch reorder (f78394) + tap targets + input-zoom + modal bottom-sheets +
  safe-area/`dvh`.

## Verify
- `npm run build && npm run lint` clean.
- Manual at 375 / 768 / 1024px: no horizontal overflow; drawer opens/closes; tabs scroll;
  chat usable with the on-screen keyboard; sidebar reorder works by touch.

## Non-goals
- PWA / offline / installable app — separate spec.
- Gestures beyond drawer + reorder; landscape-specific tuning beyond "not broken".

## Related
- Board card **f78394** (tablet drag) — closed by §5.
- [[spec-015-oss-runtime]] — English-only applies to every new string here.
