/**
 * Collapsible panel showing what files/commands the current session has touched.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { SessionContext } from '../types'

interface Props {
  projectId: string
  /** Increment to trigger a reload (e.g. after run_end) */
  refreshKey: number
}

export function SessionContextPanel({ projectId, refreshKey }: Props) {
  const [ctx, setCtx] = useState<SessionContext | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    api.sessionContext(projectId).then(d => {
      setCtx(d)
      setLoading(false)
    }).catch(() => {
      setLoading(false)
    })
  }, [projectId])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  const totalFiles = (ctx?.read.length ?? 0) + (ctx?.edited.length ?? 0)
  const hasData = totalFiles > 0 || (ctx?.commands.length ?? 0) > 0

  if (!ctx || (!hasData && !loading)) return null

  return (
    <div className="ctx-panel">
      <button
        className="ctx-panel-toggle"
        onClick={() => setOpen(o => !o)}
        title={open ? 'Свернуть контекст сессии' : 'Развернуть контекст сессии'}
        aria-expanded={open}
      >
        <span className="ctx-panel-icon">📎</span>
        <span className="ctx-panel-label">
          Контекст: {totalFiles} файл{totalFiles === 1 ? '' : totalFiles >= 2 && totalFiles <= 4 ? 'а' : 'ов'}
          {ctx.commands.length > 0 && `, ${ctx.commands.length} команд`}
        </span>
        <span className="ctx-panel-chevron">{open ? '▲' : '▼'}</span>
        <button
          className="ctx-refresh-btn"
          onClick={e => { e.stopPropagation(); load() }}
          title="Обновить контекст"
          disabled={loading}
          aria-label="Обновить контекст сессии"
        >↺</button>
      </button>

      {open && (
        <div className="ctx-panel-body">
          {loading && <div className="ctx-loading">обновление…</div>}

          {ctx.read.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">📖 Прочитано ({ctx.read.length})</div>
              <div className="ctx-list">
                {ctx.read.map((f, i) => (
                  <div key={i} className="ctx-item">{f}</div>
                ))}
              </div>
            </div>
          )}

          {ctx.edited.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">✏️ Изменено ({ctx.edited.length})</div>
              <div className="ctx-list">
                {ctx.edited.map((f, i) => (
                  <div key={i} className="ctx-item ctx-item-edited">{f}</div>
                ))}
              </div>
            </div>
          )}

          {ctx.commands.length > 0 && (
            <div className="ctx-section">
              <div className="ctx-section-label">⚙ Команды ({ctx.commands.length})</div>
              <div className="ctx-list">
                {ctx.commands.map((c, i) => (
                  <div key={i} className="ctx-item ctx-item-cmd">{c}</div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
