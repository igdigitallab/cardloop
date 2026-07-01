import { useState, useEffect, useCallback, useRef } from 'react'
import { api, UsageDashboard, UsageLimits, UsageLedger } from '../api'
import { fmtReset, pickClass, fmtPct, LIMIT_LABELS } from '../components/usageFormat'

// Full historical cost/usage dashboard over ALL ~/.claude transcripts (CLI +
// Cardloop + sub-agents), indexed by usage_scanner.py. Hand-rolled CSS/SVG-free
// charts — zero bundle weight, achromatic Graphite & Chalk. Complements the live
// quota badge (/api/usage) and the Cardloop-only ledger (/api/usage/ledger).

const RANGES: { key: number | 'all'; label: string }[] = [
  { key: 1, label: 'Today' },
  { key: 7, label: '7d' },
  { key: 30, label: '30d' },
  { key: 90, label: '90d' },
  { key: 'all', label: 'All' },
]

// ── formatting ──────────────────────────────────────────────────────────────
function fmtTok(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}
function fmtCost(c: number): string {
  if (c >= 100) return '$' + c.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (c >= 1) return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return '$' + c.toFixed(2)
}
function fmtNum(n: number): string { return n.toLocaleString() }
function shortModel(m: string): string {
  const ml = m.toLowerCase()
  let fam = ''
  if (ml.includes('fable')) fam = 'Fable'
  else if (ml.includes('mythos')) fam = 'Mythos'
  else if (ml.includes('opus')) fam = 'Opus'
  else if (ml.includes('sonnet')) fam = 'Sonnet'
  else if (ml.includes('haiku')) fam = 'Haiku'
  if (fam) {
    const two = m.match(/(\d+)[._-](\d+)/)
    if (two) return `${fam} ${two[1]}.${two[2]}`
    const one = m.match(/(\d+)/)
    return one ? `${fam} ${one[1]}` : fam
  }
  return m.split('/').pop()!.split(':')[0].replace(/[-_]?\d{6,}.*$/, '') || m
}

// ── horizontal bar list (by-model / sub-agents) ─────────────────────────────
function BarList({ rows }: { rows: { label: string; cost: number; tokens: number; sub: string }[] }) {
  const max = Math.max(1, ...rows.map(r => r.cost))
  if (!rows.length) return <div className="usage-empty">No data in this range.</div>
  return (
    <div className="usage-barlist">
      {rows.map((r, i) => (
        <div className="usage-barrow" key={i} title={`${r.label} — ${fmtTok(r.tokens)} tokens · ${r.sub}`}>
          <span className="bl">{r.label}</span>
          <div className="usage-bartrack">
            <div className="usage-barfill" style={{ width: `${Math.max(2, (r.cost / max) * 100)}%` }} />
          </div>
          <span className="bv"><b>{fmtCost(r.cost)}</b> · {fmtTok(r.tokens)}</span>
        </div>
      ))}
    </div>
  )
}

// ── daily cost columns ──────────────────────────────────────────────────────
function DailyChart({ rows }: { rows: UsageDashboard['by_day'] }) {
  if (!rows.length) return <div className="usage-empty">No daily data in this range.</div>
  const max = Math.max(1, ...rows.map(r => r.cost))
  const first = rows[0].day.slice(5)
  const last = rows[rows.length - 1].day.slice(5)
  return (
    <>
      <div className="usage-daily">
        {rows.map(r => {
          const h = (r.cost / max) * 100
          return (
            <div
              key={r.day}
              className={`usage-col${r.cost <= 0 ? ' empty' : ''}`}
              style={{ height: `${Math.max(r.cost > 0 ? 3 : 1, h)}%` }}
              title={`${r.day}\n${fmtCost(r.cost)} · ${r.turns} turns\nout ${fmtTok(r.output)} · in ${fmtTok(r.input)} · cache-read ${fmtTok(r.cache_read)}`}
            />
          )
        })}
      </div>
      <div className="usage-daily-x"><span>{first}</span><span>{last}</span></div>
    </>
  )
}

// ── live subscription limits panel (card 3a) ─────────────────────────────────
function LiveLimitsPanel({ data }: { data: UsageLimits }) {
  const now = data.now
  const keys = ['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage']
  const rows = keys.map(k => ({ k, d: data.limits[k] })).filter(x => x.d)
  if (!rows.length) return <div className="usage-empty">No live limit data available yet.</div>
  return (
    <div className="usage-live-limits">
      {rows.map(({ k, d }) => (
        <div key={k} className={`usage-limit-row ${pickClass(d)}`}>
          <span className="usage-limit-label">{LIMIT_LABELS[k]}</span>
          <span className="usage-limit-pct">{fmtPct(d.utilization) || d.status}</span>
          <div className="usage-limit-bar">
            <div className="usage-limit-fill" style={{ width: `${Math.round((d.utilization ?? 0) * 100)}%` }} />
          </div>
          <span className="usage-limit-reset">resets {fmtReset(d.resets_at, now)}</span>
        </div>
      ))}
    </div>
  )
}

// ── 24-hour UTC cost chart (card 3b) ─────────────────────────────────────────
function HourChart({ rows }: { rows: { hour: number; turns: number; cost: number; input: number; output: number }[] }) {
  if (!rows.length) return <div className="usage-empty">No hourly data in this range.</div>
  const max = Math.max(1, ...rows.map(r => r.cost))
  return (
    <>
      <div className="usage-daily usage-hourly">
        {rows.map(r => {
          const h = (r.cost / max) * 100
          return (
            <div
              key={r.hour}
              className={`usage-col${r.cost <= 0 ? ' empty' : ''}`}
              style={{ height: `${Math.max(r.cost > 0 ? 3 : 1, h)}%` }}
              title={`Hour ${r.hour}:00 UTC\n${fmtCost(r.cost)} · ${r.turns} turns\nout ${fmtTok(r.output)} · in ${fmtTok(r.input)}`}
            />
          )
        })}
      </div>
      <div className="usage-daily-x"><span>0h UTC</span><span>23h UTC</span></div>
      <div className="usage-note" style={{ marginTop: '4px' }}>Times in UTC — Anthropic throttle windows reset on UTC hour boundaries.</div>
    </>
  )
}

// ── Delegation stacked daily chart ──────────────────────────────────────────
// Renders main_cost + sub_cost as two stacked segments per day column.
// Segment heights are proportional to their cost within the max total day.
interface DelegationDayRow { day: string; main_cost: number; sub_cost: number; main_turns: number; sub_turns: number }
function DelegationTrendChart({ rows }: { rows: DelegationDayRow[] }) {
  if (!rows.length) return <div className="usage-empty">No daily data in this range.</div>
  const totals = rows.map(r => r.main_cost + r.sub_cost)
  const max = Math.max(1, ...totals)
  const first = rows[0].day.slice(5)
  const last = rows[rows.length - 1].day.slice(5)
  return (
    <>
      <div className="usage-legend" style={{ marginBottom: '8px' }}>
        <span><i style={{ background: 'var(--text)' }} /> main (orchestrator)</span>
        <span><i style={{ background: 'var(--text3)' }} /> sub-agents (workers)</span>
      </div>
      <div className="usage-daily">
        {rows.map((r) => {
          const total = r.main_cost + r.sub_cost
          const mainH = total > 0 ? (r.main_cost / max) * 100 : 0
          const subH = total > 0 ? (r.sub_cost / max) * 100 : 0
          const totalH = mainH + subH
          return (
            <div
              key={r.day}
              className={`usage-col${total <= 0 ? ' empty' : ''}`}
              style={{
                height: `${Math.max(total > 0 ? 3 : 1, totalH)}%`,
                background: total <= 0
                  ? 'var(--bg4)'
                  : `linear-gradient(to top, var(--text3) ${Math.round((subH / totalH) * 100)}%, var(--text) ${Math.round((subH / totalH) * 100)}%)`,
              }}
              title={`${r.day}\nMain: ${fmtCost(r.main_cost)} · ${r.main_turns} turns\nSub: ${fmtCost(r.sub_cost)} · ${r.sub_turns} turns`}
            />
          )
        })}
      </div>
      <div className="usage-daily-x"><span>{first}</span><span>{last}</span></div>
    </>
  )
}

// ── Small bar for turns-only data (top_tools / by_effort) ───────────────────
function TurnsBar({ rows }: { rows: { label: string; turns: number; sub?: string }[] }) {
  const max = Math.max(1, ...rows.map(r => r.turns))
  if (!rows.length) return <div className="usage-empty">No data yet.</div>
  return (
    <div className="usage-barlist">
      {rows.map((r, i) => (
        <div className="usage-barrow" key={i} title={r.sub ?? `${r.label}: ${r.turns} turns`}>
          <span className="bl">{r.label}</span>
          <div className="usage-bartrack">
            <div className="usage-barfill" style={{ width: `${Math.max(2, (r.turns / max) * 100)}%` }} />
          </div>
          <span className="bv"><b>{fmtNum(r.turns)}</b> turns{r.sub ? ` · ${r.sub}` : ''}</span>
        </div>
      ))}
    </div>
  )
}

// ── Effort bar (cost + turns, from ledger.by_effort) ─────────────────────────
interface ByEffortRow { effort: string; turns: number; fresh_tokens: number; cost_usd: number }
function EffortBar({ rows }: { rows: ByEffortRow[] }) {
  const max = Math.max(1, ...rows.map(r => r.cost_usd))
  if (!rows.length) return <div className="usage-empty">No data yet.</div>
  return (
    <div className="usage-barlist">
      {rows.map((r, i) => (
        <div className="usage-barrow" key={i}
          title={`${r.effort} — ${fmtNum(r.turns)} turns · ${fmtTok(r.fresh_tokens)} fresh tokens`}>
          <span className="bl">{r.effort}</span>
          <div className="usage-bartrack">
            <div className="usage-barfill" style={{ width: `${Math.max(2, (r.cost_usd / max) * 100)}%` }} />
          </div>
          <span className="bv"><b>{fmtCost(r.cost_usd)}</b> · {fmtNum(r.turns)} turns</span>
        </div>
      ))}
    </div>
  )
}

// ── Cardloop vs CLI ledger panel (card 3c) ────────────────────────────────────
function LedgerPanel({ data }: { data: UsageLedger }) {
  const ep = data.by_entrypoint
  const epRows = Object.entries(ep).sort((a, b) => b[1].cost_usd - a[1].cost_usd)
  const maxCost = Math.max(1, ...epRows.map(([, v]) => v.cost_usd))
  const uc = data.ultracode
  return (
    <div className="usage-barlist">
      {epRows.map(([key, val]) => (
        <div className="usage-barrow" key={key} title={`${val.turns} turns · ${fmtTok(val.fresh_tokens)} fresh + ${fmtTok(val.cache_read_tokens)} cache`}>
          <span className="bl">{key}</span>
          <div className="usage-bartrack">
            <div className="usage-barfill" style={{ width: `${Math.max(2, (val.cost_usd / maxCost) * 100)}%` }} />
          </div>
          <span className="bv"><b>{fmtCost(val.cost_usd)}</b> · {fmtNum(val.turns)} turns</span>
        </div>
      ))}
      {uc.turns > 0 && (
        <div className="usage-barrow usage-barrow-accent" title="Turns with ultracode (max-effort) enabled">
          <span className="bl">ultracode</span>
          <div className="usage-bartrack">
            <div className="usage-barfill" style={{ width: `${Math.max(2, (uc.cost_usd / maxCost) * 100)}%` }} />
          </div>
          <span className="bv"><b>{fmtCost(uc.cost_usd)}</b> · {fmtNum(uc.turns)} turns</span>
        </div>
      )}
    </div>
  )
}

export function UsageTab() {
  const [data, setData] = useState<UsageDashboard | null>(null)
  const [liveLimits, setLiveLimits] = useState<UsageLimits | null>(null)
  const [ledger, setLedger] = useState<UsageLedger | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState<number | 'all'>(30)
  // null = no filter (all models); otherwise the selected subset.
  const [models, setModels] = useState<Set<string> | null>(null)
  const [modelPanel, setModelPanel] = useState(false)
  const [rescanning, setRescanning] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async (showSpinner = false) => {
    if (showSpinner) setLoading(true)
    try {
      const sel = models && models.size ? [...models] : undefined
      const [res, limits, ldg] = await Promise.all([
        api.usageDashboard(days, sel),
        api.usage().catch(() => null),
        api.usageLedger(days).catch(() => null),
      ])
      setData(res)
      if (limits) setLiveLimits(limits)
      if (ldg) setLedger(ldg)
      setError(null)
      // While a background scan is running, re-poll until it settles.
      if (res.scanning) {
        if (pollRef.current) clearTimeout(pollRef.current)
        pollRef.current = setTimeout(() => void load(false), 3500)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [days, models])

  useEffect(() => { void load(true) }, [load])
  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current) }, [])

  // Close the model panel on outside click.
  useEffect(() => {
    if (!modelPanel) return
    function onOut(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setModelPanel(false)
    }
    document.addEventListener('mousedown', onOut)
    return () => document.removeEventListener('mousedown', onOut)
  }, [modelPanel])

  const handleRescan = useCallback(async () => {
    setRescanning(true)
    try {
      await api.usageScan()
      await load(false)
    } catch { /* surfaced via the next load */ }
    finally { setRescanning(false) }
  }, [load])

  const toggleModel = useCallback((m: string, all: string[]) => {
    setModels(prev => {
      const next = new Set(prev ?? all)
      if (next.has(m)) next.delete(m); else next.add(m)
      return next.size === all.length ? null : next
    })
  }, [])

  const handleExport = useCallback(async () => {
    try {
      const sel = models && models.size ? [...models] : undefined
      const blob = await api.usageExport(days, sel)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `usage-export-${String(days)}d.csv`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a); URL.revokeObjectURL(url)
    } catch { /* silently ignore — server errors visible via the dashboard */ }
  }, [days, models])

  const ov = data?.overview
  const allModels = data?.all_models ?? []
  const modelLabel = !models || models.size === allModels.length
    ? 'All models'
    : models.size === 0 ? 'No models' : `${models.size} model${models.size > 1 ? 's' : ''}`

  return (
    <div className="usage-container">
      {/* ── Header / filters ── */}
      <div className="usage-head">
        <h2>Usage &amp; Cost</h2>
        <div className="usage-seg">
          {RANGES.map(r => (
            <button key={String(r.key)} className={days === r.key ? 'active' : ''} onClick={() => setDays(r.key)}>
              {r.label}
            </button>
          ))}
        </div>
        <div className="usage-modelsel" ref={panelRef}>
          <button className="usage-btn" onClick={() => setModelPanel(o => !o)}>
            {modelLabel} ▾
          </button>
          {modelPanel && (
            <div className="usage-modelsel-panel">
              <div className="usage-modelsel-actions">
                <button className="usage-btn" onClick={() => setModels(null)}>All</button>
                <button className="usage-btn" onClick={() => setModels(new Set())}>None</button>
              </div>
              {allModels.map(m => {
                const on = !models || models.has(m)
                return (
                  <div key={m} className={`usage-modelsel-row${on ? ' on' : ''}`} onClick={() => toggleModel(m, allModels)}>
                    <span className="usage-modelsel-box">✓</span>
                    <span>{m}</span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
        <span className="usage-spacer" />
        {data?.scanning && <span className="usage-scanning">indexing…</span>}
        <button className="usage-btn" onClick={handleExport} title="Download usage CSV (by project + branch)">
          ⬇ CSV
        </button>
        <button className="usage-btn" onClick={handleRescan} disabled={rescanning} title="Re-scan transcripts for new turns">
          {rescanning ? '…' : '↻ Rescan'}
        </button>
      </div>

      {loading && !data && <div className="usage-empty">Loading usage…</div>}
      {error && !data && <div className="usage-empty">Failed to load: {error}</div>}

      {data && !data.ready && (
        <div className="usage-empty">
          No usage indexed yet.{data.scanning ? ' Indexing transcripts — this can take ~30s on the first run…' : ''}
        </div>
      )}

      {ov && data?.ready && (
        <>
          {/* ── Overview stat cards ── */}
          <div className="usage-stats">
            <div className="usage-stat accent">
              <div className="lbl">Notional cost</div>
              <div className="val">{fmtCost(ov.cost)}</div>
              <div className="sub">list-price estimate</div>
            </div>
            <div className="usage-stat">
              <div className="lbl">Turns</div>
              <div className="val">{fmtNum(ov.turns)}</div>
              <div className="sub">{fmtNum(ov.sessions)} sessions</div>
            </div>
            <div className="usage-stat">
              <div className="lbl">Sub-agent cost</div>
              <div className="val">{fmtCost(ov.subagent_cost)}</div>
              <div className="sub">{ov.cost > 0 ? Math.round((ov.subagent_cost / ov.cost) * 100) : 0}% · {fmtNum(ov.subagent_turns)} turns</div>
            </div>
            <div className="usage-stat">
              <div className="lbl">Output tokens</div>
              <div className="val">{fmtTok(ov.output)}</div>
              <div className="sub">in {fmtTok(ov.input)}</div>
            </div>
            <div className="usage-stat">
              <div className="lbl">Cache read</div>
              <div className="val">{fmtTok(ov.cache_read)}</div>
              <div className="sub">cache-write {fmtTok(ov.cache_creation)}</div>
            </div>
          </div>

          {/* ── Live subscription limits (card 3a) ── */}
          {liveLimits && (
            <div className="usage-card">
              <div className="usage-card-head">
                <span className="usage-card-title">Live subscription limits</span>
                <span className="usage-note">current window utilization</span>
              </div>
              <LiveLimitsPanel data={liveLimits} />
            </div>
          )}

          {/* ── Daily cost ── */}
          <div className="usage-card">
            <div className="usage-card-head">
              <span className="usage-card-title">Daily cost (notional)</span>
            </div>
            <DailyChart rows={data.by_day} />
          </div>

          {/* ── Peak-hour view (card 3b) ── */}
          {data.by_hour && data.by_hour.length > 0 && (
            <div className="usage-card">
              <div className="usage-card-head">
                <span className="usage-card-title">Cost by hour of day (UTC)</span>
              </div>
              <HourChart rows={data.by_hour} />
            </div>
          )}

          {/* ── Cardloop vs CLI (card 3c) ── */}
          {ledger && Object.keys(ledger.by_entrypoint).length > 0 && (
            <div className="usage-card">
              <div className="usage-card-head">
                <span className="usage-card-title">Cardloop vs CLI</span>
                <span className="usage-note">Cardloop-originated turns only</span>
              </div>
              <LedgerPanel data={ledger} />
              <div className="usage-note" style={{ marginTop: '6px' }}>
                Cardloop-originated turns only, since the cost ledger was introduced (not retroactive).
              </div>
            </div>
          )}

          {/* ── By model ── */}
          <div className="usage-card">
            <div className="usage-card-head"><span className="usage-card-title">Cost by model</span></div>
            <BarList rows={data.by_model.map(m => ({
              label: shortModel(m.model),
              cost: m.cost,
              tokens: m.input + m.output + m.cache_read + m.cache_creation,
              sub: `${fmtNum(m.turns)} turns`,
            }))} />
          </div>

          {/* ── Sub-agents by type (ultracode fan-out) ── */}
          <div className="usage-card">
            <div className="usage-card-head">
              <span className="usage-card-title">Sub-agent cost by type</span>
              <span className="usage-note">fan-out (ultracode / Task)</span>
            </div>
            <BarList rows={data.subagents.map(s => ({
              label: s.agent_type,
              cost: s.cost,
              tokens: s.input + s.output + s.cache_read + s.cache_creation,
              sub: `${fmtNum(s.dispatches)} dispatches · ${fmtNum(s.turns)} turns`,
            }))} />
          </div>

          {/* ── Delegation & Ultracode ───────────────────────────────────────────── */}
          {/* Sourced from two independent backends; each panel carries its source label.
              Guard all new fields with optional-chaining — the backend track adds them
              in parallel; an older payload must not crash the tab. */}
          {(() => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const d = data as any
            const deleg = d?.delegation as {
              main: { turns: number; input: number; output: number; cache_read: number; cache_creation: number; cost: number }
              sub:  { turns: number; input: number; output: number; cache_read: number; cache_creation: number; cost: number }
              by_role_model: { role: 'main' | 'sub'; model: string; turns: number; input: number; output: number; cost: number }[]
              ratio_cost:  number
              ratio_turns: number
            } | undefined
            const delegByDay = d?.delegation_by_day as DelegationDayRow[] | undefined
            const health = d?.subagent_health as {
              dispatches: number; completed: number; other: number
              failure_rate_pct: number; avg_tool_uses: number; avg_duration_ms: number
              by_status: { status: string; count: number }[]
            } | undefined
            const topTools = d?.top_tools as { tool: string; turns: number }[] | undefined
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const ldgAny = ledger as any
            const ultDetail = ldgAny?.ultracode_detail as {
              on:  { turns: number; fresh_tokens: number; cache_read_tokens: number; cost_usd: number; avg_cost_per_turn: number }
              off: { turns: number; fresh_tokens: number; cache_read_tokens: number; cost_usd: number; avg_cost_per_turn: number }
              by_model: { model: string; turns: number; cost_usd: number }[]
            } | undefined
            const byEffort = ldgAny?.by_effort as ByEffortRow[] | undefined

            // Nothing to render if none of the new fields arrived yet.
            if (!deleg && !delegByDay && !health && !topTools && !ultDetail && !byEffort) return null

            const ratioTurnsPct = deleg ? Math.round(deleg.ratio_turns * 100) : 0
            const ratioCostPct  = deleg ? Math.round(deleg.ratio_cost  * 100) : 0
            const totalDelegCost = deleg ? deleg.main.cost + deleg.sub.cost : 0

            return (
              <>
                {/* ── Section header ── */}
                <div className="usage-card-head" style={{ marginBottom: 0 }}>
                  <span className="usage-card-title" style={{ fontSize: '13px', textTransform: 'none', letterSpacing: 0, fontWeight: 700, color: 'var(--text)' }}>
                    Delegation &amp; Ultracode
                  </span>
                </div>

                {/* 1. Headline: delegation ratio */}
                {deleg && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Delegation overview</span>
                      <span className="usage-note">all transcripts (Cardloop + CLI)</span>
                    </div>
                    <div className="usage-stats" style={{ marginBottom: '10px' }}>
                      <div className="usage-stat accent">
                        <div className="lbl">Sub-agent turns</div>
                        <div className="val">{ratioTurnsPct}%</div>
                        <div className="sub">{fmtNum(deleg.sub.turns)} of {fmtNum(deleg.main.turns + deleg.sub.turns)} total</div>
                      </div>
                      <div className="usage-stat">
                        <div className="lbl">Sub-agent cost</div>
                        <div className="val">{ratioCostPct}%</div>
                        <div className="sub">{fmtCost(deleg.sub.cost)} of {fmtCost(totalDelegCost)}</div>
                      </div>
                      <div className="usage-stat">
                        <div className="lbl">Orchestrator cost</div>
                        <div className="val">{fmtCost(deleg.main.cost)}</div>
                        <div className="sub">{fmtNum(deleg.main.turns)} turns</div>
                      </div>
                    </div>
                    <div className="usage-note">
                      Your main model plans and delegates; sub-agents (mostly Sonnet/Haiku) do the execution.
                    </div>
                  </div>
                )}

                {/* 2. Orchestrator vs Workers: two-group bar split by model */}
                {deleg && deleg.by_role_model && deleg.by_role_model.length > 0 && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Orchestrator vs Workers — cost by model</span>
                      <span className="usage-note">all transcripts (Cardloop + CLI)</span>
                    </div>
                    {/* Main (orchestrator) group */}
                    {deleg.by_role_model.filter(r => r.role === 'main').length > 0 && (
                      <>
                        <div className="usage-note" style={{ marginBottom: '6px', fontWeight: 600, color: 'var(--text2)' }}>
                          Orchestrator (main)
                        </div>
                        <BarList rows={deleg.by_role_model
                          .filter(r => r.role === 'main')
                          .map(r => ({
                            label: shortModel(r.model),
                            cost: r.cost,
                            tokens: r.input + r.output,
                            sub: `${fmtNum(r.turns)} turns`,
                          }))}
                        />
                      </>
                    )}
                    {deleg.by_role_model.filter(r => r.role === 'sub').length > 0 && (
                      <>
                        <div className="usage-note" style={{ marginTop: '14px', marginBottom: '6px', fontWeight: 600, color: 'var(--text2)' }}>
                          Workers (sub-agents)
                        </div>
                        <BarList rows={deleg.by_role_model
                          .filter(r => r.role === 'sub')
                          .map(r => ({
                            label: shortModel(r.model),
                            cost: r.cost,
                            tokens: r.input + r.output,
                            sub: `${fmtNum(r.turns)} turns`,
                          }))}
                        />
                      </>
                    )}
                    <div className="usage-note" style={{ marginTop: '8px' }}>
                      Opus thinks / Sonnet executes — the cost split reflects that contract.
                    </div>
                  </div>
                )}

                {/* 3. Delegation trend: stacked daily chart */}
                {delegByDay && delegByDay.length > 0 && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Delegation trend (daily)</span>
                      <span className="usage-note">all transcripts (Cardloop + CLI)</span>
                    </div>
                    <DelegationTrendChart rows={delegByDay} />
                  </div>
                )}

                {/* 4. Ultracode card */}
                {(ultDetail || byEffort) && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Ultracode (⚡ max effort)</span>
                      <span className="usage-note">Cardloop-only · not retroactive · orchestrator-side</span>
                    </div>
                    {ultDetail ? (
                      <>
                        <div className="usage-stats" style={{ marginBottom: '10px' }}>
                          <div className="usage-stat">
                            <div className="lbl">Runs with ultracode ON</div>
                            <div className="val">{fmtNum(ultDetail.on.turns)}</div>
                            <div className="sub">avg {fmtCost(ultDetail.on.avg_cost_per_turn ?? 0)}/turn</div>
                          </div>
                          <div className="usage-stat">
                            <div className="lbl">Runs without ultracode</div>
                            <div className="val">{fmtNum(ultDetail.off.turns)}</div>
                            <div className="sub">avg {fmtCost(ultDetail.off.avg_cost_per_turn ?? 0)}/turn</div>
                          </div>
                          <div className="usage-stat accent">
                            <div className="lbl">Orchestrator cost (ON)</div>
                            <div className="val">{fmtCost(ultDetail.on.cost_usd)}</div>
                            <div className="sub">{fmtTok(ultDetail.on.fresh_tokens + ultDetail.on.cache_read_tokens)} tok</div>
                          </div>
                        </div>
                        {ultDetail.by_model && ultDetail.by_model.length > 0 && (
                          <>
                            <div className="usage-note" style={{ marginBottom: '6px' }}>By model (ultracode ON):</div>
                            <BarList rows={ultDetail.by_model.map(r => ({
                              label: shortModel(r.model),
                              cost: r.cost_usd,
                              tokens: 0,
                              sub: `${fmtNum(r.turns)} turns`,
                            }))} />
                          </>
                        )}
                      </>
                    ) : (
                      <div className="usage-empty" style={{ padding: '16px 0' }}>No ultracode detail yet.</div>
                    )}

                    {/* Cost by effort */}
                    {byEffort && byEffort.length > 0 && (
                      <>
                        <div className="usage-note" style={{ marginTop: '14px', marginBottom: '6px', fontWeight: 600, color: 'var(--text2)' }}>
                          Cost by effort level <span style={{ fontWeight: 400 }}>(note: max ≈ ultracode)</span>
                        </div>
                        <EffortBar rows={byEffort} />
                      </>
                    )}

                    <div className="usage-note" style={{ marginTop: '10px' }}>
                      <b>Orchestrator-side only</b> — ultracode fan-out spawns sub-agent workers whose cost
                      appears in the Delegation split above, not here. Cardloop-originated turns only; not retroactive.
                    </div>
                  </div>
                )}

                {/* 5. Sub-agent health */}
                {health && health.dispatches > 0 && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Sub-agent health</span>
                      <span className="usage-note">all transcripts (Cardloop + CLI)</span>
                    </div>
                    <div className="usage-stats">
                      <div className="usage-stat">
                        <div className="lbl">Dispatches</div>
                        <div className="val">{fmtNum(health.dispatches)}</div>
                        <div className="sub">{fmtNum(health.completed)} completed</div>
                      </div>
                      <div className="usage-stat" style={health.failure_rate_pct > 10 ? { borderColor: 'var(--red)' } : {}}>
                        <div className="lbl">Failure rate</div>
                        <div className="val" style={{ color: health.failure_rate_pct > 10 ? 'var(--red)' : undefined }}>
                          {health.failure_rate_pct.toFixed(1)}%
                        </div>
                        <div className="sub">{fmtNum(health.other)} non-completed</div>
                      </div>
                      <div className="usage-stat">
                        <div className="lbl">Avg tools/agent</div>
                        <div className="val">{health.avg_tool_uses.toFixed(1)}</div>
                        <div className="sub">tool uses per dispatch</div>
                      </div>
                      <div className="usage-stat">
                        <div className="lbl">Avg duration</div>
                        <div className="val">{health.avg_duration_ms > 0 ? `${(health.avg_duration_ms / 1000).toFixed(0)}s` : '—'}</div>
                        <div className="sub">per dispatch</div>
                      </div>
                    </div>
                  </div>
                )}

                {/* 6. Top tools */}
                {topTools && topTools.length > 0 && (
                  <div className="usage-card">
                    <div className="usage-card-head">
                      <span className="usage-card-title">Top tools used</span>
                      <span className="usage-note">all transcripts (Cardloop + CLI)</span>
                    </div>
                    <TurnsBar rows={topTools.slice(0, 10).map(r => ({ label: r.tool, turns: r.turns }))} />
                  </div>
                )}
              </>
            )
          })()}

          {/* ── By project (card 3d — with branch when available) ── */}
          <div className="usage-card">
            <div className="usage-card-head"><span className="usage-card-title">Cost by project</span></div>
            <div className="usage-table-wrap">
              {data.by_project_branch && data.by_project_branch.length > 0 ? (
                <table className="usage-table">
                  <thead><tr>
                    <th>Project</th><th>Branch</th><th className="num">Sessions</th>
                    <th className="num">Turns</th><th className="num">Output</th><th className="num">Cost</th>
                  </tr></thead>
                  <tbody>
                    {data.by_project_branch.slice(0, 30).map((p, i) => (
                      <tr key={i}>
                        <td className="strong">{p.project}</td>
                        <td><span className="usage-tag">{p.branch || '—'}</span></td>
                        <td className="num">{fmtNum(p.sessions)}</td>
                        <td className="num">{fmtNum(p.turns)}</td>
                        <td className="num">{fmtTok(p.output)}</td>
                        <td className="num usage-cost">{fmtCost(p.cost)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table className="usage-table">
                  <thead><tr>
                    <th>Project</th><th className="num">Sessions</th><th className="num">Turns</th>
                    <th className="num">Output</th><th className="num">Cost</th>
                  </tr></thead>
                  <tbody>
                    {data.by_project.slice(0, 25).map((p, i) => (
                      <tr key={i}>
                        <td className="strong">{p.project}</td>
                        <td className="num">{fmtNum(p.sessions)}</td>
                        <td className="num">{fmtNum(p.turns)}</td>
                        <td className="num">{fmtTok(p.output)}</td>
                        <td className="num usage-cost">{fmtCost(p.cost)}</td>
                      </tr>
                    ))}
                    {!data.by_project.length && <tr><td colSpan={5} className="usage-empty">No data in this range.</td></tr>}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* ── Recent sessions ── */}
          <div className="usage-card">
            <div className="usage-card-head"><span className="usage-card-title">Recent sessions</span></div>
            <div className="usage-table-wrap">
              <table className="usage-table">
                <thead><tr>
                  <th>Project</th><th>Model</th><th className="num">Turns</th>
                  <th className="num">Output</th><th className="num">Cost</th><th>Last active</th>
                </tr></thead>
                <tbody>
                  {data.recent_sessions.slice(0, 30).map((s, i) => (
                    <tr key={i}>
                      <td className="strong" title={s.branch ? `branch: ${s.branch}` : undefined}>{s.project}</td>
                      <td><span className="usage-tag">{shortModel(s.model)}</span></td>
                      <td className="num">{fmtNum(s.turns)}</td>
                      <td className="num">{fmtTok(s.output)}</td>
                      <td className="num usage-cost">{fmtCost(s.cost)}</td>
                      <td>{s.last}</td>
                    </tr>
                  ))}
                  {!data.recent_sessions.length && <tr><td colSpan={6} className="usage-empty">No sessions yet.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="usage-note">
            Indexed from <b>~/.claude</b> transcripts (CLI + Cardloop + sub-agents) — retained even after Claude Code
            prunes old transcripts. Cost is a <b>notional</b> list-price estimate (Anthropic API pricing, {data.pricing_as_of});
            on a subscription the real cost is flat. Updated {data.generated_at}.
          </div>
        </>
      )}
    </div>
  )
}
