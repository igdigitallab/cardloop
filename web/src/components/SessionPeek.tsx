import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { HistoryMessage, SessionPeekTarget } from '../types'
import { t } from '../i18n'
import { mdComponents } from './markdown'
import { Modal } from './Modal'

interface Props {
  target: SessionPeekTarget
  onClose: () => void
  /** "Open in chat" — hands the project id back to the app's project-switch handler. */
  onOpenProject: (projectId: string) => void
}

/**
 * spec-079 — read-only transcript peek for a chat search hit.
 *
 * Deliberately NOT a detour through ChatTab: switching the live chat to a historical
 * session either mutates the active binding (POST /session {action:'resume'}, which 409s
 * while a run is in flight) or leaves the feed showing session A while typing still goes
 * to session B. Reading is what a search result is for, so this reads — and offers an
 * explicit "open the project" escape hatch for when the operator wants to act on it.
 *
 * The window is centred server-side on the matched message (`around_uuid` / `around_ts`),
 * so a hit thousands of messages deep in a transcript is reachable.
 */
export function SessionPeek({ target, onClose, onOpenProject }: Props) {
  const [messages, setMessages] = useState<HistoryMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setFailed(false)
    api.sessionHistory(target.projectId, target.sessionId, {
      uuid: target.uuid,
      // The index stores epoch SECONDS; the feed speaks epoch MILLISECONDS.
      ts: target.ts ? target.ts * 1000 : undefined,
    }, target.codexThreadId)
      .then(res => {
        if (cancelled) return
        setMessages(res.messages || [])
      })
      .catch(() => { if (!cancelled) setFailed(true) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [target.projectId, target.sessionId, target.codexThreadId, target.uuid, target.ts])

  // Which rendered message is the hit? uuid is exact but only user messages carry one, so
  // an assistant hit resolves to the message with the closest timestamp.
  const anchorIdx = useMemo(() => {
    if (!messages.length) return -1
    if (target.uuid) {
      const i = messages.findIndex(m => m.uuid === target.uuid)
      if (i >= 0) return i
    }
    if (target.ts) {
      const wantMs = target.ts * 1000
      let best = -1
      let bestDelta = Infinity
      messages.forEach((m, i) => {
        if (m.ts == null) return
        const d = Math.abs(m.ts - wantMs)
        if (d < bestDelta) { bestDelta = d; best = i }
      })
      return best
    }
    return -1
  }, [messages, target.uuid, target.ts])

  // Scroll the anchor into view once the window has rendered.
  useEffect(() => {
    if (loading || anchorIdx < 0) return
    anchorRef.current?.scrollIntoView({ block: 'center' })
  }, [loading, anchorIdx])

  return (
    <Modal onClose={onClose} className="session-peek-modal">
      <div className="session-peek">
        <div className="session-peek-head">
          <div className="session-peek-title">
            <span className="session-peek-project">{target.projectName}</span>
            <span className="session-peek-sub">{t['search.peek_subtitle']}</span>
          </div>
          <div className="session-peek-actions">
            <button
              className="session-peek-open"
              onClick={() => { onOpenProject(target.projectId); onClose() }}
            >
              {t['search.peek_open_project']}
            </button>
            <button className="session-peek-close" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </div>

        <div className="session-peek-body">
          {loading && <div className="session-peek-hint">{t['search.peek_loading']}</div>}
          {!loading && failed && <div className="session-peek-hint">{t['search.peek_failed']}</div>}
          {!loading && !failed && messages.length === 0 && (
            <div className="session-peek-hint">{t['search.peek_empty']}</div>
          )}
          {!loading && messages.map((m, i) => (
            <div
              key={i}
              ref={i === anchorIdx ? anchorRef : undefined}
              className={`session-peek-msg session-peek-${m.role}${i === anchorIdx ? ' session-peek-hit' : ''}`}
            >
              <div className="session-peek-role">
                {m.role === 'user' ? t['search.peek_you'] : t['search.peek_agent']}
                {m.ts != null && (
                  <span className="session-peek-ts">{new Date(m.ts).toLocaleString()}</span>
                )}
              </div>
              {m.text && (
                <div className="session-peek-text">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {m.text}
                  </ReactMarkdown>
                </div>
              )}
              {!m.text && m.tools.length > 0 && (
                <div className="session-peek-tools">{m.tools.length} tool call(s)</div>
              )}
            </div>
          ))}
        </div>
      </div>
    </Modal>
  )
}
