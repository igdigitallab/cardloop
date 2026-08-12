/**
 * Spec-065 Phase B — live agent-driven browser pane.
 *
 * Connects to ws(s)://<host>/api/browser/ws?project=<projectId> (see webapp.py).
 * The server streams JPEG frames as binary WS messages (raw bytes, 1280×720 native).
 * Text messages carry JSON control events: ready / nav / error.
 * The client sends JSON text messages for mouse, keyboard, wheel, and navigate commands.
 *
 * Coordinates for all input events are mapped from the displayed <img> rect to the
 * 1280×720 frame coordinate space before sending.
 *
 * WebSocket lifecycle mirrors TerminalTab.tsx (open on mount, close on unmount,
 * show disconnected state on unexpected close — no automatic reconnect loops that
 * would spam the server; a single reconnect after close is offered via a button).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { t } from '../i18n'

// Native frame dimensions the server always streams at
const FRAME_W = 1280
const FRAME_H = 720

// Throttle mouse-move events to ~30 per second
const MOUSE_MOVE_INTERVAL_MS = 33

// Wheel/touch-scroll flush cadence. Measured on a real page (spec: browser pane
// perf note 2026-08-11): the CDP screencast sends a full JPEG frame every time,
// with no delta/video encoding — frame SIZE is ~constant (~65-70KB) regardless of
// how little changed, so what we control is frame COUNT. Flushing coalesced wheel
// deltas at 16ms (one per animation frame) vs 33ms, for the identical total scroll
// distance: 13 frames/662KB/489ms settle vs 7 frames/358KB/260ms settle — 33ms
// roughly halves both bandwidth and settle time with no perceptible smoothness
// loss (the operator is watching a re-encoded remote stream either way, not the
// local page — 30fps reads as smooth same as it does for any video call).
const WHEEL_FLUSH_MS = 33

// Touch: movement (in client px) beyond this turns a tap into a scroll gesture.
const TAP_SLOP = 8
// Chromium fires compatibility mouse events shortly AFTER touchend; the mouse
// handlers ignore anything within this window of a touch so they don't steal
// keyboard focus from the hidden input or double the click.
const TOUCH_GUARD_MS = 700
// Hidden-input padding for the mobile soft keyboard. The capture input always
// holds this 1-char pad so a Backspace ALWAYS has something to delete and thus
// reliably fires an `input` event (empty inputs swallow Backspace on Android).
const KBD_PAD = ' '

interface Props {
  projectId: string
}

const keyRowBtn: React.CSSProperties = {
  flexShrink: 0,
  minWidth: 34,
  fontSize: 12,
  lineHeight: 1,
  padding: '6px 8px',
  borderRadius: 5,
  border: '1px solid var(--border, #2a2a2a)',
  background: 'var(--bg, #0d0d0d)',
  color: 'var(--text, #d4d4d4)',
  cursor: 'pointer',
  userSelect: 'none',
}

type ConnState = 'connecting' | 'ready' | 'disconnected' | 'error'

interface BrowserTabInfo { id: string; title: string; url: string; active: boolean }

/**
 * Clamp a value to [min, max].
 */
function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v))
}

/**
 * Map a point from the displayed <img> element's client rect to the 1280×720
 * frame coordinate space.
 */
function toFrameCoords(
  clientX: number,
  clientY: number,
  rect: DOMRect,
): { x: number; y: number } {
  const x = Math.round(clamp(((clientX - rect.left) / rect.width) * FRAME_W, 0, FRAME_W))
  const y = Math.round(clamp(((clientY - rect.top) / rect.height) * FRAME_H, 0, FRAME_H))
  return { x, y }
}

function buttonName(button: number): 'left' | 'right' | 'middle' {
  if (button === 1) return 'middle'
  if (button === 2) return 'right'
  return 'left'
}

/**
 * CDP modifier bitmask (alt=1, ctrl=2, meta=4, shift=8) — mirrored by
 * browser_pane._key. Without it the remote page never sees Ctrl+A, Shift-select,
 * or a capital typed with Shift held.
 */
function modsOf(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0)
}

// Keys whose browser default would move/scroll the COCKPIT instead of reaching the pane.
const SWALLOW_KEYS = new Set([
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Tab', 'Backspace', 'Delete',
  'Home', 'End', 'PageUp', 'PageDown', ' ', 'Enter',
])

// The special-key row shown on touch devices: an on-screen keyboard has no Esc,
// Tab or arrows, and long-pressing for them is not a thing.
const KEY_ROW: { label: string; key: string; title: string }[] = [
  { label: 'Esc', key: 'Escape', title: 'Escape' },
  { label: '⇥', key: 'Tab', title: 'Tab' },
  { label: '⌫', key: 'Backspace', title: 'Backspace' },
  { label: 'Del', key: 'Delete', title: 'Delete' },
  { label: '←', key: 'ArrowLeft', title: 'Left' },
  { label: '→', key: 'ArrowRight', title: 'Right' },
  { label: '↑', key: 'ArrowUp', title: 'Up' },
  { label: '↓', key: 'ArrowDown', title: 'Down' },
  { label: '⏎', key: 'Enter', title: 'Enter' },
]

/**
 * What to show in the URL bar. The branded start page is a long
 * `data:text/html;base64,…` URL (and a reset session sits on `about:blank`) —
 * showing either is confusing noise, so render an empty bar (placeholder) instead.
 */
function displayUrl(url: string): string {
  if (!url || url === 'about:blank' || url.startsWith('data:') || url.startsWith('about:')) return ''
  return url
}

export function BrowserTab({ projectId }: Props) {
  const wsRef = useRef<WebSocket | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const lastObjUrlRef = useRef<string | null>(null)
  const lastMouseMoveRef = useRef<number>(0)
  // Coalesced wheel/touch-scroll deltas — see accumulateWheel below.
  const wheelAccumRef = useRef<{ x: number; y: number; dx: number; dy: number } | null>(null)
  const wheelFlushScheduledRef = useRef<boolean>(false)
  // Mobile co-control: hidden input that captures the soft keyboard, plus
  // touch-gesture state (tap vs scroll).
  const hiddenInputRef = useRef<HTMLInputElement | null>(null)
  const touchStartRef = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null)
  const lastTouchRef = useRef<{ cx: number; cy: number } | null>(null)
  const touchMovedRef = useRef<boolean>(false)
  // Timestamp of the last touch. Chromium synthesizes compatibility mouse events
  // (mousedown/up/click) shortly AFTER touchend — they would steal focus back to
  // the container (killing the soft keyboard) and double the click. Mouse handlers
  // ignore anything within this window of a touch.
  const lastTouchTimeRef = useRef<number>(0)

  const [connState, setConnState] = useState<ConnState>('connecting')
  const [errorMsg, setErrorMsg] = useState<string>('')
  const [frameSrc, setFrameSrc] = useState<string>('')
  // Current page URL/title received from the server
  const [urlValue, setUrlValue] = useState<string>('')
  const [urlInput, setUrlInput] = useState<string>('')
  // spec-066: which backend acquired the live session (builtin / cloakbrowser / external-cdp)
  const [backend, setBackend] = useState<string>('')
  // Multi-tab strip state
  const [tabs, setTabs] = useState<BrowserTabInfo[]>([])
  const [activeId, setActiveId] = useState<string>('')
  // Touch device → show the special-key row (Esc/Tab/arrows/paste)
  const [isTouch] = useState<boolean>(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches,
  )
  // Right-click / long-press context menu (Copy / Paste / Select all). Position is in
  // container-local px (for the popup) — the frame coords are only needed if we ever
  // add a menu action that targets the click point.
  const [ctxMenu, setCtxMenu] = useState<{ cx: number; cy: number } | null>(null)
  // Resolver for the one in-flight "copy" round-trip: server reads the remote
  // selection and replies {type:'clipboard'}; there's only ever one pending request
  // at a time (the menu is closed as soon as Copy is clicked).
  const pendingCopyRef = useRef<((text: string) => void) | null>(null)
  // Last selection text read from the remote page, refreshed on every mouseup and
  // on menu-open — see copySelection() for why this cache exists (async clipboard
  // writes silently fail outside the synchronous click that started them).
  const lastSelectionRef = useRef<string>('')
  // Brief inline feedback ("Copied" / "Copy failed") — the alternative is a SILENT
  // failure, which is exactly the bug this whole feature was chasing.
  const [copyToast, setCopyToast] = useState<string | null>(null)
  const copyToastTimerRef = useRef<number | null>(null)
  // Long-press-to-open-menu on touch (no right-click there). Cancelled by movement
  // past TAP_SLOP (touchMovedRef) same as the tap-vs-scroll gesture.
  const longPressTimerRef = useRef<number | null>(null)
  const longPressFiredRef = useRef<boolean>(false)

  // ── WebSocket lifecycle ──────────────────────────────────────────────────────
  const connect = useCallback(() => {
    // Close any existing connection before opening a new one
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.close()
      wsRef.current = null
    }

    setConnState('connecting')
    setErrorMsg('')

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(
      `${proto}//${location.host}/api/browser/ws?project=${encodeURIComponent(projectId)}`,
    )
    // Accept binary frames as Blob (easier for createObjectURL)
    ws.binaryType = 'blob'
    wsRef.current = ws

    ws.onmessage = (e: MessageEvent) => {
      if (e.data instanceof Blob) {
        // Binary message = JPEG frame
        const newUrl = URL.createObjectURL(e.data)
        setFrameSrc(newUrl)
        // Revoke previous URL to avoid memory leaks
        if (lastObjUrlRef.current) {
          URL.revokeObjectURL(lastObjUrlRef.current)
        }
        lastObjUrlRef.current = newUrl
      } else if (typeof e.data === 'string') {
        // Text message = JSON control event
        try {
          const msg = JSON.parse(e.data) as Record<string, unknown>
          if (msg.type === 'ready') {
            setConnState('ready')
            if (typeof msg.backend === 'string') setBackend(msg.backend)
          } else if (msg.type === 'nav') {
            const shown = displayUrl((msg.url as string) ?? '')
            setUrlValue(shown)
            setUrlInput(shown)
          } else if (msg.type === 'error') {
            setErrorMsg((msg.message as string) ?? 'Unknown error')
            setConnState('error')
          } else if (msg.type === 'tabs') {
            setTabs(Array.isArray(msg.tabs) ? (msg.tabs as BrowserTabInfo[]) : [])
            setActiveId(typeof msg.activeId === 'string' ? msg.activeId : '')
          } else if (msg.type === 'clipboard') {
            const resolve = pendingCopyRef.current
            pendingCopyRef.current = null
            resolve?.(typeof msg.text === 'string' ? msg.text : '')
          }
        } catch {
          // Malformed JSON — ignore
        }
      }
    }

    ws.onopen = () => {
      // State will be set to 'ready' when server sends {type:"ready"}
      // If the server doesn't send it, we stay in 'connecting' which is fine.
    }

    ws.onclose = () => {
      setConnState('disconnected')
    }

    ws.onerror = () => {
      setConnState('disconnected')
    }
  }, [projectId])

  // Open WS on mount, clean up on unmount
  useEffect(() => {
    connect()
    return () => {
      const ws = wsRef.current
      if (ws) {
        ws.onclose = null // suppress state update on intentional close
        ws.close()
        wsRef.current = null
      }
      // Revoke the last object URL to avoid leaks
      if (lastObjUrlRef.current) {
        URL.revokeObjectURL(lastObjUrlRef.current)
        lastObjUrlRef.current = null
      }
      if (longPressTimerRef.current !== null) {
        window.clearTimeout(longPressTimerRef.current)
        longPressTimerRef.current = null
      }
      if (copyToastTimerRef.current !== null) {
        window.clearTimeout(copyToastTimerRef.current)
        copyToastTimerRef.current = null
      }
    }
  }, [connect])

  // Reconnect when the app returns to the foreground. On a phone, turning the
  // screen off suspends the page → the browser WS drops (idle proxy + the pane's
  // own watchdog), leaving a dead/blank pane on wake. Re-open it when the tab
  // becomes visible / the network resumes — the server re-primes the last frame
  // (or a fresh start page) so the browser comes back instead of staying broken.
  const connStateRef = useRef(connState)
  useEffect(() => { connStateRef.current = connState }, [connState])
  useEffect(() => {
    const maybeReconnect = () => {
      if (document.visibilityState !== 'visible') return
      if (connStateRef.current === 'error') return // server refused (e.g. module off) — don't loop
      const ws = wsRef.current
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        connect()
      }
    }
    document.addEventListener('visibilitychange', maybeReconnect)
    window.addEventListener('online', maybeReconnect)
    window.addEventListener('focus', maybeReconnect)
    return () => {
      document.removeEventListener('visibilitychange', maybeReconnect)
      window.removeEventListener('online', maybeReconnect)
      window.removeEventListener('focus', maybeReconnect)
    }
  }, [connect])

  // ── Send helpers ─────────────────────────────────────────────────────────────
  const send = useCallback((payload: object) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload))
    }
  }, [])

  // Coalesce a burst of wheel/touch-scroll deltas into ONE 'wheel' message every
  // WHEEL_FLUSH_MS. Every raw wheel/touchmove event used to become its own WS
  // round trip + server-side CDP dispatch, processed strictly in order — a fast
  // trackpad flick or touch drag easily fires dozens of them within a couple
  // hundred ms, and the visible scroll lags behind the real gesture by however
  // long draining that backlog takes (worse the higher the ping — this is queuing
  // delay on top of network latency, not just network latency). Summing deltas
  // into one dispatch per frame keeps the same total scroll distance with a
  // fraction of the round trips.
  const flushWheel = useCallback(() => {
    wheelFlushScheduledRef.current = false
    const acc = wheelAccumRef.current
    if (!acc) return
    wheelAccumRef.current = null
    send({ t: 'wheel', x: acc.x, y: acc.y, dx: acc.dx, dy: acc.dy })
  }, [send])

  const accumulateWheel = useCallback(
    (x: number, y: number, dx: number, dy: number) => {
      const acc = wheelAccumRef.current
      if (acc) {
        acc.dx += dx
        acc.dy += dy
        acc.x = x
        acc.y = y
      } else {
        wheelAccumRef.current = { x, y, dx, dy }
      }
      if (!wheelFlushScheduledRef.current) {
        wheelFlushScheduledRef.current = true
        window.setTimeout(flushWheel, WHEEL_FLUSH_MS)
      }
    },
    [flushWheel],
  )

  // ── Mouse event handlers ─────────────────────────────────────────────────────
  const getImgRect = useCallback((): DOMRect | null => {
    return imgRef.current?.getBoundingClientRect() ?? null
  }, [])

  // ── Context menu actions (Copy / Paste / Select all) ─────────────────────────
  // Defined here, ahead of the mouse handlers below, because onMouseUp warms the
  // selection cache on every drag-end and needs refreshSelectionCache in scope.
  const closeCtxMenu = useCallback(() => setCtxMenu(null), [])

  const showCopyToast = useCallback((msg: string) => {
    setCopyToast(msg)
    if (copyToastTimerRef.current !== null) window.clearTimeout(copyToastTimerRef.current)
    copyToastTimerRef.current = window.setTimeout(() => setCopyToast(null), 1400)
  }, [])

  // Writes into the OPERATOR's OS clipboard. navigator.clipboard.writeText() can
  // silently reject when it isn't reached synchronously from the click that started
  // it (some browsers gate it on transient user activation / document focus) — the
  // legacy execCommand('copy') path is a robust fallback for that case. Either way
  // the outcome is surfaced (showCopyToast) instead of failing invisibly.
  const writeToClipboard = useCallback(async (text: string) => {
    if (!text) { showCopyToast('Nothing selected'); return }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text)
        showCopyToast('Copied')
        return
      }
      throw new Error('Clipboard API unavailable')
    } catch {
      try {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.focus()
        ta.select()
        const ok = document.execCommand('copy')
        document.body.removeChild(ta)
        showCopyToast(ok ? 'Copied' : 'Copy failed')
      } catch {
        showCopyToast('Copy failed')
      }
    }
  }, [showCopyToast])

  // Silently refreshes the "last known selection" cache (no clipboard write) — called
  // on every mouseup and whenever the context menu opens, so that by the time the
  // operator actually clicks Copy / presses Ctrl+C, the text is already in hand and
  // the write can happen SYNCHRONOUSLY inside that click/keypress handler. Without
  // this, writeToClipboard would only ever run after the WS round-trip completes,
  // which is exactly the async gap that made Copy silently do nothing.
  const refreshSelectionCache = useCallback(() => {
    pendingCopyRef.current = (text: string) => { lastSelectionRef.current = text }
    send({ t: 'copy' })
  }, [send])

  // Reads the remote page's current selection and writes it into the OPERATOR's
  // clipboard. A forwarded Ctrl+C only reaches the remote Chromium's OWN (invisible,
  // server-side) clipboard, so the text has to be pulled out and shipped back — mirror
  // of pasteClipboard's direction (defined below, near the keyboard handlers).
  const copySelection = useCallback(() => {
    setCtxMenu(null)
    const cached = lastSelectionRef.current
    if (cached) {
      void writeToClipboard(cached)
    }
    // Refresh in the background too — covers a selection that changed since the
    // cache was last warmed. Best-effort only if nothing was cached above: that write
    // happens after the round-trip completes, outside the original click/keypress.
    pendingCopyRef.current = (text: string) => {
      lastSelectionRef.current = text
      if (!cached) void writeToClipboard(text)
    }
    send({ t: 'copy' })
  }, [send, writeToClipboard])

  const selectAll = useCallback(() => {
    setCtxMenu(null)
    containerRef.current?.focus()
    send({ t: 'key', action: 'down', key: 'a', text: '', mods: 2 })
    send({ t: 'key', action: 'up', key: 'a', text: '', mods: 2 })
  }, [send])

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const now = Date.now()
      if (now - lastTouchTimeRef.current < TOUCH_GUARD_MS) return // synthetic from touch
      if (now - lastMouseMoveRef.current < MOUSE_MOVE_INTERVAL_MS) return
      lastMouseMoveRef.current = now
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      // `buttons` is what makes a move a DRAG (text selection) rather than a hover.
      send({ t: 'mouse', action: 'move', x, y, buttons: e.buttons, mods: modsOf(e) })
    },
    [send, getImgRect],
  )

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (Date.now() - lastTouchTimeRef.current < TOUCH_GUARD_MS) return // synthetic from touch
      // Right button opens the LOCAL context menu (onContextMenu, below) instead of
      // being forwarded — the remote page has no useful reaction to a bare right-click
      // and its own native context menu is browser chrome the screencast can't show.
      if (e.button === 2) return
      const rect = getImgRect()
      if (!rect) return
      // Give the pane keyboard focus on click so the operator can type immediately
      // afterwards (the container, tabIndex=0, owns the desktop key handlers).
      containerRef.current?.focus()
      setCtxMenu(null)
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      // e.detail = the click count (2 = select word, 3 = select line, for Chromium).
      send({
        t: 'mouse', action: 'down', x, y, button: buttonName(e.button),
        buttons: e.buttons, mods: modsOf(e), clickCount: e.detail || 1,
      })
    },
    [send, getImgRect],
  )

  const onMouseUp = useCallback(
    (e: React.MouseEvent) => {
      if (Date.now() - lastTouchTimeRef.current < TOUCH_GUARD_MS) return // synthetic from touch
      if (e.button === 2) return
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      send({
        t: 'mouse', action: 'up', x, y, button: buttonName(e.button),
        buttons: e.buttons, mods: modsOf(e), clickCount: e.detail || 1,
      })
      // A left-button mouseup is the natural end of a drag-selection — warm the
      // clipboard cache now so Copy/Ctrl+C can write synchronously later.
      if (e.button === 0) refreshSelectionCache()
    },
    [send, getImgRect, refreshSelectionCache],
  )

  // ── Context menu (Copy / Paste / Select all) ─────────────────────────────────
  const onContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      const containerRect = containerRef.current?.getBoundingClientRect()
      if (!containerRect) return
      containerRef.current?.focus()
      setCtxMenu({ cx: e.clientX - containerRect.left, cy: e.clientY - containerRect.top })
      refreshSelectionCache()
    },
    [refreshSelectionCache],
  )

  // ── Touch handlers (mobile co-control) ───────────────────────────────────────
  // A tap becomes a left click; dragging past TAP_SLOP becomes a wheel scroll.
  // The <img> sets touch-action:none so the page itself never steals the gesture.
  const clearLongPress = useCallback(() => {
    if (longPressTimerRef.current !== null) {
      window.clearTimeout(longPressTimerRef.current)
      longPressTimerRef.current = null
    }
  }, [])

  const onTouchStart = useCallback(
    (e: React.TouchEvent) => {
      lastTouchTimeRef.current = Date.now()
      const tc = e.touches[0]
      const rect = getImgRect()
      if (!tc || !rect) return
      const { x, y } = toFrameCoords(tc.clientX, tc.clientY, rect)
      touchStartRef.current = { x, y, cx: tc.clientX, cy: tc.clientY }
      lastTouchRef.current = { cx: tc.clientX, cy: tc.clientY }
      touchMovedRef.current = false
      longPressFiredRef.current = false
      // No right-click on touch — a long-press opens the same Copy/Paste/Select-all
      // menu instead. Cancelled by movement (onTouchMove) or a normal tap (onTouchEnd).
      clearLongPress()
      longPressTimerRef.current = window.setTimeout(() => {
        longPressFiredRef.current = true
        const containerRect = containerRef.current?.getBoundingClientRect()
        if (containerRect) {
          setCtxMenu({ cx: tc.clientX - containerRect.left, cy: tc.clientY - containerRect.top })
        }
        refreshSelectionCache()
      }, 550)
    },
    [getImgRect, clearLongPress, refreshSelectionCache],
  )

  const onTouchMove = useCallback(
    (e: React.TouchEvent) => {
      lastTouchTimeRef.current = Date.now()
      const tc = e.touches[0]
      const rect = getImgRect()
      const start = touchStartRef.current
      const last = lastTouchRef.current
      if (!tc || !rect || !start || !last) return
      if (!touchMovedRef.current && Math.hypot(tc.clientX - start.cx, tc.clientY - start.cy) > TAP_SLOP) {
        touchMovedRef.current = true
        clearLongPress()
      }
      if (touchMovedRef.current) {
        const { x, y } = toFrameCoords(tc.clientX, tc.clientY, rect)
        // Scale the finger delta into frame space; natural-scroll sign (finger up → page down).
        const dx = (last.cx - tc.clientX) * (FRAME_W / rect.width)
        const dy = (last.cy - tc.clientY) * (FRAME_H / rect.height)
        accumulateWheel(x, y, dx, dy)
      }
      lastTouchRef.current = { cx: tc.clientX, cy: tc.clientY }
    },
    [getImgRect, accumulateWheel, clearLongPress],
  )

  const onTouchEnd = useCallback((e: React.TouchEvent) => {
    lastTouchTimeRef.current = Date.now()
    clearLongPress()
    // Suppress the compatibility mouse events (mousedown/up/click) Chromium would
    // synthesize next: their NATIVE default would move focus to the container div,
    // stealing it from the hidden input and preventing the soft keyboard. touchend
    // is non-passive in React, so preventDefault() is honoured here.
    e.preventDefault()
    const start = touchStartRef.current
    const wasLongPress = longPressFiredRef.current
    touchStartRef.current = null
    lastTouchRef.current = null
    longPressFiredRef.current = false
    if (!start || touchMovedRef.current || wasLongPress) return
    // A tap → left click at the touch-down point, then raise the soft keyboard so
    // the operator can type into whatever field the click just focused.
    const { x, y } = start
    send({ t: 'mouse', action: 'move', x, y })
    send({ t: 'mouse', action: 'down', x, y, button: 'left' })
    send({ t: 'mouse', action: 'up', x, y, button: 'left' })
    hiddenInputRef.current?.focus()
  }, [send, clearLongPress])

  // ── Hidden-input soft keyboard (mobile) ──────────────────────────────────────
  // Tapping the pane focuses this off-screen input → the OS keyboard appears. We
  // diff its value against KBD_PAD on every `input` event and forward the delta as
  // char/Backspace key events (robust across Android keyboards that don't emit
  // usable keydown). Special keys (Enter/Tab/arrows) come through keydown.
  const resetHidden = useCallback(() => {
    const el = hiddenInputRef.current
    if (!el) return
    el.value = KBD_PAD
    try {
      el.setSelectionRange(KBD_PAD.length, KBD_PAD.length)
    } catch {
      /* setSelectionRange throws on some input types — harmless */
    }
  }, [])

  const onHiddenInput = useCallback(
    (e: React.FormEvent<HTMLInputElement>) => {
      const val = e.currentTarget.value
      if (val.length > KBD_PAD.length) {
        for (const ch of val.slice(KBD_PAD.length)) send({ t: 'key', action: 'char', text: ch })
      } else if (val.length < KBD_PAD.length) {
        for (let i = val.length; i < KBD_PAD.length; i++) {
          send({ t: 'key', action: 'down', key: 'Backspace', text: '' })
          send({ t: 'key', action: 'up', key: 'Backspace', text: '' })
        }
      }
      resetHidden()
    },
    [send, resetHidden],
  )

  const onHiddenKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const k = e.key
      // Printable keys (including Space) MUST fall through to the value diff above —
      // intercepting them here would send a keystroke with no text and insert nothing.
      // Backspace also comes through the diff (Android keyboards do not report it
      // reliably as a keydown); everything else non-printable is forwarded here.
      if (k.length > 1 && k !== 'Backspace' && (SWALLOW_KEYS.has(k) || k === 'Escape')) {
        const mods = modsOf(e)
        send({ t: 'key', action: 'down', key: k, text: k === 'Enter' ? '\r' : '', mods })
        send({ t: 'key', action: 'up', key: k, text: '', mods })
        e.preventDefault()
      }
    },
    [send],
  )

  // ── Wheel handler ────────────────────────────────────────────────────────────
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault()
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      accumulateWheel(x, y, e.deltaX, e.deltaY)
    },
    [accumulateWheel, getImgRect],
  )

  // ── Keyboard handler ─────────────────────────────────────────────────────────
  // Paste needs its own path: the remote Chromium has its own (empty) clipboard, so
  // Ctrl+V forwarded as a keystroke pastes nothing. Read the operator's clipboard and
  // ship the text — that is how a password lands in a remote login form.
  const pasteClipboard = useCallback(async () => {
    try {
      const text = await navigator.clipboard.readText()
      if (text) send({ t: 'paste', text })
    } catch {
      /* denied / unsupported — nothing sensible to do */
    }
  }, [send])

  const menuPaste = useCallback(() => {
    setCtxMenu(null)
    void pasteClipboard()
  }, [pasteClipboard])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (ctxMenu) {
        // Any keypress dismisses the open menu, mirroring a native context menu;
        // Escape specifically must not also be forwarded to the remote page.
        setCtxMenu(null)
        if (e.key === 'Escape') { e.preventDefault(); return }
      }
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'v' || e.key === 'V')) {
        e.preventDefault()
        void pasteClipboard()
        return
      }
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'c' || e.key === 'C')) {
        e.preventDefault()
        copySelection()
        return
      }
      // Prevent browser scroll/shortcuts for forwarded keys
      if (SWALLOW_KEYS.has(e.key) || ((e.ctrlKey || e.metaKey) && e.key.length === 1)) {
        e.preventDefault()
      }
      send({
        t: 'key',
        action: 'down',
        key: e.key,
        text: e.key.length === 1 ? e.key : '',
        mods: modsOf(e),
        repeat: e.repeat,
      })
    },
    [send, pasteClipboard, copySelection, ctxMenu],
  )

  const onKeyUp = useCallback(
    (e: React.KeyboardEvent) => {
      send({ t: 'key', action: 'up', key: e.key, text: '', mods: modsOf(e) })
    },
    [send],
  )

  // One tap on the special-key row = a full down/up of that key.
  const tapKey = useCallback(
    (key: string) => {
      send({ t: 'key', action: 'down', key, text: key === 'Enter' ? '\r' : '' })
      send({ t: 'key', action: 'up', key, text: '' })
    },
    [send],
  )

  // ── URL bar navigation ───────────────────────────────────────────────────────
  const navigate = useCallback(() => {
    const url = urlInput.trim()
    if (!url) return
    send({ t: 'navigate', url })
  }, [send, urlInput])

  const onUrlKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        navigate()
      }
    },
    [navigate],
  )

  // ── Overlay messages ─────────────────────────────────────────────────────────
  function renderOverlay() {
    if (connState === 'ready' && frameSrc) return null

    let message = ''
    if (connState === 'connecting') {
      message = t['browser.connecting']
    } else if (connState === 'disconnected') {
      message = t['browser.disconnected']
    } else if (connState === 'error') {
      message = t['browser.error'].replace('{msg}', errorMsg)
    } else if (connState === 'ready' && !frameSrc) {
      message = t['browser.not_ready']
    }

    return (
      <div
        style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          color: 'var(--text-dim, #888)',
          fontSize: 13,
          background: 'var(--bg, #0d0d0d)',
        }}
      >
        <span>{message}</span>
        {(connState === 'disconnected' || connState === 'error') && (
          <button
            onClick={connect}
            style={{
              fontSize: 12,
              padding: '4px 14px',
              borderRadius: 6,
              border: '1px solid var(--border, #2a2a2a)',
              background: 'var(--bg2, #161616)',
              color: 'var(--text, #d4d4d4)',
              cursor: 'pointer',
            }}
          >
            Reconnect
          </button>
        )}
      </div>
    )
  }

  // ── Context menu ─────────────────────────────────────────────────────────────
  function renderCtxMenu() {
    if (!ctxMenu) return null
    const items: { label: string; onClick: () => void }[] = [
      { label: 'Copy', onClick: copySelection },
      { label: 'Paste', onClick: menuPaste },
      { label: 'Select all', onClick: selectAll },
    ]
    return (
      <>
        {/* Full-pane click-catcher so any click outside the menu dismisses it */}
        <div
          onClick={closeCtxMenu}
          onContextMenu={e => { e.preventDefault(); closeCtxMenu() }}
          style={{ position: 'absolute', inset: 0, zIndex: 20 }}
        />
        <div
          style={{
            position: 'absolute',
            left: ctxMenu.cx,
            top: ctxMenu.cy,
            zIndex: 21,
            minWidth: 140,
            padding: '4px 0',
            borderRadius: 7,
            border: '1px solid var(--border, #2a2a2a)',
            background: 'var(--bg2, #161616)',
            boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
          }}
        >
          {items.map(item => (
            <div
              key={item.label}
              onClick={item.onClick}
              style={{
                padding: '7px 14px',
                fontSize: 13,
                color: 'var(--text, #d4d4d4)',
                cursor: 'pointer',
                userSelect: 'none',
              }}
              onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.background = 'var(--bg, #0d0d0d)' }}
              onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.background = 'transparent' }}
            >
              {item.label}
            </div>
          ))}
        </div>
      </>
    )
  }

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg, #0d0d0d)',
        overflow: 'hidden',
      }}
    >
      {/* Tab strip — only shown when ready and at least one tab exists */}
      {connState === 'ready' && tabs.length >= 1 && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'row',
            alignItems: 'center',
            overflowX: 'auto',
            flexShrink: 0,
            gap: 4,
            padding: '0 4px',
            height: 30,
            borderBottom: '1px solid var(--border, #1e1e1e)',
            background: 'var(--bg2, #111)',
            whiteSpace: 'nowrap',
          }}
        >
          {tabs.map(tab => {
            const isActive = tab.active || tab.id === activeId
            return (
              <div
                key={tab.id}
                onClick={() => send({ t: 'tab.activate', id: tab.id })}
                title={tab.url || tab.title || 'New tab'}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  maxWidth: 120,
                  fontSize: 12,
                  borderRadius: 5,
                  padding: '3px 8px',
                  cursor: 'pointer',
                  flexShrink: 0,
                  color: isActive ? 'var(--text, #d4d4d4)' : 'var(--text-dim, #6e7681)',
                  background: isActive ? 'var(--bg, #0d0d0d)' : 'transparent',
                  borderTop: isActive ? '2px solid var(--accent)' : '2px solid transparent',
                  userSelect: 'none',
                }}
              >
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    maxWidth: tabs.length > 1 ? 96 : 108,
                  }}
                >
                  {tab.title || tab.url || 'New tab'}
                </span>
                {tabs.length > 1 && (
                  <span
                    onClick={e => { e.stopPropagation(); send({ t: 'tab.close', id: tab.id }) }}
                    title="Close tab"
                    style={{
                      marginLeft: 6,
                      color: 'var(--text-dim, #6e7681)',
                      lineHeight: 1,
                      cursor: 'pointer',
                    }}
                    onMouseEnter={e => { (e.currentTarget as HTMLSpanElement).style.color = 'var(--text, #d4d4d4)' }}
                    onMouseLeave={e => { (e.currentTarget as HTMLSpanElement).style.color = 'var(--text-dim, #6e7681)' }}
                  >
                    ×
                  </span>
                )}
              </div>
            )
          })}
          {/* New-tab button */}
          <div
            onClick={() => send({ t: 'tab.new' })}
            title="New tab"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 14,
              borderRadius: 5,
              padding: '2px 8px',
              cursor: 'pointer',
              flexShrink: 0,
              color: 'var(--text-dim, #6e7681)',
              background: 'transparent',
              borderTop: '2px solid transparent',
              userSelect: 'none',
            }}
            onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.color = 'var(--text, #d4d4d4)' }}
            onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.color = 'var(--text-dim, #6e7681)' }}
          >
            +
          </div>
        </div>
      )}

      {/* URL bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '4px 6px',
          borderBottom: '1px solid var(--border, #1e1e1e)',
          background: 'var(--bg2, #111)',
          flexShrink: 0,
        }}
      >
        {/* Connection status dot */}
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            flexShrink: 0,
            background:
              connState === 'ready'
                ? 'var(--green, #3fb950)'
                : connState === 'connecting'
                  ? 'var(--yellow, #d29922)'
                  : 'var(--text-dim, #555)',
          }}
          title={connState}
        />
        {/* History controls — co-browsing without them means retyping URLs by hand */}
        {([
          { label: '←', act: 'back', title: 'Back' },
          { label: '→', act: 'forward', title: 'Forward' },
          { label: '⟳', act: 'reload', title: 'Reload' },
        ] as const).map(b => (
          <button
            key={b.act}
            onClick={() => send({ t: b.act })}
            disabled={connState !== 'ready'}
            title={b.title}
            style={{
              flexShrink: 0,
              fontSize: 13,
              lineHeight: 1,
              padding: '4px 7px',
              borderRadius: 5,
              border: '1px solid var(--border, #2a2a2a)',
              background: 'var(--bg, #0d0d0d)',
              color: connState === 'ready' ? 'var(--text, #d4d4d4)' : 'var(--text-dim, #555)',
              cursor: connState === 'ready' ? 'pointer' : 'not-allowed',
            }}
          >
            {b.label}
          </button>
        ))}
        {/* spec-066: stealth / external backend badge (built-in is the silent default) */}
        {backend && backend !== 'builtin' && (
          <span
            title={`Backend: ${backend}`}
            style={{
              flexShrink: 0, fontSize: 10, fontWeight: 600, letterSpacing: 0.3,
              padding: '2px 6px', borderRadius: 5, textTransform: 'uppercase',
              color: 'var(--text2, #aaa)', border: '1px solid var(--border, #333)',
              background: 'var(--bg, #0d0d0d)',
            }}
          >
            {backend === 'cloakbrowser' ? '🛡 stealth' : '🔌 cdp'}
          </span>
        )}
        <input
          type="url"
          value={urlInput}
          onChange={e => setUrlInput(e.target.value)}
          onKeyDown={onUrlKeyDown}
          placeholder={t['browser.url_placeholder']}
          style={{
            flex: 1,
            fontSize: 12,
            padding: '3px 7px',
            borderRadius: 5,
            border: '1px solid var(--border, #2a2a2a)',
            background: 'var(--bg, #0d0d0d)',
            color: 'var(--text, #d4d4d4)',
            fontFamily: 'inherit',
            outline: 'none',
          }}
        />
        <button
          onClick={navigate}
          disabled={connState !== 'ready'}
          style={{
            fontSize: 12,
            padding: '3px 10px',
            borderRadius: 5,
            border: '1px solid var(--border, #2a2a2a)',
            background: connState === 'ready' ? 'var(--bg2, #161616)' : 'var(--bg, #0d0d0d)',
            color: connState === 'ready' ? 'var(--text, #d4d4d4)' : 'var(--text-dim, #555)',
            cursor: connState === 'ready' ? 'pointer' : 'not-allowed',
            whiteSpace: 'nowrap',
          }}
        >
          {t['browser.go']}
        </button>
        {/* Current URL display (read-only, shows server-confirmed URL) */}
        {urlValue && urlValue !== urlInput && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--text-dim, #6e7681)',
              maxWidth: 200,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={urlValue}
          >
            → {urlValue}
          </span>
        )}
      </div>

      {/* Special-key row — a soft keyboard has no Esc/Tab/arrows, and pasting a
          password needs the operator's clipboard, not the remote one. Touch only:
          on desktop the physical keyboard already sends all of this. */}
      {isTouch && connState === 'ready' && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            padding: '4px 6px',
            overflowX: 'auto',
            flexShrink: 0,
            borderBottom: '1px solid var(--border, #1e1e1e)',
            background: 'var(--bg2, #111)',
          }}
        >
          {KEY_ROW.map(k => (
            <button
              key={k.key}
              title={k.title}
              // Keep focus on the hidden input so the soft keyboard stays up
              onPointerDown={e => e.preventDefault()}
              onClick={() => { tapKey(k.key); hiddenInputRef.current?.focus() }}
              style={keyRowBtn}
            >
              {k.label}
            </button>
          ))}
          <button
            title="Paste from clipboard"
            onPointerDown={e => e.preventDefault()}
            onClick={() => { void pasteClipboard(); hiddenInputRef.current?.focus() }}
            style={{ ...keyRowBtn, marginLeft: 'auto' }}
          >
            📋
          </button>
        </div>
      )}

      {/* Frame viewport */}
      <div
        ref={containerRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onWheel={onWheel}
        onContextMenu={onContextMenu}
        style={{
          flex: 1,
          position: 'relative',
          overflow: 'hidden',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--bg, #0d0d0d)',
          outline: 'none',
          cursor: connState === 'ready' ? 'default' : 'not-allowed',
        }}
      >
        {frameSrc && (
          <img
            ref={imgRef}
            src={frameSrc}
            alt="Live browser frame"
            draggable={false}
            onMouseMove={onMouseMove}
            onMouseDown={onMouseDown}
            onMouseUp={onMouseUp}
            onTouchStart={onTouchStart}
            onTouchMove={onTouchMove}
            onTouchEnd={onTouchEnd}
            style={{
              // Fill the pane while preserving aspect ratio
              maxWidth: '100%',
              maxHeight: '100%',
              objectFit: 'contain',
              display: 'block',
              userSelect: 'none',
              WebkitUserSelect: 'none',
              // Own the touch gesture so the page never scrolls/zooms it away
              touchAction: 'none',
              // Prevent the image from consuming focus (the container div does)
              pointerEvents: connState === 'ready' ? 'auto' : 'none',
            }}
          />
        )}
        {/* Off-screen capture for the mobile soft keyboard (focused on tap). */}
        <input
          ref={hiddenInputRef}
          defaultValue={KBD_PAD}
          onInput={onHiddenInput}
          onKeyDown={onHiddenKeyDown}
          onFocus={resetHidden}
          autoCapitalize="none"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          aria-hidden="true"
          tabIndex={-1}
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: 1,
            height: 1,
            opacity: 0,
            border: 'none',
            padding: 0,
            margin: 0,
            // iOS zooms the viewport when focusing inputs with font-size < 16px
            fontSize: 16,
            // Never intercept taps — it's focused programmatically from onTouchEnd
            pointerEvents: 'none',
          }}
        />
        {renderOverlay()}
        {renderCtxMenu()}
        {copyToast && (
          <div
            style={{
              position: 'absolute',
              left: '50%',
              bottom: 14,
              transform: 'translateX(-50%)',
              zIndex: 22,
              padding: '5px 12px',
              borderRadius: 6,
              fontSize: 12,
              color: 'var(--text, #d4d4d4)',
              background: 'var(--bg2, #161616)',
              border: '1px solid var(--border, #2a2a2a)',
              pointerEvents: 'none',
            }}
          >
            {copyToast}
          </div>
        )}
      </div>
    </div>
  )
}
