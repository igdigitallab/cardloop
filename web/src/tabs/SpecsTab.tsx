import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { mdComponents } from '../components/markdown'
import { api } from '../api'
import { EpicSpec, EpicSpecsResp } from '../types'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
}

// ── Inline progress bar (no shared component) ─────────────────────────────────

interface ProgressBarProps {
  pct: number
}

function ProgressBar({ pct }: ProgressBarProps) {
  return (
    <div
      style={{
        height: 4,
        background: 'var(--bg3)',
        borderRadius: 'var(--radius-pill)',
        overflow: 'hidden',
        flex: 1,
        minWidth: 40,
      }}
    >
      <div
        style={{
          width: `${Math.min(100, Math.max(0, pct * 100))}%`,
          height: '100%',
          background: pct >= 1 ? 'var(--green)' : 'var(--accent)',
          transition: 'width var(--transition)',
        }}
      />
    </div>
  )
}

// ── Sort specs by spec_id numeric descending (newest first) ───────────────────

function sortSpecsDesc(specs: EpicSpec[]): EpicSpec[] {
  return [...specs].sort((a, b) => {
    const na = parseInt(a.spec_id, 10)
    const nb = parseInt(b.spec_id, 10)
    if (!isNaN(na) && !isNaN(nb)) return nb - na
    return b.spec_id.localeCompare(a.spec_id)
  })
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export function SpecsTab({ projectId }: Props) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [specs, setSpecs] = useState<EpicSpec[]>([])

  // Selected spec (name key)
  const [selected, setSelected] = useState<string | null>(null)

  // Right-pane content
  const [content, setContent] = useState('')
  const [contentLoading, setContentLoading] = useState(false)

  // Expanded card sets (keyed by spec name)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const reload = useCallback(() => {
    api.epicSpecs(projectId).then((data: EpicSpecsResp) => {
      setSpecs(sortSpecsDesc(data.specs))
      setError('')
    }).catch(e => setError(String(e.message || e)))
  }, [projectId])

  // Initial load with cancellation
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setSpecs([])
    setSelected(null)
    setContent('')
    setExpanded(new Set())

    api.epicSpecs(projectId).then((data: EpicSpecsResp) => {
      if (!cancelled) {
        setSpecs(sortSpecsDesc(data.specs))
        setLoading(false)
      }
    }).catch(e => {
      if (!cancelled) {
        setError(String(e.message || e))
        setLoading(false)
      }
    })

    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  // Fetch content when selection changes
  const contentCancelRef = useRef<boolean>(false)
  useEffect(() => {
    if (!selected) { setContent(''); return }
    contentCancelRef.current = false
    setContentLoading(true)
    setContent('')

    api.epicSpecContent(projectId, selected).then(data => {
      if (!contentCancelRef.current) {
        setContent(data.content)
        setContentLoading(false)
      }
    }).catch(() => {
      if (!contentCancelRef.current) {
        setContent('')
        setContentLoading(false)
      }
    })

    return () => { contentCancelRef.current = true }
  }, [projectId, selected])

  function toggleExpand(name: string) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  // ── States ───────────────────────────────────────────────────────────────────

  if (loading) return <Spinner label={t['specs.loading']} />
  if (error) return <div className="error-state">⚠ {error}</div>
  if (specs.length === 0) {
    return (
      <div className="memory-empty">
        <div className="memory-empty-title">{t['specs.empty_title']}</div>
        <p className="memory-empty-text">{t['specs.empty_text']}</p>
      </div>
    )
  }

  // ── Two-pane layout ──────────────────────────────────────────────────────────

  return (
    <div className="specs-layout">
      {/* LEFT pane — epic rows */}
      <div className="specs-list">
        <div className="specs-list-label">Specs</div>

        {specs.map(spec => {
          const isSelected = selected === spec.name
          const isExpanded = expanded.has(spec.name)
          const hasCards = spec.total > 0
          const shortName = spec.name.replace(/\.md$/, '')

          return (
            <div key={spec.name}>
              {/* Row */}
              <div
                className={`spec-item ${isSelected ? 'active' : ''}`}
                onClick={() => setSelected(spec.name)}
                style={{ flexDirection: 'column', alignItems: 'stretch', gap: 4, userSelect: 'none' }}
              >
                {/* Top line: toggle + name + status */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  {/* Expand toggle — only when there are linked cards */}
                  <span
                    style={{
                      fontSize: 10,
                      color: 'var(--text3)',
                      cursor: hasCards ? 'pointer' : 'default',
                      opacity: hasCards ? 1 : 0,
                      flexShrink: 0,
                      width: 12,
                      textAlign: 'center',
                    }}
                    onClick={hasCards ? (e) => { e.stopPropagation(); toggleExpand(spec.name) } : undefined}
                  >
                    {isExpanded ? '▾' : '▸'}
                  </span>

                  {/* Spec name */}
                  <span
                    style={{
                      flex: 1,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      fontSize: 13,
                    }}
                    title={spec.title}
                  >
                    {shortName}
                  </span>

                  {/* Status badge */}
                  {spec.status && (
                    <span
                      style={{
                        fontSize: 10,
                        color: 'var(--text3)',
                        flexShrink: 0,
                        maxWidth: 120,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      title={spec.status}
                    >
                      {spec.status}
                    </span>
                  )}
                </div>

                {/* Progress row — only when total > 0 */}
                {hasCards && (
                  <div
                    style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 18 }}
                    onClick={e => e.stopPropagation()}
                  >
                    <ProgressBar pct={spec.progress} />
                    <span style={{ fontSize: 10, color: 'var(--text3)', flexShrink: 0, whiteSpace: 'nowrap' }}>
                      {spec.done_count}/{spec.total} {t['specs.progress_label']}
                    </span>
                  </div>
                )}

                {/* No-cards hint */}
                {!hasCards && (
                  <span style={{ fontSize: 10, color: 'var(--text3)', paddingLeft: 18 }}>
                    {t['specs.no_cards']}
                  </span>
                )}
              </div>

              {/* Expanded card list */}
              {isExpanded && hasCards && (
                <div style={{ paddingLeft: 24, paddingBottom: 4 }}>
                  {spec.cards.open.map(card => (
                    <div
                      key={card.id}
                      style={{ fontSize: 11, color: 'var(--text2)', padding: '1px 0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                      title={card.text}
                    >
                      {card.column ? `[${card.column}] ` : ''}{card.text}
                    </div>
                  ))}
                  {spec.cards.done.map(card => (
                    <div
                      key={card.id}
                      style={{ fontSize: 11, color: 'var(--text3)', padding: '1px 0', textDecoration: 'line-through', opacity: 0.6, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                      title={card.text}
                    >
                      {card.text}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* RIGHT pane — spec content */}
      <div className="spec-content">
        {!selected && (
          <div className="no-content" style={{ paddingTop: 4 }}>
            {t['specs.select']}
          </div>
        )}
        {selected && contentLoading && (
          <Spinner label={t['specs.loading']} />
        )}
        {selected && !contentLoading && content && (
          <div className="markdown-wrap">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  )
}
