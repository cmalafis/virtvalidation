// Virtual Machines — the primary working surface.
//
// Rebuilt on PatternFly's composable Table + Toolbar. Keeps the same
// server contract as the old InventoryTable (GET /api/vms with skip /
// limit / sort_by / sort_order / search + repeated filter params, plus
// GET /api/vms/facets for the filter counts), so this is a presentation
// change only.
//
// URL-synced state is preserved: page, sort and every filter live in the
// query string so an operator can bookmark or share a filtered view.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Bullseye,
  Button,
  Divider,
  MenuToggle,
  Pagination,
  Select,
  SelectList,
  SelectOption,
  PageSection,
  SearchInput,
  Skeleton,
  Toolbar,
  ToolbarContent,
  ToolbarFilter,
  ToolbarGroup,
  ToolbarItem,
  ToolbarToggleGroup,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import FilterIcon from "@patternfly/react-icons/dist/esm/icons/filter-icon";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import {
  ErrorEmptyState,
  GuidedEmptyState,
  NO_VMS,
  NoResultsEmptyState,
} from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";
import AddVMModal from "./inventory/AddVMModal";

// Explicit widths: without them the sort carets steal enough room from
// the flexible columns that "Environment" renders as "Enviro...".
const COLUMNS = [
  { key: "name", label: "Name", sortable: true, width: 20 },
  { key: "status", label: "Status", sortable: true, width: 15 },
  { key: "lifecycle_state", label: "Plan", sortable: true, width: 10 },
  { key: "environment", label: "Environment", sortable: true, width: 15 },
  { key: "os_family", label: "OS", sortable: true, width: 10 },
  { key: "ip_address", label: "IP address", sortable: false, width: 15 },
  { key: "target_namespace", label: "Namespace", sortable: false, width: 15 },
];

// Filter dimensions the backend exposes as repeatable query params and
// reports counts for via /api/vms/facets.
const FILTERS = [
  { key: "status", label: "Status" },
  { key: "lifecycle_state", label: "Plan state" },
  { key: "environment", label: "Environment" },
  { key: "os_family", label: "OS family" },
];

const DEFAULTS = { page: 1, perPage: 20, sortBy: "name", sortOrder: "asc" };
const SEARCH_DEBOUNCE_MS = 300;

/** Read all state out of the query string so URLs are shareable. */
function readState(params) {
  const multi = {};
  for (const { key } of FILTERS) multi[key] = params.getAll(key);
  return {
    page: Number(params.get("page")) || DEFAULTS.page,
    perPage: Number(params.get("perPage")) || DEFAULTS.perPage,
    sortBy: params.get("sort_by") || DEFAULTS.sortBy,
    sortOrder: params.get("sort_order") || DEFAULTS.sortOrder,
    search: params.get("search") || "",
    filters: multi,
  };
}

function buildQuery(state, { forFacets = false } = {}) {
  const qs = new URLSearchParams();
  if (!forFacets) {
    qs.set("skip", String((state.page - 1) * state.perPage));
    qs.set("limit", String(state.perPage));
    qs.set("sort_by", state.sortBy);
    qs.set("sort_order", state.sortOrder);
    // Facet counts stay stable while typing if the search is omitted.
    if (state.search) qs.set("search", state.search);
  }
  for (const { key } of FILTERS) {
    for (const value of state.filters[key] ?? []) qs.append(key, value);
  }
  return qs.toString();
}

// `facets` arrive from the API as a {value: count} map per dimension,
// so entries() gives the [value, count] pairs rendered below.
function FacetSelect({ label, name, selected, counts, onChange }) {
  const [open, setOpen] = useState(false);
  const chosen = selected ?? [];
  const options = Object.entries(counts ?? {});

  const toggle = (value) =>
    onChange(
      chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value],
    );

  return (
    <ToolbarFilter
      labels={chosen}
      deleteLabel={(_cat, chip) => toggle(String(chip))}
      deleteLabelGroup={() => onChange([])}
      categoryName={label}
    >
      <Select
        aria-label={label}
        role="menu"
        isOpen={open}
        selected={chosen}
        onSelect={(_e, value) => toggle(String(value))}
        onOpenChange={setOpen}
        toggle={(ref) => (
          <MenuToggle
            ref={ref}
            onClick={() => setOpen((v) => !v)}
            isExpanded={open}
            badge={chosen.length > 0 ? chosen.length : undefined}
            style={{ width: 200 }}
          >
            {label}
          </MenuToggle>
        )}
      >
        <SelectList>
          {options.length === 0 && <SelectOption isDisabled>No values</SelectOption>}
          {options.map(([value, count]) => (
            <SelectOption
              key={`${name}-${value}`}
              value={value}
              hasCheckbox
              isSelected={chosen.includes(value)}
            >
              {`${value} (${count})`}
            </SelectOption>
          ))}
        </SelectList>
      </Select>
    </ToolbarFilter>
  );
}

export default function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = useMemo(() => readState(searchParams), [searchParams]);

  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [addOpen, setAddOpen] = useState(false);

  // Local mirror of the search box so typing feels instant while the
  // committed value (which drives fetches) is debounced.
  const [searchDraft, setSearchDraft] = useState(state.search);

  const patch = useCallback(
    (changes) => {
      const next = new URLSearchParams(searchParams);
      for (const [key, value] of Object.entries(changes)) {
        if (key === "filters") {
          for (const { key: fk } of FILTERS) {
            next.delete(fk);
            for (const v of value[fk] ?? []) next.append(fk, v);
          }
        } else if (value === null || value === "" || value === undefined) {
          next.delete(key);
        } else {
          next.set(key, String(value));
        }
      }
      // Any change other than paging returns to page 1 — otherwise a
      // filter that shrinks the result set strands the operator on an
      // empty page N.
      if (!("page" in changes)) next.set("page", "1");
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  useEffect(() => {
    setSearchDraft(state.search);
  }, [state.search]);

  useEffect(() => {
    if (searchDraft === state.search) return undefined;
    const t = setTimeout(() => patch({ search: searchDraft }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchDraft, state.search, patch]);

  const queryKey = buildQuery(state);
  const facetKey = buildQuery(state, { forFacets: true });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON(`/api/vms?${queryKey}`);
      setItems(asArray(data));
      setTotal(data?.total ?? 0);
      setError(null);
    } catch (e) {
      setError(e);
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [queryKey]);

  useEffect(() => {
    load();
  }, [load]);

  const loadFacets = useCallback(
    (isCancelled = () => false) =>
      fetchJSON(`/api/vms/facets?${facetKey}`)
        .then((data) => {
          if (!isCancelled()) setFacets(data ?? {});
        })
        // Facets are decoration; losing them shouldn't surface an error
        // over a table that loaded fine.
        .catch(() => {}),
    [facetKey],
  );

  useEffect(() => {
    let cancelled = false;
    loadFacets(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [loadFacets]);

  const activeFilterCount = FILTERS.reduce(
    (n, f) => n + (state.filters[f.key]?.length ?? 0),
    0,
  );
  const hasQuery = activeFilterCount > 0 || Boolean(state.search);

  const clearAll = () =>
    patch({
      search: null,
      filters: Object.fromEntries(FILTERS.map((f) => [f.key, []])),
    });

  const onSort = (_e, index, direction) => {
    const col = COLUMNS.filter((c) => c.sortable)[index];
    if (col) patch({ sort_by: col.key, sort_order: direction });
  };

  const sortableColumns = COLUMNS.filter((c) => c.sortable);
  const activeSortIndex = sortableColumns.findIndex((c) => c.key === state.sortBy);

  const allOnPageSelected =
    items.length > 0 && items.every((vm) => selected.has(vm.id));

  const toggleAll = (isSelecting) => {
    const next = new Set(selected);
    for (const vm of items) {
      if (isSelecting) next.add(vm.id);
      else next.delete(vm.id);
    }
    setSelected(next);
  };

  const toggleRow = (id, isSelecting) => {
    const next = new Set(selected);
    if (isSelecting) next.add(id);
    else next.delete(id);
    setSelected(next);
  };

  const captureSelected = async () => {
    const ids = [...selected];
    const results = await Promise.allSettled(
      ids.map((id) => fetchJSON(`/api/vms/${id}/capture`, { method: "POST" })),
    );
    const failed = results.filter((r) => r.status === "rejected").length;
    if (failed === 0) {
      toast.success(`Baseline capture started for ${ids.length} VM(s)`);
    } else {
      // Partial success is the common case at scale — say how many, so
      // the operator knows whether to retry all or investigate a few.
      toast.error(`${failed} of ${ids.length} capture request(s) failed`);
    }
    setSelected(new Set());
    load();
  };

  const pagination = (variant) => (
    <Pagination
      itemCount={total}
      page={state.page}
      perPage={state.perPage}
      onSetPage={(_e, page) => patch({ page })}
      onPerPageSelect={(_e, perPage) => patch({ perPage, page: 1 })}
      variant={variant}
      isCompact={variant === "top"}
    />
  );

  const toolbar = (
    <Toolbar
      id="inventory-toolbar"
      clearAllFilters={clearAll}
      collapseListedFiltersBreakpoint="xl"
    >
      <ToolbarContent>
        <ToolbarItem variant="search-filter">
          <SearchInput
            aria-label="Search virtual machines"
            placeholder="Search by name, IP, or owner"
            value={searchDraft}
            onChange={(_e, v) => setSearchDraft(v)}
            onClear={() => setSearchDraft("")}
          />
        </ToolbarItem>

        <ToolbarToggleGroup toggleIcon={<FilterIcon />} breakpoint="xl">
          <ToolbarGroup variant="filter-group">
            {FILTERS.map((f) => (
              <FacetSelect
                key={f.key}
                name={f.key}
                label={f.label}
                selected={state.filters[f.key]}
                counts={facets?.[f.key]}
                onChange={(values) =>
                  patch({ filters: { ...state.filters, [f.key]: values } })
                }
              />
            ))}
          </ToolbarGroup>
        </ToolbarToggleGroup>

        <ToolbarGroup variant="action-group">
          <ToolbarItem>
            <Button
              variant="secondary"
              isDisabled={selected.size === 0}
              onClick={captureSelected}
            >
              Capture baseline{selected.size > 0 ? ` (${selected.size})` : ""}
            </Button>
          </ToolbarItem>
          <ToolbarItem>
            <Button variant="secondary" onClick={() => setAddOpen(true)}>
              Add VM
            </Button>
          </ToolbarItem>
          <ToolbarItem>
            <Button variant="primary" component={(p) => <Link to="/rvtools/upload" {...p} />}>
              Import VMs
            </Button>
          </ToolbarItem>
        </ToolbarGroup>

        <ToolbarItem variant="pagination" align={{ default: "alignEnd" }}>
          {pagination("top")}
        </ToolbarItem>
      </ToolbarContent>
    </Toolbar>
  );

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && items.length === 0) {
      return (
        <>
          {Array.from({ length: 6 }).map((_, i) => (
            <Tr key={`sk-${i}`}>
              <Td colSpan={COLUMNS.length + 1}>
                <Skeleton screenreaderText="Loading virtual machines" />
              </Td>
            </Tr>
          ))}
        </>
      );
    }

    if (items.length === 0) {
      return (
        <Tr>
          <Td colSpan={COLUMNS.length + 1}>
            <Bullseye>
              {hasQuery ? (
                <NoResultsEmptyState onClearFilters={clearAll} />
              ) : (
                <GuidedEmptyState
                  title={NO_VMS.title}
                  body={NO_VMS.body}
                  primary={{ label: "Import VMs", to: "/rvtools/upload" }}
                  secondary={{ label: "Add a VM manually", onClick: () => setAddOpen(true) }}
                />
              )}
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return items.map((vm) => (
      <Tr key={vm.id}>
        <Td
          select={{
            rowIndex: vm.id,
            isSelected: selected.has(vm.id),
            onSelect: (_e, isSelecting) => toggleRow(vm.id, isSelecting),
          }}
        />
        <Td dataLabel="Name">
          <Link to={`/vms/${vm.id}`}>{vm.name}</Link>
        </Td>
        <Td dataLabel="Status">
          <StatusLabel kind="status" value={vm.status} />
        </Td>
        <Td dataLabel="Plan">
          <StatusLabel kind="lifecycle" value={vm.lifecycle_state} />
        </Td>
        <Td dataLabel="Environment">{vm.environment ?? "—"}</Td>
        <Td dataLabel="OS">{vm.os_family ?? "—"}</Td>
        <Td dataLabel="IP address">{vm.ip_address ?? "—"}</Td>
        <Td dataLabel="Target namespace">{vm.target_namespace ?? "—"}</Td>
      </Tr>
    ));
  };

  return (
    <PageFrame
      title="Virtual machines"
      description="Every VM VirtValidate knows about, and where each one is in the migration."
      breadcrumbs={[{ label: "Discover" }, { label: "Virtual machines" }]}
    >
      <PageSection hasBodyWrapper={false}>
        {toolbar}
        <Table aria-label="Virtual machines" variant="compact">
          <Thead>
            <Tr>
              <Th
                select={{
                  onSelect: (_e, isSelecting) => toggleAll(isSelecting),
                  isSelected: allOnPageSelected,
                }}
                aria-label="Select all on page"
              />
              {COLUMNS.map((col) => (
                <Th
                  key={col.key}
                  width={col.width}
                  sort={
                    col.sortable
                      ? {
                          sortBy: {
                            index: activeSortIndex,
                            direction: state.sortOrder,
                          },
                          onSort,
                          columnIndex: sortableColumns.findIndex((c) => c.key === col.key),
                        }
                      : undefined
                  }
                >
                  {col.label}
                </Th>
              ))}
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
        <Divider />
        {pagination("bottom")}
      </PageSection>
      <AddVMModal
        isOpen={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={() => {
          // Re-read rather than splicing the new row in: the listing is
          // sorted + filtered server-side, so the VM may not belong on
          // this page at all.
          load();
          loadFacets();
        }}
      />
    </PageFrame>
  );
}
