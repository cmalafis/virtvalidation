// Route-level error boundary.
//
// CLAUDE.md requires pages be wrapped in error boundaries, but only
// VMDetail ever had one (VMDetailErrorBoundary). Mounting this once
// around the layout's <Outlet/> covers every route, so a render crash in
// one page shows a recoverable message inside the shell instead of
// blanking the whole app to a white screen.
//
// Keyed by pathname in AppLayout so navigating away clears the error —
// otherwise React keeps the boundary tripped and every subsequent page
// renders the fallback.

import { Component } from "react";
import {
  Button,
  EmptyState,
  EmptyStateActions,
  EmptyStateBody,
  EmptyStateFooter,
  PageSection,
} from "@patternfly/react-core";
import ExclamationCircleIcon from "@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon";

export default class RouteErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // No telemetry sink in an air-gapped appliance — the browser console
    // is where an operator (or a support bundle) will look.
    console.error("Unhandled render error:", error, info?.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <PageSection>
        <EmptyState
          status="danger"
          icon={ExclamationCircleIcon}
          titleText="This page failed to render"
          headingLevel="h2"
        >
          <EmptyStateBody>
            {error?.message || "An unexpected error occurred."} Reloading may
            clear it. If it persists, check the browser console for the full
            stack trace.
          </EmptyStateBody>
          <EmptyStateFooter>
            <EmptyStateActions>
              <Button variant="primary" onClick={() => this.setState({ error: null })}>
                Try again
              </Button>
              <Button variant="link" onClick={() => window.location.reload()}>
                Reload page
              </Button>
            </EmptyStateActions>
          </EmptyStateFooter>
        </EmptyState>
      </PageSection>
    );
  }
}
