import { useEffect, useRef, useCallback, useState, type ReactNode } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'
import { ArrowUp, ArrowDown, ArrowLeft, ArrowRight } from 'lucide-react'
import { t } from '../i18n'
import {
  QUICK_KEY_ORDER,
  QUICK_KEY_SEQUENCES,
  applyCtrlLatch,
  type QuickKeyId,
} from './quickKeys'

interface Props {
  isActive: boolean
}

// Reconstruct the most recent URL from the terminal buffer, re-joining
// lines split by soft-wrap (xterm marks continuation rows with isWrapped).
// This lets us grab a full login URL that the user could never reliably
// select+copy by hand on mobile (selection truncates at the wrap boundary).
function findLastUrl(term: Terminal): string | null {
  const buf = term.buffer.active
  const logical: string[] = []
  let cur = ''
  for (let i = 0; i < buf.length; i++) {
    const line = buf.getLine(i)
    if (!line) continue
    if (line.isWrapped) {
      cur += line.translateToString(true)
    } else {
      if (cur) logical.push(cur)
      cur = line.translateToString(true)
    }
  }
  if (cur) logical.push(cur)

  const re = /https?:\/\/[^\s]+/g
  let last: string | null = null
  let lastClaude: string | null = null
  for (const ln of logical) {
    const matches = ln.match(re)
    if (!matches) continue
    for (const m of matches) {
      last = m
      if (/claude|oauth|anthropic/i.test(m)) lastClaude = m
    }
  }
  // Prefer a Claude/OAuth URL (the login link) over any other URL on screen.
  return lastClaude || last
}

// Visible glyph and accessible name for each QuickKeys bar button. Bare
// punctuation is shown as the literal character sent (not translated — same
// reasoning as a physical keypad's "7" not needing localization); the
// keyboard abbreviations and all aria-labels go through i18n. Arrows are
// lucide-react icons (already a project dependency, no new one added) rather
// than Unicode glyphs — some platforms render ↑↓←→ through a color-emoji
// fallback font that ignores the button's CSS `color`, breaking the
// achromatic styling; an inline SVG with stroke="currentColor" cannot.
const QUICK_KEY_LABELS: Record<QuickKeyId, ReactNode> = {
  esc: t['quickkeys.esc'],
  tab: t['quickkeys.tab'],
  ctrl: t['quickkeys.ctrl'],
  up: <ArrowUp size={16} aria-hidden="true" />,
  down: <ArrowDown size={16} aria-hidden="true" />,
  left: <ArrowLeft size={16} aria-hidden="true" />,
  right: <ArrowRight size={16} aria-hidden="true" />,
  'ctrl-c': t['quickkeys.ctrl_c'],
  pipe: '|',
  tilde: '~',
  slash: '/',
  dash: '-',
}

const QUICK_KEY_ARIA: Record<QuickKeyId, string> = {
  esc: t['quickkeys.aria_esc'],
  tab: t['quickkeys.aria_tab'],
  ctrl: t['quickkeys.aria_ctrl'],
  up: t['quickkeys.aria_up'],
  down: t['quickkeys.aria_down'],
  left: t['quickkeys.aria_left'],
  right: t['quickkeys.aria_right'],
  'ctrl-c': t['quickkeys.aria_ctrl_c'],
  pipe: t['quickkeys.aria_pipe'],
  tilde: t['quickkeys.aria_tilde'],
  slash: t['quickkeys.aria_slash'],
  dash: t['quickkeys.aria_dash'],
}

export function TerminalTab({ isActive }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  // Last string an in-terminal app asked to copy via OSC 52 (e.g. Claude's
  // "(c to copy)" on the login screen). Authoritative — the exact bytes the
  // app intended, free of any visual line-wrapping.
  const lastClipboardRef = useRef<string | null>(null)
  const [copied, setCopied] = useState<'idle' | 'ok' | 'none'>('idle')

  // ── QuickKeys (spec-082 D): mobile key bar for Esc/Tab/Ctrl/arrows/etc ──
  // ctrlArmedRef is the source of truth read inside the WS-lifecycle effect's
  // term.onData closure (captured once at mount); ctrlArmed (state) exists
  // only to re-render the button's visible latched styling.
  const ctrlArmedRef = useRef(false)
  const [ctrlArmed, setCtrlArmed] = useState(false)
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.('(max-width: 768px)').matches,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(max-width: 768px)')
    const onChange = () => setIsMobile(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // Single write path onto the terminal's WebSocket — the same path
  // term.onData already uses. QuickKeys buttons call this directly instead
  // of going through xterm's key handling (they're synthetic taps, not real
  // keystrokes), so there is still only one transport to the PTY.
  const sendToTerminal = useCallback((data: string) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data))
    }
  }, [])

  const handleQuickKey = useCallback(
    (id: QuickKeyId) => {
      if (id === 'ctrl') {
        setCtrlArmed((prev) => {
          const next = !prev
          ctrlArmedRef.current = next
          return next
        })
        return
      }
      // Any other key — including a second, unrelated quick-key tap — always
      // releases a pending Ctrl latch, matching "de-latches after one key".
      if (ctrlArmedRef.current) {
        ctrlArmedRef.current = false
        setCtrlArmed(false)
      }
      const seq = QUICK_KEY_SEQUENCES[id]
      if (seq) sendToTerminal(seq)
    },
    [sendToTerminal],
  )

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
      // Enable xterm's proposed API: required by current addons (unicode11/webgl) and
      // forward-compatible. Harmless for the core terminal.
      allowProposedApi: true,
      // Generous scrollback for long agent/login sessions; alt-screen TUIs are unaffected.
      scrollback: 5000,
    })

    const fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    // Make URLs tappable — opens in a new tab so the user never has to
    // hand-select a wrapped login URL on a phone.
    term.loadAddon(
      new WebLinksAddon((event, uri) => {
        event.preventDefault()
        window.open(uri, '_blank', 'noopener,noreferrer')
      }),
    )

    // Honor OSC 52 clipboard writes. xterm ignores them by default, so an
    // in-terminal "copy" (e.g. Claude Code's "(c to copy)" on the OAuth login
    // screen) silently does nothing and the user pastes stale/partial text.
    // OSC 52 carries the FULL string the app intended, regardless of how the
    // URL visually wraps in the viewport — this is the reliable copy path.
    term.parser.registerOscHandler(52, (data) => {
      const semi = data.indexOf(';')
      if (semi === -1) return false
      const payload = data.slice(semi + 1)
      if (!payload || payload === '?') return false // paste/read request — unsupported
      let text: string
      try {
        // base64 → UTF-8 (atob yields a binary string; unescape the bytes)
        text = decodeURIComponent(escape(atob(payload)))
      } catch {
        try {
          text = atob(payload)
        } catch {
          return false
        }
      }
      if (!text) return false
      lastClipboardRef.current = text
      // The OSC arrives ms after the user pressed the key, still inside the
      // keystroke's transient activation window, so the async write is allowed.
      // If it's blocked, the toolbar Copy button falls back to this stash.
      navigator.clipboard?.writeText(text).catch(() => {})
      return true
    })

    // ── Work around two xterm 6.0.0 issues that blank the pane for modern TUIs ──
    // (the Antigravity `agy` CLI, Claude Code, anything on Bubbletea/ratatui).
    //
    // 1) DECRQM crash — the real "agy won't open" bug. xterm's built-in mode-query handler
    //    (requestMode) is broken in our bundled build: a compiled `const enum` lost its
    //    variable declaration during minification, so the handler throws
    //    "ReferenceError: n is not defined" the instant an app probes a mode — e.g. agy's
    //    startup `CSI ? 2026 $ p`. The throw aborts xterm's parse/write loop, so the
    //    terminal freezes on whatever was last painted (the prompt) and the app never shows.
    //    (The non-minified UMD build is fine, which is why it only bites in the cockpit.)
    //    Intercept DECRQM and consume it so the broken builtin never runs; apps that probe a
    //    mode just get no reply and fall back, exactly as on a terminal lacking that mode.
    const consumeDecrqm = () => true
    term.parser.registerCsiHandler({ prefix: '?', intermediates: '$', final: 'p' }, consumeDecrqm)
    term.parser.registerCsiHandler({ intermediates: '$', final: 'p' }, consumeDecrqm)
    //
    // 2) Synchronized output (DEC mode 2026). While it's on, xterm SKIPS painting; a
    //    delayed/lost closing `?2026l` would leave the pane blank. We don't need batched
    //    frames here, so swallow the standalone set/reset and always paint. Other private
    //    modes (alt-screen 1049, bracketed paste 2004, …) pass through untouched.
    const isSync2026 = (params: (number | number[])[]) =>
      params.length === 1 && params[0] === 2026
    term.parser.registerCsiHandler({ prefix: '?', final: 'h' }, isSync2026)
    term.parser.registerCsiHandler({ prefix: '?', final: 'l' }, isSync2026)

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

    // Forward keyboard input to PTY. Routed through the sticky-Ctrl latch
    // (spec-082 D QuickKeys) so a Ctrl bar tap followed by an ordinary
    // keystroke — typed on the on-screen keyboard, which has no physical
    // Ctrl key — is translated into its control byte.
    term.onData((data) => {
      const { output, nextArmed } = applyCtrlLatch(ctrlArmedRef.current, data)
      if (ctrlArmedRef.current !== nextArmed) {
        ctrlArmedRef.current = nextArmed
        setCtrlArmed(nextArmed)
      }
      sendToTerminal(output)
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
  }, [sendToTerminal])

  const copyLoginUrl = useCallback(async () => {
    const term = termRef.current
    if (!term) return
    // Prefer the exact string an app copied via OSC 52 (authoritative, never
    // wrap-truncated); fall back to reconstructing the last URL from the buffer.
    const stash = lastClipboardRef.current
    const url = stash && /^https?:\/\//.test(stash) ? stash : findLastUrl(term)
    if (!url) {
      setCopied('none')
      setTimeout(() => setCopied('idle'), 2000)
      return
    }
    try {
      await navigator.clipboard.writeText(url)
      setCopied('ok')
    } catch {
      // Clipboard API may be blocked; fall back to opening the link directly.
      window.open(url, '_blank', 'noopener,noreferrer')
      setCopied('ok')
    }
    setTimeout(() => setCopied('idle'), 2000)
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
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '4px 6px',
          borderBottom: '1px solid #1e1e1e',
          background: '#0d0d0d',
          flexShrink: 0,
        }}
      >
        <button
          onClick={copyLoginUrl}
          style={{
            fontSize: 12,
            padding: '4px 10px',
            borderRadius: 6,
            border: '1px solid #2a2a2a',
            background: copied === 'ok' ? '#1f3a24' : '#161616',
            color: copied === 'ok' ? '#56d364' : '#d4d4d4',
            cursor: 'pointer',
            whiteSpace: 'nowrap',
          }}
        >
          {copied === 'ok' ? '✓ Copied' : copied === 'none' ? 'No link found' : '🔗 Copy login link'}
        </button>
        <span style={{ fontSize: 11, color: '#6e7681' }}>
          Tap a link to open it, or copy the login link here.
        </span>
      </div>
      <div
        ref={containerRef}
        style={{ flex: 1, overflow: 'hidden', padding: '4px 6px' }}
      />
      {isMobile && (
        <div className="terminal-quickkeys" role="toolbar" aria-label={t['quickkeys.bar_label']}>
          {QUICK_KEY_ORDER.map((id) => {
            const armed = id === 'ctrl' && ctrlArmed
            return (
              <button
                key={id}
                type="button"
                className={`terminal-quickkey${armed ? ' armed' : ''}`}
                aria-label={armed ? t['quickkeys.aria_ctrl_armed'] : QUICK_KEY_ARIA[id]}
                aria-pressed={id === 'ctrl' ? ctrlArmed : undefined}
                // onMouseDown + preventDefault (not onClick) so the tap never
                // steals focus from the terminal — stealing focus would close
                // the on-screen keyboard mid-session (see GOTCHAS: the mobile
                // "+" menu focus trap).
                onMouseDown={(e) => {
                  e.preventDefault()
                  handleQuickKey(id)
                }}
              >
                {QUICK_KEY_LABELS[id]}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
