import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { SearchHit } from '../types'
import { t } from '../i18n'
import { Modal } from './Modal'

interface Props {
  /** Navigates to a project's chat tab — reuses App.tsx's existing project-switch handler. */
  onNavigate: (projectId: string) => void
  onClose: () => void
}

const SOURCE_ICON: Record<SearchHit['source'], string> = { chat: '💬', board: '📋', timeline: '🕓' }
const SOURCE_LABEL_KEY: Record<SearchHit['source'], 'search.source_chat' | 'search.source_board' | 'search.source_timeline'> = {
  chat: 'search.source_chat',
  board: 'search.source_board',
  timeline: 'search.source_timeline',
}

// Backend snippet() delimiters (search.py: SNIPPET_OPEN/SNIPPET_CLOSE) — private-use
// control chars, never literal HTML. Rendered as a real <mark> so nothing here ever
// needs dangerouslySetInnerHTML, regardless of what a document's own text contains.
const MARK_OPEN = ''
const MARK_CLOSE = ''

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

export function SearchOverlay({ onNavigate, onClose }: Props) {
  const [query, setQuery] = useState('')
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
    onNavigate(hit.project_id)
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
                    <span className="search-overlay-hit-snippet">{renderSnippet(h.snippet)}</span>
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
