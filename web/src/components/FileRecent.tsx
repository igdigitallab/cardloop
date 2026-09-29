/** The explorer's "Recent" list: files the agent just wrote, and files that just changed. */
import { fileIcon } from '../lib/fileIcons'
import { dirname, displayPath, formatAgo } from '../lib/fsPath'
import type { FsRecent } from '../types'

interface Props {
  items: FsRecent[] | null
  error: string
  home: string
  activePath: string | null
  onOpen: (path: string) => void
}

export function FileRecent({ items, error, home, activePath, onOpen }: Props) {
  if (error) return <div className="no-content">⚠ {error}</div>
  if (!items) return <div className="no-content">Loading…</div>
  if (items.length === 0) return <div className="no-content">Nothing written or changed in the last two days.</div>
  const now = Date.now()
  return (
    <div className="files-recent" role="listbox" aria-label="Recent files">
      {items.map(it => {
        const Icon = fileIcon(it.name)
        return (
          <div
            key={it.path}
            className={`files-recent-row${activePath === it.path ? ' active' : ''}`}
            role="option"
            aria-selected={activePath === it.path}
            tabIndex={0}
            title={it.path}
            onClick={() => onOpen(it.path)}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(it.path) } }}
          >
            <Icon size={13} className="files-recent-icon" aria-hidden="true" />
            <span className="files-recent-text">
              <span className="files-recent-name">{it.name}</span>
              <span className="files-recent-dir"><bdo dir="ltr">{displayPath(dirname(it.path), home)}</bdo></span>
            </span>
            <span
              className={`files-recent-meta${it.src === 'agent' ? ' agent' : ''}`}
              title={it.src === 'agent' ? `written by the agent (${it.tool})` : 'changed on disk'}
            >
              {it.src === 'agent' ? '✎ ' : ''}{formatAgo(it.t, now)}
            </span>
          </div>
        )
      })}
    </div>
  )
}
