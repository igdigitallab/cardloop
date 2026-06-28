import { useState } from 'react'
import { api } from '../api'
import { ProjectStructureHealth } from '../types'

// Version with projectId for audit action (used in OverviewTab)
interface FullProps {
  health: ProjectStructureHealth | null
  refreshHealth: () => void
  projectId: string
}

export function ProjectStructureCardFull({ health, refreshHealth, projectId }: FullProps) {
  const [auditMsg, setAuditMsg] = useState<string>('')
  const [auditBusy, setAuditBusy] = useState(false)

  // run_end → refresh is handled by HealthRunEndRefresher in the header (always-mounted)
  void refreshHealth  // prop kept for API compatibility

  async function handleAudit() {
    if (auditBusy) return
    setAuditBusy(true)
    setAuditMsg('')
    try {
      await api.auditProject(projectId)
      setAuditMsg('Audit started — check chat, findings will go to Backlog')
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setAuditMsg('Project is busy — try again later')
      } else {
        setAuditMsg('Error: ' + (e instanceof Error ? e.message : String(e)))
      }
    } finally {
      setAuditBusy(false)
    }
  }

  if (!health) return null

  return (
    <div className="git-card" style={{ marginTop: 16 }}>
      <div className="git-card-header" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <span>Connected capabilities</span>
      </div>

      {health.security_warn && health.security_hint && (
        <div style={{ fontSize: 12, color: 'var(--red)', marginBottom: 10, fontWeight: 500 }}>
          &#9888; {health.security_hint}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
        {health.capabilities.map(cap => (
          <div key={cap.key} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                fontSize: 12,
                color: cap.on ? 'var(--green)' : 'var(--text3)',
                flexShrink: 0,
                fontWeight: 600,
              }}>
                {cap.on ? '✓' : '○'}
              </span>
              <span style={{ fontSize: 13, color: 'var(--text2)' }}>
                {cap.label}
              </span>
            </div>
            {!cap.on && cap.hint && (
              <div style={{ fontSize: 11, color: 'var(--text3)', paddingLeft: 18 }}>
                {cap.hint}
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button
          className="git-sync-btn"
          style={{ fontSize: 12, padding: '5px 12px' }}
          onClick={handleAudit}
          disabled={auditBusy}
          title="Runs an agent that reviews the project and files findings as cards in the Backlog"
        >
          {auditBusy ? '⏳…' : '🩺 Audit project'}
        </button>
        {auditMsg && (
          <span style={{
            fontSize: 12,
            color: auditMsg.startsWith('Error') || auditMsg.includes('busy') ? 'var(--red)' : 'var(--green)',
          }}>
            {auditMsg.startsWith('Audit') ? '✓ ' : ''}{auditMsg}
          </span>
        )}
      </div>
    </div>
  )
}
