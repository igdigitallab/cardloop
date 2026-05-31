import { ru } from './ru'

/** Simple accessor — no runtime overhead, tree-shakeable.
 *  Usage: import { t } from '../i18n'
 *         t['common.save']
 */
export const t = ru

export type { TKey } from './ru'
