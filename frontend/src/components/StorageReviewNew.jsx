import { useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate } from "react-router-dom";
import { throwForResponse } from "../utils/apiError";

// New Storage Design Review wizard. Sister page to NetworkReviewNew —
// same single-page form pattern (name → notes → YAML → analyze) but
// scoped to the storage layer: StorageClass / VolumeSnapshotClass /
// StorageMap manifests + storage-tier customer notes.

const TOAST_OPTS = {
  style: { background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
           fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5 },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

// Realistic OCP-Virt storage placeholder. Authored at module level
// (not inline in JSX) so the multiline string keeps real newlines
// — JSX attribute parsing chokes on \n escapes embedded in attributes.
const YAML_PLACEHOLDER = `# Proposed OpenShift Virtualization storage design (multi-document)
# Paste your StorageClass / VolumeSnapshotClass / StorageMap manifests here.
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: tier1-ssd-replicated
provisioner: openshift-storage.rbd.csi.ceph.com
parameters:
  pool: replicapool
  csi.storage.k8s.io/fstype: ext4
allowVolumeExpansion: true
reclaimPolicy: Retain
---
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: tier1-ssd-snapshots
driver: openshift-storage.rbd.csi.ceph.com
deletionPolicy: Retain
---
apiVersion: forklift.konveyor.io/v1beta1
kind: StorageMap
metadata:
  name: vmware-to-ocs
  namespace: openshift-mtv
spec:
  map:
    - source:
        name: vmware-prod-tier1
      destination:
        storageClass: tier1-ssd-replicated
`;

const NOTES_PLACEHOLDER = `# Source storage environment notes

Tell the analyzer about anything not visible in RVTools:
  - Performance tier of each datastore (SSD vs HDD, IOPS class)
  - Latency requirements ("production DBs need <5ms")
  - Replication topology (sync vs async, peer site)
  - Backup strategy (VMware snapshots, array snapshots, agent-based)
  - Multipath policy (round-robin, ALUA failover, fixed)
  - Shared-disk patterns (Oracle RAC, Microsoft cluster, GFS2)
  - Capacity headroom expectations

Example:
  prod-array-tier1: SSD, replicated to DR site, snapshots daily.
  prod-array-tier2: HDD, no replication, weekly backups.
  RAC cluster shares VMDKs across 3 nodes on prod-array-tier1.
`;

async function fetchJSON(url, opts = {}) {
  const init = { method: "GET", ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (!r.ok) await throwForResponse(r);
  return r.json();
}

export default function StorageReviewNew() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [yaml, setYaml] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [analyzeNow, setAnalyzeNow] = useState(true);

  const onFile = (setter) => async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    try {
      setter(await f.text());
      toast.success(`Loaded ${f.name}`, TOAST_OPTS);
    } catch (err) {
      toast.error(`Could not read ${f.name}: ${err.message}`, TOAST_OPTS);
    }
  };

  const canSubmit = name.trim().length > 0 && !submitting;

  const submit = async (e) => {
    e?.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const created = await fetchJSON("/api/storage-reviews", {
        method: "POST",
        body: { name: name.trim(), customer_notes: notes, proposed_yaml: yaml },
      });
      toast.success(`Created ${created.name}`, TOAST_OPTS);
      if (analyzeNow) {
        await fetchJSON(`/api/storage-reviews/${created.id}/analyze`, { method: "POST" });
        toast(`Analysis started — refresh in ~30s`, { ...TOAST_OPTS, icon: "🤖" });
      }
      navigate(`/design-reviews/storage/${created.id}`);
    } catch (err) {
      toast.error(err.message || "Failed to create review", TOAST_OPTS);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={{
        position: "sticky", top: 0, zIndex: 50,
        background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
        borderBottom: "1px solid #1a1a2e",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "16px 32px", gap: 24,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <Link to="/" style={btnSecondary}>← Inventory</Link>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>New Storage Design Review</div>
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 3 }}>
              Validate proposed OCP-Virt storage design against the source VMware datastore layout.
            </div>
          </div>
        </div>
      </header>

      <main style={{ maxWidth: 920, margin: "0 auto", padding: 32 }}>
        <form onSubmit={submit}>
          <Section title="1. Review name" subtitle="Used as the page title and in audit logs.">
            <input
              style={inputStyle}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Storage tier review — Phase 1 cutover"
              maxLength={255}
              autoFocus
              required
            />
          </Section>

          <Section title="2. Customer notes about VMware storage" subtitle="Plain English. Describe storage tiers, performance requirements, replication, backup, multipath. Anything not visible in RVTools.">
            <textarea
              style={{ ...inputStyle, minHeight: 220, fontFamily: "'Share Tech Mono', monospace", resize: "vertical" }}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder={NOTES_PLACEHOLDER}
              maxLength={200000}
            />
            <FileUpload onFile={onFile(setNotes)} accept=".md,.txt,text/markdown,text/plain" label="…or upload a markdown / text file" />
          </Section>

          <Section title="3. Proposed OpenShift storage YAML" subtitle="StorageClass, VolumeSnapshotClass, StorageMap (Forklift), optional StorageProfile. Multi-document YAML accepted.">
            <textarea
              style={{ ...inputStyle, minHeight: 240, fontFamily: "'Share Tech Mono', monospace", resize: "vertical" }}
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              placeholder={YAML_PLACEHOLDER}
              maxLength={200000}
            />
            <FileUpload onFile={onFile(setYaml)} accept=".yaml,.yml,text/yaml,application/x-yaml" label="…or upload a YAML file" />
          </Section>

          <Section title="4. Run analysis" subtitle="Generates findings using the local LLM. Same backend as Network Design Review.">
            <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 14, color: "#ccccee" }}>
              <input
                type="checkbox"
                checked={analyzeNow}
                onChange={(e) => setAnalyzeNow(e.target.checked)}
              />
              Analyze immediately after creation
            </label>
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 8, lineHeight: 1.6 }}>
              <strong style={{ color: "#ffaa00" }}>Limitations:</strong> the analyzer only sees declared specs, not
              actual storage performance. StorageClass capabilities depend on the backing CSI driver. Snapshot
              quiescence requires application coordination not validated here.
            </div>
          </Section>

          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 22 }}>
            <Link to="/" style={btnSecondary}>Cancel</Link>
            <button type="submit" disabled={!canSubmit} style={{ ...btnPrimary, opacity: canSubmit ? 1 : 0.5 }}>
              {submitting ? "Creating…" : (analyzeNow ? "Create + Analyze" : "Create")}
            </button>
          </div>
        </form>
      </main>
    </Shell>
  );
}


function FileUpload({ onFile, accept, label }) {
  return (
    <label style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      marginTop: 10, padding: "8px 14px",
      border: "1px dashed #3a3a55", color: "#aaaacc", fontSize: 12,
      letterSpacing: "0.04em", cursor: "pointer", fontFamily: "'Barlow', sans-serif",
    }}>
      <input type="file" accept={accept} onChange={onFile} style={{ display: "none" }} />
      📄 {label}
    </label>
  );
}


function Section({ title, subtitle, children }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <div style={{ fontSize: 14, color: "#eeeeff", fontWeight: 700, fontFamily: "'Barlow', sans-serif", letterSpacing: "0.04em" }}>
        {title}
      </div>
      {subtitle && (
        <div style={{ fontSize: 13, color: "#aaaacc", marginTop: 4, marginBottom: 12, lineHeight: 1.6 }}>
          {subtitle}
        </div>
      )}
      <div style={{ marginTop: subtitle ? 0 : 12 }}>
        {children}
      </div>
    </section>
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


const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "10px 12px", fontFamily: "'Barlow', sans-serif", fontSize: 14,
  width: "100%", outline: "none",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
