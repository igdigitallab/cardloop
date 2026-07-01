/** Shared model registry — value = alias stored in backend (topics.json),
 *  label = real version name shown to the user.
 *  IMPORTANT: do NOT change `value` — it maps to what run_engine / setModel expects.
 */
export const MODELS = [
  { value: 'fable',  label: 'Fable 5'    },
  { value: 'sonnet', label: 'Sonnet 5'   },
  { value: 'opus',   label: 'Opus 4.8'   },
  { value: 'haiku',  label: 'Haiku 4.5'  },
] as const

export type ModelValue = (typeof MODELS)[number]['value']

/** Returns the display label for a stored alias, or the alias itself as fallback. */
export function modelLabel(value: string): string {
  return MODELS.find(m => m.value === value)?.label ?? value
}
