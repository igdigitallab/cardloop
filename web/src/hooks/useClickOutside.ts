import { RefObject, useEffect } from 'react'

/**
 * Fires `callback` when a mousedown event happens outside `ref`.
 * Only active when `enabled` is true (default: true).
 */
export function useClickOutside<T extends HTMLElement>(
  ref: RefObject<T>,
  callback: () => void,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled) return
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        callback()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [ref, callback, enabled])
}
