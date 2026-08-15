// Audit log.
//
// Was the dashboard's "audit log" useState tab. Read-only record of
// mutations, newest first. Filters are server-side (the endpoint takes
// `action` and `resource_type`), so they narrow the whole log rather
// than just the loaded page.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bullseye,
  ExpandableSection,
  MenuToggle,
  PageSection,
  Select,
  SelectList,
  SelectOption,
  Skeleton,
  Toolbar,
  ToolbarContent,
  ToolbarFilter,
  ToolbarGroup,
  ToolbarItem,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import { ErrorEmptyState, GuidedEmptyState, NoResultsEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

function SingleSelect({ label, value, options, onChange }) {
  const [open, setOpen] = useState(false);
  return (
    <ToolbarFilter
      labels={value ? [value] : []}
      deleteLabel={() => onChange(null)}
      categoryName={label}
    >
      <Select
        aria-label={label}
        isOpen={open}
        selected={value}
        onOpenChange={setOpen}
        onSelect={(_e, v) => {
          onChange(v === value ? null : String(v));
          setOpen(false);
        }}
        toggle={(ref) => (
          <MenuToggle
            ref={ref}
            onClick={() => setOpen((o) => !o)}
            isExpanded={open}
            style={{ width: 220 }}
          >
            {value || label}
          </MenuToggle>
        )}
      >
        <SelectList>
          {options.length === 0 && <SelectOption isDisabled>No values</SelectOption>}
          {options.map((opt) => (
            <SelectOption key={opt} value={opt} isSelected={opt === value}>
              {opt}
            </SelectOption>
          ))}
        </SelectList>
      </Select>
    </ToolbarFilter>
  );
}

export default function AuditLogPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [action, setAction] = useState(null);
  const [resourceType, setResourceType] = useState(null);

  // Option lists come from what's currently loaded. The endpoint has no
  // facets, so this reflects the visible window rather than the whole
  // table — acceptable for a log view, and it avoids a second round trip.
  const [seen, setSeen] = useState({ actions: [], resourceTypes: [] });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ limit: "200" });
      if (action) qs.set("action", action);
      if (resourceType) qs.set("resource_type", resourceType);
      const data = await fetchJSON(`/api/audit?${qs}`);
      const rows = Array.isArray(data) ? data : [];
      setEntries(rows);
      setError(null);
      // Only widen the option lists — filtering shouldn't shrink the
      // menu you used to get there.
      setSeen((prev) => ({
        actions: [...new Set([...prev.actions, ...rows.map((r) => r.action)])]
          .filter(Boolean)
          .sort(),
        resourceTypes: [
          ...new Set([...prev.resourceTypes, ...rows.map((r) => r.resource_type)]),
        ]
          .filter(Boolean)
          .sort(),
      }));
    } catch (e) {
      setError(e);
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [action, resourceType]);

  useEffect(() => {
    load();
  }, [load]);

  const hasFilters = Boolean(action || resourceType);
  const clearAll = () => {
    setAction(null);
    setResourceType(null);
  };

  const toolbar = useMemo(
    () => (
      <Toolbar id="audit-toolbar" clearAllFilters={clearAll}>
        <ToolbarContent>
          <ToolbarGroup variant="filter-group">
            <ToolbarItem>
              <SingleSelect
                label="Action"
                value={action}
                options={seen.actions}
                onChange={setAction}
              />
            </ToolbarItem>
            <ToolbarItem>
              <SingleSelect
                label="Resource type"
                value={resourceType}
                options={seen.resourceTypes}
                onChange={setResourceType}
              />
            </ToolbarItem>
          </ToolbarGroup>
        </ToolbarContent>
      </Toolbar>
    ),
    [action, resourceType, seen],
  );

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && entries.length === 0) {
      return Array.from({ length: 8 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={5}>
            <Skeleton screenreaderText="Loading audit log" />
          </Td>
        </Tr>
      ));
    }

    if (entries.length === 0) {
      return (
        <Tr>
          <Td colSpan={5}>
            <Bullseye>
              {hasFilters ? (
                <NoResultsEmptyState onClearFilters={clearAll} />
              ) : (
                <GuidedEmptyState
                  title="No audit entries yet"
                  body="Every mutation VirtValidate makes is recorded here — enrollments, plan generation, baseline captures and validations. The log fills in as you use the product."
                  primary={{ label: "Go to inventory", to: "/inventory" }}
                />
              )}
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return entries.map((e) => {
      const details = e?.details ?? {};
      const hasDetails = Object.keys(details).length > 0;
      return (
        <Tr key={e.id}>
          <Td dataLabel="Time">
            {e?.timestamp ? new Date(e.timestamp).toLocaleString() : "—"}
          </Td>
          <Td dataLabel="Action">{e?.action ?? "—"}</Td>
          <Td dataLabel="Actor">{e?.actor ?? "—"}</Td>
          <Td dataLabel="Resource">
            {e?.resource_type ? `${e.resource_type}${e.resource_id ? ` #${e.resource_id}` : ""}` : "—"}
          </Td>
          <Td dataLabel="Details">
            {hasDetails ? (
              <ExpandableSection toggleText="View" isIndented>
                <pre
                  className="pf-v6-u-font-size-sm"
                  style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}
                >
                  {JSON.stringify(details, null, 2)}
                </pre>
              </ExpandableSection>
            ) : (
              "—"
            )}
          </Td>
        </Tr>
      );
    });
  };

  return (
    <PageFrame
      title="Audit log"
      description="Every mutation VirtValidate has made, newest first."
      breadcrumbs={[{ label: "Administration" }, { label: "Audit log" }]}
    >
      <PageSection hasBodyWrapper={false}>
        {toolbar}
        <Table aria-label="Audit log" variant="compact">
          <Thead>
            <Tr>
              <Th width={20}>Time</Th>
              <Th width={20}>Action</Th>
              <Th width={15}>Actor</Th>
              <Th width={20}>Resource</Th>
              <Th width={25}>Details</Th>
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>
    </PageFrame>
  );
}
