import React, { useEffect, useRef, useState } from 'react'
import type { Components } from 'react-markdown'
import { Lightbox } from './Lightbox'
import './markdown.css'

// Shared ReactMarkdown component overrides (ops:mermaid).
// Renders ```mermaid fenced blocks as live diagrams in the browser —
// zero server cost. Mermaid is lazy-imported so it lands in its own chunk
// and never bloats the initial bundle (only loaded when a diagram exists).

let _mermaidReady = false
let _mermaidSeq = 0

function MermaidDiagram({ code }: { code: string }) {
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  const [zoom, setZoom] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const mermaid = (await import('mermaid')).default
        if (!_mermaidReady) {
          mermaid.initialize({
            startOnLoad: false,
            theme: 'dark',
            securityLevel: 'strict',
            fontFamily: 'inherit',
            // Don't inject mermaid's global error "bomb" — on a syntax error we
            // fall back to showing the raw source ourselves (see catch below).
            suppressErrorRendering: true,
          })
          _mermaidReady = true
        }
        const id = 'mmd-' + ++_mermaidSeq
        const out = await mermaid.render(id, code)
        if (!cancelled) {
          setSvg(out.svg)
          setError('')
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [code])

  // On a syntax error fall back to the raw source so nothing is ever lost.
  if (error) {
    return (
      <pre className="mermaid-error">
        {code}
        {'\n\n⚠ ' + error}
      </pre>
    )
  }
  // Tap the diagram or the ⤢ button to open it full-screen with zoom/pan.
  return (
    <div className="mermaid-diagram">
      {svg && (
        <button
          className="mermaid-expand"
          onClick={() => setZoom(true)}
          aria-label="Expand diagram"
          title="Развернуть"
        >
          ⤢
        </button>
      )}
      <div
        className="mermaid-svg"
        onClick={() => svg && setZoom(true)}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      {zoom && <Lightbox svg={svg} onClose={() => setZoom(false)} />}
    </div>
  )
}

// Copy button for fenced code blocks — appears top-right on hover.
// Mermaid blocks are skipped: when the `code` renderer returns <MermaidDiagram>,
// the pre's child type is a function component, not the string 'code'.
function CodeBlockPre({ children, ...props }: React.HTMLAttributes<HTMLPreElement>) {
  const preRef = useRef<HTMLPreElement>(null)
  const [copied, setCopied] = useState(false)

  // Detect mermaid: child.type === 'code' → normal code block; anything else → skip copy
  const isMermaid = (() => {
    const arr = Array.isArray(children) ? children : [children]
    for (const child of arr) {
      if (child && typeof child === 'object' && 'type' in child) {
        if ((child as { type: unknown }).type !== 'code') return true
      }
    }
    return false
  })()

  const handleCopy = async () => {
    const text = preRef.current?.querySelector('code')?.textContent ?? ''
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked */ }
  }

  return (
    <div className="code-block-wrap">
      {!isMermaid && (
        <button
          className={`code-copy-btn${copied ? ' code-copy-btn--ok' : ''}`}
          onClick={handleCopy}
          title="Copy code"
          aria-label="Copy code"
        >
          {copied ? '✓' : 'Copy'}
        </button>
      )}
      <pre ref={preRef} {...props}>{children}</pre>
    </div>
  )
}

export const mdComponents: Components = {
  pre: CodeBlockPre as Components['pre'],
  code({ className, children, ...rest }) {
    const lang = /language-(\w+)/.exec(className || '')?.[1]
    if (lang === 'mermaid') {
      return <MermaidDiagram code={String(children).replace(/\n$/, '')} />
    }
    // `node` is a react-markdown extra prop — strip it so it isn't passed to the DOM.
    delete (rest as Record<string, unknown>).node
    return (
      <code className={className} {...rest}>
        {children}
      </code>
    )
  },
}
