import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

import { parseRVToolsXLSX } from "../utils/parseRVTools";

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



export default function VCenterSources() {
  const [vcenters, setVcenters] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [uploadFor, setUploadFor] = useState(null); // vcenter row when uploading
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
                onUpload={() => setUploadFor(vc)}
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
      {uploadFor && (
        <UploadRVToolsModal
          vcenter={uploadFor}
          onClose={() => setUploadFor(null)}
          onImported={async () => { setUploadFor(null); await load(); }}
        />
      )}
    </Shell>
  );
}


function Row({ vc, categorizing, onDelete, onCategorize, onUpload }) {
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
          onClick={onUpload}
          title="Upload RVTools XLSX (preview + import)"
          style={btnGhost}
        >
          ⬆ Upload RVTools
        </button>
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


function UploadRVToolsModal({ vcenter, onClose, onImported }) {
  // Three-stage modal: pick file → review preview → confirm import.
  // Stays mounted across stages so the operator can re-pick a file
  // without closing.
  const [stage, setStage] = useState("pick"); // pick | preview | importing | done
  const [filename, setFilename] = useState("");
  const [parseStats, setParseStats] = useState(null);
  const [vms, setVms] = useState([]);
  const [delta, setDelta] = useState(null);
  const [importResult, setImportResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const onFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const result = await parseRVToolsXLSX(file);
      if (result.vms.length === 0) {
        toast.error("No VMs parsed from file — wrong sheet?", TOAST_OPTS);
        return;
      }
      setFilename(file.name);
      setParseStats({
        total: result.totalRows,
        parsed: result.vms.length,
        skipped: result.skipped.length,
        sheet: result.sheetName,
      });
      setVms(result.vms);
      // Immediately fire the preview so the operator sees the delta
      // before they confirm. Big files might take a beat.
      const previewBody = await fetchJSON(
        `/api/sources/vcenters/${vcenter.id}/rvtools/preview`,
        { method: "POST", body: { vms: result.vms } },
      );
      setDelta(previewBody);
      setStage("preview");
    } catch (err) {
      toast.error(err.message || "Failed to parse XLSX", TOAST_OPTS);
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = async () => {
    setBusy(true);
    setStage("importing");
    try {
      const body = await fetchJSON(
        `/api/sources/vcenters/${vcenter.id}/rvtools/import`,
        { method: "POST", body: { vms } },
      );
      // Async branch — body has task_id but no `created` counts.
      if (body?.task_id && body.status === "running") {
        const startedAt = Date.now();
        let final = null;
        while (final == null && Date.now() - startedAt < 10 * 60 * 1000) {
          await new Promise((r) => setTimeout(r, 2000));
          try {
            const status = await fetchJSON(
              `/api/sources/vcenters/${vcenter.id}/rvtools/import/${body.task_id}`,
            );
            if (status.status === "completed") {
              final = status.result;
            } else if (status.status === "failed") {
              throw new Error(status.error || "Import failed");
            }
          } catch (e) {
            // 404 / network blip — keep polling unless explicit failure
            if (String(e.message).includes("not found")) throw e;
          }
        }
        setImportResult(final);
      } else {
        setImportResult(body);
      }
      setStage("done");
      const r = body.task_id ? importResult : body;
      const counts = r || {};
      toast.success(
        `Imported · ${counts.created ?? 0} new · ${counts.updated ?? 0} updated · ${counts.marked_missing ?? 0} missing`,
        TOAST_OPTS,
      );
    } catch (err) {
      toast.error(err.message || "Import failed", TOAST_OPTS);
      setStage("preview");
    } finally {
      setBusy(false);
    }
  };

  const close = async () => {
    if (stage === "done") {
      await onImported();
    } else {
      onClose();
    }
  };

  return (
    <div style={modalOverlay} onClick={busy ? undefined : close}>
      <div onClick={(e) => e.stopPropagation()} style={{ ...modalBox, width: 720 }}>
        <div style={{ borderBottom: "1px solid #1a1a2e", padding: "18px 22px" }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>Upload RVTools — {vcenter.name}</div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4 }}>
            Parses the vInfo sheet. Preview shows the delta against current
            inventory in this vCenter; confirm to commit.
          </div>
        </div>

        <div style={{ padding: "18px 22px", display: "grid", gap: 14 }}>
          {stage === "pick" && (
            <label style={{
              display: "flex", flexDirection: "column", alignItems: "center",
              padding: 32, border: "1px dashed #3a3a55", cursor: busy ? "not-allowed" : "pointer",
              background: "#07070f",
            }}>
              <div style={{ fontSize: 28, color: "#aaaacc", marginBottom: 12 }}>↑</div>
              <div style={{ fontSize: 14, color: "#eeeeff", fontWeight: 600 }}>
                {busy ? "Parsing…" : "Click to choose RVTools .xlsx"}
              </div>
              <div style={{ fontSize: 12, color: "#888899", marginTop: 6 }}>
                Looks for a sheet named &quot;vInfo&quot; (falls back to first sheet).
              </div>
              <input
                type="file"
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                style={{ display: "none" }}
                onChange={onFile}
                disabled={busy}
              />
            </label>
          )}

          {(stage === "preview" || stage === "importing" || stage === "done") && parseStats && (
            <div style={{
              padding: "10px 14px", border: "1px solid #1a1a2e", background: "#0a0a16",
              fontSize: 12, color: "#ccccee",
            }}>
              <div><strong>{filename}</strong> · sheet: {parseStats.sheet}</div>
              <div style={{ color: "#aaaacc", marginTop: 4 }}>
                Parsed {parseStats.parsed} of {parseStats.total} rows
                {parseStats.skipped > 0 ? ` (${parseStats.skipped} skipped — missing both name and hostname)` : ""}
              </div>
            </div>
          )}

          {(stage === "preview" || stage === "importing") && delta && (
            <DeltaSummaryGrid delta={delta} />
          )}

          {stage === "done" && importResult && (
            <DoneSummary result={importResult} />
          )}
        </div>

        <div style={{ borderTop: "1px solid #1a1a2e", padding: "14px 22px", display: "flex", justifyContent: "flex-end", gap: 10 }}>
          {stage === "pick" && (
            <button onClick={onClose} style={btnSecondary} disabled={busy}>Cancel</button>
          )}
          {stage === "preview" && (
            <>
              <button onClick={() => { setStage("pick"); setDelta(null); }} style={btnSecondary} disabled={busy}>← Pick another file</button>
              <button onClick={onConfirm} style={btnPrimary} disabled={busy}>
                {busy ? "Importing…" : "Confirm Import"}
              </button>
            </>
          )}
          {stage === "importing" && (
            <button style={btnPrimary} disabled>Importing…</button>
          )}
          {stage === "done" && (
            <button onClick={close} style={btnPrimary}>Close</button>
          )}
        </div>
      </div>
    </div>
  );
}

function DeltaSummaryGrid({ delta }) {
  const buckets = [
    ["new", "#00ff88", delta.new?.length ?? 0, "VMs to create"],
    ["updated", "#ffaa00", delta.updated?.length ?? 0, "Tracked fields changed"],
    ["unchanged", "#aaaacc", delta.unchanged?.length ?? 0, "Already match"],
    ["removed", "#ff5577", delta.removed?.length ?? 0, "Will be flagged missing (not deleted)"],
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
      {buckets.map(([label, color, count, hint]) => (
        <div key={label} style={{
          padding: "10px 12px", border: `1px solid ${color}55`,
          background: `${color}0d`,
        }}>
          <div style={{ fontSize: 11, color, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            {label}
          </div>
          <div style={{ fontSize: 22, color: "#eeeeff", fontFamily: "'Share Tech Mono', monospace", marginTop: 2 }}>
            {count}
          </div>
          <div style={{ fontSize: 11, color: "#888899", marginTop: 4 }}>{hint}</div>
        </div>
      ))}
    </div>
  );
}

function DoneSummary({ result }) {
  const items = [
    ["Created", result.created],
    ["Updated", result.updated],
    ["Marked missing", result.marked_missing],
    ["Unchanged", result.unchanged],
  ];
  return (
    <div style={{ padding: "16px 18px", border: "1px solid #00ff8855", background: "rgba(0,255,136,0.06)" }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: "#00ff88", marginBottom: 8 }}>
        ✓ Import complete
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
        {items.map(([label, count]) => (
          <div key={label}>
            <div style={{ fontSize: 11, color: "#aaaacc", textTransform: "uppercase" }}>{label}</div>
            <div style={{ fontSize: 18, fontFamily: "'Share Tech Mono', monospace", color: "#eeeeff" }}>{count ?? 0}</div>
          </div>
        ))}
      </div>
      {(result.errors || []).length > 0 && (
        <div style={{ marginTop: 10, fontSize: 12, color: "#ff99aa" }}>
          {result.errors.length} error{result.errors.length === 1 ? "" : "s"} — see audit log
        </div>
      )}
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
  gridTemplateColumns: "1fr 1.4fr 1fr 1.2fr 0.8fr 0.5fr 2.4fr",
  padding: "12px 18px",
  borderBottom: "1px solid #1a1a2e",
  background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontWeight: 700, textTransform: "uppercase",
  fontFamily: "'Barlow', sans-serif",
};

const tableRowStyle = {
  display: "grid",
  gridTemplateColumns: "1fr 1.4fr 1fr 1.2fr 0.8fr 0.5fr 2.4fr",
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
