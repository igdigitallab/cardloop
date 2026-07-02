/**
 * Rich terminal-style rendering of a single tool call.
 * Renders file ops as: <verb> <file-type icon> <basename>
 * matching a ChatGPT-style compact row aesthetic.
 */
import { useState } from 'react'
import { Search, Terminal, Wrench } from 'lucide-react'
import { RichTool } from '../types'
import { fileIcon, basename } from '../lib/fileIcons'

export function ToolBlock({ tool }: { tool: RichTool }) {
  const [expanded, setExpanded] = useState(false)

  if (tool.kind === 'bash') {
    return (
      <div className="chat-tool-row chat-tool-bash">
        <span className="chat-tool-icon chat-tool-lucide"><Terminal size={12} /></span>
        <div className="chat-tool-bash-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-verb">Ran</span>
            <pre className="chat-tool-cmd">{tool.cmd}</pre>
          </div>
          {tool.desc && <span className="chat-tool-desc">{tool.desc}</span>}
        </div>
      </div>
    )
  }

  if (tool.kind === 'edit') {
    const hasOldNew = 'old' in tool && 'new' in tool
    const count = 'count' in tool ? tool.count : undefined
    const cellType = 'cell_type' in tool ? tool.cell_type : undefined
    const FileIcon = fileIcon(tool.file)
    const name = basename(tool.file)
    return (
      <div className="chat-tool-row chat-tool-edit">
        <span className="chat-tool-icon chat-tool-lucide"><FileIcon size={12} /></span>
        <div className="chat-tool-edit-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-verb">Edited</span>
            <span className="chat-tool-file" title={tool.file}>{name}</span>
            {count !== undefined && count > 1 && (
              <span className="chat-tool-desc">×{count}</span>
            )}
            {cellType && (
              <span className="chat-tool-desc">{cellType}</span>
            )}
            {hasOldNew && (
              <button
                className="chat-tool-expand-btn chat-tool-expand-inline"
                onClick={() => setExpanded(e => !e)}
              >{expanded ? '▲ hide' : '▼ diff'}</button>
            )}
          </div>
          {hasOldNew && expanded && (
            <div className="chat-tool-diff">
              {tool.old && (
                <pre className="chat-tool-diff-old">- {tool.old}</pre>
              )}
              {tool.new && (
                <pre className="chat-tool-diff-new">+ {tool.new}</pre>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  if (tool.kind === 'write') {
    const FileIcon = fileIcon(tool.file)
    const name = basename(tool.file)
    return (
      <div className="chat-tool-row chat-tool-write">
        <span className="chat-tool-icon chat-tool-lucide"><FileIcon size={12} /></span>
        <div className="chat-tool-write-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-verb">Wrote</span>
            <span className="chat-tool-file" title={tool.file}>{name}</span>
            {tool.preview && (
              <button
                className="chat-tool-expand-btn chat-tool-expand-inline"
                onClick={() => setExpanded(e => !e)}
              >{expanded ? '▲ hide' : '▼ contents'}</button>
            )}
          </div>
          {expanded && tool.preview && (
            <pre className="chat-tool-preview">{tool.preview}</pre>
          )}
        </div>
      </div>
    )
  }

  if (tool.kind === 'read') {
    const FileIcon = fileIcon(tool.file)
    const name = basename(tool.file)
    return (
      <div className="chat-tool-row chat-tool-read">
        <span className="chat-tool-icon chat-tool-lucide"><FileIcon size={12} /></span>
        <span className="chat-tool-verb">Read</span>
        <span className="chat-tool-file chat-tool-file-read" title={tool.file}>{name}</span>
      </div>
    )
  }

  if (tool.kind === 'search') {
    return (
      <div className="chat-tool-row chat-tool-search">
        <span className="chat-tool-icon chat-tool-lucide"><Search size={12} /></span>
        <span className="chat-tool-verb">Searched</span>
        <span className="chat-tool-pattern">{tool.pattern}</span>
        {tool.path && <span className="chat-tool-desc">{tool.path}</span>}
      </div>
    )
  }

  // other / fallback
  return (
    <div className="chat-tool-row chat-tool-other">
      <span className="chat-tool-icon chat-tool-lucide"><Wrench size={12} /></span>
      <span className="chat-tool-verb">{tool.name}</span>
      {tool.summary && <span className="chat-tool-input">{tool.summary}</span>}
    </div>
  )
}
