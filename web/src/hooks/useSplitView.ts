/**
 * Manages split-view state: splitPairs and splitWidth.
 * Persists to localStorage.
 */
import { useCallback, useEffect, useState } from 'react'
import { readLS, writeLS, readLSNumber } from '../lib/storage'

const LS_SPLIT_PAIRS = 'cops.splitPairs'
const LS_SPLIT_WIDTH = 'cops.splitWidth'

export function useSplitView(validIds: Set<string>) {
  const [splitPairs, setSplitPairs] = useState<Record<string, string>>(() => {
    const obj = readLS<Record<string, string>>(LS_SPLIT_PAIRS, {})
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {}
  })
  const [splitWidth, setSplitWidth] = useState<number>(() => {
    const n = readLSNumber(LS_SPLIT_WIDTH, 50)
    return Math.max(20, Math.min(80, n))
  })

  // Persist
  useEffect(() => { writeLS(LS_SPLIT_PAIRS, splitPairs) }, [splitPairs])
  useEffect(() => {
    try { localStorage.setItem(LS_SPLIT_WIDTH, String(splitWidth)) } catch {}
  }, [splitWidth])

  // Prune dead IDs
  useEffect(() => {
    if (!validIds.size) return
    setSplitPairs(prev => {
      const next: Record<string, string> = {}
      let changed = false
      for (const [k, v] of Object.entries(prev)) {
        if (validIds.has(k) && validIds.has(v)) next[k] = v
        else changed = true
      }
      return changed ? next : prev
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [validIds.size])

  const removePair = useCallback((id: string) => {
    setSplitPairs(prev => { const { [id]: _, ...rest } = prev; return rest })
  }, [])

  const setPair = useCallback((leftId: string, rightId: string) => {
    setSplitPairs(prev => ({ ...prev, [leftId]: rightId }))
  }, [])

  const onDividerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const parent = (e.currentTarget as HTMLElement).parentElement
    if (!parent) return
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    function onMove(ev: MouseEvent) {
      const rect = parent!.getBoundingClientRect()
      const pct = ((ev.clientX - rect.left) / rect.width) * 100
      setSplitWidth(Math.max(20, Math.min(80, pct)))
    }
    function onUp() {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  return { splitPairs, setSplitPairs, splitWidth, removePair, setPair, onDividerMouseDown }
}
