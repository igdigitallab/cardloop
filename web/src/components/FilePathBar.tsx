/**
 * Toolbar of the Files explorer: hide/show the tree, up one folder, the path bar, paste-and-go.
 *
 * The path bar is breadcrumbs until clicked, then a text field holding the full path. Pasting
 * over a fully-selected field (the state a click leaves it in) jumps at once — the whole point
 * is turning a path an agent printed into a tap and a Ctrl+V.
 */
import { useEffect, useRef, useState } from 'react'
import { ArrowUp, ClipboardPaste, PanelLeftClose, PanelLeftOpen, RefreshCw } from 'lucide-react'
import type { FsCrumb } from '../types'

interface Props {
  crumbs: FsCrumb[]
  home: string
  rootPath: string
  canUp: boolean
  treeVisible: boolean
  busy: boolean
  note: string
  onGo: (text: string) => void
  onNavigate: (path: string) => void
  onUp: () => void
  onToggleTree: () => void
  onRefresh: () => void
}

export function FilePathBar({
  crumbs, home, rootPath, canUp, treeVisible, busy, note,
  onGo, onNavigate, onUp, onToggleTree, onRefresh,
}: Props) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const crumbsRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  // Long paths overflow to the left: keep the current folder in view.
  useEffect(() => {
    const el = crumbsRef.current
    if (el) el.scrollLeft = el.scrollWidth
  }, [crumbs, editing])

  function startEditing() {
    setText(rootPath)
    setEditing(true)
  }

  function submit(value: string) {
    setEditing(false)
    if (value.trim()) onGo(value)
  }

  async function pasteAndGo() {
    try {
      const clip = await navigator.clipboard.readText()
      if (clip.trim()) { onGo(clip); return }
    } catch { /* no clipboard permission or insecure context — fall through to manual paste */ }
    startEditing()
  }

  const visible = crumbs.filter(c => c.ok)

  return (
    <>
      <div className="files-toolbar">
        <button
          className="files-tb-btn"
          onClick={onToggleTree}
          aria-pressed={treeVisible}
          title={treeVisible ? 'Hide explorer (Ctrl+B)' : 'Show explorer (Ctrl+B)'}
        >
          {treeVisible ? <PanelLeftClose size={15} /> : <PanelLeftOpen size={15} />}
        </button>
        <button className="files-tb-btn" onClick={onUp} disabled={!canUp} title="Up one folder">
          <ArrowUp size={15} />
        </button>

        <div
          className={`files-pathbar${editing ? ' editing' : ''}`}
          onClick={e => { if (!editing && e.target === e.currentTarget) startEditing() }}
        >
          {editing ? (
            <input
              ref={inputRef}
              className="files-path-input"
              value={text}
              autoFocus
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              aria-label="Path"
              placeholder="Paste a path and press Enter"
              onChange={e => setText(e.target.value)}
              onBlur={() => setEditing(false)}
              onKeyDown={e => {
                if (e.key === 'Enter') { e.preventDefault(); submit(text) }
                else if (e.key === 'Escape') { e.preventDefault(); setEditing(false) }
              }}
              onPaste={e => {
                const el = e.currentTarget
                const whole = el.selectionStart === 0 && el.selectionEnd === el.value.length
                const clip = e.clipboardData.getData('text')
                if (clip.trim() && (whole || !el.value)) { e.preventDefault(); submit(clip) }
              }}
            />
          ) : (
            <nav className="files-crumbs" ref={crumbsRef} aria-label="Current folder">
              {visible.map((c, i) => (
                <span key={c.path} className="files-crumb-wrap">
                  {i > 0 && <span className="files-crumb-sep" aria-hidden="true">›</span>}
                  <button
                    className={`files-crumb${i === visible.length - 1 ? ' current' : ''}`}
                    onClick={() => onNavigate(c.path)}
                    title={c.path}
                  >
                    {c.path === home ? '~' : c.name}
                  </button>
                </span>
              ))}
              <button className="files-crumb-edit" onClick={startEditing} title="Type or paste a path" aria-label="Edit path">
                {visible.length === 0 ? rootPath : ' '}
              </button>
            </nav>
          )}
        </div>

        <button className="files-tb-btn" onClick={pasteAndGo} title="Paste a path from the clipboard and go">
          <ClipboardPaste size={15} />
        </button>
        <button className="files-tb-btn" onClick={onRefresh} disabled={busy} title="Refresh">
          <RefreshCw size={14} className={busy ? 'files-spin' : undefined} />
        </button>
      </div>
      {note && <div className="files-note" role="status">{note}</div>}
    </>
  )
}
