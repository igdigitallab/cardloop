import { useMemo } from 'react'
import { FileExplorer } from '../components/FileExplorer'
import { serverFs } from '../lib/fsAdapter'

export function GlobalFilesTab() {
  const fs = useMemo(() => serverFs(), [])
  return <FileExplorer fs={fs} />
}
