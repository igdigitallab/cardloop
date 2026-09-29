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
 *  3. history.back() is asynchronous and traverses relative to whatever entry is current WHEN
 *     it runs. A layer opened in the same tick as another one closes (project switch, a modal
 *     replacing a modal) pushed its entry first, and the pending back() then popped THAT one:
 *     the new layer ended up one entry short and the next Back left the app. Such a push now
 *     waits until the browser has paid every pop we owe it.
 * Tests: backStack.test.ts (run command in its header).
 */

interface Layer {
  dismiss: () => void
  poppedByBack: boolean
  /** Has this layer's history entry been pushed yet? (Deferred while a pop is in flight.) */
  pushed: boolean
}

export interface BackEnv {
  history: { pushState(state: unknown, title: string): void; back(): void }
  addEventListener(type: 'popstate', fn: () => void): void
}

export function createBackStack(env: BackEnv) {
  const stack: Layer[] = []
  let ownPops = 0
  let listening = false

  const pushEntry = (layer: Layer) => {
    env.history.pushState({ copsLayer: true }, '')
    layer.pushed = true
  }

  const onPop = () => {
    if (ownPops > 0) {
      ownPops--
      // Every pop we owed has landed: the layers that opened meanwhile can take their entry now.
      if (ownPops === 0) for (const l of stack) if (!l.pushed) pushEntry(l)
      return
    }
    const top = stack[stack.length - 1]
    if (!top) return
    top.poppedByBack = true
    top.dismiss()
  }

  return {
    /** Open a layer. Returns the function that closes it from the UI side. */
    push(dismiss: () => void): () => void {
      const layer: Layer = { dismiss, poppedByBack: false, pushed: false }
      stack.push(layer)
      if (ownPops === 0) pushEntry(layer)
      if (!listening) { env.addEventListener('popstate', onPop); listening = true }
      return () => {
        const i = stack.indexOf(layer)
        if (i >= 0) stack.splice(i, 1)
        if (!layer.pushed) return  // never got an entry, so there is nothing to give back
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
