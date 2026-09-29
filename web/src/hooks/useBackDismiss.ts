import { useEffect, useRef } from 'react'
import { backStack } from '../lib/backStack'

/**
 * Make the Android back gesture (and the browser's Back) dismiss ONE layer of UI instead of
 * leaving the app.
 *
 * The mechanism is a history entry per open layer (lib/backStack.ts): opening pushes one, Back
 * pops it and the TOP layer closes, and closing from the UI pops our own entry so the stack
 * never grows. Nesting works: a lightbox opened over a modal owns a second entry, and two
 * Backs unwind them in order. Because it rides the WebView's own history, the wrapper needs
 * no native bridge for it and the browser PWA behaves identically.
 *
 * `onDismiss` is held in a ref on purpose: the effect must key off `active` alone. Keying off
 * the callback (as the first copy of this logic in Lightbox did) re-runs it whenever the
 * parent re-renders with a fresh arrow function, which pushes a new entry every render and
 * silently turns Back into "press it five times".
 */
export function useBackDismiss(active: boolean, onDismiss: () => void): void {
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss

  useEffect(() => {
    if (!active) return
    return backStack().push(() => dismissRef.current())
  }, [active])
}
