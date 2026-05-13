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
  const [confirmingDelete, setConfirmingDelete] = useState(null);

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
                  <span style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 12 }}>
                    <button
                      type="button"
                      style={btnDanger}
                      title={`Delete ${m.name}`}
                      onClick={(e) => {
                        // The row is wrapped in a Link; without this
                        // the click would also navigate to the editor.
                        e.preventDefault();
                        e.stopPropagation();
                        setConfirmingDelete(m);
                      }}
                    >
                      Delete
                    </button>
                    <span style={{ color: "#aaaacc", fontSize: 12 }}>→</span>
                  </span>
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
      {confirmingDelete && (
        <ConfirmDeleteModal mapping={confirmingDelete}
          onClose={() => setConfirmingDelete(null)}
          onDeleted={async () => { setConfirmingDelete(null); await load(); }} />
      )}
    </Shell>
  );
}

function ConfirmDeleteModal({ mapping, onClose, onDeleted }) {
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(null);

  const confirm = async () => {
    setBusy(true);
    setConflict(null);
    try {
      await fetchJSON(`/api/mappings/${mapping.id}`, { method: "DELETE" });
      toast.success(`Deleted ${mapping.name}`, TOAST_OPTS);
      await onDeleted();
    } catch (e) {
      // The 409 body shape is {detail: {detail, referenced_by: [...]}}.
      // fetchJSON's err.detail is whatever lives under top-level "detail",
      // which here is our nested object. Surface the referencing plans
      // inline rather than toasting a generic message.
      const nested = e?.detail;
      if (e?.status === 409 && nested && typeof nested === "object" && nested.referenced_by) {
        setConflict(nested);
      } else {
        toast.error(e.message, TOAST_OPTS);
      }
      setBusy(false);
    }
  };
  return (
    <div style={modalOverlay} onClick={busy ? undefined : onClose}>
      <div onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: "#ff5577" }}>
            Delete resource mapping?
          </div>
        </div>
        <div style={{ padding: "18px 22px", color: "#ccccee", fontSize: 13, lineHeight: 1.6 }}>
          You are about to delete{" "}
          <strong style={{ color: "#eeeeff" }}>{mapping?.name}</strong>.
          <p style={{ marginTop: 12 }}>
            The backend rejects this delete if any plan still references
            the mapping — delete or reassign those plans first.
          </p>
          {conflict && (
            <div style={{ marginTop: 14, border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)", padding: "12px 14px" }}>
              <div style={{ color: "#ff5577", fontWeight: 700, fontSize: 13 }}>
                Cannot delete — referenced by {(conflict.referenced_by ?? []).length} plan(s)
              </div>
              <ul style={{ marginTop: 8, paddingLeft: 18, color: "#ccccee", fontSize: 12 }}>
                {(conflict.referenced_by ?? []).map((row) => (
                  <li key={row?.plan_id}>
                    <Link to={`/plans/${row?.plan_id}`} style={{ color: "#88aaff" }}>
                      Plan #{row?.plan_id} {row?.plan_name ? `· ${row.plan_name}` : ""}
                    </Link>
                    {row?.status ? <span style={{ color: "#aaaacc" }}> · status: {row.status}</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={busy}>
            {conflict ? "Close" : "Cancel"}
          </button>
          {!conflict && (
            <button type="button" onClick={confirm} disabled={busy}
              style={{ ...btnDanger, opacity: busy ? 0.5 : 1 }}>
              {busy ? "Deleting…" : "Delete"}
            </button>
          )}
        </div>
      </div>
    </div>
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
const DEFAULT_NS_STRATEGY = {
  strategy: "per_environment",
  single_namespace: "migrated-vms",
  per_env_namespaces: {
    production: "prod-vms",
    staging: "staging-vms",
    development: "dev-vms",
  },
  per_app_prefix: "app",
};

// Normalise namespace_mappings as it comes off the wire: legacy list
// shape gets folded into the default strategy; new dict shape passes
// through; null/empty gets the default. Resolves once at load so the
// editor only ever deals with the dict.
function normaliseNamespaceStrategy(raw) {
  if (!raw) return { ...DEFAULT_NS_STRATEGY };
  if (Array.isArray(raw)) {
    // Best-effort migration from the legacy criteria-row schema:
    // if a "default" row exists, promote it to single_namespace and
    // switch the strategy. Anything more nuanced gets reset to the
    // default; the operator was always going to re-make the decision
    // in this UI anyway.
    const defaultRow = raw.find((r) => (r?.criteria || "default") === "default" && r?.target_namespace);
    if (defaultRow) {
      return {
        ...DEFAULT_NS_STRATEGY,
        strategy: "single",
        single_namespace: defaultRow.target_namespace,
      };
    }
    return { ...DEFAULT_NS_STRATEGY };
  }
  // Dict shape — merge over defaults so missing keys don't crash the UI.
  return {
    ...DEFAULT_NS_STRATEGY,
    ...raw,
    per_env_namespaces: { ...DEFAULT_NS_STRATEGY.per_env_namespaces, ...(raw.per_env_namespaces || {}) },
  };
}

// Sentinel value the dropdown emits when the operator picks
// "+ Create new …". Distinct from any real entity name so we can
// detect it in the onChange handler without ambiguity.
const CREATE_NEW_SENTINEL = "__create_new__";

export function ResourceMappingDetail() {
  const { id } = useParams();
  const [mapping, setMapping] = useState(null);
  const [target, setTarget] = useState(null);
  const [targetNetworks, setTargetNetworks] = useState([]);   // TargetNetwork rows
  const [targetSCs, setTargetSCs] = useState([]);             // TargetStorageClass rows
  const [sourceSignals, setSourceSignals] = useState({ networks: [], datastores: [] });
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [networkRows, setNetworkRows] = useState([]);
  const [storageRows, setStorageRows] = useState([]);
  const [nsStrategy, setNsStrategy] = useState(DEFAULT_NS_STRATEGY);
  const [name, setName] = useState("");
  const [active, setActive] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preflight, setPreflight] = useState(null);
  // When the operator picks "+ Create new …" we stash the row that
  // triggered the modal so we can auto-select the new entity onto
  // that row once it's created.
  const [creatingNetworkFor, setCreatingNetworkFor] = useState(null);
  const [creatingSCFor, setCreatingSCFor] = useState(null);
  // Loaded snapshot for the dirty-flag — compared against current
  // state to drive the Save button's disabled state.
  const [loadedSnapshot, setLoadedSnapshot] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const m = await fetchJSON(`/api/mappings/${id}`);
      const t = await fetchJSON(`/api/sources/targets/${m?.ocp_target_id}`);
      const [nets, scs] = await Promise.all([
        fetchJSON(`/api/ocp-targets/${m?.ocp_target_id}/networks`),
        fetchJSON(`/api/ocp-targets/${m?.ocp_target_id}/storage-classes`),
      ]);
      // Source signals: walk VMs in this vCenter to count distinct
      // network names and datastore names. limit=1000 matches the
      // backend MAX_PAGE_SIZE cap.
      const vmsResp = await fetchJSON(`/api/vms?source_vcenter_id=${m?.vcenter_source_id}&limit=1000`);
      const netCount = {}; const dsCount = {};
      for (const vm of (Array.isArray(vmsResp) ? vmsResp : (vmsResp?.items || []))) {
        for (const n of vm?.vsphere_networks || []) netCount[n] = (netCount[n] || 0) + 1;
        for (const d of vm?.vsphere_datastores || []) dsCount[d] = (dsCount[d] || 0) + 1;
      }
      setMapping(m); setTarget(t);
      setTargetNetworks(nets || []); setTargetSCs(scs || []);
      const initName = m?.name ?? "";
      const initActive = !!m?.is_active;
      const initNetRows = m?.network_mappings ?? [];
      const initStoreRows = m?.storage_mappings ?? [];
      const initNs = normaliseNamespaceStrategy(m?.namespace_mappings);
      setName(initName); setActive(initActive);
      setNetworkRows(initNetRows);
      setStorageRows(initStoreRows);
      setNsStrategy(initNs);
      setLoadedSnapshot({
        name: initName,
        active: initActive,
        networkRows: initNetRows,
        storageRows: initStoreRows,
        nsStrategy: initNs,
      });
      setSourceSignals({
        networks: Object.entries(netCount).sort((a, b) => b[1] - a[1]).map(([n, c]) => ({ name: n, count: c })),
        datastores: Object.entries(dsCount).sort((a, b) => b[1] - a[1]).map(([n, c]) => ({ name: n, count: c })),
      });
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  // Refetch only the target-entity catalogs (used after inline create
  // so the dropdown picks up the new row without a full reload).
  const refetchCatalogs = useCallback(async () => {
    if (!mapping?.ocp_target_id) return { nets: [], scs: [] };
    const [nets, scs] = await Promise.all([
      fetchJSON(`/api/ocp-targets/${mapping.ocp_target_id}/networks`),
      fetchJSON(`/api/ocp-targets/${mapping.ocp_target_id}/storage-classes`),
    ]);
    setTargetNetworks(nets || []);
    setTargetSCs(scs || []);
    return { nets: nets || [], scs: scs || [] };
  }, [mapping?.ocp_target_id]);

  // Render one row per distinct source network/datastore (signal-driven).
  // Existing mapping rows are merged so saved-but-no-longer-seen sources
  // remain visible to the operator until they explicitly clear them.
  const allNetworkRows = useMemo(() => {
    const byName = new Map();
    for (const r of networkRows) byName.set(r.source_network, { ...r });
    for (const s of sourceSignals.networks) {
      if (!byName.has(s.name)) byName.set(s.name, { source_network: s.name });
    }
    return Array.from(byName.values());
  }, [networkRows, sourceSignals.networks]);

  const allStorageRows = useMemo(() => {
    const byName = new Map();
    for (const r of storageRows) byName.set(r.source_datastore, { ...r });
    for (const s of sourceSignals.datastores) {
      if (!byName.has(s.name)) byName.set(s.name, { source_datastore: s.name });
    }
    return Array.from(byName.values());
  }, [storageRows, sourceSignals.datastores]);

  const mappedNetworkCount = allNetworkRows.filter((r) => r.target_network_name).length;
  const mappedStorageCount = allStorageRows.filter((r) => r.target_storage_class).length;

  // Dirty flag: did anything change since load? Stringify is good enough
  // here — payloads are small and shape-equal arrays serialize identically.
  const isDirty = useMemo(() => {
    if (!loadedSnapshot) return false;
    return (
      loadedSnapshot.name !== name
      || loadedSnapshot.active !== active
      || JSON.stringify(loadedSnapshot.networkRows) !== JSON.stringify(networkRows)
      || JSON.stringify(loadedSnapshot.storageRows) !== JSON.stringify(storageRows)
      || JSON.stringify(loadedSnapshot.nsStrategy) !== JSON.stringify(nsStrategy)
    );
  }, [loadedSnapshot, name, active, networkRows, storageRows, nsStrategy]);

  // Pick the chosen target's metadata so we can auto-fill the read-only
  // type + namespace columns when the operator picks a target.
  const networkByName = useMemo(() => {
    const m = {};
    for (const n of targetNetworks) m[n.name] = n;
    return m;
  }, [targetNetworks]);

  const updateNetworkTarget = useCallback((sourceName, targetName) => {
    setNetworkRows((rows) => {
      const idx = rows.findIndex((r) => r.source_network === sourceName);
      const meta = targetName ? networkByName[targetName] : null;
      const patched = {
        source_network: sourceName,
        target_network_name: targetName || null,
        target_network_type: meta?.network_type || null,
        target_namespace: meta?.namespace || null,
      };
      if (idx === -1) return [...rows, patched];
      const out = rows.slice();
      out[idx] = { ...out[idx], ...patched };
      return out;
    });
  }, [networkByName]);

  const updateStorageTarget = useCallback((sourceName, targetName) => {
    setStorageRows((rows) => {
      const idx = rows.findIndex((r) => r.source_datastore === sourceName);
      const patched = {
        source_datastore: sourceName,
        target_storage_class: targetName || null,
      };
      if (idx === -1) return [...rows, patched];
      const out = rows.slice();
      out[idx] = { ...out[idx], ...patched };
      return out;
    });
  }, []);

  // Dropdown change handler — intercepts the sentinel and opens the
  // inline-create modal instead of writing it into the row.
  const handleNetworkSelect = (sourceName, value) => {
    if (value === CREATE_NEW_SENTINEL) {
      setCreatingNetworkFor(sourceName);
      return;
    }
    updateNetworkTarget(sourceName, value);
  };
  const handleStorageSelect = (sourceName, value) => {
    if (value === CREATE_NEW_SENTINEL) {
      setCreatingSCFor(sourceName);
      return;
    }
    updateStorageTarget(sourceName, value);
  };

  const onNetworkCreated = async (newEntry) => {
    // Refetch the canonical list so we never trust a single response
    // for downstream meta lookups, then auto-select onto the row.
    const { nets } = await refetchCatalogs();
    const targetRow = creatingNetworkFor;
    setCreatingNetworkFor(null);
    if (targetRow && (nets || []).some((n) => n.name === newEntry?.name)) {
      updateNetworkTarget(targetRow, newEntry.name);
    }
  };
  const onSCCreated = async (newEntry) => {
    const { scs } = await refetchCatalogs();
    const targetRow = creatingSCFor;
    setCreatingSCFor(null);
    if (targetRow && (scs || []).some((s) => s.name === newEntry?.name)) {
      updateStorageTarget(targetRow, newEntry.name);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const patchedNetworkRows = networkRows
        .filter((r) => r.target_network_name)
        .map((r) => {
          const meta = networkByName[r.target_network_name];
          return {
            source_network: r.source_network,
            target_network_name: r.target_network_name,
            target_network_type: meta?.network_type || r.target_network_type || "nad",
            target_namespace: meta?.namespace || r.target_namespace || null,
            confidence: r.confidence,
            rationale: r.rationale,
          };
        });
      const patchedStorageRows = storageRows
        .filter((r) => r.target_storage_class)
        .map((r) => ({
          source_datastore: r.source_datastore,
          target_storage_class: r.target_storage_class,
          access_mode: r.access_mode || null,
          confidence: r.confidence,
          rationale: r.rationale,
        }));
      const patched = await fetchJSON(`/api/mappings/${id}`, {
        method: "PATCH",
        body: {
          name: name.trim(),
          is_active: active,
          network_mappings: patchedNetworkRows,
          storage_mappings: patchedStorageRows,
          namespace_mappings: nsStrategy,
        },
      });
      toast.success(`Saved · status: ${patched?.status ?? "unknown"}`, TOAST_OPTS);
      setMapping(patched);
      // Reset the dirty baseline to what we just saved.
      setLoadedSnapshot({
        name: name.trim(),
        active,
        networkRows: patched?.network_mappings ?? patchedNetworkRows,
        storageRows: patched?.storage_mappings ?? patchedStorageRows,
        nsStrategy,
      });
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  const runPreflight = async () => {
    try {
      setPreflight(await fetchJSON(`/api/mappings/${id}/preflight`, { method: "POST" }));
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  // AI Suggest fills the dropdowns but does NOT save — operator
  // reviews and clicks Save.
  const suggestNetwork = async () => {
    try {
      const r = await fetchJSON(`/api/mappings/${id}/suggest-network`, { method: "POST" });
      setNetworkRows((prev) => {
        const byName = new Map();
        for (const row of prev) byName.set(row.source_network, { ...row });
        for (const s of r.suggestions || []) {
          if (!s.source_network) continue;
          const existing = byName.get(s.source_network) || { source_network: s.source_network };
          byName.set(s.source_network, { ...existing, ...s });
        }
        return Array.from(byName.values());
      });
      toast.success(r.rationale_summary || "Network suggestions applied", TOAST_OPTS);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  const suggestStorage = async () => {
    try {
      const r = await fetchJSON(`/api/mappings/${id}/suggest-storage`, { method: "POST" });
      setStorageRows((prev) => {
        const byName = new Map();
        for (const row of prev) byName.set(row.source_datastore, { ...row });
        for (const s of r.suggestions || []) {
          if (!s.source_datastore) continue;
          const existing = byName.get(s.source_datastore) || { source_datastore: s.source_datastore };
          byName.set(s.source_datastore, { ...existing, ...s });
        }
        return Array.from(byName.values());
      });
      toast.success(r.rationale_summary || "Storage suggestions applied", TOAST_OPTS);
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  if (loading) return <Shell><div style={{ padding: 40, color: "#aaaacc" }}>Loading…</div></Shell>;
  if (err || !mapping) return (
    <Shell>
      <ErrorBlock msg={err || "Mapping not found"} onRetry={load} />
    </Shell>
  );

  const noTargetNetworks = targetNetworks.length === 0;
  const noTargetSCs = targetSCs.length === 0;

  const mappingStatus = mapping?.status ?? "incomplete";

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div style={{ minWidth: 0 }}>
          <div style={{ color: "#88aaff", fontSize: 11, letterSpacing: "0.06em", marginBottom: 4, textTransform: "uppercase", fontWeight: 700 }}>
            <Link to="/mappings" style={{ color: "#88aaff", textDecoration: "none" }}>Resource Mappings</Link>
            <span style={{ color: "#3a3a55", margin: "0 8px" }}>/</span>
            <span style={{ color: "#aaaacc" }}>{name || "(unnamed)"}</span>
          </div>
          <input style={{ ...inputStyle, fontWeight: 700, fontSize: 18, padding: "6px 8px", width: 360 }}
            value={name} onChange={(e) => setName(e.target.value)} />
          <div style={{ color: "#aaaacc", fontSize: 12, marginTop: 6, fontFamily: "'Share Tech Mono', monospace", display: "flex", gap: 14, flexWrap: "wrap" }}>
            <Link to={`/sources/targets/${mapping?.ocp_target_id}`} style={{ color: "#88aaff", textDecoration: "none" }}>
              {target?.name || "?"}
            </Link>
            <span>
              status:{" "}
              <span style={{ color: STATUS_COLORS[mappingStatus] || "#aaaacc", fontWeight: 700 }}>
                {String(mappingStatus).replace("_", " ").toUpperCase()}
              </span>
            </span>
            <span>
              {mappedNetworkCount} of {allNetworkRows.length} networks mapped
              {" · "}
              {mappedStorageCount} of {allStorageRows.length} datastores mapped
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label style={{ display: "flex", gap: 6, alignItems: "center", color: "#ccccee", fontSize: 12 }}>
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Active
          </label>
          <button onClick={runPreflight} style={btnGhost}>✓ Preflight</button>
          <button onClick={save} style={{ ...btnPrimary, opacity: (saving || !isDirty) ? 0.5 : 1 }} disabled={saving || !isDirty}
            title={!isDirty ? "No changes to save" : ""}>
            {saving ? "Saving…" : isDirty ? "Save" : "Saved"}
          </button>
          <Link to="/mappings" style={btnSecondary}>← Back</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32, display: "grid", gap: 28 }}>
        {preflight && <PreflightPanel result={preflight} onClose={() => setPreflight(null)} />}

        <NamespaceStrategySection strategy={nsStrategy} setStrategy={setNsStrategy} />

        <Section
          title={`Network mappings — ${mappedNetworkCount} of ${allNetworkRows.length} source networks mapped`}
          action={
            <button
              style={{ ...btnGhost, opacity: noTargetNetworks ? 0.5 : 1 }}
              onClick={suggestNetwork}
              disabled={noTargetNetworks}
              title={noTargetNetworks ? "Define target networks on the cluster first" : "Ask the LLM for matches"}
            >
              🤖 AI Suggest
            </button>
          }
        >
          {noTargetNetworks && (
            <EmptyCatalogHint
              kind="networks"
              targetId={mapping?.ocp_target_id}
              onCreate={() => setCreatingNetworkFor("")}
            />
          )}
          {allNetworkRows.length === 0 ? null : (
            <RowGrid
              template={netEditorRow.gridTemplateColumns}
              headers={["", "Source network", "VMs", "Target network", "Type", "Namespace", "Confidence"]}>
              {allNetworkRows.map((row) => {
                const signal = (sourceSignals.networks || []).find((n) => n.name === row.source_network);
                const meta = row.target_network_name ? networkByName[row.target_network_name] : null;
                const mapped = !!row.target_network_name;
                return (
                  <div key={row.source_network} style={netEditorRow}>
                    <StatusDot mapped={mapped} />
                    <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#eeeeff" }}>
                      {row.source_network}
                    </span>
                    <span style={{ color: "#aaaacc", fontSize: 12 }}>{signal?.count ?? "—"}</span>
                    <select style={inputStyle}
                      value={row.target_network_name || ""}
                      onChange={(e) => handleNetworkSelect(row.source_network, e.target.value)}>
                      <option value="">— None selected —</option>
                      {(targetNetworks || []).map((n) => (
                        <option key={n.id} value={n.name}>
                          {n.name} ({n.network_type}{n.namespace ? ` · ${n.namespace}` : ""})
                        </option>
                      ))}
                      <option value={CREATE_NEW_SENTINEL}>+ Create new target network…</option>
                    </select>
                    <span style={{ color: "#aaaacc", fontSize: 12, fontFamily: "'Share Tech Mono', monospace" }}>
                      {meta?.network_type || "—"}
                    </span>
                    <span style={{ color: "#aaaacc", fontSize: 12, fontFamily: "'Share Tech Mono', monospace" }}>
                      {meta?.namespace || "—"}
                    </span>
                    <ConfidencePill value={row.confidence} />
                  </div>
                );
              })}
            </RowGrid>
          )}
        </Section>

        <Section
          title={`Storage mappings — ${mappedStorageCount} of ${allStorageRows.length} source datastores mapped`}
          action={
            <button
              style={{ ...btnGhost, opacity: noTargetSCs ? 0.5 : 1 }}
              onClick={suggestStorage}
              disabled={noTargetSCs}
              title={noTargetSCs ? "Define target storage classes on the cluster first" : "Ask the LLM for matches"}
            >
              🤖 AI Suggest
            </button>
          }
        >
          {noTargetSCs && (
            <EmptyCatalogHint
              kind="storage classes"
              targetId={mapping?.ocp_target_id}
              onCreate={() => setCreatingSCFor("")}
            />
          )}
          {allStorageRows.length === 0 ? null : (
            <RowGrid
              template={storageEditorRow.gridTemplateColumns}
              headers={["", "Source datastore", "VMs", "Target StorageClass", "Access mode", "Confidence"]}>
              {allStorageRows.map((row) => {
                const signal = (sourceSignals.datastores || []).find((d) => d.name === row.source_datastore);
                const mapped = !!row.target_storage_class;
                const sc = (targetSCs || []).find((s) => s.name === row.target_storage_class);
                return (
                  <div key={row.source_datastore} style={storageEditorRow}>
                    <StatusDot mapped={mapped} />
                    <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#eeeeff" }}>
                      {row.source_datastore}
                    </span>
                    <span style={{ color: "#aaaacc", fontSize: 12 }}>{signal?.count ?? "—"}</span>
                    <select style={inputStyle}
                      value={row.target_storage_class || ""}
                      onChange={(e) => handleStorageSelect(row.source_datastore, e.target.value)}>
                      <option value="">— None selected —</option>
                      {(targetSCs || []).map((s) => (
                        <option key={s.id} value={s.name}>{s.name} ({s.access_mode})</option>
                      ))}
                      <option value={CREATE_NEW_SENTINEL}>+ Create new storage class…</option>
                    </select>
                    <span style={{ color: "#aaaacc", fontSize: 12, fontFamily: "'Share Tech Mono', monospace" }}>
                      {sc?.access_mode || row.access_mode || "—"}
                    </span>
                    <ConfidencePill value={row.confidence} />
                  </div>
                );
              })}
            </RowGrid>
          )}
        </Section>
      </main>
      {creatingNetworkFor !== null && (
        <InlineCreateNetworkModal
          targetId={mapping?.ocp_target_id}
          onClose={() => setCreatingNetworkFor(null)}
          onCreated={onNetworkCreated}
        />
      )}
      {creatingSCFor !== null && (
        <InlineCreateStorageClassModal
          targetId={mapping?.ocp_target_id}
          onClose={() => setCreatingSCFor(null)}
          onCreated={onSCCreated}
        />
      )}
    </Shell>
  );
}

function InlineCreateNetworkModal({ targetId, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [type, setType] = useState("nad");
  const [namespace, setNamespace] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const created = await fetchJSON(`/api/ocp-targets/${targetId}/networks`, {
        method: "POST",
        body: {
          name: name.trim(),
          network_type: type,
          namespace: namespace.trim() || null,
          is_default: isDefault,
        },
      });
      toast.success(`Created ${created?.name ?? name}`, TOAST_OPTS);
      await onCreated(created);
    } catch (err) {
      toast.error(err.message, TOAST_OPTS);
      setBusy(false);
    }
  };

  return (
    <div style={modalOverlay} onClick={busy ? undefined : onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>New target network</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            Operator-declared NetworkAttachmentDefinition / CUDN / UDN /
            Pod-network entry for this cluster.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)}
              required autoFocus maxLength={128} placeholder="e.g. prod-vlan-100" />
          </Field>
          <Field label="Type" required>
            <select style={inputStyle} value={type} onChange={(e) => setType(e.target.value)} required>
              <option value="nad">NAD — NetworkAttachmentDefinition</option>
              <option value="cudn">CUDN — Cluster User-Defined Network</option>
              <option value="udn">UDN — User-Defined Network (namespaced)</option>
              <option value="pod">Pod network (default cluster network)</option>
            </select>
          </Field>
          {type !== "pod" && (
            <Field label="Namespace" hint="Where the NAD/UDN lives (NADs are namespaced; CUDN is cluster-scoped)">
              <input style={inputStyle} value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                maxLength={128} placeholder="openshift-multus" />
            </Field>
          )}
          <label style={{ display: "flex", gap: 8, color: "#ccccee", fontSize: 13 }}>
            <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
            Default for this type
          </label>
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={busy}>Cancel</button>
          <button type="submit" disabled={busy || !name.trim()}
            style={{ ...btnPrimary, opacity: (busy || !name.trim()) ? 0.5 : 1 }}>
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}

function InlineCreateStorageClassModal({ targetId, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [accessMode, setAccessMode] = useState("ReadWriteOnce");
  const [isDefault, setIsDefault] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const created = await fetchJSON(`/api/ocp-targets/${targetId}/storage-classes`, {
        method: "POST",
        body: {
          name: name.trim(),
          access_mode: accessMode,
          is_default: isDefault,
        },
      });
      toast.success(`Created ${created?.name ?? name}`, TOAST_OPTS);
      await onCreated(created);
    } catch (err) {
      toast.error(err.message, TOAST_OPTS);
      setBusy(false);
    }
  };

  return (
    <div style={modalOverlay} onClick={busy ? undefined : onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>New target storage class</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            Operator-declared StorageClass entry. Plans render this name
            into MTV YAML at generation time.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)}
              required autoFocus maxLength={128} placeholder="e.g. ocs-rbd" />
          </Field>
          <Field label="Access mode" required>
            <select style={inputStyle} value={accessMode}
              onChange={(e) => setAccessMode(e.target.value)} required>
              <option value="ReadWriteOnce">ReadWriteOnce (RWO)</option>
              <option value="ReadWriteMany">ReadWriteMany (RWX)</option>
              <option value="ReadOnlyMany">ReadOnlyMany (ROX)</option>
            </select>
          </Field>
          <label style={{ display: "flex", gap: 8, color: "#ccccee", fontSize: 13 }}>
            <input type="checkbox" checked={isDefault} onChange={(e) => setIsDefault(e.target.checked)} />
            Default for this cluster
          </label>
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={busy}>Cancel</button>
          <button type="submit" disabled={busy || !name.trim()}
            style={{ ...btnPrimary, opacity: (busy || !name.trim()) ? 0.5 : 1 }}>
            {busy ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}

function EmptyCatalogHint({ kind, targetId, onCreate }) {
  return (
    <div style={{ border: "1px dashed #2a2a44", background: "#0a0a18", padding: "18px 22px", marginBottom: 14, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
      <div>
        <div style={{ color: "#eeeeff", fontSize: 14, fontWeight: 700 }}>
          No target {kind} defined for this cluster yet
        </div>
        <div style={{ color: "#aaaacc", fontSize: 13, marginTop: 4 }}>
          Add one inline below, or manage the full catalog on the
          {" "}
          <Link to={`/sources/targets/${targetId}`} style={{ color: "#88aaff" }}>target detail page</Link>.
        </div>
      </div>
      <button type="button" onClick={onCreate} style={btnPrimary}>+ Create new</button>
    </div>
  );
}

function NamespaceStrategySection({ strategy, setStrategy }) {
  const choose = (s) => setStrategy({ ...strategy, strategy: s });
  return (
    <Section title="Namespace strategy">
      <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: "18px 22px" }}>
        <div style={{ display: "flex", gap: 18, marginBottom: 14 }}>
          {[
            ["single", "Single namespace"],
            ["per_environment", "Per-environment"],
            ["per_application", "Per-application"],
          ].map(([key, label]) => (
            <label key={key} style={{ display: "flex", gap: 8, alignItems: "center", color: "#ccccee", fontSize: 13, cursor: "pointer" }}>
              <input type="radio" name="ns-strategy"
                checked={strategy.strategy === key}
                onChange={() => choose(key)} />
              {label}
            </label>
          ))}
        </div>

        {strategy.strategy === "single" && (
          <Field label="Target namespace" hint="All VMs land in this namespace">
            <input style={{ ...inputStyle, maxWidth: 320 }}
              value={strategy.single_namespace || ""}
              placeholder="migrated-vms"
              onChange={(e) => setStrategy({ ...strategy, single_namespace: e.target.value })}
              maxLength={253} />
          </Field>
        )}

        {strategy.strategy === "per_environment" && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14, maxWidth: 720 }}>
            {["production", "staging", "development"].map((env) => (
              <Field key={env} label={env}>
                <input style={inputStyle}
                  value={strategy.per_env_namespaces?.[env] || ""}
                  onChange={(e) => setStrategy({
                    ...strategy,
                    per_env_namespaces: { ...(strategy.per_env_namespaces || {}), [env]: e.target.value },
                  })}
                  maxLength={253} />
              </Field>
            ))}
          </div>
        )}

        {strategy.strategy === "per_application" && (
          <div style={{ color: "#ccccee", fontSize: 13, lineHeight: 1.6 }}>
            VMs will be grouped into namespaces based on detected application
            tags. Defaults to{" "}
            <code style={{ background: "#1a1a2e", padding: "2px 6px", color: "#88aaff" }}>
              {`${strategy.per_app_prefix || "app"}-<name>-vms`}
            </code>.
            <div style={{ marginTop: 12, maxWidth: 320 }}>
              <Field label="Prefix" hint="Used as the namespace name prefix">
                <input style={inputStyle}
                  value={strategy.per_app_prefix || ""}
                  onChange={(e) => setStrategy({ ...strategy, per_app_prefix: e.target.value })}
                  maxLength={64} />
              </Field>
            </div>
          </div>
        )}
      </div>
    </Section>
  );
}

function StatusDot({ mapped }) {
  return (
    <span title={mapped ? "Mapped" : "Unmapped"}
      style={{
        width: 10, height: 10, borderRadius: "50%",
        background: mapped ? "#00ff88" : "#ff5577",
        display: "inline-block",
      }} />
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
function RowGrid({ headers, template, children }) {
  // template overrides the default equal-column grid when caller
  // needs widths that match a custom row layout (e.g. status dot
  // first, then variable-width source/target columns).
  const gridTemplateColumns = template || `repeat(${headers.length}, 1fr)`;
  return (
    <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
      <div style={{ display: "grid", gridTemplateColumns, gap: 10,
        padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
        fontSize: 11, color: "#aaaacc", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {headers.map((h, i) => <span key={`${h || "col"}-${i}`}>{h}</span>)}
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
  gridTemplateColumns: "1.4fr 1.2fr 1.2fr 0.6fr 0.6fr 1fr 1.1fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const tableRowStyle = {
  display: "grid",
  gridTemplateColumns: "1.4fr 1.2fr 1.2fr 0.6fr 0.6fr 1fr 1.1fr",
  padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13, cursor: "pointer",
};
const netEditorRow = {
  display: "grid",
  gridTemplateColumns: "30px 1.4fr 0.5fr 1.8fr 0.6fr 1fr 0.7fr",
  padding: "10px 14px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", gap: 10,
};
const storageEditorRow = {
  display: "grid",
  gridTemplateColumns: "30px 1.4fr 0.5fr 1.8fr 1fr 0.7fr",
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
const btnDanger = {
  background: "transparent", border: "1px solid #ff5577", color: "#ff5577",
  padding: "6px 12px", fontFamily: "'Barlow', sans-serif", fontSize: 11,
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
