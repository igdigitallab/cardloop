/**
 * Один SSE-коннект на проект (provider в ProjectView), все табы подписываются через hooks.
 *
 * Зачем: каждый таб раньше открывал свой fetch+ReadableStream на /activity-stream.
 * 7 табов = 7 сокетов на каждый открытый проект. Через контекст — один на проект-вкладку.
 *
 * Использование:
 *   <ProjectActivityProvider projectId={p.id}>  ← в ProjectView
 *     ...
 *     useOnRunEnd(() => reload())               ← в каждом табе/секции
 */
import {
  createContext, useContext, useEffect, useRef, useCallback, ReactNode,
} from 'react'
import { ActivityEvent } from '../types'

type Handler = (evt: ActivityEvent) => void

interface BusValue {
  /** Подписаться на ВСЕ события шины. Возвращает unsubscribe. */
  subscribe: (h: Handler) => () => void
}

const BusContext = createContext<BusValue | null>(null)

interface ProviderProps {
  projectId: string
  children: ReactNode
}

export function ProjectActivityProvider({ projectId, children }: ProviderProps) {
  // Множество активных подписчиков (mutable ref — без перерендера на (un)subscribe)
  const handlersRef = useRef<Set<Handler>>(new Set())

  const subscribe = useCallback((h: Handler) => {
    handlersRef.current.add(h)
    return () => { handlersRef.current.delete(h) }
  }, [])

  // Один SSE-коннект на projectId. Переподключение при разрыве через 2с.
  useEffect(() => {
    const ac = new AbortController()
    let active = true

    async function connect() {
      while (active) {
        try {
          const res = await fetch(`/api/projects/${projectId}/activity-stream`, {
            credentials: 'include',
            signal: ac.signal,
          })
          if (!res.ok || !res.body) {
            await new Promise(r => setTimeout(r, 2000))
            continue
          }
          const reader = res.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''
          while (active) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            const lines = buf.split('\n')
            buf = lines.pop() ?? ''
            for (const ln of lines) {
              if (!ln.startsWith('data: ')) continue
              try {
                const evt = JSON.parse(ln.slice(6)) as ActivityEvent
                for (const h of handlersRef.current) {
                  try { h(evt) } catch { /* подписчик не должен валить шину */ }
                }
              } catch { /* skip битый JSON / heartbeat */ }
            }
          }
        } catch (err: unknown) {
          const name = (err as { name?: string })?.name
          if (!active || name === 'AbortError') break
          await new Promise(r => setTimeout(r, 2000))
        }
      }
    }
    connect()
    return () => { active = false; ac.abort() }
  }, [projectId])

  return (
    <BusContext.Provider value={{ subscribe }}>
      {children}
    </BusContext.Provider>
  )
}

/** Подписаться на ВСЕ события шины. handler нестабильным быть может (внутри используем ref). */
export function useProjectActivity(handler: Handler) {
  const ctx = useContext(BusContext)
  const handlerRef = useRef(handler)
  useEffect(() => { handlerRef.current = handler }, [handler])

  useEffect(() => {
    if (!ctx) return
    return ctx.subscribe(evt => handlerRef.current(evt))
  }, [ctx])
}

/** Удобный хук: вызывает callback на каждом run_end из шины. */
export function useOnRunEnd(callback: () => void) {
  useProjectActivity(evt => {
    if (evt.kind === 'run_end') callback()
  })
}

/** Хук: refresh при возврате фокуса/видимости + опционально через polling. */
export function useFocusRefresh(callback: () => void, pollMs?: number) {
  const cbRef = useRef(callback)
  useEffect(() => { cbRef.current = callback }, [callback])

  useEffect(() => {
    const onFocus = () => cbRef.current()
    const onVis = () => { if (document.visibilityState === 'visible') cbRef.current() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVis)
    let id: ReturnType<typeof setInterval> | null = null
    if (pollMs && pollMs > 0) {
      id = setInterval(() => {
        if (document.visibilityState === 'visible') cbRef.current()
      }, pollMs)
    }
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVis)
      if (id) clearInterval(id)
    }
  }, [pollMs])
}
