import { api } from '../api'
import { EditableMarkdown } from '../components/EditableMarkdown'

interface Props {
  projectId: string
}

export function ReadmeTab({ projectId }: Props) {
  return (
    <EditableMarkdown
      projectId={projectId}
      load={api.readme}
      save={api.saveReadme}
      spinnerLabel="Загрузка README..."
      emptyLabel="Нет README для этого проекта"
    />
  )
}
