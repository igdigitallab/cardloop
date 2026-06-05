import { en } from './en'

/** Simple accessor — no runtime overhead, tree-shakeable.
 *  Usage: import { t } from '../i18n'
 *         t['common.save']
 */
export const t = en

export type { TKey } from './en'
