import { useCallback, useRef } from 'react'
import { api } from '../api'
import { FileExplorer } from '../components/FileExplorer'
import { useOnRunEnd } from '../hooks/useProjectActivity'

interface Props {
  projectId: string
  /** spec-079: open this repo-relative path straight away (from a file search hit). */
  openPath?: { path: string; nonce: number } | null
}

export function FilesTab({ projectId, openPath }: Props) {
  const fetchDir = useCallback((path: string) =>
    api.files(projectId, path), [projectId])

  const fetchFile = useCallback((path: string) =>
    api.file(projectId, path), [projectId])

  // Ref populated by FileExplorer — called on run_end to refresh the tree
  const refreshRef = useRef<(() => Promise<void>) | null>(null)
  useOnRunEnd(() => { refreshRef.current?.() })

  return (
    <FileExplorer
      fetchDir={fetchDir}
      fetchFile={fetchFile}
      treeLabel="Project files"
      refreshRef={refreshRef}
      openPath={openPath}
    />
  )
}
