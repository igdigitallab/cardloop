/**
 * Vertical drag handle between two side-by-side panes, sized in px.
 *
 * Mouse and touch both work (pointer events, `touch-action: none` in CSS); keyboard: arrows
 * resize, Home/End jump to the limits; double-click resets. Document-level listeners like the
 * project divider, so the drag survives the pointer leaving the thin handle.
 */
import { useCallback, useRef } from 'react'

interface Props {
  width: number
  min: number
  /** The pane may not grow past container width minus this (the other pane keeps its room). */
  minRest: number
  containerRef: React.RefObject<HTMLElement | null>
  onChange: (w: number) => void
  onReset: () => void
  label?: string
}

export function SplitHandle({ width, min, minRest, containerRef, onChange, onReset, label = 'Resize explorer' }: Props) {
  const drag = useRef<{ x: number; w: number } | null>(null)
  const handleRef = useRef<HTMLDivElement>(null)

  const limit = useCallback(() => {
    const total = containerRef.current?.clientWidth ?? 0
    // The handle sits between the panes, so it eats part of the room the other pane needs.
    const handle = handleRef.current?.offsetWidth ?? 0
    return Math.max(min, total - minRest - handle)
  }, [containerRef, min, minRest])

  const clamp = useCallback((w: number) => Math.round(Math.max(min, Math.min(limit(), w))), [limit, min])

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    e.preventDefault()
    drag.current = { x: e.clientX, w: width }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const move = (ev: PointerEvent) => {
      if (drag.current) onChange(clamp(drag.current.w + ev.clientX - drag.current.x))
    }
    const up = () => {
      drag.current = null
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      document.removeEventListener('pointermove', move)
      document.removeEventListener('pointerup', up)
      document.removeEventListener('pointercancel', up)
    }
    document.addEventListener('pointermove', move)
    document.addEventListener('pointerup', up)
    document.addEventListener('pointercancel', up)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 64 : 16
    if (e.key === 'ArrowLeft') { e.preventDefault(); onChange(clamp(width - step)) }
    else if (e.key === 'ArrowRight') { e.preventDefault(); onChange(clamp(width + step)) }
    else if (e.key === 'Home') { e.preventDefault(); onChange(min) }
    else if (e.key === 'End') { e.preventDefault(); onChange(limit()) }
    else if (e.key === 'Enter') { e.preventDefault(); onReset() }
  }

  return (
    <div
      ref={handleRef}
      className="files-split-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={min}
      tabIndex={0}
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
    />
  )
}
