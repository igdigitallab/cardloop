import { t } from '../i18n'

interface Props {
  label?: string
}

export function Spinner({ label = t['spinner.default'] }: Props) {
  return (
    <div className="spinner-wrap">
      <div className="spinner" />
      <span>{label}</span>
    </div>
  )
}
