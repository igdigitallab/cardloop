import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type UsageLimits } from '../api'
import { fmtReset, pickClass, fmtPct, limitLabel } from './usageFormat'
import { RuntimeLine, RuntimeTagChip, runtimeStats } from './RuntimeTag'
import {
  useRuntimeStatus, buildRuntimeRows, globalDefaultAccount, refreshRuntimeStatus, serverNow, windowKeys,
  type RuntimeRow,
} from '../lib/runtimeStatus'

const USAGE_URL = 'https://claude.ai/settings/usage'

/** Open in a new tab. In an installed PWA `target=_blank` navigates inside the window —
 *  an explicit window.open on a user gesture more reliably opens an external browser. */
function openUsage(e: React.MouseEvent) {
  e.preventDefault()
  window.open(USAGE_URL, '_blank', 'noopener,noreferrer')
}

/** spec-093: the default for everything that carries no pin of its own — unpinned chats and
 *  board cards. This is the old "Subscription" switcher, kept as a separate, explicitly
 *  labelled line: the rows above it now act on THIS chat, and moving every chat at once must
 *  never be mistaken for moving one.
 *
 *  An inactive account's percentage can be missing or aged: only a running CLI refreshes
 *  that account's access token. Showing "—" is honest; inventing 0% would not be. */
function DefaultSwitch({ rows, now, projectAccount, onSwitched }: {
  rows: RuntimeRow[]
  now: number
  projectAccount: string | null
  onSwitched: (msg: string) => void
}) {
  const [busy, setBusy] = useState<string | null>(null)

  async function pick(r: RuntimeRow) {
    if (r.isGlobalDefault || busy || !r.account) return
    setBusy(r.account)
    try {
      const res = await api.accountActivate(r.account)
      onSwitched(res.in_flight > 0
        ? `Default is now ${r.name}. ${res.in_flight} run(s) already in flight stay on the old one.`
        : `Default is now ${r.name} — unpinned chats and board cards use it from the next turn.`)
    } catch (e) {
      onSwitched(`Could not switch: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(null)
      refreshRuntimeStatus()
    }
  }

  return (
    <div className="usage-accounts">
      <div className="usage-accounts-head">Default — unpinned chats &amp; board cards</div>
      <div className="rt-seg">
        {rows.map(r => {
          const s = runtimeStats(r, now, true)
          return (
            <button
              key={r.key}
              className={`rt-seg-btn${r.isGlobalDefault ? ' is-active' : ''}`}
              disabled={!r.available || busy != null}
              onClick={() => pick(r)}
              title={r.available ? s.title : `Unusable: ${r.reason}`}
            >
              <RuntimeTagChip tag={r.tag} />
              <span className={`rt-seg-pct ${s.cls}`}>{busy === r.account ? '…' : s.pct}</span>
            </button>
          )
        })}
      </div>
      {projectAccount && (
        <div className="usage-accounts-foot">
          This project is pinned to <b>{projectAccount}</b> (Settings) — its unpinned chats follow
          that, not this default.
        </div>
      )}
      {rows.some(r => !r.available) && (
        <div className="usage-accounts-foot">
          Greyed-out account needs a login: <code>tools/claude-acct login &lt;id&gt;</code>
        </div>
      )}
    </div>
  )
}

/** The row to show when the registry does not list the runtime on screen (first paint, a
 *  failed /api/agent-providers, Codex switched off, an account removed). Only the GLOBAL
 *  account's own limits are known without the registry, and they are attached ONLY when that
 *  is the runtime being shown — pinning them on a Codex or local chat would claim a quota that
 *  chat does not spend, the exact drift this pill exists to remove. */
function fallbackRow(key: string, globalKey: string, usage: UsageLimits | null, now: number): RuntimeRow {
  const [provider, account, backend] = key.split(':')
  const own = key === globalKey && !!usage
  const tag = provider === 'codex' ? 'C' : backend === 'ollama' ? 'L'
    : ((account || provider || '?')[0] || '?').toUpperCase()
  return {
    key, tag, name: account || backend || provider || key, plan: '',
    provider: (provider || 'claude') as RuntimeRow['provider'], account: account || null,
    backend: backend || '', available: own,
    reason: own ? undefined : 'not listed by the server right now',
    hasQuota: backend !== 'ollama', windows: own ? usage!.limits : null,
    ts: own ? now : null, stale: false, isGlobalDefault: own,
  }
}

/** spec-093: the runtime pill — `M 18% — 1h 9m` — for the chat on screen.
 *
 *  It used to show the GLOBAL account while each chat could run on its own, so the number
 *  on screen and the subscription a turn billed could silently differ. Now it follows the
 *  chat the visible ChatTab publishes; with no chat on screen it shows the global default.
 *  Codex and the local backend are rows of the same list, not a second pill. */
export function UsageBadge({ compact = false, onOpen }: { compact?: boolean; onOpen?: () => void } = {}) {
  const status = useRuntimeStatus()
  const { usage, providers, providersLoaded, current } = status
  const [hover, setHover] = useState(false)
  // compact (mobile): tap toggles the full breakdown instead of opening an external link.
  const [expanded, setExpanded] = useState(false)
  const [switchMsg, setSwitchMsg] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  // Mobile: the pill sits mid-composer, so an absolutely positioned panel anchored to it is
  // either too narrow to read or runs off an edge. Pin it full-width just above the pill.
  const [sheetBottom, setSheetBottom] = useState<number | null>(null)
  useEffect(() => {
    if (!compact || !expanded) { setSheetBottom(null); return }
    // Same coordinate space as position:fixed; re-measured while open, because the keyboard,
    // a rotation or a growing composer all move the pill.
    const measure = () => {
      const r = wrapRef.current?.getBoundingClientRect()
      if (r) setSheetBottom(Math.max(8, window.innerHeight - r.top + 6))
    }
    measure()
    const vv = window.visualViewport
    window.addEventListener('resize', measure)
    vv?.addEventListener('resize', measure)
    vv?.addEventListener('scroll', measure)
    return () => {
      window.removeEventListener('resize', measure)
      vv?.removeEventListener('resize', measure)
      vv?.removeEventListener('scroll', measure)
    }
  }, [compact, expanded])

  // Hover opens the desktop dropdown; a TOUCH "hover" (the synthetic mouseenter a tap fires)
  // has no mouseleave to close it, so it is ignored and any outside press closes both states.
  const showDropdown = compact ? expanded : (hover || expanded)
  useEffect(() => {
    if (!showDropdown) return
    function onOut(e: PointerEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setExpanded(false)
        setHover(false)
      }
    }
    document.addEventListener('pointerdown', onOut)
    return () => document.removeEventListener('pointerdown', onOut)
  }, [showDropdown])

  const rows = useMemo(() => buildRuntimeRows(providers, usage), [providers, usage])
  // Countdowns run on the server clock advanced locally; re-render twice a minute even when a
  // poll fails, so "resets in 9m" never freezes on screen.
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  if (!usage && !providersLoaded) return null

  const now = serverNow(status)
  const globalKey = `claude:${globalDefaultAccount(providers, usage)}:`
  const shownKey = current?.runtimeKey ?? globalKey
  const shown: RuntimeRow = rows.find(r => r.key === shownKey) ?? fallbackRow(shownKey, globalKey, usage, now)

  const stats = runtimeStats(shown, now, compact)
  const claudeAccounts = rows.filter(r => r.provider === 'claude' && r.account)
  const scope = current ? 'this chat' : 'default (no chat on screen)'

  function handleSwitched(msg: string) {
    setSwitchMsg(msg)
    setTimeout(() => setSwitchMsg(''), 6000)
  }

  const pillBody = (
    <>
      <RuntimeTagChip tag={shown.tag} />
      <span>{stats.pct}</span>
      {stats.reset && !compact && <span className="usage-sep">—</span>}
      {stats.reset && <span className={compact ? 'rt-pill-reset-short' : undefined}>{stats.reset}</span>}
    </>
  )

  return (
    <div
      className={`usage-badge-wrap${compact ? ' usage-compact' : ''}`}
      ref={wrapRef}
      onPointerEnter={e => { if (e.pointerType === 'mouse') setHover(true) }}
      onPointerLeave={e => { if (e.pointerType === 'mouse') setHover(false) }}
    >
      {compact ? (
        // Mobile: a button that toggles the full breakdown (no external navigation).
        <button
          className={`usage-badge ${stats.cls}`}
          onClick={() => setExpanded(e => !e)}
          title={`${stats.title} — ${scope}. Tap for every subscription`}
          aria-expanded={expanded}
        >
          {pillBody}
        </button>
      ) : (
        <a
          className={`usage-badge ${stats.cls}`}
          href={USAGE_URL}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => { if (onOpen) { e.preventDefault(); onOpen() } else { openUsage(e) } }}
          title={`${stats.title} — ${scope}. Click for Usage & cost, hover for every subscription`}
        >
          {pillBody}
        </a>
      )}

      {showDropdown && (
        <div
          className="usage-dropdown"
          style={compact && sheetBottom != null
            ? { position: 'fixed', left: 8, right: 8, bottom: sheetBottom, top: 'auto', maxWidth: 'none' }
            : undefined}
        >
          {/* Headline action: our own usage & cost dashboard. The desktop tab-bar badge passes
              onOpen directly; the mobile composer badge (deep in ChatTab, no handler) falls
              back to a window event App listens for. */}
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

          <div className="usage-accounts-head">
            {current
              ? `This chat · ${current.projectName}${current.chatName ? ` / ${current.chatName}` : ''}`
              : 'Subscriptions'}
            {current?.busy && <span className="rt-head-note"> · busy — switch after the turn</span>}
          </div>
          <div role="listbox" className="rt-list">
            {rows.map(r => {
              const offBackend = !!current?.projectBackend && r.key !== `claude::${current.projectBackend}`
              return (
                <RuntimeLine
                  key={r.key}
                  row={r}
                  now={now}
                  selected={r.key === shownKey}
                  pinnable={!!current && !current.pinned}
                  isDefault={current ? r.key === current.inheritedKey : r.isGlobalDefault}
                  disabled={!current || !r.available || current.busy || offBackend}
                  onPick={current ? () => current.pick(r.key) : undefined}
                />
              )
            })}
            {current?.pinned && !current.projectBackend && (
              <div
                role="option"
                aria-selected={false}
                className={`rt-line rt-follow${current.busy ? ' inert' : ''}`}
                title="Drop this chat's own pin: it will follow the default, including future switches"
                onMouseDown={e => { e.preventDefault(); if (!current.busy) current.followDefault() }}
              >
                <span className="rt-follow-icon">↺</span>
                <span className="rt-line-name">Follow default</span>
                <span className="rt-line-stat">{rows.find(r => r.key === current.inheritedKey)?.tag ?? ''}</span>
                <span className="rt-line-mark" />
              </div>
            )}
          </div>
          {current?.error && <div className="usage-accounts-msg rt-error">{current.error}</div>}

          {/* Every window of the runtime on screen — the pill only carries the binding one. */}
          {shown.windows ? (
            <div className="rt-windows">
              {windowKeys(shown).map(k => {
                const d = shown.windows![k]
                return (
                  <div key={k} className={`usage-row ${shown.stale || d.utilization == null ? (d.status === 'rejected' ? 'usage-red' : 'usage-dim') : pickClass(d)}`}>
                    <span className="usage-row-label">{shown.tag} · {limitLabel(k, d)}</span>
                    <span className="usage-row-pct">{fmtPct(d.utilization) || d.status}</span>
                    <span className="usage-row-reset">
                      {d.resets_at ? `resets ${fmtReset(d.resets_at, now)}` : ''}
                    </span>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="usage-accounts-foot">{stats.title}</div>
          )}

          {claudeAccounts.length > 1 && (
            <DefaultSwitch
              rows={claudeAccounts}
              now={now}
              projectAccount={current?.projectAccount ?? null}
              onSwitched={handleSwitched}
            />
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
  )
}
