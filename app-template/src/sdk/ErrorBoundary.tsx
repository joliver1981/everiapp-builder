import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * AppErrorBoundary — a render-error firewall for AIHub-generated apps.
 *
 * React unmounts the whole tree when a render/lifecycle error escapes, which
 * shows the user a blank white screen with no explanation. Wrapping the app in
 * this boundary turns that into a friendly, recoverable panel and surfaces the
 * error to the console (so the platform's runtime verifier and the browser dev
 * tools both see it) and to an optional `onError` callback (wire it to bug
 * reporting / telemetry).
 *
 * Note: error boundaries only catch errors thrown during render, in lifecycle
 * methods, and in constructors of the tree below them. They do NOT catch errors
 * in event handlers, async code, or SSR — use try/catch for those.
 */
export interface AppErrorBoundaryProps {
  children: ReactNode
  /**
   * Custom fallback renderer. Receives the caught error and a `reset()` that
   * clears the boundary so the children can try to re-render. When omitted, a
   * built-in panel is shown.
   */
  fallback?: (error: Error, reset: () => void) => ReactNode
  /**
   * Called once per caught error. Use it to report to the platform bug widget
   * or your own telemetry. It must never throw — exceptions here are swallowed.
   */
  onError?: (error: Error, info: ErrorInfo) => void
  /**
   * Optional label for the failing area, shown in the default panel
   * (e.g. "Sales chart"). Helps users describe what broke.
   */
  label?: string
}

interface AppErrorBoundaryState {
  error: Error | null
}

export class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Always log — this is what the headless runtime probe listens for, and
    // what a developer sees in dev tools.
    // eslint-disable-next-line no-console
    console.error('[AIHub] Unhandled render error:', error, info.componentStack)
    try {
      this.props.onError?.(error, info)
    } catch {
      /* an error reporter must never crash the error boundary */
    }
  }

  reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (error) {
      if (this.props.fallback) return this.props.fallback(error, this.reset)
      return (
        <DefaultErrorFallback
          error={error}
          reset={this.reset}
          label={this.props.label}
        />
      )
    }
    return this.props.children
  }
}

function DefaultErrorFallback({
  error,
  reset,
  label,
}: {
  error: Error
  reset: () => void
  label?: string
}) {
  return (
    <div
      role="alert"
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '2rem',
        boxSizing: 'border-box',
        fontFamily:
          'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
        background: '#09090b',
        color: '#e4e4e7',
      }}
    >
      <div style={{ maxWidth: '32rem', width: '100%', textAlign: 'center' }}>
        <div
          aria-hidden
          style={{
            width: '3rem',
            height: '3rem',
            margin: '0 auto 1rem',
            borderRadius: '9999px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(239, 68, 68, 0.12)',
            color: '#f87171',
            fontSize: '1.5rem',
            lineHeight: 1,
          }}
        >
          !
        </div>
        <h1 style={{ fontSize: '1.25rem', fontWeight: 600, margin: '0 0 0.5rem' }}>
          {label ? `${label} hit a snag` : 'Something went wrong'}
        </h1>
        <p
          style={{
            fontSize: '0.875rem',
            color: '#a1a1aa',
            margin: '0 0 1rem',
            lineHeight: 1.5,
          }}
        >
          This part of the app ran into an unexpected error. You can try again,
          or reload the page.
        </p>
        <pre
          style={{
            textAlign: 'left',
            fontSize: '0.75rem',
            color: '#fca5a5',
            background: 'rgba(127, 29, 29, 0.18)',
            border: '1px solid rgba(248, 113, 113, 0.25)',
            borderRadius: '0.5rem',
            padding: '0.75rem',
            margin: '0 0 1.25rem',
            overflowX: 'auto',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {error.message || String(error)}
        </pre>
        <div
          style={{
            display: 'flex',
            gap: '0.5rem',
            justifyContent: 'center',
            flexWrap: 'wrap',
          }}
        >
          <button
            type="button"
            onClick={reset}
            style={{
              cursor: 'pointer',
              border: 'none',
              borderRadius: '0.5rem',
              padding: '0.5rem 1rem',
              fontSize: '0.875rem',
              fontWeight: 500,
              color: '#fafafa',
              background: '#4f46e5',
            }}
          >
            Try again
          </button>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{
              cursor: 'pointer',
              borderRadius: '0.5rem',
              padding: '0.5rem 1rem',
              fontSize: '0.875rem',
              fontWeight: 500,
              color: '#e4e4e7',
              background: 'transparent',
              border: '1px solid #3f3f46',
            }}
          >
            Reload page
          </button>
        </div>
      </div>
    </div>
  )
}

export default AppErrorBoundary
