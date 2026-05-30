import { useEffect, useState } from 'react'
import { api } from '../api'

interface RawLimit {
  status: string
  resets_at: number | null
  utilization: number | null
  ts: number
}

interface UsageData {
  limits: Record<string, RawLimit>
  now: number
}

/** Форматирует «через 2ч 15м» или «12м». */
function fmtReset(resetsAt: number | null, now: number): string {
  if (!resetsAt) return '—'
  const delta = resetsAt - now
  if (delta <= 0) return 'скоро'
  const h = Math.floor(delta / 3600)
  const m = Math.floor((delta % 3600) / 60)
  return h > 0 ? `${h}ч ${m}м` : `${m}м`
}

/** Цвет по utilization (или по status). */
function pickClass(d: RawLimit | undefined): string {
  if (!d) return 'usage-dim'
  if (d.status === 'rejected') return 'usage-red'
  if (d.status === 'allowed_warning') return 'usage-yellow'
  const u = d.utilization ?? 0
  if (u > 0.85) return 'usage-red'
  if (u > 0.6) return 'usage-yellow'
  return 'usage-green'
}

function fmtPct(u: number | null): string {
  if (u == null) return ''
  return `${Math.round(u * 100)}%`
}

export function UsageBadge() {
  const [data, setData] = useState<UsageData | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const res = await api.usage()
        if (!cancelled) setData(res)
      } catch {
        if (!cancelled) setData(null)
      }
    }
    load()
    const id = setInterval(load, 30_000)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => {
      cancelled = true
      clearInterval(id)
      window.removeEventListener('focus', onFocus)
    }
  }, [])

  if (!data) return null

  const fiveH = data.limits.five_hour
  const week  = data.limits.seven_day
  const now   = data.now

  // Если ничего ещё не прилетало с SDK — placeholder
  if (!fiveH && !week) {
    return (
      <div className="usage-badge usage-dim" title="Лимиты придут с первым ответом от Claude">
        ⏱ —
      </div>
    )
  }

  return (
    <div className="usage-badge-wrap">
      <button
        className={`usage-badge ${pickClass(fiveH)}`}
        onClick={() => setOpen(o => !o)}
        title="Лимиты подписки Claude Code (клик — детали)"
      >
        {fiveH && (
          <>
            <span className="usage-icon">⏱</span>
            <span>5ч {fmtPct(fiveH.utilization)}</span>
            <span className="usage-sep">·</span>
            <span>{fmtReset(fiveH.resets_at, now)}</span>
          </>
        )}
        {!fiveH && week && (
          <>
            <span className="usage-icon">📅</span>
            <span>7д {fmtPct(week.utilization)}</span>
          </>
        )}
      </button>

      {open && (
        <div className="usage-dropdown" onClick={() => setOpen(false)}>
          {['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage'].map(k => {
            const d = data.limits[k]
            if (!d) return null
            const label = ({
              five_hour: '5-часовое окно',
              seven_day: 'Неделя (всё)',
              seven_day_opus: 'Неделя Opus',
              seven_day_sonnet: 'Неделя Sonnet',
              overage: 'Перерасход',
            } as Record<string, string>)[k]
            return (
              <div key={k} className={`usage-row ${pickClass(d)}`}>
                <span className="usage-row-label">{label}</span>
                <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                <span className="usage-row-reset">сброс {fmtReset(d.resets_at, now)}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
