import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * A view that throws must not take the shell down (and must not be papered over
 * with a friendly fake screen): the error text is shown, and the rest of the app
 * keeps running.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; label: string }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error(`[omni-ui] ${this.props.label} crashed`, error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="glass hud-corners relative m-3 rounded-2xl p-4">
          <p className="font-display text-[13px] tracking-[0.14em] text-danger uppercase">{this.props.label} failed to render</p>
          <p className="mt-1 text-[11.5px] text-muted">{this.state.error.message}</p>
          <pre className="mt-2 max-h-[220px] overflow-auto rounded-lg border border-line/45 bg-inset/40 p-2 font-mono text-[10px] text-faint">
            {this.state.error.stack ?? "no stack"}
          </pre>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="mt-2 rounded-lg border border-accent/50 bg-accent/10 px-2.5 py-1 text-[11.5px] text-accent"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
