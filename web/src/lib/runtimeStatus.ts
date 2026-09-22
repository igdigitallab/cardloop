/** spec-093: ONE source for "which runtime (engine x subscription) is this chat spending".
 *
 *  Before this module three widgets answered that question from three sources on three
 *  cadences: the top pill read the GLOBAL account from /api/usage every 30 s, the chat's
 *  runtime menu read /api/agent-providers ONCE at mount (so its "· default" marker went stale
 *  the moment the operator switched), and a pill next to the model showed the PROJECT pin.
 *  Every one of them could disagree with the subscription a turn actually billed.
 *
 *  Now every consumer reads one polled snapshot, keys runtimes the same way the picker PATCH
 *  does, and resolves a chat's effective runtime through ONE chain
 *  (chat -> project -> globally active -> main, see accounts.resolve), so the thing a widget
 *  highlights and the thing the engine receives cannot drift apart. */
import { useSyncExternalStore } from 'react'
import { api, type UsageLimits, type UsageLimitRow } from '../api'
import type { AgentProviderInfo, Chat, Provider } from '../types'

/** One selectable runtime, in the unified shape every surface renders. */
export interface RuntimeRow {
  /** `claude:<account>:` · `claude::<backend>` · `<provider>::` — the picker's own key scheme. */
  key: string
  /** Short letter tag: M (main), W (work), C (Codex), L (local Ollama). */
  tag: string
  name: string
  plan: string
  provider: Provider
  /** Explicit Claude account id; null for runtimes with no account dimension. */
  account: string | null
  /** "" = the provider's own cloud endpoint, "ollama" = the local box. */
  backend: string
  available: boolean
  reason?: string
  /** False for a local backend: it spends no subscription, so there is no percentage. */
  hasQuota: boolean
  /** Subscription windows, or null when nothing has been read yet. */
  windows: Record<string, UsageLimitRow> | null
  /** When `windows` was read (unix sec), null if never. */
  ts: number | null
  /** The number may be old: an inactive account not refreshed recently, or a Codex snapshot
   *  from a run long ago. Rendered dimmed with a `*` — never passed off as live. */
  stale: boolean
  /** True for the globally active Claude account (what unpinned chats and cards inherit). */
  isGlobalDefault: boolean
  defaultModel?: string
}

/** What the chat on screen publishes so the top pill (which lives outside the chat) can show
 *  and change THAT chat's runtime instead of the global one. */
export interface CurrentChat {
  owner: string
  projectName: string
  chatName: string
  runtimeKey: string
  /** The key an unpinned chat of this project would run on (project pin -> global -> main). */
  inheritedKey: string
  /** True when this chat carries its own pin (account, provider or backend). */
  pinned: boolean
  /** Project-level account pin, if any — unpinned chats here follow it, not the global. */
  projectAccount: string | null
  /** Project-level backend pin: outranks every chat in the project. */
  projectBackend: string
  busy: boolean
  error: string
  pick: (key: string) => void
  followDefault: () => void
}

interface Snapshot {
  usage: UsageLimits | null
  /** Browser clock (unix sec) when `usage` arrived — `usage.now` is frozen between polls. */
  usageAt: number
  providers: AgentProviderInfo[]
  providersLoaded: boolean
  current: CurrentChat | null
}

/** An inactive account's numbers are only refreshed by the backend every few minutes. */
const CLAUDE_STALE_AFTER_SEC = 900
/** Codex reports limits only DURING a run; older than its short window = not a live reading. */
const CODEX_STALE_AFTER_SEC = 6 * 3600
const USAGE_POLL_MS = 30_000
const PROVIDERS_POLL_MS = 60_000

let snap: Snapshot = { usage: null, usageAt: 0, providers: [], providersLoaded: false, current: null }
const listeners = new Set<() => void>()
let timers: ReturnType<typeof setInterval>[] = []

function emit(next: Partial<Snapshot>) {
  snap = { ...snap, ...next }
  listeners.forEach(l => l())
}

async function loadUsage() {
  try {
    const usage = await api.usage()
    emit({ usage, usageAt: Date.now() / 1000 })
  } catch { /* keep the last good reading */ }
}

/** The server's clock NOW: `usage.now` advanced by the time since it arrived, so countdowns
 *  and "has this window already reset" keep moving between polls and across a failed poll. */
export function serverNow(s: Pick<Snapshot, 'usage' | 'usageAt'>): number {
  const local = Date.now() / 1000
  return s.usage ? s.usage.now + (local - s.usageAt) : local
}

// The registry changes a few times a day, but it is polled every minute. Re-emitting an
// identical payload would hand every subscriber a new array reference — and ChatTab (one per
// open project) re-renders on it. Emit only on a real change.
let providersSig = ''

async function loadProviders() {
  try {
    const res = await api.agentProviders()
    const sig = JSON.stringify(res.providers)
    if (sig === providersSig && snap.providersLoaded) return
    providersSig = sig
    emit({ providers: res.providers, providersLoaded: true })
  } catch { if (!snap.providersLoaded) emit({ providersLoaded: true }) }
}

/** Refetch both halves now — call after anything that changes a runtime or the default. */
export function refreshRuntimeStatus() {
  void loadUsage()
  void loadProviders()
}

function onWake() { if (!document.hidden) refreshRuntimeStatus() }

function start() {
  refreshRuntimeStatus()
  timers = [
    setInterval(() => { if (!document.hidden) void loadUsage() }, USAGE_POLL_MS),
    setInterval(() => { if (!document.hidden) void loadProviders() }, PROVIDERS_POLL_MS),
  ]
  window.addEventListener('focus', onWake)
  document.addEventListener('visibilitychange', onWake)
}

function stop() {
  timers.forEach(clearInterval)
  timers = []
  window.removeEventListener('focus', onWake)
  document.removeEventListener('visibilitychange', onWake)
}

function subscribe(l: () => void) {
  listeners.add(l)
  if (listeners.size === 1) start()
  return () => {
    listeners.delete(l)
    if (listeners.size === 0) stop()
  }
}

/** The current snapshot, outside React (tests, event handlers). */
export function getRuntimeSnapshot(): Readonly<Snapshot> {
  return snap
}

/** Everything, re-rendering on every poll — for the small widgets that show percentages. */
export function useRuntimeStatus(): Snapshot {
  return useSyncExternalStore(subscribe, () => snap)
}

/** Only the provider registry: a stable reference until it really changes. ChatTab uses this
 *  so a 30 s usage poll does not re-render every open project's chat. */
export function useRuntimeProviders(): AgentProviderInfo[] {
  return useSyncExternalStore(subscribe, () => snap.providers)
}

/** Only the globally active account id (a string — equal values never re-render). */
export function useGlobalAccountId(): string {
  return useSyncExternalStore(subscribe, () => globalDefaultAccount(snap.providers, snap.usage))
}

const CURRENT_FIELDS: (keyof CurrentChat)[] = [
  'owner', 'projectName', 'chatName', 'runtimeKey', 'inheritedKey', 'pinned', 'projectAccount',
  'projectBackend', 'busy', 'error',
]

// Every visible chat keeps its latest payload here; the pill follows ONE of them. A free-chat
// split shows two chats at once, both active — a single last-writer-wins slot let the pill
// describe (and re-pin!) the pane the operator was not using. The slot now follows the pane
// the operator last touched (claimCurrentChat), and a data update never steals it.
const payloads = new Map<string, CurrentChat>()
let focusOwner: string | null = null

function recomputeCurrent() {
  let cur: CurrentChat | null = focusOwner ? payloads.get(focusOwner) ?? null : null
  if (!cur) {
    // Focus holder gone: fall back to the most recently registered visible chat.
    for (const v of payloads.values()) cur = v
    focusOwner = cur?.owner ?? null
  }
  if (cur !== snap.current) emit({ current: cur })
}

/** A visible ChatTab announces (or refreshes) itself. `pick`/`followDefault` must be stable
 *  (ref-backed): only the data fields are compared, so a new closure alone never emits. */
export function publishCurrentChat(cur: CurrentChat) {
  const prev = payloads.get(cur.owner)
  if (prev && CURRENT_FIELDS.every(f => prev[f] === cur[f])) return
  payloads.set(cur.owner, cur)
  recomputeCurrent()
}

/** The operator touched this chat — the pill follows it from now on. */
export function claimCurrentChat(owner: string) {
  if (!payloads.has(owner) || focusOwner === owner) return
  focusOwner = owner
  recomputeCurrent()
}

/** Withdraw a chat that went hidden or unmounted. */
export function clearCurrentChat(owner: string) {
  if (!payloads.delete(owner)) return
  if (focusOwner === owner) focusOwner = null
  recomputeCurrent()
}

// ─── Tags ────────────────────────────────────────────────────────────────────

const FIXED_TAGS: Record<string, string> = { codex: 'C', ollama: 'L' }

function alnum(s: string): string {
  return s.replace(/[^\p{L}\p{N}]/gu, '')
}

/** Assign short tags: first letter of the account label; Codex = C, local Ollama = L. A clash
 *  widens the account tags involved to two letters (fixed tags never move), then numbers. */
function assignTags(rows: { key: string; seed: string; fixed: boolean }[]): Record<string, string> {
  const out: Record<string, string> = {}
  const taken = new Set<string>()
  for (const r of rows) if (r.fixed) { out[r.key] = r.seed; taken.add(r.seed) }
  const free = rows.filter(r => !r.fixed)
  const first = (r: { seed: string }) => (alnum(r.seed)[0] || '?').toUpperCase()
  const counts: Record<string, number> = {}
  for (const r of free) counts[first(r)] = (counts[first(r)] || 0) + 1
  for (const r of free) {
    const a = alnum(r.seed)
    let tag = first(r)
    if (counts[tag] > 1 || taken.has(tag)) tag = (a[0] || '?').toUpperCase() + (a[1] || '').toLowerCase()
    let n = 2
    const base = tag
    while (taken.has(tag)) tag = `${base}${n++}`
    out[r.key] = tag
    taken.add(tag)
  }
  return out
}

// ─── Rows ────────────────────────────────────────────────────────────────────

/** Build the unified runtime list from the two halves of the snapshot. */
export function buildRuntimeRows(providers: AgentProviderInfo[], usage: UsageLimits | null): RuntimeRow[] {
  const now = usage?.now ?? Date.now() / 1000
  const rows: Omit<RuntimeRow, 'tag'>[] = []
  const seeds: { key: string; seed: string; fixed: boolean }[] = []
  for (const p of providers) {
    const defaultModel = p.models.find(m => m.default)?.value || p.models[0]?.value
    if (p.provider === 'claude') {
      for (const a of p.accounts ?? []) {
        const key = `claude:${a.id}:`
        // Limits: the multi-account block carries every account; a single-account install
        // only has the top-level `limits`, which then belongs to that one account.
        const acctRow = usage?.accounts?.find(r => r.id === a.id)
        // An unnamed active account (server omitted it) on a single-account install is still
        // the one those limits belong to.
        const soleAccount = !usage?.accounts?.length && (!usage?.account || usage.account === a.id)
        const windows = acctRow ? acctRow.limits : (usage && soleAccount ? usage.limits : null)
        const ts = acctRow ? acctRow.limits_ts : (windows ? now : null)
        rows.push({
          key, name: a.label, plan: (a.plan || '').toUpperCase(), provider: 'claude',
          account: a.id, backend: '', available: p.available && a.available,
          reason: !p.available ? (p.error || 'Claude is not available')
            : a.available ? undefined : (a.reason || 'this subscription cannot run'),
          hasQuota: true, windows: windows && Object.keys(windows).length ? windows : null,
          ts, stale: !a.active && ts != null && now - ts > CLAUDE_STALE_AFTER_SEC,
          isGlobalDefault: !!a.active, defaultModel,
        })
        seeds.push({ key, seed: a.label || a.id, fixed: false })
      }
      for (const b of p.backends ?? []) {
        if (!b.id) continue
        const key = `claude::${b.id}`
        rows.push({
          key, name: b.label.replace(/\s*\(local\)\s*/i, '') || b.id, plan: 'LOCAL',
          provider: 'claude', account: null, backend: b.id, available: b.available,
          reason: b.available ? undefined : (b.error || `${b.label} is not answering`),
          hasQuota: false, windows: null, ts: null, stale: false, isGlobalDefault: false,
          defaultModel: b.models?.[0]?.value,
        })
        seeds.push({ key, seed: FIXED_TAGS[b.id] || b.id, fixed: !!FIXED_TAGS[b.id] })
      }
    } else {
      const key = `${p.provider}::`
      const codex = p.provider === 'codex' ? usage?.codex ?? null : null
      const ts = codex?.ts ?? null
      rows.push({
        key, name: p.provider === 'codex' ? 'Codex' : p.provider,
        plan: (codex?.plan_type || '').toUpperCase(), provider: p.provider, account: null,
        backend: '', available: p.available && p.enabled,
        reason: !p.enabled ? `${p.provider} is switched off in this install`
          : p.available ? undefined : (p.error || `${p.provider} is not available`),
        hasQuota: true, windows: codex?.limits && Object.keys(codex.limits).length ? codex.limits : null,
        ts, stale: ts != null && now - ts > CODEX_STALE_AFTER_SEC, isGlobalDefault: false,
        defaultModel,
      })
      seeds.push({ key, seed: FIXED_TAGS[p.provider] || p.provider, fixed: !!FIXED_TAGS[p.provider] })
    }
  }
  // Two registry entries with one key (a duplicated account id) would render as one React key
  // and share a tag — keep the first.
  const seen = new Set<string>()
  const unique = rows.filter(r => !seen.has(r.key) && !!seen.add(r.key))
  const tags = assignTags(seeds.filter((sd, i) => seeds.findIndex(o => o.key === sd.key) === i))
  return unique.map(r => ({ ...r, tag: tags[r.key] }))
}

/** The id of the globally active Claude account, from whichever half has it. */
export function globalDefaultAccount(providers: AgentProviderInfo[], usage: UsageLimits | null): string {
  return usage?.account
    || providers.find(p => p.provider === 'claude')?.accounts?.find(a => a.active)?.id
    || 'main'
}

/** THE chain. The runtime a chat actually runs on: a non-Claude provider is its own runtime;
 *  on Claude a project backend pin outranks the chat's (webapp `_resolve_run_backend`), and
 *  the account resolves chat -> project -> global -> main. Every surface that shows or
 *  highlights a chat's runtime must come through here. */
export function effectiveRuntimeKey(
  chat: Pick<Chat, 'provider' | 'account' | 'backend'> | null | undefined,
  project: { account?: string | null; backend?: string | null },
  globalAccount: string,
  /** Accounts that cannot run right now. The server treats a PROJECT pin on one of them as
   *  soft and silently runs the global account instead (accounts.resolve), so showing the
   *  project's account there would name a subscription nobody is spending. A CHAT pin is
   *  strict server-side (the turn is refused), so it is still shown as-is. */
  unusable?: ReadonlySet<string>,
): string {
  const provider = chat?.provider ?? 'claude'
  if (provider !== 'claude') return `${provider}::`
  const backend = project.backend || chat?.backend || ''
  if (backend) return `claude::${backend}`
  const projectAccount = project.account && !unusable?.has(project.account) ? project.account : ''
  return `claude:${chat?.account || projectAccount || globalAccount || 'main'}:`
}

/** Ids of Claude accounts the registry says cannot run. */
export function unusableAccounts(providers: AgentProviderInfo[]): Set<string> {
  const out = new Set<string>()
  for (const a of providers.find(p => p.provider === 'claude')?.accounts ?? []) {
    if (!a.available) out.add(a.id)
  }
  return out
}

/** What an unpinned chat of this project inherits (no chat-level pin at all). */
export function inheritedRuntimeKey(
  project: { account?: string | null; backend?: string | null },
  globalAccount: string,
  unusable?: ReadonlySet<string>,
): string {
  return effectiveRuntimeKey(null, project, globalAccount, unusable)
}

/** Does this chat carry its own pin (anything that stops it from following the default)? */
export function chatIsPinned(chat: Pick<Chat, 'provider' | 'account' | 'backend'> | null | undefined): boolean {
  if (!chat) return false
  return !!chat.account || (chat.provider ?? 'claude') !== 'claude' || !!chat.backend
}

// ─── Lead window ─────────────────────────────────────────────────────────────

/** The pill's window: the SHORT one, always — the operator reads the pill as "how much of
 *  this 5-hour window is gone and when does it roll over" (decision 2026-09-22). Codex's
 *  `primary` is its short window. The weekly and per-model buckets live in the dropdown. */
const PILL_KEY: Record<string, string> = { claude: 'five_hour', codex: 'primary' }

/** Windows whose rejection actually blocks a turn. Not the per-model buckets (Fable, Opus —
 *  they only bite when that model runs) and not Codex `credits` (the prepaid wallet, reported
 *  "rejected" whenever it is empty, also on a healthy subscription). */
const BLOCKING_KEYS: Record<string, string[]> = {
  claude: ['five_hour', 'seven_day'],
  codex: ['primary', 'secondary'],
}

/** Display order of a runtime's windows in the breakdown: the lead windows first. */
export function windowKeys(row: RuntimeRow): string[] {
  if (!row.windows) return []
  const known = row.provider === 'claude'
    ? ['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage']
    : BLOCKING_KEYS[row.provider] || []
  const rest = Object.keys(row.windows).filter(k => !known.includes(k)).sort()
  return [...known, ...rest].filter(k => row.windows![k])
}

export interface Lead { key: string; d: UsageLimitRow }

/** What the pill shows: the short window — unless another window has actually cut the
 *  runtime off, which then leads (a green "3%" on a subscription whose weekly limit is spent
 *  would be the lie this pill exists to prevent). A window whose reset has passed has rolled
 *  over and says nothing about now. */
export function leadWindow(row: RuntimeRow, now: number): Lead | null {
  if (!row.windows) return null
  const live = (k: string) => {
    const d = row.windows![k]
    return d && !(d.resets_at != null && d.resets_at <= now) ? d : null
  }
  for (const k of BLOCKING_KEYS[row.provider] || []) {
    const d = live(k)
    if (d && d.status === 'rejected') return { key: k, d }
  }
  const key = PILL_KEY[row.provider]
  const d = key ? live(key) : null
  return d ? { key, d } : null
}
