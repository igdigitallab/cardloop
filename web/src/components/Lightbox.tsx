import { useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from 'react'
import { createPortal } from 'react-dom'

// Full-screen viewer with zoom + pan, shared by chat images/video and mermaid
// diagrams (ops:6605db). Zoom is applied to the content via CSS transform, so it
// works the same on mobile (pinch) and desktop (wheel/buttons) WITHOUT touching
// the page viewport — fixes "everything zooms together" on phones.

interface Props {
  /** Image/video source. Mutually exclusive with `svg`. */
  src?: string
  alt?: string
  /** Raw SVG markup (mermaid diagram). Mutually exclusive with `src`. */
  svg?: string
  /** Render `src` as a <video> instead of <img>. */
  video?: boolean
  onClose: () => void
}

const MIN_SCALE = 1
const MAX_SCALE = 8

const clamp = (v: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, v))

export function Lightbox({ src, alt = '', svg, video, onClose }: Props) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handleKey)

    // Hijack the device/browser Back button: opening pushes a history entry so
    // Back closes the viewer instead of leaving the app. If closed via UI we
    // pop that entry ourselves on cleanup.
    let closedByBack = false
    window.history.pushState({ copsLightbox: true }, '')
    const onPop = () => { closedByBack = true; onClose() }
    window.addEventListener('popstate', onPop)

    return () => {
      window.removeEventListener('keydown', handleKey)
      window.removeEventListener('popstate', onPop)
      if (!closedByBack) window.history.back()
    }
  }, [onClose])

  const [scale, setScale] = useState(1)
  const [tx, setTx] = useState(0)
  const [ty, setTy] = useState(0)

  // Live pointer tracking for pan (1 finger) and pinch (2 fingers).
  const pointers = useRef<Map<number, { x: number; y: number }>>(new Map())
  const pinchStart = useRef<{ dist: number; scale: number } | null>(null)
  const panLast = useRef<{ x: number; y: number } | null>(null)

  const zoomable = video === undefined || video === false
  const reset = () => { setScale(1); setTx(0); setTy(0) }
  const zoomBy = (factor: number) => setScale(s => {
    const next = clamp(s * factor)
    if (next === 1) { setTx(0); setTy(0) }
    return next
  })

  function pointerDown(e: ReactPointerEvent) {
    ;(e.currentTarget as Element).setPointerCapture?.(e.pointerId)
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointers.current.size === 1) {
      panLast.current = { x: e.clientX, y: e.clientY }
    } else if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]
      pinchStart.current = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale }
    }
  }

  function pointerMove(e: ReactPointerEvent) {
    if (!pointers.current.has(e.pointerId)) return
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY })

    if (pointers.current.size === 2 && pinchStart.current) {
      const [a, b] = [...pointers.current.values()]
      const dist = Math.hypot(a.x - b.x, a.y - b.y)
      setScale(clamp(pinchStart.current.scale * (dist / pinchStart.current.dist)))
    } else if (pointers.current.size === 1) {
      if (scale > 1 && panLast.current) {
        const dx = e.clientX - panLast.current.x
        const dy = e.clientY - panLast.current.y
        setTx(v => v + dx)
        setTy(v => v + dy)
      }
      panLast.current = { x: e.clientX, y: e.clientY }
    }
  }

  function pointerUp(e: ReactPointerEvent) {
    pointers.current.delete(e.pointerId)
    if (pointers.current.size < 2) pinchStart.current = null
    panLast.current = pointers.current.size === 1
      ? [...pointers.current.values()][0]
      : null
  }

  function wheel(e: ReactWheelEvent) {
    e.preventDefault()
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15)
  }

  const transform = { transform: `translate(${tx}px, ${ty}px) scale(${scale})` }

  let content
  if (video) {
    // Video keeps native controls; no transform/zoom.
    content = (
      <video
        className="lightbox-video"
        src={src}
        controls
        autoPlay
        onClick={e => e.stopPropagation()}
      />
    )
  } else if (svg) {
    content = (
      <div
        className="lightbox-zoomable lightbox-svg"
        style={transform}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    )
  } else {
    content = (
      <img className="lightbox-zoomable" style={transform} src={src} alt={alt} draggable={false} />
    )
  }

  return createPortal(
    <div className="lightbox-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <button className="lightbox-close" onClick={onClose} aria-label="Close">✕</button>
      {zoomable && (
        <div className="lightbox-zoom-ctrls" onClick={e => e.stopPropagation()}>
          <button onClick={() => zoomBy(1 / 1.3)} aria-label="Zoom out">−</button>
          <button onClick={reset} aria-label="Reset zoom">⟲</button>
          <button onClick={() => zoomBy(1.3)} aria-label="Zoom in">+</button>
        </div>
      )}
      <div
        className={'lightbox-stage' + (scale > 1 ? ' is-zoomed' : '')}
        onClick={e => e.stopPropagation()}
        onPointerDown={zoomable ? pointerDown : undefined}
        onPointerMove={zoomable ? pointerMove : undefined}
        onPointerUp={zoomable ? pointerUp : undefined}
        onPointerCancel={zoomable ? pointerUp : undefined}
        onWheel={zoomable ? wheel : undefined}
        onDoubleClick={zoomable ? () => (scale > 1 ? reset() : zoomBy(2)) : undefined}
      >
        {content}
      </div>
    </div>,
    document.body,
  )
}
