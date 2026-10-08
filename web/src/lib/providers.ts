/** spec-095: the ONE table of engines the cockpit UI knows about.
 *
 *  Before this module every surface that had to name a provider carried its own literal —
 *  `['claude', 'codex']` arrays, `provider === 'codex' ? 'Codex' : 'Claude Code'` ternaries,
 *  a `codex_thread_id` here and a `codex_model` there — and a third provider would have meant
 *  editing two dozen of them (and silently mislabelling it as Claude wherever one was missed).
 *  The facts that differ per provider live here, once; call sites ask the table.
 *
 *  Mirrors `providers.py` (the server's seam): Claude is the cockpit's OWN harness (`adapter:
 *  false`); every other provider wraps a foreign CLI and so has its own continuity field, a
 *  `<name>_model` project field and a registry-driven model list. Claude-only feature gates
 *  (accounts, auto-rotate, the cloud model list) stay `=== 'claude'` at their sites — a new
 *  provider must NOT inherit them by default.
 *
 *  Deliberately NOT here: availability, models and capabilities. Those come from the live
 *  registry (`GET /api/agent-providers`); this table only holds what is true regardless of
 *  whether the server is reachable.
 *
 *  Leaf module: no imports, so `types.ts`, `api.ts` and the node tests can all depend on it. */

export interface ProviderMeta {
  /** Full name — pickers, error text, the handoff strip. */
  label: string
  /** Compact name — card badges, filter chips. */
  short: string
  /** Runtime-row tag (`C`, `G`). null = derived from the account label (Claude's M / W). */
  tag: string | null
  /** False only for the cockpit's own harness (Claude). Adapters read their models, reasoning
   *  levels and capabilities from the registry and keep their own resume id. */
  adapter: boolean
  /** Chat / free-chat record field holding THIS provider's resume id. */
  continuityField: 'session_id' | 'codex_thread_id' | 'grok_session_id'
  /** Project-settings field holding this provider's default model. */
  modelField: 'model' | 'codex_model' | 'grok_model'
  /** True when the server hands EVERY project a default value in `modelField` whether or not this
   *  provider exists on the install, so a value there says nothing about the project. The server
   *  does that for every registered adapter (`_project_settings_view`), so it is true for each of
   *  them - a new adapter row says `true` too (tests/test_spec096_p8a_runsites.py checks the pair). */
  servesDefaultModel: boolean
  /** The subscription the provider's turns ride on (Usage tab: "SuperGrok subscription"). */
  subscription: string
  /** Does the provider publish subscription windows the pill can show? A provider that does
   *  not is rendered muted ("limits not reported") — unknown is never green. */
  reportsLimits: boolean
}

export const PROVIDERS = {
  claude: {
    label: 'Claude Code', short: 'Claude', tag: null, adapter: false,
    continuityField: 'session_id', modelField: 'model', subscription: 'Claude', servesDefaultModel: false, reportsLimits: true,
  },
  codex: {
    label: 'Codex', short: 'Codex', tag: 'C', adapter: true,
    continuityField: 'codex_thread_id', modelField: 'codex_model', subscription: 'ChatGPT', servesDefaultModel: true, reportsLimits: true,
  },
  grok: {
    label: 'Grok', short: 'Grok', tag: 'G', adapter: true,
    continuityField: 'grok_session_id', modelField: 'grok_model', subscription: 'SuperGrok', servesDefaultModel: true, reportsLimits: false,
  },
} as const satisfies Record<string, ProviderMeta>

export type Provider = keyof typeof PROVIDERS

/** Every provider, in display order (Claude first). */
export const PROVIDER_IDS = Object.keys(PROVIDERS) as Provider[]

/** What a record with no provider field (a legacy chat, a project that never chose) runs on. */
export const DEFAULT_PROVIDER: Provider = 'claude'

/** Own-property check: `'constructor'` / `'toString'` / `'__proto__'` are NOT providers, and a
 *  plain `in` would say they are (and then `PROVIDERS[x].label` would throw at render time). */
export function isKnownProvider(x: unknown): x is Provider {
  return typeof x === 'string' && Object.prototype.hasOwnProperty.call(PROVIDERS, x)
}

/** Permissive coercion for display and form state: anything unknown becomes the default.
 *  ⚠️ Never use this to pick an engine for a RUN — the server's `providers.get` refuses an
 *  unknown name on purpose; the UI only labels. */
export function normalizeProvider(x: unknown): Provider {
  return isKnownProvider(x) ? x : DEFAULT_PROVIDER
}

/** Display name. An unknown (future / newer-server) provider shows its raw id instead of
 *  being passed off as Claude; an absent one is the default provider. */
export function providerLabel(x: unknown): string {
  if (isKnownProvider(x)) return PROVIDERS[x].label
  return typeof x === 'string' && x ? x : PROVIDERS[DEFAULT_PROVIDER].label
}

/** Compact display name (badges, filter chips); same unknown rules as `providerLabel`. */
export function providerShort(x: unknown): string {
  if (isKnownProvider(x)) return PROVIDERS[x].short
  return typeof x === 'string' && x ? x : PROVIDERS[DEFAULT_PROVIDER].short
}

/** The provider's fixed runtime tag, or null (Claude derives its tag from the account). */
export function providerTag(x: unknown): string | null {
  return isKnownProvider(x) ? PROVIDERS[x].tag : null
}

/** True for a known provider that is not the cockpit's own harness. Unknown is false: nothing
 *  adapter-shaped is assumed about a name this build has never heard of. */
export function isAdapterProvider(x: unknown): boolean {
  return isKnownProvider(x) && PROVIDERS[x].adapter
}

/** Can this provider's subscription windows drive the pill? Unknown = no (muted, not green). */
export function providerReportsLimits(x: unknown): boolean {
  return isKnownProvider(x) && PROVIDERS[x].reportsLimits
}

/** "SuperGrok" / "ChatGPT" / "Claude"; an unknown provider has no known subscription name. */
export function providerSubscription(x: unknown): string {
  return isKnownProvider(x) ? PROVIDERS[x].subscription : 'subscription'
}

/** Name of the chat-record field that holds this provider's resume id; null if unknown. */
export function continuityField(x: unknown): string | null {
  return isKnownProvider(x) ? PROVIDERS[x].continuityField : null
}

/** The project-settings field for this provider's default model; null if unknown. */
export function projectModelField(x: unknown): string | null {
  return isKnownProvider(x) ? PROVIDERS[x].modelField : null
}

/** The resume id a chat / free-chat / search-hit record holds for `provider` (defaults to the
 *  record's own `provider`). Reads ONLY that provider's field: a Codex chat must never hand a
 *  Claude `session_id` to the Codex history reader, or the reverse. */
export function continuityId(
  rec: Readonly<Record<string, unknown>> | null | undefined,
  provider?: unknown,
): string | null {
  if (!rec) return null
  const field = continuityField(provider ?? rec.provider ?? DEFAULT_PROVIDER)
  if (!field) return null
  const v = rec[field]
  return typeof v === 'string' && v ? v : null
}

/** Which thread a search hit opens. `sessionId` is what the peek feeds the history endpoint as
 *  `session_id` (the old rule: the hit's `session_id`, else its provider's own id); `continuityId`
 *  is set only for an adapter provider and names that provider's thread — its own field when
 *  the hit carries it, else the `session_id` (a server that files an adapter's id there). */
export function hitThread(
  ref: Readonly<Record<string, unknown>>,
  provider: unknown,
): { sessionId: string; continuityId?: string } | null {
  const own = continuityId(ref, provider)
  const sid = typeof ref.session_id === 'string' && ref.session_id ? ref.session_id : null
  const sessionId = sid || own
  if (!sessionId) return null
  return isAdapterProvider(provider) ? { sessionId, continuityId: own || sessionId } : { sessionId }
}

/** Query parameters that ask a history endpoint for ONE specific thread of a non-default
 *  provider: `provider=<p>` plus the id under that provider's continuity field name (Codex
 *  keeps its original `codex_thread_id`, so the wire is unchanged for it). Empty for the
 *  default provider (its id rides as `session_id`) and for an unknown provider or empty id. */
export function continuityQuery(provider: unknown, id: string | null | undefined): Record<string, string> {
  if (!id || !isAdapterProvider(provider)) return {}
  return { provider: provider as string, [PROVIDERS[provider as Provider].continuityField]: id }
}

/** Minimal registry-row shape the pickers need (structural: no import cycle with types.ts). */
export interface ProviderRowLike {
  provider: string
  enabled: boolean
  available: boolean
  error?: string | null
}

/** Providers a picker should OFFER. Claude always (it is the cockpit's own harness and the
 *  fallback when the registry fetch fails); an adapter only when the server lists it — a
 *  server with the feature switched off hides the row, and a permanently greyed button for
 *  something that does not exist here is noise. */
export function selectableProviders(
  registry: readonly { provider: string }[],
  /** A provider the record already holds: kept in the list even when the server no longer
   *  lists it, so a `<select>` bound to it does not render blank. */
  keep?: unknown,
): Provider[] {
  return PROVIDER_IDS.filter(id => !PROVIDERS[id].adapter || id === keep || registry.some(r => r.provider === id))
}

/** Why `id` cannot be picked right now; '' when it can. The cockpit's own harness is never
 *  refused here. An adapter is refused when the registry lists it as disabled or unavailable,
 *  with the registry's own reason when it gave one. */
export function providerUnavailableReason(id: Provider, row: ProviderRowLike | undefined | null): string {
  if (!PROVIDERS[id].adapter) return ''
  if (row && row.enabled && row.available) return ''
  return row?.error || `${PROVIDERS[id].label} unavailable`
}

/** Providers that get a "<name> board model" row in Settings: adapters the server lists, or
 *  that the project is already set up for — its board default IS that provider, or (for a provider
 *  whose default model the server does not hand to every project) it names a model. A provider that
 *  does `servesDefaultModel` (Codex and Grok today) would otherwise put a row on every Settings
 *  page of a cockpit that has it switched off. */
export function boardModelProviders(
  registry: readonly { provider: string }[],
  settings: object,
): Provider[] {
  const s = settings as Record<string, unknown>
  return PROVIDER_IDS.filter(id => {
    const meta = PROVIDERS[id]
    if (!meta.adapter) return false
    if (registry.some(r => r.provider === id)) return true
    if (s.board_provider === id) return true
    if (meta.servesDefaultModel) return false
    return typeof s[meta.modelField] === 'string' && s[meta.modelField] !== ''
  })
}

/** Usage-tab provider filter: 'all' shows every section, otherwise only the chosen one. */
export type UsageFilter = 'all' | Provider
export function usageSectionVisible(filter: UsageFilter, section: Provider): boolean {
  return filter === 'all' || filter === section
}
