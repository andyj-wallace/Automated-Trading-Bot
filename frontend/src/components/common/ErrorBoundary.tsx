/** Contains a render crash to one section, with a plain message and a retry. */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** What failed, in plain words: "System status". */
  label?: string;
}

export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`${this.props.label ?? "Section"} crashed`, error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="rounded-lg border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-300">
        <p className="font-medium">{this.props.label ?? "This section"} couldn't be shown.</p>
        <p className="mt-1 text-gray-300">The bot itself isn't affected. This is a display problem.</p>
        <button
          onClick={() => this.setState({ failed: false })}
          className="mt-3 px-3 py-1.5 text-sm rounded border border-amber-800 hover:bg-amber-950"
        >
          Try again
        </button>
      </div>
    );
  }
}
