/**
 * Manages the set of open project tabs and the active tab.
 * Persists openIds and activeId to localStorage.
 */
import { useCallback, useState, useEffect } from 'react'
import { readLS, writeLS, readLSString, writeLSString } from '../lib/storage'

const LS_OPEN = 'cops.openProjects'
const LS_ACTIVE = 'cops.activeProject'

export function useTabManager(validIds: Set<string>, globalFilesId: string) {
  const [openIds, setOpenIds] = useState<string[]>(() => {
    const arr = readLS<string[]>(LS_OPEN, [])
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  })
  const [activeId, setActiveId] = useState<string | null>(() => readLSString(LS_ACTIVE))

  // Persist
  useEffect(() => { writeLS(LS_OPEN, openIds) }, [openIds])
  useEffect(() => {
    writeLSString(LS_ACTIVE, activeId)
  }, [activeId])

  // Prune dead IDs after project list changes
  useEffect(() => {
    if (!validIds.size) return
    setOpenIds(prev => {
      const next = prev.filter(id => validIds.has(id))
      return next.length === prev.length ? prev : next
    })
    setActiveId(prev =>
      prev === globalFilesId || (prev && validIds.has(prev)) ? prev : null
    )
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [validIds.size, globalFilesId])

  const openTab = useCallback((id: string) => {
    setOpenIds(prev => prev.includes(id) ? prev : [...prev, id])
    setActiveId(id)
  }, [])

  const activateTab = useCallback((id: string) => {
    setActiveId(id)
  }, [])

  const closeTab = useCallback((id: string, onActivate: (newId: string | null) => void) => {
    setOpenIds(prev => {
      const idx = prev.indexOf(id)
      if (idx === -1) return prev
      const next = prev.filter(x => x !== id)
      setActiveId(curActive => {
        if (curActive !== id) return curActive
        if (next.length === 0) { onActivate(null); return null }
        const newId = next[Math.min(idx, next.length - 1)]
        onActivate(newId)
        return newId
      })
      return next
    })
  }, [])

  const renameTabId = useCallback((oldId: string, newId: string) => {
    setOpenIds(prev => prev.map(id => id === oldId ? newId : id))
    setActiveId(prev => prev === oldId ? newId : prev)
  }, [])

  return { openIds, setOpenIds, activeId, setActiveId, openTab, activateTab, closeTab, renameTabId }
}
