/**
 * Image / PDF / video / audio preview for the Files explorer. The bytes come from
 * /api/fs/raw through a plain URL, so the browser does the decoding and Range requests.
 */
import { useState } from 'react'
import { Download, ExternalLink } from 'lucide-react'
import { Lightbox } from './Lightbox'
import type { FsMediaKind } from '../types'

interface Props {
  kind: FsMediaKind
  name: string
  url: string
  downloadUrl: string
  /** Phones: an <iframe> PDF is not rendered by the Android WebView, so offer the file instead. */
  narrow: boolean
}

export function FileMedia({ kind, name, url, downloadUrl, narrow }: Props) {
  const [zoom, setZoom] = useState(false)
  const [failed, setFailed] = useState(false)

  const links = (
    <div className="files-media-links">
      <a className="files-media-link" href={url} target="_blank" rel="noopener noreferrer">
        <ExternalLink size={13} /> Open in a new tab
      </a>
      <a className="files-media-link" href={downloadUrl} download={name}>
        <Download size={13} /> Download
      </a>
    </div>
  )

  if (failed) {
    return (
      <div className="files-media">
        <div className="error-state">⚠ The browser could not play or show this file.</div>
        {links}
      </div>
    )
  }

  if (kind === 'image') {
    return (
      <div className="files-media">
        <img
          className="files-media-img"
          src={url}
          alt={name}
          onClick={() => setZoom(true)}
          onError={() => setFailed(true)}
          title="Click to zoom"
        />
        {links}
        {zoom && <Lightbox src={url} alt={name} onClose={() => setZoom(false)} />}
      </div>
    )
  }

  if (kind === 'video') {
    return (
      <div className="files-media">
        <video className="files-media-video" src={url} controls preload="metadata" onError={() => setFailed(true)} />
        {links}
      </div>
    )
  }

  if (kind === 'audio') {
    return (
      <div className="files-media">
        <audio className="files-media-audio" src={url} controls preload="metadata" onError={() => setFailed(true)} />
        {links}
      </div>
    )
  }

  // pdf
  return (
    <div className="files-media files-media-pdf">
      {narrow ? (
        <div className="files-pdf-card">PDF preview is not available on phones here — open or download it.</div>
      ) : (
        <iframe className="files-pdf" src={url} title={name} />
      )}
      {links}
    </div>
  )
}
