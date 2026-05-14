import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

// OCP target cluster registry. Operators register the cluster they're
// migrating to and declare the available resources via the per-cluster
// detail page (TargetNetworks / TargetStorageClasses / Namespaces tabs).
// VirtValidate does NOT authenticate to clusters — operator declares
// ground truth, the appliance generates MTV YAML for `oc apply`.

const TOAST_OPTS = {
  style: {
    background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const STATUS_LABEL = {
  active: { label: "ACTIVE", color: "#00ff88" },
  inactive: { label: "INACTIVE", color: "#aaaacc" },
  error: { label: "ERROR", color: "#ff5577" },
};


export default function OCPTargets() {
  const [targets, setTargets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setTargets(await fetchJSON("/api/sources/targets"));
    } catch (e) { setErr(e.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const onDelete = async (t) => {
    if (!window.confirm(`Delete target ${t.name}? Mappings pointing at this cluster will need to be reassigned.`)) return;
    try {
      await fetchJSON(`/api/sources/targets/${t.id}`, { method: "DELETE" });
      toast.success(`Deleted ${t.name}`, TOAST_OPTS);
      await load();
    } catch (e) { toast.error(e.message, TOAST_OPTS); }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>OCP Target Clusters</h1>
          <p style={{ color: "#aaaacc", fontSize: 13, margin: "4px 0 0", lineHeight: 1.5 }}>
            Register destination OpenShift Virtualization clusters and declare
            their available networks, storage classes, and namespaces on the
            cluster detail page. Mappings then route source vSphere resources
            onto operator-declared targets — no live cluster authentication.
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/mappings" style={btnSecondary}>Mappings →</Link>
          <Link to="/" style={btnSecondary}>← Inventory</Link>
          <button onClick={() => setCreateOpen(true)} style={btnPrimary}>+ Register Target</button>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        {loading ? <div style={{ color: "#aaaacc", padding: 40 }}>Loading…</div>
        : err ? <ErrorBlock msg={err} onRetry={load} />
        : targets.length === 0 ? <Empty onAdd={() => setCreateOpen(true)} />
        : (
          <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
            <div style={tableHeaderStyle}>
              {["Name", "Endpoint", "OCP", "Region/Site", "Status", "Actions"].map((h) => (
                <span key={h}>{h}</span>
              ))}
            </div>
            {targets.map((t) => (
              <Row key={t.id} t={t} onDelete={() => onDelete(t)} />
            ))}
          </div>
        )}
      </main>

      {createOpen && (
        <CreateModal onClose={() => setCreateOpen(false)} onSaved={async () => {
          setCreateOpen(false); await load();
        }} />
      )}
    </Shell>
  );
}

function Row({ t, onDelete }) {
  const stat = STATUS_LABEL[t.status] || STATUS_LABEL.inactive;
  const regionSite = [t.region, t.site].filter(Boolean).join(" / ") || "—";
  return (
    <div style={tableRowStyle}>
      <Link to={`/sources/targets/${t.id}`}
        style={{ color: "#eeeeff", fontWeight: 600, textDecoration: "none" }}>
        {t.name}
      </Link>
      <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>{t.api_endpoint}</span>
      <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>{t.ocp_version || "—"}</span>
      <span style={{ color: "#ccccee", fontSize: 12 }}>{regionSite}</span>
      <Pill label={stat.label} color={stat.color} />
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Link to={`/sources/targets/${t.id}`} style={btnGhost}>Open</Link>
        <button onClick={onDelete} style={{ ...btnGhost, color: "#ff99aa", borderColor: "#ff557755" }}>Delete</button>
      </div>
    </div>
  );
}

function CreateModal({ onClose, onSaved }) {
  const [form, setForm] = useState({
    name: "", api_endpoint: "https://api.ocp.example.com:6443",
    region: "", site: "",
    classification_level: "unclassified",
    notes: "",
  });
  const [saving, setSaving] = useState(false);
  const canSave = form.name.trim() && form.api_endpoint.trim();

  const submit = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      const payload = { ...form };
      Object.keys(payload).forEach((k) => {
        if (typeof payload[k] === "string" && payload[k].trim() === "") payload[k] = null;
      });
      payload.name = form.name.trim();
      payload.api_endpoint = form.api_endpoint.trim();
      await fetchJSON("/api/sources/targets", { method: "POST", body: payload });
      toast.success(`Registered ${form.name}`, TOAST_OPTS);
      await onSaved();
    } catch (err) { toast.error(err.message, TOAST_OPTS); }
    finally { setSaving(false); }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>Register OCP Target</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            Declare networks, storage classes, and namespaces on the cluster detail page after registration.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required>
            <input style={inputStyle} value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={128} required autoFocus />
          </Field>
          <Field label="API endpoint" required hint="https://api.cluster.domain:6443">
            <input style={inputStyle} value={form.api_endpoint}
              onChange={(e) => setForm({ ...form, api_endpoint: e.target.value })}
              maxLength={512} required />
          </Field>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="Region"><input style={inputStyle} value={form.region}
              onChange={(e) => setForm({ ...form, region: e.target.value })} /></Field>
            <Field label="Site"><input style={inputStyle} value={form.site}
              onChange={(e) => setForm({ ...form, site: e.target.value })} /></Field>
          </div>
          <Field label="Classification">
            <select style={inputStyle} value={form.classification_level}
              onChange={(e) => setForm({ ...form, classification_level: e.target.value })}>
              <option value="unclassified">Unclassified</option>
              <option value="cui">CUI</option>
              <option value="secret">Secret</option>
              <option value="top_secret">Top Secret</option>
            </select>
          </Field>
          <Field label="Notes">
            <textarea style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={!canSave || saving}
            style={{ ...btnPrimary, opacity: (!canSave || saving) ? 0.5 : 1 }}>
            {saving ? "Saving…" : "Register"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Empty({ onAdd }) {
  return (
    <div style={{ padding: "60px 40px", border: "1px dashed #2a2a44", textAlign: "center" }}>
      <div style={{ fontSize: 28, color: "#aaaacc", marginBottom: 14 }}>◎</div>
      <div style={{ fontSize: 16, color: "#eeeeff", marginBottom: 8, fontWeight: 700 }}>
        No OCP targets registered yet
      </div>
      <div style={{ color: "#aaaacc", fontSize: 14, lineHeight: 1.6, maxWidth: 520, margin: "0 auto 20px" }}>
        Register at least one OpenShift target before generating MTV YAML — plans
        without a mapped target reference placeholder resource names.
      </div>
      <button onClick={onAdd} style={btnPrimary}>+ Register First Target</button>
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
  gridTemplateColumns: "1.2fr 1.6fr 0.6fr 1.2fr 1fr 1.4fr",
  padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
};
const tableRowStyle = {
  display: "grid",
  gridTemplateColumns: "1.2fr 1.6fr 0.6fr 1.2fr 1fr 1.4fr",
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
  background: "#0a0a18", border: "1px solid #1a1a2e", width: 600,
  maxWidth: "90vw", maxHeight: "90vh", overflow: "auto",
};
