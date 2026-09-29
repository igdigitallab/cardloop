/**
 * The stack behind useBackDismiss: one browser-history entry per open UI layer (modal,
 * lightbox, drawer, project tab-back), so Back closes ONE layer instead of leaving the app.
 *
 * Two rules the per-hook version got wrong, both visible as "closing a dialog flipped the
 * project tab underneath it" (and threw away the unsaved draft that tab held):
 *  1. A layer that closes itself from the UI pops its own history entry with history.back().
 *     That fires `popstate` — which every OTHER layer used to hear as a real Back gesture.
 *     Such pops are now counted and swallowed.
 *  2. A real Back must close only the TOP layer. With one listener per layer, all of them
 *     fired at once.
 * Tests: backStack.test.ts (run command in its header).
 */

interface Layer {
  dismiss: () => void
  poppedByBack: boolean
}

export interface BackEnv {
  history: { pushState(state: unknown, title: string): void; back(): void }
  addEventListener(type: 'popstate', fn: () => void): void
}

export function createBackStack(env: BackEnv) {
  const stack: Layer[] = []
  let ownPops = 0
  let listening = false

  const onPop = () => {
    if (ownPops > 0) { ownPops--; return }
    const top = stack[stack.length - 1]
    if (!top) return
    top.poppedByBack = true
    top.dismiss()
  }

  return {
    /** Open a layer. Returns the function that closes it from the UI side. */
    push(dismiss: () => void): () => void {
      const layer: Layer = { dismiss, poppedByBack: false }
      stack.push(layer)
      env.history.pushState({ copsLayer: true }, '')
      if (!listening) { env.addEventListener('popstate', onPop); listening = true }
      return () => {
        const i = stack.indexOf(layer)
        if (i >= 0) stack.splice(i, 1)
        // Closed by Back: the browser already consumed its entry. Closed from the UI: consume
        // it ourselves, or the next Back would be spent on a layer that is already gone.
        if (!layer.poppedByBack) { ownPops++; env.history.back() }
        // The listener stays attached on purpose: history.back() is ASYNC, so the popstate of
        // the pop we just made arrives after this returns. Detaching now would leave `ownPops`
        // uncleared and the next REAL Back would be swallowed as if it were ours.
      }
    },
    /** Test hook. */
    depth: () => stack.length,
  }
}

let shared: ReturnType<typeof createBackStack> | null = null

/** The app-wide stack, bound to the real window on first use. */
export function backStack() {
  return (shared ??= createBackStack(window as unknown as BackEnv))
}
