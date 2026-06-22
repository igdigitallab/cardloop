import { useEffect, useState } from 'react'
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

export const mdComponents: Components = {
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
