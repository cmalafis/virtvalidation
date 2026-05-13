import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

// Resource-mapping editor. Operators map source vSphere networks /
// datastores onto discovered OCP cluster resources. The editor
// renders the union of source resources (collected from VMs in the
// vCenter scope) and target resources (from the OCP target's
// discovery cache), then lets the operator paint a target onto each
// source row. LLM suggestion buttons fill rows with high-confidence
// guesses; preflight surfaces remaining gaps.

const TOAST_OPTS = {
  style: {
    background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const STATUS_COLORS = {
  complete: "#00ff88",
  incomplete: "#ffaa00",
  needs_review: "#ff5577",
};


// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------
export default function ResourceMappings() {
  const [mappings, setMappings] = useState([]);
  const [vcenters, setVcenters] = useState([]);
  const [targets, setTargets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const [m, v, t] = await Promise.all([
        fetchJSON("/api/mappings"),
        fetchJSON("/api/sources/vcenters"),
        fetchJSON("/api/sources/targets"),
      ]);
      setMappings(m); setVcenters(v); setTargets(t);
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const lookup = useMemo(() => ({
    vc: Object.fromEntries(vcenters.map((v) => [v.id, v.name])),
    target: Object.fromEntries(targets.map((t) => [t.id, t.name])),
  }), [vcenters, targets]);

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Resource Mappings</h1>
          <p style={{ color: "#aaaacc", fontSize: 13, margin: "4px 0 0", lineHeight: 1.5 }}>
            Map source vSphere networks and datastores to discovered OCP target
            resources. Plans without a complete mapping fall back to placeholder
            names in the generated MTV YAML.
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/sources/targets" style={btnSecondary}>← OCP Targets</Link>
          <Link to="/" style={btnSecondary}>Inventory</Link>
          <button onClick={() => setCreateOpen(true)} style={btnPrimary}
            disabled={!vcenters.length || !targets.length}
            title={(!vcenters.length || !targets.length) ? "Register a vCenter and an OCP target first" : ""}>
            + New Mapping
          </button>
        </div>
      </header>
      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        {loading ? <div style={{ color: "#aaaacc", padding: 40 }}>Loading…</div>
        : err ? <ErrorBlock msg={err} onRetry={load} />
        : mappings.length === 0 ? (
          <Empty disabled={!vcenters.length || !targets.length}
            onAdd={() => setCreateOpen(true)} />
        ) : (
          <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
            <div style={tableHeaderStyle}>
              {["Name", "Source vCenter", "Target Cluster", "Networks", "Storage", "Status", ""].map((h) =>
                <span key={h}>{h}</span>)}
            </div>
            {mappings.map((m) => (
              <Link key={m.id} to={`/mappings/${m.id}`} style={{ textDecoration: "none" }}>
                <div style={tableRowStyle}>
                  <span style={{ color: "#eeeeff", fontWeight: 600 }}>
                    {m.is_active && <span title="Active mapping" style={{ color: "#00ff88", marginRight: 6 }}>●</span>}
                    {m.name}
                  </span>
                  <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>
                    {lookup.vc[m.vcenter_source_id] || `#${m.vcenter_source_id}`}
                  </span>
                  <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>
                    {lookup.target[m.ocp_target_id] || `#${m.ocp_target_id}`}
                  </span>
                  <span style={{ color: "#ccccee", fontSize: 12 }}>{m.network_mappings?.length ?? 0}</span>
                  <span style={{ color: "#ccccee", fontSize: 12 }}>{m.storage_mappings?.length ?? 0}</span>
                  <Pill label={m.status?.toUpperCase().replace("_", " ")} color={STATUS_COLORS[m.status]} />
                  <span style={{ color: "#aaaacc", textAlign: "right", fontSize: 12 }}>open →</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
      {createOpen && (
        <CreateModal vcenters={vcenters} targets={targets}
          onClose={() => setCreateOpen(false)}
          onSaved={async () => { setCreateOpen(false); await load(); }} />
      )}
    </Shell>
  );
}

function CreateModal({ vcenters, targets, onClose, onSaved }) {
  const [name, setName] = useState("");
  const [vcId, setVcId] = useState(vcenters[0]?.id || "");
  const [tgId, setTgId] = useState(targets[0]?.id || "");
  const [active, setActive] = useState(false);
  const [saving, setSaving] = useState(false);
  const navigate = useNavigate();

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const created = await fetchJSON("/api/mappings", {
        method: "POST",
        body: {
          name: name.trim(), vcenter_source_id: Number(vcId),
          ocp_target_id: Number(tgId), is_active: active,
          network_mappings: [], storage_mappings: [], namespace_mappings: [],
        },
      });
      toast.success(`Created ${created.name}`, TOAST_OPTS);
      await onSaved();
      navigate(`/mappings/${created.id}`);
    } catch (err) { toast.error(err.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>New Resource Mapping</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            You&apos;ll edit network/storage rows on the next screen.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)}
              required autoFocus maxLength={255} />
          </Field>
          <Field label="Source vCenter" required>
            <select style={inputStyle} value={vcId} onChange={(e) => setVcId(e.target.value)} required>
              {vcenters.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </Field>
          <Field label="Target OCP cluster" required>
            <select style={inputStyle} value={tgId} onChange={(e) => setTgId(e.target.value)} required>
              {targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </Field>
          <label style={{ display: "flex", gap: 8, color: "#ccccee", fontSize: 13 }}>
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Mark this mapping active (deactivates other mappings for this source/target pair)
          </label>
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={saving || !name.trim()}
            style={{ ...btnPrimary, opacity: (saving || !name.trim()) ? 0.5 : 1 }}>
            {saving ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail / editor
// ---------------------------------------------------------------------------
export function ResourceMappingDetail() {
  const { id } = useParams();
  const [mapping, setMapping] = useState(null);
  const [target, setTarget] = useState(null);
  const [sourceSignals, setSourceSignals] = useState({ networks: [], datastores: [] });
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [networkRows, setNetworkRows] = useState([]);
  const [storageRows, setStorageRows] = useState([]);
  const [namespaceRows, setNamespaceRows] = useState([]);
  const [name, setName] = useState("");
  const [active, setActive] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preflight, setPreflight] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const m = await fetchJSON(`/api/mappings/${id}`);
      const t = await fetchJSON(`/api/sources/targets/${m.ocp_target_id}`);
      // Build source-signal lists by walking VMs in scope. limit=1000
      // matches the backend MAX_PAGE_SIZE cap (previously
      // limit=10000 here triggered a 422 against the mapping page).
      // For vCenters with >1000 VMs, the aggregation under-counts
      // signals that only appear on later pages — a dedicated
      // /api/sources/vcenters/{id}/source-signals endpoint that
      // aggregates server-side is the open follow-on; this cap
      // unblocks the mapping page today.
      const vmsResp = await fetchJSON(`/api/vms?source_vcenter_id=${m.vcenter_source_id}&limit=1000`);
      const netCount = {}; const dsCount = {};
      for (const vm of (Array.isArray(vmsResp) ? vmsResp : (vmsResp?.items || []))) {
        for (const n of vm.vsphere_networks || []) netCount[n] = (netCount[n] || 0) + 1;
        for (const d of vm.vsphere_datastores || []) dsCount[d] = (dsCount[d] || 0) + 1;
      }
      setMapping(m); setTarget(t);
      setName(m.name); setActive(m.is_active);
      setNetworkRows(m.network_mappings || []);
      setStorageRows(m.storage_mappings || []);
      setNamespaceRows(m.namespace_mappings || []);
      setSourceSignals({
        networks: Object.entries(netCount).sort((a, b) => b[1] - a[1]).map(([n, c]) => ({ name: n, count: c })),
        datastores: Object.entries(dsCount).sort((a, b) => b[1] - a[1]).map(([n, c]) => ({ name: n, count: c })),
      });
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  const ensureRow = (rows, key, value) => {
    const existing = rows.find((r) => r[key] === value);
    if (existing) return rows;
    return [...rows, { [key]: value }];
  };

  // Synthesize editor rows from union(source signals + existing rows).
  // Operator deletes a source-row only by clearing the target.
  const allNetworkRows = useMemo(() => {
    const rows = [...networkRows];
    for (const s of sourceSignals.networks) {
      if (!rows.find((r) => r.source_network === s.name)) rows.push({ source_network: s.name });
    }
    return rows;
  }, [networkRows, sourceSignals.networks]);

  const allStorageRows = useMemo(() => {
    const rows = [...storageRows];
    for (const s of sourceSignals.datastores) {
      if (!rows.find((r) => r.source_datastore === s.name)) rows.push({ source_datastore: s.name });
    }
    return rows;
  }, [storageRows, sourceSignals.datastores]);

  const updateNetworkRow = (sourceName, patch) => {
    setNetworkRows((rows) => {
      const next = ensureRow(rows, "source_network", sourceName);
      return next.map((r) => r.source_network === sourceName ? { ...r, ...patch } : r);
    });
  };
  const updateStorageRow = (sourceName, patch) => {
    setStorageRows((rows) => {
      const next = ensureRow(rows, "source_datastore", sourceName);
      return next.map((r) => r.source_datastore === sourceName ? { ...r, ...patch } : r);
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      const patched = await fetchJSON(`/api/mappings/${id}`, {
        method: "PATCH",
        body: {
          name: name.trim(),
          is_active: active,
          network_mappings: networkRows.filter((r) => r.target_network_name),
          storage_mappings: storageRows.filter((r) => r.target_storage_class),
          namespace_mappings: namespaceRows.filter((r) => r.target_namespace),
        },
      });
      toast.success(`Saved · status: ${patched.status}`, TOAST_OPTS);
      setMapping(patched);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  const runPreflight = async () => {
    try {
      setPreflight(await fetchJSON(`/api/mappings/${id}/preflight`, { method: "POST" }));
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  const suggestNetwork = async () => {
    try {
      const r = await fetchJSON(`/api/mappings/${id}/suggest-network`, { method: "POST" });
      const merged = networkRows.slice();
      for (const s of r.suggestions || []) {
        const existing = merged.find((m) => m.source_network === s.source_network);
        if (existing) Object.assign(existing, s);
        else merged.push(s);
      }
      setNetworkRows(merged);
      toast.success(r.rationale_summary || "Suggestions applied", TOAST_OPTS);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };
  const suggestStorage = async () => {
    try {
      const r = await fetchJSON(`/api/mappings/${id}/suggest-storage`, { method: "POST" });
      const merged = storageRows.slice();
      for (const s of r.suggestions || []) {
        const existing = merged.find((m) => m.source_datastore === s.source_datastore);
        if (existing) Object.assign(existing, s);
        else merged.push(s);
      }
      setStorageRows(merged);
      toast.success(r.rationale_summary || "Suggestions applied", TOAST_OPTS);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  if (loading) return <Shell><div style={{ padding: 40, color: "#aaaacc" }}>Loading…</div></Shell>;
  if (err || !mapping) return (
    <Shell>
      <ErrorBlock msg={err || "Mapping not found"} onRetry={load} />
    </Shell>
  );

  const targetNetworks = (target?.network_attachments || []).map((n) => n.name);
  const targetSCs = (target?.storage_classes || []).map((s) => s.name);
  const targetNamespaces = (target?.namespaces || []).map((n) => n.name);

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <input style={{ ...inputStyle, fontWeight: 700, fontSize: 18, padding: "6px 8px", width: 360 }}
            value={name} onChange={(e) => setName(e.target.value)} />
          <div style={{ color: "#aaaacc", fontSize: 12, marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
            {target?.name || "?"} · status: <span style={{ color: STATUS_COLORS[mapping.status] || "#aaaacc" }}>
              {mapping.status}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <label style={{ display: "flex", gap: 6, alignItems: "center", color: "#ccccee", fontSize: 12 }}>
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Active
          </label>
          <button onClick={runPreflight} style={btnGhost}>✓ Preflight</button>
          <button onClick={save} style={btnPrimary} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <Link to="/mappings" style={btnSecondary}>← Back</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32, display: "grid", gap: 28 }}>
        {preflight && <PreflightPanel result={preflight} onClose={() => setPreflight(null)} />}

        <Section title={`Network mappings (${allNetworkRows.length} source rows)`}
          action={<button style={btnGhost} onClick={suggestNetwork}>🤖 Suggest</button>}>
          <RowGrid headers={["Source network", "VMs", "Target NAD/CUDN", "Target NS", "Confidence"]}>
            {allNetworkRows.map((row) => {
              const signal = sourceSignals.networks.find((n) => n.name === row.source_network);
              return (
                <div key={row.source_network} style={editorRow}>
                  <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>{row.source_network}</span>
                  <span style={{ color: "#aaaacc", fontSize: 12 }}>{signal?.count ?? "—"}</span>
                  <select style={inputStyle} value={row.target_network_name || ""}
                    onChange={(e) => updateNetworkRow(row.source_network, { target_network_name: e.target.value || null })}>
                    <option value="">—</option>
                    {targetNetworks.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                  <input style={inputStyle} value={row.target_namespace || ""}
                    placeholder="openshift-multus"
                    onChange={(e) => updateNetworkRow(row.source_network, { target_namespace: e.target.value || null })} />
                  <ConfidencePill value={row.confidence} />
                </div>
              );
            })}
          </RowGrid>
        </Section>

        <Section title={`Storage mappings (${allStorageRows.length} source rows)`}
          action={<button style={btnGhost} onClick={suggestStorage}>🤖 Suggest</button>}>
          <RowGrid headers={["Source datastore", "VMs", "Target StorageClass", "Access mode", "Confidence"]}>
            {allStorageRows.map((row) => {
              const signal = sourceSignals.datastores.find((d) => d.name === row.source_datastore);
              return (
                <div key={row.source_datastore} style={editorRow}>
                  <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>{row.source_datastore}</span>
                  <span style={{ color: "#aaaacc", fontSize: 12 }}>{signal?.count ?? "—"}</span>
                  <select style={inputStyle} value={row.target_storage_class || ""}
                    onChange={(e) => updateStorageRow(row.source_datastore, { target_storage_class: e.target.value || null })}>
                    <option value="">—</option>
                    {targetSCs.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                  <select style={inputStyle} value={row.access_mode || ""}
                    onChange={(e) => updateStorageRow(row.source_datastore, { access_mode: e.target.value || null })}>
                    <option value="">—</option>
                    <option value="ReadWriteOnce">ReadWriteOnce</option>
                    <option value="ReadWriteMany">ReadWriteMany</option>
                    <option value="ReadOnlyMany">ReadOnlyMany</option>
                  </select>
                  <ConfidencePill value={row.confidence} />
                </div>
              );
            })}
          </RowGrid>
        </Section>

        <Section title={`Namespace mappings (${namespaceRows.length})`}
          action={<button style={btnGhost} onClick={() => setNamespaceRows([...namespaceRows,
            { criteria: "default", target_namespace: targetNamespaces[0] || "" }])}>
              + Row
            </button>}>
          <RowGrid headers={["Criteria", "Value", "Target namespace", ""]}>
            {namespaceRows.map((row, i) => (
              <div key={i} style={editorRow}>
                <select style={inputStyle} value={row.criteria}
                  onChange={(e) => setNamespaceRows((rs) => rs.map((r, j) => i === j ? { ...r, criteria: e.target.value } : r))}>
                  <option value="default">default</option>
                  <option value="environment">environment</option>
                  <option value="application">application</option>
                  <option value="vcenter_folder">vcenter_folder</option>
                </select>
                <input style={inputStyle} value={row.criteria_value || ""} disabled={row.criteria === "default"}
                  onChange={(e) => setNamespaceRows((rs) => rs.map((r, j) => i === j ? { ...r, criteria_value: e.target.value } : r))} />
                <select style={inputStyle} value={row.target_namespace}
                  onChange={(e) => setNamespaceRows((rs) => rs.map((r, j) => i === j ? { ...r, target_namespace: e.target.value } : r))}>
                  <option value="">—</option>
                  {targetNamespaces.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
                <button style={{ ...btnGhost, color: "#ff99aa" }}
                  onClick={() => setNamespaceRows((rs) => rs.filter((_, j) => i !== j))}>×</button>
              </div>
            ))}
          </RowGrid>
        </Section>
      </main>
    </Shell>
  );
}

function PreflightPanel({ result, onClose }) {
  const items = [
    ["Unmapped networks", result.unmapped_networks],
    ["Unmapped datastores", result.unmapped_datastores],
    ["Missing storage classes on target", result.missing_storage_classes_on_target],
    ["Missing networks on target", result.missing_networks_on_target],
  ].filter(([, v]) => (v || []).length);
  return (
    <div style={{
      border: `1px solid ${result.ok ? "#00ff88" : "#ffaa00"}`,
      background: result.ok ? "rgba(0,255,136,0.06)" : "rgba(255,170,0,0.06)",
      padding: "14px 18px",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <div style={{ fontWeight: 700, color: result.ok ? "#00ff88" : "#ffaa00" }}>
          {result.ok ? "✓ Pre-flight OK" : "⚠ Pre-flight reports gaps"}
        </div>
        <button onClick={onClose} style={btnSecondary}>×</button>
      </div>
      {items.length > 0 && (
        <ul style={{ marginTop: 10, color: "#ccccee", fontSize: 12, paddingLeft: 18 }}>
          {items.map(([label, vals]) =>
            <li key={label}>{label}: {vals.join(", ")}</li>
          )}
        </ul>
      )}
      {result.warnings?.length > 0 && (
        <ul style={{ marginTop: 10, color: "#aaaacc", fontSize: 12, paddingLeft: 18 }}>
          {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>
  );
}

function Section({ title, action, children }) {
  return (
    <section>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <h2 style={{ fontSize: 15, fontWeight: 700, margin: 0 }}>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
function RowGrid({ headers, children }) {
  return (
    <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${headers.length}, 1fr)`,
        padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
        fontSize: 11, color: "#aaaacc", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {headers.map((h) => <span key={h}>{h}</span>)}
      </div>
      {children}
    </div>
  );
}
function ConfidencePill({ value }) {
  const colors = { high: "#00ff88", medium: "#ffaa00", low: "#ff5577" };
  if (!value) return <span style={{ color: "#aaaacc", fontSize: 12 }}>—</span>;
  return <Pill label={value.toUpperCase()} color={colors[value] || "#aaaacc"} />;
}
function Empty({ disabled, onAdd }) {
  return (
    <div style={{ padding: "60px 40px", border: "1px dashed #2a2a44", textAlign: "center" }}>
      <div style={{ fontSize: 28, color: "#aaaacc", marginBottom: 14 }}>⤳</div>
      <div style={{ fontSize: 16, color: "#eeeeff", marginBottom: 8, fontWeight: 700 }}>
        No resource mappings yet
      </div>
      <div style={{ color: "#aaaacc", fontSize: 14, lineHeight: 1.6, maxWidth: 520, margin: "0 auto 20px" }}>
        Each mapping ties a vCenter source to an OCP target with concrete network /
        storage / namespace mappings. Run target discovery first.
      </div>
      <button onClick={onAdd} style={btnPrimary} disabled={disabled}
        title={disabled ? "Register a vCenter and run OCP target discovery first" : ""}>
        + Create Mapping
      </button>
    </div>
  );
}

function Pill({ label, color }) {
  return (
    <span style={{
      fontSize: 11, color, border: `1px solid ${color}66`,
      padding: "3px 9px", letterSpacing: "0.06em", fontWeight: 700,
      textTransform: "uppercase", fontFamily: "'Share Tech Mono', monospace",
      display: "inline-block", justifySelf: "start",
    }}>{label}</span>
  );
}
function Field({ label, hint, required, children }) {
  return (
    <label style={{ display: "block" }}>
      <span style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", display: "block", marginBottom: 6 }}>
        {label}{required ? " *" : ""}
      </span>
      {children}
      {hint && <span style={{ fontSize: 12, color: "#888899", display: "block", marginTop: 4 }}>{hint}</span>}
    </label>
  );
}
function ErrorBlock({ msg, onRetry }) {
  return (
    <div style={{ padding: "20px 24px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
      <div style={{ color: "#ccaaaa", fontSize: 14 }}>{msg}</div>
      <button onClick={onRetry} style={btnSecondary}>↻ Retry</button>
    </div>
  );
}
function Shell({ children }) {
  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0d0d1a; }
        ::-webkit-scrollbar-thumb { background: #2a2a44; border-radius: 2px; }
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
  display: "grid",
  gridTemplateColumns: "1.4fr 1.2fr 1.2fr 0.6fr 0.6fr 1fr 0.6fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const tableRowStyle = {
  display: "grid",
  gridTemplateColumns: "1.4fr 1.2fr 1.2fr 0.6fr 0.6fr 1fr 0.6fr",
  padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13, cursor: "pointer",
};
const editorRow = {
  display: "grid",
  gridTemplateColumns: "1.5fr 0.5fr 1.5fr 1.2fr 0.7fr",
  padding: "10px 14px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", gap: 10,
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "8px 10px", fontFamily: "'Share Tech Mono', monospace",
  fontSize: 12, width: "100%", outline: "none", borderRadius: 0,
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none", display: "inline-block",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none", display: "inline-block",
};
const btnGhost = {
  background: "transparent", border: "1px solid #2a2a44", color: "#ccccee",
  padding: "8px 12px", fontFamily: "'Barlow', sans-serif", fontSize: 11,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};
const modalOverlay = {
  position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
  display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
};
const modalBox = {
  background: "#0a0a18", border: "1px solid #1a1a2e", width: 600,
  maxWidth: "90vw", maxHeight: "90vh", overflow: "auto",
};
