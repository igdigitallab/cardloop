/** spec-095: telling a DELIBERATE server refusal apart from the other things a 409 means.
 *
 *  The server answers 409 for unrelated reasons: "a turn is running" (busy), a stale
 *  compare-and-swap revision, a backend that is not answering - and a deliberate refusal
 *  ("this project is pinned to another backend"). Two call sites used to read every 409 as
 *  the first thing they knew (the board: "project is busy"; the runtime picker: "changed
 *  elsewhere, reloaded"), which would have told the operator to wait or retry on a refusal no
 *  amount of waiting fixes. This returns the server's own sentence for a refusal and null for
 *  everything else, so the caller keeps its existing handling for those. */

interface ApiErrorLike {
  status?: number
  body?: {
    error?: unknown
    busy?: unknown
    backend_unavailable?: unknown
    current_revision?: unknown
  } | null
}

export function refusalReason(e: unknown): string | null {
  const err = e as ApiErrorLike | null
  if (!err || err.status !== 409) return null
  const b = err.body
  if (!b || typeof b.error !== 'string' || !b.error) return null
  // These three have their own handling (and wording) at the sites that can see them.
  if (b.busy || b.backend_unavailable || b.current_revision != null) return null
  // A bare "project busy" without the flag is still a busy answer, not a refusal.
  if (/\bbusy\b/i.test(b.error)) return null
  return b.error
}

/** The sentence inside a failed response body: `{"error":"…"}` → the error text, anything else
 *  (HTML from a proxy, an empty body, plain text) unchanged. A raw fetch of the chat POST
 *  rethrows the whole body as the Error message, which put a JSON blob in the chat feed. */
export function errorTextFromBody(text: string): string {
  try {
    const parsed: unknown = JSON.parse(text)
    const err = (parsed as { error?: unknown } | null)?.error
    if (typeof err === 'string' && err) return err
  } catch { /* not JSON */ }
  return text
}
