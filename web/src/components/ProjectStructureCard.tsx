import { useState } from 'react'
import { api } from '../api'
import { ProjectStructureHealth } from '../types'

// Version with projectId for audit/upgrade actions (used in OverviewTab)
interface FullProps {
  health: ProjectStructureHealth | null
  refreshHealth: () => void
  projectId: string
}

export function ProjectStructureCardFull({ health, refreshHealth, projectId }: FullProps) {
  const [auditMsg, setAuditMsg] = useState<string>('')
  const [auditBusy, setAuditBusy] = useState(false)
  const [upgradeBusy, setUpgradeBusy] = useState(false)

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

  async function handleFix() {
    if (upgradeBusy) return
    setUpgradeBusy(true)
    setAuditMsg('')
    try {
      await api.upgradeProject(projectId)
      setAuditMsg('Upgrade started — check chat, changes toward baseline')
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setAuditMsg('Project is busy — try again later')
      } else {
        setAuditMsg('Error: ' + (e instanceof Error ? e.message : String(e)))
      }
    } finally {
      setUpgradeBusy(false)
    }
  }

  if (!health) return null

  const dotClass = health.color === 'green' ? 'green' : health.color === 'yellow' ? 'yellow' : 'red'
  const failingItems = health.items.filter(item => !item.optional && !item.ok)
  const allPassed = failingItems.length === 0

  if (allPassed) {
    return (
      <div className="git-card" style={{ marginTop: 16 }}>
        <div className="git-card-header" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span>Project structure</span>
          <span className="git-sync-dot green" />
          <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text2)', textTransform: 'none', letterSpacing: 0 }}>
            ✓ all connected
          </span>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <button className="git-sync-btn" style={{ fontSize: 12, padding: '5px 12px' }}
            onClick={handleAudit} disabled={auditBusy} title="Run project audit">
            {auditBusy ? '⏳…' : '🩺 Audit project'}
          </button>
          {auditMsg && <span style={{ fontSize: 12, color: auditMsg.startsWith('Error') ? 'var(--red)' : 'var(--green)' }}>
            {auditMsg.startsWith('Card') ? '✓ ' : ''}{auditMsg}
          </span>}
        </div>
      </div>
    )
  }

  return (
    <div className="git-card" style={{ marginTop: 16 }}>
      <div className="git-card-header" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <span>Project structure</span>
        <span className={`git-sync-dot ${dotClass}`} title={`${health.score}/${health.total}`} />
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text2)', textTransform: 'none', letterSpacing: 0 }}>
          {health.score}/{health.total}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
        {failingItems.map(item => (
          <div key={item.key} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ fontSize: 12, color: 'var(--yellow)', flexShrink: 0, fontWeight: 600 }}>✗</span>
              <span style={{ fontSize: 13, color: 'var(--text2)' }}>
                to do: {item.label}
              </span>
            </div>
            {item.hint && (
              <div style={{ fontSize: 11, color: 'var(--text3)', paddingLeft: 18 }}>
                {item.hint}
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="git-sync-btn" style={{ fontSize: 12, padding: '5px 12px' }}
          onClick={handleAudit} disabled={auditBusy} title="Run project audit">
          {auditBusy ? '⏳…' : '🩺 Audit project'}
        </button>
        {health.color !== 'green' && (
          <button className="git-sync-btn"
            style={{ fontSize: 12, padding: '5px 12px', color: 'var(--text2)' }}
            onClick={handleFix} disabled={upgradeBusy}
            title="Fill in CLAUDE.md/TASKS.md/README/.gitignore from template without overwriting existing content">
            {upgradeBusy ? '⏳…' : '🔧 Bring up to standard'}
          </button>
        )}
        {auditMsg && (
          <span style={{
            fontSize: 12,
            color: auditMsg.startsWith('Error') || auditMsg.includes('busy') ? 'var(--red)' : 'var(--green)',
          }}>
            {auditMsg.startsWith('Card') ? '✓ ' : ''}{auditMsg}
          </span>
        )}
      </div>
    </div>
  )
}

