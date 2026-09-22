/** spec-093: the ONE visual vocabulary for a runtime — `M 18% — 1h 9m`.
 *  Used by the top pill, the mobile composer pill, the pill's dropdown, the chat's model
 *  menu and the chat tabs, so the same subscription never looks two different ways. */
import { fmtPct, fmtReset, fmtResetShort, pickClass } from './usageFormat'
import { leadWindow, type RuntimeRow } from '../lib/runtimeStatus'

export function RuntimeTagChip({ tag, title, className = '' }: { tag: string; title?: string; className?: string }) {
  return <span className={`rt-tag ${className}`} title={title}>{tag}</span>
}

export interface RuntimeStats {
  /** "18%" (+ "*" when the reading is old), "local", "off" or "—". */
  pct: string
  /** "1h 9m" / "6d 2h" (compact: "1h" / "6d"); "" when there is nothing to count down to. */
  reset: string
  /** Colour class shared with the old badge: usage-green / -yellow / -red / -dim. */
  cls: string
  title: string
}

/** Everything a surface needs to print a runtime's status, derived one way for all of them. */
export function runtimeStats(row: RuntimeRow, now: number, compact = false): RuntimeStats {
  if (!row.hasQuota) {
    return {
      pct: row.available ? 'local' : 'off', reset: '',
      cls: row.available ? 'usage-green' : 'usage-dim',
      title: row.available
        ? `${row.name}: local model on your own GPU — spends no subscription`
        : `${row.name}: ${row.reason || 'not answering'}`,
    }
  }
  const lead = leadWindow(row, now)
  if (!lead) {
    const why = row.provider === 'codex'
      ? 'Codex reports its limits only while a Codex turn is running — none seen yet'
      : (row.available ? 'limits not read yet' : row.reason || 'unavailable')
    return { pct: '—', reset: '', cls: 'usage-dim', title: `${row.name}: ${why}` }
  }
  const base = fmtPct(lead.d.utilization) || (lead.d.status === 'rejected' ? 'limit' : '—')
  const pct = row.stale ? `${base}*` : base
  const reset = compact ? fmtResetShort(lead.d.resets_at, now) : (lead.d.resets_at ? fmtReset(lead.d.resets_at, now) : '')
  const window = lead.d.label || (lead.key === 'five_hour' ? '5-hour window'
    : lead.key === 'seven_day' ? 'weekly window' : lead.key)
  const age = row.ts != null ? Math.max(0, now - row.ts) : null
  const ageTxt = age == null ? '' : age < 3600 ? `${Math.max(1, Math.round(age / 60))}m` : `${Math.round(age / 3600)}h`
  return {
    pct, reset,
    // An unknown reading is not "0 %, all good": grey, not green.
    cls: row.stale || (lead.d.utilization == null && lead.d.status !== 'rejected')
      ? 'usage-dim' : pickClass(lead.d),
    title: `${row.name}${row.plan ? ` ${row.plan}` : ''}: ${base} of the ${window} used`
      + (lead.d.resets_at ? `, resets in ${fmtReset(lead.d.resets_at, now)}` : '')
      + (row.stale && ageTxt ? ` — reading is ${ageTxt} old` : ''),
  }
}

/** One runtime as a list row: `[M] Main MAX ········ 18% — 1h 9m  ●`. */
export function RuntimeLine({
  row, now, selected, pinnable, isDefault, disabled, onPick, trailing,
}: {
  row: RuntimeRow
  now: number
  selected?: boolean
  /** The chat INHERITS this selected runtime (no pin of its own): clicking it pins the chat
   *  here, so a later switch of the default no longer drags it along. */
  pinnable?: boolean
  /** Marks the runtime unpinned chats inherit. */
  isDefault?: boolean
  disabled?: boolean
  onPick?: () => void
  trailing?: React.ReactNode
}) {
  const s = runtimeStats(row, now)
  const inert = disabled || !onPick
  const pinHere = !!selected && !!pinnable
  return (
    <div
      role="option"
      aria-selected={!!selected}
      aria-disabled={inert}
      className={`rt-line${selected ? ' selected' : ''}${pinHere ? ' pin-here' : ''}${!row.available ? ' is-off' : ''}${inert ? ' inert' : ''}`}
      title={!row.available ? `${row.name}: ${row.reason || 'unavailable'}`
        : pinHere ? `${s.title}. This chat follows the default — click to pin it to ${row.name}`
        : s.title}
      onMouseDown={e => {
        e.preventDefault()
        if (inert || (selected && !pinnable)) return
        onPick?.()
      }}
    >
      <RuntimeTagChip tag={row.tag} />
      <span className="rt-line-name">
        {row.name}
        {row.plan && <span className="rt-line-plan">{row.plan}</span>}
        {isDefault && <span className="rt-line-default">default</span>}
      </span>
      <span className={`rt-line-stat ${s.cls}`}>
        {s.pct}{s.reset && <span className="rt-line-reset"> — {s.reset}</span>}
      </span>
      {/* ● = pinned to this chat, ○ = selected by inheritance (follows the default). */}
      <span className="rt-line-mark">{trailing ?? (selected ? (pinnable ? '○' : '●') : '')}</span>
    </div>
  )
}
