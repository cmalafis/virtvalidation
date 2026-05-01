import { Component, useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate, useParams } from "react-router-dom";

// Per-VM detail page — everything operators want to see about a VM in one
// scrollable surface, organized into collapsible sections. Replaces the
// old click-to-select-on-the-dashboard interaction model with a real
// linkable URL.

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

const STATUS_COLOR = { pass: "#00ff88", warn: "#ffaa00", fail: "#ff3355", pending: "#aaaacc" };
const STATUS_LABEL = { pass: "HEALTHY", warn: "DEGRADED", fail: "FAILED", pending: "PENDING" };
const SEVERITY_COLOR = {
  critical: "#ff3355",
  high:     "#ff7755",
  medium:   "#ffaa00",
  low:      "#88aaff",
  info:     "#4488ff",
  warn:     "#ffaa00",
};
const CONFIDENCE_COLOR = {
  high: { color: "#00ff88", label: "HIGH" },
  medium: { color: "#ffaa00", label: "MEDIUM" },
  low: { color: "#ff5577", label: "LOW" },
};

const STEP_LABEL = {
  queued: "queued",
  ssh_collecting: "collecting current state",
  llm_reasoning: "reasoning over diff",
  storing: "storing result",
  completed: "completed",
  failed: "failed",
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

function classifyCaptureError(raw) {
  // Mirror of the dashboard's classifier — kept local so the detail page
  // can stand alone if extracted.
  const m = String(raw || "").toLowerCase();
  if (!m) return "No error detail recorded.";
  if (m.includes("not found") && m.includes("known_hosts")) return "Strict mode + unknown host. Add via ssh-keyscan or switch to auto-accept.";
  if (m.includes("man-in-the-middle") || (m.includes("host key") && m.includes("changed"))) return "Host key changed since last connection — investigate before re-accepting.";
  if (m.includes("connection refused") || m.includes("errno 111")) return "Connection refused — check sshd is running and the port is open.";
  if (m.includes("timed out") || m.includes("timeout")) return "Connection timed out — check network reachability.";
  if (m.includes("authentication") || m.includes("permission denied")) return "Authentication failed — verify the appliance public key is in ~/.ssh/authorized_keys.";
  if (m.includes("sudo") || m.includes("not in sudoers")) return "sudo required — add a NOPASSWD rule for systemctl/ss/findmnt.";
  if (m.includes("command not found")) return "Required tool missing on VM — check OS compatibility matrix.";
  return raw;
}


function VMDetailBody() {
  const { id } = useParams();
  const vmId = Number(id);
  const navigate = useNavigate();

  const [vm, setVm] = useState(null);
  const [vmError, setVmError] = useState(null);
  const [snapshots, setSnapshots] = useState([]);
  const [profile, setProfile] = useState(null);
  const [validation, setValidation] = useState(null);
  const [plans, setPlans] = useState([]);
  const [audit, setAudit] = useState([]);
  const [loading, setLoading] = useState(true);
  const [capturing, setCapturing] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validationStep, setValidationStep] = useState(null);
  const [validationProgress, setValidationProgress] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [vmRes, snapRes, profRes, valRes, plansRes, auditRes] = await Promise.all([
        fetchJSON(`/api/vms/${vmId}`).catch((e) => ({ error: e })),
        fetchJSON(`/api/vms/${vmId}/snapshots`).catch((e) => ({ error: e })),
        fetchJSON(`/api/vms/${vmId}/baseline/profile`).catch((e) => ({ error: e })),
        fetchJSON(`/api/vms/${vmId}/validation/latest`).catch((e) => ({ error: e })),
        fetchJSON(`/api/plans?limit=20`).catch((e) => ({ error: e })),
        fetchJSON(`/api/audit?resource_type=vm&limit=50`).catch((e) => ({ error: e })),
      ]);

      if (vmRes.error || vmRes.status === 404) {
        setVmError(vmRes.error?.message || "VM not found");
        setVm(null);
      } else {
        setVm(vmRes.data);
        setVmError(null);
      }

      setSnapshots(snapRes.data || []);
      const meta = profRes.data?.latest_meta || {};
      setProfile(meta.os_profile || null);

      // /validation/latest now returns 200 with { validation: null } when
      // none exist — the wrapper means the empty state is just data, not an
      // error path the UI has to differentiate from "VM doesn't exist".
      if (!valRes.error) setValidation(valRes.data?.validation || null);

      // Filter the global audit feed down to this VM. The audit endpoint
      // doesn't accept a resource_id query yet, so we filter client-side
      // out of the recent slice. Good enough for "Last 10 entries".
      const all = auditRes.data || [];
      setAudit(all.filter((a) => String(a.resource_id) === String(vmId)).slice(0, 10));

      // Plans referencing this VM.
      const planList = (plansRes.data || []).filter((p) => (p.vm_ids || []).includes(vmId));
      setPlans(planList);
    } finally {
      setLoading(false);
    }
  }, [vmId]);

  useEffect(() => { load(); }, [load]);

  const onCaptureNow = async () => {
    if (capturing) return;
    setCapturing(true);
    try {
      const { data } = await fetchJSON(`/api/vms/${vmId}/capture`, { method: "POST" });
      toast(`Capturing baseline for ${vm?.name || `vm ${vmId}`}…`, { ...TOAST_OPTS, icon: "📡" });
      // Poll until the task resolves so the UI reflects the result without
      // a manual refresh.
      const startedAt = Date.now();
      let resolved = false;
      while (!resolved && Date.now() - startedAt < 90_000) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const status = await fetchJSON(`/api/vms/${vmId}/capture/${data.task_id}`);
          if (status.data?.status === "completed") {
            toast.success("Baseline captured", TOAST_OPTS);
            resolved = true;
          } else if (status.data?.status === "failed") {
            toast.error(`Capture failed: ${classifyCaptureError(status.data.error)}`, { ...TOAST_OPTS, duration: 8000 });
            resolved = true;
          }
        } catch { /* keep polling */ }
      }
      if (!resolved) toast("Capture still running — refresh in a moment", { ...TOAST_OPTS, icon: "⏱" });
      await load();
    } catch (e) {
      toast.error(e.message || "Failed to start capture", TOAST_OPTS);
    } finally {
      setCapturing(false);
    }
  };

  const onRunValidation = async () => {
    if (validating) return;
    if (!snapshots.length) {
      toast.error("Capture a baseline first before running validation", TOAST_OPTS);
      return;
    }
    setValidating(true);
    setValidationStep("queued");
    setValidationProgress(0);
    try {
      const { data } = await fetchJSON(`/api/vms/${vmId}/validate`, { method: "POST" });
      toast(`Validating ${vm?.name || `vm ${vmId}`}…`, { ...TOAST_OPTS, icon: "🤖" });
      const startedAt = Date.now();
      let finalStatus = null;
      // Poll every 3s until terminal — same cadence used elsewhere.
      while (finalStatus == null && Date.now() - startedAt < 180_000) {
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const status = await fetchJSON(`/api/vms/${vmId}/validate/${data.task_id}`);
          if (status.data) {
            setValidationStep(status.data.current_step);
            setValidationProgress(status.data.progress_percent || 0);
            if (status.data.status === "completed") {
              finalStatus = "completed";
              const v = status.data.verdict;
              const msg = v === "pass" ? "Validation passed" : v === "warn" ? "Validation completed with warnings" : "Validation found critical issues";
              if (v === "pass") toast.success(msg, TOAST_OPTS);
              else if (v === "warn") toast(msg, { ...TOAST_OPTS, icon: "⚠️" });
              else toast.error(msg, { ...TOAST_OPTS, duration: 8000 });
            } else if (status.data.status === "failed") {
              finalStatus = "failed";
              toast.error(`Validation failed: ${classifyCaptureError(status.data.error)}`, { ...TOAST_OPTS, duration: 8000 });
            }
          }
        } catch { /* keep polling */ }
      }
      if (finalStatus == null) toast("Validation still running — refresh in a moment", { ...TOAST_OPTS, icon: "⏱" });
      await load();
    } catch (e) {
      toast.error(e.message || "Failed to start validation", TOAST_OPTS);
    } finally {
      setValidating(false);
      setValidationStep(null);
      setValidationProgress(0);
    }
  };

  const onDelete = async () => {
    if (!vm) return;
    const typed = window.prompt(`Type the hostname to confirm deletion of ${vm.name}:\n${vm.source_hostname}`);
    if (typed !== vm.source_hostname) {
      if (typed != null) toast.error("Hostname did not match — VM not deleted", TOAST_OPTS);
      return;
    }
    try {
      await fetchJSON(`/api/vms/${vmId}`, { method: "DELETE" });
      toast.success(`Deleted ${vm.name}`, TOAST_OPTS);
      navigate("/");
    } catch (e) {
      toast.error(e.message || `Failed to delete ${vm.name}`, TOAST_OPTS);
    }
  };

  const verdict = validation?.status || null;
  const hasBaseline = snapshots.length > 0;

  // "Last Capture Attempt" inferred from the audit feed — the most recent
  // capture.triggered or baseline.collected row for this VM.
  const lastAttempt = (audit || []).find((a) =>
    a?.action === "capture.triggered" || a?.action === "baseline.collected"
  );

  if (loading) {
    return <Shell><div style={{ padding: 40, color: "#aaaacc" }}>Loading…</div></Shell>;
  }
  if (vmError) {
    return <Shell><Error msg={vmError} /></Shell>;
  }
  if (!vm) return null;

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS}/>

      {/* Header */}
      <header style={{
        position: "sticky", top: 0, zIndex: 50,
        background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
        borderBottom: "1px solid #1a1a2e",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "16px 32px", gap: 24,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <button onClick={() => navigate("/")} style={btnSecondary}>← Inventory</button>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{vm.name}</div>
            <div style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace", marginTop: 3 }}>
              {vm.source_hostname}{vm.ip_address ? ` · ${vm.ip_address}` : ""}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button onClick={onCaptureNow} disabled={capturing} style={{ ...btnPrimary, opacity: capturing ? 0.6 : 1 }}>
            {capturing ? "Capturing…" : "📡 Capture Now"}
          </button>
          <button
            onClick={onRunValidation}
            disabled={validating || !hasBaseline}
            title={!hasBaseline ? "Capture a baseline first" : "Run post-migration validation now"}
            style={{
              ...btnPrimary,
              background: validating ? "#1d3a8a" : "#0d4a3a",
              borderColor: "#00ff8888",
              opacity: (validating || !hasBaseline) ? 0.6 : 1,
              cursor: (validating || !hasBaseline) ? "not-allowed" : "pointer",
            }}
          >
            {validating ? "Validating…" : (validation ? "🤖 Re-run Validation" : "🤖 Run Validation")}
          </button>
          <button onClick={onDelete} style={{ ...btnSecondary, color: "#ff99aa", borderColor: "#ff557755" }}>🗑 Delete</button>
        </div>
      </header>

      <main style={{ maxWidth: 1280, margin: "0 auto", padding: 32 }}>
        {lastAttempt && (
          <Banner
            label="Last capture attempt"
            ts={lastAttempt.timestamp}
            kind={lastAttempt.action === "baseline.collected" ? "ok" : "info"}
            text={
              lastAttempt.action === "baseline.collected"
                ? `Baseline #${lastAttempt.details?.snapshot_number || "?"} collected by ${lastAttempt.actor || "unknown"}`
                : `Capture triggered (task ${(lastAttempt.details?.task_id || "").slice(0, 8) || "?"}…)`
            }
          />
        )}

        <Section title="Overview" defaultOpen>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 18 }}>
            <Field label="Hostname"          value={vm.source_hostname} mono />
            <Field label="IP Address"        value={vm.ip_address || "—"} mono />
            <Field label="SSH User"          value={vm.ssh_user || "(default)"} mono />
            <Field label="SSH Port"          value={vm.ssh_port} mono />
            <Field label="OS Family"         value={vm.os_family || "—"} mono />
            <Field label="Role"              value={vm.role || "—"} />
            <Field label="Current Platform"  value={vm.current_platform || "—"} />
            <Field label="Environment"       value={vm.environment || "—"} />
            <Field label="Owner"             value={vm.owner || "—"} />
            <Field label="Status"            value={vm.status} mono />
          </div>
          {vm.notes && (
            <div style={{ marginTop: 18, padding: "14px 16px", border: "1px solid #14142a", background: "#07070f", fontSize: 14, color: "#ccccee", lineHeight: 1.6 }}>
              {vm.notes}
            </div>
          )}
        </Section>

        <Section title="Baseline Status" defaultOpen={false}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 18 }}>
            <Stat label="Snapshots Collected" value={snapshots.length} />
            <Stat label="Earliest Collected" value={snapshots.length ? new Date(snapshots[snapshots.length - 1].collected_at).toLocaleString() : "—"} />
            <Stat label="Latest Collected"   value={snapshots.length ? new Date(snapshots[0].collected_at).toLocaleString() : "—"} />
          </div>
          {profile && (
            <div style={{ marginTop: 18 }}>
              <Subtitle>Detected OS</Subtitle>
              <OSBadge profile={profile} />
            </div>
          )}
          {snapshots.length === 0 ? (
            <div style={{ marginTop: 22, padding: "16px 18px", border: "1px dashed #2a2a44", color: "#aaaacc", fontSize: 13, lineHeight: 1.6 }}>
              No baseline captured yet. Click <strong style={{ color: "#ccccee" }}>📡 Capture Now</strong> in the header to record this VM&apos;s pre-migration state.
            </div>
          ) : (
            <div style={{ marginTop: 22 }}>
              <Subtitle>Snapshot History (last {Math.min(snapshots.length, 10)})</Subtitle>
              <div style={{ border: "1px solid #1a1a2e" }}>
                <div style={{ display: "grid", gridTemplateColumns: "0.5fr 1.5fr 1fr 1fr", padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16" }}>
                  {["#", "Collected At", "SSH User", "Checksum"].map((h) => (
                    <span key={h} style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>{h}</span>
                  ))}
                </div>
                {snapshots.slice(0, 10).map((s) => (
                  <div key={s.id} style={{ display: "grid", gridTemplateColumns: "0.5fr 1.5fr 1fr 1fr", padding: "10px 14px", borderBottom: "1px solid #0f0f1e", fontSize: 13, fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>
                    <span>{s?.snapshot_number ?? "—"}</span>
                    <span>{s?.collected_at ? new Date(s.collected_at).toLocaleString() : "—"}</span>
                    <span>{s?.ssh_user ?? "—"}</span>
                    <span style={{ color: "#aaaacc" }}>{s?.checksum ? `${s.checksum.slice(0, 12)}…` : "—"}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Section>

        <Section title="Validation Results" defaultOpen>
          {validating && (
            <ValidationProgress step={validationStep} percent={validationProgress} />
          )}
          {verdict === null ? (
            <div style={{ padding: "18px 20px", border: "1px dashed #2a2a44", background: "#07070f" }}>
              <div style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.6, marginBottom: 14 }}>
                {hasBaseline
                  ? "No validation runs yet. Click Run Validation to compare current state against the baseline."
                  : "Capture a baseline first — validation compares the current state against the recorded baseline."}
              </div>
              {hasBaseline && (
                <button
                  onClick={onRunValidation}
                  disabled={validating}
                  style={{
                    background: "#0d4a3a", border: "1px solid #00ff8888", color: "#eef2ff",
                    padding: "10px 16px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
                    letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                    cursor: validating ? "not-allowed" : "pointer", opacity: validating ? 0.6 : 1,
                  }}
                >
                  {validating ? "Validating…" : "🤖 Run Validation"}
                </button>
              )}
            </div>
          ) : (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
                <span style={{
                  fontSize: 12, color: STATUS_COLOR[verdict], border: `1px solid ${STATUS_COLOR[verdict]}55`,
                  background: `${STATUS_COLOR[verdict]}11`, padding: "5px 14px",
                  letterSpacing: "0.08em", fontWeight: 700, fontFamily: "'Share Tech Mono', monospace",
                }}>{STATUS_LABEL[verdict]}</span>
                <span style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>
                  Validated {validation?.validated_at ? new Date(validation.validated_at).toLocaleString() : "—"}
                </span>
                <span style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>
                  · {validation?.findings?.length || 0} finding{validation?.findings?.length === 1 ? "" : "s"}
                </span>
              </div>
              {validation?.summary && (
                <p style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.6, marginBottom: 22 }}>{validation.summary}</p>
              )}
              {validation?.findings?.length > 0 && (
                <>
                  <Subtitle>Findings ({(validation.findings ?? []).length})</Subtitle>
                  {(validation.findings ?? []).map((f, i) => (
                    <Finding key={i} f={f} />
                  ))}
                </>
              )}
              {validation?.remediation?.length > 0 && (
                <>
                  <Subtitle>Top-Level Remediation</Subtitle>
                  {validation.remediation.map((r, i) => (
                    <div key={i} style={{ marginBottom: 8 }}>
                      <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6 }}>
                        <span style={{ color: "#88aaff", fontWeight: 700, marginRight: 8 }}>{r.step ?? i + 1}.</span>{r.action}
                      </div>
                      {r.command && (
                        <code style={{ display: "block", marginTop: 4, padding: "8px 12px", background: "#07070f", border: "1px solid #1a1a2e", fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: "#ccccee" }}>$ {r.command}</code>
                      )}
                    </div>
                  ))}
                </>
              )}
            </>
          )}
          <div style={{ marginTop: 14 }}>
            <Link to="/reports/full-validation" style={{ color: "#88aaff", fontSize: 13 }}>
              View full validation report →
            </Link>
          </div>
        </Section>

        <Section title="Migration Plan Membership" defaultOpen={false}>
          {plans.length === 0 ? (
            <p style={{ color: "#aaaacc" }}>This VM is not in any migration plan yet.</p>
          ) : plans.map((p) => {
            const wave = (p.waves || []).find((w) => (w.vm_ids || []).includes(vmId));
            return (
              <div key={p.id} style={{ border: "1px solid #1a1a2e", padding: 18, marginBottom: 12, background: "#0a0a18" }}>
                <div style={{ fontSize: 15, fontWeight: 600 }}>Plan #{p.id}</div>
                <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
                  {(p.waves || []).length} waves · model {p.model || "—"} · {p.created_at ? new Date(p.created_at).toLocaleString() : "—"}
                </div>
                {wave && (
                  <div style={{ marginTop: 10, fontSize: 13, color: "#ccccee" }}>
                    Member of <strong>Wave {wave.wave_number}</strong> · risk {wave.estimated_risk}
                    {wave.rationale ? <div style={{ color: "#aaaacc", marginTop: 6, lineHeight: 1.6 }}>{wave.rationale}</div> : null}
                  </div>
                )}
              </div>
            );
          })}
        </Section>

        <Section title="Audit Trail" defaultOpen={false}>
          {audit.length === 0 ? (
            <p style={{ color: "#aaaacc" }}>No audit log entries for this VM yet.</p>
          ) : (
            <div style={{ border: "1px solid #1a1a2e" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1fr", padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16" }}>
                {["Timestamp", "Actor", "Action", "Details"].map((h) => (
                  <span key={h} style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>{h}</span>
                ))}
              </div>
              {audit.map((a) => {
                const details = JSON.stringify(a?.details ?? {});
                return (
                  <div key={a.id} style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr 1fr", padding: "10px 14px", borderBottom: "1px solid #0f0f1e", fontSize: 12, fontFamily: "'Share Tech Mono', monospace" }}>
                    <span style={{ color: "#aaaacc" }}>{a?.timestamp ? new Date(a.timestamp).toLocaleString() : "—"}</span>
                    <span style={{ color: "#eeeeff" }}>{a?.actor ?? "—"}</span>
                    <span style={{ color: "#88aaff" }}>{a?.action ?? "—"}</span>
                    <span title={details} style={{ color: "#aaaacc", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {details.slice(0, 80)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          <div style={{ marginTop: 14 }}>
            <Link to="/" style={{ color: "#88aaff", fontSize: 13 }}>
              View full audit log →
            </Link>
          </div>
        </Section>
      </main>
    </Shell>
  );
}


// ---------------------------------------------------------------------------
// Reusable bits (kept local to this file for now; can be promoted later)
// ---------------------------------------------------------------------------
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};

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

function Section({ title, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section style={{ border: "1px solid #1a1a2e", marginBottom: 18, background: "#0a0a18" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          width: "100%", padding: "16px 22px",
          background: "transparent", border: "none", color: "#eeeeff",
          fontFamily: "'Barlow', sans-serif", fontSize: 15, fontWeight: 700,
          letterSpacing: "0.04em", textTransform: "uppercase", cursor: "pointer",
        }}>
        <span>{title}</span>
        <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#aaaacc", fontSize: 13, transform: open ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}>▶</span>
      </button>
      {open && <div style={{ padding: "4px 22px 22px" }}>{children}</div>}
    </section>
  );
}

function Field({ label, value, mono }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 5 }}>{label}</div>
      <div style={{ fontSize: 14, color: "#ccccee", fontFamily: mono ? "'Share Tech Mono', monospace" : "'Barlow', sans-serif" }}>{value ?? "—"}</div>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div style={{ border: "1px solid #14142a", background: "#07070f", padding: "16px 18px" }}>
      <div style={{ fontSize: 18, color: "#eeeeff", fontWeight: 700, fontFamily: "'Share Tech Mono', monospace" }}>{value}</div>
      <div style={{ fontSize: 11, color: "#aaaacc", marginTop: 4, letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>{label}</div>
    </div>
  );
}

function Subtitle({ children }) {
  return <div style={{ fontSize: 12, color: "#aaaacc", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 10 }}>{children}</div>;
}

function Banner({ label, ts, text, kind }) {
  const color = kind === "ok" ? "#00ff88" : "#88aaff";
  return (
    <div style={{ padding: "14px 18px", border: `1px solid ${color}55`, background: `${color}0d`, marginBottom: 22, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
      <div>
        <div style={{ fontSize: 11, color, letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>{label}</div>
        <div style={{ fontSize: 13, color: "#ccccee", marginTop: 4 }}>{text}</div>
      </div>
      <span style={{ fontSize: 12, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>{ts ? new Date(ts).toLocaleString() : "—"}</span>
    </div>
  );
}

function Error({ msg }) {
  return (
    <div style={{ maxWidth: 720, margin: "60px auto", padding: "20px 24px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)" }}>
      <div style={{ fontSize: 11, color: "#ff5577", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 6 }}>Error</div>
      <div style={{ fontSize: 14, color: "#ccaaaa" }}>{msg}</div>
      <div style={{ marginTop: 14 }}><Link to="/" style={{ color: "#88aaff" }}>← Back to inventory</Link></div>
    </div>
  );
}

function ValidationProgress({ step, percent }) {
  const label = STEP_LABEL[step] || "starting";
  const pct = Math.max(0, Math.min(100, percent || 0));
  return (
    <div style={{
      marginBottom: 18, padding: "14px 16px",
      border: "1px solid #4488ff55", background: "rgba(68,136,255,0.06)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 11, color: "#88aaff", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase" }}>
          Validation in progress · {label}
        </span>
        <span style={{ fontSize: 12, color: "#88aaff", fontFamily: "'Share Tech Mono', monospace" }}>{pct}%</span>
      </div>
      <div style={{ height: 4, background: "#0a0a18", border: "1px solid #1a1a2e" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: "#4488ff", transition: "width 0.4s ease" }} />
      </div>
    </div>
  );
}

function Finding({ f }) {
  const sevColor = SEVERITY_COLOR[f.severity] || "#aaaacc";
  const conf = CONFIDENCE_COLOR[f.confidence] || null;
  return (
    <div style={{
      padding: "14px 18px", marginBottom: 12,
      border: "1px solid #1a1a2e", borderLeft: `3px solid ${sevColor}`,
      background: "#07070f",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        <span style={{
          fontSize: 11, color: sevColor, border: `1px solid ${sevColor}66`,
          padding: "3px 9px", letterSpacing: "0.08em", fontWeight: 700,
          textTransform: "uppercase", fontFamily: "'Share Tech Mono', monospace",
        }}>{f.severity || "info"}</span>
        {f.category && (
          <span style={{ fontSize: 11, color: "#aaaacc", letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 600 }}>
            {f.category}
          </span>
        )}
        {conf && (
          <span style={{
            fontSize: 10, color: conf.color, border: `1px solid ${conf.color}55`,
            padding: "2px 7px", letterSpacing: "0.08em", fontWeight: 700,
            textTransform: "uppercase",
          }}>{conf.label} CONFIDENCE</span>
        )}
      </div>
      {f.title && (
        <div style={{ fontSize: 14, color: "#eeeeff", fontWeight: 700, marginBottom: 6, lineHeight: 1.5 }}>
          {f.title}
        </div>
      )}
      {(f.description || f.message) && (
        <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6, marginBottom: f.source_evidence || f.current_evidence ? 12 : 0 }}>
          {f.description || f.message}
        </div>
      )}
      {(f.source_evidence || f.current_evidence) && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: f.remediation ? 12 : 0 }}>
          {f.source_evidence && (
            <Evidence label="Baseline" value={f.source_evidence} />
          )}
          {f.current_evidence && (
            <Evidence label="Current" value={f.current_evidence} accent="#ffaa00" />
          )}
        </div>
      )}
      {f.remediation && (
        <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6, paddingTop: 10, borderTop: "1px solid #1a1a2e" }}>
          <span style={{ color: "#88aaff", fontWeight: 700, marginRight: 8 }}>Remediation:</span>{f.remediation}
        </div>
      )}
    </div>
  );
}

function Evidence({ label, value, accent = "#88aaff" }) {
  return (
    <div style={{ padding: "8px 10px", background: "#0a0a18", border: "1px solid #1a1a2e" }}>
      <div style={{ fontSize: 10, color: accent, letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 4 }}>
        {label}
      </div>
      <code style={{ fontSize: 12, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", lineHeight: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {value}
      </code>
    </div>
  );
}

function OSBadge({ profile }) {
  const conf = CONFIDENCE_COLOR[profile?.detection_confidence] || CONFIDENCE_COLOR.low;
  const label = profile?.distro || "Unknown";
  const ver = profile?.major_version ? `${profile.major_version}.${profile.minor_version || 0}` : "?";
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 14, padding: "10px 14px", border: `1px solid ${conf.color}55`, background: `${conf.color}0d` }}>
      <div>
        <div style={{ fontSize: 13, color: "#eeeeff", fontWeight: 600 }}>
          {label}{" "}
          <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>{ver}</span>
        </div>
        {profile?.kernel_version && (
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 3, fontFamily: "'Share Tech Mono', monospace" }}>
            kernel {profile.kernel_version}{profile.architecture ? ` · ${profile.architecture}` : ""}
          </div>
        )}
      </div>
      <span style={{ fontSize: 10, color: conf.color, border: `1px solid ${conf.color}66`, padding: "3px 9px", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", fontFamily: "'Barlow', sans-serif" }}>
        {conf.label} CONFIDENCE
      </span>
    </div>
  );
}


// Catches render-time exceptions so a single bad row in a finding/snapshot/audit
// list never blanks the whole page. Shows a friendly fallback with a Reload
// button and dumps the error to console for diagnosis.
class VMDetailErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error("VMDetail render error:", error, info);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Shell>
        <div style={{ maxWidth: 720, margin: "60px auto", padding: "24px 28px", border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)" }}>
          <div style={{ fontSize: 11, color: "#ff5577", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 8 }}>
            Something went wrong
          </div>
          <div style={{ fontSize: 14, color: "#ccaaaa", lineHeight: 1.6, marginBottom: 18 }}>
            The VM detail view hit an unexpected error and couldn&apos;t finish rendering. The full
            stack is in the browser console. You can reload the page to retry, or head back to
            the inventory.
          </div>
          <div style={{ fontSize: 12, color: "#8888aa", fontFamily: "'Share Tech Mono', monospace", marginBottom: 18, padding: "10px 12px", background: "#07070f", border: "1px solid #1a1a2e", overflow: "auto" }}>
            {String(this.state.error?.message || this.state.error)}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button
              onClick={() => window.location.reload()}
              style={{ background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff", padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12, letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer" }}
            >
              Reload
            </button>
            <Link to="/" style={{ background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc", padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12, letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, textDecoration: "none" }}>
              ← Back to inventory
            </Link>
          </div>
        </div>
      </Shell>
    );
  }
}

export default function VMDetail() {
  return (
    <VMDetailErrorBoundary>
      <VMDetailBody />
    </VMDetailErrorBoundary>
  );
}
