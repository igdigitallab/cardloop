/**
 * Background-task monitors panel (card b6f5cc).
 *
 * A compact, collapsible strip above the composer listing the long-running "service monitors"
 * the agent started — background shells (npm run dev, journalctl -f …) and Monitor/Workflow
 * tasks. Read-only: shows status + last output tail.
 *
 * spec-069 P3-B: "agent" kind monitors get special treatment — always-visible inline tail,
 * Bot icon (spinning while running, checkmark when done), and the panel auto-expands whenever
 * ≥1 agent monitor is running, then auto-collapses when none remain.
 *
 * spec-089 §1: the panel header also carries the "⏹ stop agents" control — the ONLY way to
 * kill a background Workflow/sub-agent short of typing a message and waiting for the CLI to
 * notice. There is no server-side kill; the button asks the model to call TaskStop for every
 * running row (see api.stopAgents), which flips rows to a 'stopping' status first (instant
 * feedback) and to 'stopped' once the CLI confirms (or the server's 60s follow-up gives up).
 *
 * spec-089 §7: an agent/workflow row's head is ALSO the transcript-peek toggle — a click fetches
 * the row's last N steps (api.monitorTail) and shows them in a scrollable panel, polled every
 * 5 s while the row is still live. "What are they doing?" used to mean waiting for the one-line
 * inline tail to update; the peek gives the operator the actual recent steps on demand.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, Bot, CheckCircle2, Terminal, Workflow, ChevronRight, X } from 'lucide-react'
import { Monitor } from '../types'
import { api } from '../api'
import { ConfirmModal } from './ConfirmModal'

// https://lucide.dev/icons/ — Bot for agent sub-process, CheckCircle2 for done state
const KIND_ICON: Record<string, typeof Terminal> = {
  bash: Terminal,
  monitor: Activity,
  workflow: Workflow,
  agent: Bot,
}

function statusClass(s: string): string {
  // spec-089 §1: 'stopping' renders exactly like 'running' — it is still active work, just
  // headed for a terminal state.
  if (s === 'running' || s === 'stopping') return 'mon-running'
  if (s === 'failed') return 'mon-failed'
  return 'mon-stopped' // stopped / done
}

/** Render one monitor row. Agent/workflow kind gets inline tail + a click-to-expand transcript
 *  peek (spec-089 §7); other kinds keep the plain click-to-expand tail (spec-069 P3-B). */
function MonitorRow({ m, onDismiss, projectId }: {
  m: Monitor; onDismiss: (id: string) => void; projectId: string
}) {
  const [open, setOpen] = useState(false)
  // spec-088: a workflow row behaves like an agent row — its tail is the live progress summary
  // the server keeps ("3/5 agents done · ↳ Bash"), shown inline, icon spinning while running.
  const isAgent = m.kind === 'agent' || m.kind === 'workflow'
  const isDone = m.status === 'done' || m.status === 'stopped'
  const isLive = m.status === 'running' || m.status === 'stopping' // spec-089 §1
  // Agent rows: pick checkmark icon when done, spinning Bot when running/stopping/failed
  const Icon = isAgent
    ? (isDone ? CheckCircle2 : (m.kind === 'workflow' ? Workflow : Bot))
    : (KIND_ICON[m.kind] || Activity)
  const hasTail = !!(m.tail && m.tail.trim())

  // Agent rows: always show tail inline (no click needed — this is the per-tool live feed)
  // Other rows: expand/collapse on click (existing behaviour)
  const showTailInline = isAgent && hasTail
  const showTailExpanded = !isAgent && open && hasTail

  // spec-089 §7: transcript peek — agent/workflow rows only. null lines = not fetched yet
  // (renders "Loading…"); [] with peekError = the 404 "no transcript" case.
  const [peekOpen, setPeekOpen] = useState(false)
  const [peekLines, setPeekLines] = useState<string[] | null>(null)
  const [peekPath, setPeekPath] = useState<string | null>(null)
  const [peekError, setPeekError] = useState(false)

  const fetchPeek = useCallback(() => {
    api.monitorTail(projectId, m.id)
      .then(r => { setPeekLines(r.lines); setPeekPath(r.path); setPeekError(false) })
      // 404 (unknown monitor / stream-only row with no transcript) or any other failure — render
      // "(no transcript yet)" rather than an error toast; the bus will fix a stale row anyway.
      .catch(() => { setPeekError(true); setPeekLines([]) })
  }, [projectId, m.id])

  // Poll every 5s while the peek is open AND the row is still live; re-evaluated on every
  // status change so the interval stops the moment the row goes terminal (no leaked timers).
  useEffect(() => {
    if (!peekOpen) return
    fetchPeek()
    if (!isLive) return
    const t = setInterval(fetchPeek, 5000)
    return () => clearInterval(t)
  }, [peekOpen, isLive, fetchPeek])

  const togglePeek = () => {
    setPeekOpen(o => {
      const next = !o
      if (!next) { setPeekLines(null); setPeekPath(null); setPeekError(false) }
      return next
    })
  }

  return (
    <div className={`mon-row${isAgent ? ' mon-row-agent' : ''}`}>
      <div className="mon-head-row">
        <button
          className="mon-head"
          onClick={() => { if (isAgent) togglePeek(); else if (hasTail) setOpen(o => !o) }}
          style={{ cursor: (isAgent || hasTail) ? 'pointer' : 'default' }}
          title={m.label}
        >
          <span className={`mon-dot ${statusClass(m.status)}`} />
          <Icon
            size={13}
            className={`mon-kind-icon${isAgent && isLive ? ' mon-agent-spin' : ''}`}
          />
          <span className="mon-label">{m.label || m.id}</span>
          {m.agent && <span className="mon-agent">{m.agent}</span>}
          {/* spec-089 §1: a stop glyph alongside the spinning icon while the row is 'stopping' */}
          {m.status === 'stopping' && (
            <span className="mon-stop-glyph" title="Stop requested">⏹</span>
          )}
          <span className="mon-status">{m.status}</span>
          {(isAgent || hasTail) && (
            <ChevronRight size={13} className="mon-chevron" style={{
              transform: (isAgent ? peekOpen : open) ? 'rotate(90deg)' : 'none',
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
      {/* spec-089 §7: agent/workflow transcript peek, click-to-expand on the row head above */}
      {isAgent && peekOpen && (
        <pre className="mon-peek">
          {peekPath && (
            <span className="mon-peek-path" title={peekPath}>{peekPath.split('/').pop()}</span>
          )}
          {peekPath && '\n'}
          {peekError || (peekLines !== null && peekLines.length === 0)
            ? '(no transcript yet)'
            : (peekLines === null ? 'Loading…' : peekLines.join('\n'))}
        </pre>
      )}
    </div>
  )
}

export function MonitorsPanel({
  monitors,
  onDismiss,
  projectId,
}: {
  monitors: Monitor[]
  onDismiss: (id: string) => void
  projectId: string
}) {
  // manualOverride: null = no user action yet (auto-drive), true/false = user toggled
  const [manualOverride, setManualOverride] = useState<boolean | null>(null)
  const prevAgentRunning = useRef(false)
  // spec-089 §1: confirm-before-stop (no window.confirm — this is a mobile cockpit) + a busy
  // flag so a double-tap while the request is in flight can't fire it twice.
  const [confirmStop, setConfirmStop] = useState(false)
  const [stopBusy, setStopBusy] = useState(false)

  const agentOrWorkflow = (m: Monitor) => m.kind === 'agent' || m.kind === 'workflow'
  const anyAgentRunning = monitors.some(m => agentOrWorkflow(m) && m.status === 'running')
  const anyAgentStopping = monitors.some(m => agentOrWorkflow(m) && m.status === 'stopping')
  // spec-088/spec-089 §1: a running Workflow counts as agent work too, and so does a 'stopping'
  // one — the panel stays expanded and the Stop button stays visible until the stop resolves.
  const agentRunning = anyAgentRunning || anyAgentStopping

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
  const agentStopping = monitors.filter(m => agentOrWorkflow(m) && m.status === 'stopping').length
  const agentDone = monitors.filter(m => m.kind === 'agent' && (m.status === 'done' || m.status === 'stopped')).length
  const agentFailed = monitors.filter(m => m.kind === 'agent' && m.status === 'failed').length
  const workflowRunning = monitors.filter(m => m.kind === 'workflow' && m.status === 'running').length

  async function handleStopConfirm() {
    setConfirmStop(false)
    setStopBusy(true)
    try {
      await api.stopAgents(projectId)
      // Rows flip to 'stopping' (and later 'stopped') via the existing monitor bus events —
      // nothing else to do here.
    } catch (err) {
      console.error('[agents-stop] request failed', err)
    } finally {
      setStopBusy(false)
    }
  }

  return (
    <div className="mon-panel">
      <div className="mon-panel-head-row">
        <button
          className="mon-panel-head"
          onClick={() => setManualOverride(!collapsed)}
        >
          {agentRunning ? <Bot size={12} className="mon-agent-spin" /> : <Activity size={12} />}
          <span className="mon-panel-title">
            {agentCount > 0 || workflowRunning > 0 || agentStopping > 0 ? `Agent Activity` : 'Monitors'}
          </span>
          <span className="mon-panel-count">
            {agentCount > 0 || workflowRunning > 0 || agentStopping > 0
              ? `${agentCount} agent${agentCount === 1 ? '' : 's'} running`
                + (agentStopping > 0 ? ` · ${agentStopping} stopping` : '')
                + (agentDone > 0 ? ` · ${agentDone} done` : '')
                + (agentFailed > 0 ? ` · ${agentFailed} failed` : '')
                + (workflowRunning > 0 ? ` · ${workflowRunning} workflow${workflowRunning === 1 ? '' : 's'}` : '')
              : `${running} running · ${monitors.length} total`}
          </span>
          <ChevronRight size={13} className="mon-chevron" style={{
            marginLeft: 'auto', transform: collapsed ? 'none' : 'rotate(90deg)',
          }} />
        </button>
        {agentRunning && (
          <button
            className="mon-stop-btn"
            onClick={() => setConfirmStop(true)}
            disabled={!anyAgentRunning || stopBusy}
            title={anyAgentRunning
              ? 'Stop every running sub-agent and Workflow task'
              : 'Waiting for the CLI to confirm the stop'}
          >
            {anyAgentRunning ? '⏹ stop agents' : 'stopping…'}
          </button>
        )}
      </div>
      {!collapsed && (
        <div className="mon-list">
          {monitors.map(m => (
            <MonitorRow key={m.id} m={m} onDismiss={onDismiss} projectId={projectId} />
          ))}
        </div>
      )}
      {confirmStop && (
        <ConfirmModal
          title="Stop agents"
          message="Stop every running sub-agent and Workflow task for this project? This asks the model to call TaskStop for each of them — in-progress work is interrupted."
          confirmLabel="Stop agents"
          danger
          onConfirm={handleStopConfirm}
          onCancel={() => setConfirmStop(false)}
        />
      )}
    </div>
  )
}
