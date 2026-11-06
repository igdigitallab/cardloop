// What a chat bubble says when its live stream breaks.
//
// A send's POST stream dies on the client side for transport reasons (a phone that slept or
// switched networks, a tunnel hop that reset) while the turn keeps running on the server: the
// server only logs "client disconnected, task continues in background". The browser's own text
// for that — Chrome "network error", "Failed to fetch"; Safari "Load failed"; Firefox
// "NetworkError …" — reads as if the agent died, and the operator re-asks a question whose
// answer is already being written. The cockpit pulls the finished reply in by itself once the
// connection is back (the /live poll and the activity stream), so the bubble says exactly that.

export const CONNECTION_LOST_MSG =
  'Connection lost — the agent keeps working on the server. Its reply appears here once the connection is back.'

// Transport failures only. A TypeError from our own code ("Cannot read properties of undefined")
// must keep its real text, so the error class alone is not enough.
const TRANSPORT_RE = /network ?error|failed to fetch|load failed|network connection was lost|error in input stream|err_(connection|network|internet|socket|http2|quic|incomplete)/i

export function isConnectionDrop(err: unknown): boolean {
  if (!(err instanceof Error)) return false
  return TRANSPORT_RE.test(err.message)
}

/** The text a bubble shows for a stream that ended in `err`. */
export function streamErrorText(err: unknown): string {
  if (isConnectionDrop(err)) return CONNECTION_LOST_MSG
  return err instanceof Error ? err.message : String(err)
}
