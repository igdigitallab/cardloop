import { api } from '../api'
import { EditableMarkdown } from '../components/EditableMarkdown'

interface Props {
  projectId: string
}

export function ClaudeMdTab({ projectId }: Props) {
  return (
    <EditableMarkdown
      projectId={projectId}
      load={api.claudeMd}
      save={api.saveClaudeMd}
      spinnerLabel="Загрузка CLAUDE.md..."
      emptyLabel="Нет CLAUDE.md для этого проекта"
    />
  )
}
