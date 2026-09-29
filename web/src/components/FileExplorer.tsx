/**
 * File explorer: tree + tabbed viewer/editor over ONE absolute-path view of the server.
 * Used by FilesTab (a project's files, starting in its cwd) and GlobalFilesTab (starting at $HOME).
 *
 * The tree root is movable — up a folder, a breadcrumb, or a pasted path — within whatever the
 * server allows (see fs_browser.py). Open files are tabs whose state lives in lib/filesTabs.ts.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy } from 'lucide-react'
import { apiErrorMessage } from '../api'
import { mdComponents } from './markdown'
import { FileEntry, FsFile, FsInfo, FsListing, FsStat } from '../types'
import { FsAdapter } from '../lib/fsAdapter'
import {
  DocSnapshot, EMPTY_TABS, isDirty, parsePersisted, tabsReducer, toPersisted,
} from '../lib/filesTabs'
import { collectDrafts, DraftMap, parseDrafts } from '../lib/filesDrafts'
import { ancestorsBetween, baseName, dirname, isUnder, joinPath, looksLikePath } from '../lib/fsPath'
import { readLS, readLSString, writeLS, writeLSString } from '../lib/storage'
import { isPopoutWindow, popoutKey } from '../lib/popout'
import { ConfirmModal } from './ConfirmModal'
import { FilePathBar } from './FilePathBar'
import { FileTabsBar } from './FileTabsBar'
import { SplitHandle } from './SplitHandle'
import { Spinner } from './Spinner'

// ─── Tree types ───────────────────────────────────────────────────────────────

export interface TreeNode {
  name: string
  type: 'dir' | 'file'
  size: number
  /** Absolute path. */
  path: string
  depth: number
  open?: boolean
  children?: TreeNode[]
  loading?: boolean
  loadError?: string
}

function buildNodes(entries: FileEntry[], parentPath: string, depth: number): TreeNode[] {
  return entries.map(e => ({
    name: e.name,
    type: e.type,
    size: e.size,
    path: joinPath(parentPath, e.name),
    depth,
  }))
}

function findByPath(nodes: TreeNode[] | null, path: string): TreeNode | null {
  if (!nodes) return null
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.children) {
      const r = findByPath(n.children, path)
      if (r) return r
    }
  }
  return null
}

function mutateNode(nodes: TreeNode[], targetPath: string, mutate: (n: TreeNode) => void): boolean {
  for (const n of nodes) {
    if (n.path === targetPath) { mutate(n); return true }
    if (n.type === 'dir' && n.children && mutateNode(n.children, targetPath, mutate)) return true
  }
  return false
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function toSnapshot(d: FsFile, writable: boolean): DocSnapshot {
  return {
    content: d.content, rev: d.rev, lang: d.lang, size: d.size, error: d.error,
    editable: writable && !!d.editable,
  }
}

// ─── TreeView ─────────────────────────────────────────────────────────────────

interface TreeProps {
  nodes: TreeNode[]
  selectedPath: string | null
  onFileClick: (node: TreeNode) => void
  onDirToggle: (node: TreeNode) => void
}

function TreeView({ nodes, selectedPath, onFileClick, onDirToggle }: TreeProps) {
  return (
    <>
      {nodes.map(node => (
        <div key={node.path}>
          <div
            className={`file-tree-row ${selectedPath === node.path ? 'active' : ''}`}
            style={{ paddingLeft: `${8 + node.depth * 14}px` }}
            title={node.path}
            data-path={node.path}
            role={node.type === 'dir' ? 'button' : 'option'}
            aria-expanded={node.type === 'dir' ? node.open : undefined}
            aria-selected={selectedPath === node.path}
            tabIndex={0}
            onClick={() => node.type === 'dir' ? onDirToggle(node) : onFileClick(node)}
            onKeyDown={e => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                if (node.type === 'dir') onDirToggle(node); else onFileClick(node)
              }
            }}
          >
            {node.type === 'dir' ? (
              <>
                <span className="file-tree-caret" aria-hidden="true">{node.open ? '▾' : '▸'}</span>
                <span className="file-tree-icon" aria-hidden="true">📁</span>
              </>
            ) : (
              <>
                <span className="file-tree-caret" aria-hidden="true" />
                <span className="file-tree-icon" aria-hidden="true">📄</span>
              </>
            )}
            <span className="file-tree-name">{node.name}</span>
            {node.loading && <span className="file-tree-spinner">…</span>}
            {node.loadError && <span className="file-tree-err" title={node.loadError}>⚠</span>}
          </div>
          {node.open && node.children && (
            <TreeView
              nodes={node.children}
              selectedPath={selectedPath}
              onFileClick={onFileClick}
              onDirToggle={onDirToggle}
            />
          )}
        </div>
      ))}
    </>
  )
}

// ─── Persisted UI prefs ───────────────────────────────────────────────────────

const LS_TREE_W = 'cops.files.treeWidth'
const LS_TREE_HIDDEN = 'cops.files.treeHidden'
const LS_TABS = 'cops.files.tabs.'
const LS_DRAFTS = 'cops.files.drafts.'
const TREE_W_DEFAULT = 220
const TREE_W_MIN = 140
const VIEWER_MIN = 240
const NARROW_QUERY = '(max-width: 768px)'

// The nonce of the last search-hit request each explorer scope has acted on. Module-level on
// purpose: the tab unmounts on every switch to another project tab, and a ref would forget, so
// the same old hit would re-open (and yank the tree away) on every return.
const handledOpen = new Map<string, number>()

// A pop-out keeps its own layout so resizing it does not move the main window's (popout.ts).
const lsk = (key: string) => (isPopoutWindow() ? popoutKey(key) : key)

// ─── FileExplorer ─────────────────────────────────────────────────────────────

export interface FileExplorerProps {
  fs: FsAdapter
  /**
   * If provided, a ref that FileExplorer will populate with its refresh function.
   * Callers can then invoke it imperatively (e.g. on run_end).
   */
  refreshRef?: React.MutableRefObject<(() => Promise<void>) | null>
  /** spec-079: open this path (from a file search hit). A relative path is taken against the
   *  explorer's start folder; nonce-keyed so picking the same file twice re-opens it. */
  openPath?: { path: string; nonce: number } | null
}

export function FileExplorer({ fs, refreshRef, openPath }: FileExplorerProps) {
  const writable = !!fs.write
  const tabsKey = lsk(LS_TABS + fs.scope)
  const draftsKey = lsk(LS_DRAFTS + fs.scope)

  const [info, setInfo] = useState<FsInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [rootPath, setRootPath] = useState('')
  const [rootListing, setRootListing] = useState<FsListing | null>(null)
  const [rootNodes, setRootNodes] = useState<TreeNode[] | null>(null)
  const [tabs, dispatch] = useReducer(tabsReducer, EMPTY_TABS)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [copied, setCopied] = useState(false)
  const [confirmClose, setConfirmClose] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  const [treeW, setTreeW] = useState<number>(() => Math.max(TREE_W_MIN, readLS(lsk(LS_TREE_W), TREE_W_DEFAULT)))
  const [treeVisible, setTreeVisible] = useState<boolean>(() => !readLS(lsk(LS_TREE_HIDDEN), false))
  const [narrow, setNarrow] = useState<boolean>(() => !!window.matchMedia?.(NARROW_QUERY).matches)
  // On a phone the tree and the viewer take turns: opening a file shows the viewer.
  const [narrowTree, setNarrowTree] = useState(true)

  const nodesRef = useRef<TreeNode[] | null>(null)
  const rootPathRef = useRef('')
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const navSeq = useRef(0)
  const goSeq = useRef(0)
  const storedDrafts = useRef<DraftMap>({})
  const readyRef = useRef(false)
  const infoRef = useRef<FsInfo | null>(null)
  const layoutRef = useRef<HTMLDivElement>(null)
  const rootElRef = useRef<HTMLDivElement>(null)

  const showTree = narrow ? narrowTree : treeVisible

  useEffect(() => {
    const mq = window.matchMedia?.(NARROW_QUERY)
    if (!mq) return
    const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  useEffect(() => { writeLS(lsk(LS_TREE_W), treeW) }, [treeW])
  useEffect(() => { writeLS(lsk(LS_TREE_HIDDEN), !treeVisible) }, [treeVisible])

  useEffect(() => {
    if (!note) return
    const id = setTimeout(() => setNote(''), 9000)
    return () => clearTimeout(id)
  }, [note])

  // ── Tree plumbing ─────────────────────────────────────────────────────────

  const forceUpdate = useCallback(() => {
    if (nodesRef.current) setRootNodes([...nodesRef.current])
  }, [])

  const applyRoot = useCallback((l: FsListing) => {
    nodesRef.current = buildNodes(l.entries, l.path, 0)
    rootPathRef.current = l.path
    setRootListing(l)
    setRootPath(l.path)
    setRootNodes([...nodesRef.current])
  }, [])

  /** Make `path` the tree root. Returns false (and says why) when the server refuses. */
  const changeRoot = useCallback(async (path: string): Promise<boolean> => {
    const seq = ++navSeq.current
    setBusy(true)
    try {
      const l = await fs.list(path)
      if (seq !== navSeq.current) return false
      applyRoot(l)
      return true
    } catch (e) {
      if (seq === navSeq.current) setNote(apiErrorMessage(e))
      return false
    } finally {
      if (seq === navSeq.current) setBusy(false)
    }
  }, [fs, applyRoot])

  const handleDirToggle = useCallback((node: TreeNode) => {
    if (!nodesRef.current) return
    if (node.children !== undefined) {
      mutateNode(nodesRef.current, node.path, n => { n.open = !n.open })
      forceUpdate()
      return
    }
    mutateNode(nodesRef.current, node.path, n => { n.open = true; n.loading = true })
    forceUpdate()
    const root = rootPathRef.current
    fs.list(node.path).then(d => {
      if (!nodesRef.current || rootPathRef.current !== root) return
      mutateNode(nodesRef.current, node.path, n => {
        n.loading = false
        n.loadError = undefined
        n.children = buildNodes(d.entries, node.path, node.depth + 1)
      })
      forceUpdate()
    }).catch(e => {
      if (!nodesRef.current || rootPathRef.current !== root) return
      mutateNode(nodesRef.current, node.path, n => {
        n.loading = false
        n.loadError = apiErrorMessage(e)
        n.open = false
      })
      forceUpdate()
    })
  }, [fs, forceUpdate])

  /** Expand the folders above `abs` (best effort) and scroll its row into view. */
  const revealInTree = useCallback(async (abs: string) => {
    const root = rootPathRef.current
    for (const dir of ancestorsBetween(root, abs)) {
      if (!nodesRef.current || rootPathRef.current !== root) return
      const node = findByPath(nodesRef.current, dir)
      if (!node) return  // hidden folder or tree still booting — give up quietly
      if (node.children === undefined) {
        try {
          const d = await fs.list(dir)
          if (!nodesRef.current || rootPathRef.current !== root) return
          mutateNode(nodesRef.current, dir, n => {
            n.loading = false
            n.children = buildNodes(d.entries, dir, n.depth + 1)
            n.open = true
          })
        } catch { return }
      } else {
        mutateNode(nodesRef.current, dir, n => { n.open = true })
      }
      forceUpdate()
    }
    requestAnimationFrame(() => {
      const rows = rootElRef.current?.querySelectorAll<HTMLElement>('.file-tree-row')
      if (!rows) return
      for (const r of Array.from(rows)) {
        if (r.dataset.path === abs) { r.scrollIntoView({ block: 'nearest' }); break }
      }
    })
  }, [fs, forceUpdate])

  // ── Tabs ──────────────────────────────────────────────────────────────────

  /** `restore`: this tab is being brought back after a reload / tab switch, so an unsaved draft
   *  stored for it (see the drafts effect) is put back in the editor once the file is loaded. */
  const loadTab = useCallback(async (path: string, restore = false) => {
    try {
      const d = await fs.read(path)
      dispatch({ type: 'loaded', path, doc: toSnapshot(d, writable) })
      const kept = restore ? storedDrafts.current[path] : undefined
      if (kept) dispatch({ type: 'restoreDraft', path, draft: kept.draft, rev: kept.rev })
    } catch (e) {
      const status = (e as { status?: number }).status
      if (restore && (status === 404 || status === 403)) dispatch({ type: 'close', path })
      else dispatch({ type: 'failed', path, message: apiErrorMessage(e) })
    }
  }, [fs, writable])

  const openFile = useCallback((path: string) => {
    const existing = tabsRef.current.tabs.find(t => t.path === path)
    dispatch({ type: 'open', path })
    if (!existing || existing.status === 'error') void loadTab(path)
    setNarrowTree(false)
    void revealInTree(path)
  }, [loadTab, revealInTree])

  // ── Init / reset when the adapter changes ────────────────────────────────

  useEffect(() => {
    let cancelled = false
    ++navSeq.current
    readyRef.current = false
    setReady(false)
    nodesRef.current = null
    setLoading(true)
    setLoadError('')
    setNote('')
    setRootNodes(null)
    dispatch({ type: 'reset' })

    ;(async () => {
      try {
        const inf = await fs.info()
        if (cancelled) return
        infoRef.current = inf
        setInfo(inf)
        const saved = parsePersisted(readLSString(tabsKey))
        storedDrafts.current = parseDrafts(readLSString(draftsKey))
        let listing: FsListing
        try {
          listing = await fs.list(saved?.root || inf.start)
        } catch {
          listing = await fs.list(inf.start)
        }
        if (cancelled) return
        applyRoot(listing)
        if (saved) {
          for (const p of saved.paths) {
            dispatch({ type: 'open', path: p })
            void loadTab(p, true)
          }
          if (saved.active) dispatch({ type: 'activate', path: saved.active })
        }
        readyRef.current = true
        setReady(true)
        setLoading(false)
      } catch (e) {
        if (cancelled) return
        setLoadError(apiErrorMessage(e))
        setLoading(false)
      }
    })()

    return () => { cancelled = true }
  }, [fs, tabsKey, draftsKey, applyRoot, loadTab])

  // Remember the open files and the root (only paths/active/root — not every keystroke).
  const persistSig = JSON.stringify(toPersisted(rootPath, tabs))
  useEffect(() => {
    if (!readyRef.current || !rootPath) return
    writeLSString(tabsKey, persistSig)
  }, [persistSig, rootPath, tabsKey])

  // Unsaved edits outlive the tab: switching to another project tab unmounts this whole
  // explorer, and a reload or crash takes the page. Debounced while typing, and flushed on
  // unmount. Skipped while any tab is still loading — restoring is not finished, and writing
  // "no drafts" now would erase the very drafts we are about to put back.
  const writeDraftsRef = useRef<() => void>(() => {})
  writeDraftsRef.current = () => {
    if (!readyRef.current) return
    const list = tabsRef.current.tabs
    if (list.some(t => t.status === 'loading')) return
    const drafts = collectDrafts(list)
    writeLSString(draftsKey, Object.keys(drafts).length ? JSON.stringify(drafts) : null)
  }
  useEffect(() => {
    const id = setTimeout(() => writeDraftsRef.current(), 400)
    return () => clearTimeout(id)
  }, [tabs])
  useEffect(() => () => writeDraftsRef.current(), [])

  // ── Refresh (run_end / focus / button) ───────────────────────────────────

  const refresh = useCallback(async () => {
    if (!nodesRef.current) return
    const root = rootPathRef.current
    try {
      const l = await fs.list(root)
      const old = nodesRef.current
      const fresh = buildNodes(l.entries, l.path, 0)
      const merge = async (nodes: TreeNode[]) => {
        for (const n of nodes) {
          const was = findByPath(old, n.path)
          if (was && was.type === 'dir' && was.open) {
            n.open = true
            try {
              const sub = await fs.list(n.path)
              n.children = buildNodes(sub.entries, n.path, n.depth + 1)
              await merge(n.children)
            } catch { /* skip */ }
          }
        }
      }
      await merge(fresh)
      if (rootPathRef.current === root) {
        nodesRef.current = fresh
        setRootListing(l)
        setRootNodes([...fresh])
      }
    } catch { /* silently ignore */ }

    await Promise.all(tabsRef.current.tabs.map(async t => {
      const gen = t.gen  // a save/reload that lands while this read is in flight makes it stale
      try {
        const d = await fs.read(t.path)
        dispatch({ type: 'refreshed', path: t.path, doc: toSnapshot(d, writable), gen })
      } catch (e) {
        if ((e as { status?: number }).status === 404) {
          dispatch({ type: 'refreshed', path: t.path, gen, doc: { content: '', rev: '', editable: false, lang: '', size: 0, error: 'file no longer exists' } })
        }
      }
    }))
  }, [fs, writable])

  const refreshFnRef = useRef(refresh)
  useEffect(() => {
    refreshFnRef.current = refresh
    if (refreshRef) refreshRef.current = refresh
  }, [refresh, refreshRef])

  // ── Navigation by typed / pasted path ────────────────────────────────────

  const go = useCallback(async (text: string) => {
    const base = rootPathRef.current
    const seq = ++goSeq.current
    setBusy(true)
    setNote('')
    let st: FsStat
    try {
      st = await fs.stat(text, base)
    } catch (e) {
      if (seq === goSeq.current) { setNote(apiErrorMessage(e)); setBusy(false) }
      return
    }
    if (seq !== goSeq.current) return  // a newer paste superseded this one
    setBusy(false)
    if (st.kind === 'dir' && st.path) { await changeRoot(st.path); return }
    if (st.kind === 'file' && st.path) {
      if (!isUnder(st.path, base)) await changeRoot(dirname(st.path))
      openFile(st.path)
      return
    }
    if (st.kind === 'missing' && st.nearest) {
      if (await changeRoot(st.nearest)) setNote(`Not found: ${st.path ?? text.trim()} — showing the nearest folder`)
      return
    }
    const shown = text.trim().split('\n')[0]
    setNote(st.kind === 'denied'
      ? `Outside the folders the explorer may show: ${shown}`
      : `Not found: ${shown}`)
  }, [fs, changeRoot, openFile])

  // spec-079: a file-search hit asks to open a path. The tab may mount WITH the request already
  // set (search switches to Files and asks in one go), so it waits for the tree to be ready and
  // is handled once per nonce.
  useEffect(() => {
    const target = openPath?.path
    if (!target || !ready || handledOpen.get(fs.scope) === openPath.nonce) return
    handledOpen.set(fs.scope, openPath.nonce)
    const abs = target.startsWith('/') ? target : joinPath(infoRef.current?.start ?? rootPathRef.current, target)
    void go(abs)
  }, [openPath?.nonce, openPath?.path, ready, go, fs.scope])

  // ── Edit / save ───────────────────────────────────────────────────────────

  const saveTab = useCallback(async (path: string, force = false) => {
    const t = tabsRef.current.tabs.find(x => x.path === path)
    if (!t || t.draft === null || !fs.write || t.saving) return
    const text = t.draft
    dispatch({ type: 'saveStart', path })
    try {
      const r = await fs.write(path, text, force ? null : t.rev, force)
      dispatch({ type: 'saveOk', path, saved: text, rev: r.rev })
    } catch (e) {
      dispatch({ type: 'saveFail', path, message: apiErrorMessage(e), conflict: (e as { status?: number }).status === 409 })
    }
  }, [fs])

  const reloadTab = useCallback(async (path: string) => {
    try {
      const d = await fs.read(path)
      dispatch({ type: 'reload', path, doc: toSnapshot(d, writable) })
    } catch (e) {
      dispatch({ type: 'saveFail', path, message: apiErrorMessage(e), conflict: false })
    }
  }, [fs, writable])

  const requestClose = useCallback((path: string) => {
    const t = tabsRef.current.tabs.find(x => x.path === path)
    if (t && isDirty(t)) setConfirmClose(path)
    else dispatch({ type: 'close', path })
  }, [])

  // Closing the window with unsaved edits is the one loss we can still warn about.
  const anyDirty = tabs.tabs.some(isDirty)
  useEffect(() => {
    if (!anyDirty) return
    const on = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', on)
    return () => window.removeEventListener('beforeunload', on)
  }, [anyDirty])

  async function copyPath(path: string) {
    try {
      await navigator.clipboard.writeText(path)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = path
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch { /* nothing more to try */ }
      ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1400)
  }

  function toggleTree() {
    if (narrow) setNarrowTree(v => !v)
    else setTreeVisible(v => !v)
  }

  // ── Derived ───────────────────────────────────────────────────────────────

  const activeTab = useMemo(
    () => tabs.tabs.find(t => t.path === tabs.active) ?? null,
    [tabs],
  )
  const home = info?.home ?? ''

  // ─── Render ───────────────────────────────────────────────────────────────

  if (loading) return <Spinner label="Loading files..." />
  if (loadError) return <div className="error-state">⚠ {loadError}</div>
  if (!rootNodes || !rootListing) return null

  const editing = !!activeTab && activeTab.draft !== null
  const dirty = !!activeTab && isDirty(activeTab)

  return (
    <div
      className={`files-explorer${narrow ? ' is-narrow' : ''}`}
      ref={rootElRef}
      tabIndex={-1}
      onPaste={e => {
        // A bare Ctrl+V inside the explorer jumps to the pasted path — but never steals a paste
        // meant for the editor or a text field.
        const t = e.target as HTMLElement
        if (t.closest('input, textarea, [contenteditable="true"]')) return
        const clip = e.clipboardData.getData('text')
        if (looksLikePath(clip)) { e.preventDefault(); void go(clip) }
      }}
      onKeyDown={e => {
        if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
          e.preventDefault()
          toggleTree()
        }
      }}
    >
      <FilePathBar
        crumbs={rootListing.crumbs}
        home={home}
        rootPath={rootPath}
        canUp={!!rootListing.parent}
        treeVisible={showTree}
        busy={busy}
        note={note}
        onGo={t => { void go(t) }}
        onNavigate={p => { void changeRoot(p) }}
        onUp={() => { if (rootListing.parent) void changeRoot(rootListing.parent) }}
        onToggleTree={toggleTree}
        onRefresh={() => { void refresh() }}
      />

      <div className="files-layout" ref={layoutRef}>
        {showTree && (
          <div className="files-tree-pane" style={narrow ? undefined : { width: treeW }}>
            {info && info.roots.length > 1 && (
              <div className="files-roots">
                {info.roots.map(r => (
                  <button
                    key={r.path}
                    className={`files-root-chip${r.path === rootPath ? ' active' : ''}`}
                    title={r.path}
                    onClick={() => { void changeRoot(r.path) }}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            )}
            <div className="files-tree-scroll">
              {rootNodes.length === 0 ? (
                <div className="no-content">Directory is empty</div>
              ) : (
                <TreeView
                  nodes={rootNodes}
                  selectedPath={tabs.active}
                  onFileClick={n => openFile(n.path)}
                  onDirToggle={handleDirToggle}
                />
              )}
              {rootListing.truncated && (
                <div className="no-content">Showing the first entries only — this folder is very large.</div>
              )}
            </div>
          </div>
        )}

        {showTree && !narrow && (
          <SplitHandle
            width={treeW}
            min={TREE_W_MIN}
            minRest={VIEWER_MIN}
            containerRef={layoutRef}
            onChange={setTreeW}
            onReset={() => setTreeW(TREE_W_DEFAULT)}
          />
        )}

        {(!narrow || !showTree) && (
          <div className="files-viewer-pane">
            <FileTabsBar
              tabs={tabs.tabs}
              active={tabs.active}
              onActivate={p => dispatch({ type: 'activate', path: p })}
              onClose={requestClose}
            />

            {!activeTab && (
              <div className="no-content files-viewer-hint">
                {showTree ? '← Select a file' : 'No file open — show the explorer, or paste a path (Ctrl+V).'}
              </div>
            )}

            {activeTab && activeTab.status === 'loading' && <Spinner label="Loading..." />}

            {activeTab && activeTab.status !== 'loading' && (
              <>
                <div className="files-viewer-header">
                  <span className="files-viewer-path" title={activeTab.path}><bdo dir="ltr">{activeTab.path}</bdo></span>
                  <button
                    className="file-edit-btn files-copy-btn"
                    onClick={() => { void copyPath(activeTab.path) }}
                    title="Copy the full path"
                  >
                    {copied ? <Check size={12} /> : <Copy size={12} />}
                  </button>
                  {activeTab.size > 0 && (
                    <span className="files-viewer-size">{formatSize(activeTab.size)}</span>
                  )}
                  {activeTab.status === 'ready' && writable && !activeTab.editable && (
                    <span className="files-viewer-size" title="Not UTF-8 text: the explorer will not rewrite it">read-only</span>
                  )}
                  {activeTab.status === 'ready' && activeTab.editable && !editing && (
                    <button
                      className="file-edit-btn"
                      onClick={() => dispatch({ type: 'startEdit', path: activeTab.path })}
                      title="Edit this file"
                    >
                      ✎ Edit
                    </button>
                  )}
                  {editing && (
                    <div className="file-edit-actions">
                      {activeTab.saveError && !activeTab.conflict && (
                        <span className="file-edit-err">⚠ {activeTab.saveError}</span>
                      )}
                      <button
                        className="btn-primary file-save-btn"
                        onClick={() => { void saveTab(activeTab.path) }}
                        disabled={activeTab.saving || !dirty}
                        title="Save (Ctrl+S)"
                      >
                        {activeTab.saving ? '…' : 'Save'}
                      </button>
                      <button
                        className="btn-secondary"
                        onClick={() => {
                          if (dirty && !window.confirm('Discard your unsaved changes?')) return
                          dispatch({ type: 'cancelEdit', path: activeTab.path })
                        }}
                        disabled={activeTab.saving}
                      >
                        {dirty ? 'Discard' : 'Close editor'}
                      </button>
                    </div>
                  )}
                </div>

                {editing && activeTab.conflict && (
                  <div className="files-banner files-banner-warn" role="alert">
                    <span>Not saved — the file changed on disk after you opened it.</span>
                    <button className="btn-secondary" onClick={() => { void saveTab(activeTab.path, true) }}>Overwrite</button>
                    <button className="btn-secondary" onClick={() => { void reloadTab(activeTab.path) }}>Reload (drop mine)</button>
                  </div>
                )}
                {editing && !activeTab.conflict && activeTab.diskChanged && (
                  <div className="files-banner files-banner-warn" role="alert">
                    <span>This file changed on disk while you were editing.</span>
                    <button className="btn-secondary" onClick={() => { void reloadTab(activeTab.path) }}>Reload (drop mine)</button>
                    <button className="btn-secondary" onClick={() => dispatch({ type: 'keepMine', path: activeTab.path })}>Keep mine</button>
                  </div>
                )}

                <div className={`files-viewer-body${editing ? ' files-viewer-editing' : ''}`}>
                  {activeTab.status === 'error' ? (
                    <div className="error-state">⚠ {activeTab.error}</div>
                  ) : editing ? (
                    <textarea
                      key={activeTab.path}
                      className="file-edit-textarea"
                      value={activeTab.draft ?? ''}
                      onChange={e => dispatch({ type: 'setDraft', path: activeTab.path, text: e.target.value })}
                      onKeyDown={e => {
                        if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void saveTab(activeTab.path) }
                        else if (e.key === 'Escape' && !dirty) dispatch({ type: 'cancelEdit', path: activeTab.path })
                      }}
                      autoFocus
                      spellCheck={false}
                    />
                  ) : activeTab.lang === 'md' ? (
                    <div className="markdown-wrap">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{activeTab.content}</ReactMarkdown>
                    </div>
                  ) : (
                    <pre className="files-code-block"><code>{activeTab.content}</code></pre>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {confirmClose && (
        <ConfirmModal
          title="Discard unsaved changes?"
          message={`${baseName(confirmClose)} has changes that are not saved.`}
          confirmLabel="Discard"
          danger
          onConfirm={() => { dispatch({ type: 'close', path: confirmClose }); setConfirmClose(null) }}
          onCancel={() => setConfirmClose(null)}
        />
      )}
    </div>
  )
}
