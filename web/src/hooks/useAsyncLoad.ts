import { useEffect, useReducer, useCallback } from 'react'

interface AsyncState<T> {
  data: T | null
  loading: boolean
  error: string
}

type AsyncAction<T> =
  | { type: 'start' }
  | { type: 'success'; payload: T }
  | { type: 'error'; message: string }
  | { type: 'reset' }

function asyncReducer<T>(_state: AsyncState<T>, action: AsyncAction<T>): AsyncState<T> {
  switch (action.type) {
    case 'start':   return { data: null, loading: true, error: '' }
    case 'success': return { data: action.payload, loading: false, error: '' }
    case 'error':   return { data: null, loading: false, error: action.message }
    case 'reset':   return { data: null, loading: false, error: '' }
  }
}

/**
 * Generic async loader: tracks data / loading / error for a single fetch.
 * Returns [state, reload] where reload() re-runs the fetch.
 *
 * @param fetchFn — factory called on every reload; return null to skip.
 * @param deps — deps array that triggers a reload (same semantics as useEffect).
 */
export function useAsyncLoad<T>(
  fetchFn: () => Promise<T> | null,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  deps: any[],
): [AsyncState<T>, () => void] {
  const [state, dispatch] = useReducer(
    asyncReducer as (s: AsyncState<T>, a: AsyncAction<T>) => AsyncState<T>,
    { data: null, loading: false, error: '' },
  )

  // Stable reload function — dispatches start + runs the fetch
  // We intentionally ignore deps change inside this callback; the
  // useEffect below handles dep-triggered reloads.
  const reload = useCallback(() => {
    const p = fetchFn()
    if (!p) return
    dispatch({ type: 'start' })
    p.then(d => dispatch({ type: 'success', payload: d }))
     .catch(e => dispatch({ type: 'error', message: e instanceof Error ? e.message : String(e) }))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    reload()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload])

  return [state, reload]
}
