import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { SearchHit } from '../types'
import { t } from '../i18n'
import { Modal } from './Modal'

interface Props {
  /** spec-079: hands the whole hit back — App routes by source (chat → transcript peek,
   *  board → board tab + card focus, timeline → timeline tab). The overlay stays dumb. */
  onPick: (hit: SearchHit) => void
  onClose: () => void
  /** Pre-filled query, e.g. handed over from the sidebar's project filter. */
  initialQuery?: string
}

/** Compact, locale-aware date for a hit row. Index ts is epoch SECONDS; 0 = unknown. */
function hitDate(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const sameYear = d.getFullYear() === now.getFullYear()
  return d.toLocaleDateString(undefined, sameYear
    ? { day: 'numeric', month: 'short' }
    : { day: 'numeric', month: 'short', year: 'numeric' })
}

const SOURCE_ICON: Record<SearchHit['source'], string> = { chat: '💬', board: '📋', timeline: '🕓', file: '📄' }
const SOURCE_LABEL_KEY: Record<SearchHit['source'],
  'search.source_chat' | 'search.source_board' | 'search.source_timeline' | 'search.source_file'> = {
  chat: 'search.source_chat',
  board: 'search.source_board',
  timeline: 'search.source_timeline',
  file: 'search.source_file',
}

// Backend snippet() delimiters (search.py: SNIPPET_OPEN/SNIPPET_CLOSE) — private-use
// control chars, never literal HTML. Rendered as a real <mark> so nothing here ever
// needs dangerouslySetInnerHTML, regardless of what a document's own text contains.
const MARK_OPEN = '\x01'
const MARK_CLOSE = '\x02'

function renderSnippet(raw: string) {
  if (!raw.includes(MARK_OPEN)) return raw
  const nodes: React.ReactNode[] = []
  const segments = raw.split(MARK_OPEN)
  segments.forEach((seg, i) => {
    if (i === 0) {
      if (seg) nodes.push(seg)
      return
    }
    const closeIdx = seg.indexOf(MARK_CLOSE)
    if (closeIdx === -1) {
      nodes.push(seg)
      return
    }
    nodes.push(<mark key={i}>{seg.slice(0, closeIdx)}</mark>)
    const rest = seg.slice(closeIdx + MARK_CLOSE.length)
    if (rest) nodes.push(rest)
  })
  return nodes
}

interface Group {
  project_id: string
  project_name: string
  hits: SearchHit[]
}

export function SearchOverlay({ onPick, onClose, initialQuery = '' }: Props) {
  const [query, setQuery] = useState(initialQuery)
  const [hits, setHits] = useState<SearchHit[]>([])
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const reqIdRef = useRef(0)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Debounced search (250ms)
  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setHits([])
      setLoading(false)
      return
    }
    setLoading(true)
    const myReq = ++reqIdRef.current
    const timer = window.setTimeout(() => {
      api.search(q)
        .then(res => {
          if (reqIdRef.current !== myReq) return
          setHits(res.hits)
          setSelected(0)
        })
        .catch(() => {
          if (reqIdRef.current !== myReq) return
          setHits([])
        })
        .finally(() => {
          if (reqIdRef.current !== myReq) return
          setLoading(false)
        })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [query])

  // Group hits by project, preserving the server's relevance order (first-seen project wins position)
  const groups = useMemo<Group[]>(() => {
    const out: Group[] = []
    const idxByProject = new Map<string, number>()
    for (const h of hits) {
      let i = idxByProject.get(h.project_id)
      if (i === undefined) {
        i = out.length
        idxByProject.set(h.project_id, i)
        out.push({ project_id: h.project_id, project_name: h.project_name, hits: [] })
      }
      out[i].hits.push(h)
    }
    return out
  }, [hits])

  function navigateTo(hit: SearchHit) {
    onPick(hit)
    onClose()
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!hits.length) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSelected(s => Math.min(s + 1, hits.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSelected(s => Math.max(s - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const hit = hits[selected]
      if (hit) navigateTo(hit)
    }
  }

  let flatIdx = -1

  return (
    <Modal onClose={onClose} className="search-overlay-modal">
      <div className="search-overlay" onKeyDown={onKeyDown}>
        <div className="search-overlay-input-row">
          <span className="search-overlay-icon" aria-hidden="true">🔍</span>
          <input
            ref={inputRef}
            className="search-overlay-input"
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={t['search.placeholder']}
          />
          {loading && <span className="search-overlay-spinner" aria-hidden="true" />}
        </div>
        <div className="search-overlay-results">
          {!query.trim() && (
            <div className="search-overlay-hint">{t['search.empty_hint']}</div>
          )}
          {!!query.trim() && !loading && hits.length === 0 && (
            <div className="search-overlay-hint">{t['search.no_results']}</div>
          )}
          {groups.map(g => (
            <div key={g.project_id} className="search-overlay-group">
              <div className="search-overlay-group-title">{g.project_name}</div>
              {g.hits.map(h => {
                flatIdx += 1
                const isSelected = flatIdx === selected
                const rowIdx = flatIdx
                return (
                  <div
                    key={`${h.project_id}-${h.source}-${rowIdx}`}
                    className={`search-overlay-hit${isSelected ? ' selected' : ''}`}
                    onMouseEnter={() => setSelected(rowIdx)}
                    onClick={() => navigateTo(h)}
                  >
                    <span className="search-overlay-hit-icon" title={t[SOURCE_LABEL_KEY[h.source]]}>
                      {SOURCE_ICON[h.source] ?? '•'}
                    </span>
                    <span className="search-overlay-hit-main">
                      <span className="search-overlay-hit-snippet">{renderSnippet(h.snippet)}</span>
                      <span className="search-overlay-hit-meta">
                        <span>{h.source === 'file' && h.ref.path
                          ? `${h.ref.path}${h.ref.line ? `:${h.ref.line}` : ''}`
                          : t[SOURCE_LABEL_KEY[h.source]]}</span>
                        {hitDate(h.ts) && <span>· {hitDate(h.ts)}</span>}
                      </span>
                    </span>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </div>
    </Modal>
  )
}
