import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { Spec } from '../types'
import { Spinner } from '../components/Spinner'

interface Props {
  projectId: string
}

export function SpecsTab({ projectId }: Props) {
  const [specs, setSpecs] = useState<Spec[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [specContent, setSpecContent] = useState<string | null>(null)
  const [specLoading, setSpecLoading] = useState(false)
  const [specError, setSpecError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setSpecs([])
    setSelected(null)
    setSpecContent(null)

    api.specs(projectId).then(d => {
      if (!cancelled) { setSpecs(d.specs); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  function selectSpec(name: string) {
    if (selected === name) return
    setSelected(name)
    setSpecContent(null)
    setSpecError('')
    setSpecLoading(true)

    api.spec(projectId, name).then(d => {
      setSpecContent(d.content)
      setSpecLoading(false)
    }).catch(e => {
      setSpecError(String(e.message || e))
      setSpecLoading(false)
    })
  }

  if (loading) return <Spinner label="Загрузка спецификаций..." />
  if (error) return <div className="error-state">⚠ {error}</div>

  if (specs.length === 0) {
    return (
      <div className="no-content">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
          <polyline points="10 9 9 9 8 9"/>
        </svg>
        Нет спецификаций
      </div>
    )
  }

  return (
    <div className="specs-layout">
      <div className="specs-list">
        <div className="specs-list-label">Спеки</div>
        {specs.map(s => (
          <div
            key={s.name}
            className={`spec-item ${selected === s.name ? 'active' : ''}`}
            onClick={() => selectSpec(s.name)}
            title={s.path}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              style={{ flexShrink: 0, opacity: 0.5 }}>
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
            {s.name}
          </div>
        ))}
      </div>

      <div className="spec-content">
        {!selected && (
          <div className="no-content" style={{ paddingTop: 4 }}>← Выберите спеку</div>
        )}
        {selected && specLoading && <Spinner label="Загрузка..." />}
        {selected && specError && <div className="error-state">⚠ {specError}</div>}
        {selected && specContent !== null && !specLoading && (
          <div className="markdown-wrap">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{specContent}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  )
}
