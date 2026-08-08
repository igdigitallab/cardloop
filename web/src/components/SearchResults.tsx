/* eslint-disable react-refresh/only-export-components -- hit helpers intentionally share the renderer module */
/**
 * spec-079 — shared rendering for global search hits.
 *
 * Extracted from the old Cmd+K overlay so the SIDEBAR search field can render results
 * inline. That field is the single search entry point: the operator works mostly from
 * the mobile app, where keyboard shortcuts do not exist and a modal behind an icon in a
 * drawer is effectively invisible. One field, results underneath, no extra tap.
 */
import { SearchHit } from '../types'
import { t } from '../i18n'

const SOURCE_ICON: Record<SearchHit['source'], string> = {
  chat: '💬', board: '📋', timeline: '🕓', file: '📄',
}
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

export function renderSnippet(raw: string) {
  if (!raw.includes(MARK_OPEN)) return raw
  const nodes: React.ReactNode[] = []
  raw.split(MARK_OPEN).forEach((seg, i) => {
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

/** Compact, locale-aware date. Index ts is epoch SECONDS; 0 = unknown. */
function hitDate(ts: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleDateString(undefined, sameYear
    ? { day: 'numeric', month: 'short' }
    : { day: 'numeric', month: 'short', year: 'numeric' })
}

/** The identifying line under a snippet: a file shows its path, everything else its kind. */
function hitMeta(h: SearchHit): string {
  if (h.source === 'file' && h.ref.path) {
    return h.ref.line ? `${h.ref.path}:${h.ref.line}` : h.ref.path
  }
  return t[SOURCE_LABEL_KEY[h.source]]
}

interface Props {
  hits: SearchHit[]
  loading: boolean
  query: string
  onPick: (hit: SearchHit) => void
  /** Shown above the list; omitted when the caller renders its own heading. */
  showProjectNames?: boolean
}

export function SearchResults({ hits, loading, query, onPick, showProjectNames = true }: Props) {
  if (!query.trim()) return null
  if (!hits.length) {
    return (
      <div className="search-results-hint">
        {loading ? t['search.searching'] : t['search.no_results']}
      </div>
    )
  }
  return (
    <div className="search-results">
      {hits.map((h, i) => (
        <button
          key={`${h.project_id}-${h.source}-${i}`}
          className="search-result-row"
          onClick={() => onPick(h)}
        >
          <span className="search-result-icon" aria-hidden="true">{SOURCE_ICON[h.source] ?? '•'}</span>
          <span className="search-result-main">
            <span className="search-result-snippet">{renderSnippet(h.snippet)}</span>
            <span className="search-result-meta">
              {showProjectNames && <span className="search-result-project">{h.project_name}</span>}
              <span>{hitMeta(h)}</span>
              {hitDate(h.ts) && <span>· {hitDate(h.ts)}</span>}
            </span>
          </span>
        </button>
      ))}
    </div>
  )
}
