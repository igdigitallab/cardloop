import { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { t } from '../i18n'

export interface ScheduleRecord {
  id: string
  source: string
  schedule: string
  command: string
  project: string | null
  last_run: string | null
  next_run: string | null
  status: 'ok' | 'stale' | 'broken' | 'unknown'
  purpose: string | null
  annotations: Record<string, unknown>
}

export interface SchedulesResponse {
  scanned_at: string | null
  source_statuses: Array<{ source: string; status: string; count?: number; error?: string }>
  records: ScheduleRecord[]
}

const STATUS_COLORS: Record<string, string> = {
  ok: '#22c55e',
  stale: '#f59e0b',
  broken: '#ef4444',
  unknown: '#6b7280',
}

const STATUS_BG: Record<string, string> = {
  ok: '#dcfce7',
  stale: '#fef3c7',
  broken: '#fee2e2',
  unknown: '#f3f4f6',
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: 12,
      fontSize: 12,
      fontWeight: 600,
      color: STATUS_COLORS[status] ?? '#6b7280',
      background: STATUS_BG[status] ?? '#f3f4f6',
      border: `1px solid ${STATUS_COLORS[status] ?? '#6b7280'}33`,
    }}>
      {status}
    </span>
  )
}

function formatTs(ts: string | null): string {
  if (!ts) return '—'
  try {
    const d = new Date(ts)
    if (isNaN(d.getTime())) return ts
    return d.toLocaleString()
  } catch {
    return ts
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '…' : s
}

export function SchedulesTab() {
  const [data, setData] = useState<SchedulesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [investigatingId, setInvestigatingId] = useState<string | null>(null)
  const [cancellingId, setCancellingId] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  // Filters
  const [filterSource, setFilterSource] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterProject, setFilterProject] = useState('')

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams()
      if (filterSource) params.set('source', filterSource)
      if (filterStatus) params.set('status', filterStatus)
      if (filterProject) params.set('project', filterProject)
      const qs = params.toString() ? `?${params.toString()}` : ''
      const resp = await api.schedules(qs)
      setData(resp)
      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filterSource, filterStatus, filterProject])

  useEffect(() => {
    setLoading(true)
    load()
  }, [load])

  const handleScan = async () => {
    setScanning(true)
    try {
      await api.schedulesScan()
      // Wait 2s then reload
      setTimeout(() => {
        setLoading(true)
        load().finally(() => setScanning(false))
      }, 2000)
    } catch (e: unknown) {
      setScanning(false)
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleInvestigate = async (record: ScheduleRecord) => {
    setInvestigatingId(record.id)
    try {
      const res = await api.schedulesInvestigate(record.id)
      showToast(`${t['schedules.investigate_created']}: ${res.card_id}`)
      load()
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
    } finally {
      setInvestigatingId(null)
    }
  }

  const handleCancel = async (record: ScheduleRecord) => {
    const deferredId = record.annotations?.deferred_id as string | undefined
    if (!deferredId) return
    setCancellingId(record.id)
    try {
      await api.deferredDelete(deferredId)
      showToast(t['schedules.deferred_cancelled'])
      load()
    } catch (e: unknown) {
      showToast(e instanceof Error ? e.message : String(e))
    } finally {
      setCancellingId(null)
    }
  }

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }

  const records = data?.records ?? []

  // Unique sources/projects for filter dropdowns
  const allSources = Array.from(new Set((data?.records ?? []).map(r => r.source))).sort()
  const allProjects = Array.from(new Set((data?.records ?? []).map(r => r.project).filter(Boolean) as string[])).sort()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '12px 16px',
        borderBottom: '1px solid var(--border)',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        flexShrink: 0,
        flexWrap: 'wrap',
      }}>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>{t['schedules.title']}</h2>
        {data?.scanned_at && (
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            {t['schedules.last_scan']}: {formatTs(data.scanned_at)}
          </span>
        )}
        <button
          className="btn btn-secondary btn-sm"
          onClick={handleScan}
          disabled={scanning}
          style={{ marginLeft: 'auto' }}
        >
          {scanning ? t['schedules.scanning'] : t['schedules.scan_now']}
        </button>
      </div>

      {/* Filter bar */}
      <div style={{
        padding: '8px 16px',
        borderBottom: '1px solid var(--border)',
        display: 'flex',
        gap: 8,
        flexShrink: 0,
        flexWrap: 'wrap',
        alignItems: 'center',
      }}>
        <select
          value={filterSource}
          onChange={e => setFilterSource(e.target.value)}
          style={{ fontSize: 13, padding: '3px 6px', borderRadius: 4, border: '1px solid var(--border)' }}
        >
          <option value="">{t['schedules.filter_all_sources']}</option>
          {allSources.map(s => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <select
          value={filterStatus}
          onChange={e => setFilterStatus(e.target.value)}
          style={{ fontSize: 13, padding: '3px 6px', borderRadius: 4, border: '1px solid var(--border)' }}
        >
          <option value="">{t['schedules.filter_all_statuses']}</option>
          <option value="ok">ok</option>
          <option value="stale">stale</option>
          <option value="broken">broken</option>
          <option value="unknown">unknown</option>
        </select>
        <select
          value={filterProject}
          onChange={e => setFilterProject(e.target.value)}
          style={{ fontSize: 13, padding: '3px 6px', borderRadius: 4, border: '1px solid var(--border)' }}
        >
          <option value="">{t['schedules.filter_all_projects']}</option>
          {allProjects.map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          {records.length} {t['schedules.entries']}
        </span>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: 'auto' }}>
        {loading && (
          <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)' }}>
            {t['schedules.loading']}
          </div>
        )}
        {error && (
          <div style={{ padding: 16, color: '#ef4444' }}>{error}</div>
        )}
        {!loading && !error && records.length === 0 && (
          <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-muted)' }}>
            {t['schedules.empty']}
          </div>
        )}
        {!loading && !error && records.length > 0 && (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ background: 'var(--bg-secondary)', position: 'sticky', top: 0 }}>
                <th style={thStyle}>{t['schedules.col_schedule']}</th>
                <th style={thStyle}>{t['schedules.col_command']}</th>
                <th style={thStyle}>{t['schedules.col_source']}</th>
                <th style={thStyle}>{t['schedules.col_project']}</th>
                <th style={thStyle}>{t['schedules.col_last_run']}</th>
                <th style={thStyle}>{t['schedules.col_next_run']}</th>
                <th style={thStyle}>{t['schedules.col_status']}</th>
                <th style={thStyle}>{t['schedules.col_purpose']}</th>
                <th style={thStyle}>{t['schedules.col_actions']}</th>
              </tr>
            </thead>
            <tbody>
              {records.map(record => (
                <>
                  <tr
                    key={record.id}
                    style={{
                      cursor: 'pointer',
                      background: expandedId === record.id ? 'var(--bg-hover)' : undefined,
                      borderBottom: '1px solid var(--border)',
                    }}
                    onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                  >
                    <td style={tdStyle}>
                      <code style={{ fontSize: 12 }}>{truncate(record.schedule, 24)}</code>
                    </td>
                    <td style={{ ...tdStyle, maxWidth: 240 }}>
                      <code style={{ fontSize: 12, wordBreak: 'break-all' }}>
                        {truncate(record.command, 60)}
                      </code>
                    </td>
                    <td style={tdStyle}>
                      <span style={{
                        fontSize: 11,
                        padding: '1px 6px',
                        borderRadius: 8,
                        background: 'var(--bg-secondary)',
                        color: 'var(--text-muted)',
                      }}>
                        {record.source}
                      </span>
                    </td>
                    <td style={tdStyle}>{record.project ?? <span style={{ color: 'var(--text-muted)' }}>—</span>}</td>
                    <td style={{ ...tdStyle, fontSize: 12 }}>{formatTs(record.last_run)}</td>
                    <td style={{ ...tdStyle, fontSize: 12 }}>{formatTs(record.next_run)}</td>
                    <td style={tdStyle}><StatusBadge status={record.status} /></td>
                    <td style={{ ...tdStyle, maxWidth: 200 }}>
                      {record.purpose
                        ? <span title={record.purpose}>{truncate(record.purpose, 40)}</span>
                        : <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
                      }
                    </td>
                    <td style={tdStyle} onClick={e => e.stopPropagation()}>
                      {record.source === 'deferred' && record.annotations?.deferred_id ? (
                        <button
                          className="btn btn-secondary btn-xs"
                          disabled={cancellingId === record.id}
                          onClick={() => handleCancel(record)}
                          title={t['schedules.deferred_cancel_title']}
                          style={{ color: '#ef4444', borderColor: '#ef444433' }}
                        >
                          {cancellingId === record.id
                            ? t['schedules.deferred_cancelling']
                            : t['schedules.deferred_cancel']}
                        </button>
                      ) : !record.purpose && (
                        <button
                          className="btn btn-secondary btn-xs"
                          disabled={investigatingId === record.id}
                          onClick={() => handleInvestigate(record)}
                          title={t['schedules.investigate_title']}
                        >
                          {investigatingId === record.id
                            ? t['schedules.investigating']
                            : t['schedules.investigate']}
                        </button>
                      )}
                    </td>
                  </tr>
                  {expandedId === record.id && (
                    <tr key={`${record.id}-expanded`} style={{ background: 'var(--bg-secondary)' }}>
                      <td colSpan={9} style={{ padding: '12px 16px' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: '6px 12px', fontSize: 13 }}>
                          <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>ID</span>
                          <code style={{ fontSize: 12 }}>{record.id}</code>
                          <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>{t['schedules.col_schedule']}</span>
                          <code style={{ fontSize: 12 }}>{record.schedule}</code>
                          <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>{t['schedules.col_command']}</span>
                          <code style={{ fontSize: 12, wordBreak: 'break-all' }}>{record.command}</code>
                          <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>{t['schedules.col_purpose']}</span>
                          <span>{record.purpose ?? '—'}</span>
                          {Object.keys(record.annotations).length > 0 && (
                            <>
                              <span style={{ fontWeight: 600, color: 'var(--text-muted)' }}>Annotations</span>
                              <pre style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap' }}>
                                {JSON.stringify(record.annotations, null, 2)}
                              </pre>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Source status footer */}
      {data?.source_statuses && data.source_statuses.length > 0 && (
        <div style={{
          padding: '6px 16px',
          borderTop: '1px solid var(--border)',
          display: 'flex',
          gap: 8,
          flexWrap: 'wrap',
          flexShrink: 0,
          fontSize: 12,
          color: 'var(--text-muted)',
        }}>
          {data.source_statuses.map(ss => (
            <span
              key={ss.source}
              title={ss.error || undefined}
              style={{ color: ss.status === 'ok' ? 'var(--text-muted)' : '#f59e0b' }}
            >
              {ss.source}: {ss.status}{ss.count !== undefined ? ` (${ss.count})` : ''}
            </span>
          ))}
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed',
          bottom: 24,
          right: 24,
          padding: '10px 18px',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: 8,
          boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
          fontSize: 13,
          zIndex: 9999,
        }}>
          {toast}
        </div>
      )}
    </div>
  )
}

const thStyle: React.CSSProperties = {
  padding: '8px 12px',
  textAlign: 'left',
  fontWeight: 600,
  fontSize: 12,
  color: 'var(--text-muted)',
  borderBottom: '1px solid var(--border)',
  whiteSpace: 'nowrap',
}

const tdStyle: React.CSSProperties = {
  padding: '6px 12px',
  verticalAlign: 'middle',
}
