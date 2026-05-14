import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useParams } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

// OCP target detail page. Two operator-driven catalogs hang off the
// target row: TargetNetwork (NADs / CUDNs / UDNs / pod-net) and
// TargetStorageClass. The mapping editor's dropdowns are populated
// from these tables. Live cluster discovery isn't authoritative —
// air-gapped operators declare what's available manually.

const TOAST_OPTS = {
  style: {
    background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const NETWORK_TYPE_LABEL = {
  nad: "NAD",
  cudn: "CUDN",
  udn: "UDN",
  pod: "Pod network",
};

export default function OCPTargetDetail() {
  const { id } = useParams();
  const [target, setTarget] = useState(null);
  const [networks, setNetworks] = useState([]);
  const [storageClasses, setStorageClasses] = useState([]);
  const [namespaces, setNamespaces] = useState([]);
  const [namespacesTotal, setNamespacesTotal] = useState(0);
  const [tab, setTab] = useState("networks");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [editNet, setEditNet] = useState(null);    // null = closed; {} = new; row = edit
  const [editSc, setEditSc] = useState(null);
  const [editNs, setEditNs] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const [t, n, s, nsResp] = await Promise.all([
        fetchJSON(`/api/sources/targets/${id}`),
        fetchJSON(`/api/ocp-targets/${id}/networks`),
        fetchJSON(`/api/ocp-targets/${id}/storage-classes`),
        // Namespaces listing is paginated; pull up to 500 for the tab
        // (operators don't typically declare more than that per cluster
        // and the UI is read+inline-edit; no virtualization needed).
        fetchJSON(`/api/ocp-targets/${id}/namespaces?limit=500`),
      ]);
      setTarget(t); setNetworks(n); setStorageClasses(s);
      setNamespaces(nsResp?.items ?? []);
      setNamespacesTotal(nsResp?.total ?? 0);
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  const deleteNetwork = async (row) => {
    if (!window.confirm(`Delete target network "${row.name}"?`)) return;
    try {
      await fetchJSON(`/api/ocp-targets/${id}/networks/${row.id}`,
        { method: "DELETE" });
      toast.success(`Deleted ${row.name}`, TOAST_OPTS);
      await load();
    } catch (e) {
      // 409 message already includes the referencing mapping names —
      // the shared formatApiErrorDetail handles the dict-shaped detail
      // that the delete-protection endpoint returns.
      toast.error(e.message, TOAST_OPTS);
    }
  };

  const deleteSc = async (row) => {
    if (!window.confirm(`Delete target StorageClass "${row.name}"?`)) return;
    try {
      await fetchJSON(`/api/ocp-targets/${id}/storage-classes/${row.id}`,
        { method: "DELETE" });
      toast.success(`Deleted ${row.name}`, TOAST_OPTS);
      await load();
    } catch (e) {
      toast.error(e.message, TOAST_OPTS);
    }
  };

  const deleteNs = async (row) => {
    if (!window.confirm(`Delete target namespace "${row.name}"?`)) return;
    try {
      await fetchJSON(`/api/ocp-targets/${id}/namespaces/${row.id}`,
        { method: "DELETE" });
      toast.success(`Deleted ${row.name}`, TOAST_OPTS);
      await load();
    } catch (e) {
      toast.error(e.message, TOAST_OPTS);
    }
  };

  if (loading) return <Shell><div style={{ padding: 40, color: "#aaaacc" }}>Loading…</div></Shell>;
  if (err || !target) return (
    <Shell>
      <div style={{ padding: 40 }}>
        <ErrorBlock msg={err || "Target not found"} onRetry={load} />
      </div>
    </Shell>
  );

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>{target.name}</h1>
          <div style={{ color: "#aaaacc", fontSize: 12, marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
            {target.api_endpoint}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/sources/targets" style={btnSecondary}>← OCP Targets</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        <div style={{ display: "flex", gap: 6, borderBottom: "1px solid #1a1a2e", marginBottom: 18 }}>
          <Tab active={tab === "networks"} onClick={() => setTab("networks")}>
            Networks ({networks.length})
          </Tab>
          <Tab active={tab === "storage"} onClick={() => setTab("storage")}>
            Storage Classes ({storageClasses.length})
          </Tab>
          <Tab active={tab === "namespaces"} onClick={() => setTab("namespaces")}>
            Namespaces ({namespacesTotal})
          </Tab>
        </div>

        {tab === "networks" && (
          <NetworksTab
            networks={networks}
            onAdd={() => setEditNet({})}
            onEdit={(row) => setEditNet(row)}
            onDelete={deleteNetwork}
          />
        )}
        {tab === "storage" && (
          <StorageTab
            rows={storageClasses}
            onAdd={() => setEditSc({})}
            onEdit={(row) => setEditSc(row)}
            onDelete={deleteSc}
          />
        )}
        {tab === "namespaces" && (
          <NamespacesTab
            rows={namespaces}
            onAdd={() => setEditNs({})}
            onEdit={(row) => setEditNs(row)}
            onDelete={deleteNs}
          />
        )}
      </main>

      {editNet !== null && (
        <NetworkModal
          targetId={id}
          row={editNet.id ? editNet : null}
          onClose={() => setEditNet(null)}
          onSaved={async () => { setEditNet(null); await load(); }}
        />
      )}
      {editSc !== null && (
        <StorageModal
          targetId={id}
          row={editSc.id ? editSc : null}
          onClose={() => setEditSc(null)}
          onSaved={async () => { setEditSc(null); await load(); }}
        />
      )}
      {editNs !== null && (
        <NamespaceModal
          targetId={id}
          row={editNs.id ? editNs : null}
          onClose={() => setEditNs(null)}
          onSaved={async () => { setEditNs(null); await load(); }}
        />
      )}
    </Shell>
  );
}

function NetworksTab({ networks, onAdd, onEdit, onDelete }) {
  if (!networks.length) {
    return (
      <Empty
        icon="◌"
        title="No target networks defined for this cluster"
        body="Add one to start mapping source networks."
        action={<button onClick={onAdd} style={btnPrimary}>+ Add Network</button>}
      />
    );
  }
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
        <button onClick={onAdd} style={btnPrimary}>+ Add Network</button>
      </div>
      <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
        <div style={netHeader}>
          {["Name", "Type", "Namespace", "Default", "Actions"].map((h) => <span key={h}>{h}</span>)}
        </div>
        {networks.map((n) => (
          <div key={n.id} style={netRow}>
            <span style={{ color: "#eeeeff", fontWeight: 600, fontFamily: "'Share Tech Mono', monospace", fontSize: 13 }}>
              {n.name}
            </span>
            <span style={{ color: "#ccccee", fontSize: 12 }}>{NETWORK_TYPE_LABEL[n.network_type] || n.network_type}</span>
            <span style={{ color: "#ccccee", fontSize: 12, fontFamily: "'Share Tech Mono', monospace" }}>
              {n.namespace || "—"}
            </span>
            <span>
              {n.is_default && <Pill label="DEFAULT" color="#00ff88" />}
            </span>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={btnGhost} onClick={() => onEdit(n)}>Edit</button>
              <button style={{ ...btnGhost, color: "#ff99aa", borderColor: "#ff557755" }}
                onClick={() => onDelete(n)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StorageTab({ rows, onAdd, onEdit, onDelete }) {
  if (!rows.length) {
    return (
      <Empty
        icon="▦"
        title="No target StorageClasses defined for this cluster"
        body="Add one to start mapping source datastores."
        action={<button onClick={onAdd} style={btnPrimary}>+ Add StorageClass</button>}
      />
    );
  }
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
        <button onClick={onAdd} style={btnPrimary}>+ Add StorageClass</button>
      </div>
      <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
        <div style={scHeader}>
          {["Name", "Access mode", "Default", "Notes", "Actions"].map((h) => <span key={h}>{h}</span>)}
        </div>
        {rows.map((r) => (
          <div key={r.id} style={scRow}>
            <span style={{ color: "#eeeeff", fontWeight: 600, fontFamily: "'Share Tech Mono', monospace", fontSize: 13 }}>
              {r.name}
            </span>
            <span style={{ color: "#ccccee", fontSize: 12 }}>{r.access_mode}</span>
            <span>
              {r.is_default && <Pill label="DEFAULT" color="#00ff88" />}
            </span>
            <span style={{ color: "#aaaacc", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.notes || ""}
            </span>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={btnGhost} onClick={() => onEdit(r)}>Edit</button>
              <button style={{ ...btnGhost, color: "#ff99aa", borderColor: "#ff557755" }}
                onClick={() => onDelete(r)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------------
function NetworkModal({ targetId, row, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: row?.name || "",
    network_type: row?.network_type || "nad",
    namespace: row?.namespace || "",
    is_default: !!row?.is_default,
    notes: row?.notes || "",
  });
  const [saving, setSaving] = useState(false);
  // NAD must have a namespace; Pod-net has none; CUDN/UDN are optional.
  const namespaceRequired = form.network_type === "nad";
  const namespaceHidden = form.network_type === "pod";
  const canSave = useMemo(() => {
    if (!form.name.trim()) return false;
    if (namespaceRequired && !form.namespace.trim()) return false;
    return true;
  }, [form, namespaceRequired]);

  const submit = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        network_type: form.network_type,
        namespace: namespaceHidden ? null : (form.namespace.trim() || null),
        is_default: form.is_default,
        notes: form.notes.trim() || null,
      };
      const url = row
        ? `/api/ocp-targets/${targetId}/networks/${row.id}`
        : `/api/ocp-targets/${targetId}/networks`;
      await fetchJSON(url, { method: row ? "PATCH" : "POST", body: payload });
      toast.success(row ? `Updated ${form.name}` : `Added ${form.name}`, TOAST_OPTS);
      await onSaved();
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={modalHeader}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>
            {row ? "Edit Target Network" : "Add Target Network"}
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={form.name} autoFocus
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={128} required />
          </Field>
          <Field label="Type" required>
            <select style={inputStyle} value={form.network_type}
              onChange={(e) => setForm({ ...form, network_type: e.target.value })}>
              <option value="nad">NAD (NetworkAttachmentDefinition)</option>
              <option value="cudn">CUDN (ClusterUserDefinedNetwork)</option>
              <option value="udn">UDN (UserDefinedNetwork)</option>
              <option value="pod">Pod network</option>
            </select>
          </Field>
          {!namespaceHidden && (
            <Field label="Namespace" required={namespaceRequired}
              hint={namespaceRequired ? "NADs are namespaced — required" : "Optional for CUDN / UDN"}>
              <input style={inputStyle} value={form.namespace}
                onChange={(e) => setForm({ ...form, namespace: e.target.value })}
                maxLength={128} required={namespaceRequired} />
            </Field>
          )}
          <label style={{ display: "flex", gap: 8, color: "#ccccee", fontSize: 13 }}>
            <input type="checkbox" checked={form.is_default}
              onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            Mark as default for this type
          </label>
          <Field label="Notes">
            <textarea style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>
        </div>
        <div style={modalFooter}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={!canSave || saving}
            style={{ ...btnPrimary, opacity: (!canSave || saving) ? 0.5 : 1 }}>
            {saving ? "Saving…" : row ? "Save" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}

function StorageModal({ targetId, row, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: row?.name || "",
    access_mode: row?.access_mode || "ReadWriteOnce",
    is_default: !!row?.is_default,
    notes: row?.notes || "",
  });
  const [saving, setSaving] = useState(false);
  const canSave = form.name.trim().length > 0;

  const submit = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        access_mode: form.access_mode,
        is_default: form.is_default,
        notes: form.notes.trim() || null,
      };
      const url = row
        ? `/api/ocp-targets/${targetId}/storage-classes/${row.id}`
        : `/api/ocp-targets/${targetId}/storage-classes`;
      await fetchJSON(url, { method: row ? "PATCH" : "POST", body: payload });
      toast.success(row ? `Updated ${form.name}` : `Added ${form.name}`, TOAST_OPTS);
      await onSaved();
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={modalHeader}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>
            {row ? "Edit Target StorageClass" : "Add Target StorageClass"}
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={form.name} autoFocus
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={128} required />
          </Field>
          <Field label="Access mode" required>
            <select style={inputStyle} value={form.access_mode}
              onChange={(e) => setForm({ ...form, access_mode: e.target.value })}>
              <option value="ReadWriteOnce">ReadWriteOnce</option>
              <option value="ReadWriteMany">ReadWriteMany</option>
              <option value="ReadOnlyMany">ReadOnlyMany</option>
            </select>
          </Field>
          <label style={{ display: "flex", gap: 8, color: "#ccccee", fontSize: 13 }}>
            <input type="checkbox" checked={form.is_default}
              onChange={(e) => setForm({ ...form, is_default: e.target.checked })} />
            Mark as default StorageClass for this cluster
          </label>
          <Field label="Notes">
            <textarea style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>
        </div>
        <div style={modalFooter}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={!canSave || saving}
            style={{ ...btnPrimary, opacity: (!canSave || saving) ? 0.5 : 1 }}>
            {saving ? "Saving…" : row ? "Save" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}

function NamespacesTab({ rows, onAdd, onEdit, onDelete }) {
  if (!rows.length) {
    return (
      <Empty
        icon="◐"
        title="No target namespaces declared for this cluster"
        body="Add the namespaces where migrated VMs will land. MTV auto-creates them at plan-apply time if they don't yet exist on the cluster — this catalog is operator intent."
        action={<button onClick={onAdd} style={btnPrimary}>+ Add Namespace</button>}
      />
    );
  }
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
        <button onClick={onAdd} style={btnPrimary}>+ Add Namespace</button>
      </div>
      <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
        <div style={nsHeader}>
          {["Name", "Description", "Actions"].map((h) => <span key={h}>{h}</span>)}
        </div>
        {rows.map((r) => (
          <div key={r.id} style={nsRow}>
            <span style={{ color: "#eeeeff", fontWeight: 600, fontFamily: "'Share Tech Mono', monospace", fontSize: 13 }}>
              {r.name}
            </span>
            <span style={{ color: "#aaaacc", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.description || ""}
            </span>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={btnGhost} onClick={() => onEdit(r)}>Edit</button>
              <button style={{ ...btnGhost, color: "#ff99aa", borderColor: "#ff557755" }}
                onClick={() => onDelete(r)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function NamespaceModal({ targetId, row, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: row?.name || "",
    description: row?.description || "",
  });
  const [saving, setSaving] = useState(false);
  const canSave = form.name.trim().length > 0;

  const submit = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        description: form.description.trim() || null,
      };
      const url = row
        ? `/api/ocp-targets/${targetId}/namespaces/${row.id}`
        : `/api/ocp-targets/${targetId}/namespaces`;
      await fetchJSON(url, { method: row ? "PATCH" : "POST", body: payload });
      toast.success(row ? `Updated ${form.name}` : `Added ${form.name}`, TOAST_OPTS);
      await onSaved();
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={modalHeader}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>
            {row ? "Edit Target Namespace" : "Add Target Namespace"}
          </div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            RFC1123 lowercase letters, digits, and `-`. Max 253 chars.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Namespace name" required>
            <input style={inputStyle} value={form.name} autoFocus
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={253} required />
          </Field>
          <Field label="Description" hint="Free-form note — what lands here">
            <textarea style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </Field>
        </div>
        <div style={modalFooter}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={!canSave || saving}
            style={{ ...btnPrimary, opacity: (!canSave || saving) ? 0.5 : 1 }}>
            {saving ? "Saving…" : row ? "Save" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------
function Tab({ active, onClick, children }) {
  return (
    <button onClick={onClick}
      style={{
        background: "transparent",
        border: "none",
        borderBottom: active ? "2px solid #4488ff" : "2px solid transparent",
        color: active ? "#eeeeff" : "#aaaacc",
        padding: "10px 16px",
        fontFamily: "'Barlow', sans-serif",
        fontSize: 13, fontWeight: 700, letterSpacing: "0.06em",
        textTransform: "uppercase", cursor: "pointer",
      }}>
      {children}
    </button>
  );
}

function Empty({ icon, title, body, action }) {
  return (
    <div style={{ padding: "60px 40px", border: "1px dashed #2a2a44", textAlign: "center", background: "#0a0a18" }}>
      <div style={{ fontSize: 28, color: "#aaaacc", marginBottom: 14 }}>{icon}</div>
      <div style={{ fontSize: 16, color: "#eeeeff", marginBottom: 8, fontWeight: 700 }}>{title}</div>
      <div style={{ color: "#aaaacc", fontSize: 14, lineHeight: 1.6, maxWidth: 520, margin: "0 auto 20px" }}>
        {body}
      </div>
      {action}
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
const netHeader = {
  display: "grid",
  gridTemplateColumns: "1.4fr 0.6fr 1.2fr 0.6fr 1fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const netRow = {
  display: "grid",
  gridTemplateColumns: "1.4fr 0.6fr 1.2fr 0.6fr 1fr",
  padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13,
};
const scHeader = {
  display: "grid",
  gridTemplateColumns: "1.2fr 0.8fr 0.6fr 1.4fr 1fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const scRow = {
  display: "grid",
  gridTemplateColumns: "1.2fr 0.8fr 0.6fr 1.4fr 1fr",
  padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13,
};
const nsHeader = {
  display: "grid",
  gridTemplateColumns: "1.4fr 2fr 1fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const nsRow = {
  display: "grid",
  gridTemplateColumns: "1.4fr 2fr 1fr",
  padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13,
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "10px 12px", fontFamily: "'Share Tech Mono', monospace",
  fontSize: 13, width: "100%", outline: "none", borderRadius: 0,
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
  background: "#0a0a18", border: "1px solid #1a1a2e", width: 540,
  maxWidth: "90vw", maxHeight: "90vh", overflow: "auto",
};
const modalHeader = { borderBottom: "1px solid #1a1a2e", padding: "18px 22px" };
const modalFooter = {
  borderTop: "1px solid #1a1a2e", padding: "14px 22px",
  display: "flex", justifyContent: "flex-end", gap: 10,
};
