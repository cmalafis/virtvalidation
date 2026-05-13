// Paginated inventory table for the dashboard's inventory tab.
//
// Owns: page/sort/filter state, URL state sync, /api/vms fetch, facet
//       fetch, selection state, page-level + bulk actions (Delete All,
//       Refresh, Export CSV).
// Receives: per-row action callbacks (onCapture, onEdit, onDelete,
//       onAddVM, onBulkImport) — the parent already has these wired
//       to the existing modals and audit/refresh flows, so we don't
//       duplicate them here.
//
// Pagination contract from /api/vms — {items, total, skip, limit}.
// `total` is post-filter / pre-pagination so the pager's "X to Y of Z"
// line can render without a second round-trip.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import toast from "react-hot-toast";

import { fetchJSON } from "../utils/fetchJSON";

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250];

const STATUS_OPTIONS = [
  { value: "discovered", label: "Discovered" },
  { value: "baseline_captured", label: "Baseline Captured" },
  { value: "migrated", label: "Migrated" },
  { value: "validated", label: "Validated" },
  { value: "failed", label: "Failed" },
];

// Plan-membership lifecycle. Parallel state machine to VMStatus —
// "Plan" filter answers "is this VM available to add to a new plan?"
// while "Status" answers the validation lifecycle.
const LIFECYCLE_OPTIONS = [
  { value: "available", label: "Available" },
  { value: "planned", label: "Planned" },
  { value: "migrated", label: "Migrated" },
  { value: "rolled_back", label: "Rolled Back" },
  { value: "unmanageable", label: "Unmanageable" },
];

const STATUS_COLOR = {
  discovered: "#9ca3ff",
  baseline_captured: "#4488ff",
  migrated: "#ffaa00",
  validated: "#00ff88",
  failed: "#ff3355",
};

// Plan-membership lifecycle colors. Distinct palette from VM.status
// (operational lifecycle) so the two pills stay legible side-by-side.
const LIFECYCLE_COLOR = {
  available: "#9ca3ff",
  planned: "#ffaa00",
  migrated: "#00ff88",
  rolled_back: "#ff9933",
  unmanageable: "#666688",
};

// Sort dropdowns operate on the same column ids the backend accepts.
const SORTABLE_COLUMNS = [
  { id: "name", label: "VM Name" },
  { id: "status", label: "Status" },
  { id: "lifecycle_state", label: "Plan" },
  { id: "environment", label: "Environment" },
  { id: "os_family", label: "OS Family" },
  { id: "application_hint", label: "Application" },
  { id: "source_vcenter_id", label: "vCenter" },
  { id: "updated_at", label: "Last Modified" },
];

// Debounce for the search input. 300ms is short enough that the
// operator perceives instant filtering but long enough that we don't
// spam the backend on every keystroke during fast typing.
const SEARCH_DEBOUNCE_MS = 300;

const INPUT_STYLE = {
  background: "#0a0a18",
  border: "1px solid #1a1a2e",
  color: "#eeeeff",
  padding: "8px 10px",
  fontSize: 12,
  fontFamily: "'Share Tech Mono', monospace",
  outline: "none",
};

const SECONDARY_BTN_STYLE = {
  background: "transparent",
  border: "1px solid #3a3a55",
  color: "#aaaacc",
  padding: "8px 14px",
  fontSize: 11,
  fontFamily: "'Barlow', sans-serif",
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  fontWeight: 700,
  cursor: "pointer",
};

const DANGER_BTN_STYLE = {
  ...SECONDARY_BTN_STYLE,
  border: "1px solid #ff5577",
  color: "#ff99aa",
};

const PRIMARY_BTN_STYLE = {
  ...SECONDARY_BTN_STYLE,
  border: "1px solid #4488ff",
  color: "#aaccff",
};

function StatusPill({ status }) {
  const color = STATUS_COLOR[status] || "#9ca3ff";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "3px 10px",
      border: `1px solid ${color}55`,
      background: `${color}11`,
      color, fontSize: 11,
      fontFamily: "'Share Tech Mono', monospace",
      letterSpacing: "0.04em", fontWeight: 700,
      textTransform: "uppercase",
    }}>
      <span style={{
        width: 6, height: 6, borderRadius: "50%",
        background: color, boxShadow: `0 0 6px ${color}`,
      }} />
      {status}
    </span>
  );
}

function LifecyclePill({ state }) {
  const color = LIFECYCLE_COLOR[state] || "#888899";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "3px 10px",
      border: `1px solid ${color}55`,
      background: `${color}11`,
      color, fontSize: 11,
      fontFamily: "'Share Tech Mono', monospace",
      letterSpacing: "0.04em", fontWeight: 700,
      textTransform: "uppercase",
    }}>
      {state || "—"}
    </span>
  );
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  return v;
}

function formatTimestamp(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").slice(0, 16);
}

// Parses URL search params into the inventory query state. The URL is
// the source of truth so reload + share + back-button all work.
function readStateFromUrl(searchParams) {
  const arr = (key) => searchParams.getAll(key).filter(Boolean);
  return {
    page: Math.max(0, parseInt(searchParams.get("page") || "0", 10) || 0),
    pageSize: (() => {
      const raw = parseInt(searchParams.get("pageSize") || "50", 10);
      return PAGE_SIZE_OPTIONS.includes(raw) ? raw : 50;
    })(),
    sortBy: searchParams.get("sortBy") || "name",
    sortOrder: searchParams.get("sortOrder") === "desc" ? "desc" : "asc",
    search: searchParams.get("search") || "",
    status: arr("status"),
    lifecycle_state: arr("lifecycle_state"),
    environment: arr("environment"),
    os_family: arr("os_family"),
    application_hint: arr("application_hint"),
    vcenter_source_id: arr("vcenter_source_id"),
  };
}

function writeStateToUrl(state) {
  const params = new URLSearchParams();
  if (state.page > 0) params.set("page", String(state.page));
  if (state.pageSize !== 50) params.set("pageSize", String(state.pageSize));
  if (state.sortBy !== "name") params.set("sortBy", state.sortBy);
  if (state.sortOrder !== "asc") params.set("sortOrder", state.sortOrder);
  if (state.search) params.set("search", state.search);
  for (const [key, values] of [
    ["status", state.status],
    ["lifecycle_state", state.lifecycle_state],
    ["environment", state.environment],
    ["os_family", state.os_family],
    ["application_hint", state.application_hint],
    ["vcenter_source_id", state.vcenter_source_id],
  ]) {
    for (const v of values || []) params.append(key, v);
  }
  return params;
}

function buildQueryString(state) {
  const params = new URLSearchParams();
  params.set("skip", String(state.page * state.pageSize));
  params.set("limit", String(state.pageSize));
  params.set("sort_by", state.sortBy);
  params.set("sort_order", state.sortOrder);
  if (state.search) params.set("search", state.search);
  for (const [key, values] of [
    ["status", state.status],
    ["lifecycle_state", state.lifecycle_state],
    ["environment", state.environment],
    ["os_family", state.os_family],
    ["application_hint", state.application_hint],
    ["vcenter_source_id", state.vcenter_source_id],
  ]) {
    for (const v of values || []) params.append(key, v);
  }
  return params.toString();
}

// "Delete all" confirmation modal. Requires literal "DELETE ALL" so
// the operator doesn't muscle-memory their way through it. Federal
// customers' tabletop exercises specifically test against this kind
// of confirmation gating.
function DeleteAllModal({ open, totalLabel, filtersDescription, busy, onClose, onConfirm }) {
  const [typed, setTyped] = useState("");
  useEffect(() => { if (!open) setTyped(""); }, [open]);
  if (!open) return null;
  const phrase = "DELETE ALL";
  const enabled = typed === phrase && !busy;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 1000,
    }}>
      <div style={{
        background: "#0a0a18", border: "1px solid #ff5577",
        padding: 28, width: 520, maxWidth: "95vw",
      }}>
        <div style={{
          fontSize: 18, color: "#ffccdd", fontFamily: "'Barlow', sans-serif",
          fontWeight: 700, marginBottom: 12,
        }}>{totalLabel}</div>
        <div style={{
          fontSize: 13, color: "#aaaacc", marginBottom: 18,
          fontFamily: "'Barlow', sans-serif", lineHeight: 1.55,
        }}>
          This permanently removes the listed VMs plus their baselines,
          validation history, and snapshots. The action cannot be undone.
          <br /><br />
          <strong style={{ color: "#eeeeff" }}>Filter scope:</strong> {filtersDescription}
        </div>
        <label style={{
          display: "block", fontSize: 12, color: "#aaaacc",
          fontFamily: "'Share Tech Mono', monospace", marginBottom: 6,
          letterSpacing: "0.04em",
        }}>
          Type <code style={{ color: "#ff99aa" }}>{phrase}</code> to confirm:
        </label>
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={phrase}
          autoFocus
          style={{ ...INPUT_STYLE, width: "100%", marginBottom: 18 }}
        />
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} disabled={busy} style={SECONDARY_BTN_STYLE}>
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={!enabled}
            style={{
              ...DANGER_BTN_STYLE,
              opacity: enabled ? 1 : 0.4,
              cursor: enabled ? "pointer" : "not-allowed",
            }}>
            {busy ? "Deleting…" : `Delete ${totalLabel.includes("ALL") ? "All" : "Filtered"}`}
          </button>
        </div>
      </div>
    </div>
  );
}

// Generic multi-select facet dropdown. Closes on outside click.
function FacetDropdown({ label, options, selected, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);
  const summary = selected.length === 0
    ? `${label}: all`
    : `${label}: ${selected.length} selected`;
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          ...INPUT_STYLE,
          cursor: "pointer", minWidth: 160, textAlign: "left",
          display: "inline-flex", alignItems: "center", justifyContent: "space-between",
          gap: 8,
        }}>
        <span>{summary}</span>
        <span style={{ color: "#777799" }}>{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", left: 0, marginTop: 4,
          background: "#0a0a18", border: "1px solid #1a1a2e",
          minWidth: 220, maxHeight: 280, overflowY: "auto",
          zIndex: 50, padding: 6,
        }}>
          {options.length === 0 ? (
            <div style={{ padding: 12, color: "#777799", fontSize: 12 }}>
              No values
            </div>
          ) : (
            options.map((opt) => {
              const checked = selected.includes(opt.value);
              return (
                <label
                  key={opt.value}
                  style={{
                    display: "flex", alignItems: "center", gap: 8,
                    padding: "6px 10px", cursor: "pointer",
                    fontSize: 12, color: "#eeeeff",
                    fontFamily: "'Share Tech Mono', monospace",
                  }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => {
                      const next = checked
                        ? selected.filter((v) => v !== opt.value)
                        : [...selected, opt.value];
                      onChange(next);
                    }}
                    style={{ accentColor: "#4488ff" }}
                  />
                  <span style={{ flex: 1 }}>{opt.label}</span>
                  {typeof opt.count === "number" && (
                    <span style={{ color: "#777799", fontSize: 11 }}>{opt.count}</span>
                  )}
                </label>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

export default function InventoryTable({
  onAddVM,
  onCapture,
  onEdit,
  onDelete,
  onBulkDelete,
  onBulkImport,
  onRevertToVMware,
  onMakeAvailable,
  vcenterSources = [],
  refreshSignal = 0,
  onMutate,
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const initial = useMemo(() => readStateFromUrl(searchParams), []);  // eslint-disable-line react-hooks/exhaustive-deps

  const [page, setPage] = useState(initial.page);
  const [pageSize, setPageSize] = useState(initial.pageSize);
  const [sortBy, setSortBy] = useState(initial.sortBy);
  const [sortOrder, setSortOrder] = useState(initial.sortOrder);
  const [searchInput, setSearchInput] = useState(initial.search);
  const [search, setSearch] = useState(initial.search);
  const [statusFilter, setStatusFilter] = useState(initial.status);
  const [lifecycleStateFilter, setLifecycleStateFilter] = useState(initial.lifecycle_state);
  const [environmentFilter, setEnvironmentFilter] = useState(initial.environment);
  const [osFamilyFilter, setOsFamilyFilter] = useState(initial.os_family);
  const [applicationFilter, setApplicationFilter] = useState(initial.application_hint);
  const [vcenterFilter, setVcenterFilter] = useState(initial.vcenter_source_id);

  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [facets, setFacets] = useState(null);

  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [deleteAllOpen, setDeleteAllOpen] = useState(false);
  const [deletingAll, setDeletingAll] = useState(false);

  // Debounce raw input → committed search.
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchInput]);

  // Reset to first page whenever filters change. Without this the
  // user could be on page 5 of an unfiltered list, apply a filter,
  // and land on an empty page 5.
  useEffect(() => {
    setPage(0);
  }, [
    search, sortBy, sortOrder, pageSize,
    statusFilter, lifecycleStateFilter, environmentFilter, osFamilyFilter,
    applicationFilter, vcenterFilter,
  ]);

  // Build the query state once per dependent value change.
  const queryState = useMemo(() => ({
    page, pageSize, sortBy, sortOrder, search,
    status: statusFilter, lifecycle_state: lifecycleStateFilter,
    environment: environmentFilter,
    os_family: osFamilyFilter, application_hint: applicationFilter,
    vcenter_source_id: vcenterFilter,
  }), [
    page, pageSize, sortBy, sortOrder, search,
    statusFilter, lifecycleStateFilter, environmentFilter, osFamilyFilter,
    applicationFilter, vcenterFilter,
  ]);

  // URL state sync — replaceState so the back button doesn't accumulate
  // one entry per keystroke.
  useEffect(() => {
    const params = writeStateToUrl(queryState);
    setSearchParams(params, { replace: true });
  }, [queryState, setSearchParams]);

  const loadItems = useCallback(async () => {
    setLoading(true);
    try {
      const qs = buildQueryString(queryState);
      const data = await fetchJSON(`/api/vms?${qs}`);
      setItems(data?.items || []);
      setTotal(data?.total || 0);
      setError(null);
    } catch (e) {
      setError(e?.message || "Failed to load inventory");
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [queryState]);

  const loadFacets = useCallback(async () => {
    try {
      // Facets without page/sort — only the filter dimensions matter.
      // We omit the search to keep facet counts stable while typing.
      const params = new URLSearchParams();
      for (const [key, values] of [
        ["status", statusFilter],
        ["lifecycle_state", lifecycleStateFilter],
        ["environment", environmentFilter],
        ["os_family", osFamilyFilter],
        ["application_hint", applicationFilter],
        ["vcenter_source_id", vcenterFilter],
      ]) {
        for (const v of values) params.append(key, v);
      }
      const data = await fetchJSON(`/api/vms/facets?${params.toString()}`);
      setFacets(data);
    } catch {
      // Facets are advisory — failure shouldn't block the table.
      setFacets(null);
    }
  }, [statusFilter, lifecycleStateFilter, environmentFilter, osFamilyFilter, applicationFilter, vcenterFilter]);

  useEffect(() => { loadItems(); }, [loadItems, refreshSignal]);
  useEffect(() => { loadFacets(); }, [loadFacets, refreshSignal]);

  // Clear stale selection when the page changes or items refresh —
  // the operator's selection should always refer to visible rows.
  useEffect(() => {
    setSelectedIds((prev) => {
      const visible = new Set(items.map((v) => v.id));
      const next = new Set();
      for (const id of prev) if (visible.has(id)) next.add(id);
      return next;
    });
  }, [items]);

  const toggleSort = useCallback((columnId) => {
    setSortBy((prev) => {
      if (prev === columnId) {
        setSortOrder((o) => (o === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortOrder("asc");
      return columnId;
    });
  }, []);

  const allSelected = items.length > 0 && items.every((v) => selectedIds.has(v.id));
  const toggleSelectAll = useCallback(() => {
    setSelectedIds((prev) => {
      if (allSelected) return new Set();
      const next = new Set(prev);
      for (const v of items) next.add(v.id);
      return next;
    });
  }, [allSelected, items]);

  const toggleSelected = useCallback((id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const clearFilters = useCallback(() => {
    setSearchInput("");
    setSearch("");
    setStatusFilter([]);
    setLifecycleStateFilter([]);
    setEnvironmentFilter([]);
    setOsFamilyFilter([]);
    setApplicationFilter([]);
    setVcenterFilter([]);
  }, []);

  const onDeleteAllConfirm = useCallback(async () => {
    setDeletingAll(true);
    try {
      const params = new URLSearchParams();
      params.set("confirm", "true");
      for (const [key, values] of [
        ["status", statusFilter],
        ["lifecycle_state", lifecycleStateFilter],
        ["environment", environmentFilter],
        ["os_family", osFamilyFilter],
        ["application_hint", applicationFilter],
        ["vcenter_source_id", vcenterFilter],
      ]) {
        for (const v of values) params.append(key, v);
      }
      if (search) params.set("search", search);
      const result = await fetchJSON(`/api/vms/all?${params.toString()}`, {
        method: "DELETE",
      });
      toast.success(`Deleted ${result.deleted_count} VM${result.deleted_count === 1 ? "" : "s"}`);
      setDeleteAllOpen(false);
      setSelectedIds(new Set());
      setPage(0);
      await loadItems();
      await loadFacets();
      if (onMutate) onMutate();
    } catch (e) {
      toast.error(e?.message || "Delete All failed");
    } finally {
      setDeletingAll(false);
    }
  }, [
    statusFilter, lifecycleStateFilter, environmentFilter, osFamilyFilter, applicationFilter,
    vcenterFilter, search, loadItems, loadFacets, onMutate,
  ]);

  const exportCsv = useCallback(() => {
    // Plain CSV from the currently visible page. Exporting the full
    // filtered set would need a separate endpoint; that's flagged
    // deferred in INVENTORY_GUIDE.md. The on-screen page already
    // covers the common case (filter → eyeball → export).
    if (items.length === 0) {
      toast.error("No rows to export");
      return;
    }
    const headers = [
      "id", "name", "source_hostname", "ip_address", "os_family",
      "environment", "application_hint", "status", "source_vcenter_id",
      "target_namespace", "updated_at",
    ];
    const rows = items.map((vm) => headers.map((h) => {
      const v = vm[h];
      if (v === null || v === undefined) return "";
      const s = String(v).replace(/"/g, '""');
      return /[",\n]/.test(s) ? `"${s}"` : s;
    }).join(","));
    const csv = [headers.join(","), ...rows].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vms-page-${page + 1}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [items, page]);

  const onBulkDeleteClick = useCallback(() => {
    if (!onBulkDelete) return;
    const targets = items.filter((v) => selectedIds.has(v.id));
    if (targets.length === 0) return;
    onBulkDelete(targets, () => {
      // After the parent's bulk-delete modal commits, refresh us.
      setSelectedIds(new Set());
      loadItems();
      loadFacets();
      if (onMutate) onMutate();
    });
  }, [items, selectedIds, onBulkDelete, loadItems, loadFacets, onMutate]);

  // Facet options enriched with counts (when available). Application
  // and environment dropdowns reflect actual values seen in the data;
  // they aren't a fixed enum.
  const facetOptions = useMemo(() => {
    const opt = (dim) => {
      const facet = facets?.[dim] || {};
      return Object.keys(facet)
        .sort()
        .map((k) => ({ value: k, label: k, count: facet[k] }));
    };
    return {
      status: STATUS_OPTIONS.map((o) => ({
        ...o, count: facets?.status?.[o.value],
      })),
      lifecycle_state: LIFECYCLE_OPTIONS.map((o) => ({
        ...o, count: facets?.lifecycle_state?.[o.value],
      })),
      environment: opt("environment"),
      os_family: opt("os_family"),
      application_hint: opt("application_hint"),
      vcenter_source_id: (vcenterSources || []).map((s) => ({
        value: String(s.id),
        label: s.name || s.hostname,
        count: facets?.vcenter_source_id?.[String(s.id)],
      })),
    };
  }, [facets, vcenterSources]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const firstVisible = total === 0 ? 0 : page * pageSize + 1;
  const lastVisible = Math.min(total, (page + 1) * pageSize);

  const filterDescription = useMemo(() => {
    const parts = [];
    if (statusFilter.length) parts.push(`status: ${statusFilter.join(", ")}`);
    if (lifecycleStateFilter.length) parts.push(`plan: ${lifecycleStateFilter.join(", ")}`);
    if (environmentFilter.length) parts.push(`environment: ${environmentFilter.join(", ")}`);
    if (osFamilyFilter.length) parts.push(`os_family: ${osFamilyFilter.join(", ")}`);
    if (applicationFilter.length) parts.push(`application: ${applicationFilter.join(", ")}`);
    if (vcenterFilter.length) parts.push(`vCenter: ${vcenterFilter.length} selected`);
    if (search) parts.push(`search: "${search}"`);
    return parts.length === 0 ? "no filters — every VM in inventory" : parts.join(" · ");
  }, [
    statusFilter, lifecycleStateFilter, environmentFilter, osFamilyFilter, applicationFilter,
    vcenterFilter, search,
  ]);

  return (
    <div className="fade-in">
      <DeleteAllModal
        open={deleteAllOpen}
        totalLabel={
          filterDescription === "no filters — every VM in inventory"
            ? `Delete ALL ${total} VMs in inventory?`
            : `Delete ${total} filtered VMs?`
        }
        filtersDescription={filterDescription}
        busy={deletingAll}
        onClose={() => setDeleteAllOpen(false)}
        onConfirm={onDeleteAllConfirm}
      />

      {/* Filter bar */}
      <div style={{
        display: "flex", flexWrap: "wrap", gap: 10, marginBottom: 14,
        padding: 12, border: "1px solid #1a1a2e", background: "#0a0a14",
      }}>
        <input
          type="text"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search name / owner / app…"
          style={{ ...INPUT_STYLE, flex: "1 1 260px" }}
        />
        <FacetDropdown
          label="Status"
          options={facetOptions.status}
          selected={statusFilter}
          onChange={setStatusFilter}
        />
        <FacetDropdown
          label="Plan"
          options={facetOptions.lifecycle_state}
          selected={lifecycleStateFilter}
          onChange={setLifecycleStateFilter}
        />
        <FacetDropdown
          label="Environment"
          options={facetOptions.environment}
          selected={environmentFilter}
          onChange={setEnvironmentFilter}
        />
        <FacetDropdown
          label="OS Family"
          options={facetOptions.os_family}
          selected={osFamilyFilter}
          onChange={setOsFamilyFilter}
        />
        <FacetDropdown
          label="Application"
          options={facetOptions.application_hint}
          selected={applicationFilter}
          onChange={setApplicationFilter}
        />
        <FacetDropdown
          label="vCenter"
          options={facetOptions.vcenter_source_id}
          selected={vcenterFilter}
          onChange={setVcenterFilter}
        />
        <button type="button" onClick={clearFilters} style={SECONDARY_BTN_STYLE}>
          Clear filters
        </button>
      </div>

      {/* Page-level actions + result count */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        marginBottom: 12, flexWrap: "wrap", gap: 8,
      }}>
        <div style={{
          fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace",
        }}>
          {loading ? "Loading…" : `Showing ${firstVisible}–${lastVisible} of ${total}`}
          {!loading && total > 0 && (
            <span style={{ color: "#777799" }}> · {filterDescription}</span>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" onClick={loadItems} disabled={loading} style={SECONDARY_BTN_STYLE}>
            ↻ Refresh
          </button>
          <button type="button" onClick={exportCsv} disabled={items.length === 0} style={SECONDARY_BTN_STYLE}>
            ⬇ Export CSV
          </button>
          {onBulkImport && (
            <button type="button" onClick={onBulkImport} style={SECONDARY_BTN_STYLE}>
              ⤴ Bulk Import
            </button>
          )}
          {onAddVM && (
            <button type="button" onClick={onAddVM} style={PRIMARY_BTN_STYLE}>
              + Add VM
            </button>
          )}
          <button
            type="button"
            onClick={() => setDeleteAllOpen(true)}
            disabled={total === 0}
            style={{
              ...DANGER_BTN_STYLE,
              opacity: total === 0 ? 0.4 : 1,
            }}>
            🗑 Delete All
          </button>
        </div>
      </div>

      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          marginBottom: 12, padding: "10px 16px",
          border: "1px solid #4488ff66", background: "rgba(68,136,255,0.06)",
        }}>
          <span style={{
            fontSize: 13, color: "#eeeeff",
            fontFamily: "'Barlow', sans-serif", fontWeight: 600,
          }}>
            {selectedIds.size} selected
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="button" onClick={() => setSelectedIds(new Set())} style={SECONDARY_BTN_STYLE}>
              Clear
            </button>
            <button type="button" onClick={onBulkDeleteClick} style={DANGER_BTN_STYLE}>
              Delete Selected
            </button>
          </div>
        </div>
      )}

      {/* Table */}
      <div style={{
        border: "1px solid #1a1a2e", background: "#07070f",
        opacity: loading ? 0.6 : 1, transition: "opacity 0.15s",
      }}>
        <table style={{
          width: "100%", borderCollapse: "collapse",
          fontFamily: "'Share Tech Mono', monospace", fontSize: 12,
        }}>
          <thead>
            <tr style={{
              background: "#0a0a16", position: "sticky", top: 0,
              borderBottom: "1px solid #1a1a2e",
            }}>
              <th style={{ padding: "12px 14px", textAlign: "left", width: 36 }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={toggleSelectAll}
                  style={{ accentColor: "#4488ff" }}
                  aria-label="Select all on this page"
                />
              </th>
              {SORTABLE_COLUMNS.map((c) => (
                <th key={c.id}
                  style={{
                    padding: "12px 14px", textAlign: "left",
                    fontSize: 11, color: "#aaaacc",
                    fontFamily: "'Barlow', sans-serif", letterSpacing: "0.08em",
                    textTransform: "uppercase", fontWeight: 700,
                    cursor: "pointer", userSelect: "none",
                  }}
                  onClick={() => toggleSort(c.id)}>
                  {c.label}
                  {sortBy === c.id && (
                    <span style={{ marginLeft: 6, color: "#4488ff" }}>
                      {sortOrder === "asc" ? "▲" : "▼"}
                    </span>
                  )}
                </th>
              ))}
              <th style={{ padding: "12px 14px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={SORTABLE_COLUMNS.length + 2} style={{
                  padding: "32px 20px", textAlign: "center", color: "#777799",
                }}>
                  {error
                    ? `Couldn't load inventory: ${error}`
                    : total === 0 && filterDescription !== "no filters — every VM in inventory"
                      ? "No VMs match the current filter. Try clearing one of the dropdowns."
                      : "No VMs in inventory. Use Add VM or Bulk Import to get started."}
                </td>
              </tr>
            )}
            {items.map((vm) => {
              const selected = selectedIds.has(vm.id);
              return (
                <tr key={vm.id} style={{
                  borderBottom: "1px solid #0f0f1e",
                  background: selected ? "rgba(68,136,255,0.04)" : "transparent",
                }}>
                  <td style={{ padding: "10px 14px" }}>
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={() => toggleSelected(vm.id)}
                      aria-label={`Select ${vm.name}`}
                      style={{ accentColor: "#4488ff" }}
                    />
                  </td>
                  <td style={{ padding: "10px 14px" }}>
                    <Link to={`/vms/${vm.id}`} style={{
                      color: "#aaccff", textDecoration: "none", fontWeight: 600,
                    }}>{vm.name}</Link>
                    <div style={{ color: "#777799", fontSize: 11, marginTop: 2 }}>
                      {fmt(vm.ip_address)}
                    </div>
                  </td>
                  <td style={{ padding: "10px 14px" }}>
                    <StatusPill status={vm.status} />
                  </td>
                  <td style={{ padding: "10px 14px" }}>
                    <LifecyclePill state={vm.lifecycle_state} />
                  </td>
                  <td style={{ padding: "10px 14px" }}>{fmt(vm.environment)}</td>
                  <td style={{ padding: "10px 14px" }}>{fmt(vm.os_family)}</td>
                  <td style={{ padding: "10px 14px" }}>{fmt(vm.application_hint)}</td>
                  <td style={{ padding: "10px 14px" }}>{fmt(vm.source_vcenter_id)}</td>
                  <td style={{ padding: "10px 14px" }}>{formatTimestamp(vm.updated_at)}</td>
                  <td style={{ padding: "10px 14px", textAlign: "right" }}>
                    {onCapture && (
                      <button
                        type="button"
                        onClick={() => onCapture(vm)}
                        title="Capture baseline"
                        style={{
                          ...SECONDARY_BTN_STYLE, padding: "4px 8px",
                          marginRight: 4,
                        }}>📡</button>
                    )}
                    {onEdit && (
                      <button
                        type="button"
                        onClick={() => onEdit(vm)}
                        title="Edit"
                        style={{
                          ...SECONDARY_BTN_STYLE, padding: "4px 8px",
                          marginRight: 4,
                        }}>✎</button>
                    )}
                    {onRevertToVMware && vm.lifecycle_state === "migrated" && (
                      <button
                        type="button"
                        onClick={() => onRevertToVMware(vm, () => {
                          loadItems();
                          loadFacets();
                          if (onMutate) onMutate();
                        })}
                        title="Revert to VMware (rollback)"
                        style={{
                          ...SECONDARY_BTN_STYLE,
                          border: "1px solid #ff9933",
                          color: "#ffcc88",
                          padding: "4px 8px",
                          marginRight: 4,
                        }}>↺ Revert</button>
                    )}
                    {onMakeAvailable && vm.lifecycle_state === "rolled_back" && (
                      <button
                        type="button"
                        onClick={() => onMakeAvailable(vm, () => {
                          loadItems();
                          loadFacets();
                          if (onMutate) onMutate();
                        })}
                        title="Mark Available (re-open for plans)"
                        style={{
                          ...PRIMARY_BTN_STYLE,
                          padding: "4px 8px",
                          marginRight: 4,
                        }}>✓ Available</button>
                    )}
                    {onDelete && (
                      <button
                        type="button"
                        onClick={() => onDelete(vm, () => {
                          loadItems();
                          loadFacets();
                          if (onMutate) onMutate();
                        })}
                        title="Delete"
                        style={{ ...DANGER_BTN_STYLE, padding: "4px 8px" }}>🗑</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pager + page-size selector */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "12px 14px", border: "1px solid #1a1a2e", borderTop: "none",
        background: "#0a0a14", flexWrap: "wrap", gap: 8,
      }}>
        <div style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>
          Page {page + 1} of {pageCount}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span style={{ fontSize: 11, color: "#777799" }}>Rows:</span>
          {PAGE_SIZE_OPTIONS.map((n) => (
            <button key={n} type="button" onClick={() => setPageSize(n)}
              style={{
                ...SECONDARY_BTN_STYLE,
                padding: "4px 10px", fontSize: 11,
                ...(n === pageSize
                  ? { borderColor: "#4488ff", color: "#aaccff" }
                  : {}),
              }}>{n}</button>
          ))}
          <button type="button" onClick={() => setPage(0)} disabled={page === 0} style={SECONDARY_BTN_STYLE}>
            ⏮
          </button>
          <button type="button" onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0} style={SECONDARY_BTN_STYLE}>
            ◀
          </button>
          <button type="button" onClick={() => setPage(Math.min(pageCount - 1, page + 1))} disabled={page >= pageCount - 1} style={SECONDARY_BTN_STYLE}>
            ▶
          </button>
          <button type="button" onClick={() => setPage(pageCount - 1)} disabled={page >= pageCount - 1} style={SECONDARY_BTN_STYLE}>
            ⏭
          </button>
        </div>
      </div>
    </div>
  );
}
