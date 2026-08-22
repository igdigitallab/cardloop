/**
 * "Would a reload cost the operator something right now?" — read by useBuildWatch before it
 * self-updates onto a new build.
 *
 * A module flag rather than lifted state on purpose: the answer lives inside ChatTab (a
 * streaming turn, a draft in the composer), the asker sits in App, and threading a callback
 * through every tab in between would touch a dozen files for one boolean. The flag is
 * write-one-place / read-one-place, so it cannot drift the way a duplicated prop would.
 */

let busy = false

/** ChatTab keeps this in sync with its own render state. */
export function setAppBusy(next: boolean): void {
  busy = next
}

/** True while a turn is streaming here or the composer holds unsent text. */
export function isAppBusy(): boolean {
  return busy
}
