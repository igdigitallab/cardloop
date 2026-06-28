/**
 * Spec-065 Phase A: module/extension registry hook + provider.
 *
 * Fetches GET /api/modules once after auth, exposes read + toggle helpers.
 * Provider is mounted in App.tsx so both GlobalSettingsTab and ProjectView can consume it.
 *
 * Default for isEnabled() while loading is TRUE for non-destructive rendering
 * (e.g. the github badge should not flicker-hide on every load).
 */
import {
  createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode,
} from 'react'
import { api } from '../api'
import { Module } from '../types'

interface ModulesValue {
  modules: Module[]
  loading: boolean
  isEnabled: (id: string) => boolean
  setEnabled: (id: string, on: boolean) => Promise<void>
  setConfig: (id: string, config: Record<string, unknown>) => Promise<Module>
  reload: () => Promise<void>
}

const ModulesContext = createContext<ModulesValue | null>(null)

export function ModulesProvider({ children }: { children: ReactNode }) {
  const [modules, setModules] = useState<Module[]>([])
  const [loading, setLoading] = useState(true)
  // Ref-map for fast isEnabled lookups without O(n) find on every render
  const mapRef = useRef<Map<string, boolean>>(new Map())

  const doLoad = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.listModules()
      setModules(res.modules)
      const m = new Map<string, boolean>()
      for (const mod of res.modules) m.set(mod.id, mod.enabled)
      mapRef.current = m
    } catch {
      // Network / auth failure — leave empty list; UI falls back gracefully
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void doLoad()
  }, [doLoad])

  const isEnabled = useCallback((id: string): boolean => {
    // Default TRUE while loading so non-destructive surfaces don't flicker-hide
    if (loading) return true
    const val = mapRef.current.get(id)
    // Unknown module id → default enabled (forward-compat)
    return val === undefined ? true : val
  }, [loading])

  const setEnabled = useCallback(async (id: string, on: boolean) => {
    // Optimistic local update
    setModules(prev => prev.map(m => m.id === id ? { ...m, enabled: on } : m))
    mapRef.current.set(id, on)
    try {
      const res = await api.setModule(id, on)
      // Reconcile with server answer
      setModules(prev => prev.map(m => m.id === id ? res.module : m))
      mapRef.current.set(id, res.module.enabled)
    } catch {
      // Roll back optimistic update on error
      setModules(prev => prev.map(m => m.id === id ? { ...m, enabled: !on } : m))
      mapRef.current.set(id, !on)
    }
  }, [])

  // spec-066: persist a module's config block and reconcile with the server answer.
  const setConfig = useCallback(async (id: string, config: Record<string, unknown>) => {
    const res = await api.setModuleConfig(id, config)
    setModules(prev => prev.map(m => m.id === id ? res.module : m))
    mapRef.current.set(id, res.module.enabled)
    return res.module
  }, [])

  const reload = useCallback(() => doLoad(), [doLoad])

  return (
    <ModulesContext.Provider value={{ modules, loading, isEnabled, setEnabled, setConfig, reload }}>
      {children}
    </ModulesContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- hook + provider co-located by design
export function useModules(): ModulesValue {
  const ctx = useContext(ModulesContext)
  if (!ctx) throw new Error('useModules must be used inside <ModulesProvider>')
  return ctx
}
