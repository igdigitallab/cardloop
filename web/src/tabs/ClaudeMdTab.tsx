import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { ClaudeMd } from '../types'
import { Spinner } from '../components/Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'

interface Props {
  projectId: string
}

export function ClaudeMdTab({ projectId }: Props) {
  const [data, setData] = useState<ClaudeMd | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const reload = useCallback(() => {
    api.claudeMd(projectId).then(d => {
      setData(d); setError('')
    }).catch(e => setError(String(e.message || e)))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setData(null)

    api.claudeMd(projectId).then(d => {
      if (!cancelled) { setData(d); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  if (loading) return <Spinner label="Загрузка CLAUDE.md..." />
  if (error) return <div className="error-state">⚠ {error}</div>
  if (!data || !data.exists) {
    return (
      <div className="no-content">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
        Нет CLAUDE.md для этого проекта
        {data && <span style={{ color: 'var(--text3)', marginLeft: 6, fontSize: 11 }}>({data.path})</span>}
      </div>
    )
  }

  return (
    <div className="markdown-wrap">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.content}</ReactMarkdown>
    </div>
  )
}
