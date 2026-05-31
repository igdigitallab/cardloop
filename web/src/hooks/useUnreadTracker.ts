/**
 * Tracks unread message counts per session key.
 * Persists to localStorage.
 */
import { useCallback, useState } from 'react'
import { readLS, writeLS } from '../lib/storage'

const LS_UNREAD = 'cops.unreadBySession'

function parseUnread(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'number' && v > 0) out[k] = v
  }
  return out
}

export function useUnreadTracker() {
  const [unreadBySession, setUnreadBySession] = useState<Record<string, number>>(() =>
    parseUnread(readLS<unknown>(LS_UNREAD, {}))
  )

  const incrementUnread = useCallback((sessionKey: string) => {
    setUnreadBySession(prev => {
      const next = { ...prev, [sessionKey]: (prev[sessionKey] || 0) + 1 }
      writeLS(LS_UNREAD, next)
      return next
    })
  }, [])

  const clearUnreadForSession = useCallback((sessionKey: string) => {
    setUnreadBySession(prev => {
      if (!prev[sessionKey]) return prev
      const next = { ...prev }
      delete next[sessionKey]
      writeLS(LS_UNREAD, next)
      return next
    })
  }, [])

  const resetUnread = useCallback(() => {
    setUnreadBySession({})
    writeLS(LS_UNREAD, {})
  }, [])

  return { unreadBySession, incrementUnread, clearUnreadForSession, resetUnread }
}
