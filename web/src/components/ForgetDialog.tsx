/**
 * spec-098: "Delete session forever" dialog.
 * Opens with a dry run, shows exactly what goes, executes with the plan_token.
 * Esc / outside click closes (Modal). Enter never confirms: focus starts on Cancel and the
 * destructive button swallows Enter, so only a deliberate click or Space-on-focus deletes.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { providerLabel } from '../lib/providers'
import {
  ForgetPlan, ForgetTurnPlan, ForgetTurnResult, alsoDeletePayload, diaryLine, forgetErrorView, lineageKey, summaryLine,
} from '../lib/forget'
import { Modal, ModalHead } from './Modal'

interface Props {
  projectId: string
  provider: string
  sessionId: string
  /** spec-098 P4: "Forget from here" mode - one chat's turns from an anchor user row on, instead of
   *  the whole session. Absent = the delete-session dialog, unchanged. */
  turn?: { chatId: string; anchorUuid: string }
  /** Turn mode: called after a successful forget (the caller closes the dialog). */
  onForgotten?: (res: ForgetTurnResult) => void
  /** Display name when the plan has no title yet (the list row's label / preview). */
  fallbackTitle?: string
  onClose: () => void
  /** Called after a successful delete (dialog is closed by the caller's handler). */
  onDeleted: (info: { alsoDeleted: number }) => void
}

type Phase = 'planning' | 'ready' | 'deleting'

export function ForgetDialog({ projectId, provider, sessionId, turn, onForgotten, fallbackTitle, onClose, onDeleted }: Props) {
  const [phase, setPhase] = useState<Phase>('planning')
  const [plan, setPlan] = useState<ForgetPlan | ForgetTurnPlan | null>(null)
  const [token, setToken] = useState('')
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [error, setError] = useState<{ message: string; replan: boolean } | null>(null)

  const loadPlan = useCallback(async () => {
    setPhase('planning')
    setError(null)
    setPlan(null)
    try {
      const res = turn
        ? await api.forgetFromHere(projectId, turn.chatId, { anchor_uuid: turn.anchorUuid, dry_run: true })
        : await api.deleteSession(projectId, provider, sessionId, { dry_run: true })
      if (!('plan' in res)) throw new Error('unexpected response')
      setPlan(res.plan)
      setToken(res.plan_token)
      setChecked(new Set())
      setPhase('ready')
    } catch (e) {
      setError(errView(e))
      setPhase('ready')
    }
  }, [projectId, provider, sessionId, turn?.chatId, turn?.anchorUuid]) // eslint-disable-line react-hooks/exhaustive-deps -- `turn` is rebuilt per render; its two fields are the identity

  useEffect(() => { loadPlan() }, [loadPlan])

  async function confirm() {
    if (!plan || phase !== 'ready') return
    setPhase('deleting')
    setError(null)
    try {
      const also = alsoDeletePayload(plan.lineage, checked)
      if (turn) {
        const done = await api.forgetFromHere(projectId, turn.chatId, {
          anchor_uuid: turn.anchorUuid, dry_run: false, plan_token: token, ...(also.length ? { also_delete: also } : {}),
        })
        if (!('forgotten' in done)) throw new Error('unexpected response')
        onForgotten?.(done)
        return
      }
      const res = await api.deleteSession(projectId, provider, sessionId, {
        dry_run: false, plan_token: token, ...(also.length ? { also_delete: also } : {}),
      })
      onDeleted({ alsoDeleted: 'also_deleted' in res ? res.also_deleted.length : 0 })
    } catch (e) {
      setError(errView(e))
      setPhase('ready')
    }
  }

  function toggle(key: string) {
    setChecked(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }

  const busy = phase !== 'ready'
  const stale = !!error?.replan
  const title = (plan && 'title' in plan ? plan.title : '') || fallbackTitle || sessionId.slice(0, 8) + '…'
  const turnPlan = plan && 'restored_prompt' in plan ? plan : null
  const diary = plan ? diaryLine(plan.diary_days) : null

  return (
    <Modal onClose={busy && phase === 'deleting' ? () => { /* a purge in flight cannot be cancelled */ } : onClose}>
      <ModalHead title={turn ? 'Forget from here' : 'Delete session forever'} onClose={onClose} />
      <div className="run-modal-body forget-dialog">
        {phase === 'planning' && <div className="forget-loading">Checking what would be deleted…</div>}

        {plan && (
          <>
            <div className="forget-what">
              {!turnPlan && <div className="forget-title" title={title}>{title}</div>}
              <div className="forget-sub">{providerLabel(plan.provider)} · {summaryLine(plan)}</div>
              <div className="forget-note">
                {turnPlan
                  ? 'Removes this turn and every later turn of this chat — prompts, replies, tool calls — and everything the cockpit stored about them: timeline, audit, search index, uploads. It cannot be undone.'
                  : 'Removes the transcript and everything the cockpit stored about it: timeline, audit, search index, uploads. It cannot be undone.'}
              </div>
              {turnPlan?.whole_session && (
                <div className="forget-note forget-whole">
                  This is the first turn: the whole session is deleted; the chat starts fresh.
                </div>
              )}
            </div>

            {turnPlan && turnPlan.restored_prompt && (
              <div className="forget-sec">
                <div className="forget-sec-h">Goes back to the composer</div>
                <div className="forget-prompt">{turnPlan.restored_prompt}</div>
              </div>
            )}

            {plan.warnings.length > 0 && (
              <ul className="forget-list forget-warn">
                {plan.warnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            )}

            {plan.side_effects.length > 0 && (
              <div className="forget-sec">
                <div className="forget-sec-h">What the agent did stays done</div>
                <ul className="forget-list">
                  {plan.side_effects.map((s, i) => (
                    <li key={i}><span className="forget-kind">{s.kind.replace('_', ' ')}</span> {s.detail}</li>
                  ))}
                </ul>
              </div>
            )}

            {plan.lineage.length > 0 && (
              <div className="forget-sec">
                <div className="forget-sec-h">Summary carried into / from</div>
                {plan.lineage.map(l => {
                  const key = lineageKey(l)
                  return (
                    <label key={key} className="forget-lineage">
                      <input type="checkbox" checked={checked.has(key)} disabled={busy}
                        onChange={() => toggle(key)} />
                      <span>
                        Also delete: {l.title || l.session_id.slice(0, 8) + '…'}
                        <span className="forget-sub"> · {providerLabel(l.provider)} · {l.kind === 'rotate' ? 'reset' : 'runtime switch'}</span>
                      </span>
                    </label>
                  )
                })}
              </div>
            )}

            {diary && <div className="forget-diary">{diary}</div>}

            {plan.limits.length > 0 && (
              <div className="forget-sec">
                <div className="forget-sec-h">Limits — what deleting here does not reach</div>
                <ul className="forget-list forget-limits">
                  {plan.limits.map((l, i) => <li key={i}>{l}</li>)}
                </ul>
              </div>
            )}
          </>
        )}

        {error && (
          <div className="forget-error" role="alert">
            {error.message}
            {stale && (
              <button className="btn-secondary forget-replan" onClick={loadPlan} disabled={phase === 'deleting'}>
                Reopen plan
              </button>
            )}
          </div>
        )}

        <div className="forget-actions">
          <button className="btn-secondary" autoFocus onClick={onClose} disabled={phase === 'deleting'}>Cancel</button>
          <button
            className="btn-danger"
            disabled={!plan || busy || stale}
            onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
            onClick={confirm}
          >
            {phase === 'deleting' ? (turn ? 'Forgetting…' : 'Deleting…') : (turn ? 'Forget forever' : 'Delete forever')}
          </button>
        </div>
      </div>
    </Modal>
  )
}

function errView(e: unknown) {
  const x = e as { status?: number; body?: { error?: unknown; code?: unknown } | null; message?: string }
  const text = typeof x?.body?.error === 'string' ? x.body.error : undefined
  const code = typeof x?.body?.code === 'string' ? x.body.code : undefined
  return forgetErrorView(x?.status, code, text ?? (x?.status ? undefined : x?.message))
}
