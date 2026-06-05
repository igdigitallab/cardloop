import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
  projectName: string
}

export function LogsTab({ projectId, projectName }: Props) {
  const [lines, setLines] = useState<string[]>([])
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [taskAdded, setTaskAdded] = useState(false)
  const [addingTask, setAddingTask] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  const reload = useCallback(() => {
    api.projectLogs(projectId).then(d => {
      setConfigured(d.configured)
      setLines(d.lines)
      setError('')
    }).catch(e => setError(String(e.message || e)))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setLines([])
    setConfigured(null)
    setTaskAdded(false)

    api.projectLogs(projectId).then(d => {
      if (cancelled) return
      setConfigured(d.configured)
      setLines(d.lines)
      setLoading(false)
    }).catch(e => {
      if (cancelled) return
      setError(String(e.message || e))
      setLoading(false)
    })

    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  async function handleAddTask() {
    setAddingTask(true)
    try {
      const title = `Configure log source for ${projectName}`
      const instruction = [
        `Goal: configure the log source (and, if the project has tests, the test source) for project "${projectName}" so that the Logs tab in the cockpit and the background incident scanner work.`,
        ``,
        `You are running in the cwd of THIS project (check: pwd). The config lives in data/topics.json at the root of the claude-ops-bot project — NOT inside the current project. Find it with: find $HOME -maxdepth 3 -name topics.json -path "*/claude-ops-bot/*" 2>/dev/null | head -1`,
        ``,
        `STEPS:`,
        `1. Open data/topics.json at the claude-ops-bot root (path found above). Find the block whose "cwd" matches your pwd. Make changes ONLY in that block. Save valid JSON (don't break other blocks).`,
        ``,
        `2. Pick "log_cmd" based on how this project is actually running. The command is executed as the current user ($(whoami)), WITHOUT sudo and without a shell (exec, not bash) — so NO pipes, &&, >, $ variables, or wrapper quotes:`,
        `   • systemd service → "journalctl -u <unit> -n 300 --no-pager". Find the real unit: systemctl list-units --type=service | grep -i <name-part>. journalctl works without sudo if the user is in the adm group.`,
        `   • Docker/Coolify container → "docker logs --tail 300 <container>". Find the name: docker ps --format '{{.Names}}' | grep -i <name-part>.`,
        `   • Just a log file → "tail -n 300 /absolute/path/to.log".`,
        `   IMPORTANT: log_cmd runs WITHOUT the project cwd → use absolute paths and full unit/container names.`,
        ``,
        `3. If the project has tests — add "test_cmd". It runs INSIDE the project cwd, so paths are relative to the project root: e.g. ".venv/bin/python -m pytest -q tests/" or "venv/bin/python -m pytest -q". No tests — do NOT add test_cmd.`,
        ``,
        `4. VERIFY before finishing (required, otherwise the task is not done):`,
        `   • Run the chosen log_cmd EXACTLY as-is (without sudo) and confirm it prints real fresh lines — not empty, not "permission denied", not "unit not found"/"no such container". Empty or error → pick a different command and verify again.`,
        `   • If you added test_cmd — run it in the project cwd and confirm pytest actually starts (not "No such file or directory").`,
        ``,
        `5. Restart is NOT needed: the cockpit re-reads topics.json from disk on the fly (hot-reload). After saving, the Logs tab will work immediately. Do NOT restart claude-ops-bot and do not write "restart needed".`,
        ``,
        `In the report state: which log_cmd (and test_cmd if set) was written + the first 3-5 verified output lines.`,
      ].join('\n')
      await api.createTask(projectId, title, 'backlog', instruction)
      setTaskAdded(true)
    } catch {
      // ignore
    } finally {
      setAddingTask(false)
    }
  }

  if (loading) return <Spinner label={t['logs.loading']} />

  if (error) return <div className="error-state">⚠ {error}</div>

  if (!configured) {
    return (
      <div className="logs-empty-state">
        <div className="logs-empty-icon">📋</div>
        <h3>{t['logs.not_configured_title']}</h3>
        <p>
          No log source configured for this project.<br />
          Set <code>log_cmd</code> (and, if there are tests, <code>test_cmd</code>) in <code>data/topics.json</code> — or click the button below and the agent will configure it.
        </p>
        <p className="logs-empty-example">
          Example: <code>"log_cmd": "journalctl -u my-service -n 300 --no-pager"</code><br />
          or: <code>"log_cmd": "docker logs --tail 300 my-container"</code><br />
          or: <code>"log_cmd": "tail -n 300 /var/log/myapp.log"</code>
        </p>
        {taskAdded ? (
          <div className="logs-task-added">✓ Task added to backlog</div>
        ) : (
          <button
            className="btn-primary logs-add-task-btn"
            onClick={handleAddTask}
            disabled={addingTask}
          >
            {addingTask ? '…' : '+ Add task to backlog'}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="logs-container">
      <div className="logs-toolbar">
        <button className="btn-secondary logs-refresh-btn" onClick={reload} title={t['logs.refresh_title']}>{t['logs.refresh']}</button>
        <span className="logs-count">{lines.length} lines</span>
      </div>
      <div className="logs-output">
        {lines.length === 0
          ? <div className="logs-no-output">{t['logs.no_output']}</div>
          : lines.map((line, i) => (
            <div key={i} className="log-line">{line}</div>
          ))
        }
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
