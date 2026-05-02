import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";

// Scale-aware vCenter source registry. List + create + edit + delete.
// Categorization (Level 1) trigger ships here too — operators look at
// a vCenter's row and click "Categorize" to fire the batched LLM run.
//
// The page is intentionally minimal: federal customers register
// 5–50 vCenters, so virtualization isn't required. If the count grows
// beyond that the same react-window upgrade tagged for the inventory
// table covers this page too.

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

const CLASSIFICATION_LABEL = {
  unclassified: { label: "UNCLASSIFIED", color: "#88aaff" },
  cui: { label: "CUI", color: "#ffaa00" },
  secret: { label: "SECRET", color: "#ff5577" },
  top_secret: { label: "TOP SECRET", color: "#ff3355" },
};

const STATUS_LABEL = {
  active: { label: "ACTIVE", color: "#00ff88" },
  paused: { label: "PAUSED", color: "#ffaa00" },
  archived: { label: "ARCHIVED", color: "#aaaacc" },
};

async function fetchJSON(url, opts = {}) {
  const init = { method: "GET", ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json())?.detail ?? ""; } catch { /* */ }
    throw new Error(detail ? `HTTP ${r.status}: ${detail}` : `HTTP ${r.status}`);
  }
  if (r.status === 204) return null;
  return await r.json();
}


export default function VCenterSources() {
  const [vcenters, setVcenters] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [categorizing, setCategorizing] = useState(null); // vcenter id when running

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const data = await fetchJSON("/api/sources/vcenters");
      setVcenters(Array.isArray(data) ? data : []);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onDelete = async (vc) => {
    const typed = window.prompt(
      `Type the vCenter name to confirm deletion of ${vc.name}:\n` +
      `${vc.vm_count} VM${vc.vm_count === 1 ? "" : "s"} will be unassigned (not deleted).`
    );
    if (typed !== vc.name) {
      if (typed != null) toast.error("Name didn't match — vCenter not deleted", TOAST_OPTS);
      return;
    }
    try {
      await fetchJSON(`/api/sources/vcenters/${vc.id}`, { method: "DELETE" });
      toast.success(`Deleted ${vc.name}`, TOAST_OPTS);
      await load();
    } catch (e) {
      toast.error(e.message || `Failed to delete ${vc.name}`, TOAST_OPTS);
    }
  };

  const onCategorize = async (vc) => {
    if (vc.vm_count === 0) {
      toast.error("Upload RVTools first — no VMs to categorize", TOAST_OPTS);
      return;
    }
    setCategorizing(vc.id);
    try {
      const spawn = await fetchJSON(`/api/sources/vcenters/${vc.id}/categorize`, {
        method: "POST",
      });
      toast(`Categorizing ${vc.vm_count} VMs in ${vc.name}…`, { ...TOAST_OPTS, icon: "🤖" });
      // Poll until completion. Categorization takes minutes for thousands
      // of VMs; the polling cap is generous.
      const startedAt = Date.now();
      let final = null;
      while (final == null && Date.now() - startedAt < 30 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 4000));
        try {
          const status = await fetchJSON(
            `/api/sources/vcenters/${vc.id}/categorize/${spawn.task_id}`,
          );
          if (status.status === "completed") {
            final = "completed";
            toast.success(
              `Categorized ${status.groups_created} groups across ${status.batches_total} batches`,
              TOAST_OPTS,
            );
          } else if (status.status === "failed") {
            final = "failed";
            toast.error(`Categorization failed: ${status.error}`, { ...TOAST_OPTS, duration: 8000 });
          }
        } catch { /* keep polling */ }
      }
      if (final == null) {
        toast("Still running — check audit log later", { ...TOAST_OPTS, icon: "⏱" });
      }
    } catch (e) {
      toast.error(e.message || "Failed to start categorization", TOAST_OPTS);
    } finally {
      setCategorizing(null);
    }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />

      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>vCenter Sources</h1>
          <p style={{ color: "#aaaacc", fontSize: 13, margin: "4px 0 0", lineHeight: 1.5 }}>
            Register vCenters before uploading RVTools. Per-vCenter scope enables
            delta detection, default mappings, and federal classification boundaries.
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/" style={btnSecondary}>← Inventory</Link>
          <button onClick={() => setCreateOpen(true)} style={btnPrimary}>+ Register vCenter</button>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        {loading ? (
          <div style={{ color: "#aaaacc", padding: 40 }}>Loading…</div>
        ) : err ? (
          <ErrorBlock msg={err} onRetry={load} />
        ) : vcenters.length === 0 ? (
          <Empty onAdd={() => setCreateOpen(true)} />
        ) : (
          <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
            <div style={tableHeaderStyle}>
              {["Name", "Hostname", "Region / Site", "Classification", "Status", "VMs", "Actions"].map((h) => (
                <span key={h}>{h}</span>
              ))}
            </div>
            {vcenters.map((vc) => (
              <Row key={vc.id}
                vc={vc}
                categorizing={categorizing === vc.id}
                onDelete={() => onDelete(vc)}
                onCategorize={() => onCategorize(vc)}
              />
            ))}
          </div>
        )}
      </main>

      {createOpen && (
        <CreateModal onClose={() => setCreateOpen(false)} onSaved={async () => {
          setCreateOpen(false);
          await load();
        }}/>
      )}
    </Shell>
  );
}


function Row({ vc, categorizing, onDelete, onCategorize }) {
  const cls = CLASSIFICATION_LABEL[vc.classification_level] || CLASSIFICATION_LABEL.unclassified;
  const stat = STATUS_LABEL[vc.status] || STATUS_LABEL.active;
  return (
    <div style={tableRowStyle}>
      <span style={{ color: "#eeeeff", fontWeight: 600, fontFamily: "'Barlow', sans-serif" }}>
        {vc.name}
      </span>
      <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>
        {vc.hostname}
      </span>
      <span style={{ color: "#aaaacc", fontSize: 12 }}>
        {vc.region || "—"} {vc.site ? `· ${vc.site}` : ""}
      </span>
      <Pill label={cls.label} color={cls.color} />
      <Pill label={stat.label} color={stat.color} />
      <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>
        {vc.vm_count}
      </span>
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button
          onClick={onCategorize}
          disabled={categorizing || vc.vm_count === 0}
          title={vc.vm_count === 0 ? "Upload RVTools first" : "Run Level 1 categorization"}
          style={{ ...btnGhost, opacity: (categorizing || vc.vm_count === 0) ? 0.5 : 1 }}
        >
          {categorizing ? "Running…" : "🤖 Categorize"}
        </button>
        <button onClick={onDelete} style={{ ...btnGhost, color: "#ff99aa", borderColor: "#ff557755" }}>
          Delete
        </button>
      </div>
    </div>
  );
}


function CreateModal({ onClose, onSaved }) {
  const [form, setForm] = useState({
    name: "",
    hostname: "",
    region: "",
    site: "",
    classification_level: "unclassified",
    default_target_namespace: "",
    default_target_storage_class: "",
    notes: "",
  });
  const [saving, setSaving] = useState(false);

  const canSave = form.name.trim() && form.hostname.trim();

  const submit = async (e) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    try {
      // Strip empty optional fields so the backend persists NULLs not "".
      const payload = Object.fromEntries(
        Object.entries(form).map(([k, v]) => [k, typeof v === "string" && v.trim() === "" ? null : v]),
      );
      payload.name = form.name.trim();
      payload.hostname = form.hostname.trim();
      await fetchJSON("/api/sources/vcenters", { method: "POST", body: payload });
      toast.success(`Registered ${form.name}`, TOAST_OPTS);
      await onSaved();
    } catch (err) {
      toast.error(err.message || "Failed to register vCenter", TOAST_OPTS);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={modalOverlay} onClick={onClose}>
      <form onSubmit={submit} onClick={(e) => e.stopPropagation()} style={modalBox}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>Register vCenter Source</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            VMs imported under this source carry its default mappings + classification.
          </div>
        </div>
        <div style={{ padding: "18px 22px", display: "grid", gap: 12 }}>
          <Field label="Name" required hint="Customer-defined, must be unique">
            <input style={inputStyle} value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={128} required autoFocus />
          </Field>
          <Field label="Hostname" required hint="FQDN of the vCenter (informational — not used for SSH)">
            <input style={inputStyle} value={form.hostname}
              onChange={(e) => setForm({ ...form, hostname: e.target.value })}
              maxLength={255} required />
          </Field>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Field label="Region" hint="us-east, eu-west, …">
              <input style={inputStyle} value={form.region}
                onChange={(e) => setForm({ ...form, region: e.target.value })}
                maxLength={64} />
            </Field>
            <Field label="Site" hint="Data center identifier">
              <input style={inputStyle} value={form.site}
                onChange={(e) => setForm({ ...form, site: e.target.value })}
                maxLength={64} />
            </Field>
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
          <Field label="Default target namespace" hint="Applied to imported VMs that don't carry their own">
            <input style={inputStyle} value={form.default_target_namespace}
              onChange={(e) => setForm({ ...form, default_target_namespace: e.target.value })}
              maxLength={253} />
          </Field>
          <Field label="Default target storage class">
            <input style={inputStyle} value={form.default_target_storage_class}
              onChange={(e) => setForm({ ...form, default_target_storage_class: e.target.value })}
              maxLength={253} />
          </Field>
          <Field label="Notes">
            <textarea style={{ ...inputStyle, minHeight: 60, resize: "vertical" }}
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              maxLength={2048} />
          </Field>
        </div>
        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} style={btnSecondary} disabled={saving}>Cancel</button>
          <button type="submit" disabled={!canSave || saving} style={{ ...btnPrimary, opacity: (!canSave || saving) ? 0.5 : 1 }}>
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
        No vCenter sources yet
      </div>
      <div style={{ color: "#aaaacc", fontSize: 14, lineHeight: 1.6, maxWidth: 520, margin: "0 auto 20px" }}>
        Register the vCenters that own the VMs you&apos;ll migrate. Per-vCenter scope
        enables delta-aware RVTools uploads and Level 1 categorization.
      </div>
      <button onClick={onAdd} style={btnPrimary}>+ Register First vCenter</button>
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
      <span style={{
        fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
        fontWeight: 700, textTransform: "uppercase",
        fontFamily: "'Barlow', sans-serif", display: "block", marginBottom: 6,
      }}>
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


// ---------- styles ----------
const headerStyle = {
  position: "sticky", top: 0, zIndex: 50,
  background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
  borderBottom: "1px solid #1a1a2e",
  display: "flex", alignItems: "center", justifyContent: "space-between",
  padding: "16px 32px", gap: 24,
};

const tableHeaderStyle = {
  display: "grid",
  gridTemplateColumns: "1fr 1.4fr 1fr 1.2fr 0.8fr 0.5fr 1.5fr",
  padding: "12px 18px",
  borderBottom: "1px solid #1a1a2e",
  background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
  fontFamily: "'Barlow', sans-serif",
};

const tableRowStyle = {
  display: "grid",
  gridTemplateColumns: "1fr 1.4fr 1fr 1.2fr 0.8fr 0.5fr 1.5fr",
  padding: "14px 18px",
  borderBottom: "1px solid #0f0f1e",
  alignItems: "center",
  fontSize: 13,
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
