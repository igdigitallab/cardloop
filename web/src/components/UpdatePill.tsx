import { t } from '../i18n'

/**
 * Shown only when a new build is waiting and reloading right now would interrupt something
 * (a streaming turn, a draft in the composer). useBuildWatch applies the update by itself as
 * soon as the app goes idle — this is the manual override for an operator who wants it now.
 */
export function UpdatePill({ onApply }: { onApply: () => void }) {
  return (
    <div className="update-pill" role="status">
      <span className="update-pill-msg">{t['update.available']}</span>
      <button className="update-pill-btn" onClick={onApply}>{t['update.reload']}</button>
    </div>
  )
}
