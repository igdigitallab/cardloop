import { useEffect, useState } from 'react'
import { api } from '../api'
import { ProjectStructureHealth } from '../types'
import { useOnRunEnd } from '../hooks/useProjectActivity'

interface Props {
  projectId: string
}

export function ProjectStructureCard({ projectId }: Props) {
  const [health, setHealth] = useState<ProjectStructureHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [auditMsg, setAuditMsg] = useState<string>('')
  const [auditBusy, setAuditBusy] = useState(false)

  async function load() {
    try {
      const h = await api.projectHealth(projectId)
      setHealth(h)
    } catch {
      // silently ignore — endpoint may not exist yet
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    setHealth(null)
    setAuditMsg('')
    load()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // Refresh on run_end — agent may have fixed things
  useOnRunEnd(load)

  async function handleAudit() {
    if (auditBusy) return
    setAuditBusy(true)
    setAuditMsg('')
    try {
      await api.auditProject(projectId)
      setAuditMsg('Карточка создана')
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setAuditMsg('Проект занят — попробуй позже')
      } else {
        setAuditMsg('Ошибка: ' + (e instanceof Error ? e.message : String(e)))
      }
    } finally {
      setAuditBusy(false)
    }
  }

  const [upgradeBusy, setUpgradeBusy] = useState(false)
  async function handleFix() {
    if (upgradeBusy) return
    setUpgradeBusy(true)
    setAuditMsg('')
    try {
      await api.upgradeProject(projectId)
      setAuditMsg('Карточка апгрейда создана')
    } catch (e: unknown) {
      const status = (e as { status?: number }).status
      if (status === 409) {
        setAuditMsg('Проект занят — попробуй позже')
      } else {
        setAuditMsg('Ошибка: ' + (e instanceof Error ? e.message : String(e)))
      }
    } finally {
      setUpgradeBusy(false)
    }
  }

  if (loading) return null
  if (!health) return null

  const dotClass = health.color === 'green' ? 'green' : health.color === 'yellow' ? 'yellow' : 'red'

  return (
    <div className="git-card" style={{ marginTop: 16 }}>
      <div className="git-card-header" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <span>Структура проекта</span>
        <span className={`git-sync-dot ${dotClass}`} title={`${health.score}/${health.total}`} />
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text2)', textTransform: 'none', letterSpacing: 0 }}>
          {health.score}/{health.total}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
        {health.items.map(item => (
          <div key={item.key} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                fontSize: 12,
                color: item.ok ? 'var(--green)' : 'var(--yellow)',
                flexShrink: 0,
                fontWeight: 600,
              }}>
                {item.ok ? '✓' : '✗'}
              </span>
              <span style={{ fontSize: 13, color: item.ok ? 'var(--text)' : 'var(--text2)' }}>
                {item.label}
              </span>
            </div>
            {!item.ok && item.hint && (
              <div style={{ fontSize: 11, color: 'var(--text3)', paddingLeft: 18 }}>
                {item.hint}
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
          title="Запустить аудит проекта"
        >
          {auditBusy ? '⏳…' : '🩺 Аудит проекта'}
        </button>
        {health.color !== 'green' && (
          <button
            className="git-sync-btn"
            style={{ fontSize: 12, padding: '5px 12px', color: 'var(--text2)' }}
            onClick={handleFix}
            disabled={upgradeBusy}
            title="Дополнить CLAUDE.md/TASKS.md/README/.gitignore по шаблону, не переписывая существующее"
          >
            {upgradeBusy ? '⏳…' : '🔧 Подтянуть до стандарта'}
          </button>
        )}
        {auditMsg && (
          <span style={{
            fontSize: 12,
            color: auditMsg.startsWith('Ошибка') || auditMsg.includes('занят') ? 'var(--red)' : 'var(--green)',
          }}>
            {auditMsg.startsWith('Карточка') ? '✓ ' : ''}{auditMsg}
          </span>
        )}
      </div>
    </div>
  )
}
