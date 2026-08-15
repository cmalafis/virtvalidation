// Primary navigation.
//
// Ordered by the operator's actual workflow rather than by object type:
// configure the endpoints, discover what's out there, plan and run the
// migration, then verify it. The grouping is the point — a new operator
// should be able to read the sidebar top-to-bottom and understand the
// sequence without opening the guide.
//
// Replaces six unicode-glyph links crammed into the old header
// (◎ vCenters, ⊕ OCP Targets, ⤳ Mappings, ◈ Agent Activity, ⚙ Settings).
//
// Router integration: NavItem styles a valid-element child for us
// (`styleChildren` defaults to true), so we hand it a plain <Link> and
// drive `isActive` from the current location. Detail routes keep their
// section lit — /plans/12 highlights "Migration Plans".

import { Link, useLocation } from "react-router-dom";
import { Nav, NavGroup, NavItem, NavList } from "@patternfly/react-core";

const SECTIONS = [
  {
    title: null,
    items: [{ to: "/", label: "Overview", exact: true }],
  },
  {
    title: "Configure",
    items: [
      { to: "/sources/vcenters", label: "vCenter Sources" },
      { to: "/sources/targets", label: "OCP Targets" },
      { to: "/mappings", label: "Resource Mappings" },
    ],
  },
  {
    title: "Discover",
    items: [
      // /vms/:id is the detail route for an inventory row, so it lights
      // up "Virtual Machines" rather than nothing.
      { to: "/inventory", label: "Virtual Machines", also: ["/vms"] },
      { to: "/design-reviews", label: "Design Reviews" },
    ],
  },
  {
    title: "Migrate",
    items: [
      { to: "/plans", label: "Migration Plans" },
      { to: "/operations", label: "Bulk Operations" },
    ],
  },
  {
    title: "Verify",
    items: [
      { to: "/validations", label: "Validations" },
      { to: "/reports", label: "Reports" },
    ],
  },
  {
    title: "Administration",
    items: [
      { to: "/agent-activity", label: "Agent Activity" },
      { to: "/audit", label: "Audit Log" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

function isPrefixMatch(pathname, base) {
  return pathname === base || pathname.startsWith(`${base}/`);
}

function matches(pathname, item) {
  if (item.exact) return pathname === item.to;
  if (isPrefixMatch(pathname, item.to)) return true;
  return (item.also ?? []).some((base) => isPrefixMatch(pathname, base));
}

export default function AppNav() {
  const { pathname } = useLocation();

  const renderItem = (item) => (
    <NavItem key={item.to} isActive={matches(pathname, item)}>
      <Link to={item.to}>{item.label}</Link>
    </NavItem>
  );

  return (
    <Nav aria-label="Primary navigation">
      <NavList>
        {SECTIONS.map((section) =>
          section.title === null ? (
            section.items.map(renderItem)
          ) : (
            <NavGroup key={section.title} title={section.title}>
              {section.items.map(renderItem)}
            </NavGroup>
          ),
        )}
      </NavList>
    </Nav>
  );
}
