interface Props {
  label?: string
}

export function Spinner({ label = 'Загрузка...' }: Props) {
  return (
    <div className="spinner-wrap">
      <div className="spinner" />
      <span>{label}</span>
    </div>
  )
}
