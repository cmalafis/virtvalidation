import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

// Bulk operations page — selection-driven capture + validation.
// Two tabs share the same selection UI (filter + multi-select):
// "Capture baselines" hits POST /api/snapshots/capture-bulk; "Validate batch"
// hits POST /api/validations/run-bulk and previews the tier distribution
// before submission so operators see the estimated LLM call count.

const TOAST_OPTS = {
  style: {
    background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};


export default function BulkOperations() {
  const [tab, setTab] = useState("capture"); // 'capture' | 'validate'
  const [vms, setVms] = useState([]);
  const [vcenters, setVcenters] = useState([]);
  const [filters, setFilters] = useState({
    vcenter_id: "", environment: "", application_hint: "",
    status: "", search: "",
  });
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [task, setTask] = useState(null);
  const [tierPreview, setTierPreview] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [v, c] = await Promise.all([
        fetchJSON("/api/vms?limit=10000"),
        fetchJSON("/api/sources/vcenters").catch(() => []),
      ]);
      setVms(Array.isArray(v) ? v : []);
      setVcenters(Array.isArray(c) ? c : []);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    return vms.filter((vm) => {
      if (filters.vcenter_id && String(vm.source_vcenter_id) !== String(filters.vcenter_id)) return false;
      if (filters.environment && vm.environment !== filters.environment) return false;
      if (filters.application_hint && vm.application_hint !== filters.application_hint) return false;
      if (filters.status && vm.status !== filters.status) return false;
      if (filters.search) {
        const q = filters.search.toLowerCase();
        if (!vm.name?.toLowerCase().includes(q)
            && !vm.source_hostname?.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [vms, filters]);

  const allSelected = filtered.length > 0 && filtered.every((vm) => selected.has(vm.id));
  const toggleAll = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(filtered.map((vm) => vm.id)));
  };
  const toggle = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };

  // Distinct environments / app hints / statuses for the filter dropdowns.
  const envs = useMemo(() => [...new Set(vms.map((v) => v.environment).filter(Boolean))], [vms]);
  const apps = useMemo(() => [...new Set(vms.map((v) => v.application_hint).filter(Boolean))], [vms]);
  const statuses = useMemo(() => [...new Set(vms.map((v) => v.status).filter(Boolean))], [vms]);

  const onPreview = async () => {
    if (selected.size === 0) {
      toast.error("Select at least one VM", TOAST_OPTS);
      return;
    }
    setTierPreview(null);
    try {
      const result = await fetchJSON("/api/validations/preview-tiers", {
        method: "POST",
        body: { vm_ids: [...selected] },
      });
      setTierPreview(result);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  const onSubmit = async () => {
    if (selected.size === 0) {
      toast.error("Select at least one VM", TOAST_OPTS);
      return;
    }
    setTask(null);
    try {
      let endpoint, body;
      if (tab === "capture") {
        endpoint = "/api/snapshots/capture-bulk";
        body = { vm_ids: [...selected], max_parallel: 10, per_vcenter_parallel: 5 };
      } else {
        endpoint = "/api/validations/run-bulk";
        body = { vm_ids: [...selected], use_cache: true };
      }
      const spawn = await fetchJSON(endpoint, { method: "POST", body });
      toast.success(
        `${tab === "capture" ? "Capture" : "Validation"} task started · ${spawn.total} VMs`,
        TOAST_OPTS,
      );
      // Poll status every 2s.
      const statusUrl =
        tab === "capture"
          ? `/api/snapshots/capture-bulk/${spawn.task_id}`
          : `/api/validations/run-bulk/${spawn.task_id}`;
      const started = Date.now();
      let final = null;
      while (final == null && Date.now() - started < 60 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 2500));
        const status = await fetchJSON(statusUrl).catch(() => null);
        if (status) {
          setTask(status);
          if (status.status === "completed" || status.status === "failed") {
            final = status.status;
          }
        }
      }
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Bulk Operations</h1>
          <p style={{ color: "#aaaacc", fontSize: 13, margin: "4px 0 0", lineHeight: 1.5 }}>
            Select a scope and run baseline capture or post-migration validation
            against many VMs in one shot. Validation routes most VMs through
            the no-LLM tier classifier; see Preview for the estimate.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Link to="/" style={btnSecondary}>← Inventory</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 18 }}>
          {[
            ["capture", "Capture baselines"],
            ["validate", "Validate batch"],
          ].map(([k, l]) => (
            <button key={k} onClick={() => { setTab(k); setTierPreview(null); setTask(null); }}
              style={{ ...btnGhost, ...(tab === k ? { borderColor: "#4488ff", color: "#eeeeff" } : {}) }}>
              {l}
            </button>
          ))}
        </div>

        <FiltersPanel filters={filters} setFilters={setFilters}
          vcenters={vcenters} envs={envs} apps={apps} statuses={statuses} />

        <div style={{ marginTop: 14, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontSize: 13, color: "#ccccee" }}>
            <strong>{filtered.length}</strong> matched · <strong>{selected.size}</strong> selected
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {tab === "validate" && (
              <button style={btnGhost} onClick={onPreview} disabled={selected.size === 0}>
                Preview tiers
              </button>
            )}
            <button onClick={onSubmit} disabled={selected.size === 0}
              style={{ ...btnPrimary, opacity: selected.size === 0 ? 0.5 : 1 }}>
              {tab === "capture" ? "Capture baselines" : "Run validation"}
            </button>
          </div>
        </div>

        {tierPreview && tab === "validate" && (
          <TierPreviewCard preview={tierPreview} />
        )}
        {task && <TaskStatusCard task={task} tab={tab} />}

        {loading ? (
          <div style={{ color: "#aaaacc", padding: 40 }}>Loading…</div>
        ) : (
          <div style={{ marginTop: 16, border: "1px solid #1a1a2e", background: "#0a0a18" }}>
            <div style={tableHeaderStyle}>
              <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                <span>Name</span>
              </label>
              <span>Hostname</span>
              <span>Env</span>
              <span>App</span>
              <span>Status</span>
              <span>vCenter</span>
            </div>
            <div style={{ maxHeight: 400, overflowY: "auto" }}>
              {filtered.slice(0, 500).map((vm) => (
                <label key={vm.id} style={tableRowStyle}>
                  <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="checkbox" checked={selected.has(vm.id)} onChange={() => toggle(vm.id)} />
                    <span style={{ color: "#eeeeff", fontWeight: 600 }}>{vm.name}</span>
                  </span>
                  <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>{vm.source_hostname || "—"}</span>
                  <span style={{ fontSize: 12, color: "#ccccee" }}>{vm.environment || "—"}</span>
                  <span style={{ fontSize: 12, color: "#ccccee" }}>{vm.application_hint || "—"}</span>
                  <span style={{ fontSize: 12, color: "#aaaacc" }}>{vm.status}</span>
                  <span style={{ fontSize: 12, color: "#aaaacc" }}>
                    {vcenters.find((c) => c.id === vm.source_vcenter_id)?.name || "—"}
                  </span>
                </label>
              ))}
              {filtered.length > 500 && (
                <div style={{ padding: 12, color: "#888899", fontSize: 12, textAlign: "center" }}>
                  Showing first 500 of {filtered.length} — narrow the filter to see more.
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </Shell>
  );
}


function FiltersPanel({ filters, setFilters, vcenters, envs, apps, statuses }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 10 }}>
      <Field label="Search">
        <input style={inputStyle} value={filters.search}
          onChange={(e) => setFilters({ ...filters, search: e.target.value })}
          placeholder="name or hostname" />
      </Field>
      <Field label="vCenter">
        <select style={inputStyle} value={filters.vcenter_id}
          onChange={(e) => setFilters({ ...filters, vcenter_id: e.target.value })}>
          <option value="">All</option>
          {vcenters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </Field>
      <Field label="Environment">
        <select style={inputStyle} value={filters.environment}
          onChange={(e) => setFilters({ ...filters, environment: e.target.value })}>
          <option value="">All</option>
          {envs.map((e) => <option key={e} value={e}>{e}</option>)}
        </select>
      </Field>
      <Field label="Application">
        <select style={inputStyle} value={filters.application_hint}
          onChange={(e) => setFilters({ ...filters, application_hint: e.target.value })}>
          <option value="">All</option>
          {apps.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
      </Field>
      <Field label="Status">
        <select style={inputStyle} value={filters.status}
          onChange={(e) => setFilters({ ...filters, status: e.target.value })}>
          <option value="">All</option>
          {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </Field>
    </div>
  );
}


function TierPreviewCard({ preview }) {
  const buckets = [
    ["Tier 1", "#00ff88", preview.tier1, "no LLM"],
    ["Tier 2", "#88aaff", preview.tier2, "rule-based"],
    ["Tier 3", "#ffaa00", preview.tier3, `${preview.estimated_llm_calls} LLM calls`],
  ];
  return (
    <div style={{ marginTop: 12, padding: "12px 16px", border: "1px solid #4488ff55",
      background: "rgba(68,136,255,0.06)" }}>
      <div style={{ fontSize: 13, color: "#ccccee", marginBottom: 8 }}>
        Tier preview · estimated LLM calls: <strong>{preview.estimated_llm_calls}</strong> ·
        likely cache hits: <strong>{preview.cache_estimate}</strong>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        {buckets.map(([label, color, count, hint]) => (
          <div key={label} style={{ padding: "8px 12px", border: `1px solid ${color}55`, background: `${color}0d` }}>
            <div style={{ fontSize: 11, color, fontWeight: 700, textTransform: "uppercase" }}>{label}</div>
            <div style={{ fontSize: 20, color: "#eeeeff", fontFamily: "'Share Tech Mono', monospace" }}>{count}</div>
            <div style={{ fontSize: 11, color: "#888899" }}>{hint}</div>
          </div>
        ))}
      </div>
      {(preview.errors || []).length > 0 && (
        <ul style={{ marginTop: 8, fontSize: 12, color: "#ffaa00", paddingLeft: 16 }}>
          {preview.errors.slice(0, 5).map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
  );
}


function TaskStatusCard({ task, tab }) {
  const pct = task.total > 0
    ? Math.round(100 * ((task.completed || 0) + (task.failed || 0)) / task.total)
    : 0;
  return (
    <div style={{ marginTop: 12, padding: "12px 16px", border: "1px solid #1a1a2e", background: "#0a0a16" }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span style={{ fontSize: 12, color: "#88aaff", fontWeight: 700, textTransform: "uppercase" }}>
          {task.status}
        </span>
        <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#88aaff" }}>
          {task.completed || 0}/{task.total} · {task.failed || 0} failed · {pct}%
        </span>
      </div>
      <div style={{ height: 4, background: "#07070f", border: "1px solid #1a1a2e", marginTop: 6 }}>
        <div style={{ height: "100%", width: `${pct}%`, background: "#4488ff", transition: "width 0.4s ease" }} />
      </div>
      {tab === "validate" && task.tier_distribution && (
        <div style={{ marginTop: 8, fontSize: 12, color: "#ccccee" }}>
          Tiers: T1=<strong>{task.tier_distribution.tier1}</strong> ·
          T2=<strong>{task.tier_distribution.tier2}</strong> ·
          T3=<strong>{task.tier_distribution.tier3}</strong>
          {" · "}LLM calls: <strong>{task.llm_calls}</strong> ·
          cache hits: <strong>{task.cache_hits}</strong>
        </div>
      )}
      {task.current_vm && (
        <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
          Current: <em>{task.current_vm}</em>
        </div>
      )}
    </div>
  );
}


function Field({ label, children }) {
  return (
    <label style={{ display: "block" }}>
      <span style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", display: "block", marginBottom: 6 }}>
        {label}
      </span>
      {children}
    </label>
  );
}

function Shell({ children }) {
  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
      `}</style>
      {children}
    </div>
  );
}

const headerStyle = {
  position: "sticky", top: 0, zIndex: 50,
  background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
  borderBottom: "1px solid #1a1a2e",
  display: "flex", alignItems: "center", justifyContent: "space-between",
  padding: "16px 32px", gap: 24,
};
const tableHeaderStyle = {
  display: "grid", gridTemplateColumns: "2fr 1.6fr 0.8fr 1fr 1fr 1fr",
  padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", fontWeight: 700, textTransform: "uppercase",
};
const tableRowStyle = {
  display: "grid", gridTemplateColumns: "2fr 1.6fr 0.8fr 1fr 1fr 1fr",
  padding: "10px 14px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13, cursor: "pointer",
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "8px 10px", fontFamily: "'Share Tech Mono', monospace",
  fontSize: 12, width: "100%", outline: "none",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
  textDecoration: "none",
};
const btnGhost = {
  background: "transparent", border: "1px solid #2a2a44", color: "#ccccee",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 11,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};

