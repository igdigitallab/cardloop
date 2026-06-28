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

interface Props {
  projectId: string
}

type ConnState = 'connecting' | 'ready' | 'disconnected' | 'error'

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

export function BrowserTab({ projectId }: Props) {
  const wsRef = useRef<WebSocket | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const lastObjUrlRef = useRef<string | null>(null)
  const lastMouseMoveRef = useRef<number>(0)

  const [connState, setConnState] = useState<ConnState>('connecting')
  const [errorMsg, setErrorMsg] = useState<string>('')
  const [frameSrc, setFrameSrc] = useState<string>('')
  // Current page URL/title received from the server
  const [urlValue, setUrlValue] = useState<string>('')
  const [urlInput, setUrlInput] = useState<string>('')

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
          } else if (msg.type === 'nav') {
            const url = (msg.url as string) ?? ''
            setUrlValue(url)
            setUrlInput(url)
          } else if (msg.type === 'error') {
            setErrorMsg((msg.message as string) ?? 'Unknown error')
            setConnState('error')
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
    }
  }, [connect])

  // ── Send helpers ─────────────────────────────────────────────────────────────
  const send = useCallback((payload: object) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload))
    }
  }, [])

  // ── Mouse event handlers ─────────────────────────────────────────────────────
  const getImgRect = useCallback((): DOMRect | null => {
    return imgRef.current?.getBoundingClientRect() ?? null
  }, [])

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const now = Date.now()
      if (now - lastMouseMoveRef.current < MOUSE_MOVE_INTERVAL_MS) return
      lastMouseMoveRef.current = now
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      send({ t: 'mouse', action: 'move', x, y })
    },
    [send, getImgRect],
  )

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      send({ t: 'mouse', action: 'down', x, y, button: buttonName(e.button) })
    },
    [send, getImgRect],
  )

  const onMouseUp = useCallback(
    (e: React.MouseEvent) => {
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      send({ t: 'mouse', action: 'up', x, y, button: buttonName(e.button) })
    },
    [send, getImgRect],
  )

  const onContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
  }, [])

  // ── Wheel handler ────────────────────────────────────────────────────────────
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault()
      const rect = getImgRect()
      if (!rect) return
      const { x, y } = toFrameCoords(e.clientX, e.clientY, rect)
      send({ t: 'wheel', x, y, dx: e.deltaX, dy: e.deltaY })
    },
    [send, getImgRect],
  )

  // ── Keyboard handler ─────────────────────────────────────────────────────────
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Prevent browser scroll/shortcuts for forwarded keys
      if (
        e.key === 'ArrowUp' ||
        e.key === 'ArrowDown' ||
        e.key === 'ArrowLeft' ||
        e.key === 'ArrowRight' ||
        e.key === 'Tab' ||
        e.key === 'Backspace' ||
        e.key === ' '
      ) {
        e.preventDefault()
      }
      send({
        t: 'key',
        action: 'down',
        key: e.key,
        text: e.key.length === 1 ? e.key : '',
      })
    },
    [send],
  )

  const onKeyUp = useCallback(
    (e: React.KeyboardEvent) => {
      send({ t: 'key', action: 'up', key: e.key, text: '' })
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
            style={{
              // Fill the pane while preserving aspect ratio
              maxWidth: '100%',
              maxHeight: '100%',
              objectFit: 'contain',
              display: 'block',
              userSelect: 'none',
              WebkitUserSelect: 'none',
              // Prevent the image from consuming focus (the container div does)
              pointerEvents: connState === 'ready' ? 'auto' : 'none',
            }}
          />
        )}
        {renderOverlay()}
      </div>
    </div>
  )
}
