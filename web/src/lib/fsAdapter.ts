/**
 * What the Files explorer needs from the server, behind one interface, so the project tab and
 * the Server-files tab differ only in which adapter they hand FileExplorer.
 */
import { api } from '../api'
import type { FsFile, FsInfo, FsListing, FsStat } from '../types'

export interface FsAdapter {
  /** Persistence namespace: open tabs and root are remembered per scope. */
  scope: string
  info(): Promise<FsInfo>
  list(path: string): Promise<FsListing>
  read(path: string): Promise<FsFile>
  /** Absent = the explorer is read-only. */
  write?(path: string, content: string, baseRev: string | null, force?: boolean): Promise<{ rev: string; size: number }>
  stat(text: string, base?: string): Promise<FsStat>
}

/** A project's explorer: starts in its cwd, can climb to $HOME. */
export function projectFs(projectId: string): FsAdapter {
  return {
    scope: `project:${projectId}`,
    info: () => api.fsInfo(projectId),
    list: p => api.fsList(p, projectId),
    read: p => api.fsFile(p, projectId),
    write: (p, c, rev, force) => api.fsWrite(p, c, rev, projectId, force),
    stat: (t, base) => api.fsStat(t, base, projectId),
  }
}

/** The server-wide explorer: starts at $HOME. */
export function serverFs(): FsAdapter {
  return {
    scope: 'server',
    info: () => api.fsInfo(),
    list: p => api.fsList(p),
    read: p => api.fsFile(p),
    write: (p, c, rev, force) => api.fsWrite(p, c, rev, undefined, force),
    stat: (t, base) => api.fsStat(t, base),
  }
}
