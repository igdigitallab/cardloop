import { useEffect, useRef, useState } from 'react'
import { useLoadStatus, litSegments, SEGMENTS, verdict, type LoadView } from '../lib/loadStatus'

/** spec-094 — vertical LED-bar meter for "how loaded is this server", next to the rate-limit pill.
 *
 *  The bar height is the worst signal's pressure (the zones line up with the verdict: green below
 *  the warn line, amber up to crit, red at it). Hover (desktop) or tap (mobile) opens the detail:
 *  what is wrong, why it matters and what to do about it, and the heaviest chats.
 *
 *  Hidden entirely when the server has the feature off (404). A server that does not answer is
 *  drawn as a hollow red bar — never as the last green reading. */

const LABEL: Record<string, string> = {
  mem: 'Memory', host_mem: 'Host RAM', mem_psi: 'Mem stalls', swap: 'Swap', evictions: 'Evictions',
  agents: 'Agent procs', oom: 'OOM kills', loop_lag: 'Loop lag', fds: 'File descs',
  disk: 'Data disk', tmp: 'Temp dir', cpu: 'CPU',
}

function Detail({ v }: { v: LoadView }) {
  const d = v.data
  const bad = d ? d.signals.filter(s => s.level !== 'ok') : []
  const good = d ? d.signals.filter(s => s.level === 'ok') : []
  const host = d?.host
  const hostLine = host && host.os
    ? [host.os, host.cpus ? `${host.cpus} cores` : '', host.mem_gb ? `${host.mem_gb} GB` : ''].filter(Boolean).join(' · ')
    : ''
  return (
    <>
      <div className={`load-pop-head is-${v.kind}`}>{verdict(v)}</div>
      {v.kind === 'down' && (
        <div className="load-pop-note">
          The cockpit did not answer the last polls. If its process is still up it may be stalled by
          memory pressure — check the journal, or run <code>make doctor</code>.
        </div>
      )}
      {v.kind === 'signedout' && <div className="load-pop-note">Sign in again to see the load.</div>}
      {v.kind === 'stale' && d && (
        <div className="load-pop-note">The numbers below are old; the next poll should refresh them.</div>
      )}
      {d && (
        <div className="load-pop-sub">
          {hostLine}{hostLine && d.chats.max ? ' · ' : ''}{d.chats.max ? `chats live ${d.chats.live}/${d.chats.max}` : ''}
        </div>
      )}
      {bad.map(s => (
        <div key={s.id} className="load-bad">
          <div className={`load-row is-${s.level}`}>
            <span className="load-row-dot" aria-hidden />
            <span className="load-row-text">{s.text}</span>
          </div>
          {s.hint && <div className="load-row-hint">{s.hint}</div>}
        </div>
      ))}
      {good.length > 0 && (
        <>
          <div className="load-sec">Normal</div>
          <div className="load-ok-grid">
            {good.map(s => (
              <span key={s.id} className="load-ok-item">
                <span>{LABEL[s.id] ?? s.id}</span><b>{s.value}</b>
              </span>
            ))}
          </div>
        </>
      )}
      {d && d.top.length > 0 && (
        <>
          <div className="load-sec">Heaviest</div>
          <div className="load-top">
            {d.top.map((t, i) => (
              <span key={i} className="load-ok-item"><span>{t.project}</span><b>{t.rss_mb} MB</b></span>
            ))}
          </div>
        </>
      )}
    </>
  )
}

export function LoadMeter({ compact = false }: { compact?: boolean }) {
  const v = useLoadStatus()
  const [hover, setHover] = useState(false)
  const [pinned, setPinned] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  // Mobile: the meter sits mid-composer, so an anchored panel would run off an edge. Pin the
  // panel full-width just above it (same approach as UsageBadge).
  const [sheetBottom, setSheetBottom] = useState<number | null>(null)
  const open = compact ? pinned : (hover || pinned)

  useEffect(() => {
    if (!compact || !pinned) { setSheetBottom(null); return }
    const measure = () => {
      const r = wrapRef.current?.getBoundingClientRect()
      if (r) setSheetBottom(Math.max(8, window.innerHeight - r.top + 6))
    }
    measure()
    const vv = window.visualViewport
    window.addEventListener('resize', measure)
    vv?.addEventListener('resize', measure)
    vv?.addEventListener('scroll', measure)
    return () => {
      window.removeEventListener('resize', measure)
      vv?.removeEventListener('resize', measure)
      vv?.removeEventListener('scroll', measure)
    }
  }, [compact, pinned])

  useEffect(() => {
    if (!open) return
    const onOut = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) { setPinned(false); setHover(false) }
    }
    document.addEventListener('pointerdown', onOut)
    return () => document.removeEventListener('pointerdown', onOut)
  }, [open])

  if (v.kind === 'off') return null

  const lit = litSegments(v.kind, v.data?.score ?? 0)
  return (
    <div
      className={`load-meter-wrap${compact ? ' load-compact' : ''}`}
      ref={wrapRef}
      onPointerEnter={e => { if (e.pointerType === 'mouse') setHover(true) }}
      onPointerLeave={e => { if (e.pointerType === 'mouse') setHover(false) }}
    >
      <button
        className={`load-meter is-${v.kind}`}
        onClick={() => setPinned(p => !p)}
        aria-label={verdict(v)}
        aria-expanded={open}
      >
        <span className="load-led" aria-hidden>
          {SEGMENTS.map((s, i) => <i key={i} className={`load-seg z-${s.zone}${i < lit ? ' on' : ''}`} />)}
        </span>
      </button>
      {open && (
        <div
          className="load-pop"
          role="dialog"
          aria-label="Server load"
          style={compact && sheetBottom != null
            ? { position: 'fixed', left: 8, right: 8, bottom: sheetBottom, top: 'auto', maxWidth: 'none' }
            : undefined}
        >
          <Detail v={v} />
        </div>
      )}
    </div>
  )
}
