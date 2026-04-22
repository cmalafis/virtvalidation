import { useEffect, useMemo, useState } from "react";

const STATUS_CONFIG = {
  healthy:  { color: "#00ff88", bg: "rgba(0,255,136,0.08)", label: "HEALTHY",  dot: "#00ff88" },
  degraded: { color: "#ffaa00", bg: "rgba(255,170,0,0.08)",  label: "DEGRADED", dot: "#ffaa00" },
  failed:   { color: "#ff3355", bg: "rgba(255,51,85,0.08)",  label: "FAILED",   dot: "#ff3355" },
  captured: { color: "#4488ff", bg: "rgba(68,136,255,0.08)", label: "CAPTURED", dot: "#4488ff" },
  pending:  { color: "#666677", bg: "rgba(102,102,119,0.08)",label: "PENDING",  dot: "#666677" },
};

const SEVERITY_COLOR = { critical: "#ff3355", high: "#ffaa00", medium: "#ffdd44", low: "#4488ff" };

const VM_STATUS_MAP = {
  discovered:         { preStatus: "pending",  postStatus: "pending" },
  baseline_captured:  { preStatus: "captured", postStatus: "pending" },
  migrated:           { preStatus: "captured", postStatus: "pending" },
  validated:          { preStatus: "captured", postStatus: "healthy" },
  failed:             { preStatus: "captured", postStatus: "failed"  },
};

function mapVM(vm) {
  const mapped = VM_STATUS_MAP[vm.status] ?? { preStatus: "pending", postStatus: "pending" };
  return {
    id: vm.id,
    name: vm.name,
    ip: vm.ip_address ?? "—",
    os: vm.os_family ?? "—",
    role: vm.role ?? "—",
    preStatus: mapped.preStatus,
    postStatus: mapped.postStatus,
    rawStatus: vm.status,
    // Not tracked by the API yet — shown as "—" in the UI.
    cpu: null,
    mem: null,
    disk: null,
    wave: null,
    findings: [],
  };
}

const fmt = (v) => (v === null || v === undefined || v === "" ? "—" : v);

const StatusBadge = ({ status }) => {
  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.pending;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "3px 10px", borderRadius: 2,
      background: cfg.bg, border: `1px solid ${cfg.color}22`,
      fontSize: 10, fontFamily: "'Share Tech Mono', monospace",
      color: cfg.color, letterSpacing: "0.12em", fontWeight: 700,
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: cfg.color, boxShadow: `0 0 6px ${cfg.color}` }} />
      {cfg.label}
    </span>
  );
};

const SeverityTag = ({ s }) => (
  <span style={{
    fontSize: 9, fontFamily: "'Share Tech Mono', monospace",
    color: SEVERITY_COLOR[s], border: `1px solid ${SEVERITY_COLOR[s]}44`,
    padding: "2px 7px", borderRadius: 2, letterSpacing: "0.1em",
    textTransform: "uppercase", fontWeight: 700,
  }}>{s}</span>
);

const Metric = ({ label, value }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
    <span style={{ fontSize: 9, color: "#555577", fontFamily: "'Share Tech Mono', monospace", letterSpacing: "0.1em" }}>{label}</span>
    <span style={{ fontSize: 13, color: "#aaaacc", fontFamily: "'Share Tech Mono', monospace" }}>{value}</span>
  </div>
);

const Notice = ({ children, tone = "info" }) => {
  const color = tone === "error" ? "#ff3355" : tone === "warn" ? "#ffaa00" : "#4488ff";
  return (
    <div style={{
      padding: "12px 16px", border: `1px solid ${color}33`,
      background: `${color}0d`, fontSize: 11, color: "#8888aa",
      fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
    }}>
      <span style={{ color, letterSpacing: "0.15em", marginRight: 8, fontFamily: "'Share Tech Mono', monospace", fontSize: 10 }}>
        {tone === "error" ? "ERROR" : tone === "warn" ? "NOTICE" : "INFO"}
      </span>
      {children}
    </div>
  );
};

export default function VirtValidate() {
  const [activeTab, setActiveTab] = useState("validation");
  const [selectedVM, setSelectedVM] = useState(null);
  const [vms, setVms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoading(true);
        const res = await fetch("/api/vms");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) {
          setVms(data.map(mapVM));
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e.message || "Failed to load VMs");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (selectedVM && !vms.some((v) => v.id === selectedVM.id)) {
      setSelectedVM(null);
    }
  }, [vms, selectedVM]);

  const { healthy, degraded, failed, pending, total, totalFindings } = useMemo(() => {
    const counts = { healthy: 0, degraded: 0, failed: 0, pending: 0 };
    let findings = 0;
    for (const v of vms) {
      counts[v.postStatus] = (counts[v.postStatus] || 0) + 1;
      findings += v.findings.length;
    }
    return { ...counts, total: vms.length, totalFindings: findings };
  }, [vms]);

  const validatedPct = total === 0 ? 0 : Math.round(((healthy + degraded + failed) / total) * 100);

  const tabs = ["validation", "migration plan", "inventory", "reports"];

  return (
    <div style={{
      minHeight: "100vh", background: "#07070f",
      fontFamily: "'Share Tech Mono', monospace",
      color: "#ccccdd",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: #0d0d1a; }
        ::-webkit-scrollbar-thumb { background: #2a2a44; border-radius: 2px; }
        .vm-row:hover { background: rgba(68,136,255,0.04) !important; cursor: pointer; }
        .tab-btn:hover { color: #aaaaff !important; }
        .wave-header:hover { background: rgba(255,255,255,0.03) !important; cursor: pointer; }
        .finding-row { border-left: 2px solid transparent; padding-left: 12px; margin-bottom: 12px; transition: border-color 0.2s; }
        @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.4 } }
        @keyframes fadeIn { from { opacity:0; transform: translateY(8px); } to { opacity:1; transform: translateY(0); } }
        .fade-in { animation: fadeIn 0.25s ease forwards; }
        .scanline {
          position: fixed; top: 0; left: 0; right: 0; bottom: 0;
          background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
          pointer-events: none; z-index: 9999;
        }
      `}</style>

      <div className="scanline" />

      {/* Header */}
      <div style={{
        borderBottom: "1px solid #1a1a2e",
        padding: "0 32px",
        background: "linear-gradient(180deg, #0a0a18 0%, #07070f 100%)",
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", height: 56 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{
              width: 28, height: 28, border: "1px solid #4488ff44",
              display: "flex", alignItems: "center", justifyContent: "center",
              position: "relative",
            }}>
              <div style={{ width: 10, height: 10, background: "#4488ff", clipPath: "polygon(50% 0%, 100% 100%, 0% 100%)" }} />
              <div style={{ position: "absolute", inset: -3, border: "1px solid #4488ff22" }} />
            </div>
            <div>
              <div style={{ fontSize: 15, fontFamily: "'Barlow', sans-serif", fontWeight: 700, color: "#eeeeff", letterSpacing: "0.08em" }}>
                VIRTVALIDATE
              </div>
              <div style={{ fontSize: 9, color: "#444466", letterSpacing: "0.2em" }}>VM MIGRATION VALIDATION PLATFORM</div>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
            <div style={{ display: "flex", gap: 20 }}>
              {[
                { label: "HEALTHY", val: healthy, color: "#00ff88" },
                { label: "DEGRADED", val: degraded, color: "#ffaa00" },
                { label: "FAILED", val: failed, color: "#ff3355" },
                { label: "PENDING", val: pending, color: "#555577" },
              ].map(s => (
                <div key={s.label} style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 18, fontWeight: 700, color: s.color, fontFamily: "'Barlow', sans-serif", lineHeight: 1 }}>{s.val}</div>
                  <div style={{ fontSize: 8, color: "#444466", letterSpacing: "0.15em", marginTop: 2 }}>{s.label}</div>
                </div>
              ))}
            </div>
            <div style={{ width: 1, height: 32, background: "#1a1a2e" }} />
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 9, color: "#444466", letterSpacing: "0.15em" }}>CLUSTER</div>
              <div style={{ fontSize: 11, color: "#6666aa" }}>ocp-virt-prod-01</div>
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 0, marginTop: 0 }}>
          {tabs.map(tab => (
            <button key={tab} className="tab-btn" onClick={() => setActiveTab(tab)} style={{
              background: "none", border: "none", cursor: "pointer",
              padding: "10px 20px", fontSize: 10,
              fontFamily: "'Share Tech Mono', monospace", letterSpacing: "0.15em",
              textTransform: "uppercase",
              color: activeTab === tab ? "#4488ff" : "#444466",
              borderBottom: activeTab === tab ? "2px solid #4488ff" : "2px solid transparent",
              transition: "all 0.15s",
            }}>{tab}</button>
          ))}
        </div>
      </div>

      <div style={{ display: "flex", height: "calc(100vh - 105px)" }}>

        {/* Main content */}
        <div style={{ flex: 1, overflow: "auto", padding: 24 }}>

          {error && (
            <div style={{ marginBottom: 16 }}>
              <Notice tone="error">Failed to load VMs from backend: {error}. Is the API running at /api?</Notice>
            </div>
          )}
          {loading && !error && (
            <div style={{ marginBottom: 16 }}>
              <Notice>Loading VM inventory from backend…</Notice>
            </div>
          )}
          {!loading && !error && total === 0 && (
            <div style={{ marginBottom: 16 }}>
              <Notice tone="warn">No VMs registered yet. POST to /api/vms to add one.</Notice>
            </div>
          )}

          {/* VALIDATION TAB */}
          {activeTab === "validation" && (
            <div className="fade-in">
              {/* Progress bar */}
              <div style={{ marginBottom: 24, padding: "16px 20px", border: "1px solid #1a1a2e", background: "#0a0a18" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                  <span style={{ fontSize: 10, color: "#555577", letterSpacing: "0.15em" }}>MIGRATION VALIDATION PROGRESS</span>
                  <span style={{ fontSize: 11, color: "#4488ff" }}>{validatedPct}% VALIDATED</span>
                </div>
                <div style={{ height: 4, background: "#111122", borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ display: "flex", height: "100%" }}>
                    <div style={{ width: `${total === 0 ? 0 : (healthy / total) * 100}%`, background: "#00ff88", transition: "width 0.5s" }} />
                    <div style={{ width: `${total === 0 ? 0 : (degraded / total) * 100}%`, background: "#ffaa00", transition: "width 0.5s" }} />
                    <div style={{ width: `${total === 0 ? 0 : (failed / total) * 100}%`, background: "#ff3355", transition: "width 0.5s" }} />
                  </div>
                </div>
              </div>

              {/* VM Table */}
              <div style={{ border: "1px solid #1a1a2e" }}>
                <div style={{
                  display: "grid", gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
                  padding: "8px 16px", borderBottom: "1px solid #1a1a2e",
                  background: "#0a0a16",
                }}>
                  {["VM NAME", "ROLE", "IP ADDRESS", "vCPU", "MEM", "DISK", "STATUS"].map(h => (
                    <span key={h} style={{ fontSize: 9, color: "#444466", letterSpacing: "0.15em" }}>{h}</span>
                  ))}
                </div>
                {vms.map((vm, i) => (
                  <div key={vm.id} className="vm-row" onClick={() => setSelectedVM(selectedVM?.id === vm.id ? null : vm)}
                    style={{
                      display: "grid", gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
                      padding: "12px 16px",
                      borderBottom: i < vms.length - 1 ? "1px solid #0f0f1e" : "none",
                      background: selectedVM?.id === vm.id ? "rgba(68,136,255,0.06)" : "transparent",
                      transition: "background 0.15s",
                    }}>
                    <div>
                      <div style={{ fontSize: 12, color: "#ccccee", fontFamily: "'Barlow', sans-serif", fontWeight: 600 }}>{vm.name}</div>
                      <div style={{ fontSize: 9, color: "#444466", marginTop: 2 }}>{vm.os}</div>
                    </div>
                    <span style={{ fontSize: 11, color: "#8888aa", alignSelf: "center" }}>{vm.role}</span>
                    <span style={{ fontSize: 11, color: "#6666aa", alignSelf: "center", fontFamily: "'Share Tech Mono'" }}>{vm.ip}</span>
                    <span style={{ fontSize: 11, color: "#8888aa", alignSelf: "center" }}>{fmt(vm.cpu)}</span>
                    <span style={{ fontSize: 11, color: "#8888aa", alignSelf: "center" }}>{vm.mem == null ? "—" : `${vm.mem}GB`}</span>
                    <span style={{ fontSize: 11, color: "#8888aa", alignSelf: "center" }}>{fmt(vm.disk)}</span>
                    <div style={{ alignSelf: "center" }}><StatusBadge status={vm.postStatus} /></div>
                  </div>
                ))}
              </div>

              {/* Selected VM detail */}
              {selectedVM && (
                <div className="fade-in" style={{ marginTop: 16, border: "1px solid #1a1a2e", background: "#0a0a18", padding: 20 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
                    <div>
                      <div style={{ fontSize: 14, fontFamily: "'Barlow', sans-serif", fontWeight: 700, color: "#eeeeff" }}>{selectedVM.name}</div>
                      <div style={{ fontSize: 10, color: "#555577", marginTop: 3, letterSpacing: "0.1em" }}>
                        AI VALIDATION REPORT — {String(selectedVM.role || "UNASSIGNED").toUpperCase()}
                      </div>
                    </div>
                    <StatusBadge status={selectedVM.postStatus} />
                  </div>

                  <div style={{ display: "flex", gap: 32, marginBottom: 20, paddingBottom: 16, borderBottom: "1px solid #111122" }}>
                    <Metric label="PRE-MIGRATION" value={<StatusBadge status={selectedVM.preStatus} />} />
                    <Metric label="POST-MIGRATION" value={<StatusBadge status={selectedVM.postStatus} />} />
                    <Metric label="WAVE" value={selectedVM.wave == null ? "—" : `Wave ${selectedVM.wave}`} />
                    <Metric label="OS" value={selectedVM.os} />
                    <Metric label="IP" value={selectedVM.ip} />
                  </div>

                  {selectedVM.findings.length === 0 ? (
                    <div style={{ display: "flex", alignItems: "center", gap: 10, color: "#555577", fontSize: 11 }}>
                      <span style={{ fontSize: 14, color: "#4488ff" }}>◌</span>
                      <span>No validation findings available yet. Run validation to generate an AI report.</span>
                    </div>
                  ) : (
                    <div>
                      <div style={{ fontSize: 9, color: "#444466", letterSpacing: "0.15em", marginBottom: 12 }}>AI FINDINGS & REMEDIATION</div>
                      {selectedVM.findings.map((f, i) => (
                        <div key={i} className="finding-row" style={{ borderLeftColor: SEVERITY_COLOR[f.severity] }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                            <SeverityTag s={f.severity} />
                          </div>
                          <div style={{ fontSize: 12, color: "#bbbbcc", marginBottom: 6, fontFamily: "'Barlow', sans-serif", lineHeight: 1.5 }}>{f.finding}</div>
                          <div style={{ fontSize: 11, color: "#555577", fontFamily: "'Barlow', sans-serif", lineHeight: 1.5 }}>
                            <span style={{ color: "#333355" }}>→ </span>{f.remediation}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* MIGRATION PLAN TAB */}
          {activeTab === "migration plan" && (
            <div className="fade-in">
              <div style={{ marginBottom: 16 }}>
                <Notice tone="warn">
                  AI migration planner not yet available in this build. VMs are listed below without wave sequencing.
                </Notice>
              </div>

              <div style={{ border: "1px solid #1a1a2e" }}>
                <div className="wave-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", background: "#0a0a16" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{
                      width: 22, height: 22, border: "1px solid #4488ff44",
                      display: "flex", alignItems: "center", justifyContent: "center",
                      fontSize: 9, color: "#4488ff", fontWeight: 700,
                    }}>—</div>
                    <span style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 600, color: "#ccccee" }}>Unsequenced VMs</span>
                    <span style={{ fontSize: 10, color: "#444466" }}>{vms.length} VMs</span>
                  </div>
                </div>
                <div style={{ padding: "14px 18px", borderTop: "1px solid #0f0f1e" }}>
                  {vms.length === 0 ? (
                    <div style={{ fontSize: 11, color: "#555577" }}>No VMs to plan.</div>
                  ) : vms.map(vm => (
                    <div key={vm.id} style={{
                      display: "flex", alignItems: "center", justifyContent: "space-between",
                      padding: "10px 0", borderBottom: "1px solid #0f0f1e",
                    }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                        <div style={{ width: 6, height: 6, background: "#2a2a44", borderRadius: "50%" }} />
                        <div>
                          <div style={{ fontSize: 12, color: "#ccccee", fontFamily: "'Barlow', sans-serif", fontWeight: 600 }}>{vm.name}</div>
                          <div style={{ fontSize: 9, color: "#444466", marginTop: 1 }}>{vm.role} · {vm.os}</div>
                        </div>
                      </div>
                      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                        <span style={{ fontSize: 10, color: "#555577" }}>{vm.ip}</span>
                        <StatusBadge status={vm.postStatus} />
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* INVENTORY TAB */}
          {activeTab === "inventory" && (
            <div className="fade-in">
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
                {vms.map(vm => (
                  <div key={vm.id} style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 16 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
                      <div>
                        <div style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 700, color: "#eeeeff" }}>{vm.name}</div>
                        <div style={{ fontSize: 9, color: "#444466", marginTop: 2 }}>{vm.role}</div>
                      </div>
                      <StatusBadge status={vm.postStatus} />
                    </div>
                    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                      <Metric label="OS" value={vm.os} />
                      <Metric label="IP" value={vm.ip} />
                      <Metric label="vCPU" value={fmt(vm.cpu)} />
                      <Metric label="MEMORY" value={vm.mem == null ? "—" : `${vm.mem}GB`} />
                      <Metric label="DISK" value={fmt(vm.disk)} />
                      <Metric label="WAVE" value={vm.wave == null ? "—" : `Wave ${vm.wave}`} />
                    </div>
                    {vm.findings.length > 0 && (
                      <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px solid #111122" }}>
                        <span style={{ fontSize: 9, color: "#ff335588", letterSpacing: "0.1em" }}>{vm.findings.length} FINDING{vm.findings.length > 1 ? "S" : ""}</span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* REPORTS TAB */}
          {activeTab === "reports" && (
            <div className="fade-in">
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {[
                  { name: "Full Migration Validation Report", desc: "All VMs, all findings, remediation steps. CISO-ready.", icon: "▤" },
                  { name: "Executive Summary", desc: "High-level migration status, risk overview, wave completion.", icon: "◈" },
                  { name: "Failed & Degraded VMs", desc: "Filtered report — only VMs requiring action.", icon: "⚠" },
                  { name: "Migration Wave Plan", desc: "AI-generated wave sequencing with rationale.", icon: "◎" },
                  { name: "Pre-Migration Baseline Snapshot", desc: "Full captured state of all VMs before migration.", icon: "◷" },
                ].map(r => (
                  <div key={r.name} style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    padding: "16px 20px", border: "1px solid #1a1a2e", background: "#0a0a18",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                      <span style={{ fontSize: 18, color: "#2a2a44" }}>{r.icon}</span>
                      <div>
                        <div style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 600, color: "#ccccee" }}>{r.name}</div>
                        <div style={{ fontSize: 10, color: "#444466", marginTop: 2 }}>{r.desc}</div>
                      </div>
                    </div>
                    <button style={{
                      background: "none", border: "1px solid #2a2a44", color: "#6666aa",
                      padding: "6px 16px", fontSize: 9, fontFamily: "'Share Tech Mono', monospace",
                      letterSpacing: "0.12em", cursor: "pointer",
                    }}>EXPORT PDF</button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right sidebar — system info */}
        <div style={{ width: 220, borderLeft: "1px solid #1a1a2e", padding: 16, background: "#080814", flexShrink: 0 }}>
          <div style={{ fontSize: 9, color: "#333355", letterSpacing: "0.15em", marginBottom: 16 }}>SYSTEM</div>

          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {[
              { label: "APPLIANCE", value: "v0.1.0-alpha" },
              { label: "MODEL", value: "llama3:8b" },
              { label: "INFERENCE", value: "LOCAL / OLLAMA" },
              { label: "CLUSTER", value: "ocp-virt-prod-01" },
              { label: "TOTAL VMS", value: total },
              { label: "VALIDATED", value: `${healthy + degraded + failed} / ${total}` },
              { label: "FINDINGS", value: totalFindings },
            ].map(item => (
              <div key={item.label}>
                <div style={{ fontSize: 8, color: "#333355", letterSpacing: "0.15em", marginBottom: 3 }}>{item.label}</div>
                <div style={{ fontSize: 11, color: "#6666aa" }}>{item.value}</div>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid #111122" }}>
            <div style={{ fontSize: 9, color: "#333355", letterSpacing: "0.15em", marginBottom: 12 }}>QUICK ACTIONS</div>
            {["RUN VALIDATION", "CAPTURE BASELINE", "GENERATE PLAN"].map(action => (
              <button key={action} style={{
                display: "block", width: "100%", marginBottom: 8,
                background: "none", border: "1px solid #1a1a2e", color: "#555577",
                padding: "8px 10px", fontSize: 9, fontFamily: "'Share Tech Mono', monospace",
                letterSpacing: "0.1em", cursor: "pointer", textAlign: "left",
                transition: "all 0.15s",
              }}
                onMouseOver={e => { e.target.style.borderColor = "#4488ff44"; e.target.style.color = "#4488ff"; }}
                onMouseOut={e => { e.target.style.borderColor = "#1a1a2e"; e.target.style.color = "#555577"; }}
              >{action}</button>
            ))}
          </div>

          <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid #111122" }}>
            <div style={{ fontSize: 9, color: "#333355", letterSpacing: "0.15em", marginBottom: 8 }}>AI ENGINE</div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div style={{ width: 6, height: 6, borderRadius: "50%", background: "#00ff88", boxShadow: "0 0 8px #00ff88", animation: "pulse 2s infinite" }} />
              <span style={{ fontSize: 10, color: "#00ff8899" }}>ONLINE — AIR GAPPED</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
