import { useEffect, useState } from 'react'
import { api } from '../api'
import { Spinner } from '../components/Spinner'

interface Props {
  projectId: string
}

export function ActivityTab({ projectId }: Props) {
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setLines([])

    api.activity(projectId).then(d => {
      if (!cancelled) {
        // новые сверху
        setLines([...d.lines].reverse())
        setLoading(false)
      }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })

    return () => { cancelled = true }
  }, [projectId])

  if (loading) return <Spinner label="Загрузка активности..." />
  if (error) return <div className="error-state">⚠ {error}</div>

  if (lines.length === 0) {
    return <div className="no-content">Нет записей активности</div>
  }

  return (
    <div className="activity-log">
      {lines.map((line, i) => (
        <div key={i} className="activity-line">{line}</div>
      ))}
    </div>
  )
}
