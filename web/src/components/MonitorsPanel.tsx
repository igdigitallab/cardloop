/**
 * Background-task monitors panel (card b6f5cc).
 *
 * A compact, collapsible strip above the composer listing the long-running "service monitors"
 * the agent started — background shells (npm run dev, journalctl -f …) and Monitor/Workflow
 * tasks. Read-only: shows status + last output tail. Stop/control is card 6c9a57.
 *
 * spec-069 P3-B: "agent" kind monitors get special treatment — always-visible inline tail,
 * Bot icon (spinning while running, checkmark when done), and the panel auto-expands whenever
 * ≥1 agent monitor is running, then auto-collapses when none remain.
 */
import { useEffect, useRef, useState } from 'react'
import { Activity, Bot, CheckCircle2, Terminal, Workflow, ChevronRight, X } from 'lucide-react'
import { Monitor } from '../types'

// https://lucide.dev/icons/ — Bot for agent sub-process, CheckCircle2 for done state
const KIND_ICON: Record<string, typeof Terminal> = {
  bash: Terminal,
  monitor: Activity,
  workflow: Workflow,
  agent: Bot,
}

function statusClass(s: string): string {
  if (s === 'running') return 'mon-running'
  if (s === 'failed') return 'mon-failed'
  return 'mon-stopped' // stopped / done
}

/** Render one monitor row. Agent kind gets inline tail + no expand-click requirement. */
function MonitorRow({ m, onDismiss }: { m: Monitor; onDismiss: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const isAgent = m.kind === 'agent'
  const isDone = m.status === 'done' || m.status === 'stopped'
  // Agent rows: pick checkmark icon when done, spinning Bot when running/failed
  const Icon = isAgent
    ? (isDone ? CheckCircle2 : Bot)
    : (KIND_ICON[m.kind] || Activity)
  const hasTail = !!(m.tail && m.tail.trim())

  // Agent rows: always show tail inline (no click needed — this is the per-tool live feed)
  // Other rows: expand/collapse on click (existing behaviour)
  const showTailInline = isAgent && hasTail
  const showTailExpanded = !isAgent && open && hasTail

  return (
    <div className={`mon-row${isAgent ? ' mon-row-agent' : ''}`}>
      <div className="mon-head-row">
        <button
          className="mon-head"
          onClick={() => !isAgent && hasTail && setOpen(o => !o)}
          style={{ cursor: (!isAgent && hasTail) ? 'pointer' : 'default' }}
          title={m.label}
        >
          <span className={`mon-dot ${statusClass(m.status)}`} />
          <Icon
            size={13}
            className={`mon-kind-icon${isAgent && m.status === 'running' ? ' mon-agent-spin' : ''}`}
          />
          <span className="mon-label">{m.label || m.id}</span>
          {m.agent && <span className="mon-agent">{m.agent}</span>}
          <span className="mon-status">{m.status}</span>
          {!isAgent && hasTail && (
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
      {/* Agent: always-visible live tail (per-tool progress, updates ~2s) */}
      {showTailInline && (
        <div className="mon-agent-tail">{m.tail}</div>
      )}
      {/* Other kinds: collapsible tail */}
      {showTailExpanded && <pre className="mon-tail">{m.tail}</pre>}
    </div>
  )
}

export function MonitorsPanel({
  monitors,
  onDismiss,
}: {
  monitors: Monitor[]
  onDismiss: (id: string) => void
}) {
  // manualOverride: null = no user action yet (auto-drive), true/false = user toggled
  const [manualOverride, setManualOverride] = useState<boolean | null>(null)
  const prevAgentRunning = useRef(false)

  const agentRunning = monitors.some(m => m.kind === 'agent' && m.status === 'running')

  // Auto-expand when an agent becomes active; auto-collapse when the last one finishes —
  // but only if the user has NOT manually toggled since the last auto-event.
  useEffect(() => {
    if (agentRunning && !prevAgentRunning.current) {
      // Agent just started — auto-expand, clear any stale manual override
      setManualOverride(null)
    } else if (!agentRunning && prevAgentRunning.current) {
      // Last agent just finished — auto-collapse only if no override
      setManualOverride(cur => cur === null ? null : cur)
    }
    prevAgentRunning.current = agentRunning
  }, [agentRunning])

  if (monitors.length === 0) return null

  // collapsed = user said so (manualOverride=true), or no agents running and no override (default closed)
  // expanded  = user said so (manualOverride=false), or an agent is running and no override
  const collapsed: boolean = manualOverride !== null
    ? manualOverride
    : !agentRunning

  const running = monitors.filter(m => m.status === 'running').length
  const agentCount = monitors.filter(m => m.kind === 'agent' && m.status === 'running').length

  return (
    <div className="mon-panel">
      <button
        className="mon-panel-head"
        onClick={() => setManualOverride(!collapsed)}
      >
        {agentRunning ? <Bot size={12} className="mon-agent-spin" /> : <Activity size={12} />}
        <span className="mon-panel-title">
          {agentCount > 0 ? `Agent Activity` : 'Monitors'}
        </span>
        <span className="mon-panel-count">
          {agentCount > 0
            ? `${agentCount} agent${agentCount > 1 ? 's' : ''} running · ${monitors.length} total`
            : `${running} running · ${monitors.length} total`}
        </span>
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
