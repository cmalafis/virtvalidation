import { useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

// Per-VM plan creation page. Two steps:
//
//   1. Plan name + mapping selection — operator picks the
//      ResourceMappings that drive target namespace / network /
//      storage resolution. Multi-select because mappings are scoped
//      per source vCenter and a plan covering multiple vCenters
//      needs one mapping per vcenter. Defaults to all-selected so
//      the common case (operator just wants every applicable
//      mapping considered) is one click.
//   2. VM selector — filters + checkbox-per-VM list, capped at
//      MAX_VMS_PER_PLAN (250). The selector hides VMs that are
//      already in another active plan by default; toggles expose
//      planned + migrated rows for auditing (not selectable).
//
// Submission POSTs to /api/plans with {name, mapping_ids, vm_ids}
// and polls /api/plans/{id} every 2s until status reaches
// "complete" or "failed". The new pipeline is deterministic for
// stages 1-5+7 and LLM-bounded for stage 6 (~25 parallel calls
// for a 250-VM plan), so total wall-clock is typically <30s.

const MAX_VMS_PER_PLAN = 250;
const PAGE_SIZE = 50;

const TOAST_OPTS = {
  style: {
    background: "#0a0a18",
    border: "1px solid #2a2a44",
    color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif",
    fontSize: 14,
    lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const LIFECYCLE_COLORS = {
  available: "#9ca3ff",
  planned: "#ffaa00",
  migrated: "#00ff88",
  rolled_back: "#ff9933",
  unmanageable: "#666688",
};

export default function PlanWizard() {
  const navigate = useNavigate();

  // Step 1 state
  const [name, setName] = useState("");
  // Set of selected mapping ids (numbers). Default-populated with
  // every available mapping after fetch so the operator's common
  // case is one click.
  const [mappingIds, setMappingIds] = useState(() => new Set());
  const [mappings, setMappings] = useState([]);
  const [step, setStep] = useState(1);

  // Step 2 state — filters
  const [filters, setFilters] = useState({
    application_hint: [],
    environment: [],
    vcenter_source_id: [],
    search: "",
  });
  const [showPlanned, setShowPlanned] = useState(false);
  const [showMigrated, setShowMigrated] = useState(false);

  // Step 2 — list + facets
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [skip, setSkip] = useState(0);
  const [facets, setFacets] = useState(null);
  const [loading, setLoading] = useState(false);

  // Per-row selection. Keyed by vm.id → vm (so we keep details
  // after the row scrolls off the current page).
  const [selected, setSelected] = useState(new Map());

  // Submission state
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState(null);

  // ---------------------------------------------------------------
  // Load mappings once. Defensive: empty list on failure so the
  // dropdown still renders.
  // ---------------------------------------------------------------
  useEffect(() => {
    fetchJSON("/api/mappings")
      .then((data) => {
        const list = Array.isArray(data) ? data : [];
        setMappings(list);
        // Default: all selected. Operator deselects what they want
        // to exclude; the validator routes each VM to the mapping
        // whose vcenter matches.
        setMappingIds(new Set(list.map((m) => m.id)));
      })
      .catch(() => setMappings([]));
  }, []);

  // ---------------------------------------------------------------
  // Build the querystring for /api/vms and /api/vms/facets.
  // ---------------------------------------------------------------
  const lifecycleStates = useMemo(() => {
    const out = ["available"];
    if (showPlanned) out.push("planned");
    if (showMigrated) out.push("migrated");
    return out;
  }, [showPlanned, showMigrated]);

  const buildQs = (extra = {}) => {
    const params = new URLSearchParams();
    lifecycleStates.forEach((s) => params.append("lifecycle_state", s));
    (filters.application_hint || []).forEach((v) =>
      params.append("application_hint", v),
    );
    (filters.environment || []).forEach((v) => params.append("environment", v));
    (filters.vcenter_source_id || []).forEach((v) =>
      params.append("vcenter_source_id", String(v)),
    );
    if (filters.search) params.set("search", filters.search);
    Object.entries(extra).forEach(([k, v]) => {
      if (v != null) params.set(k, String(v));
    });
    return params.toString();
  };

  // ---------------------------------------------------------------
  // Fetch the current page + facets whenever filters / lifecycle
  // toggles / pagination change. Step 2 only.
  // ---------------------------------------------------------------
  useEffect(() => {
    if (step !== 2) return;
    setLoading(true);
    const qs = buildQs({ skip, limit: PAGE_SIZE, sort_by: "name" });
    Promise.all([
      fetchJSON(`/api/vms?${qs}`).catch(() => ({ items: [], total: 0 })),
      fetchJSON(`/api/vms/facets?${buildQs()}`).catch(() => null),
    ])
      .then(([list, facetsData]) => {
        setItems(Array.isArray(list?.items) ? list.items : []);
        setTotal(list?.total ?? 0);
        setFacets(facetsData);
      })
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, JSON.stringify(filters), showPlanned, showMigrated, skip]);

  // ---------------------------------------------------------------
  // Selection helpers
  // ---------------------------------------------------------------
  const isSelectable = (vm) => vm.lifecycle_state === "available";

  const toggleSelected = (vm) => {
    if (!isSelectable(vm)) return;
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(vm.id)) next.delete(vm.id);
      else next.set(vm.id, vm);
      return next;
    });
  };

  const clearSelection = () => setSelected(new Map());

  const selectAllOnPage = () => {
    setSelected((prev) => {
      const next = new Map(prev);
      for (const vm of items) {
        if (isSelectable(vm) && next.size < MAX_VMS_PER_PLAN) {
          next.set(vm.id, vm);
        }
      }
      return next;
    });
  };

  const remaining = MAX_VMS_PER_PLAN - selected.size;
  const overCap = selected.size > MAX_VMS_PER_PLAN;
  const canGenerate =
    step === 2 &&
    selected.size > 0 &&
    selected.size <= MAX_VMS_PER_PLAN &&
    name.trim().length > 0 &&
    !generating;

  // ---------------------------------------------------------------
  // Suggested narrowing chips — top application_hints in the facet
  // response (sorted by count descending). Only show when the
  // filtered total exceeds the cap.
  // ---------------------------------------------------------------
  const narrowingChips = useMemo(() => {
    if (!facets || total <= MAX_VMS_PER_PLAN) return [];
    const apps = Object.entries(facets.application_hint || {})
      .filter(([k]) => !(filters.application_hint || []).includes(k))
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
    return apps.map(([value, count]) => ({ value, count }));
  }, [facets, total, filters.application_hint]);

  // ---------------------------------------------------------------
  // Submit + poll
  // ---------------------------------------------------------------
  const submit = async () => {
    if (!canGenerate) return;
    setGenerating(true);
    setProgress({ status: "pending", progress_message: "Queued", progress_percent: 0 });
    try {
      const body = {
        name: name.trim(),
        vm_ids: Array.from(selected.keys()),
        // Always send the list (even empty) so the backend can
        // distinguish "operator opted out" from "field omitted".
        mapping_ids: Array.from(mappingIds),
      };
      const plan = await fetchJSON("/api/plans", { method: "POST", body });
      toast("Generating plan…", { ...TOAST_OPTS, icon: "🤖" });

      const startedAt = Date.now();
      let final = null;
      while (final == null && Date.now() - startedAt < 5 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const status = await fetchJSON(`/api/plans/${plan.id}`);
          setProgress(status);
          if (status.status === "complete") {
            final = "complete";
            toast.success("Migration plan ready", TOAST_OPTS);
            navigate(`/plans/${status.id}`);
          } else if (status.status === "failed") {
            final = "failed";
            toast.error(
              `Plan generation failed: ${status.error_message || "unknown error"}`,
              { ...TOAST_OPTS, duration: 10000 },
            );
          }
        } catch {
          /* keep polling */
        }
      }
      if (final == null)
        toast("Still running — check the plan detail page in a moment", {
          ...TOAST_OPTS,
          icon: "⏱",
        });
    } catch (err) {
      toast.error(err.message || "Failed to start plan generation", TOAST_OPTS);
    } finally {
      setGenerating(false);
    }
  };

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------
  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>New Migration Plan</div>
          <div style={{ fontSize: 13, color: "#aaaacc", marginTop: 4 }}>
            Pick the mapping, then pick the VMs you want migrated.
          </div>
        </div>
        <Link to="/" style={btnSecondary}>
          ← Back
        </Link>
      </header>

      <main style={{ maxWidth: 1080, margin: "0 auto", padding: 32 }}>
        <StepBar current={step} />

        {step === 1 && (
          <Step n={1} title="Plan name + mapping">
            <label style={labelStyle}>Plan name</label>
            <input
              style={inputStyle}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. DHA Q3 2026 — East Coast"
              maxLength={255}
              autoFocus
            />

            <label style={{ ...labelStyle, marginTop: 18 }}>Resource mappings</label>
            <MappingMultiSelect
              mappings={mappings}
              selected={mappingIds}
              onChange={setMappingIds}
            />
            <div style={{ fontSize: 12, color: "#888899", marginTop: 6 }}>
              Mappings resolve source vSphere resources to target cluster
              resources. Each VM is routed to the mapping whose source
              vCenter matches the VM's. Plan creation refuses VMs whose
              vCenter isn't covered by any selected mapping.
            </div>

            <div
              style={{
                display: "flex",
                gap: 10,
                justifyContent: "flex-end",
                marginTop: 24,
              }}
            >
              <Link to="/" style={btnSecondary}>
                Cancel
              </Link>
              <button
                type="button"
                disabled={!name.trim()}
                onClick={() => setStep(2)}
                style={{ ...btnPrimary, opacity: !name.trim() ? 0.5 : 1 }}
              >
                Next: select VMs
              </button>
            </div>
          </Step>
        )}

        {step === 2 && (
          <Step n={2} title="Select VMs">
            <FilterBar
              filters={filters}
              facets={facets}
              onChange={(next) => {
                setSkip(0);
                setFilters(next);
              }}
            />

            <div style={toggleRowStyle}>
              <Toggle
                checked={showPlanned}
                onChange={() => setShowPlanned((v) => !v)}
                label="Show VMs in active plans"
              />
              <Toggle
                checked={showMigrated}
                onChange={() => setShowMigrated((v) => !v)}
                label="Show migrated VMs"
              />
            </div>

            {narrowingChips.length > 0 && (
              <NarrowingPanel
                total={total}
                cap={MAX_VMS_PER_PLAN}
                chips={narrowingChips}
                onAdd={(value) => {
                  setSkip(0);
                  setFilters((prev) => ({
                    ...prev,
                    application_hint: [
                      ...(prev.application_hint || []),
                      value,
                    ],
                  }));
                }}
              />
            )}

            <SelectionCounter
              selected={selected.size}
              total={total}
              cap={MAX_VMS_PER_PLAN}
              loading={loading}
              onClear={clearSelection}
              onSelectAllOnPage={selectAllOnPage}
            />

            <VMTable
              items={items}
              selected={selected}
              onToggle={toggleSelected}
              isSelectable={isSelectable}
            />

            <Pager
              skip={skip}
              limit={PAGE_SIZE}
              total={total}
              onPage={(s) => setSkip(s)}
            />

            {generating && progress && <ProgressBar progress={progress} />}

            <div
              style={{
                display: "flex",
                gap: 10,
                justifyContent: "space-between",
                marginTop: 18,
                paddingTop: 18,
                borderTop: "1px solid #1a1a2e",
              }}
            >
              <button
                type="button"
                style={btnSecondary}
                onClick={() => setStep(1)}
              >
                ← Back
              </button>
              <div style={{ display: "flex", gap: 10 }}>
                <Link to="/" style={btnSecondary}>
                  Cancel
                </Link>
                <button
                  type="button"
                  disabled={!canGenerate || overCap}
                  onClick={submit}
                  style={{
                    ...btnPrimary,
                    opacity: !canGenerate || overCap ? 0.5 : 1,
                  }}
                >
                  {generating
                    ? "Generating…"
                    : overCap
                      ? `GENERATE (${MAX_VMS_PER_PLAN} max — ${selected.size} selected, narrow filters)`
                      : `GENERATE (${selected.size}/${MAX_VMS_PER_PLAN})`}
                </button>
              </div>
            </div>
          </Step>
        )}
      </main>
    </Shell>
  );
}

// ---------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------
function StepBar({ current }) {
  const steps = [
    { n: 1, label: "Name + mapping" },
    { n: 2, label: "Select VMs" },
  ];
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        marginBottom: 24,
        fontFamily: "'Share Tech Mono', monospace",
        fontSize: 11,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
      }}
    >
      {steps.map((s) => (
        <div
          key={s.n}
          style={{
            padding: "6px 12px",
            border:
              s.n === current ? "1px solid #4488ff" : "1px solid #2a2a44",
            color: s.n === current ? "#eef2ff" : "#888899",
            background: s.n === current ? "#1d3a8a22" : "transparent",
          }}
        >
          {s.n}. {s.label}
        </div>
      ))}
    </div>
  );
}

function MappingMultiSelect({ mappings, selected, onChange }) {
  if (!mappings || mappings.length === 0) {
    return (
      <div
        style={{
          padding: "12px 14px",
          border: "1px solid #2a2a44",
          background: "#07070f",
          color: "#888899",
          fontSize: 13,
        }}
      >
        No resource mappings defined yet. The plan will rely entirely
        on per-VM target fields.
      </div>
    );
  }
  const selectAll = () => onChange(new Set(mappings.map((m) => m.id)));
  const clearAll = () => onChange(new Set());
  const toggle = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(next);
  };
  return (
    <div
      style={{
        border: "1px solid #2a2a44",
        background: "#07070f",
        padding: "10px 12px",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 8,
        }}
      >
        <span
          style={{
            fontSize: 11,
            color: "#888899",
            fontFamily: "'Share Tech Mono', monospace",
            letterSpacing: "0.06em",
          }}
        >
          {selected.size} of {mappings.length} selected
        </span>
        <div style={{ display: "flex", gap: 6 }}>
          <button type="button" style={btnGhost} onClick={selectAll}>
            Select all
          </button>
          <button type="button" style={btnGhost} onClick={clearAll}>
            Clear all
          </button>
        </div>
      </div>
      <div style={{ maxHeight: 220, overflow: "auto" }}>
        {mappings.map((m) => {
          const checked = selected.has(m.id);
          return (
            <label
              key={m.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "6px 4px",
                cursor: "pointer",
                color: "#ccccee",
                fontSize: 13,
              }}
            >
              <input
                type="checkbox"
                checked={checked}
                onChange={() => toggle(m.id)}
              />
              <span style={{ flex: 1 }}>
                <span style={{ fontWeight: 600 }}>{m.name}</span>
                <span style={{ color: "#888899", marginLeft: 8, fontSize: 12 }}>
                  · {m.status}
                </span>
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}


function FilterBar({ filters, facets, onChange }) {
  const setMulti = (key, value) => {
    const list = filters[key] || [];
    const next = list.includes(value)
      ? list.filter((v) => v !== value)
      : [...list, value];
    onChange({ ...filters, [key]: next });
  };
  const setSearch = (value) => onChange({ ...filters, search: value });
  const appOptions = facets ? Object.keys(facets.application_hint || {}) : [];
  const envOptions = facets ? Object.keys(facets.environment || {}) : [];
  const vcOptions = facets
    ? Object.entries(facets.vcenter_source_id || {}).map(([id, count]) => ({
        id,
        count,
      }))
    : [];

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 1fr 1fr 2fr",
        gap: 10,
        marginBottom: 14,
      }}
    >
      <FacetDropdown
        label="Application"
        options={appOptions}
        selected={filters.application_hint || []}
        onToggle={(v) => setMulti("application_hint", v)}
        facets={facets?.application_hint}
      />
      <FacetDropdown
        label="Environment"
        options={envOptions}
        selected={filters.environment || []}
        onToggle={(v) => setMulti("environment", v)}
        facets={facets?.environment}
      />
      <FacetDropdown
        label="vCenter"
        options={vcOptions.map((o) => o.id)}
        selected={filters.vcenter_source_id?.map(String) || []}
        onToggle={(v) => setMulti("vcenter_source_id", v)}
        facets={facets?.vcenter_source_id}
      />
      <input
        style={inputStyle}
        placeholder="Search name, owner, application…"
        value={filters.search || ""}
        onChange={(e) => setSearch(e.target.value)}
      />
    </div>
  );
}

function FacetDropdown({ label, options, selected, onToggle, facets }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          ...inputStyle,
          textAlign: "left",
          cursor: "pointer",
          paddingTop: 9,
          paddingBottom: 9,
        }}
      >
        {label}
        {(selected || []).length > 0 ? ` (${selected.length})` : ""}
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 20,
            background: "#0a0a16",
            border: "1px solid #2a2a44",
            maxHeight: 260,
            overflow: "auto",
            marginTop: 4,
          }}
        >
          {options.length === 0 ? (
            <div style={{ padding: 10, color: "#888899", fontSize: 12 }}>
              No options yet.
            </div>
          ) : (
            options.map((value) => (
              <label
                key={value}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 10px",
                  cursor: "pointer",
                  color: "#ccccee",
                  fontSize: 13,
                }}
              >
                <input
                  type="checkbox"
                  checked={(selected || []).includes(value)}
                  onChange={() => onToggle(value)}
                />
                <span>{value || "—"}</span>
                {facets && facets[value] != null && (
                  <span
                    style={{
                      marginLeft: "auto",
                      color: "#888899",
                      fontFamily: "'Share Tech Mono', monospace",
                      fontSize: 11,
                    }}
                  >
                    {facets[value]}
                  </span>
                )}
              </label>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
        color: "#aaaacc",
        fontSize: 13,
      }}
    >
      <input type="checkbox" checked={checked} onChange={onChange} />
      {label}
    </label>
  );
}

function NarrowingPanel({ total, cap, chips, onAdd }) {
  return (
    <div
      style={{
        padding: "12px 14px",
        border: "1px solid #ffaa0055",
        background: "rgba(255,170,0,0.04)",
        marginBottom: 14,
      }}
    >
      <div style={{ fontSize: 13, color: "#ffcc77", marginBottom: 8 }}>
        Filtered set is <strong>{total}</strong> VMs — over the {cap} cap.
        Narrow by application:
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {chips.map((c) => (
          <button
            key={c.value}
            type="button"
            onClick={() => onAdd(c.value)}
            style={{
              background: "transparent",
              border: "1px solid #2a2a44",
              color: "#ccccee",
              fontSize: 12,
              fontFamily: "'Share Tech Mono', monospace",
              padding: "4px 10px",
              cursor: "pointer",
            }}
          >
            + {c.value} <span style={{ color: "#888899" }}>({c.count})</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SelectionCounter({
  selected,
  total,
  cap,
  loading,
  onClear,
  onSelectAllOnPage,
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "8px 12px",
        background: "#0a0a16",
        border: "1px solid #1a1a2e",
        marginBottom: 8,
        fontSize: 13,
      }}
    >
      <div style={{ color: "#ccccee" }}>
        <strong>{selected}</strong> selected · <strong>{total}</strong> match
        filters · cap {cap}
        {loading && (
          <span style={{ marginLeft: 8, color: "#888899" }}>· loading…</span>
        )}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" style={btnGhost} onClick={onSelectAllOnPage}>
          Select page
        </button>
        <button type="button" style={btnGhost} onClick={onClear}>
          Clear selection
        </button>
      </div>
    </div>
  );
}

function VMTable({ items, selected, onToggle, isSelectable }) {
  if ((items || []).length === 0) {
    return (
      <div
        style={{
          padding: "32px 16px",
          textAlign: "center",
          color: "#888899",
          border: "1px solid #1a1a2e",
        }}
      >
        No VMs match the current filters.
      </div>
    );
  }
  return (
    <table
      style={{
        width: "100%",
        borderCollapse: "collapse",
        fontSize: 13,
      }}
    >
      <thead>
        <tr style={{ background: "#0a0a16" }}>
          <th style={thStyle}></th>
          <th style={thStyle}>Name</th>
          <th style={thStyle}>Environment</th>
          <th style={thStyle}>Application</th>
          <th style={thStyle}>vCenter</th>
          <th style={thStyle}>Lifecycle</th>
        </tr>
      </thead>
      <tbody>
        {items.map((vm) => {
          const checked = selected.has(vm.id);
          const selectable = isSelectable(vm);
          const color = LIFECYCLE_COLORS[vm.lifecycle_state] || "#888899";
          return (
            <tr
              key={vm.id}
              style={{
                borderBottom: "1px solid #1a1a2e",
                color: selectable ? "#eeeeff" : "#666688",
                background: checked ? "#1d3a8a18" : "transparent",
                cursor: selectable ? "pointer" : "default",
              }}
              onClick={() => onToggle(vm)}
            >
              <td style={tdStyle}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!selectable}
                  onChange={() => onToggle(vm)}
                  onClick={(e) => e.stopPropagation()}
                />
              </td>
              <td style={tdStyle}>
                <span style={{ fontFamily: "'Share Tech Mono', monospace" }}>
                  {vm.name}
                </span>
              </td>
              <td style={tdStyle}>{vm.environment ?? "—"}</td>
              <td style={tdStyle}>{vm.application_hint ?? "—"}</td>
              <td style={tdStyle}>{vm.source_vcenter_id ?? "—"}</td>
              <td style={tdStyle}>
                <span
                  style={{
                    fontSize: 11,
                    padding: "2px 8px",
                    border: `1px solid ${color}55`,
                    background: `${color}11`,
                    color,
                    fontFamily: "'Share Tech Mono', monospace",
                    textTransform: "uppercase",
                  }}
                >
                  {vm.lifecycle_state}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Pager({ skip, limit, total, onPage }) {
  const page = Math.floor((skip || 0) / limit) + 1;
  const pageCount = Math.max(1, Math.ceil((total || 0) / limit));
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        alignItems: "center",
        justifyContent: "center",
        marginTop: 12,
        fontFamily: "'Share Tech Mono', monospace",
        fontSize: 12,
        color: "#aaaacc",
      }}
    >
      <button
        type="button"
        style={btnGhost}
        disabled={page <= 1}
        onClick={() => onPage(Math.max(0, skip - limit))}
      >
        ‹ Prev
      </button>
      <span>
        Page {page} / {pageCount}
      </span>
      <button
        type="button"
        style={btnGhost}
        disabled={page >= pageCount}
        onClick={() => onPage(skip + limit)}
      >
        Next ›
      </button>
    </div>
  );
}

function ProgressBar({ progress }) {
  const pct = Math.max(
    0,
    Math.min(100, progress?.progress_percent ?? 0),
  );
  return (
    <div
      style={{
        marginTop: 18,
        padding: "14px 16px",
        border: "1px solid #4488ff55",
        background: "rgba(68,136,255,0.06)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 8,
          fontSize: 11,
          fontFamily: "'Share Tech Mono', monospace",
        }}
      >
        <span style={{ color: "#88aaff", textTransform: "uppercase" }}>
          {progress?.status} · {progress?.progress_message || progress?.status}
        </span>
        <span style={{ color: "#88aaff" }}>{pct}%</span>
      </div>
      <div
        style={{
          height: 4,
          background: "#0a0a18",
          border: "1px solid #1a1a2e",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background: "#4488ff",
            transition: "width 0.4s ease",
          }}
        />
      </div>
    </div>
  );
}

function Step({ n, title, children }) {
  return (
    <section
      style={{
        marginBottom: 28,
        padding: "20px 24px",
        background: "#0a0a16",
        border: "1px solid #1a1a2e",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 12,
          marginBottom: 14,
        }}
      >
        <span
          style={{
            fontSize: 11,
            color: "#88aaff",
            letterSpacing: "0.08em",
            fontWeight: 700,
            fontFamily: "'Share Tech Mono', monospace",
            border: "1px solid #4488ff66",
            padding: "3px 8px",
            textTransform: "uppercase",
          }}
        >
          step {n}
        </span>
        <span
          style={{
            fontSize: 16,
            color: "#eeeeff",
            fontWeight: 700,
            fontFamily: "'Barlow', sans-serif",
          }}
        >
          {title}
        </span>
      </div>
      {children}
    </section>
  );
}

function Shell({ children }) {
  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#07070f",
        color: "#eeeeff",
        fontFamily: "'Barlow', sans-serif",
      }}
    >
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        button:disabled { cursor: not-allowed; }
      `}</style>
      {children}
    </div>
  );
}

// ---------- styles ----------
const headerStyle = {
  position: "sticky",
  top: 0,
  zIndex: 50,
  background: "rgba(7,7,15,0.96)",
  backdropFilter: "blur(8px)",
  borderBottom: "1px solid #1a1a2e",
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "16px 32px",
  gap: 24,
};
const inputStyle = {
  background: "#07070f",
  border: "1px solid #2a2a44",
  color: "#eeeeff",
  padding: "10px 12px",
  fontFamily: "'Barlow', sans-serif",
  fontSize: 14,
  width: "100%",
  outline: "none",
};
const labelStyle = {
  display: "block",
  fontSize: 11,
  color: "#aaaacc",
  letterSpacing: "0.08em",
  fontWeight: 700,
  textTransform: "uppercase",
  marginBottom: 6,
};
const toggleRowStyle = {
  display: "flex",
  gap: 16,
  marginBottom: 12,
};
const thStyle = {
  textAlign: "left",
  padding: "8px 10px",
  borderBottom: "1px solid #2a2a44",
  fontSize: 11,
  color: "#888899",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  fontFamily: "'Share Tech Mono', monospace",
};
const tdStyle = {
  padding: "8px 10px",
  fontFamily: "'Barlow', sans-serif",
};
const btnPrimary = {
  background: "#1d3a8a",
  border: "1px solid #4488ff",
  color: "#eef2ff",
  padding: "10px 18px",
  fontFamily: "'Barlow', sans-serif",
  fontSize: 12,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  fontWeight: 700,
  cursor: "pointer",
  textDecoration: "none",
};
const btnSecondary = {
  background: "transparent",
  border: "1px solid #3a3a55",
  color: "#aaaacc",
  padding: "10px 18px",
  fontFamily: "'Barlow', sans-serif",
  fontSize: 12,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  fontWeight: 700,
  cursor: "pointer",
  textDecoration: "none",
};
const btnGhost = {
  background: "transparent",
  border: "1px solid #2a2a44",
  color: "#ccccee",
  padding: "6px 12px",
  fontFamily: "'Barlow', sans-serif",
  fontSize: 11,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  fontWeight: 700,
  cursor: "pointer",
};
