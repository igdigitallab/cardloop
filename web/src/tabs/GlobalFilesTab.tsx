import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { FileContent, FileEntry } from '../types'
import { Spinner } from '../components/Spinner'

interface TreeNode {
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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

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

export function GlobalFilesTab() {
  const [rootNodes, setRootNodes] = useState<TreeNode[] | null>(null)
  const [rootLoading, setRootLoading] = useState(true)
  const [rootError, setRootError] = useState('')
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState<FileContent | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const nodesRef = useRef<TreeNode[] | null>(null)

  useEffect(() => {
    setRootLoading(true)
    setRootError('')
    setRootNodes(null)
    setSelectedPath(null)
    setFileContent(null)
    nodesRef.current = null

    api.globalFiles('').then(d => {
      const nodes = buildNodes(d.entries, '', 0)
      nodesRef.current = nodes
      setRootNodes([...nodes])
      setRootLoading(false)
    }).catch(e => {
      setRootError(String(e.message || e))
      setRootLoading(false)
    })
  }, [])

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

  const handleDirToggle = useCallback((node: TreeNode) => {
    if (!nodesRef.current) return
    if (node.children !== undefined) {
      mutateNode(nodesRef.current, node.path, n => { n.open = !n.open })
      forceUpdate()
      return
    }
    mutateNode(nodesRef.current, node.path, n => { n.open = true; n.loading = true })
    forceUpdate()

    api.globalFiles(node.path).then(d => {
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
        n.loadError = String(e.message || e)
        n.open = false
      })
      forceUpdate()
    })
  }, [])

  function handleFileClick(node: TreeNode) {
    if (selectedPath === node.path) return
    setSelectedPath(node.path)
    setFileContent(null)
    setFileLoading(true)

    api.globalFile(node.path).then(d => {
      setFileContent(d)
      setFileLoading(false)
    }).catch(e => {
      setFileContent({ path: node.path, content: '', lang: '', size: 0, error: String(e.message || e) })
      setFileLoading(false)
    })
  }

  if (rootLoading) return <Spinner label="Загрузка файлов сервера..." />
  if (rootError) return <div className="error-state">⚠ {rootError}</div>
  if (!rootNodes) return null

  return (
    <div className="files-layout">
      <div className="files-tree-pane">
        <div className="files-tree-label">
          📁 Файлы сервера <span className="files-root-hint">~/</span>
        </div>
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

      <div className="files-viewer-pane">
        {!selectedPath && (
          <div className="no-content files-viewer-hint">← Выберите файл</div>
        )}
        {selectedPath && fileLoading && <Spinner label="Загрузка..." />}
        {selectedPath && !fileLoading && fileContent && (
          <>
            <div className="files-viewer-header">
              <span className="files-viewer-path">~/{fileContent.path}</span>
              {fileContent.size > 0 && (
                <span className="files-viewer-size">{formatSize(fileContent.size)}</span>
              )}
            </div>
            <div className="files-viewer-body">
              {fileContent.error ? (
                <div className="error-state">⚠ {fileContent.error}</div>
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
