// What a chat message renders as: the markdown body, the file cards pulled out of its
// `attached file:` lines, and a trailing ```options picker. Pure (no DOM, no React) so the
// way these three compose is testable — see messageLayout.test.ts.
import { parseOptionsBlock, type ParsedOptions } from '../components/OptionPicker'
import type { ChatMessage } from '../types'

const _IMG_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|ico|svg)$/i

export function isVideoSrc(src: string): boolean {
  const ext = src.split('?')[0].split('.').pop()?.toLowerCase() ?? ''
  return ['mp4', 'webm', 'mov', 'ogg', 'ogv'].includes(ext)
}

export interface ParsedAttachment {
  url: string
  name: string
  kind: 'image' | 'video' | 'file'
}

/** Split a user prompt into prose + uploaded-file references.
 *  The composer appends "attached file: <abs path>" lines on send; those lines persist
 *  verbatim in the transcript, so this also reconstructs inline previews after a reload.
 *  The servable URL is derived from the basename — the upload-serve route reads DATA/inbox
 *  by filename (project id only gates auth), so the current projectId is fine. */
export function parseAttachedFiles(text: string, projectId: string): { body: string; files: ParsedAttachment[] } {
  const files: ParsedAttachment[] = []
  const bodyLines: string[] = []
  // A file line inside a fenced code block is an example someone is showing, not a drop.
  let fence: string | null = null
  for (const line of text.split('\n')) {
    const f = /^\s{0,3}(`{3,}|~{3,})/.exec(line)
    if (f) {
      if (fence === null) fence = f[1]
      else if (line.trim() === f[1] && f[1][0] === fence[0] && f[1].length >= fence.length) fence = null
      bodyLines.push(line)
      continue
    }
    // Leading whitespace is tolerated: a model sometimes indents the line under a list item.
    const m = fence === null ? /^\s*attached file:\s*(.+\S)\s*$/.exec(line) : null
    if (m) {
      const raw = m[1]
      // Two marker shapes share this line:
      //  • a ready URL path — agent file-drop (cockpit-file / cockpit-img) prints
      //    "/api/projects/<id>/media/<f>"; use it verbatim.
      //  • an absolute fs path — operator uploads append the local path; its basename maps
      //    to the /upload/ serve route (reads DATA/inbox by filename; project id gates auth).
      // A servable URL is an /api/ route (agent file-drop prints /api/projects/<id>/media/<f>)
      // or an http(s) URL. An operator upload appends an ABSOLUTE fs path (/home/.../data/inbox/…)
      // which ALSO starts with '/', so we must match '/api/' — NOT a bare leading slash — otherwise
      // the abs path is used verbatim as <img src> and 404s (the "broken image" bug).
      const isUrlPath = raw.startsWith('/api/') || /^https?:\/\//i.test(raw)
      const seg = raw.split(/[/\\?#]/).filter(Boolean).pop() || raw
      let name = seg
      try { name = decodeURIComponent(seg) } catch { /* keep raw segment */ }
      // Stored media files are prefixed "<unix_ts>_" for uniqueness — hide it in the label.
      const display = name.replace(/^\d{9,}_/, '')
      const url = isUrlPath
        ? raw
        : `/api/projects/${encodeURIComponent(projectId)}/upload/${encodeURIComponent(seg)}`
      const kind = isVideoSrc(name) ? 'video' : _IMG_EXT_RE.test(name) ? 'image' : 'file'
      files.push({ url, name: display, kind })
    } else {
      bodyLines.push(line)
    }
  }
  return { body: bodyLines.join('\n').trim(), files }
}

export interface MessageLayout {
  /** Markdown to render; may be empty. */
  body: string
  files: ParsedAttachment[]
  /** The trailing picker's choices, or null when the message has none. */
  options: ParsedOptions['options'] | null
}

/** A streaming assistant message is shown as-is: a half-written fence or file line would
 *  flicker between shapes. */
export function layoutMessage(
  text: string, role: ChatMessage['role'], streaming: boolean, projectId: string,
): MessageLayout {
  const parsedOpts = role === 'assistant' && text && !streaming ? parseOptionsBlock(text) : null
  const options = parsedOpts ? parsedOpts.options : null
  // Files are pulled from the text BEFORE the picker: an agent that drops a file usually
  // ends the same reply with a picker (135 of 260 such replies, 2026-10-13), and parsing
  // only one of the two left the file lines as dead text.
  const prose = parsedOpts ? parsedOpts.prefix : text
  const attach = text && (role === 'user' || (role === 'assistant' && !streaming))
    ? parseAttachedFiles(prose, projectId)
    : null
  if (attach && attach.files.length > 0) return { body: attach.body, files: attach.files, options }
  return { body: prose, files: [], options }
}
