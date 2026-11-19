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
export function summaryLine(p: ForgetPlan): string {
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
