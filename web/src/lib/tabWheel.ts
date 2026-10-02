/** Mouse wheel over the open-tabs bar scrolls the strip sideways, like a browser's tab strip.
 *
 *  Pure on purpose (the DOM listener lives in ProjectTabBar): it decides HOW FAR a wheel event
 *  should move the strip, or returns null when the event is not ours to handle — most importantly
 *  Ctrl+wheel, which is the browser's zoom. */
export interface WheelLike {
  deltaX: number
  deltaY: number
  /** 0 = pixels, 1 = lines (Firefox with a notched mouse), 2 = pages. */
  deltaMode: number
  ctrlKey: boolean
}

const LINE_PX = 40

export function wheelScrollDelta(e: WheelLike, pageWidth: number): number | null {
  if (e.ctrlKey) return null
  // A trackpad swipe is mostly horizontal, a mouse wheel is vertical: follow the dominant axis.
  let d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY
  if (e.deltaMode === 1) d *= LINE_PX
  else if (e.deltaMode === 2) d *= pageWidth
  return d === 0 ? null : d
}
