/**
 * State of the Files explorer's open-file tabs, as a pure reducer (no React, no I/O).
 *
 * The rules that used to be scattered through the component live here so they can be tested:
 *  - `content` is the last text known to be on disk, `draft` is what the operator typed
 *    (null = not editing). Dirty is DERIVED (draft !== content), never a flag, so a save
 *    that is still in flight while the operator keeps typing cannot mark the tab clean.
 *  - a background refresh never touches a tab that has unsaved edits: it only raises
 *    `diskChanged`, and the operator chooses reload or keep;
 *  - a 409 on save raises `conflict` and keeps the draft;
 *  - `gen` counts how often the on-disk baseline was replaced. A refresh carries the gen it
 *    started from and is dropped if a save/reload landed meanwhile — otherwise a slow refresh
 *    (run_end + window focus fire together) rolls a just-saved tab back to the old text and
 *    the operator's next save fails with a 409 against their own previous save.
 * Tests: filesTabs.test.ts (run command in its header).
 */

export interface DocSnapshot {
  content: string
  rev: string
  editable: boolean
  lang: string
  size: number
  /** Server refusal ("binary file", "file too large") — the tab shows it instead of text. */
  error?: string
}

export interface OpenFile {
  path: string
  status: 'loading' | 'ready' | 'error'
  error: string
  content: string
  rev: string
  editable: boolean
  lang: string
  size: number
  /** null = viewing; a string = the editor is open with this text. */
  draft: string | null
  diskChanged: boolean
  conflict: boolean
  saving: boolean
  saveError: string
  /** Bumped every time the on-disk baseline (content/rev) is replaced. */
  gen: number
}

export interface TabsState {
  tabs: OpenFile[]
  active: string | null
}

export const EMPTY_TABS: TabsState = { tabs: [], active: null }

export type TabsAction =
  | { type: 'open'; path: string }
  | { type: 'loaded'; path: string; doc: DocSnapshot }
  | { type: 'failed'; path: string; message: string }
  | { type: 'close'; path: string }
  | { type: 'activate'; path: string }
  | { type: 'startEdit'; path: string }
  | { type: 'setDraft'; path: string; text: string }
  | { type: 'cancelEdit'; path: string }
  | { type: 'saveStart'; path: string }
  | { type: 'saveOk'; path: string; saved: string; rev: string }
  | { type: 'saveFail'; path: string; message: string; conflict: boolean }
  | { type: 'refreshed'; path: string; doc: DocSnapshot; gen?: number }
  | { type: 'restoreDraft'; path: string; draft: string; rev: string }
  | { type: 'reload'; path: string; doc: DocSnapshot }
  | { type: 'keepMine'; path: string }
  | { type: 'reset' }

export function isDirty(t: OpenFile): boolean {
  return t.draft !== null && t.draft !== t.content
}

function blank(path: string): OpenFile {
  return {
    path, status: 'loading', error: '', content: '', rev: '', editable: false, lang: '', size: 0,
    draft: null, diskChanged: false, conflict: false, saving: false, saveError: '', gen: 0,
  }
}

function fromDoc(t: OpenFile, doc: DocSnapshot): OpenFile {
  if (doc.error) return { ...t, status: 'error', error: doc.error, size: doc.size, lang: doc.lang, gen: t.gen + 1 }
  return {
    ...t, status: 'ready', error: '', content: doc.content, rev: doc.rev, editable: doc.editable,
    lang: doc.lang, size: doc.size, gen: t.gen + 1,
  }
}

/** Apply `fn` to one tab. Returns the SAME state object when nothing changed, so a no-op
 *  refresh does not re-render the explorer. */
function patch(s: TabsState, path: string, fn: (t: OpenFile) => OpenFile): TabsState {
  const i = s.tabs.findIndex(t => t.path === path)
  if (i < 0) return s
  const next = fn(s.tabs[i])
  if (next === s.tabs[i]) return s
  const tabs = s.tabs.slice()
  tabs[i] = next
  return { ...s, tabs }
}

export function tabsReducer(s: TabsState, a: TabsAction): TabsState {
  switch (a.type) {
    case 'reset':
      return EMPTY_TABS
    case 'open':
      if (s.tabs.some(t => t.path === a.path)) return { ...s, active: a.path }
      return { tabs: [...s.tabs, blank(a.path)], active: a.path }
    case 'loaded':
      return patch(s, a.path, t => fromDoc(t, a.doc))
    case 'failed':
      return patch(s, a.path, t => ({ ...t, status: 'error', error: a.message }))
    case 'close': {
      const i = s.tabs.findIndex(t => t.path === a.path)
      if (i < 0) return s
      const tabs = s.tabs.filter(t => t.path !== a.path)
      // Closing the active tab activates its right neighbour, else the left one.
      const active = s.active !== a.path ? s.active : (tabs[i] ?? tabs[i - 1])?.path ?? null
      return { tabs, active }
    }
    case 'activate':
      return s.tabs.some(t => t.path === a.path) ? { ...s, active: a.path } : s
    case 'startEdit':
      return patch(s, a.path, t => (t.editable && t.status === 'ready' && t.draft === null
        ? { ...t, draft: t.content, saveError: '', conflict: false } : t))
    case 'setDraft':
      return patch(s, a.path, t => (t.draft === null ? t : { ...t, draft: a.text }))
    case 'cancelEdit':
      return patch(s, a.path, t => ({ ...t, draft: null, saveError: '', conflict: false, diskChanged: false }))
    case 'saveStart':
      return patch(s, a.path, t => ({ ...t, saving: true, saveError: '', conflict: false }))
    case 'saveOk':
      return patch(s, a.path, t => ({
        ...t, saving: false, saveError: '', conflict: false, diskChanged: false,
        content: a.saved, rev: a.rev, gen: t.gen + 1,
        // Typed nothing since the request left: back to viewing. Typed more: still editing.
        draft: t.draft === a.saved ? null : t.draft,
      }))
    case 'saveFail':
      return patch(s, a.path, t => ({ ...t, saving: false, saveError: a.message, conflict: a.conflict }))
    case 'refreshed':
      return patch(s, a.path, t => {
        if (a.gen !== undefined && a.gen !== t.gen) return t  // a save/reload landed since it began
        if (a.doc.error) return isDirty(t) ? t : fromDoc(t, a.doc)
        if (a.doc.rev === t.rev) return t
        // The file changed on disk. Unsaved edits win until the operator decides.
        if (isDirty(t)) return { ...t, diskChanged: true }
        return { ...fromDoc(t, a.doc), draft: t.draft === null ? null : a.doc.content }
      })
    case 'reload':
      return patch(s, a.path, t => ({
        ...fromDoc(t, a.doc), draft: null, diskChanged: false, conflict: false, saveError: '',
      }))
    case 'restoreDraft':
      // An unsaved draft that outlived its tab (tab switch, reload). If the disk moved on since
      // the draft was made, keep the ORIGINAL rev: the save then 409s instead of silently
      // clobbering what changed, and the banner offers reload / overwrite.
      return patch(s, a.path, t => (t.status === 'ready' && t.editable && t.draft === null && a.draft !== t.content
        ? { ...t, draft: a.draft, rev: a.rev, diskChanged: a.rev !== t.rev } : t))
    case 'keepMine':
      return patch(s, a.path, t => ({ ...t, diskChanged: false }))
  }
}

// ── persistence ───────────────────────────────────────────────────────────────

export interface PersistedTabs {
  v: 1
  root: string
  active: string | null
  paths: string[]
}

/** Parse what localStorage held; anything malformed yields null (start clean). */
export function parsePersisted(raw: string | null): PersistedTabs | null {
  if (!raw) return null
  try {
    const p = JSON.parse(raw) as Partial<PersistedTabs>
    if (p.v !== 1 || typeof p.root !== 'string' || !Array.isArray(p.paths)) return null
    const paths = p.paths.filter((x): x is string => typeof x === 'string' && x.startsWith('/'))
    const active = typeof p.active === 'string' && paths.includes(p.active) ? p.active : (paths[0] ?? null)
    return { v: 1, root: p.root, active, paths }
  } catch {
    return null
  }
}

export function toPersisted(root: string, s: TabsState): PersistedTabs {
  return { v: 1, root, active: s.active, paths: s.tabs.map(t => t.path) }
}
