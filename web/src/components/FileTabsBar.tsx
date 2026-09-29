/** The strip of open files above the viewer. */
import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { fileIcon } from '../lib/fileIcons'
import { tabLabels } from '../lib/fsPath'
import { isDirty, OpenFile } from '../lib/filesTabs'

interface Props {
  tabs: OpenFile[]
  active: string | null
  onActivate: (path: string) => void
  onClose: (path: string) => void
}

export function FileTabsBar({ tabs, active, onActivate, onClose }: Props) {
  const stripRef = useRef<HTMLDivElement>(null)
  const labels = tabLabels(tabs.map(t => t.path))

  useEffect(() => {
    stripRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ inline: 'nearest', block: 'nearest' })
  }, [active, tabs.length])

  if (tabs.length === 0) return null
  return (
    <div className="files-tabs" role="tablist" ref={stripRef}>
      {tabs.map((t, i) => {
        const Icon = fileIcon(labels[i].name)
        const selected = t.path === active
        return (
          <div
            key={t.path}
            className={`files-tab${selected ? ' active' : ''}`}
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            title={t.path}
            onClick={() => onActivate(t.path)}
            onAuxClick={e => { if (e.button === 1) { e.preventDefault(); onClose(t.path) } }}
            onMouseDown={e => { if (e.button === 1) e.preventDefault() }}
            onKeyDown={e => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onActivate(t.path) }
              else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                const n = tabs[i + (e.key === 'ArrowRight' ? 1 : -1)]
                if (n) { e.preventDefault(); onActivate(n.path) }
              }
            }}
          >
            <Icon size={13} className="files-tab-icon" aria-hidden="true" />
            <span className="files-tab-name">{labels[i].name}</span>
            {labels[i].hint && <span className="files-tab-hint">{labels[i].hint}</span>}
            {isDirty(t) && <span className="files-tab-dirty" title="Unsaved changes" aria-label="unsaved changes">●</span>}
            <button
              className="files-tab-close"
              aria-label={`Close ${labels[i].name}`}
              title="Close"
              onClick={e => { e.stopPropagation(); onClose(t.path) }}
            >
              <X size={12} />
            </button>
          </div>
        )
      })}
    </div>
  )
}
