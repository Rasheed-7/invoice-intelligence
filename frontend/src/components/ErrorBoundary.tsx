import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  /**
   * Name of the section being guarded, shown in the fallback so the user knows
   * *which* panel failed rather than seeing a generic message in an undifferentiated
   * column of cards.
   */
  sectionName: string;
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Isolates a section's render failure from the rest of the page.
 *
 * React unmounts the entire tree when a component throws during render and there
 * is no boundary above it. Every section on the invoice detail page reads
 * `details` blobs straight out of the database, verbatim, so one row written
 * under an older schema — or a partial one — would otherwise blank the whole page
 * and lose the invoice data the reviewer came for.
 *
 * One boundary per section, deliberately. Wrapping the page in a single boundary
 * would convert a broken PO match panel into a blank screen; wrapping each
 * section means a failure is contained to the card that caused it and the
 * remaining evidence stays readable.
 *
 * This is a recovery net, not a reporting tool: the underlying problem is a shape
 * mismatch, which the defensive reads in each section address directly. This
 * boundary catches what those guards miss.
 */
export default class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the failure for a human to chase. Left as a plain console call
    // rather than swallowed: an error boundary that hides its own errors is
    // worse than a crash, because the failure becomes invisible.
    console.error(
      `InvoiceDetailPage: "${this.props.sectionName}" failed to render`,
      error,
      info.componentStack
    );
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) {
      return this.props.children;
    }

    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 p-5">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-amber-800">
          {this.props.sectionName}
        </h2>
        <p className="text-sm font-medium text-amber-900">
          This section failed to load. The rest of the page is unaffected.
        </p>
        <p className="mt-2 text-xs text-amber-800">{error.message}</p>
      </div>
    );
  }
}