// Scrolling a chat back past the hydrated window (GET session-history: `idx`, `before`, `since`).
//
// The feed shows the last 100 messages of a session; pages above them are fetched by message
// position and kept APART from the chat's `messages`, because every hydrate (mount, tab switch,
// reconnect, end of a turn) replaces `messages` wholesale. Once pages are loaded, a hydrate asks
// for `since` = the message right after them, so the window grows instead of sliding away and
// leaving a gap between the pages and the live tail.
import type { ChatMessage } from '../types'

export const OLDER_PAGE = 100

/** Start loading the next page when the feed is scrolled this close to its top (px). */
export const OLDER_TRIGGER_PX = 600

/** `before` for the next page above everything loaded; null once the first message is loaded. */
export function olderCursor(older: ChatMessage[], windowStart: number | null): number | null {
  const first = older.length ? older[0].idx : windowStart
  return first != null && first > 0 ? first : null
}

/** `since` for a hydrate: the position right after the loaded pages, if there are any. */
export function sinceFor(older: ChatMessage[]): number | undefined {
  const last = older.length ? older[older.length - 1].idx : undefined
  return last != null ? last + 1 : undefined
}

/**
 * The loaded pages that still sit directly above a freshly hydrated window that starts at
 * `windowStart`. Another session, a window without positions (Codex, Grok) or a gap (the server
 * fell back to the tail) drops them all; an overlap keeps only what is above the window.
 */
export function olderAfterHydrate(older: ChatMessage[], windowStart: number | null,
                                  sameSession: boolean): ChatMessage[] {
  if (!older.length) return older
  if (!sameSession || windowStart == null) return []
  const last = older[older.length - 1].idx
  if (last == null) return []
  if (last + 1 === windowStart) return older
  if (last + 1 > windowStart) return older.filter(m => m.idx != null && m.idx < windowStart)
  return []
}
