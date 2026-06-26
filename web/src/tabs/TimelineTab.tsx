/**
 * TimelineTab — chronological event feed for a project (Spec 008).
 *
 * History is loaded via GET /api/projects/{id}/timeline.
 * Live events are received through the existing activity-stream SSE (useProjectActivity).
 * Pagination: "Load earlier" button fetches events before=<oldest_ts>.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { TimelineEvent, RichTool } from '../types'
import { Spinner } from '../components/Spinner'
import { useProjectActivity } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
}

// ── Event icon by kind ────────────────────────────────────────────────────────

function kindIcon(evt: TimelineEvent): string {
  switch (evt.kind) {
    case 'run_start': return '▶'
    case 'run_end':   return evt.outcome === 'ok' ? '✅' : '❌'
    case 'tool':      return '🔧'
    case 'text':      return '💬'
    case 'session_rotated':
    case 'auto_rotated': return '🔄'
    default:          return '•'
  }
}

// ── Session-rotation row: human-readable "when & why" ─────────────────────────
function rotationDescription(evt: TimelineEvent): string {
  const fmtK = (n?: number) => (n ? `${Math.round(n / 1000)}K` : '—')
  // Historical 'auto_rotated' records predate the `trigger` field — treat them as auto.
  const isAuto = evt.kind === 'auto_rotated' || evt.trigger === 'auto'
  const base = isAuto
    ? t['timeline.event_rotated_auto']
        .replace('{ctx}', fmtK(evt.context_tokens))
        .replace('{cap}', fmtK(evt.threshold))
    : t['timeline.event_rotated_manual']
  return base + (evt.handoff ? t['timeline.rotated_handoff'] : t['timeline.rotated_no_handoff'])
}

// ── Event description ─────────────────────────────────────────────────────────

function eventDescription(evt: TimelineEvent): string {
  switch (evt.kind) {
    case 'run_start': {
      const p = evt.prompt ?? ''
      return p.length > 120 ? p.slice(0, 120) + '…' : p
    }
    case 'run_end':
      return evt.outcome === 'ok' ? t['timeline.event_run_end_ok'] : t['timeline.event_run_end_fail']
    case 'text': {
      const txt = evt.text ?? ''
      return txt.length > 200 ? txt.slice(0, 200) + '…' : txt
    }
    case 'tool': {
      const tool = evt.tool as RichTool | undefined
      if (!tool) return t['timeline.event_tool']
      switch (tool.kind) {
        case 'bash':   return `Bash: ${(tool.cmd ?? '').slice(0, 120)}`
        case 'edit':   return `Edit: ${tool.file}`
        case 'write':  return `Write: ${tool.file}`
        case 'read':   return `Read: ${tool.file}`
        case 'search': return `${tool.name}: ${tool.pattern}`
        default:       return tool.name
      }
    }
    case 'session_rotated':
    case 'auto_rotated':
      return rotationDescription(evt)
    default:
      return evt.kind
  }
}

// ── Timestamp ─────────────────────────────────────────────────────────────────

function formatTs(ts: number): string {
  const d = new Date(ts * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) {
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }
  return d.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

// ── Single event row ─────────────────────────────────────────────────────────

interface EventRowProps {
  evt: TimelineEvent
  isLive?: boolean
}

function EventRow({ evt, isLive }: EventRowProps) {
  const icon = kindIcon(evt)
  const desc = eventDescription(evt)
  const ts = formatTs(evt.ts)
  const runId = evt.run_id && evt.run_id !== '-' ? evt.run_id : null

  return (
    <div
      className={`timeline-row timeline-row--${evt.kind}`}
      role="listitem"
    >
      <span className="timeline-icon" aria-hidden="true">{icon}</span>
      <span className="timeline-ts" title={new Date(evt.ts * 1000).toISOString()}>{ts}</span>
      <span className="timeline-desc">{desc}</span>
      {runId && (
        <span className="timeline-run-id" title={`run_id: ${runId}`}>
          #{runId.slice(0, 6)}
        </span>
      )}
      {isLive && (
        <span className="timeline-live-badge" aria-label={t['timeline.live_badge']}>
          {t['timeline.live_badge']}
        </span>
      )}
    </div>
  )
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export function TimelineTab({ projectId }: Props) {
  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  // Track which event IDs are "live" (just arrived via SSE) for a brief highlight
  const [liveIds, setLiveIds] = useState<Set<string>>(new Set())
  const bottomRef = useRef<HTMLDivElement>(null)
  const LIMIT = 200

  // ── Load initial history ──────────────────────────────────────────────────

  const loadHistory = useCallback(async (before?: number, prepend = false) => {
    try {
      const res = await api.timeline(projectId, { limit: LIMIT, before })
      const incoming = res.events
      if (prepend) {
        setEvents(prev => {
          // De-duplicate by ts+session_key+kind
          const existingKeys = new Set(prev.map(e => `${e.ts}:${e.kind}:${e.run_id ?? ''}`))
          const fresh = incoming.filter(e => !existingKeys.has(`${e.ts}:${e.kind}:${e.run_id ?? ''}`))
          return [...fresh, ...prev]
        })
        setHasMore(incoming.length === LIMIT)
      } else {
        setEvents(incoming)
        setHasMore(incoming.length === LIMIT)
      }
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    }
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setEvents([])
    setHasMore(true)

    api.timeline(projectId, { limit: LIMIT })
      .then(res => {
        if (!cancelled) {
          setEvents(res.events)
          setHasMore(res.events.length === LIMIT)
          setLoading(false)
        }
      })
      .catch(e => {
        if (!cancelled) { setError(String(e instanceof Error ? e.message : e)); setLoading(false) }
      })

    return () => { cancelled = true }
  }, [projectId])

  // Auto-scroll to bottom when new events appear at initial load
  useEffect(() => {
    if (!loading) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [loading])

  // ── Live events via existing SSE ──────────────────────────────────────────

  useProjectActivity(evt => {
    // Compose a TimelineEvent from the ActivityEvent.
    // ActivityEventSubagent.run_id may be null (not undefined) — normalize to avoid TS error.
    const evtNormalized = { ...evt, run_id: (evt as { run_id?: string | null }).run_id ?? undefined }
    const te: TimelineEvent = {
      ts: Date.now() / 1000,
      session_key: '',  // not known from SSE, ok
      ...evtNormalized,
    }
    const liveKey = `${te.ts}:${te.kind}:${te.run_id ?? ''}`
    setEvents(prev => [...prev, te])
    setLiveIds(prev => {
      const next = new Set(prev)
      next.add(liveKey)
      return next
    })
    // Clear live highlight after 4s
    setTimeout(() => {
      setLiveIds(prev => {
        const next = new Set(prev)
        next.delete(liveKey)
        return next
      })
    }, 4000)
    // Scroll to bottom
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  })

  // ── Load earlier ──────────────────────────────────────────────────────────

  async function loadEarlier() {
    if (loadingEarlier || events.length === 0) return
    const oldest = events[0].ts
    setLoadingEarlier(true)
    await loadHistory(oldest, true)
    setLoadingEarlier(false)
  }

  // ── Render ────────────────────────────────────────────────────────────────

  if (loading) return <Spinner label={t['timeline.loading']} />
  if (error) return <div className="error-state">⚠ {error}</div>

  return (
    <div className="timeline-pane" role="log" aria-label="Project event feed" aria-live="polite">
      {/* Load earlier button */}
      {hasMore && (
        <div className="timeline-load-earlier">
          <button
            className="doc-btn ghost"
            onClick={loadEarlier}
            disabled={loadingEarlier}
            aria-label={t['timeline.load_earlier_aria']}
          >
            {loadingEarlier ? t['timeline.loading_earlier'] : t['timeline.load_earlier']}
          </button>
        </div>
      )}

      {/* Empty state */}
      {events.length === 0 && (
        <div className="timeline-empty">
          <div className="timeline-empty-icon" aria-hidden="true">🕒</div>
          <div className="timeline-empty-title">{t['timeline.empty_title']}</div>
          <p className="timeline-empty-text">{t['timeline.empty_text']}</p>
        </div>
      )}

      {/* Events list */}
      {events.length > 0 && (
        <div className="timeline-list" role="list">
          {events.map((evt, i) => {
            const liveKey = `${evt.ts}:${evt.kind}:${evt.run_id ?? ''}`
            return (
              <EventRow
                key={`${evt.ts}-${i}`}
                evt={evt}
                isLive={liveIds.has(liveKey)}
              />
            )
          })}
        </div>
      )}

      {/* Scroll anchor */}
      <div ref={bottomRef} aria-hidden="true" />
    </div>
  )
}
