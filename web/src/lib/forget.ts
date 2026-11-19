/** spec-098 (Forget): pure view-model helpers for the delete-session dialog and the bus events.
 *  No React, no fetch - unit-tested in forget.test.ts. Shapes mirror the server contract. */

export interface ForgetSideEffect { kind: string; detail: string }
export interface ForgetLineage { provider: string; session_id: string; kind: string; title?: string }
export interface ForgetPlan {
  provider: string
  session_id: string
  title: string
  turns: number
  first_ts: number | null
  last_ts: number | null
  files: number
  bytes: number
  active_in_chats: string[]
  side_effects: ForgetSideEffect[]
  lineage: ForgetLineage[]
  diary_days: string[]
  limits: string[]
  warnings: string[]
}
/** spec-098 P4: the plan of a "Forget from here" dry run (no title / active_in_chats). */
export interface ForgetTurnPlan {
  provider: string
  session_id: string
  chat_id: string
  whole_session: boolean
  turns: number
  first_ts: number | null
  last_ts: number | null
  restored_prompt: string
  files: number
  bytes: number
  side_effects: ForgetSideEffect[]
  lineage: ForgetLineage[]
  diary_days: string[]
  limits: string[]
  warnings: string[]
}
export interface ForgetTurnResult {
  ok: true
  forgotten: { turns: number; files: number; timeline_lines: number; audit_lines: number; search_rows: number; media: number }
  restored_prompt: string
  whole_session: boolean
  also_deleted: { provider: string; session_id: string }[]
  also_failed: unknown[]
  residue: string
}
export interface ForgetResidueHit { path: string; store?: string }

const pad = (n: number) => String(n).padStart(2, '0')

/** "2026-11-17" -> "17.11". Anything not shaped like a date is passed through. */
export function diaryDay(iso: string): string {
  const m = /^\d{4}-(\d{2})-(\d{2})/.exec(iso)
  return m ? `${m[2]}.${m[1]}` : iso
}

/** "Already in the diary: 17.11, 18.11" - null when there are no days. */
export function diaryLine(days: readonly string[] | null | undefined): string | null {
  if (!days || days.length === 0) return null
  return `Already in the diary: ${days.map(diaryDay).join(', ')}`
}

function fmtMoment(ts: number): string {
  const d = new Date(ts * 1000)
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Time span of the session: "17.11 14:03 - 15:10", or one moment, or '' when unknown. */
export function formatSpan(first: number | null | undefined, last: number | null | undefined): string {
  const a = typeof first === 'number' ? first : null
  const b = typeof last === 'number' ? last : null
  if (a == null && b == null) return ''
  if (a == null || b == null || Math.abs(a - b) < 60) return fmtMoment((a ?? b) as number)
  const fa = fmtMoment(a), fb = fmtMoment(b)
  // Same calendar day: print the date once.
  return fa.slice(0, 5) === fb.slice(0, 5) ? `${fa} - ${fb.slice(6)}` : `${fa} - ${fb}`
}

export function formatBytes(n: number): string {
  if (!(n > 0)) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function lineageKey(l: Pick<ForgetLineage, 'provider' | 'session_id'>): string {
  return `${l.provider}:${l.session_id}`
}

/** "What goes" summary line: "7 turns · 17.11 14:03 - 15:10 · 12 files, 814 KB". */
export function summaryLine(p: Pick<ForgetPlan, 'turns' | 'first_ts' | 'last_ts' | 'files' | 'bytes'>): string {
  const bits = [`${p.turns} ${p.turns === 1 ? 'turn' : 'turns'}`]
  const span = formatSpan(p.first_ts, p.last_ts)
  if (span) bits.push(span)
  if (p.files > 0) bits.push(`${p.files} ${p.files === 1 ? 'file' : 'files'}, ${formatBytes(p.bytes)}`)
  return bits.join(' · ')
}

/** The `also_delete` payload from the ticked lineage rows, in plan order, unknown keys ignored. */
export function alsoDeletePayload(
  lineage: readonly ForgetLineage[], checked: ReadonlySet<string>,
): { provider: string; session_id: string }[] {
  return lineage
    .filter(l => checked.has(lineageKey(l)))
    .map(l => ({ provider: l.provider, session_id: l.session_id }))
}

export const FORGET_CHANGED_MESSAGE =
  'The session changed since you opened this — reopen to see the current plan'

export interface ForgetErrorView {
  /** Text to show. */
  message: string
  /** True when the plan is stale and a re-plan (new dry run) is the right next step. */
  replan: boolean
}

/** Map a failed delete call to the text the dialog shows. 409 `changed` is rewritten (the
 *  server's wording is generic); the other refusals show the server's own text. */
export function forgetErrorView(status: number | undefined, code: string | undefined, serverText: string | undefined): ForgetErrorView {
  if (status === 409 && code === 'changed') return { message: FORGET_CHANGED_MESSAGE, replan: true }
  if (serverText && serverText.trim()) return { message: serverText.trim(), replan: false }
  switch (code) {
    case 'disabled': return { message: 'Deleting sessions is switched off on this server', replan: false }
    case 'not_found': return { message: 'This session no longer exists', replan: false }
    case 'no_capability': return { message: 'This engine cannot delete sessions', replan: false }
    case 'busy': return { message: 'The project is busy — try again when the run ends', replan: false }
    case 'failed': return { message: 'Delete failed — try again', replan: false }
  }
  return { message: status ? `Delete failed (HTTP ${status})` : 'Delete failed', replan: false }
}

/** Toast text for a `forget_residue` bus event. Paths only, at most 3 shown. */
export function residueMessage(clean: boolean, hits: readonly ForgetResidueHit[] | null | undefined, truncated?: boolean): string {
  const list = hits ?? []
  if (clean && list.length === 0) return 'Deleted — no copies left'
  // not clean and nothing found = the check could not run or finish (nothing distinctive to look
  // for, the time budget ran out, the index was busy): say that, never imply a clean result
  if (list.length === 0) return 'Deleted — the check for leftover copies could not finish'
  const shown = list.slice(0, 3).map(h => h.path)
  const more = list.length > 3 || truncated ? ', …' : ''
  return `Deleted — still found in: ${shown.join(', ')}${more}`
}

/** Did a `session_deleted` event remove the chat's own session? `ids` = the chat's continuity ids. */
export function deletedChatSession(
  evt: { session_id?: string; chat_ids?: string[] }, chatId: string | null | undefined,
  ids: readonly (string | null | undefined)[],
): boolean {
  if (chatId && Array.isArray(evt.chat_ids) && evt.chat_ids.includes(chatId)) return true
  return !!evt.session_id && ids.some(i => i === evt.session_id)
}

/** Minimal row shape the anchor rule needs (ChatMessage satisfies it). */
export interface ForgetFeedRow {
  role: string; uuid?: string; ts?: number; streaming?: boolean; runId?: string; bgRun?: boolean
}

/** What a "Forget from here" request names: a history row's transcript uuid, or the run of a row
 *  the feed rendered live (it has no uuid until the next history read). */
export type ForgetAnchor = { uuid: string } | { runId: string }

/** Slack between a turn mark's start and the transcript timestamp of its user row (ms). */
export const FORGET_MARK_SLACK_MS = 5000

/** "Forget from here" anchor for the assistant group starting at `groupIndex`, or null when the
 *  action must not be offered. Only for a settled group of the chat's CURRENT session (no
 *  session_break after it). A group rendered live names its own run (exact: the server cuts at
 *  that run's mark). Otherwise the nearest preceding user row: its uuid when it is not older than
 *  the session's earliest turn mark (`forgetFromMs`; null = the server has no marks), else its run
 *  if it was rendered live. */
export function forgetAnchorFor(
  feed: readonly ForgetFeedRow[], groupIndex: number, forgetFromMs: number | null | undefined,
): ForgetAnchor | null {
  const first = feed[groupIndex]
  if (!first || first.role !== 'assistant') return null
  // The whole assistant run must be settled: a streaming reply cannot be cut under its own run.
  for (let i = groupIndex; i < feed.length && feed[i].role === 'assistant'; i++) {
    if (feed[i].streaming) return null
  }
  // Current session only: no session_break anywhere after this group.
  for (let i = groupIndex + 1; i < feed.length; i++) if (feed[i].role === 'session_break') return null
  // An autonomous background turn starts inside the CLI, not at a prompt the cockpit sent, so it has
  // no turn mark: its reply cannot be cut out on its own (and anchoring on the turn before it would
  // take a turn the operator did not pick).
  if (first.bgRun) return null
  if (first.runId) return { runId: first.runId }
  for (let i = groupIndex - 1; i >= 0; i--) {
    const r = feed[i]
    if (r.role === 'session_break') return null
    if (r.role !== 'user') continue
    if (r.streaming) return null
    if (r.uuid && r.ts != null && forgetFromMs != null && Number.isFinite(forgetFromMs)
        && r.ts >= forgetFromMs - FORGET_MARK_SLACK_MS) return { uuid: r.uuid }
    if (!r.uuid && r.runId) return { runId: r.runId }
    return null
  }
  return null
}

/** The request body fields of an anchor. */
export function anchorBody(a: ForgetAnchor): { anchor_uuid: string } | { anchor_run_id: string } {
  return 'uuid' in a ? { anchor_uuid: a.uuid } : { anchor_run_id: a.runId }
}

/** Stable identity of an anchor (React dependency lists). */
export function anchorKey(a: ForgetAnchor): string {
  return 'uuid' in a ? `u:${a.uuid}` : `r:${a.runId}`
}

/** Composer text after a forget: the restored prompt, then a blank line and the draft if any. */
export function composerAfterForget(restored: string, draft: string): string {
  if (!restored) return draft
  return draft.trim() ? `${restored}\n\n${draft}` : restored
}

/** Toast text for an executed turn forget. */
export function forgottenToast(turns: number, wholeSession: boolean): string {
  if (wholeSession) return 'Forgot the whole session'
  return `Forgot ${turns} ${turns === 1 ? 'turn' : 'turns'}`
}

/** Does a `chat_forgotten` event concern this chat's session? */
export function chatForgottenAffectsMe(
  evt: { chat_id?: string; session_id?: string }, chatId: string | null | undefined,
  sessionId: string | null | undefined,
): boolean {
  if (chatId && evt.chat_id === chatId) return true
  return !!evt.session_id && evt.session_id === sessionId
}
