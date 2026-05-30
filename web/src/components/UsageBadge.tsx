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

/** Цвет по utilization: <50% зелёный, 50–80% жёлтый, ≥80% красный. */
function pickClass(d: RawLimit | undefined): string {
  if (!d) return 'usage-dim'
  if (d.status === 'rejected') return 'usage-red'
  if (d.status === 'allowed_warning') return 'usage-yellow'
  const u = d.utilization ?? 0
  if (u >= 0.8) return 'usage-red'
  if (u >= 0.5) return 'usage-yellow'
  return 'usage-green'
}

const USAGE_URL = 'https://claude.ai/settings/usage'

/** Открыть в новой вкладке. В установленном PWA `target=_blank` навигирует внутри окна —
 *  явный window.open на жесте пользователя надёжнее выкидывает во внешний браузер. */
function openUsage(e: React.MouseEvent) {
  e.preventDefault()
  window.open(USAGE_URL, '_blank', 'noopener,noreferrer')
}

function fmtPct(u: number | null): string {
  if (u == null) return ''
  return `${Math.round(u * 100)}%`
}

export function UsageBadge() {
  const [data, setData] = useState<UsageData | null>(null)
  const [hover, setHover] = useState(false)

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
      <a
        className="usage-badge usage-dim"
        href={USAGE_URL}
        target="_blank"
        rel="noopener noreferrer"
        onClick={openUsage}
        title="Лимиты загружаются… (клик — claude.ai/settings/usage)"
      >
        ⏱ —
      </a>
    )
  }

  // Основной показатель — 5-часовое окно, иначе недельное.
  const primary = fiveH ?? week!
  const icon = fiveH ? '⏱' : '📅'
  const pct = fmtPct(primary.utilization)

  return (
    <div
      className="usage-badge-wrap"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <a
        className={`usage-badge ${pickClass(primary)}`}
        href={USAGE_URL}
        target="_blank"
        rel="noopener noreferrer"
        onClick={openUsage}
        title="Лимиты подписки Claude Code (клик — claude.ai/settings/usage)"
      >
        <span className="usage-icon">{icon}</span>
        {pct && <span>{pct}</span>}
        {pct && <span className="usage-sep">—</span>}
        <span>{fmtReset(primary.resets_at, now)}</span>
      </a>

      {hover && (
        <div className="usage-dropdown">
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
