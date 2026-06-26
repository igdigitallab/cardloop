/**
 * Rich terminal-style rendering of a single tool call.
 */
import { useState } from 'react'
import { RichTool } from '../types'

export function ToolBlock({ tool }: { tool: RichTool }) {
  const [expanded, setExpanded] = useState(false)

  if (tool.kind === 'bash') {
    return (
      <div className="chat-tool-row chat-tool-bash">
        <span className="chat-tool-icon">$</span>
        <div className="chat-tool-bash-body">
          <pre className="chat-tool-cmd">{tool.cmd}</pre>
          {tool.desc && <span className="chat-tool-desc">{tool.desc}</span>}
        </div>
      </div>
    )
  }

  if (tool.kind === 'edit') {
    const hasOldNew = 'old' in tool && 'new' in tool
    const count = 'count' in tool ? tool.count : undefined
    return (
      <div className="chat-tool-row chat-tool-edit">
        <span className="chat-tool-icon">✏</span>
        <div className="chat-tool-edit-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-file">{tool.file}</span>
            {count !== undefined && (
              <span className="chat-tool-desc">{count} edit{count === 1 ? '' : 's'}</span>
            )}
            {'cell_type' in tool && tool.cell_type && (
              <span className="chat-tool-desc">cell: {tool.cell_type}</span>
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
    return (
      <div className="chat-tool-row chat-tool-write">
        <span className="chat-tool-icon">📝</span>
        <div className="chat-tool-write-body">
          <div className="chat-tool-edit-line">
            <span className="chat-tool-file">{tool.file}</span>
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
    return (
      <div className="chat-tool-row chat-tool-read">
        <span className="chat-tool-icon">📖</span>
        <span className="chat-tool-file">{tool.file}</span>
      </div>
    )
  }

  if (tool.kind === 'search') {
    return (
      <div className="chat-tool-row chat-tool-search">
        <span className="chat-tool-icon">🔍</span>
        <span className="chat-tool-name">{tool.name}</span>
        <span className="chat-tool-pattern">{tool.pattern}</span>
        {tool.path && <span className="chat-tool-desc">{tool.path}</span>}
      </div>
    )
  }

  // other / fallback
  return (
    <div className="chat-tool-row chat-tool-other">
      <span className="chat-tool-icon">⚙</span>
      <span className="chat-tool-name">{tool.name}</span>
      {tool.summary && <span className="chat-tool-input">{tool.summary}</span>}
    </div>
  )
}
