// Scrolling a chat back past the hydrated window (GET session-history: `idx`, `before`, `since`,
// `prev_session`).
//
// The feed shows the last 100 messages of a session; pages above them are fetched by message
// position and kept APART from the chat's `messages`, because every hydrate (mount, tab switch,
// reconnect, end of a turn) replaces `messages` wholesale. Once pages are loaded, a hydrate asks
// for `since` = the message right after them, so the window grows instead of sliding away and
// leaving a gap between the pages and the live tail.
//
// Past a session's first message the feed carries on into the session it continued from after a
// context reset: a `session_break` row marks the reset, the previous session's pages go above it,
// and so on back. Every scroll-back row carries the session it came from (`sid`); the rows after
// the last break belong to the window's own session.
import type { ChatMessage, PrevSession } from '../types'

export const OLDER_PAGE = 100

/** Start loading the next page when the feed is scrolled this close to its top (px). */
export const OLDER_TRIGGER_PX = 600

/** What to fetch next above everything loaded. */
export type OlderStep =
  | { kind: 'page'; sid: string; before: number }
  | { kind: 'prev'; from: string | null; prev: PrevSession }
  | null

/** `prev_session` as far as it is known: undefined = not asked yet, null = nothing above. */
export type PrevOf = (sid: string | null) => PrevSession | null | undefined

function lastBreak(older: ChatMessage[]): number {
  for (let i = older.length - 1; i >= 0; i--) if (older[i].role === 'session_break') return i
  return -1
}

function crossInto(from: string | null, older: ChatMessage[], prevOf: PrevOf): OlderStep {
  const prev = prevOf(from)
  if (!prev) return null
  // A link back into a session already on screen would loop forever.
  if (older.some(m => m.sid === prev.session_id || m.sessionBreak?.prev.session_id === prev.session_id)) return null
  return { kind: 'prev', from, prev }
}

/**
 * The next thing to fetch above everything shown: a page of the topmost session, or — at a
 * session's first message — the session before it. `windowStart` is the first position of the
 * hydrated window: 0 when that window is empty, null when its positions are unknown (Codex, Grok).
 */
export function nextOlder(older: ChatMessage[], windowSid: string | null, windowStart: number | null,
                          prevOf: PrevOf): OlderStep {
  const top = older[0]
  if (top) {
    if (top.role === 'session_break') {
      const prev = top.sessionBreak?.prev
      // An unreadable previous session ends the feed; an empty one is crossed straight away.
      return prev && prev.available ? crossInto(prev.session_id, older, prevOf) : null
    }
    if (top.idx == null || !top.sid) return null
    return top.idx > 0 ? { kind: 'page', sid: top.sid, before: top.idx } : crossInto(top.sid, older, prevOf)
  }
  if (windowStart == null) return null
  if (windowStart > 0) return windowSid ? { kind: 'page', sid: windowSid, before: windowStart } : null
  return crossInto(windowSid, older, prevOf)
}

/** `since` for a hydrate: the position right after the loaded pages of the window's own session,
 *  or its first message when only earlier sessions are loaded above it. */
export function sinceFor(older: ChatMessage[]): number | undefined {
  if (!older.length) return undefined
  const b = lastBreak(older)
  if (b === older.length - 1) return 0
  const last = older[older.length - 1].idx
  return last != null ? last + 1 : undefined
}

/**
 * The loaded rows that still sit directly above a freshly hydrated window that starts at
 * `windowStart`. Another session, a window without positions (Codex, Grok) or a gap (the server
 * fell back to the tail) drops them all; an overlap keeps only what is above the window. Earlier
 * sessions stay only while the window's own rows reach its first message.
 */
export function olderAfterHydrate(older: ChatMessage[], windowStart: number | null,
                                  sameSession: boolean): ChatMessage[] {
  if (!older.length) return older
  if (!sameSession || windowStart == null) return []
  const b = lastBreak(older)
  const own = older.slice(b + 1)
  if (!own.length) return windowStart === 0 ? older : []
  const last = own[own.length - 1].idx
  if (last == null) return []
  if (last + 1 === windowStart) return older
  if (last + 1 > windowStart) {
    const kept = own.filter(m => m.idx != null && m.idx < windowStart)
    return kept.length || windowStart === 0 ? [...older.slice(0, b + 1), ...kept] : []
  }
  return []
}

/**
 * Does a hydrate with no session before (`priorSid` null: the chat had just been reset) continue
 * the same feed? Yes when the new session names the same predecessor as the break the pages end
 * on — the first turn after a reset must not throw away the session shown above it.
 */
export function continuesAfterReset(older: ChatMessage[], priorSid: string | null,
                                    prev: PrevSession | null | undefined): boolean {
  if (priorSid != null || !prev || !older.length) return false
  const tail = older[older.length - 1]
  return tail.role === 'session_break' && tail.sessionBreak?.prev.session_id === prev.session_id
}

/** The row that marks a context reset between `prev` and the session after it. */
export function breakRow(from: string | null, prev: PrevSession): ChatMessage {
  return {
    id: `break-${prev.session_id}`, role: 'session_break', text: '', tools: [], streaming: false,
    sessionBreak: { prev, next: from },
    ...(prev.at != null ? { ts: Math.round(prev.at * 1000) } : {}),
  }
}

/** One line on what reset the context, e.g. "Auto-rotation at 752K tokens". */
export function breakReason(prev: PrevSession): string {
  if (prev.trigger === 'auto') {
    return prev.context_tokens
      ? `Auto-rotation at ${Math.round(prev.context_tokens / 1000)}K tokens`
      : 'Auto-rotation'
  }
  if (prev.trigger === 'manual') return 'Session reset by hand'
  if (prev.trigger === 'new') return 'New session started from the session menu'
  return 'Context reset'
}
