import { useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useNavigate } from "react-router-dom";

// Strategy-driven migration planning wizard. 8 conceptual steps
// flattened into a scrollable single-page form so operators don't
// lose context as they fill out their migration intent. Submission
// is async — POST /api/plans/generate returns 202 + a task_id, then
// we poll status every 3s until completion or failure.

const TOAST_OPTS = {
  style: { background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
           fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5 },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

// Wave-size estimates surfaced under the radio choice. Backend doesn't
// constrain these — they're operator hints derived from real migration
// retros. Adjust if the typical-pace data shifts.
const WAVE_SIZE_OPTIONS = [
  { value: "small_5_10", label: "Small", count: "5–10 VMs/wave", flavor: "slow but safe" },
  { value: "medium_10_20", label: "Medium", count: "10–20 VMs/wave", flavor: "balanced — recommended" },
  { value: "large_20_50", label: "Large", count: "20–50 VMs/wave", flavor: "aggressive" },
  { value: "custom", label: "Custom", count: "you choose", flavor: "" },
];

const GROUPING_OPTIONS = [
  { value: "application", label: "By application",
    desc: "Group all VMs of the same application together. Best when applications have clear boundaries and shouldn't be split across waves." },
  { value: "vcenter_folder", label: "By vCenter folder",
    desc: "Use VMware folder structure as wave boundaries. Best when folders reflect organizational/operational structure." },
  { value: "application_owner", label: "By application owner",
    desc: "Group by who owns the application. Best when minimizing coordination across teams matters more than technical groupings." },
  { value: "environment", label: "By environment",
    desc: "Migrate dev → staging → prod in sequence. Classic conservative approach." },
  { value: "business_unit", label: "By business unit",
    desc: "Use organizational boundaries. Best for matrix organizations." },
  { value: "data_classification", label: "By data classification",
    desc: "Migrate similar classifications together. Required for federal multi-classification environments." },
  { value: "llm_decides", label: "Let AI decide",
    desc: "Have the LLM analyze your VM inventory and propose optimal grouping based on patterns it identifies." },
];

const RISK_OPTIONS = [
  { value: "low_first", label: "Build confidence (low risk first)",
    desc: "Migrate easy VMs first, gain experience, then tackle harder ones." },
  { value: "high_first", label: "Get hard ones over with (high risk first)",
    desc: "Tackle complex migrations first while team has fresh focus." },
  { value: "mixed", label: "Mixed per wave (balance)",
    desc: "Each wave includes a mix of easy and complex for steady progress." },
];

const PROD_OPTIONS = [
  { value: "non_prod_first", label: "All non-prod before any prod (recommended)" },
  { value: "mixed", label: "Mix prod and non-prod in same wave" },
  { value: "prod_dedicated_waves", label: "Production VMs need dedicated waves (no mixing)" },
];

const ATOMICITY_OPTIONS = [
  { value: "all_together", label: "All VMs of one application in same wave",
    desc: "Simpler coordination, more downtime per app." },
  { value: "can_split", label: "Application VMs can span multiple waves",
    desc: "More complex, but enables gradual cutover for redundancy." },
  { value: "per_app_choice", label: "Per-application choice (LLM analyzes)",
    desc: "AI looks at each application's architecture and decides." },
];

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


export default function PlanWizard() {
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [scopeMode, setScopeMode] = useState("all");
  const [vcenterId, setVcenterId] = useState("");
  const [environment, setEnvironment] = useState("");
  const [appHint, setAppHint] = useState("");
  const [primaryGrouping, setPrimaryGrouping] = useState("application");
  const [waveSize, setWaveSize] = useState("medium_10_20");
  const [waveSizeCustom, setWaveSizeCustom] = useState(15);
  const [risk, setRisk] = useState("mixed");
  const [prodHandling, setProdHandling] = useState("non_prod_first");
  const [atomicity, setAtomicity] = useState("all_together");
  const [freeform, setFreeform] = useState("");

  const [vcenters, setVcenters] = useState([]);
  const [vmCount, setVmCount] = useState(null);
  const [vmCountLoading, setVmCountLoading] = useState(false);

  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState(null);

  // Load vCenters once for the scope dropdown.
  useEffect(() => {
    fetchJSON("/api/sources/vcenters")
      .then(setVcenters)
      .catch(() => setVcenters([]));
  }, []);

  // Estimate matched VM count whenever scope changes. We hit the
  // VMs listing with the right filter and count rows — small enough
  // to be fine for inventories under a few thousand.
  useEffect(() => {
    setVmCountLoading(true);
    const params = new URLSearchParams();
    params.set("limit", "500");
    fetchJSON(`/api/vms?${params.toString()}`)
      .then((all) => {
        if (!Array.isArray(all)) return setVmCount(0);
        let filtered = all;
        if (scopeMode === "vcenter" && vcenterId) {
          filtered = all.filter((v) => String(v.source_vcenter_id) === String(vcenterId));
        } else if (scopeMode === "environment" && environment) {
          filtered = all.filter((v) => v.environment === environment);
        } else if (scopeMode === "app_hint" && appHint) {
          filtered = all.filter((v) => v.application_hint === appHint);
        }
        setVmCount(filtered.length);
      })
      .catch(() => setVmCount(null))
      .finally(() => setVmCountLoading(false));
  }, [scopeMode, vcenterId, environment, appHint]);

  const buildScope = () => {
    if (scopeMode === "vcenter" && vcenterId) {
      return { source_vcenter_id: parseInt(vcenterId, 10) };
    }
    if (scopeMode === "environment" && environment) {
      return { environment };
    }
    if (scopeMode === "app_hint" && appHint) {
      return { application_hint: appHint };
    }
    return {};
  };

  const canGenerate = name.trim().length > 0 && !generating;

  const submit = async (e) => {
    e?.preventDefault();
    if (!canGenerate) return;

    setGenerating(true);
    setProgress({ status: "running", current_step: "queued", progress_percent: 0 });
    try {
      const inline = {
        name: name.trim(),
        primary_grouping: primaryGrouping,
        wave_size_target: waveSize,
        wave_size_custom: waveSize === "custom" ? Number(waveSizeCustom) : null,
        risk_approach: risk,
        production_handling: prodHandling,
        application_atomicity: atomicity,
        freeform_constraints: freeform,
      };
      const spawn = await fetchJSON("/api/plans/generate", {
        method: "POST",
        body: { name: name.trim(), inline_strategy: inline, scope: buildScope() },
      });
      toast(`Generating plan… (estimated 3–5 minutes)`, { ...TOAST_OPTS, icon: "🤖" });

      // Poll every 3s until completed or failed. Generous timeout —
      // 5K-VM plans on Ollama llama3:8b can take 10+ minutes.
      const startedAt = Date.now();
      let final = null;
      while (final == null && Date.now() - startedAt < 30 * 60 * 1000) {
        await new Promise((r) => setTimeout(r, 3000));
        try {
          const status = await fetchJSON(`/api/plans/generate/${spawn.task_id}/status`);
          setProgress(status);
          if (status.status === "completed") {
            final = "completed";
            toast.success("Migration plan ready", TOAST_OPTS);
            navigate(`/plans/${status.plan_id}`);
          } else if (status.status === "failed") {
            final = "failed";
            toast.error(`Plan generation failed: ${status.error || "unknown error"}`, { ...TOAST_OPTS, duration: 10000 });
          }
        } catch { /* keep polling */ }
      }
      if (final == null) toast("Still running — check back in a moment", { ...TOAST_OPTS, icon: "⏱" });
    } catch (err) {
      toast.error(err.message || "Failed to start plan generation", TOAST_OPTS);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>New Migration Plan</div>
          <div style={{ fontSize: 13, color: "#aaaacc", marginTop: 4 }}>
            Capture your migration strategy → AI generates a wave plan with rationale.
          </div>
        </div>
        <Link to="/" style={btnSecondary}>← Back</Link>
      </header>

      <main style={{ maxWidth: 920, margin: "0 auto", padding: 32 }}>
        <form onSubmit={submit}>

          {/* Step 1: Name */}
          <Step n={1} title="Plan name" subtitle="Used in audit logs + the report PDF.">
            <input
              style={inputStyle}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. DHA Q3 2026 — East Coast"
              maxLength={255}
              autoFocus
              required
            />
          </Step>

          {/* Step 2: Scope */}
          <Step n={2} title="Which VMs are we planning for?" subtitle={
            vmCountLoading
              ? "Counting matching VMs…"
              : vmCount === null
                ? null
                : `${vmCount} VM${vmCount === 1 ? "" : "s"} selected${vmCount > 1000 ? " — large plans take 10–30 minutes" : ""}${vmCount > 5000 ? " (hierarchical planning coming in v0.5+)" : ""}`
          }>
            <Radio name="scope" value="all" checked={scopeMode === "all"} onChange={() => setScopeMode("all")}
                   label="All VMs in inventory" />
            <Radio name="scope" value="vcenter" checked={scopeMode === "vcenter"} onChange={() => setScopeMode("vcenter")}
                   label="Specific vCenter source" />
            {scopeMode === "vcenter" && (
              <select style={{ ...inputStyle, marginTop: 8 }} value={vcenterId} onChange={(e) => setVcenterId(e.target.value)}>
                <option value="">— select vCenter —</option>
                {vcenters.map((v) => (
                  <option key={v.id} value={v.id}>{v.name} ({v.vm_count} VMs)</option>
                ))}
              </select>
            )}
            <Radio name="scope" value="environment" checked={scopeMode === "environment"} onChange={() => setScopeMode("environment")}
                   label="Specific environment" />
            {scopeMode === "environment" && (
              <input style={{ ...inputStyle, marginTop: 8 }} value={environment}
                     onChange={(e) => setEnvironment(e.target.value)} placeholder="e.g. prod, dev, staging" />
            )}
            <Radio name="scope" value="app_hint" checked={scopeMode === "app_hint"} onChange={() => setScopeMode("app_hint")}
                   label="Specific application hint" />
            {scopeMode === "app_hint" && (
              <input style={{ ...inputStyle, marginTop: 8 }} value={appHint}
                     onChange={(e) => setAppHint(e.target.value)} placeholder="e.g. epic-emr, athena-billing" />
            )}
            {vmCount === 0 && (
              <div style={{ marginTop: 10, color: "#ff8888", fontSize: 13 }}>
                No VMs match this scope. Adjust the filter before continuing.
              </div>
            )}
          </Step>

          {/* Step 3: Primary grouping */}
          <Step n={3} title="Primary grouping" subtitle="What organizes VMs into waves?">
            {GROUPING_OPTIONS.map((opt) => (
              <Radio key={opt.value} name="grouping" value={opt.value}
                     checked={primaryGrouping === opt.value}
                     onChange={() => setPrimaryGrouping(opt.value)}
                     label={opt.label} desc={opt.desc} />
            ))}
          </Step>

          {/* Step 4: Wave sizing */}
          <Step n={4} title="Wave sizing" subtitle="How many VMs per wave?">
            {WAVE_SIZE_OPTIONS.map((opt) => (
              <Radio key={opt.value} name="wave-size" value={opt.value}
                     checked={waveSize === opt.value}
                     onChange={() => setWaveSize(opt.value)}
                     label={`${opt.label} — ${opt.count}`}
                     desc={opt.flavor} />
            ))}
            {waveSize === "custom" && (
              <input type="number" min="1" max="500"
                     style={{ ...inputStyle, marginTop: 10, maxWidth: 160 }}
                     value={waveSizeCustom}
                     onChange={(e) => setWaveSizeCustom(e.target.value)} />
            )}
            {vmCount !== null && vmCount > 0 && (
              <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 8 }}>
                Estimated waves: <code style={monoStyle}>{estimateWaveCount(vmCount, waveSize, waveSizeCustom)}</code>
              </div>
            )}
          </Step>

          {/* Step 5: Risk */}
          <Step n={5} title="Risk approach" subtitle="How does the team want to sequence complexity?">
            {RISK_OPTIONS.map((opt) => (
              <Radio key={opt.value} name="risk" value={opt.value}
                     checked={risk === opt.value} onChange={() => setRisk(opt.value)}
                     label={opt.label} desc={opt.desc} />
            ))}
          </Step>

          {/* Step 6: Production handling */}
          <Step n={6} title="Production handling" subtitle="How should prod and non-prod be sequenced?">
            {PROD_OPTIONS.map((opt) => (
              <Radio key={opt.value} name="prod" value={opt.value}
                     checked={prodHandling === opt.value}
                     onChange={() => setProdHandling(opt.value)}
                     label={opt.label} />
            ))}
          </Step>

          {/* Step 7: Application atomicity */}
          <Step n={7} title="Application atomicity" subtitle="Can applications span multiple waves?">
            {ATOMICITY_OPTIONS.map((opt) => (
              <Radio key={opt.value} name="atom" value={opt.value}
                     checked={atomicity === opt.value}
                     onChange={() => setAtomicity(opt.value)}
                     label={opt.label} desc={opt.desc} />
            ))}
          </Step>

          {/* Step 8: Freeform */}
          <Step n={8} title="Freeform constraints" subtitle="Anything else the AI should know — change windows, business cycles, hard rules.">
            <textarea
              style={{ ...inputStyle, minHeight: 140, fontFamily: "'Share Tech Mono', monospace", resize: "vertical" }}
              value={freeform}
              onChange={(e) => setFreeform(e.target.value)}
              placeholder={[
                "Examples (one per line):",
                "- No migrations during March",
                "- Hospital A maintenance window: weekends only",
                "- Sarah's team can only handle one wave per week",
                "- These two applications must NOT migrate in the same wave",
                "- Database tier needs 2-week soak before app tier migrates",
              ].join("\n")}
              maxLength={20000}
            />
          </Step>

          {/* Step 9: Review + Generate */}
          <Step n={9} title="Review and generate" subtitle="Confirm the strategy. Generation runs in the background — you can navigate away and come back.">
            {generating && progress && (
              <ProgressBar progress={progress} />
            )}
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 16 }}>
              <Link to="/" style={btnSecondary}>Cancel</Link>
              <button type="submit" disabled={!canGenerate || (vmCount === 0)}
                      style={{ ...btnPrimary, opacity: (!canGenerate || vmCount === 0) ? 0.5 : 1 }}>
                {generating ? "Generating…" : "Generate plan"}
              </button>
            </div>
          </Step>
        </form>
      </main>
    </Shell>
  );
}


function ProgressBar({ progress }) {
  const stepLabels = {
    queued: "Queued",
    aggregating_data: "Aggregating VM data",
    llm_reasoning: "AI reasoning over your strategy",
    parsing_response: "Parsing AI response",
    validating: "Validating plan integrity",
    persisting: "Saving plan",
    completed: "Done",
    failed: "Failed",
  };
  const step = stepLabels[progress.current_step] || progress.current_step;
  const pct = Math.max(0, Math.min(100, progress.progress_percent || 0));
  return (
    <div style={{
      padding: "14px 16px", border: "1px solid #4488ff55",
      background: "rgba(68,136,255,0.06)", marginBottom: 14,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 11, color: "#88aaff", letterSpacing: "0.08em",
                       fontWeight: 700, textTransform: "uppercase" }}>
          {step}
        </span>
        <span style={{ fontSize: 12, color: "#88aaff", fontFamily: "'Share Tech Mono', monospace" }}>{pct}%</span>
      </div>
      <div style={{ height: 4, background: "#0a0a18", border: "1px solid #1a1a2e" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: "#4488ff", transition: "width 0.4s ease" }} />
      </div>
    </div>
  );
}


function estimateWaveCount(vmCount, sizeKey, custom) {
  const sizes = {
    small_5_10: [5, 10],
    medium_10_20: [10, 20],
    large_20_50: [20, 50],
    custom: [Number(custom) || 15, Number(custom) || 15],
  };
  const [lo, hi] = sizes[sizeKey] || sizes.medium_10_20;
  const upper = Math.ceil(vmCount / lo);
  const lower = Math.ceil(vmCount / hi);
  return lower === upper ? `${lower}` : `${lower}–${upper}`;
}


function Radio({ name, value, checked, onChange, label, desc }) {
  return (
    <label style={{
      display: "flex", alignItems: "flex-start", gap: 10, padding: "8px 4px",
      cursor: "pointer", color: "#ccccee",
    }}>
      <input type="radio" name={name} value={value} checked={checked} onChange={onChange} style={{ marginTop: 4 }} />
      <span style={{ flex: 1 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>{label}</div>
        {desc && <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 2, lineHeight: 1.5 }}>{desc}</div>}
      </span>
    </label>
  );
}


function Step({ n, title, subtitle, children }) {
  return (
    <section style={{ marginBottom: 28, paddingBottom: 24, borderBottom: "1px solid #1a1a2e" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 8 }}>
        <span style={{
          fontSize: 11, color: "#88aaff", letterSpacing: "0.08em", fontWeight: 700,
          fontFamily: "'Share Tech Mono', monospace",
          border: "1px solid #4488ff66", padding: "3px 8px", textTransform: "uppercase",
        }}>step {n}</span>
        <span style={{ fontSize: 16, color: "#eeeeff", fontWeight: 700, fontFamily: "'Barlow', sans-serif" }}>
          {title}
        </span>
      </div>
      {subtitle && <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 12, lineHeight: 1.6 }}>{subtitle}</div>}
      {children}
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


// ---------- styles ----------
const headerStyle = {
  position: "sticky", top: 0, zIndex: 50,
  background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
  borderBottom: "1px solid #1a1a2e",
  display: "flex", alignItems: "center", justifyContent: "space-between",
  padding: "16px 32px", gap: 24,
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "10px 12px", fontFamily: "'Barlow', sans-serif", fontSize: 14,
  width: "100%", outline: "none",
};
const monoStyle = { fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" };
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
