import { useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate } from "react-router-dom";

// New Network Design Review wizard. Single-page form: name → notes upload
// or paste → YAML upload or paste → "Analyze now" CTA. Files are read
// client-side and submitted as text payloads to the PUT endpoints.

const TOAST_OPTS = {
  style: { background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
           fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5 },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

// Realistic OCP-Virt networking placeholder shown in the YAML textarea.
// Authored as a top-level constant (not inline in the JSX attribute) so
// the multiline string uses real newlines — JSX attribute parsing
// historically chokes on `\n` escapes embedded in attribute values, and
// this layout is also easier to read in source review.
const YAML_PLACEHOLDER = `# Proposed OpenShift Virtualization network design (multi-document)
# Paste your CUDN / NAD / NetworkPolicy manifests here, separated by ---
---
apiVersion: k8s.ovn.org/v1
kind: ClusterUserDefinedNetwork
metadata:
  name: tenant-finance-net
spec:
  namespaceSelector:
    matchLabels:
      tenant: finance
  network:
    topology: Layer2
    layer2:
      role: Primary
      subnets: ["10.20.0.0/16"]
      mtu: 9000
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: api-frontend-nad
  namespace: finance-prod
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "bridge",
      "bridge": "br-api",
      "vlan": 100,
      "mtu": 9000,
      "ipam": { "type": "static" }
    }
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dmz-isolation
  namespace: finance-prod
spec:
  podSelector:
    matchLabels:
      tier: dmz
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              tier: edge
`;

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
  return r.json();
}

export default function NetworkReviewNew() {
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
      toast.success(`Loaded ${f.name} (${f.size} bytes)`, TOAST_OPTS);
    } catch (err) {
      toast.error(err.message || `Failed to read ${f.name}`, TOAST_OPTS);
    }
  };

  const submit = async (e) => {
    e?.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    try {
      const created = await fetchJSON("/api/network-reviews", {
        method: "POST",
        body: { name: name.trim(), customer_notes: notes, proposed_yaml: yaml },
      });
      toast.success(`Created review: ${created.name}`, TOAST_OPTS);
      if (analyzeNow) {
        await fetchJSON(`/api/network-reviews/${created.id}/analyze`, { method: "POST" });
        toast(`Analysis started — usually completes in 30–90s`, { ...TOAST_OPTS, icon: "🧠" });
      }
      navigate(`/design-reviews/${created.id}`);
    } catch (err) {
      toast.error(err.message || "Failed to create review", TOAST_OPTS);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
      <Styles />
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS}/>

      <header style={chrome}>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <Link to="/" style={btnSecondary}>← Dashboard</Link>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>New Network Design Review</div>
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 3 }}>
              Compare a proposed OpenShift Virtualization network design against your VMware source environment
            </div>
          </div>
        </div>
      </header>

      <main style={{ maxWidth: 920, margin: "0 auto", padding: 32 }}>
        <Notice>
          <strong>Decision support, not authoritative validation.</strong> The
          analyzer surfaces gaps; a network engineer should review every
          finding before action. Customer-notes quality determines analysis
          quality.
        </Notice>

        <form onSubmit={submit}>
          <Section title="1. Name this review">
            <Field label="Review Name" hint="e.g. 'Phase 1 cutover — DC-East prod tier'">
              <input
                style={inputStyle}
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                required
                maxLength={255}
              />
            </Field>
          </Section>

          <Section title="2. Customer notes about VMware networking" subtitle="Plain English. Describe security zones, DMZ boundaries, traffic flow requirements, anything not visible in RVTools.">
            <FileLoader accept=".md,.txt,.markdown" onLoad={onFile(setNotes)} label="Upload .md / .txt" />
            <textarea
              style={{ ...inputStyle, minHeight: 220, resize: "vertical", fontFamily: "'Barlow', sans-serif", fontSize: 13 }}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="The DMZ must be air-gapped from internal. Storage segment uses jumbo frames (MTU 9000). Application tier shares a portgroup with the load balancers — that's intentional…"
            />
          </Section>

          <Section title="3. Proposed OpenShift YAML" subtitle="ClusterUserDefinedNetwork (CUDN), NetworkAttachmentDefinition (NAD), NetworkPolicy, Multus config. Multi-document YAML accepted.">
            <FileLoader accept=".yaml,.yml" onLoad={onFile(setYaml)} label="Upload .yaml / .yml" />
            <textarea
              style={{ ...inputStyle, minHeight: 320, resize: "vertical", fontFamily: "'Share Tech Mono', monospace", fontSize: 13 }}
              value={yaml}
              onChange={(e) => setYaml(e.target.value)}
              placeholder={YAML_PLACEHOLDER}
            />
          </Section>

          <Section title="4. Run analysis">
            <label style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 16px",
                            border: "1px solid #1a1a2e", background: "#07070f", cursor: "pointer", marginBottom: 14 }}>
              <input
                type="checkbox" checked={analyzeNow} onChange={(e) => setAnalyzeNow(e.target.checked)}
                style={{ accentColor: "#4488ff", width: 16, height: 16 }}
              />
              <div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>Run analysis after creating</div>
                <div style={{ fontSize: 13, color: "#aaaacc", marginTop: 3 }}>
                  Local LLM call against Ollama; runs in the background, takes ~30–90s.
                </div>
              </div>
            </label>
            <button type="submit" disabled={submitting || !name.trim()}
              style={{
                ...btnPrimary,
                opacity: (submitting || !name.trim()) ? 0.6 : 1,
                cursor: (submitting || !name.trim()) ? "not-allowed" : "pointer",
              }}>
              {submitting ? "Creating…" : analyzeNow ? "Create + analyze" : "Create draft"}
            </button>
          </Section>
        </form>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tiny presentational helpers (kept local to this file for now)
// ---------------------------------------------------------------------------
const chrome = {
  position: "sticky", top: 0, zIndex: 50, background: "rgba(7,7,15,0.96)",
  backdropFilter: "blur(8px)", borderBottom: "1px solid #1a1a2e",
  padding: "16px 32px",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "12px 24px", fontFamily: "'Barlow', sans-serif", fontSize: 13,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  fontFamily: "'Share Tech Mono', monospace", fontSize: 14,
  padding: "11px 13px", outline: "none", width: "100%",
};

function Section({ title, subtitle, children }) {
  return (
    <section style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 22, marginBottom: 18 }}>
      <div style={{ fontSize: 15, fontWeight: 700, marginBottom: subtitle ? 4 : 14, letterSpacing: "0.04em", textTransform: "uppercase" }}>{title}</div>
      {subtitle && <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 14, lineHeight: 1.6 }}>{subtitle}</div>}
      {children}
    </section>
  );
}

function Field({ label, hint, children }) {
  return (
    <label style={{ display: "block", marginBottom: 4 }}>
      <span style={{ display: "block", fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 6 }}>
        {label}
      </span>
      {children}
      {hint && <span style={{ display: "block", fontSize: 13, color: "#9999bb", marginTop: 5 }}>{hint}</span>}
    </label>
  );
}

function FileLoader({ accept, onLoad, label }) {
  const id = `nr-file-${Math.random().toString(36).slice(2, 7)}`;
  return (
    <div style={{ marginBottom: 12 }}>
      <input id={id} type="file" accept={accept} onChange={onLoad} style={{ display: "none" }}/>
      <label htmlFor={id}
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          background: "#1a1a2e", border: "1px solid #3a3a55", color: "#eeeeff",
          padding: "10px 16px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
          letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          cursor: "pointer",
        }}>
        📁 {label}
      </label>
      <span style={{ marginLeft: 12, fontSize: 12, color: "#aaaacc" }}>
        Or paste content into the box below.
      </span>
    </div>
  );
}

function Notice({ children }) {
  return (
    <div style={{
      padding: "14px 18px", border: "1px solid #ffaa0055", background: "rgba(255,170,0,0.06)",
      fontSize: 14, color: "#ccccee", lineHeight: 1.6, marginBottom: 18,
    }}>
      <span style={{ color: "#ffaa00", letterSpacing: "0.08em", marginRight: 10,
                     fontFamily: "'Barlow', sans-serif", fontSize: 11, fontWeight: 700, textTransform: "uppercase" }}>
        Caveats
      </span>
      {children}
    </div>
  );
}

function Styles() {
  return <style>{`
    @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
    * { box-sizing: border-box; margin: 0; padding: 0; }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #0d0d1a; }
    ::-webkit-scrollbar-thumb { background: #2a2a44; border-radius: 2px; }
  `}</style>;
}
