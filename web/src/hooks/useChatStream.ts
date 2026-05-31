/**
 * SSE streaming for the chat panel.
 * Handles ReadableStream reading, chunk-boundary-safe line parsing,
 * and dispatching ChatSSEEvent to the caller via onEvent.
 */
import { ChatSSEEvent } from '../types'

/** Parse a single SSE line: "data: {...}" → parsed object or null */
export function parseSseLine(line: string): ChatSSEEvent | null {
  if (!line.startsWith('data: ')) return null
  try {
    return JSON.parse(line.slice(6)) as ChatSSEEvent
  } catch {
    return null
  }
}

/**
 * Read a ReadableStream line-by-line, calling onLine for each complete line.
 * Handles chunk boundaries correctly (a single chunk may contain partial lines).
 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onLine: (line: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      if (signal.aborted) break
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const parts = buf.split('\n')
      buf = parts.pop() ?? ''
      for (const part of parts) {
        if (part.startsWith('data: ') || part.startsWith(':')) onLine(part)
      }
    }
    // flush remaining buffer
    if (buf.startsWith('data: ') || buf.startsWith(':')) onLine(buf)
  } finally {
    reader.releaseLock()
  }
}
