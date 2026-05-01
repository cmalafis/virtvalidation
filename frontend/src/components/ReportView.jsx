import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate, useParams } from "react-router-dom";

// Inline report viewer. Mirrors the dashboard aesthetic exactly so users
// reading a report don't context-switch into a different visual idiom.
// Print-friendly via the @media print rules at the bottom of the
// embedded <style> block — `window.print()` from the Export PDF button
// produces a sensible PDF without any server-side rendering for the
// non-wave reports.

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

const STATUS_COLOR = {
  healthy: "#00ff88",
  degraded: "#ffaa00",
  failed: "#ff3355",
  pending: "#aaaacc",
  complete: "#00ff88",
  in_progress: "#88aaff",
  unknown: "#aaaacc",
};

const STATUS_LABEL = {
  healthy: "HEALTHY",
  degraded: "DEGRADED",
  failed: "FAILED",
  pending: "PENDING",
  complete: "COMPLETE",
  in_progress: "IN PROGRESS",
  unknown: "UNKNOWN",
};

const SEVERITY_COLOR = {
  critical: "#ff3355",
  warn: "#ffaa00",
  info: "#4488ff",
  high: "#ff7755",
  medium: "#ffaa00",
  low: "#88aaff",
};

const RISK_COLOR = { critical: "#ff3355", high: "#ff7755", medium: "#ffaa00", low: "#88aaff", info: "#aaaacc" };

// Catalog of report types — the frontend dispatches against these.
// Each endpoint returns ``{ report_type, generated_at, ... }`` so we
// could also dispatch on ``report_type`` from the body, but keying by
// URL slug keeps the routing layer in charge.
const REPORTS = {
  "executive-summary": {
    title: "Executive Summary",
    subtitle: "High-level overview suitable for leadership.",
    endpoint: "/api/reports/executive-summary",
    render: (data) => <ExecutiveSummaryView data={data} />,
  },
  "full-validation": {
    title: "Full Migration Validation Report",
    subtitle: "All VMs, all findings, remediation steps. CISO-ready.",
    endpoint: "/api/reports/full-validation",
    render: (data) => <ValidationListView data={data} />,
  },
  "failed-degraded": {
    title: "Failed & Degraded VMs",
    subtitle: "Filtered report — only VMs requiring action.",
    endpoint: "/api/reports/failed-degraded",
    render: (data) => <ValidationListView data={data} mode="filtered" />,
  },
  "wave-plan": {
    title: "Migration Wave Plan",
    subtitle: "AI-generated wave sequencing with rationale.",
    endpoint: "/api/reports/wave-plan",
    render: (data) => <WavePlanView data={data} />,
  },
  "baseline-snapshot": {
    title: "Pre-Migration Baseline Snapshot",
    subtitle: "Full captured state of all VMs before migration.",
    endpoint: "/api/reports/baseline-snapshot",
    render: (data) => <BaselineSnapshotView data={data} />,
  },
};

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json())?.detail ?? ""; } catch { /* */ }
    throw new Error(detail ? `HTTP ${r.status}: ${detail}` : `HTTP ${r.status}`);
  }
  return r.json();
}

export default function ReportView() {
  const { type } = useParams();
  const navigate = useNavigate();
  const meta = REPORTS[type];

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!meta) return;
    setLoading(true);
    setError(null);
    try {
      setData(await fetchJSON(meta.endpoint));
    } catch (e) {
      setError(e.message || "Failed to load report");
    } finally {
      setLoading(false);
    }
  }, [meta]);

  useEffect(() => { load(); }, [load]);

  if (!meta) {
    return (
      <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", padding: 32, fontFamily: "'Barlow', sans-serif" }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 12 }}>Unknown report</h1>
        <p style={{ fontSize: 14, color: "#aaaacc" }}>
          No report registered for &quot;{type}&quot;.
        </p>
        <Link to="/" style={{ color: "#88aaff", marginTop: 18, display: "inline-block" }}>← Back to dashboard</Link>
      </div>
    );
  }

  const onExport = () => {
    window.print();
    toast("Use your browser's print dialog to save as PDF", { ...TOAST_OPTS, icon: "🖨️" });
  };

  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
      <ReportStyles />
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS}/>

      <header className="report-chrome" style={{
        position: "sticky", top: 0, zIndex: 50,
        background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
        borderBottom: "1px solid #1a1a2e",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "16px 32px", gap: 24,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <button
            type="button"
            onClick={() => navigate("/")}
            aria-label="Back to dashboard"
            style={{
              background: "transparent", border: "1px solid #3a3a55",
              color: "#aaaacc", padding: "8px 14px",
              fontFamily: "'Barlow', sans-serif", fontSize: 12,
              letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
              cursor: "pointer",
            }}>
            ← Back
          </button>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700, letterSpacing: "0.02em" }}>{meta.title}</div>
            <div style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace", marginTop: 3 }}>
              {data?.generated_at ? `Generated ${new Date(data.generated_at).toLocaleString()}` : "—"}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={onExport}
          disabled={loading || !!error}
          style={{
            background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
            padding: "10px 20px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
            letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
            cursor: (loading || error) ? "not-allowed" : "pointer",
          }}>
          ↓ Export PDF
        </button>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 14, color: "#aaaacc", lineHeight: 1.6 }}>{meta.subtitle}</div>
        </div>

        {loading && <Loader />}
        {error && <Err msg={error} />}
        {!loading && !error && data && meta.render(data)}
      </main>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Per-report renderers
// ---------------------------------------------------------------------------
function ExecutiveSummaryView({ data }) {
  const summary = data.summary || {};
  const status = summary.by_status || {};
  const findings = summary.findings_by_severity || {};
  const items = [
    { label: "Healthy",  value: status.healthy,  color: STATUS_COLOR.healthy },
    { label: "Degraded", value: status.degraded, color: STATUS_COLOR.degraded },
    { label: "Failed",   value: status.failed,   color: STATUS_COLOR.failed },
    { label: "Pending",  value: status.pending,  color: STATUS_COLOR.pending },
  ];

  return (
    <>
      {data.executive_summary && (
        <section style={cardStyle} className="exec-summary">
          <SectionTitle>Narrative</SectionTitle>
          <p style={{ fontSize: 15, color: "#eeeeff", lineHeight: 1.7 }}>
            {data.executive_summary}
          </p>
        </section>
      )}

      <SectionTitle>Verdict Distribution</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 28 }}>
        {items.map((i) => (
          <Stat key={i.label} label={i.label} value={i.value ?? 0} color={i.color} />
        ))}
      </div>

      <SectionTitle>Migration Progress</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16, marginBottom: 28 }}>
        <Stat label="Total VMs"   value={summary.total_vms ?? 0} />
        <Stat label="Validated"   value={`${summary.migration_progress_pct ?? 0}%`} />
        <Stat label="Plan Count"  value={summary.plan_count ?? 0} />
        <Stat label="Latest Plan" value={summary.latest_plan_id ? `#${summary.latest_plan_id}` : "—"} />
      </div>

      {(findings.critical || findings.warn || findings.info) ? (
        <>
          <SectionTitle>Findings by Severity</SectionTitle>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16, marginBottom: 28 }}>
            <Stat label="Critical" value={findings.critical ?? 0} color={SEVERITY_COLOR.critical} />
            <Stat label="Warn"     value={findings.warn ?? 0}     color={SEVERITY_COLOR.warn} />
            <Stat label="Info"     value={findings.info ?? 0}     color={SEVERITY_COLOR.info} />
          </div>
        </>
      ) : null}

      {(data.wave_status || []).length > 0 && (
        <>
          <SectionTitle>Wave Status</SectionTitle>
          <div style={{ border: "1px solid #1a1a2e" }}>
            <div style={{ display: "grid", gridTemplateColumns: "0.6fr 1fr 1fr 1fr 0.8fr", padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16" }}>
              {["Wave", "VMs", "Validated", "Verdict Mix", "Risk"].map((h) => (
                <span key={h} style={tableHeader}>{h}</span>
              ))}
            </div>
            {data.wave_status.map((w) => (
              <div key={w.wave_number} style={{ display: "grid", gridTemplateColumns: "0.6fr 1fr 1fr 1fr 0.8fr", padding: "12px 18px", borderBottom: "1px solid #0f0f1e", alignItems: "center" }}>
                <span style={{ fontSize: 14, color: "#eeeeff", fontWeight: 600 }}>Wave {w.wave_number}</span>
                <span style={mono}>{w.vm_count}</span>
                <span style={mono}>{w.validated_count} / {w.vm_count} ({w.completion_pct}%)</span>
                <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {Object.entries({ healthy: w.healthy, degraded: w.degraded, failed: w.failed, pending: w.pending })
                    .filter(([, n]) => n > 0).map(([k, n]) => (
                      <Pill key={k} label={`${k}·${n}`} color={STATUS_COLOR[k]} />
                    ))}
                </span>
                <Pill label={w.estimated_risk || "—"} color={RISK_COLOR[w.estimated_risk] || "#aaaacc"} />
              </div>
            ))}
          </div>
        </>
      )}

      {(data.key_risks || []).length > 0 && (
        <>
          <SectionTitle style={{ marginTop: 28 }}>Key Risks</SectionTitle>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {data.key_risks.map((r, i) => (
              <div key={i} style={{ border: "1px solid #1a1a2e", borderLeft: `3px solid ${RISK_COLOR[r.severity] || "#aaaacc"}`, background: "#0a0a18", padding: "14px 18px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
                  <Link to={`/vms/${r.vm_id}`} style={{ fontSize: 14, fontWeight: 600, color: "#eeeeff", textDecoration: "none" }}>{r.vm_name} →</Link>
                  <Pill label={r.severity} color={RISK_COLOR[r.severity] || "#aaaacc"} />
                </div>
                <div style={{ fontSize: 13, color: "#ccccee", marginTop: 6, lineHeight: 1.6 }}>{r.description}</div>
                <div style={{ fontSize: 11, color: "#aaaacc", marginTop: 6, fontFamily: "'Share Tech Mono', monospace" }}>
                  {r.category}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

function ValidationListView({ data, mode }) {
  const vms = data.vms || [];
  const summary = data.summary || {};
  const filtered = mode === "filtered";

  return (
    <>
      <SectionTitle>
        {filtered
          ? `${summary.needing_action ?? 0} of ${summary.total_in_inventory ?? 0} VMs need action`
          : `${summary.total ?? vms.length} VMs validated`}
      </SectionTitle>

      <div style={{ display: "grid", gridTemplateColumns: filtered ? "repeat(2, 1fr)" : "repeat(4, 1fr)", gap: 16, marginBottom: 28 }}>
        {filtered ? (
          <>
            <Stat label="Failed"   value={summary.failed ?? 0}   color={STATUS_COLOR.failed} />
            <Stat label="Degraded" value={summary.degraded ?? 0} color={STATUS_COLOR.degraded} />
          </>
        ) : (
          <>
            <Stat label="Healthy"  value={summary.healthy ?? 0}  color={STATUS_COLOR.healthy} />
            <Stat label="Degraded" value={summary.degraded ?? 0} color={STATUS_COLOR.degraded} />
            <Stat label="Failed"   value={summary.failed ?? 0}   color={STATUS_COLOR.failed} />
            <Stat label="Pending"  value={summary.pending ?? 0}  color={STATUS_COLOR.pending} />
          </>
        )}
      </div>

      {vms.length === 0 ? (
        <p style={{ color: "#aaaacc", fontSize: 14 }}>
          {filtered
            ? "No VMs in degraded or failed state — everything's healthy."
            : "No VMs to report on yet. Enroll a VM and run validation to populate this report."}
        </p>
      ) : vms.map((vm) => <ValidationCard key={vm.id} vm={vm} />)}
    </>
  );
}

function ValidationCard({ vm }) {
  const status = vm.status || "pending";
  const color = STATUS_COLOR[status];
  return (
    <div style={{
      border: "1px solid #1a1a2e", borderLeft: `3px solid ${color}`,
      background: "#0a0a18", padding: 20, marginBottom: 14,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div>
          <Link to={`/vms/${vm.id}`} style={{ fontSize: 16, fontWeight: 700, color: "#eeeeff", textDecoration: "none" }}>{vm.name}</Link>
          <div style={{ fontSize: 13, color: "#aaaacc", marginTop: 4 }}>
            <span style={mono}>{vm.ip_address || "—"}</span>
            {vm.role ? ` · ${vm.role}` : ""}
            {vm.os_family ? ` · ${vm.os_family}` : ""}
            {vm.environment ? ` · ${vm.environment}` : ""}
            {vm.owner ? ` · owner ${vm.owner}` : ""}
          </div>
        </div>
        <Pill label={STATUS_LABEL[status]} color={color} />
      </div>
      {vm.summary && (
        <div style={{ marginTop: 14, fontSize: 14, color: "#ccccee", lineHeight: 1.6 }}>
          {vm.summary}
        </div>
      )}
      {vm.findings?.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <SubLabel>Findings ({(vm.findings ?? []).length})</SubLabel>
          {(vm.findings ?? []).map((f, i) => (
            <div key={i} style={{ paddingLeft: 14, borderLeft: `2px solid ${SEVERITY_COLOR[f.severity] || "#aaaacc"}`, marginBottom: 10 }}>
              <div style={{ fontSize: 11, color: SEVERITY_COLOR[f.severity] || "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>
                {f.severity || "?"}{f.category ? ` · ${f.category}` : ""}
              </div>
              <div style={{ fontSize: 13, color: "#ccccee", marginTop: 4, lineHeight: 1.6 }}>{f.message}</div>
            </div>
          ))}
        </div>
      )}
      {vm.remediation?.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <SubLabel>Remediation</SubLabel>
          {vm.remediation.map((r, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6 }}>
                <span style={{ color: "#88aaff", fontWeight: 700, marginRight: 8 }}>{r.step ?? i + 1}.</span>{r.action}
              </div>
              {r.command && (
                <code style={{ display: "block", marginTop: 4, padding: "8px 12px", background: "#07070f", border: "1px solid #1a1a2e", fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#ccccee" }}>
                  $ {r.command}
                </code>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WavePlanView({ data }) {
  if (data.no_plan_message) {
    return (
      <div style={{ border: "1px dashed #2a2a44", padding: "60px 24px", textAlign: "center" }}>
        <div style={{ fontSize: 32, color: "#3a3a55", marginBottom: 14 }}>◎</div>
        <div style={{ fontSize: 16, fontWeight: 600, color: "#eeeeff", marginBottom: 8 }}>{data.no_plan_message}</div>
        <p style={{ fontSize: 14, color: "#aaaacc", maxWidth: 440, margin: "0 auto", lineHeight: 1.6 }}>
          The Migration Wave Plan report shows AI-generated wave sequencing once at least one plan exists. Open the dashboard&apos;s Migration Plan tab to generate one.
        </p>
        <Link to="/" style={{ display: "inline-block", marginTop: 18, color: "#88aaff", fontSize: 13 }}>
          Go to dashboard →
        </Link>
      </div>
    );
  }
  const waves = data.waves || [];
  return (
    <>
      <SectionTitle>Plan #{data.plan_id} · {waves.length} wave{waves.length === 1 ? "" : "s"}</SectionTitle>
      <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 18, fontFamily: "'Share Tech Mono', monospace" }}>
        Created {data.plan_created_at ? new Date(data.plan_created_at).toLocaleString() : "—"} · model {data.model || "—"}
      </div>
      {data.plan_summary && (
        <p style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.7, marginBottom: 24 }}>{data.plan_summary}</p>
      )}
      {waves.map((w) => (
        <div key={w.wave_number} style={{ border: "1px solid #1a1a2e", marginBottom: 14, background: "#0a0a18" }}>
          <div style={{ padding: "16px 20px", borderBottom: "1px solid #14142a", display: "flex", justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 10 }}>
            <div>
              <div style={{ fontSize: 16, fontWeight: 700 }}>Wave {w.wave_number}</div>
              <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
                {w.vm_count} VMs · risk {w.estimated_risk || "—"}
              </div>
            </div>
            <Pill label={STATUS_LABEL[w.status] || w.status} color={STATUS_COLOR[w.status] || "#aaaacc"} />
          </div>
          {w.rationale && (
            <div style={{ padding: "14px 20px", fontSize: 13, color: "#ccccee", lineHeight: 1.6, borderBottom: "1px solid #14142a" }}>
              {w.rationale}
            </div>
          )}
          <div style={{ padding: "10px 20px 16px" }}>
            {(w.vms || []).map((vm) => (
              <div key={vm.vm_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 0", borderBottom: "1px solid #0f0f1e" }}>
                <div>
                  <Link to={`/vms/${vm.vm_id}`} style={{ fontSize: 13, color: "#eeeeff", textDecoration: "none", fontWeight: 600 }}>
                    {vm.name}
                  </Link>
                  <span style={{ fontSize: 12, color: "#aaaacc", marginLeft: 10, fontFamily: "'Share Tech Mono', monospace" }}>
                    {vm.ip_address || "—"}{vm.role ? ` · ${vm.role}` : ""}
                  </span>
                </div>
                <Pill label={STATUS_LABEL[vm.status] || vm.status} color={STATUS_COLOR[vm.status] || "#aaaacc"} small />
              </div>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}

function BaselineSnapshotView({ data }) {
  const summary = data.summary || {};
  const vms = data.vms || [];
  return (
    <>
      <SectionTitle>{vms.length} VMs · latest baseline per VM</SectionTitle>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16, marginBottom: 28 }}>
        <Stat label="Total VMs"          value={summary.total_vms ?? 0} />
        <Stat label="With Baselines"     value={summary.vms_with_baselines ?? 0}    color={STATUS_COLOR.healthy} />
        <Stat label="Without Baselines"  value={summary.vms_without_baselines ?? 0} color={STATUS_COLOR.pending} />
      </div>
      {summary.latest_collection_at && (
        <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 18, fontFamily: "'Share Tech Mono', monospace" }}>
          Latest collection across the fleet: {new Date(summary.latest_collection_at).toLocaleString()}
        </div>
      )}
      {vms.length === 0 ? (
        <p style={{ color: "#aaaacc", fontSize: 14 }}>No VMs enrolled yet.</p>
      ) : (
        <div style={{ border: "1px solid #1a1a2e" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 0.7fr 0.7fr 0.7fr 1.2fr", padding: "12px 18px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16" }}>
            {["VM Name", "Hostname", "OS", "Snapshots", "Services", "Ports", "Last Collected"].map((h) => (
              <span key={h} style={tableHeader}>{h}</span>
            ))}
          </div>
          {vms.map((row) => (
            <div key={row.vm_id} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 0.7fr 0.7fr 0.7fr 1.2fr", padding: "12px 18px", borderBottom: "1px solid #0f0f1e", alignItems: "center", fontSize: 13 }}>
              <Link to={`/vms/${row.vm_id}`} style={{ color: "#eeeeff", fontWeight: 600, textDecoration: "none" }}>{row.name}</Link>
              <span style={mono}>{row.hostname || "—"}</span>
              <span style={mono}>
                {row.os_profile?.distro || row.os_family || "—"}
                {row.os_profile?.major_version ? ` ${row.os_profile.major_version}.${row.os_profile.minor_version || 0}` : ""}
              </span>
              <span style={mono}>{row.snapshot_count}</span>
              <span style={mono}>{row.service_count}</span>
              <span style={mono}>{row.port_count}</span>
              <span style={{ ...mono, fontSize: 12, color: "#aaaacc" }}>
                {row.last_collected_at ? new Date(row.last_collected_at).toLocaleString() : "—"}
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}


// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------
function SectionTitle({ children, style }) {
  return (
    <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 14, letterSpacing: "0.04em", textTransform: "uppercase", ...(style || {}) }}>
      {children}
    </h2>
  );
}

function SubLabel({ children }) {
  return (
    <div style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 8 }}>
      {children}
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{ border: "1px solid #1a1a2e", padding: "20px 24px", background: "#0a0a18" }}>
      <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1, fontFamily: "'Share Tech Mono', monospace", color: color || "#eeeeff" }}>
        {value ?? "—"}
      </div>
      <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 6, letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 600 }}>
        {label}
      </div>
    </div>
  );
}

function Pill({ label, color, small }) {
  return (
    <span style={{
      fontSize: small ? 9 : 11, color, border: `1px solid ${color}55`,
      background: `${color}11`, padding: small ? "2px 7px" : "4px 12px",
      letterSpacing: "0.08em", fontWeight: 700,
      fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
      whiteSpace: "nowrap",
    }}>
      {label}
    </span>
  );
}

function Loader() {
  return <div style={{ padding: 48, textAlign: "center", color: "#aaaacc" }}>Loading report…</div>;
}

function Err({ msg }) {
  return (
    <div style={{
      padding: "20px 24px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)",
      color: "#ccaaaa",
    }}>
      <div style={{ color: "#ff5577", fontWeight: 700, marginBottom: 6 }}>ERROR</div>
      <div>{msg}</div>
    </div>
  );
}

const cardStyle = { border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24, marginBottom: 22 };
const tableHeader = {
  fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
  fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
};
const mono = {
  fontFamily: "'Share Tech Mono', monospace", color: "#ccccee", fontSize: 13,
};

function ReportStyles() {
  return (
    <style>{`
      @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
      * { box-sizing: border-box; margin: 0; padding: 0; }
      ::-webkit-scrollbar { width: 6px; }
      ::-webkit-scrollbar-track { background: #0d0d1a; }
      ::-webkit-scrollbar-thumb { background: #2a2a44; border-radius: 2px; }

      @media print {
        .report-chrome { display: none !important; }
        body, html { background: #ffffff !important; color: #000000 !important; }
        main { padding: 0 !important; max-width: 100% !important; }
        h1, h2, h3, .section-title { color: #000 !important; }
        a { color: #003366 !important; text-decoration: underline !important; }
        div, span, code { color: #000 !important; background: transparent !important; }
        div[style*="border"] { border-color: #999 !important; }
      }
    `}</style>
  );
}
