/** Normalize whatever the operator typed into a bare origin with a trailing slash:
 *  `cardloop.example.com`, `https://cardloop.example.com/board?x=1#y` and
 *  `HTTPS://Cardloop.Example.com` all become `https://cardloop.example.com/`.
 *  Returns null when it cannot be parsed as a URL at all. */
export function normalizeUrl(raw: string): string | null {
  let value = raw.trim()
  if (!value) return null
  if (!/^https?:\/\//i.test(value)) value = `https://${value}`
  try {
    const u = new URL(value)
    u.hash = ''
    u.search = ''
    u.pathname = '/'
    return u.toString()
  } catch {
    return null
  }
}
