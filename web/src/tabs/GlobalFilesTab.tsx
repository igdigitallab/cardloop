import { useCallback } from 'react'
import { api } from '../api'
import { FileExplorer } from '../components/FileExplorer'

export function GlobalFilesTab() {
  const fetchDir = useCallback((path: string) =>
    api.globalFiles(path), [])

  const fetchFile = useCallback((path: string) =>
    api.globalFile(path), [])

  const onSave = useCallback(async (path: string, content: string) => {
    await api.globalFileWrite(path, content)
  }, [])

  return (
    <FileExplorer
      fetchDir={fetchDir}
      fetchFile={fetchFile}
      onSave={onSave}
      treeLabel={
        <>{'📁 Файлы сервера '}<span className="files-root-hint">~/</span></>
      }
    />
  )
}
