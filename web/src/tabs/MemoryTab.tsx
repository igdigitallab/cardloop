import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { MemoryFile, ProjectMemory } from '../types'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

interface Props {
  projectId: string
}

export function MemoryTab({ projectId }: Props) {
  const [data, setData] = useState<ProjectMemory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const reload = useCallback(() => {
    api.memory(projectId).then(d => {
      setData(d); setError('')
    }).catch(e => setError(String(e.message || e)))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setData(null)
    setSelected(null)

    api.memory(projectId).then(d => {
      if (!cancelled) {
        setData(d)
        setLoading(false)
        // Auto-select MEMORY.md if present, otherwise first file
        if (d.files.length > 0) {
          setSelected(d.files[0].name)
        }
      }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  if (loading) return <Spinner label={t['memory.loading']} />
  if (error) return <div className="error-state">⚠ {error}</div>

  if (!data || !data.exists || data.files.length === 0) {
    return (
      <div className="memory-empty">
        <div className="memory-empty-icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"/>
            <path d="M12 8v4l3 3"/>
          </svg>
        </div>
        <div className="memory-empty-title">{t['memory.empty_title']}</div>
        <p className="memory-empty-text">
          Это файлы из <code>~/.claude/projects/&lt;проект&gt;/memory/</code> — долговременная
          память агента <strong>между сессиями</strong> (в отличие от контекста, который живёт
          только внутри одной сессии и обнуляется при <code>/reset</code>).
        </p>
        <p className="memory-empty-text">
          Папка появляется сама, когда агент решит что-то запомнить: устойчивый факт о проекте,
          твоё предпочтение, повторяемый подход. Индекс <code>MEMORY.md</code> — одна строка на
          запись, сами факты — в отдельных <code>.md</code>. Пусто = агент пока ничего не
          закрепил. Можно прямо попросить в чате: «запомни, что…».
        </p>
        <p className="memory-empty-note">
          💡 Контекст сессии (счётчик 💬 в чате) обычно держит ~11–14K токенов даже на «пустой»
          сессии — это системный промпт Claude Code + определения инструментов, они уходят в
          модель каждый ход. <code>/reset</code> чистит разговор, но этот базовый пол остаётся.
        </p>
      </div>
    )
  }

  const selectedFile: MemoryFile | undefined = data.files.find(f => f.name === selected)

  return (
    <div className="specs-layout">
      {/* File list sidebar */}
      <div className="specs-list">
        <div className="specs-list-label">{t['memory.files_label']}</div>
        {data.files.map(f => (
          <div
            key={f.name}
            className={`spec-item ${selected === f.name ? 'active' : ''}`}
            onClick={() => setSelected(f.name)}
            title={f.name}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              style={{ flexShrink: 0, opacity: 0.5 }}>
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
            {f.name}
            {f.name === 'MEMORY.md' && (
              <span style={{ marginLeft: 4, fontSize: 10, color: 'var(--text3)' }}>{t['memory.index']}</span>
            )}
          </div>
        ))}
      </div>

      {/* Content area */}
      <div className="spec-content">
        {!selectedFile && (
          <div className="no-content" style={{ paddingTop: 4 }}>← Выберите файл</div>
        )}
        {selectedFile && (
          <div className="markdown-wrap">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{selectedFile.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  )
}
