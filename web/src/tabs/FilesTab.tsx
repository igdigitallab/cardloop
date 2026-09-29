import { useMemo, useRef } from 'react'
import { FileExplorer } from '../components/FileExplorer'
import { useFocusRefresh, useOnRunEnd } from '../hooks/useProjectActivity'
import { projectFs } from '../lib/fsAdapter'

interface Props {
  projectId: string
  /** spec-079: open this path straight away (from a file search hit). */
  openPath?: { path: string; nonce: number } | null
}

export function FilesTab({ projectId, openPath }: Props) {
  const fs = useMemo(() => projectFs(projectId), [projectId])

  // Ref populated by FileExplorer — called on run_end and on window focus, so a file the
  // agent just rewrote does not sit stale in an open tab.
  const refreshRef = useRef<(() => Promise<void>) | null>(null)
  useOnRunEnd(() => { refreshRef.current?.() })
  useFocusRefresh(() => { refreshRef.current?.() })

  return <FileExplorer fs={fs} refreshRef={refreshRef} openPath={openPath} />
}
