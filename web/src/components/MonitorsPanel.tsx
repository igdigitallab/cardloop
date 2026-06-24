/**
 * Background-task monitors panel (card b6f5cc).
 *
 * A compact, collapsible strip above the composer listing the long-running "service monitors"
 * the agent started — background shells (npm run dev, journalctl -f …) and Monitor/Workflow
 * tasks. Read-only: shows status + last output tail. Stop/control is card 6c9a57.
 */
import { useState } from 'react'
import { Activity, Terminal, Workflow, ChevronRight, X } from 'lucide-react'
import { Monitor } from '../types'

const KIND_ICON: Record<string, typeof Terminal> = {
  bash: Terminal,
  monitor: Activity,
  workflow: Workflow,
}

function statusClass(s: string): string {
  if (s === 'running') return 'mon-running'
  if (s === 'failed') return 'mon-failed'
  return 'mon-stopped' // stopped / done
}

function MonitorRow({ m, onDismiss }: { m: Monitor; onDismiss: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const Icon = KIND_ICON[m.kind] || Activity
  const hasTail = !!(m.tail && m.tail.trim())
  return (
    <div className="mon-row">
      <div className="mon-head-row">
        <button
          className="mon-head"
          onClick={() => hasTail && setOpen(o => !o)}
          style={{ cursor: hasTail ? 'pointer' : 'default' }}
          title={m.label}
        >
          <span className={`mon-dot ${statusClass(m.status)}`} />
          <Icon size={13} className="mon-kind-icon" />
          <span className="mon-label">{m.label || m.id}</span>
          {m.agent && <span className="mon-agent">{m.agent}</span>}
          <span className="mon-status">{m.status}</span>
          {hasTail && (
            <ChevronRight size={13} className="mon-chevron" style={{
              transform: open ? 'rotate(90deg)' : 'none',
            }} />
          )}
        </button>
        <button
          className="mon-dismiss"
          onClick={() => onDismiss(m.id)}
          title="Dismiss (clears the row — does not kill the process)"
          aria-label="Dismiss monitor"
        ><X size={13} /></button>
      </div>
      {open && hasTail && <pre className="mon-tail">{m.tail}</pre>}
    </div>
  )
}

export function MonitorsPanel({ monitors, onDismiss }: { monitors: Monitor[]; onDismiss: (id: string) => void }) {
  const [collapsed, setCollapsed] = useState(false)
  if (monitors.length === 0) return null
  const running = monitors.filter(m => m.status === 'running').length
  return (
    <div className="mon-panel">
      <button className="mon-panel-head" onClick={() => setCollapsed(c => !c)}>
        <Activity size={12} />
        <span className="mon-panel-title">Monitors</span>
        <span className="mon-panel-count">{running} running · {monitors.length} total</span>
        <ChevronRight size={13} className="mon-chevron" style={{
          marginLeft: 'auto', transform: collapsed ? 'none' : 'rotate(90deg)',
        }} />
      </button>
      {!collapsed && (
        <div className="mon-list">
          {monitors.map(m => <MonitorRow key={m.id} m={m} onDismiss={onDismiss} />)}
        </div>
      )}
    </div>
  )
}
