import { useState } from 'react'
import { createPortal } from 'react-dom'
import { Modal, ModalHead } from './Modal'
import type { ProjectHealthCheckState } from '../hooks/useProjectHealthCheck'
import { ageLabel, findingKey, pillFindings, pillState } from '../lib/healthFindings'

/**
 * Project-health pill (header meta row, next to the Tests button): renders NOTHING while the
 * project is healthy; `⚠ N` when the health check found real, actionable ailments (critical ones
 * styled stronger).  Click opens the findings with a one-line fix each — and an Acknowledge
 * button for project settings that run code, which stays quiet until the file changes.
 * Deliberately not a score: silence is the success state.
 */
export function HealthCheckPill({ health }: { health: ProjectHealthCheckState }) {
  const [open, setOpen] = useState(false)
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const pill = pillState(health.data?.findings)
  // The modal stays open while a re-check clears the last finding, so it can say "all clear".
  if (!pill && !open) return null
  const shown = pillFindings(health.data?.findings)

  async function ack(key: string, run: () => Promise<string | null>) {
    setBusyKey(key)
    const err = await run()
    setBusyKey(null)
    setNotes(n => ({ ...n, [key]: err ?? '' }))
  }

  return (
    <>
      {pill && (
        <button
          className={`health-badge health-check-pill ${pill.crit ? 'health-check-pill-crit' : 'health-badge-yellow'}`}
          onClick={() => setOpen(true)}
          title={pill.title}
          aria-label={`Project health: ${pill.title}`}
        >
          {pill.label}
        </button>
      )}
      {/* Portalled: on mobile the pill lives inside the tab strip, whose edge-fade mask would
          otherwise clip a fixed-position overlay. */}
      {open && createPortal(
        <Modal onClose={() => setOpen(false)} className="health-check-modal">
          <ModalHead
            title="Project health"
            onClose={() => setOpen(false)}
            extra={
              <button
                className="git-sync-btn"
                style={{ fontSize: 11, padding: '2px 8px' }}
                onClick={health.recheck}
                disabled={health.checking}
              >
                {health.checking ? '…' : 'Re-check'}
              </button>
            }
          />
          <div className="run-modal-body health-check-list">
            {shown.length === 0 && (
              <div className="health-check-empty">Nothing to fix.</div>
            )}
            {shown.map(f => {
              const key = findingKey(f)
              return (
                <div key={key} className={`health-check-item health-check-${f.severity}`}>
                  <div className="health-check-title">
                    <span className="health-check-dot" aria-hidden="true" />
                    <span>{f.title}</span>
                    {f.severity === 'crit' && <span className="health-check-sev">critical</span>}
                  </div>
                  <div className="health-check-detail">{f.detail}</div>
                  <div className="health-check-fix"><b>Fix:</b> {f.fix_hint}</div>
                  {f.ackable && f.ack_sha256 && (
                    <div className="health-check-actions">
                      <button
                        className="git-sync-btn health-check-ack"
                        disabled={busyKey === key}
                        onClick={() => void ack(key, () => health.acknowledge(f))}
                        title="Stay quiet about this file until its content changes"
                      >
                        {busyKey === key ? '…' : 'Acknowledge'}
                      </button>
                    </div>
                  )}
                  {notes[key] && <div className="health-check-note">{notes[key]}</div>}
                </div>
              )
            })}
            {health.data?.checked_at ? (
              <div className="health-check-foot">
                checked {ageLabel(health.data.checked_at, Math.floor(Date.now() / 1000))}
              </div>
            ) : null}
          </div>
        </Modal>,
        document.body,
      )}
    </>
  )
}
