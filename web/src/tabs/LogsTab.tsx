import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'

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
      await api.createTask(
        projectId,
        `Настроить логи для ${projectName}: добавить log_cmd в topics.json`,
        'backlog',
      )
      setTaskAdded(true)
    } catch {
      // ignore
    } finally {
      setAddingTask(false)
    }
  }

  if (loading) return <Spinner label="Загрузка логов…" />

  if (error) return <div className="error-state">⚠ {error}</div>

  if (!configured) {
    return (
      <div className="logs-empty-state">
        <div className="logs-empty-icon">📋</div>
        <h3>Логи не настроены</h3>
        <p>
          Для этого проекта не указан источник логов.<br />
          Добавьте поле <code>log_cmd</code> в <code>data/topics.json</code> для этого проекта.
        </p>
        <p className="logs-empty-example">
          Например: <code>"log_cmd": "journalctl -u my-service -n 200 --no-pager"</code><br />
          или: <code>"log_cmd": "tail -n 200 /var/log/myapp.log"</code>
        </p>
        {taskAdded ? (
          <div className="logs-task-added">✓ Задача добавлена в бэклог</div>
        ) : (
          <button
            className="btn-primary logs-add-task-btn"
            onClick={handleAddTask}
            disabled={addingTask}
          >
            {addingTask ? '…' : '+ Добавить задачу в бэклог'}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="logs-container">
      <div className="logs-toolbar">
        <button className="btn-secondary logs-refresh-btn" onClick={reload} title="Обновить">↺ Обновить</button>
        <span className="logs-count">{lines.length} строк</span>
      </div>
      <div className="logs-output">
        {lines.length === 0
          ? <div className="logs-no-output">Нет вывода</div>
          : lines.map((line, i) => (
            <div key={i} className="log-line">{line}</div>
          ))
        }
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
