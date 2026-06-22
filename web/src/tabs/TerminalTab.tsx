import { useEffect, useRef, useCallback } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

interface Props {
  isActive: boolean
}

export function TerminalTab({ isActive }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const sendResize = useCallback(() => {
    const fit = fitRef.current
    const term = termRef.current
    const ws = wsRef.current
    if (!fit || !term) return
    fit.fit()
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
  }, [])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"Geist Mono", "Cascadia Code", Menlo, Monaco, Consolas, monospace',
      theme: {
        background: '#0d0d0d',
        foreground: '#d4d4d4',
        cursor: '#d4d4d4',
        selectionBackground: '#3a3a3a',
        black: '#1e1e1e',
        red: '#f85149',
        green: '#3fb950',
        yellow: '#d29922',
        blue: '#58a6ff',
        magenta: '#bc8cff',
        cyan: '#39c5cf',
        white: '#b1bac4',
        brightBlack: '#6e7681',
        brightRed: '#ff7b72',
        brightGreen: '#56d364',
        brightYellow: '#e3b341',
        brightBlue: '#79c0ff',
        brightMagenta: '#d2a8ff',
        brightCyan: '#56d4dd',
        brightWhite: '#f0f6fc',
      },
      convertEol: false,
      allowProposedApi: false,
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(container)
    fitAddon.fit()

    termRef.current = term
    fitRef.current = fitAddon

    // Build WebSocket URL from current location
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/api/terminal/ws`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    ws.onopen = () => {
      fitAddon.fit()
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
      } else if (typeof e.data === 'string') {
        term.write(e.data)
      }
    }

    ws.onclose = () => {
      term.write('\r\n\x1b[90m[session closed — refresh tab to reconnect]\x1b[0m\r\n')
    }

    ws.onerror = () => {
      term.write('\r\n\x1b[31m[connection error]\x1b[0m\r\n')
    }

    // Forward keyboard input to PTY
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })

    // Resize on container size change
    const ro = new ResizeObserver(() => {
      // Small delay so the container has settled its new dimensions
      setTimeout(() => {
        fitAddon.fit()
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
        }
      }, 50)
    })
    ro.observe(container)

    return () => {
      ro.disconnect()
      ws.close()
      term.dispose()
      termRef.current = null
      fitRef.current = null
      wsRef.current = null
    }
  }, [])

  // Re-fit when the tab becomes visible
  useEffect(() => {
    if (isActive) {
      // display:none → visible transition; layout needs one frame to settle
      const id = setTimeout(() => sendResize(), 60)
      return () => clearTimeout(id)
    }
  }, [isActive, sendResize])

  return (
    <div
      style={{
        width: '100%',
        height: '100%',
        background: '#0d0d0d',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div
        ref={containerRef}
        style={{ flex: 1, overflow: 'hidden', padding: '4px 6px' }}
      />
    </div>
  )
}
