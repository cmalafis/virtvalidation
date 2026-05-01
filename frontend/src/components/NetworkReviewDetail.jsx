import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate, useParams } from "react-router-dom";

// Per-review detail page. Renders the full report view + lets operators
// triage findings (open / accepted / dismissed) and re-run analysis.

const TOAST_OPTS = {
  style: { background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
           fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5 },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const SEVERITY_COLOR = {
  critical: "#ff3355", high: "#ff7755", medium: "#ffaa00",
  low: "#88aaff", info: "#aaaacc",
};

const CATEGORY_LABEL = {
  coverage_gap: "Coverage Gap",
  config_mismatch: "Configuration Mismatch",
  missing_resource: "Missing Resource",
  positive_confirmation: "Positive Confirmation",
};

const CATEGORY_ORDER = [
  "coverage_gap", "config_mismatch", "missing_resource", "positive_confirmation",
];

const SEVERITY_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

const STATUS_COLOR = {
  draft: "#aaaacc",
  analyzing: "#88aaff",
  completed: "#00ff88",
  failed: "#ff5577",
};

async function fetchJSON(url, opts = {}) {
  const init = { method: "GET", ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (r.status === 404) return { status: 404, data: null };
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json())?.detail ?? ""; } catch { /* */ }
    throw new Error(detail ? `HTTP ${r.status}: ${detail}` : `HTTP ${r.status}`);
  }
  if (r.status === 204) return { status: 204, data: null };
  return { status: r.status, data: await r.json() };
}

export default function NetworkReviewDetail() {
  const { id } = useParams();
  const reviewId = Number(id);
  const navigate = useNavigate();

  const [review, setReview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data, status } = await fetchJSON(`/api/network-reviews/${reviewId}`);
      if (status === 404) {
        setError("Review not found");
        setReview(null);
      } else {
        setReview(data);
        setError(null);
      }
    } catch (e) {
      setError(e.message || "Failed to load review");
    } finally {
      setLoading(false);
    }
  }, [reviewId]);

  useEffect(() => { load(); }, [load]);

  // Poll while analyzing so the UI flips to "completed" without the
  // operator having to refresh manually.
  useEffect(() => {
    if (review?.status !== "analyzing") return;
    const t = setInterval(() => { load(); }, 3000);
    return () => clearInterval(t);
  }, [review?.status, load]);

  const onAnalyze = async () => {
    try {
      await fetchJSON(`/api/network-reviews/${reviewId}/analyze`, { method: "POST" });
      toast(`Analysis started`, { ...TOAST_OPTS, icon: "🧠" });
      await load();
    } catch (e) {
      toast.error(e.message || "Failed to start analysis", TOAST_OPTS);
    }
  };

  const onTriage = async (findingId, triage) => {
    try {
      await fetchJSON(`/api/network-reviews/${reviewId}/findings/${findingId}`, {
        method: "PATCH", body: { triage },
      });
      await load();
    } catch (e) {
      toast.error(e.message || "Failed to update finding", TOAST_OPTS);
    }
  };

  const onDelete = async () => {
    if (!review) return;
    if (!window.confirm(`Delete review "${review.name}"? This cannot be undone.`)) return;
    try {
      await fetchJSON(`/api/network-reviews/${reviewId}`, { method: "DELETE" });
      toast.success("Review deleted", TOAST_OPTS);
      navigate("/");
    } catch (e) {
      toast.error(e.message || "Failed to delete review", TOAST_OPTS);
    }
  };

  if (loading) return <Shell><div style={{ padding: 48, color: "#aaaacc" }}>Loading…</div></Shell>;
  if (error) return <Shell><Err msg={error} /></Shell>;
  if (!review) return null;

  const findings = (review.findings || [])
    .slice()
    .sort((a, b) => {
      const c = CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category);
      if (c !== 0) return c;
      return (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
    });

  const grouped = {};
  for (const f of findings) {
    (grouped[f.category] = grouped[f.category] || []).push(f);
  }

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS}/>

      <header style={chrome}>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <Link to="/" style={btnSecondary}>← Dashboard</Link>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{review.name}</div>
            <div style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace", marginTop: 3 }}>
              Created {new Date(review.created_at).toLocaleString()}
              {review.last_analyzed_at && ` · Last analyzed ${new Date(review.last_analyzed_at).toLocaleString()}`}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <StatusPill status={review.status} />
          <button onClick={() => window.print()} style={btnSecondary}>↓ Export PDF</button>
          <button onClick={onAnalyze} disabled={review.status === "analyzing"}
            style={{ ...btnPrimary, opacity: review.status === "analyzing" ? 0.6 : 1,
                     cursor: review.status === "analyzing" ? "wait" : "pointer" }}>
            {review.status === "analyzing" ? "Analyzing…" : "Re-run analysis"}
          </button>
          <button onClick={onDelete} style={{ ...btnSecondary, color: "#ff99aa", borderColor: "#ff557755" }}>🗑 Delete</button>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        <Caveats />

        {review.status === "failed" && review.last_error && (
          <div style={{ padding: "16px 20px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)", marginBottom: 22 }}>
            <div style={{ fontSize: 11, color: "#ff5577", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
              Analysis failed
            </div>
            <div style={{ fontSize: 13, color: "#ccaaaa", fontFamily: "'Share Tech Mono', monospace" }}>{review.last_error}</div>
          </div>
        )}

        {review.analysis_results?.executive_summary && (
          <section style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24, marginBottom: 22 }}>
            <H2>Executive Summary</H2>
            <p style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.7 }}>
              {review.analysis_results.executive_summary}
            </p>
          </section>
        )}

        {findings.length === 0 ? (
          <section style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24, marginBottom: 22 }}>
            <p style={{ color: "#aaaacc", fontSize: 14 }}>
              {review.status === "draft"
                ? "Draft — no analysis run yet. Click Re-run analysis above."
                : review.status === "analyzing"
                  ? "Analysis in progress…"
                  : "No findings recorded."}
            </p>
          </section>
        ) : (
          CATEGORY_ORDER.map((cat) => {
            const items = grouped[cat] || [];
            if (items.length === 0) return null;
            return (
              <section key={cat} style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24, marginBottom: 18 }}>
                <H2>{CATEGORY_LABEL[cat]} <span style={{ color: "#aaaacc", fontWeight: 400, fontSize: 14 }}>({items.length})</span></H2>
                {items.map((f) => <FindingCard key={f.id} finding={f} onTriage={onTriage} />)}
              </section>
            );
          })
        )}

        <SourceProposedBlocks notes={review.customer_notes} yaml={review.proposed_yaml} />
        <ConfidenceLimitations sourceSummary={review.analysis_results?.source_summary} model={review.analysis_results?.model} />
      </main>

      <Styles />
    </Shell>
  );
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------
function FindingCard({ finding, onTriage }) {
  const [open, setOpen] = useState(false);
  const sev = finding.severity;
  const color = SEVERITY_COLOR[sev] || "#aaaacc";
  const dimmed = finding.triage === "dismissed";
  return (
    <div style={{
      border: "1px solid #14142a", borderLeft: `3px solid ${color}`,
      background: "#07070f", padding: 18, marginBottom: 12,
      opacity: dimmed ? 0.55 : 1,
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 14, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 280 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
            <span style={{ fontSize: 10, color, letterSpacing: "0.08em", textTransform: "uppercase",
                           fontWeight: 700, fontFamily: "'Barlow', sans-serif",
                           padding: "3px 9px", border: `1px solid ${color}66`, background: `${color}11` }}>
              {sev}
            </span>
            <ConfidenceTag c={finding.confidence} />
            {finding.triage !== "open" && (
              <span style={{ fontSize: 10, color: finding.triage === "accepted" ? "#00ff88" : "#aaaacc",
                             letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 700,
                             fontFamily: "'Barlow', sans-serif" }}>
                {finding.triage}
              </span>
            )}
          </div>
          <div style={{ fontSize: 15, fontWeight: 600, color: "#eeeeff", marginBottom: 6 }}>
            {finding.title}
          </div>
          {finding.description && (
            <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6, marginBottom: 10 }}>
              {finding.description}
            </div>
          )}
          <button type="button" onClick={() => setOpen((v) => !v)}
            style={{
              background: "transparent", border: "none", color: "#88aaff",
              fontFamily: "'Barlow', sans-serif", fontSize: 12, fontWeight: 700,
              letterSpacing: "0.06em", textTransform: "uppercase", cursor: "pointer",
              padding: 0,
            }}>
            {open ? "Hide evidence" : "Show evidence"} {open ? "▼" : "▶"}
          </button>
          {open && (
            <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <EvidenceBlock label="Source evidence" content={finding.source_evidence} />
              <EvidenceBlock label="Proposed evidence" content={finding.proposed_evidence} />
              {finding.recommendation && (
                <div style={{ gridColumn: "1 / -1" }}>
                  <EvidenceBlock label="Recommendation" content={finding.recommendation} accent />
                </div>
              )}
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          {finding.triage !== "accepted" && (
            <button onClick={() => onTriage(finding.id, "accepted")} style={miniBtn("#00ff88")}>Accept</button>
          )}
          {finding.triage !== "dismissed" && (
            <button onClick={() => onTriage(finding.id, "dismissed")} style={miniBtn("#aaaacc")}>Dismiss</button>
          )}
          {finding.triage !== "open" && (
            <button onClick={() => onTriage(finding.id, "open")} style={miniBtn("#88aaff")}>Reopen</button>
          )}
        </div>
      </div>
    </div>
  );
}

function ConfidenceTag({ c }) {
  const color = c === "high" ? "#00ff88" : c === "medium" ? "#ffaa00" : "#ff5577";
  return (
    <span style={{
      fontSize: 10, color, letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 700,
      fontFamily: "'Barlow', sans-serif", padding: "3px 9px",
      border: `1px solid ${color}66`, background: `${color}11`,
    }}>
      {c} conf
    </span>
  );
}

function EvidenceBlock({ label, content, accent }) {
  if (!content || !content.trim()) return null;
  return (
    <div>
      <div style={{ fontSize: 10, color: accent ? "#88aaff" : "#aaaacc", letterSpacing: "0.08em",
                    textTransform: "uppercase", fontWeight: 700, marginBottom: 5 }}>
        {label}
      </div>
      <div style={{
        fontSize: 12, color: "#ccccee", padding: "10px 12px",
        background: accent ? "rgba(68,136,255,0.06)" : "#0a0a18",
        border: `1px solid ${accent ? "#4488ff44" : "#1a1a2e"}`,
        fontFamily: "'Share Tech Mono', monospace", lineHeight: 1.6,
        whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>
        {content}
      </div>
    </div>
  );
}

function SourceProposedBlocks({ notes, yaml }) {
  return (
    <section style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24, marginBottom: 18 }}>
      <H2>Inputs</H2>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
        <details>
          <summary style={summaryStyle}>Customer notes ({notes ? `${notes.length} chars` : "empty"})</summary>
          <pre style={preStyle}>{notes || "(empty)"}</pre>
        </details>
        <details>
          <summary style={summaryStyle}>Proposed YAML ({yaml ? `${yaml.length} chars` : "empty"})</summary>
          <pre style={preStyle}>{yaml || "(empty)"}</pre>
        </details>
      </div>
    </section>
  );
}

function ConfidenceLimitations({ sourceSummary, model }) {
  return (
    <section style={{ border: "1px dashed #2a2a44", background: "#0a0a18", padding: 22, marginBottom: 22 }}>
      <H2>Confidence &amp; Limitations</H2>
      <ul style={{ paddingLeft: 22, fontSize: 13, color: "#ccccee", lineHeight: 1.8 }}>
        <li>This is decision support, not authoritative validation.</li>
        <li>Cannot validate runtime networking behavior, only configuration.</li>
        <li>Customer notes quality determines analysis quality.</li>
        <li>Should be reviewed by a network engineer before action.</li>
        <li>The LLM may miss context not present in provided artifacts.</li>
        <li>Out of scope: auto-generating OpenShift YAML, applying changes to clusters, runtime network testing, BGP/dynamic routing analysis.</li>
      </ul>
      {model && (
        <div style={{ marginTop: 14, fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>
          Analysis ran against {model}{" · "}
          {sourceSummary?.total_vms != null
            ? `${sourceSummary.total_vms} source VMs · ${sourceSummary.vsphere_networks?.length ?? 0} unique portgroups`
            : ""}
        </div>
      )}
    </section>
  );
}

function StatusPill({ status }) {
  const c = STATUS_COLOR[status] || "#aaaacc";
  return (
    <span style={{
      fontSize: 11, color: c, letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 700,
      padding: "5px 12px", border: `1px solid ${c}66`, background: `${c}11`,
      fontFamily: "'Barlow', sans-serif",
    }}>
      {status}
    </span>
  );
}

function Caveats() {
  return (
    <div style={{
      padding: "12px 16px", border: "1px solid #ffaa0055", background: "rgba(255,170,0,0.06)",
      fontSize: 13, color: "#ccccee", lineHeight: 1.6, marginBottom: 18,
    }}>
      <span style={{ color: "#ffaa00", letterSpacing: "0.08em", marginRight: 10,
                     fontFamily: "'Barlow', sans-serif", fontSize: 11, fontWeight: 700, textTransform: "uppercase" }}>
        Caveats
      </span>
      Decision support — not authoritative. A network engineer should review every finding before action.
    </div>
  );
}

function H2({ children }) {
  return <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 14, letterSpacing: "0.04em", textTransform: "uppercase" }}>{children}</h2>;
}

function Err({ msg }) {
  return (
    <div style={{ maxWidth: 720, margin: "60px auto", padding: "20px 24px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)" }}>
      <div style={{ fontSize: 11, color: "#ff5577", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 6 }}>Error</div>
      <div style={{ fontSize: 14, color: "#ccaaaa" }}>{msg}</div>
      <div style={{ marginTop: 14 }}><Link to="/" style={{ color: "#88aaff" }}>← Back to dashboard</Link></div>
    </div>
  );
}

function Shell({ children }) {
  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
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

    @media print {
      .report-chrome, button { display: none !important; }
      body, html { background: #ffffff !important; color: #000000 !important; }
      h1, h2, h3, p, div, span, code, summary, pre { color: #000 !important; background: transparent !important; }
      div[style*="border"] { border-color: #999 !important; }
    }
  `}</style>;
}

const chrome = {
  position: "sticky", top: 0, zIndex: 50, background: "rgba(7,7,15,0.96)",
  backdropFilter: "blur(8px)", borderBottom: "1px solid #1a1a2e",
  padding: "16px 32px",
  display: "flex", alignItems: "center", justifyContent: "space-between", gap: 24, flexWrap: "wrap",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
};
const summaryStyle = {
  cursor: "pointer", fontSize: 12, color: "#88aaff", letterSpacing: "0.06em",
  textTransform: "uppercase", fontWeight: 700, marginBottom: 8,
};
const preStyle = {
  fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#ccccee",
  background: "#07070f", border: "1px solid #1a1a2e", padding: "10px 12px",
  whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto", lineHeight: 1.5,
};
const miniBtn = (color) => ({
  background: "transparent", border: `1px solid ${color}66`, color,
  padding: "6px 12px", fontSize: 11, fontFamily: "'Barlow', sans-serif",
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer",
});
