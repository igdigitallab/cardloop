import { Modal, ModalHead } from './Modal'

interface Props {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  onConfirm: () => void
  onCancel: () => void
  danger?: boolean
}

export function ConfirmModal({
  title,
  message,
  confirmLabel = 'Подтвердить',
  cancelLabel = 'Отмена',
  onConfirm,
  onCancel,
  danger = false,
}: Props) {
  return (
    <Modal onClose={onCancel}>
      <ModalHead title={title} onClose={onCancel} />
      <div className="run-modal-body">
        <p style={{ margin: '0 0 16px', lineHeight: 1.5, fontSize: 14 }}>{message}</p>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="btn-secondary" onClick={onCancel}>{cancelLabel}</button>
          <button
            className={danger ? 'btn-danger' : 'btn-primary'}
            onClick={onConfirm}
          >{confirmLabel}</button>
        </div>
      </div>
    </Modal>
  )
}
