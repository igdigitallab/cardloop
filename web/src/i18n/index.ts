import { en } from './en'

/** Simple accessor — no runtime overhead, tree-shakeable.
 *  Usage: import { t } from '../i18n'
 *         t['common.save']
 */
export const t = en

// ONE locale on purpose. A `ru.ts` sat here unreachable from the initial commit until
// 2026-09-10 and collected 36 commits of translations nothing ever read. Adding a locale
// means shipping the runtime switcher FIRST — a second dictionary alone is dead weight.

export type { TKey } from './en'
