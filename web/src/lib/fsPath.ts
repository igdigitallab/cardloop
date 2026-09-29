/**
 * Pure helpers for the Files explorer's absolute POSIX paths. No I/O, no React.
 * Tests: fsPath.test.ts (run command in its header).
 */

export function dirname(p: string): string {
  if (p === '/' || !p.includes('/')) return '/'
  const i = p.lastIndexOf('/')
  return i <= 0 ? '/' : p.slice(0, i)
}

export function baseName(p: string): string {
  if (p === '/') return '/'
  return p.slice(p.lastIndexOf('/') + 1)
}

export function joinPath(dir: string, name: string): string {
  return dir === '/' ? `/${name}` : `${dir}/${name}`
}

/** True when `path` is `dir` itself or lives below it. */
export function isUnder(path: string, dir: string): boolean {
  if (dir === '/') return path.startsWith('/')
  return path === dir || path.startsWith(`${dir}/`)
}

/** Directories to open so `file` becomes visible below `root`: root excluded, dirname(file) included. */
export function ancestorsBetween(root: string, file: string): string[] {
  if (!isUnder(file, root) || file === root) return []
  const rel = file.slice(root === '/' ? 1 : root.length + 1).split('/')
  rel.pop() // the file (or the leaf directory) itself is not an ancestor
  const out: string[] = []
  let cur = root
  for (const seg of rel) {
    cur = joinPath(cur, seg)
    out.push(cur)
  }
  return out
}

/** `/home/igor/x` -> `~/x` when it lies under home. */
export function displayPath(p: string, home: string): string {
  if (p === home) return '~'
  if (home && isUnder(p, home)) return `~${p.slice(home.length)}`
  return p
}

/**
 * Tab label. When two open tabs share a file name, the parent folder disambiguates them.
 * Returns {name, hint}; hint is '' when the name is unique.
 */
export function tabLabels(paths: string[]): { name: string; hint: string }[] {
  const counts = new Map<string, number>()
  for (const p of paths) counts.set(baseName(p), (counts.get(baseName(p)) ?? 0) + 1)
  return paths.map(p => {
    const name = baseName(p)
    return { name, hint: (counts.get(name) ?? 0) > 1 ? baseName(dirname(p)) : '' }
  })
}

/**
 * Does pasted text look like a filesystem path the operator wants to jump to? Used to turn a
 * bare Ctrl+V inside the explorer into navigation, so it must not fire on ordinary prose:
 * only a first line that STARTS like a path (optionally wrapped in quotes/backticks/parens).
 */
export function looksLikePath(text: string): boolean {
  const first = (text || '').split('\n').find(l => l.trim()) ?? ''
  const t = first.trim().replace(/^[\s`"'(<[]+/, '')
  if (!t || /\s{2,}/.test(t)) return false
  return /^(\/[^\s/]|~(\/|$)|\$HOME(\/|$)|file:\/\/\/)/.test(t)
}

/** Resolve `rel` against directory `dir` ("../x.png", "./a/b", "a//b") into an absolute path. */
export function resolveRelative(dir: string, rel: string): string {
  const out: string[] = []
  for (const seg of `${dir}/${rel}`.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') out.pop()
    else out.push(seg)
  }
  return `/${out.join('/')}`
}

/** "3m", "2h", "5d" — how long ago `epochSec` was, as of `nowMs`. */
export function formatAgo(epochSec: number, nowMs: number): string {
  const s = Math.max(0, Math.round(nowMs / 1000 - epochSec))
  if (s < 45) return 'now'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`
  if (s < 86400) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}
