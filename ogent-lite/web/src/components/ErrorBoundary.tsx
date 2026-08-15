import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // This contains component names only; document content is never logged.
    console.error("Ogent interface error", error.name, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-recovery" role="alert">
        <h1>Ogent’s interface needs to recover</h1>
        <p>
          The backend and document were not changed by this display error. Reload
          this workspace to reconnect to its durable state.
        </p>
        <button type="button" onClick={() => window.location.reload()}>
          Reload workspace
        </button>
      </main>
    );
  }
}
