import { useState, useEffect, useCallback, useRef } from 'react'
import { api, UsageDashboard } from '../api'

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

export function UsageTab() {
  const [data, setData] = useState<UsageDashboard | null>(null)
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
      const res = await api.usageDashboard(days, sel)
      setData(res)
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

          {/* ── Daily cost ── */}
          <div className="usage-card">
            <div className="usage-card-head">
              <span className="usage-card-title">Daily cost (notional)</span>
            </div>
            <DailyChart rows={data.by_day} />
          </div>

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

          {/* ── By project ── */}
          <div className="usage-card">
            <div className="usage-card-head"><span className="usage-card-title">Cost by project</span></div>
            <div className="usage-table-wrap">
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
