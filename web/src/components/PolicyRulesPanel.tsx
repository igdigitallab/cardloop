import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../api'
import type { PolicyRule, ProjectRules } from '../types'
import { Spinner } from './Spinner'
import { useOnRunEnd, useFocusRefresh } from '../hooks/useProjectActivity'
import { t } from '../i18n'

// Policy rules (docs/RULES.md): a compact READ-ONLY list of the Markdown rule files this project
// runs under, with in-memory hit counts and per-file diagnostics. Rules are edited as files (Files
// tab) — there is deliberately no editor here. The one write is the per-project trust opt-in for
// rule files the repository tracks (a project setting, off by default).

interface Props {
  projectId: string
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function hitsLabel(n: number): string {
  return n === 1 ? t['rules.hits_one'] : t['rules.hits_other'].replace('{n}', String(n))
}

function lastHitTitle(ts: number | null): string | undefined {
  return ts ? `${t['rules.last_hit']}: ${new Date(ts * 1000).toLocaleString()}` : undefined
}

function scopeLabel(tier: PolicyRule['tier']): string {
  return tier === 'pack' ? t['rules.scope_pack'] : t[`agents.scope_${tier}` as const]
}

function RuleRow({ rule }: { rule: PolicyRule }) {
  const muted = rule.status !== 'active'
  return (
    <div className={`agents-role-row rules-row${muted ? ' agents-role-row--muted' : ''}`}>
      <div className="agents-role-main">
        <div className="agents-role-name-row">
          <span className="agents-role-name">{rule.name}</span>
          {rule.action && (
            <span className={`rules-action-badge rules-action-badge--${rule.action}`}>
              {t[`rules.action_${rule.action}` as const]}
            </span>
          )}
          <span className={`agents-role-scope-badge agents-role-scope-badge--${rule.tier === 'pack' ? 'builtin' : rule.tier}`}>
            {scopeLabel(rule.tier)}
          </span>
          {rule.event && <span className="agents-role-scope-badge">{rule.event}</span>}
          {rule.status !== 'active' && (
            <span className="agents-role-shadowed">{t[`rules.status_${rule.status}` as const]}</span>
          )}
          {rule.status === 'active' && (
            <span className="rules-hits" title={lastHitTitle(rule.last_hit)}>{hitsLabel(rule.hits)}</span>
          )}
        </div>
        {rule.match && <div className="rules-match" title={rule.match}>{rule.match}</div>}
        {rule.diagnostics.map((d, i) => (
          <div
            key={i}
            className={rule.status === 'invalid' ? 'agents-error-msg rules-diag' : 'agents-role-warning-msg rules-diag'}
          >⚠ {d}</div>
        ))}
      </div>
    </div>
  )
}

export function PolicyRulesPanel({ projectId }: Props) {
  const [data, setData] = useState<ProjectRules | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const reload = useCallback(() => {
    api.rules(projectId)
      .then(d => { setData(d); setLoadError(null) })
      .catch(e => setLoadError(errMsg(e)))
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true); setLoadError(null); setData(null)
    api.rules(projectId)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(e => { if (!cancelled) { setLoadError(errMsg(e)); setLoading(false) } })
    return () => { cancelled = true }
  }, [projectId])

  useOnRunEnd(reload)
  useFocusRefresh(reload)

  async function setTrust(next: boolean) {
    setSaving(true); setSaveError(null)
    setData(d => (d ? { ...d, trust_tracked: next } : d))      // optimistic; reload() is the truth
    try {
      await api.saveProjectSettings(projectId, { rules_trust_tracked: next })
    } catch (e) {
      setSaveError(`${t['rules.trust_failed']}: ${errMsg(e)}`)
    } finally {
      reload()
      setSaving(false)
    }
  }

  if (loading) return <Spinner label={t['rules.loading']} />

  if (loadError || !data) {
    return (
      <section className="agents-roles-block">
        <div className="agents-roles-head">
          <h3 className="agents-roles-title">{t['rules.title']}</h3>
        </div>
        <div className="no-content">{t['rules.unavailable']}</div>
      </section>
    )
  }

  const hasUntrusted = data.rules.some(r => r.status === 'untrusted')
  const showTrust = hasUntrusted || data.trust_tracked

  return (
    <section className="agents-roles-block rules-panel">
      <div className="agents-roles-head">
        <h3 className="agents-roles-title">{t['rules.title']}</h3>
        <button
          className="memory-action-btn"
          title={t['rules.refresh']}
          aria-label={t['rules.refresh']}
          onClick={reload}
        ><RefreshCw size={13} /></button>
      </div>
      <p className="agents-hint">{t['rules.hint']}</p>

      {data.diagnostics.length > 0 && (
        <div className="agents-errors">
          {data.diagnostics.map((d, i) => (
            <div key={i} className="agents-error-msg">⚠ {d}</div>
          ))}
        </div>
      )}

      {showTrust && (
        <label className="rules-trust">
          <input
            type="checkbox"
            checked={data.trust_tracked}
            disabled={saving}
            onChange={e => setTrust(e.target.checked)}
          />
          <span>
            {t['rules.trust_label']}
            <span className="agents-hint rules-trust-hint">{t['rules.trust_hint']}</span>
          </span>
        </label>
      )}
      {saveError && <div className="agents-error-msg">⚠ {saveError}</div>}

      {data.rules.length === 0 ? (
        <div className="no-content">
          {t['rules.empty']
            .replace('{dir}', data.project_dir || '.claude-ops/rules')
            .replace('{globalDir}', data.global_dir)}
        </div>
      ) : (
        <div className="agents-role-list">
          {data.rules.map(r => <RuleRow key={`${r.tier}:${r.path}`} rule={r} />)}
        </div>
      )}
    </section>
  )
}
