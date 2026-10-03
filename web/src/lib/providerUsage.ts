/** spec-095: one shape for "what did this adapter provider's subscription turns add up to".
 *
 *  `/api/usage/dashboard` reports Codex and Grok in two different shapes - Codex as
 *  `cached_input` / `reasoning_output` with `by_model` an ARRAY of rows, Grok as `cached` /
 *  `reasoning` with `by_model` a RECORD keyed by model id - because each is the natural output
 *  of its own usage log. The Usage tab renders every adapter through ONE card, so the shapes
 *  are reconciled here, once, and a field the server did not send is 0, not NaN or a crash. */

export interface ProviderUsageModelRow {
  model: string
  turns: number
  input: number
  output: number
}

export interface ProviderUsage {
  turns: number
  input: number
  output: number
  cached: number
  reasoning: number
  byModel: ProviderUsageModelRow[]
  /** What the same tokens would have cost at API list prices. NOT spend: these turns ride a
   *  flat subscription. null = the server did not price them. */
  notionalUsd: number | null
}

function num(x: unknown): number {
  return typeof x === 'number' && Number.isFinite(x) ? x : 0
}

function modelRow(model: string, r: unknown): ProviderUsageModelRow {
  const o = (r && typeof r === 'object' ? r : {}) as Record<string, unknown>
  return { model, turns: num(o.turns), input: num(o.input), output: num(o.output) }
}

/** Reconcile one provider's usage block. Anything that is not an object is "no data" (null) so
 *  a card is never drawn for a provider the server did not report. */
export function normalizeProviderUsage(raw: unknown): ProviderUsage | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const o = raw as Record<string, unknown>
  const bm = o.by_model
  const rows: ProviderUsageModelRow[] = Array.isArray(bm)
    ? bm.map(r => modelRow(String((r as { model?: unknown } | null)?.model ?? 'unknown'), r))
    : bm && typeof bm === 'object'
      ? Object.entries(bm as Record<string, unknown>).map(([model, r]) => modelRow(model, r))
      : []
  const notional = o.notional_usd
  return {
    turns: num(o.turns),
    input: num(o.input),
    output: num(o.output),
    cached: num(o.cached ?? o.cached_input),
    reasoning: num(o.reasoning ?? o.reasoning_output),
    byModel: rows.sort((a, b) => b.turns - a.turns || a.model.localeCompare(b.model)),
    notionalUsd: typeof notional === 'number' && Number.isFinite(notional) ? notional : null,
  }
}
