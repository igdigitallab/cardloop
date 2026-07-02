/**
 * File-type icon picker by extension + basename helper.
 * Uses lucide-react icons already bundled in the app.
 */
import {
  File,
  FileCode,
  FileJson,
  FileText,
  Image,
  FileCog,
  Terminal,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/** Returns the lucide icon component matching the file extension. */
export function fileIcon(filename: string): LucideIcon {
  const dot = filename.lastIndexOf('.')
  const ext = dot >= 0 ? filename.slice(dot + 1).toLowerCase() : ''
  switch (ext) {
    case 'ts':
    case 'tsx':
    case 'js':
    case 'jsx':
    case 'mjs':
    case 'cjs':
    case 'css':
    case 'scss':
    case 'sass':
    case 'less':
    case 'html':
    case 'htm':
    case 'vue':
    case 'svelte':
      return FileCode
    case 'py':
    case 'rb':
    case 'rs':
    case 'go':
    case 'c':
    case 'cpp':
    case 'cc':
    case 'h':
    case 'java':
    case 'kt':
    case 'swift':
      return FileCode
    case 'json':
    case 'jsonc':
      return FileJson
    case 'md':
    case 'mdx':
    case 'txt':
    case 'rst':
      return FileText
    case 'sh':
    case 'bash':
    case 'zsh':
    case 'fish':
    case 'ps1':
      return Terminal
    case 'yaml':
    case 'yml':
    case 'toml':
    case 'ini':
    case 'cfg':
    case 'conf':
    case 'env':
      return FileCog
    case 'png':
    case 'jpg':
    case 'jpeg':
    case 'gif':
    case 'webp':
    case 'svg':
    case 'ico':
    case 'bmp':
      return Image
    default:
      return File
  }
}

/** Returns the last path segment (filename only). */
export function basename(path: string): string {
  // Handle both / and \ separators
  const parts = path.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || path
}
