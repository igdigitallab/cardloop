---
created: 2026-06-04
updated: 2026-06-11
status: revision-3 (phases A-C+D1-D5 shipped; Phase F = app-like nav stack SHIPPED 2026-06-11)
---

# Spec 016 — Mobile / responsive web cockpit

## Goal
Make the cockpit comfortable on phones and tablets (touch-first), not just desktop.
Primary requirement: the project chat (terminal) must fill 100% of the viewport width
on a phone with no side margins. Secondary: navigation, kanban, and PWA install.
Targets: 390px (phone), 768px (tablet portrait), 1024px (tablet landscape, current
desktop behavior mostly preserved).

---

## Ground truth after bd55bab (Playwright audit 2026-06-10, viewport 390×844)

### What works (phases A-C shipped)
- Off-canvas sidebar drawer + hamburger: opens/closes, backdrop tap closes, project
  select auto-closes. `drawerOpen` state in App.tsx, resize-up listener.
- No horizontal overflow: `body.scrollWidth === 390` in all tested views.
- Chat pane on project view: `390×380px`, x=0 — **100% viewport width, no left margin**.
  Stacked layout confirmed (`flex-direction: column` on `.main-content` at ≤768px).
- All `.tab-btn` heights: 44px — OK.
- `.chat-send-btn` height: 44px — OK.
- `.chat-textarea` font-size: 16px (no iOS auto-zoom) — OK.
- Free chat: `390×768px` — almost full viewport height, excellent.
- `viewport-fit=cover` in `index.html` — OK.
- `100dvh` shell, `env(safe-area-inset-bottom)` on chat-input-area and sidebar-footer — OK.
- Bottom-sheet modals at ≤640px (`modal.css`) — OK.
- Pointer Events touch-reorder in sidebar — OK (closes f78394).

### Remaining problems (ground truth from Playwright)

**P0 — Critical**

1. **Chat height too small in project view** (P0).
   `.project-chat-pane` is `height: 45vh` (`flex: 0 0 auto`, min-height: 260px).
   On 844px screen that's 380px — acceptable but fixed. More importantly, when the user
   switches to CLAUDE.md/Logs/Files/Board tabs, `project-left-pane` grows (content-driven
   `flex: 55` in desktop, overridden `flex: none` at ≤768px), potentially pushing the chat
   off-screen or leaving it cramped below. The correct mobile UX: chat is a primary tool —
   it should fill remaining height below the tab content area, not be a fixed 45vh slab.
   **Fix:** remove fixed `height: 45vh` and use `flex: 1` on `.project-chat-pane` at ≤768px,
   with `min-height: 200px`. Left pane gets a `max-height` so it doesn't collapse chat.

2. **Board tab unusable on mobile** (P0).
   `tab-content` height = 124px because left-pane layout doesn't allocate enough vertical
   space when kanban loads. Board columns render 0×0. The board requires a tall content area.
   **Fix (option A — preferred):** on ≤640px, Board tab switches to a stacked single-column
   list view instead of a kanban row. On ≤768px give the board tab-content `min-height: 60vh`
   or make `.project-left-pane` taller when the Board tab is active (add `.tab-board-active`
   class to left-pane). **Fix (option B):** make board tab-content `overflow-x: auto` +
   fixed-width columns so the kanban scrolls horizontally — simpler, board stays board.

**P1 — Important**

3. **PWA manifest missing** (P1).
   No `manifest.json`, no icons, no `<link rel="manifest">` in index.html.
   Without it, Chrome on Android shows "Add to Home Screen" as a plain bookmark, not
   a PWA install prompt; the app opens in the browser with the address bar, which steals
   ~56px of vertical space from the chat. **A standalone PWA gives the chat ~7% more height.**
   Service worker is not needed (no offline requirement). Manifest + icons alone are enough
   for a "minimal installable PWA" on Android Chrome.

4. **"Shift+Enter" placeholder hint on touch devices** (P1).
   `.chat-textarea` placeholder reads "Message to agent… (Enter to send, Shift+Enter for
   newline)". On a phone, Enter on the virtual keyboard does NOT send — it inserts a
   newline. The send button is the only send path. The hint is wrong and confusing.
   **Fix:** detect `pointer: coarse` (or `window.ontouchstart`) in the placeholder / hint
   text and show a simpler "Message to agent…" on touch.

5. **"Split" button visible in free-chat toolbar on mobile** (P1).
   The `.split-create-btn` "Split" button appears in `.free-chat-toolbar` on mobile. On
   ≤640px there is no split view (collapsed to single pane). The button should be hidden
   (`display: none`) at ≤640px.

6. **Open-tabs overflow in ptab-list** (P1).
   Multiple open project tabs overflow `.ptab-list` horizontally — scroll works but there's
   no visual affordance (fade edge). With many open projects, the active tab may be off-screen
   after reload. Auto-scroll the active ptab into view on mount / tab switch.

---

## Breakpoints (established, keep these)

| name    | query              | applies to              |
|---------|--------------------|-------------------------|
| mobile  | `max-width: 640px` | single-column, no split |
| tablet  | `max-width: 1024px`| drawer, tap targets     |
| desktop | `> 1024px`         | original layout         |

---

## Phase D — Chat height fix + Board fix (P0s) [est. 2-3h]

**D1 — Chat pane flex at ≤768px**

In `layout.css` under `@media (max-width: 768px)`:
```css
.project-chat-pane {
  flex: 1 1 auto;       /* was: flex: none; height: 45vh */
  min-height: 200px;
  /* remove the fixed height: 45vh */
}
.project-left-pane {
  flex: 0 0 auto;
  max-height: 55vh;     /* prevents left pane from crowding chat to nothing */
  overflow-y: auto;     /* tab-content scrolls internally if content is taller */
}
```
This way: left pane takes what it needs (up to 55vh), chat fills the rest.
Verify: Overview on 844px → left pane ≈ 229px, chat ≈ 570px (67%). Board/Logs tab →
left pane grows up to max 55vh (464px), chat still ≥ 200px.

**D2 — Board on mobile (Option B, simpler)**

In `board.css` under `@media (max-width: 768px)`:
```css
/* Board tab: horizontal scroll with fixed-width columns */
.kanban-board {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  flex-wrap: nowrap;
  display: flex;
  gap: 10px;
  padding-bottom: 8px;
}
.kanban-col {
  min-width: 240px;
  flex-shrink: 0;
}
```
And `.tab-content` on mobile should allow scroll:
```css
@media (max-width: 768px) {
  .tab-content {
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }
}
```
This keeps the board familiar (same columns), adds horizontal scroll. No layout change needed.
After D1, `.project-left-pane` has `max-height: 55vh` and the board tab-content gets
enough room to render.

**D3 — "Shift+Enter" hint on touch**

In `ChatTab.tsx` (or wherever the textarea placeholder is set), detect touch:
```tsx
const isTouchDevice = typeof window !== 'undefined' && 'ontouchstart' in window
const placeholder = isTouchDevice
  ? 'Message to agent…'
  : 'Message to agent… (Enter to send, Shift+Enter for newline)'
```
Or purely via CSS: use `@media (pointer: coarse)` to hide a separate hint `<span>` below
the textarea if one exists.

**D4 — Hide Split button on mobile**

In `layout.css` or `chat.css`:
```css
@media (max-width: 640px) {
  .split-create-btn { display: none; }
}
```

**D5 — Auto-scroll active ptab into view**

In `ProjectTabBar.tsx`, add `useEffect` that calls
`activeTabRef.current?.scrollIntoView({ behavior: 'smooth', inline: 'nearest' })`
when `activeId` changes.

Acceptance for Phase D:
- Playwright 390×844: chat-wrap height ≥ 300px in Overview tab.
- Playwright 390×844: board tab-content has scrollWidth > 390 (kanban visible, horizontal scroll).
- No "Shift+Enter" visible at pointer:coarse.
- Split button not rendered at ≤640px.

---

## Phase E — PWA manifest + install (P1) [est. 1h]

**Why off-canvas drawer over bottom nav:** current code already has the drawer
implemented (off-canvas sidebar). Adding a bottom nav would require duplicating
navigation logic and a new component. The drawer is cheaper and already works well
on the Playwright audit — the hamburger is reachable at x=0, y=0, 44px tap target.
**Decision: keep drawer, do not add bottom nav in this spec.** Bottom nav could be
a future enhancement for P2 (navigating between open projects without the drawer).

**E1 — manifest.json** (`web/public/manifest.json`):
```json
{
  "name": "Cardloop",
  "short_name": "Cardloop",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0e0e13",
  "theme_color": "#0e0e13",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

**E2 — Icons** (`web/public/icons/`): generate 192×192 and 512×512 PNG from the ⚡
SVG already used as favicon in `index.html`. Use sharp/imagemagick/canvas in a one-off
build script. Dark background (`#0e0e13`) + white lightning bolt. Maskable icon optional.

**E3 — Link in `index.html`**:
```html
<link rel="manifest" href="/manifest.json" />
<meta name="theme-color" content="#0e0e13" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
```

**No service worker needed.** The cockpit is always online (it's your own server),
offline mode has no value, and a SW adds maintenance overhead. Chrome will still offer
"Add to Home Screen" install for a manifest-only PWA (Installable criteria: HTTPS or
localhost + manifest with `display: standalone` + icons).

**E4 — HTTPS requirement**: the cockpit is already behind Cloudflare (`https://`) in
production so the PWA install prompt will work. On localhost dev it also works.

Acceptance for Phase E:
- Playwright: `document.querySelector('link[rel="manifest"]')` returns non-null.
- Chrome Android: "Add to Home Screen" shows app icon (not generic bookmark icon).
- After installing: opens in standalone mode (no address bar), full viewport height.

---

## Acceptance tests (Playwright, viewport 390×844)

```
[ ] body.scrollWidth === window.innerWidth (no horizontal overflow)
[ ] .project-chat-pane width === 390 (100% viewport)
[ ] .project-chat-pane height >= 300 (chat is usable, not cramped)
[ ] .chat-send-btn height >= 44 (touch target)
[ ] .chat-textarea font-size === 16px (no iOS zoom)
[ ] All .tab-btn height >= 44
[ ] Board tab-content scrollWidth > 390 (kanban visible)
[ ] .split-create-btn not visible at 390px
[ ] link[rel="manifest"] present
[ ] drawer opens on hamburger tap, closes on backdrop tap
[ ] No horizontal scrollbar visible
```

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Phase D1 (flex:1 on chat) breaks desktop layout | All changes inside `@media (max-width: 768px)` — desktop rule (>1024) unchanged |
| D1 left-pane max-height: 55vh hides content in Overview | tab-content is `overflow-y: auto` — user can scroll |
| D2 board horizontal scroll too wide on very small screen | min-width: 240px per column is safe on 390px (1.6 columns visible — scrollable) |
| E2 icon generation at build time | One-off Node script, not in CI hot path |
| PWA install on Android requires HTTPS | Already behind Cloudflare in prod |
| Desktop regression (Игорь's laptop) | All P0/P1 fixes are in mobile-only breakpoints; desktop path untouched |

---

## Phases summary

| Phase | Content | Est. |
|-------|---------|------|
| A | Off-canvas drawer + hamburger + main-area full-width | **DONE** |
| B | ProjectView: tab scroll, header condense, chat stacked ≤768, split→single ≤640 | **DONE** |
| C | Touch targets ≥44px, 16px inputs, modal bottom-sheets, dvh + safe-area | **DONE** |
| D | Chat flex height fix, board horizontal scroll, Shift+Enter hint, Split button hide | ~2-3h |
| E | PWA manifest + icons + meta tags | ~1h |
| F | App-like navigation stack (rev3, see below) | **DONE** |

---

## Phase F — App-like navigation stack (revision-3, SHIPPED 2026-06-11)

Operator verdict on phases A–C: a shrunk desktop "looks awful" on a phone. Rev3 replaces
the responsive-squeeze approach with a proper mobile nav stack at ≤768px (desktop >768 untouched):

- **Screen 1 — project list**: sidebar content covers the full screen (`.app-layout.mobile-on-list`).
  Selecting a project slides to Screen 2.
- **Screen 2 — project**: chat fills the whole screen (primary tool). `ProjectTabBar` (project
  tabs + green reply-ready badges) stays on top; the hamburger becomes a `‹` back-to-list button.
  Below it: compact horizontally scrollable inner tab strip (`.mobile-inner-tabs`) —
  Chat | CLAUDE.md | Logs | Board | Files | Memory | Activity | Settings.
- Inner tabs open full-screen replacing chat; tapping Chat returns.
- State: `mobileScreen: 'list' | 'project'` in App.tsx; `mobileInnerTab: TabId | null` in ProjectView.

### Defect found in review + root cause (fixed)

**Symptom:** Board (and any data tab) stuck on "Loading…" forever on mobile over plain HTTP.

**Root cause — NOT a mobile-layout bug per se:** every open project tab keeps its ProjectView
mounted (`display:none` slot), and each mounted ProjectView held an open per-project SSE
fetch-stream (`/api/projects/<id>/activity-stream`). UI state hydrates the operator's open-tab
list (8 tabs) from the server, so 8 SSE streams + 1 global EventSource exceeded the browser's
~6-connections-per-origin HTTP/1.1 limit → every subsequent `fetch()` queued indefinitely.
HTTP/2 (prod via Cloudflare) multiplexes and hides the bug; plain HTTP (localhost) deadlocks.

**Fix:** `ProjectActivityProvider` got an `active` prop — the SSE stream is held only for the
*active* project tab (`active={isActive}` in ProjectView). On reactivation the provider
reconnects and emits one synthetic `run_end` ("sse-catch-up") so `useOnRunEnd` subscribers
re-fetch whatever they missed while hidden. ChatTab is immune to the synthetic event
(busActiveRef gate) and self-heals via its 5s `/running` poll. Reply-ready badges are
unaffected — they ride the single global `/api/activity-stream` EventSource in App.tsx.
Connection budget after fix: 2 (global + active project) instead of N+1.

---

## Non-goals (unchanged)
- Gestures beyond drawer (swipe-to-open etc.).
- Offline / service worker.
- Landscape-specific tuning beyond "not broken".
- Bottom navigation bar (deferred, drawer is sufficient).

---

## Related
- Board card **f78394** (tablet drag) — closed by Phase C (Pointer Events).
- [[spec-015-oss-runtime]] — English-only applies to every new string here.
- [[spec-009-quality-gate]] — Phase D board fix does not affect worktree/gate flow.

---

## Phase G — QA pass (2026-06-11, 17 defects)

### Fixed
| ID | Description | Resolution |
|----|-------------|------------|
| D-01 | Breakpoint mismatch JS `< 768` | Changed to `<= 768` in ProjectView.tsx |
| D-02 | Same root cause as D-01 | Fixed with D-01 |
| D-03 | ptab-close hover-only on touch | `@media (hover: none)` always-visible on active tab |
| D-04 | Excessive chrome 130px | Removed mobile-project-header; 2-row chrome ≈ 88px |
| D-05 | No keyboard layout adjust | visualViewport resize handler in ChatTab.tsx |
| D-06 | Hamburger hidden on mobile list | Resolved by Directive 2 (toggle hidden on mobile) |
| D-07 | Tab strip no scroll indicator | mask-image fade gradient on mobile-inner-tabs |
| D-08 | Inner tab buttons 40px < 44px | min-height: 44px on .mobile-inner-tab-btn |
| D-09 | Collapse toggle 22×22px | Hidden on mobile; coarse pointer upsizes on desktop |
| D-10 | New project btn 36px | min-height: 44px at ≤768px |
| D-11 | Logout btn 31px | min-height: 44px at ≤768px |
| D-12 | ptab-folder-btn 36px | min-height: 44px at ≤768px |
| D-13 | Desktop collapsed icon btn height | Icon strip removed (CR-01 Directive 2) |
| D-14 | Sidebar toggle small | Same as D-09 |
| D-15 | Logout small | Same as D-11 |
| D-16 | New project btn small | Same as D-10 |
| CR-01 | Collapsed icon strip | Removed icon list; rail shows only expand + new btn |

### Director directives applied
- **Directive 1**: mobile-project-header removed; ProjectTabBar + mobile-inner-tabs = 2-row chrome (~88px)
- **Directive 2**: mobile toggle hidden; desktop collapsed = narrow rail (expand + new, no icon list)

---

## Phase H — Operator follow-ups after Phase G acceptance (2026-06-11)

Three mobile-only improvements requested by the operator. All changes gated behind
`@media (max-width: 768px)` (CSS) / `narrow` guard (JS); desktop >768 untouched.

**H1 — Back button on the project-list screen.**
Opening the full-screen project list from a project left no way back (the `‹` chevron
lives in ProjectTabBar, hidden on `mobile-on-list`). Fix: `.sidebar-back-btn` row at the
top of the sidebar — rendered ONLY when an active project exists (`activeProjectId` not
null / `__global__` / `__schedules__`), hidden at >768px via CSS. Tap →
`setMobileScreen('project')` (reuses the existing reverse slide). On first launch (no
active project) the button is absent. Props: `Sidebar.activeProjectId` + `Sidebar.onGoBack`
(wired in App.tsx).

**H2 — Open-tabs dropdown menu in ProjectTabBar.**
The mobile tab strip fits ~1 tab. Added a compact `▾ N` button (≥44px tap target) inside
`.ptab-list` (before the `+` button) showing the count of open projects. Tap → dropdown
listbox of all open tabs: name + green reply-ready dot + red 🚨 incidents badge + unread
counter. Items are ≥44px tall. Tap an item → `onActivate(id)` + menu closes. Horizontal
scroll of the strip is preserved; the menu is `display:none` on desktop. Click-outside
closes (document mousedown listener while open).

**H3 — Swipe to switch between open project chats.**
Gesture is active only at ≤768px AND only when the inner Chat tab is shown
(`mobileInnerTab === null`) — Board/Files/etc. keep their own scrolls untouched.
Recognition on `.mobile-project-content` (touchstart/move/end): horizontal delta >60px,
vertical drift <30px (cancels mid-gesture if exceeded — feed scroll wins), and the gesture
is ignored when it starts inside a horizontally scrollable element (`scrollWidth >
clientWidth` checked up the parent chain — pre/code blocks).

**Swipe direction mapping** (single constant block in ProjectView.tsx, flip `+1/-1` to reverse):
- Swipe RIGHT (finger moves right) → NEXT open tab (index + 1 = the project to the RIGHT in tab order)
- Swipe LEFT (finger moves left) → PREVIOUS open tab (index - 1 = to the LEFT)

At list edges: no wrap-around, gesture is a no-op. Feedback: 180ms translateX+opacity CSS
transition (`swipe-anim-left/right`) on the content area, then `onSwipeToProject(id)` →
`handleTabActivate`. Props: `ProjectView.openProjectIds` + `ProjectView.onSwipeToProject`
(wired in App.tsx).

Acceptance (Playwright 390×844): list back button appears from project & returns; ▾ menu
lists 3+ open projects & switches; synthetic touch swipe switches to the adjacent tab;
vertical feed scroll unaffected; desktop 1440×900 regression — sidebar/split/tabs unchanged.

---

## Phase H — Chrome compression (2026-06-11, operator feedback)

Operator request after Phase G: maximize vertical chat space on phone (dark theme,
Chrome in standalone/installed PWA mode — no address bar). All changes ≤768px only.

### Baseline before (Playwright 390×844, mobile-on-project)
| Row | Before |
|-----|--------|
| `.project-tabbar` | 45px |
| `.mobile-inner-tabs` | 45px |
| `.chat-session-bar` | 69px (2-row wrap) |
| `.ctx-panel` | 24px |
| **Total top chrome** | **183px** |

### Changes shipped

**HC1 — Project tabbar strip (≤768px):** `min-height` of `.project-tabbar` reduced to 38px;
`.ptab` items: `min-height: 38px`, `padding: 0 10px`, `font-size: 12px`. Hamburger/back
button keeps `min-height: 40px` as primary navigation action. Result: **41px** (actual
height driven by content/padding rounding).

**HC2 — Inner tab strip (≤768px):** `.mobile-inner-tab-btn` `min-height` reduced from
44px → 40px, horizontal padding `0 12px` → `0 10px`. Result: **41px**.

**HC3 — Session bar single-row (≤768px):** `.chat-session-bar` changed from
`flex-wrap: wrap` to `flex-wrap: nowrap` with `min-height: 36px; padding: 3px 8px; gap: 5px`.
The 2-row wrap (69px) collapses to a single compact row (36px). Model label icon hidden at
≤768px (`display: none`) to save width. Stats badge gets `flex-shrink: 1; overflow: hidden`
to truncate gracefully. The coarse-pointer 44px override for session buttons is suppressed
at ≤768px (secondary controls inside the row — full-row tap zone is acceptable). Result: **36px**.

**HC4 — Context panel hidden on mobile (≤768px):** `.ctx-panel { display: none }` at ≤768px.
Files/commands list is non-critical information on phone. The 📎 icon expand-button is
sacrificed. The panel remains fully functional on desktop (>768px).

**HC5 — `100dvh` → `100svh` for shell (Chrome compression root cause fix):**
`svh` (small viewport height) is defined as the viewport height with ALL browser UI
present — address bar, navigation bar, etc. It is always the *minimum* available height
and is the correct unit for installed PWA / standalone mode where `dvh` can return an
intermediate value causing the bottom toolbar to be clipped. Fallback chain:
`100%` (html/body base) → `100dvh` (`@supports dvh`) → `100svh` (`@supports svh`, wins).

**HC6 — visualViewport handler fixed (ChatTab.tsx):** The Phase G keyboard handler used
`window.innerHeight - vv.height` as keyboard detection, but in standalone mode
`window.innerHeight` equals full screen while `vv.height` includes address-bar compensation
— so the delta was large even without a keyboard. Fixed: use a baseline captured at mount
(`baselineHeight = vv.height`) and only shrink when `baselineHeight - vv.height > 150px`
(unambiguous keyboard open signal).

**HC7 — `chat-input-area` explicit `flex-shrink: 0` at ≤768px:** Prevents the input area
from being squeezed; the feed scrolls instead.

### Result after (Playwright 390×844, ×780, ×700)
| Row | After |
|-----|-------|
| `.project-tabbar` | 41px |
| `.mobile-inner-tabs` | 41px |
| `.chat-session-bar` | 36px |
| `.ctx-panel` | 0px (hidden) |
| **Total top chrome** | **118px** (−65px, −35%) |

Send button and tools panel: **visible at all tested heights** (844, 780, 700px) without scroll.
Desktop 1440×900: ctx-panel still visible, tabbar 37px — no regression.
