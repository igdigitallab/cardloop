/**
 * Shared file explorer: tree + viewer/editor.
 * Used by FilesTab (project files, read-only) and GlobalFilesTab (server files, editable).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { FileContent, FileEntry, FileListing } from '../types'
import { Spinner } from './Spinner'

// ─── Tree types ───────────────────────────────────────────────────────────────

export interface TreeNode {
  name: string
  type: 'dir' | 'file'
  size: number
  path: string
  depth: number
  open?: boolean
  children?: TreeNode[]
  loading?: boolean
  loadError?: string
}

function buildNodes(entries: FileEntry[], parentPath: string, depth: number): TreeNode[] {
  return entries.map(e => ({
    name: e.name,
    type: e.type,
    size: e.size,
    path: parentPath ? `${parentPath}/${e.name}` : e.name,
    depth,
  }))
}

function findByPath(nodes: TreeNode[] | null, path: string): TreeNode | null {
  if (!nodes) return null
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.children) {
      const r = findByPath(n.children, path)
      if (r) return r
    }
  }
  return null
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ─── TreeView ─────────────────────────────────────────────────────────────────

interface TreeProps {
  nodes: TreeNode[]
  selectedPath: string | null
  onFileClick: (node: TreeNode) => void
  onDirToggle: (node: TreeNode) => void
}

function TreeView({ nodes, selectedPath, onFileClick, onDirToggle }: TreeProps) {
  return (
    <>
      {nodes.map(node => (
        <div key={node.path}>
          <div
            className={`file-tree-row ${selectedPath === node.path ? 'active' : ''}`}
            style={{ paddingLeft: `${8 + node.depth * 14}px` }}
            title={node.path}
            onClick={() => node.type === 'dir' ? onDirToggle(node) : onFileClick(node)}
          >
            {node.type === 'dir' ? (
              <>
                <span className="file-tree-caret">{node.open ? '▾' : '▸'}</span>
                <span className="file-tree-icon">📁</span>
              </>
            ) : (
              <>
                <span className="file-tree-caret" />
                <span className="file-tree-icon">📄</span>
              </>
            )}
            <span className="file-tree-name">{node.name}</span>
            {node.loading && <span className="file-tree-spinner">…</span>}
            {node.loadError && <span className="file-tree-err" title={node.loadError}>⚠</span>}
          </div>
          {node.open && node.children && (
            <TreeView
              nodes={node.children}
              selectedPath={selectedPath}
              onFileClick={onFileClick}
              onDirToggle={onDirToggle}
            />
          )}
        </div>
      ))}
    </>
  )
}

// ─── FileExplorer props ───────────────────────────────────────────────────────

export interface FileExplorerProps {
  /** Fetch directory listing */
  fetchDir: (path: string) => Promise<FileListing>
  /** Fetch file content */
  fetchFile: (path: string) => Promise<FileContent>
  /** If provided, the viewer becomes editable */
  onSave?: (path: string, content: string) => Promise<void>
  /** Label shown above the tree (string or JSX) */
  treeLabel?: React.ReactNode
  /**
   * If provided, a ref that FileExplorer will populate with its refreshTree function.
   * Callers can then invoke it imperatively (e.g. on run_end).
   */
  refreshRef?: React.MutableRefObject<(() => Promise<void>) | null>
}

// ─── FileExplorer ─────────────────────────────────────────────────────────────

export function FileExplorer({
  fetchDir,
  fetchFile,
  onSave,
  treeLabel = 'Файлы',
  refreshRef,
}: FileExplorerProps) {
  const [rootNodes, setRootNodes] = useState<TreeNode[] | null>(null)
  const [rootLoading, setRootLoading] = useState(true)
  const [rootError, setRootError] = useState('')

  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState<FileContent | null>(null)
  const [fileLoading, setFileLoading] = useState(false)

  // Inline editing (only when onSave is provided)
  const [editing, setEditing] = useState(false)
  const [editContent, setEditContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  const nodesRef = useRef<TreeNode[] | null>(null)

  // ── Load root ─────────────────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false
    setRootLoading(true)
    setRootError('')
    setRootNodes(null)
    setSelectedPath(null)
    setFileContent(null)
    nodesRef.current = null
    setEditing(false)

    fetchDir('').then(d => {
      if (cancelled) return
      const nodes = buildNodes(d.entries, '', 0)
      nodesRef.current = nodes
      setRootNodes([...nodes])
      setRootLoading(false)
    }).catch(e => {
      if (cancelled) return
      setRootError(e instanceof Error ? e.message : String(e))
      setRootLoading(false)
    })

    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchDir])

  // ── Mutable tree helpers ──────────────────────────────────────────────────

  function mutateNode(nodes: TreeNode[], targetPath: string, mutate: (n: TreeNode) => void): boolean {
    for (const n of nodes) {
      if (n.path === targetPath) { mutate(n); return true }
      if (n.type === 'dir' && n.children && mutateNode(n.children, targetPath, mutate)) return true
    }
    return false
  }

  function forceUpdate() {
    if (nodesRef.current) setRootNodes([...nodesRef.current])
  }

  // ── Dir toggle ────────────────────────────────────────────────────────────

  const handleDirToggle = useCallback((node: TreeNode) => {
    if (!nodesRef.current) return
    if (node.children !== undefined) {
      mutateNode(nodesRef.current, node.path, n => { n.open = !n.open })
      forceUpdate()
      return
    }
    mutateNode(nodesRef.current, node.path, n => { n.open = true; n.loading = true })
    forceUpdate()

    fetchDir(node.path).then(d => {
      if (!nodesRef.current) return
      mutateNode(nodesRef.current, node.path, n => {
        n.loading = false
        n.loadError = undefined
        n.children = buildNodes(d.entries, node.path, node.depth + 1)
      })
      forceUpdate()
    }).catch(e => {
      if (!nodesRef.current) return
      mutateNode(nodesRef.current, node.path, n => {
        n.loading = false
        n.loadError = e instanceof Error ? e.message : String(e)
        n.open = false
      })
      forceUpdate()
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchDir])

  // ── Refresh tree (called on run_end / manual) ─────────────────────────────

  const refreshTree = useCallback(async () => {
    if (!nodesRef.current) return
    try {
      const root = await fetchDir('')
      const oldNodes = nodesRef.current
      const newRoot = buildNodes(root.entries, '', 0)
      const merge = async (newOnes: TreeNode[]) => {
        for (const n of newOnes) {
          const old = findByPath(oldNodes, n.path)
          if (old && old.type === 'dir' && old.open) {
            n.open = true
            try {
              const sub = await fetchDir(n.path)
              n.children = buildNodes(sub.entries, n.path, n.depth + 1)
              await merge(n.children)
            } catch { /* skip */ }
          }
        }
      }
      await merge(newRoot)
      nodesRef.current = newRoot
      setRootNodes([...newRoot])
    } catch { /* тихо игнорим */ }

    if (selectedPath) {
      try {
        const d = await fetchFile(selectedPath)
        setFileContent(d)
      } catch { /* skip */ }
    }
  }, [fetchDir, fetchFile, selectedPath])

  // Expose refreshTree via external ref so parent can trigger it (e.g. on run_end)
  const refreshTreeRef = useRef(refreshTree)
  useEffect(() => {
    refreshTreeRef.current = refreshTree
    if (refreshRef) refreshRef.current = refreshTree
  }, [refreshTree, refreshRef])

  // ── File click ────────────────────────────────────────────────────────────

  function handleFileClick(node: TreeNode) {
    if (selectedPath === node.path) return
    setEditing(false)
    setSaveError('')
    setSelectedPath(node.path)
    setFileContent(null)
    setFileLoading(true)

    fetchFile(node.path).then(d => {
      setFileContent(d)
      setFileLoading(false)
    }).catch(e => {
      setFileContent({ path: node.path, content: '', lang: '', size: 0, error: e instanceof Error ? e.message : String(e) })
      setFileLoading(false)
    })
  }

  // ── Edit / save ───────────────────────────────────────────────────────────

  function handleStartEdit() {
    if (!fileContent || fileContent.error || !onSave) return
    setEditContent(fileContent.content)
    setSaveError('')
    setEditing(true)
  }

  function handleCancelEdit() {
    setEditing(false)
    setSaveError('')
  }

  async function handleSave() {
    if (!selectedPath || !fileContent || !onSave) return
    setSaving(true)
    setSaveError('')
    try {
      await onSave(selectedPath, editContent)
      setFileContent({ ...fileContent, content: editContent })
      setEditing(false)
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Ошибка сохранения')
    } finally {
      setSaving(false)
    }
  }

  // ─── Render ───────────────────────────────────────────────────────────────

  if (rootLoading) return <Spinner label="Загрузка файлов..." />
  if (rootError) return <div className="error-state">⚠ {rootError}</div>
  if (!rootNodes) return null

  return (
    <div className="files-layout">
      {/* Left: file tree */}
      <div className="files-tree-pane">
        <div className="files-tree-label">{treeLabel}</div>
        <div className="files-tree-scroll">
          {rootNodes.length === 0 ? (
            <div className="no-content">Директория пуста</div>
          ) : (
            <TreeView
              nodes={rootNodes}
              selectedPath={selectedPath}
              onFileClick={handleFileClick}
              onDirToggle={handleDirToggle}
            />
          )}
        </div>
      </div>

      {/* Right: file viewer / editor */}
      <div className="files-viewer-pane">
        {!selectedPath && (
          <div className="no-content files-viewer-hint">← Выберите файл</div>
        )}

        {selectedPath && fileLoading && <Spinner label="Загрузка..." />}

        {selectedPath && !fileLoading && fileContent && (
          <>
            <div className="files-viewer-header">
              <span className="files-viewer-path">{fileContent.path}</span>
              {fileContent.size > 0 && (
                <span className="files-viewer-size">{formatSize(fileContent.size)}</span>
              )}
              {onSave && !fileContent.error && !editing && (
                <button className="file-edit-btn" onClick={handleStartEdit} title="Редактировать (или двойной клик на тексте)">
                  ✎ Изменить
                </button>
              )}
              {editing && (
                <div className="file-edit-actions">
                  {saveError && <span className="file-edit-err">⚠ {saveError}</span>}
                  <button className="btn-primary file-save-btn" onClick={handleSave} disabled={saving}>
                    {saving ? '…' : 'Сохранить'}
                  </button>
                  <button className="btn-secondary" onClick={handleCancelEdit} disabled={saving}>Отмена</button>
                </div>
              )}
            </div>
            <div
              className={`files-viewer-body${editing ? ' files-viewer-editing' : ''}`}
              onDoubleClick={onSave && !editing && !fileContent.error ? handleStartEdit : undefined}
              title={onSave && !editing && !fileContent.error ? 'Двойной клик для редактирования' : undefined}
            >
              {fileContent.error ? (
                <div className="error-state">⚠ {fileContent.error}</div>
              ) : editing ? (
                <textarea
                  className="file-edit-textarea"
                  value={editContent}
                  onChange={e => setEditContent(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Escape') handleCancelEdit()
                    if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); handleSave() }
                  }}
                  autoFocus
                  spellCheck={false}
                />
              ) : fileContent.lang === 'md' ? (
                <div className="markdown-wrap">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{fileContent.content}</ReactMarkdown>
                </div>
              ) : (
                <pre className="files-code-block"><code>{fileContent.content}</code></pre>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
