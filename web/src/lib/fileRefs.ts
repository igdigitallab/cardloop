/**
 * Spotting file paths in agent messages, so the chat can make them clickable (open in the
 * Files tab). Pure string logic; the React side lives in ChatTab.
 * Tests: fileRefs.test.ts (run command in its header).
 *
 * Two detectors with different appetite:
 *  - inline `code` spans are what an agent puts a path in on purpose, so they may be relative
 *    ("docs/plan.md", "webapp.py") as long as the extension is a known file type;
 *  - loose text is prose, so only absolute-looking paths under the usual roots qualify —
 *    "and/or" and "1/2" must never turn into links.
 * A false positive costs one click that says "not found"; the explorer resolves and verifies.
 */

const EXT = new Set([
  'md', 'mdx', 'txt', 'rst', 'log', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml', 'toml', 'ini', 'env', 'xml',
  'py', 'js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'css', 'scss', 'html', 'htm', 'sh', 'bash', 'sql', 'go', 'rs',
  'java', 'kt', 'c', 'h', 'cpp', 'hpp', 'rb', 'php', 'lock', 'conf', 'cfg',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg', 'bmp', 'ico', 'pdf',
  'mp4', 'webm', 'mov', 'mp3', 'wav', 'ogg', 'm4a', 'zip', 'tar', 'gz', 'docx', 'xlsx', 'pptx',
])

/** Trailing `:12` / `:12:5` / `#L12` that agents append. */
const LINE_SUFFIX = /(?::\d+){1,2}$|#L\d+(?:-L?\d+)?$/

export function stripLineSuffix(text: string): string {
  return text.replace(LINE_SUFFIX, '')
}

/** Is this inline-code text a file reference worth linking? */
export function looksLikeFileRef(text: string): boolean {
  const t = stripLineSuffix(text.trim())
  if (!t || t.length > 300 || /\s/.test(t)) return false
  if (/[*?<>{}()|;$=&,"'`\\]/.test(t) || t.includes('://') || t.startsWith('-') || t.startsWith('@')) return false
  const name = t.slice(t.lastIndexOf('/') + 1)
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return false  // no extension, or a dotfile with none
  return EXT.has(name.slice(dot + 1).toLowerCase())
}

/** Absolute-looking paths inside running text. */
const LOOSE = /(?<![\w/.~-])(~\/|\/(?:home|tmp|var|opt|srv|mnt|usr|etc|root|data)\/)[^\s`'"<>()[\]{},;]*[^\s`'"<>()[\]{},;.:!?]/g

export interface PathPiece { text: string; path?: string }

/** Split prose into plain pieces and path pieces (`path` set). */
export function splitLoosePaths(text: string): PathPiece[] {
  const out: PathPiece[] = []
  let last = 0
  for (const m of text.matchAll(LOOSE)) {
    const start = m.index ?? 0
    if (start > last) out.push({ text: text.slice(last, start) })
    out.push({ text: m[0], path: m[0] })
    last = start + m[0].length
  }
  if (last < text.length) out.push({ text: text.slice(last) })
  return out
}

// ── remark plugin ─────────────────────────────────────────────────────────────

/** URL fragment the plugin puts on the links it creates; ChatTab's `a` renderer recognises it. */
export const FILE_REF_PREFIX = '#cardloop-file='

interface MdNode {
  type: string
  value?: string
  url?: string
  children?: MdNode[]
}

const SKIP = new Set(['link', 'linkReference', 'inlineCode', 'code', 'image', 'imageReference', 'html'])

function transform(node: MdNode): void {
  if (!node.children) return
  const next: MdNode[] = []
  for (const child of node.children) {
    if (child.type === 'text' && typeof child.value === 'string') {
      const pieces = splitLoosePaths(child.value)
      if (pieces.length === 1 && !pieces[0].path) { next.push(child); continue }
      for (const p of pieces) {
        if (p.path) {
          next.push({ type: 'link', url: FILE_REF_PREFIX + encodeURIComponent(p.path), children: [{ type: 'text', value: p.text }] })
        } else {
          next.push({ type: 'text', value: p.text })
        }
      }
    } else {
      if (!SKIP.has(child.type)) transform(child)
      next.push(child)
    }
  }
  node.children = next
}

/** remark plugin: turn loose absolute paths in prose into `#cardloop-file=` links. */
export function remarkFileRefs() {
  return (tree: MdNode) => transform(tree)
}
