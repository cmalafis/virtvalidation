import { useCallback, useEffect, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link, useParams } from "react-router-dom";
import { throwForResponse } from "../utils/apiError";

// Strategy-driven plan detail page. Surfaces the LLM's rationale +
// per-wave reasoning prominently — that's the consultative output the
// federal customer paid for. Wave actions today: move-VM (creates a new
// revision). Split / merge / "why is this VM here" are deferred to
// follow-up LLM endpoints.

const TOAST_OPTS = {
  style: { background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
           fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5 },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const RISK_COLOR = { low: "#00ff88", medium: "#ffaa00", high: "#ff5577" };

async function fetchJSON(url, opts = {}) {
  const init = { method: "GET", ...opts };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (!r.ok) await throwForResponse(r);
  if (r.status === 204) return null;
  return r.json();
}


export default function PlanView() {
  const { id } = useParams();
  const planId = Number(id);

  const [plan, setPlan] = useState(null);
  const [chunks, setChunks] = useState([]);
  const [vmsById, setVmsById] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [planData, chunkList, vmList] = await Promise.all([
        fetchJSON(`/api/plans/${planId}`),
        fetchJSON(`/api/plans/${planId}/chunks`).catch(() => []),
        fetchJSON("/api/vms?limit=500").catch(() => []),
      ]);
      setPlan(planData);
      setChunks(Array.isArray(chunkList) ? chunkList : []);
      const map = {};
      (Array.isArray(vmList) ? vmList : []).forEach((v) => { map[v.id] = v; });
      setVmsById(map);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [planId]);

  useEffect(() => { load(); }, [load]);

  const onMoveVM = async (vmId, fromWave, targetWave) => {
    try {
      const updated = await fetchJSON(`/api/plans/${plan.id}/waves/${fromWave}/move-vm`, {
        method: "POST",
        body: { vm_id: vmId, target_wave_number: targetWave },
      });
      toast.success(`Moved to wave ${targetWave} — revision ${updated.revision_number}`, TOAST_OPTS);
      // The endpoint returns the new revision; navigate to it so the
      // operator stays on the latest revision after each move.
      window.location.assign(`/plans/${updated.id}`);
    } catch (e) {
      toast.error(e.message || "Move failed", TOAST_OPTS);
    }
  };

  if (loading) {
    return <Shell><div style={{ padding: 40, color: "#aaaacc" }}>Loading…</div></Shell>;
  }
  if (error) {
    return (
      <Shell>
        <div style={{ maxWidth: 720, margin: "60px auto", padding: 24, border: "1px solid #ff5577" }}>
          <div style={{ color: "#ff5577", fontSize: 11, fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 8 }}>
            Plan load failed
          </div>
          <div style={{ color: "#ccaaaa", fontSize: 14 }}>{error}</div>
          <Link to="/" style={{ ...btnSecondary, display: "inline-block", marginTop: 18 }}>← Back</Link>
        </div>
      </Shell>
    );
  }
  if (!plan) return null;

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Link to="/" style={btnSecondary}>← Inventory</Link>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700 }}>{plan.name || `Plan #${plan.id}`}</div>
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
              Plan #{plan.id} · revision {plan.revision_number}{plan.supersedes_plan_id ? ` · supersedes #${plan.supersedes_plan_id}` : ""} · model {plan.model}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/plans/new" style={btnPrimary}>+ New plan</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1100, margin: "0 auto", padding: 32 }}>
        {/* Plan summary + rationale — the consultative output */}
        {plan.plan_summary && (
          <Section title="Plan summary">
            <p style={{ fontSize: 15, color: "#eeeeff", lineHeight: 1.7, marginBottom: 12 }}>
              {plan.plan_summary}
            </p>
          </Section>
        )}
        {plan.rationale && (
          <Section title="Why this plan fits your strategy">
            <p style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.7, whiteSpace: "pre-wrap" }}>
              {plan.rationale}
            </p>
          </Section>
        )}

        {/* Plan-level warnings — yellow box, prominent */}
        {Array.isArray(plan.warnings) && plan.warnings.length > 0 && (
          <Section title="Plan warnings">
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {plan.warnings.map((w, i) => (
                <li key={i} style={{
                  color: "#ffe9aa", fontSize: 13, lineHeight: 1.6, marginBottom: 6,
                }}>
                  ⚠ {w}
                </li>
              ))}
            </ul>
          </Section>
        )}

        {/* Chunk navigation — only shown when the plan came from
            the hierarchical pipeline (legacy/single-shot plans have
            no chunks persisted). */}
        {chunks.length > 0 && (
          <Section title={`Chunk breakdown (${chunks.length})`}>
            <p style={{ fontSize: 13, color: "#aaaacc", marginBottom: 10 }}>
              The chunker partitioned this scope before the AI ran.
              Each chunk got its own focused planning call; cross-chunk
              ordering came from a final review pass.
            </p>
            {chunks.map((c) => (
              <ChunkCard
                key={c.chunk_id}
                chunk={c}
                waves={(plan.waves || []).filter(
                  (w) => w.chunk_id === c.chunk_id
                )}
              />
            ))}
          </Section>
        )}

        {/* Waves — the meat */}
        <Section title={`Waves (${(plan.waves || []).length})`}>
          {(plan.waves || []).map((wave) => (
            <WaveCard
              key={wave.wave_number}
              wave={wave}
              vmsById={vmsById}
              waveCount={(plan.waves || []).length}
              onMoveVM={(vmId, target) => onMoveVM(vmId, wave.wave_number, target)}
            />
          ))}
        </Section>

        {/* Next actions — checklist */}
        {Array.isArray(plan.next_actions) && plan.next_actions.length > 0 && (
          <Section title="Next actions">
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {plan.next_actions.map((a, i) => (
                <li key={i} style={{ color: "#ccccee", fontSize: 14, lineHeight: 1.7, marginBottom: 6 }}>
                  {a}
                </li>
              ))}
            </ul>
          </Section>
        )}
      </main>
    </Shell>
  );
}


function ChunkCard({ chunk, waves }) {
  const [open, setOpen] = useState(false);
  const isFoundation = chunk.sub_key?.is_foundation;
  const accent = isFoundation ? "#88aaff" : "#4488ff";
  return (
    <div style={{
      border: "1px solid #1a1a2e", background: "#0a0a18",
      borderLeft: `3px solid ${accent}`,
      marginBottom: 12, padding: "14px 18px", cursor: "pointer",
    }} onClick={() => setOpen((v) => !v)}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <span style={{ fontSize: 11, color: accent, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", fontFamily: "'Share Tech Mono', monospace", marginRight: 10 }}>
            {isFoundation ? "Foundation" : "Chunk"}
          </span>
          <span style={{ fontSize: 15, color: "#eeeeff", fontWeight: 600 }}>
            {chunk.label || "(no label)"}
          </span>
          <span style={{ fontSize: 12, color: "#aaaacc", marginLeft: 10 }}>
            {chunk.vm_ids.length} VMs · {chunk.wave_numbers.length} waves
          </span>
        </div>
        <span style={{ fontSize: 18, color: "#aaaacc" }}>{open ? "−" : "+"}</span>
      </div>
      <div style={{ fontSize: 12, color: "#888899", marginTop: 4 }}>
        {chunk.reason_for_chunk}
      </div>
      {open && (
        <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid #1a1a2e" }}>
          {chunk.chunk_rationale && (
            <p style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
              {chunk.chunk_rationale}
            </p>
          )}
          {(chunk.sequence_dependencies || []).length > 0 && (
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 8 }}>
              Depends on chunk{chunk.sequence_dependencies.length === 1 ? "" : "s"}:{" "}
              <span style={{ fontFamily: "'Share Tech Mono', monospace" }}>
                {chunk.sequence_dependencies.map((d) => d.slice(0, 8)).join(", ")}
              </span>
            </div>
          )}
          {chunk.wave_numbers.length > 0 && (
            <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 8 }}>
              Wave numbers in this chunk: {chunk.wave_numbers.join(", ")}
            </div>
          )}
          {waves.length > 0 && (
            <ul style={{ marginTop: 10, paddingLeft: 18, color: "#ccccee", fontSize: 12 }}>
              {waves.map((w) => (
                <li key={w.wave_number} style={{ marginBottom: 4 }}>
                  <strong>Wave {w.wave_number}</strong>: {w.name} · {w.vm_ids.length} VMs · risk {w.risk_level || w.estimated_risk}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}


function WaveCard({ wave, vmsById, waveCount, onMoveVM }) {
  const [moveOpen, setMoveOpen] = useState(null); // vm_id when picker is open
  const riskColor = RISK_COLOR[wave.risk_level || wave.estimated_risk] || "#aaaacc";
  return (
    <div style={{
      border: "1px solid #1a1a2e", background: "#0a0a18",
      borderLeft: `3px solid ${riskColor}`,
      marginBottom: 16, padding: "18px 22px",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 16, color: "#eeeeff", fontWeight: 700, fontFamily: "'Barlow', sans-serif" }}>
            {wave.name || `Wave ${wave.wave_number}`}
          </div>
          <div style={{ fontSize: 12, color: "#aaaacc", marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
            {(wave.vm_ids || []).length} VMs · risk {wave.risk_level || wave.estimated_risk || "?"} · {wave.estimated_duration || "duration TBD"}
          </div>
        </div>
        <span style={{
          fontSize: 11, color: riskColor, border: `1px solid ${riskColor}66`,
          padding: "3px 9px", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase",
          fontFamily: "'Share Tech Mono', monospace",
        }}>{wave.risk_level || wave.estimated_risk}</span>
      </div>

      {wave.rationale && (
        <div style={{
          padding: "12px 14px", background: "#07070f", border: "1px solid #1a1a2e",
          marginBottom: 12,
        }}>
          <div style={{ fontSize: 11, color: "#88aaff", letterSpacing: "0.08em", fontWeight: 700, textTransform: "uppercase", marginBottom: 6 }}>
            Why this wave
          </div>
          <div style={{ fontSize: 13, color: "#ccccee", lineHeight: 1.7 }}>{wave.rationale}</div>
        </div>
      )}

      {wave.considerations && (
        <div style={{ fontSize: 13, color: "#aaaacc", lineHeight: 1.6, marginBottom: 12 }}>
          <strong style={{ color: "#88aaff" }}>Considerations:</strong> {wave.considerations}
        </div>
      )}

      {Array.isArray(wave.applications_included) && wave.applications_included.length > 0 && (
        <div style={{ fontSize: 12, color: "#aaaacc", marginBottom: 12 }}>
          Applications: {wave.applications_included.map((a) => (
            <code key={a} style={{ ...monoBadge, marginRight: 6 }}>{a}</code>
          ))}
        </div>
      )}

      {wave.applications_split_warning && (
        <div style={{
          padding: "10px 12px", background: "rgba(255,170,0,0.06)",
          border: "1px solid #ffaa0066", color: "#ffe9aa",
          fontSize: 13, lineHeight: 1.6, marginBottom: 12,
        }}>
          ⚠ {wave.applications_split_warning}
        </div>
      )}

      {/* VM list with move-vm action per row */}
      <div style={{ marginTop: 8 }}>
        {(wave.vm_ids || []).map((vmId) => {
          const vm = vmsById[vmId];
          return (
            <div key={vmId} style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "8px 0", borderTop: "1px solid #0f0f1e", fontSize: 13,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
                <span style={{ ...monoBadge, opacity: 0.8 }}>{vmId}</span>
                <Link to={`/vms/${vmId}`} style={{ color: "#eeeeff", textDecoration: "none", fontWeight: 600 }}>
                  {vm?.name || `vm-${vmId}`}
                </Link>
                {vm?.role && <span style={{ color: "#aaaacc", fontSize: 12 }}>· {vm.role}</span>}
                {vm?.environment && <span style={{ color: "#aaaacc", fontSize: 12 }}>· {vm.environment}</span>}
              </div>
              {moveOpen === vmId ? (
                <MoveVMPicker
                  currentWave={wave.wave_number}
                  waveCount={waveCount}
                  onConfirm={(target) => { onMoveVM(vmId, target); setMoveOpen(null); }}
                  onCancel={() => setMoveOpen(null)}
                />
              ) : (
                <button onClick={() => setMoveOpen(vmId)} style={btnGhost}>
                  Move
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


function MoveVMPicker({ currentWave, waveCount, onConfirm, onCancel }) {
  // Show every other wave as a candidate; current wave is excluded.
  const candidates = [];
  for (let i = 1; i <= waveCount; i++) {
    if (i !== currentWave) candidates.push(i);
  }
  return (
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "#aaaacc" }}>→ Wave</span>
      {candidates.map((w) => (
        <button key={w} onClick={() => onConfirm(w)} style={{ ...btnGhost, padding: "4px 10px" }}>
          {w}
        </button>
      ))}
      <button onClick={onCancel} style={{ ...btnGhost, padding: "4px 10px", opacity: 0.6 }}>
        ✕
      </button>
    </div>
  );
}


function Section({ title, children }) {
  return (
    <section style={{ marginBottom: 28 }}>
      <h2 style={{
        fontSize: 13, color: "#aaaacc", letterSpacing: "0.08em",
        fontWeight: 700, textTransform: "uppercase",
        fontFamily: "'Barlow', sans-serif", marginBottom: 12,
      }}>{title}</h2>
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
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
  cursor: "pointer", textDecoration: "none",
};
const btnGhost = {
  background: "transparent", border: "1px solid #2a2a44", color: "#ccccee",
  padding: "6px 10px", fontFamily: "'Barlow', sans-serif", fontSize: 11,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};
const monoBadge = {
  fontFamily: "'Share Tech Mono', monospace", color: "#aaaacc",
  border: "1px solid #2a2a44", padding: "2px 7px", fontSize: 11,
};
