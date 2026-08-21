import { useEffect, useRef, useState } from 'react'
import { api, type CodexLimits, type AccountRow } from '../api'
import { fmtReset, pickClass, fmtPct, orderedLimitKeys, limitLabel, type RawLimit } from './usageFormat'

interface UsageData {
  limits: Record<string, RawLimit>
  codex?: CodexLimits | null
  now: number
  account?: string
  accounts?: AccountRow[] | null
}

/** Account switcher — rendered inside the limits dropdown, and only when a second
 *  subscription exists. This is where the operator already looks to see how much is left,
 *  so it is where the "move to the other one" action belongs.
 *
 *  An inactive account's percentage can be missing or aged: only a running CLI refreshes
 *  that account's access token. Showing "—" is honest; inventing 0% would not be. */
function AccountSwitcher({ accounts, now, onSwitched }: {
  accounts: AccountRow[]
  now: number
  onSwitched: (msg: string) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)

  async function pick(a: AccountRow) {
    if (a.active || busy) return
    setBusy(a.id)
    try {
      const res = await api.accountActivate(a.id)
      onSwitched(res.in_flight > 0
        ? `New runs use ${a.label}. ${res.in_flight} run(s) already in flight stay on the old one.`
        : `New runs use ${a.label}.`)
    } catch (e) {
      onSwitched(`Could not switch: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="usage-accounts">
      <div className="usage-accounts-head">Subscription</div>
      {accounts.map(a => {
        const lead = a.limits?.five_hour ?? a.limits?.seven_day ?? null
        const pct = lead ? fmtPct(lead.utilization) : ''
        const aged = a.limits_ts != null && now - a.limits_ts > 900
        return (
          <button
            key={a.id}
            className={`usage-account-row${a.active ? ' is-active' : ''}${a.ok ? '' : ' is-broken'}`}
            disabled={!a.ok || busy != null}
            onClick={() => pick(a)}
            title={a.ok
              ? (a.email || a.label) + (a.shared_ok ? '' : ' — history is NOT shared with the main account')
              : `Unusable: ${a.reason}`}
          >
            <span className="usage-account-mark">{a.active ? '●' : '○'}</span>
            <span className="usage-account-label">
              {a.label}
              {a.plan && <span className="usage-account-plan"> {a.plan}</span>}
            </span>
            <span className={`usage-account-pct ${lead ? pickClass(lead) : 'usage-dim'}`}>
              {busy === a.id ? '…' : (pct ? (aged ? `${pct}*` : pct) : '—')}
            </span>
          </button>
        )
      })}
      {accounts.some(a => !a.ok) && (
        <div className="usage-accounts-foot">
          Greyed-out account needs a login: <code>tools/claude-acct login &lt;id&gt;</code>
        </div>
      )}
    </div>
  )
}

/** A Codex snapshot older than this is shown dimmed — it predates at least one short window. */
const CODEX_STALE_AFTER_SEC = 6 * 3600

const USAGE_URL = 'https://claude.ai/settings/usage'

/** Open in a new tab. In an installed PWA `target=_blank` navigates inside the window —
 *  an explicit window.open on a user gesture more reliably opens an external browser. */
function openUsage(e: React.MouseEvent) {
  e.preventDefault()
  window.open(USAGE_URL, '_blank', 'noopener,noreferrer')
}

/** Second pill: Codex subscription windows, shown only once Codex has actually reported them.
 *  Codex pushes limits mid-turn and offers no way to ask, so an idle install has nothing to
 *  show — a placeholder would be inventing a number. */
function CodexPill({ codex, now, compact }: { codex: CodexLimits; now: number; compact: boolean }) {
  const [hover, setHover] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!expanded) return
    function onOut(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setExpanded(false)
    }
    document.addEventListener('mousedown', onOut)
    return () => document.removeEventListener('mousedown', onOut)
  }, [expanded])

  const keys = Object.keys(codex.limits)
  if (!keys.length) return null

  // Lead with whichever window is closest to its ceiling — that is the one about to bite.
  const leadKey = keys.reduce((a, b) =>
    (codex.limits[b].utilization ?? -1) > (codex.limits[a].utilization ?? -1) ? b : a)
  const lead = codex.limits[leadKey]
  const ageSec = codex.ts ? now - codex.ts : null
  const stale = ageSec != null && ageSec > CODEX_STALE_AFTER_SEC
  const pct = fmtPct(lead.utilization)
  const cls = stale ? 'usage-dim' : pickClass(lead)
  const title = stale
    ? `Codex limits as of ${fmtAge(ageSec)} ago — Codex only reports them during a run`
    : 'Codex subscription limits'

  return (
    <div
      className={`usage-badge-wrap${compact ? ' usage-compact' : ''}`}
      ref={wrapRef}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        className={`usage-badge ${cls}`}
        onClick={() => setExpanded(e => !e)}
        title={title}
        aria-expanded={expanded}
      >
        <span className="usage-badge-provider">gpt</span>
        {pct && <span>{pct}</span>}
        {!compact && pct && <span className="usage-sep">—</span>}
        {!compact && <span>{fmtReset(lead.resets_at, now)}</span>}
      </button>

      {(hover || expanded) && (
        <div className="usage-dropdown">
          {keys.map(k => {
            const d = codex.limits[k]
            return (
              <div key={k} className={`usage-row ${stale ? 'usage-dim' : pickClass(d)}`}>
                <span className="usage-row-label">{limitLabel(k, d)}</span>
                <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                <span className="usage-row-reset">
                  {d.resets_at ? `resets ${fmtReset(d.resets_at, now)}` : ''}
                </span>
              </div>
            )
          })}
          <div className="usage-badge-foot">
            {codex.plan_type ? `Codex · ${codex.plan_type}` : 'Codex'}
            {ageSec != null && ` · seen ${fmtAge(ageSec)} ago`}
          </div>
        </div>
      )}
    </div>
  )
}

/** Coarse age for the snapshot line: "12m", "3h", "2d". */
function fmtAge(sec: number): string {
  if (sec < 3600) return `${Math.max(1, Math.floor(sec / 60))}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`
  return `${Math.floor(sec / 86400)}d`
}

export function UsageBadge({ compact = false, onOpen }: { compact?: boolean; onOpen?: () => void } = {}) {
  const [data, setData] = useState<UsageData | null>(null)
  const [hover, setHover] = useState(false)
  // compact (mobile): tap toggles the full breakdown instead of opening an external link.
  const [expanded, setExpanded] = useState(false)
  const [switchMsg, setSwitchMsg] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  const reloadRef = useRef<() => void>(() => {})

  useEffect(() => {
    if (!expanded) return
    function onOut(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setExpanded(false)
    }
    document.addEventListener('mousedown', onOut)
    return () => document.removeEventListener('mousedown', onOut)
  }, [expanded])

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
    reloadRef.current = load
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

  const codex = data.codex?.limits ? data.codex : null
  // Only shown once a second subscription is registered; a single-account install is unchanged.
  const accounts = data.accounts && data.accounts.length > 1 ? data.accounts : null
  const activeAcct = accounts?.find(a => a.active) ?? null
  // Working on anything other than the main subscription must be visible at a glance —
  // silently spending the wrong account's quota is the failure mode worth designing against.
  const acctTag = activeAcct && !activeAcct.is_main ? activeAcct.label : ''

  function handleSwitched(msg: string) {
    setSwitchMsg(msg)
    reloadRef.current()
    setTimeout(() => setSwitchMsg(''), 6000)
  }

  // Nothing from SDK yet — show placeholder
  if (!fiveH && !week) {
    return (
      <>
        <a
          className="usage-badge usage-dim"
          href={USAGE_URL}
          target="_blank"
          rel="noopener noreferrer"
          onClick={openUsage}
          title="Limits loading… (click — claude.ai/settings/usage)"
        >
          ⏱ —
        </a>
        {codex && <CodexPill codex={codex} now={now} compact={compact} />}
      </>
    )
  }

  // Primary indicator — 5-hour window, otherwise weekly.
  const primary = fiveH ?? week!
  const icon = fiveH ? '⏱' : '📅'
  const pct = fmtPct(primary.utilization)

  const showDropdown = hover || expanded

  return (
    <>
    <div
      className={`usage-badge-wrap${compact ? ' usage-compact' : ''}`}
      ref={wrapRef}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {compact ? (
        // Mobile: a button that toggles the full breakdown (no external navigation).
        <button
          className={`usage-badge ${pickClass(primary)}`}
          onClick={() => setExpanded(e => !e)}
          title="Subscription limits — tap for the full breakdown"
          aria-expanded={expanded}
        >
          <span className="usage-icon">{icon}</span>
          {pct && <span>{pct}</span>}
          {acctTag && <span className="usage-acct-tag">{acctTag}</span>}
        </button>
      ) : (
        <a
          className={`usage-badge ${pickClass(primary)}`}
          href={USAGE_URL}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => { if (onOpen) { e.preventDefault(); onOpen() } else { openUsage(e) } }}
          title={onOpen ? 'Usage & cost dashboard (hover for live limits)' : 'Claude Code subscription limits (click — claude.ai/settings/usage)'}
        >
          <span className="usage-icon">{icon}</span>
          {pct && <span>{pct}</span>}
          {pct && <span className="usage-sep">—</span>}
          <span>{fmtReset(primary.resets_at, now)}</span>
          {acctTag && <span className="usage-acct-tag">{acctTag}</span>}
        </a>
      )}

      {showDropdown && (
        <div className="usage-dropdown">
          {/* Headline action: our own usage & cost dashboard — now the badge's primary
              destination (replaces the old claude.ai link). The desktop tab-bar badge
              passes onOpen directly; the mobile composer badge (deep in ChatTab, no
              handler) falls back to a window event App listens for. */}
          <button
            className="usage-dropdown-cta"
            onClick={() => {
              setExpanded(false); setHover(false)
              if (onOpen) onOpen()
              else window.dispatchEvent(new CustomEvent('cops:open-usage'))
            }}
          >
            <span>📊 Usage &amp; cost</span>
            <span className="usage-dropdown-cta-arrow">→</span>
          </button>
          {orderedLimitKeys(data.limits).map(k => {
            const d = data.limits[k]
            if (!d) return null
            const label = limitLabel(k, d)
            return (
              <div key={k} className={`usage-row ${pickClass(d)}`}>
                <span className="usage-row-label">{label}</span>
                <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                <span className="usage-row-reset">resets {fmtReset(d.resets_at, now)}</span>
              </div>
            )
          })}
          {accounts && (
            <AccountSwitcher accounts={accounts} now={now} onSwitched={handleSwitched} />
          )}
          {switchMsg && <div className="usage-accounts-msg">{switchMsg}</div>}
          <a
            className="usage-dropdown-claude"
            href={USAGE_URL}
            target="_blank"
            rel="noopener noreferrer"
            onClick={openUsage}
          >
            Limits on claude.ai ↗
          </a>
        </div>
      )}
    </div>
    {codex && <CodexPill codex={codex} now={now} compact={compact} />}
    </>
  )
}
