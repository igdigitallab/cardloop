import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { Board, BoardColumn } from '../types'
import { Spinner } from '../components/Spinner'

interface Props {
  projectId: string
}

const ORDER = ['backlog', 'in_progress', 'review', 'failed']

export function BoardTab({ projectId }: Props) {
  const [board, setBoard] = useState<Board | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [newText, setNewText] = useState('')
  const [showArchive, setShowArchive] = useState(false)
  const [archive, setArchive] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setBoard(null)
    setShowArchive(false); setArchive(null)
    api.tasks(projectId).then(b => {
      if (!cancelled) { setBoard(b); setLoading(false) }
    }).catch(e => {
      if (!cancelled) { setError(String(e.message || e)); setLoading(false) }
    })
    return () => { cancelled = true }
  }, [projectId])

  async function run(p: Promise<Board>) {
    setBusy(true); setError('')
    try { setBoard(await p) }
    catch (e: any) { setError(String(e.message || e)) }
    finally { setBusy(false) }
  }

  function addCard() {
    const t = newText.trim()
    if (!t) return
    setNewText('')
    run(api.createTask(projectId, t, 'backlog'))
  }

  function move(card: string, to: string) { run(api.moveTask(projectId, card, to)) }
  function del(card: string) { run(api.deleteTask(projectId, card)) }

  function toggleArchive() {
    if (showArchive) { setShowArchive(false); return }
    setShowArchive(true)
    if (archive === null) {
      api.tasksDone(projectId)
        .then(d => setArchive(d.content || '*Архив пуст*'))
        .catch(e => setArchive(`⚠ ${e.message || e}`))
    }
  }

  if (loading) return <Spinner label="Загрузка доски..." />

  const cols = board?.columns ?? []
  const colByKey = (k: string): BoardColumn | undefined => cols.find(c => c.key === k)

  return (
    <div className="board-wrap">
      {error && <div className="error-state" style={{ marginBottom: 10 }}>⚠ {error}</div>}

      <div className="board-columns">
        {ORDER.map(key => {
          const col = colByKey(key)
          if (!col) return null
          const idx = ORDER.indexOf(key)
          return (
            <div className={`board-col board-col-${key}`} key={key}>
              <div className="board-col-head">
                <span className="board-col-label">{col.label}</span>
                <span className="board-col-count">{col.cards.length}</span>
              </div>

              <div className="board-col-body">
                {col.cards.map(card => (
                  <div className="board-card" key={card.id}>
                    <div className="board-card-text">{card.text}</div>
                    <div className="board-card-actions">
                      <button title="← влево" disabled={busy || idx === 0}
                        onClick={() => move(card.id, ORDER[idx - 1])}>←</button>
                      <button title="вправо →" disabled={busy || idx === ORDER.length - 1}
                        onClick={() => move(card.id, ORDER[idx + 1])}>→</button>
                      <button title="✓ в Done (архив)" className="act-done" disabled={busy}
                        onClick={() => move(card.id, 'done')}>✓</button>
                      <button title="удалить" className="act-del" disabled={busy}
                        onClick={() => del(card.id)}>✕</button>
                    </div>
                  </div>
                ))}

                {key === 'backlog' && (
                  <div className="board-add">
                    <textarea
                      placeholder="Новая задача… (Enter — добавить)"
                      value={newText}
                      onChange={e => setNewText(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addCard() }
                      }}
                      rows={2}
                    />
                    <button className="btn-primary" disabled={busy || !newText.trim()}
                      onClick={addCard}>+ Добавить</button>
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="board-footer">
        <button className="board-archive-toggle" onClick={toggleArchive}>
          {showArchive ? '▾' : '▸'} Архив (Done) · {board?.done_count ?? 0}
        </button>
        {!board?.exists && (
          <span className="board-hint">TASKS.md ещё нет — создастся при первой задаче</span>
        )}
      </div>

      {showArchive && (
        <div className="board-archive">
          {archive === null
            ? <Spinner label="Загрузка архива..." />
            : <div className="markdown-wrap"><ReactMarkdown remarkPlugins={[remarkGfm]}>{archive}</ReactMarkdown></div>}
        </div>
      )}
    </div>
  )
}
