/**
 * Unsaved editor text that must outlive its tab: switching to another project tab unmounts the
 * Files tab, and a reload or crash takes the page. Pure helpers; FileExplorer owns the timing.
 * Tests: filesDrafts.test.ts (run command in its header).
 */
import { isDirty, OpenFile } from './filesTabs'

export interface StoredDraft {
  draft: string
  /** The on-disk revision the draft was typed against. */
  rev: string
}
export type DraftMap = Record<string, StoredDraft>

/** localStorage holds ~5 MB for the whole origin; one runaway draft must not starve the rest. */
export const MAX_DRAFT_CHARS = 400_000

export function parseDrafts(raw: string | null): DraftMap {
  if (!raw) return {}
  try {
    const obj = JSON.parse(raw) as Record<string, Partial<StoredDraft>>
    const out: DraftMap = {}
    for (const [path, d] of Object.entries(obj ?? {})) {
      if (path.startsWith('/') && d && typeof d.draft === 'string' && typeof d.rev === 'string') {
        out[path] = { draft: d.draft, rev: d.rev }
      }
    }
    return out
  } catch {
    return {}
  }
}

/** The drafts worth keeping: tabs with real unsaved edits, within the size cap. */
export function collectDrafts(tabs: OpenFile[]): DraftMap {
  const out: DraftMap = {}
  for (const t of tabs) {
    if (isDirty(t) && t.draft !== null && t.draft.length <= MAX_DRAFT_CHARS) {
      out[t.path] = { draft: t.draft, rev: t.rev }
    }
  }
  return out
}
