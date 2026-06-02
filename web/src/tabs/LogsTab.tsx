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
      const title = `Настроить источник логов для ${projectName}`
      const instruction = [
        `Цель: настроить источник логов (и, если в проекте есть тесты, источник тестов) для проекта «${projectName}», чтобы заработал таб «Логи» в кокпите и фоновый сканер инцидентов.`,
        ``,
        `Ты запущен в cwd ЭТОГО проекта (проверь: pwd). Конфиг живёт по абсолютному пути /home/igor/claude-ops-bot/data/topics.json — это НЕ внутри текущего проекта.`,
        ``,
        `ШАГИ:`,
        `1. Открой /home/igor/claude-ops-bot/data/topics.json. Найди блок, у которого "cwd" совпадает с твоим pwd. Правки вносишь ТОЛЬКО в этот блок. Сохрани валидный JSON (не сломай остальные блоки).`,
        ``,
        `2. Подбери "log_cmd" по тому, как этот проект реально запущен. Команда исполняется от пользователя igor, БЕЗ sudo и без shell (exec, не bash) — поэтому НИКАКИХ пайпов, &&, >, переменных $ и кавычек-обёрток:`,
        `   • systemd-сервис → "journalctl -u <unit> -n 300 --no-pager". Найди реальный юнит: systemctl list-units --type=service | grep -i <часть-имени>. journalctl без sudo работает (igor в группе adm).`,
        `   • Docker/Coolify-контейнер → "docker logs --tail 300 <container>". Найди имя: docker ps --format '{{.Names}}' | grep -i <часть-имени>.`,
        `   • Просто файл лога → "tail -n 300 /абсолютный/путь/к.log".`,
        `   ВАЖНО: log_cmd исполняется БЕЗ cwd проекта → используй абсолютные пути и полные имена юнитов/контейнеров.`,
        ``,
        `3. Если в проекте есть тесты — добавь "test_cmd". Он исполняется ВНУТРИ cwd проекта, поэтому пути относительные к корню проекта: напр. ".venv/bin/python -m pytest -q tests/" или "venv/bin/python -m pytest -q". Тестов нет — НЕ добавляй test_cmd.`,
        ``,
        `4. ПРОВЕРЬ перед завершением (обязательно, иначе задача не сделана):`,
        `   • Выполни выбранный log_cmd ТОЧНО как есть (без sudo) и убедись, что он печатает реальные свежие строки — не пусто, не «permission denied», не «unit not found»/«no such container». Пусто или ошибка → подбери другую команду и проверь снова.`,
        `   • Если добавил test_cmd — запусти его в cwd проекта и убедись, что pytest реально стартует (а не «No such file or directory»).`,
        ``,
        `5. Рестарт НЕ нужен: кокпит перечитывает topics.json с диска на лету (hot-reload). После сохранения таб «Логи» заработает сразу. НЕ перезапускай claude-ops-bot и не пиши «нужен рестарт».`,
        ``,
        `В отчёте укажи: какой log_cmd (и test_cmd, если ставил) записал + первые 3-5 проверенных строк вывода.`,
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
          Для этого проекта не указан источник логов.<br />
          Задайте <code>log_cmd</code> (и, если есть тесты, <code>test_cmd</code>) в <code>data/topics.json</code> — или нажмите кнопку ниже, и агент настроит сам.
        </p>
        <p className="logs-empty-example">
          Например: <code>"log_cmd": "journalctl -u my-service -n 300 --no-pager"</code><br />
          или: <code>"log_cmd": "docker logs --tail 300 my-container"</code><br />
          или: <code>"log_cmd": "tail -n 300 /var/log/myapp.log"</code>
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
        <button className="btn-secondary logs-refresh-btn" onClick={reload} title={t['logs.refresh_title']}>{t['logs.refresh']}</button>
        <span className="logs-count">{lines.length} строк</span>
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
