import { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** Optional label shown in the fallback (e.g. tab name) */
  label?: string
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary]', this.props.label ?? '', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="error-boundary-fallback">
          <div className="error-boundary-icon">⚠</div>
          <div className="error-boundary-msg">
            Something broke{this.props.label ? ` in "${this.props.label}"` : ''}.
          </div>
          {this.state.error && (
            <pre className="error-boundary-detail">{this.state.error.message}</pre>
          )}
          <button
            className="btn-primary"
            onClick={() => { this.setState({ hasError: false, error: null }) }}
          >
            Try again
          </button>
          <button
            className="btn-secondary"
            onClick={() => window.location.reload()}
            style={{ marginLeft: 8 }}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
