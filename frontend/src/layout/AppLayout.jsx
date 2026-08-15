// The application shell.
//
// This is the single highest-leverage piece of the PatternFly refactor:
// one layout route gives all 20 pages a persistent masthead and sidebar.
// Before this, navigation existed only on `/` — every other page
// hand-rolled a local Shell() and a "← Dashboard" back-link.

import { Outlet, useLocation } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { Page, PageSidebar, PageSidebarBody } from "@patternfly/react-core";

import { TOASTER_PROPS } from "../common/toast";
import AppMasthead from "./AppMasthead";
import AppNav from "./AppNav";
import RouteErrorBoundary from "./RouteErrorBoundary";

export default function AppLayout() {
  const { pathname } = useLocation();

  // `isManagedSidebar` lets PF own the open/closed state and wire the
  // masthead's PageToggleButton through context — so neither this
  // component nor AppMasthead needs to thread the state manually.
  const sidebar = (
    <PageSidebar>
      <PageSidebarBody>
        <AppNav />
      </PageSidebarBody>
    </PageSidebar>
  );

  return (
    <>
      <Page
        masthead={<AppMasthead />}
        sidebar={sidebar}
        isManagedSidebar
        defaultManagedSidebarIsOpen
      >
        {/* Keyed by pathname so navigating away resets a tripped
            boundary — otherwise every later page renders the fallback. */}
        <RouteErrorBoundary key={pathname}>
          <Outlet />
        </RouteErrorBoundary>
      </Page>
      <Toaster {...TOASTER_PROPS} />
    </>
  );
}
