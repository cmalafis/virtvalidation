import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";

const TOAST_OPTS = {
  style: {
    background: "#0a0a18",
    border: "1px solid #1a1a2e",
    color: "#ccccdd",
    fontFamily: "'Share Tech Mono', monospace",
    fontSize: 12,
    letterSpacing: "0.05em",
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error:   { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const SCHEDULE_PRESETS = [
  { value: "twice_daily", label: "Twice daily",  hint: "06:00 and 18:00 UTC — recommended for steady-state environments." },
  { value: "once_daily",  label: "Once daily",   hint: "06:00 UTC — lowest SSH overhead." },
  { value: "hourly",      label: "Hourly",       hint: "Every hour at :00 — best for active migration windows." },
];

async function fetchJSON(url, { signal, method = "GET", body } = {}) {
  const opts = { signal, method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail ?? ""; } catch { /* ignore */ }
    const err = new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

const Spinner = ({ size = 14, color = "#4488ff" }) => (
  <span aria-hidden="true" style={{
    display: "inline-block",
    width: size, height: size,
    border: `2px solid ${color}33`,
    borderTopColor: color,
    borderRadius: "50%",
    animation: "spin 0.8s linear infinite",
    verticalAlign: "middle",
  }}/>
);

const Shimmer = ({ width = "100%", height = 12 }) => (
  <div style={{
    height, width,
    background: "linear-gradient(90deg, #14142a 0%, #2a2a44 50%, #14142a 100%)",
    backgroundSize: "200% 100%",
    animation: "shimmer 1.4s ease-in-out infinite",
    borderRadius: 2,
  }}/>
);

const Section = ({ title, subtitle, children, action }) => (
  <div style={{
    border: "1px solid #1a1a2e", background: "#0a0a18",
    padding: 20, marginBottom: 16,
  }}>
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
      <div>
        <div style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 700, color: "#eeeeff", letterSpacing: "0.06em" }}>{title}</div>
        {subtitle && (
          <div style={{ fontSize: 10, color: "#9999bb", marginTop: 3, fontFamily: "'Barlow', sans-serif" }}>{subtitle}</div>
        )}
      </div>
      {action}
    </div>
    {children}
  </div>
);

const StatusDot = ({ status, latencyMs }) => {
  const color = status === "online" ? "#00ff88" : status === "offline" ? "#ff3355" : "#8888aa";
  const label = status === "online" ? "ONLINE" : status === "offline" ? "OFFLINE" : "CHECKING…";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      fontSize: 10, fontFamily: "'Share Tech Mono', monospace",
      color, letterSpacing: "0.12em", fontWeight: 700,
    }}>
      <span style={{
        width: 7, height: 7, borderRadius: "50%",
        background: color, boxShadow: `0 0 8px ${color}`,
        animation: status === undefined ? "pulse 1.2s infinite" : "none",
      }}/>
      {label}
      {status === "online" && typeof latencyMs === "number" && (
        <span style={{ color: "#8888aa", marginLeft: 4 }}>· {latencyMs}ms</span>
      )}
    </span>
  );
};

const SecondaryButton = ({ children, onClick, disabled, type = "button" }) => (
  <button type={type} onClick={onClick} disabled={disabled}
    style={{
      background: "transparent",
      border: `1px solid ${disabled ? "#222244" : "#2a2a44"}`,
      color: disabled ? "#6666aa" : "#8888aa",
      padding: "8px 14px", fontSize: 9,
      fontFamily: "'Share Tech Mono', monospace",
      letterSpacing: "0.15em", textTransform: "uppercase", fontWeight: 700,
      cursor: disabled ? "not-allowed" : "pointer",
    }}>
    {children}
  </button>
);

const PrimaryButton = ({ children, onClick, disabled, type = "button" }) => (
  <button type={type} onClick={onClick} disabled={disabled}
    style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      background: disabled ? "#1a1a2e" : "#1d3a8a",
      border: `1px solid ${disabled ? "#222244" : "#4488ff"}`,
      color: disabled ? "#6666aa" : "#dde4ff",
      padding: "9px 18px", fontSize: 10,
      fontFamily: "'Share Tech Mono', monospace",
      letterSpacing: "0.15em", textTransform: "uppercase", fontWeight: 700,
      cursor: disabled ? "not-allowed" : "pointer",
    }}>
    {children}
  </button>
);

// ---------- SSH key viewer ----------

function SSHKeyViewer() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchJSON("/api/system/ssh-public-key");
      setData(body);
    } catch (e) {
      setError(e.message || "Failed to load SSH key");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onCopy = async () => {
    if (!data?.public_key) return;
    try {
      await navigator.clipboard.writeText(data.public_key);
      toast.success("Public key copied to clipboard", TOAST_OPTS);
    } catch {
      toast.error("Clipboard access denied", TOAST_OPTS);
    }
  };

  return (
    <Section
      title="SSH PUBLIC KEY"
      subtitle="Add this to ~/.ssh/authorized_keys on every VM you enroll. The private key never leaves the appliance."
      action={data && (
        <SecondaryButton onClick={onCopy}>📋 Copy</SecondaryButton>
      )}
    >
      {loading ? (
        <Shimmer width="100%" height={42}/>
      ) : error ? (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div style={{ fontSize: 11, color: "#aa8888", fontFamily: "'Barlow', sans-serif", lineHeight: 1.5 }}>
            {error}
          </div>
          <SecondaryButton onClick={load}>↻ Retry</SecondaryButton>
        </div>
      ) : data && (
        <>
          <div style={{
            background: "#07070f", border: "1px solid #111122", padding: "12px 14px",
            fontSize: 11, fontFamily: "'Share Tech Mono', monospace",
            color: "#aaaacc", lineHeight: 1.6,
            wordBreak: "break-all", whiteSpace: "pre-wrap",
            maxHeight: 120, overflowY: "auto",
          }}>
            {data.public_key}
          </div>
          {data.fingerprint && (
            <div style={{ marginTop: 10, fontSize: 10, color: "#8888aa", fontFamily: "'Share Tech Mono', monospace" }}>
              <span style={{ color: "#6666aa", marginRight: 6 }}>FINGERPRINT</span>
              {data.fingerprint.slice(0, 23)}…
            </div>
          )}
        </>
      )}
    </Section>
  );
}

// ---------- Connection status section ----------

function ConnectionStatus() {
  const [pg, setPg] = useState(undefined);
  const [ollama, setOllama] = useState(undefined);
  const [refreshing, setRefreshing] = useState(false);

  const probe = useCallback(async () => {
    setRefreshing(true);
    const [pgRes, olRes] = await Promise.all([
      fetchJSON("/api/health/postgres").catch((e) => ({ status: "offline", error: e.message })),
      fetchJSON("/api/health/ollama").catch((e) => ({ status: "offline", error: e.message })),
    ]);
    setPg(pgRes);
    setOllama(olRes);
    setRefreshing(false);
  }, []);

  useEffect(() => { probe(); }, [probe]);

  const Row = ({ label, hostLine, info }) => (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "12px 14px", border: "1px solid #111122", background: "#07070f",
      marginBottom: 10,
    }}>
      <div>
        <div style={{ fontSize: 11, fontFamily: "'Barlow', sans-serif", fontWeight: 600, color: "#ccccee", letterSpacing: "0.05em" }}>{label}</div>
        {hostLine && (
          <div style={{ fontSize: 10, color: "#8888aa", fontFamily: "'Share Tech Mono', monospace", marginTop: 3 }}>{hostLine}</div>
        )}
      </div>
      {info}
    </div>
  );

  return (
    <Section
      title="CONNECTION STATUS"
      subtitle="Live status of the local Ollama LLM and PostgreSQL backends."
      action={<SecondaryButton onClick={probe} disabled={refreshing}>{refreshing ? <Spinner size={11}/> : "↻"} Refresh</SecondaryButton>}
    >
      <Row
        label="OLLAMA"
        hostLine={ollama?.host || "—"}
        info={
          <div style={{ textAlign: "right" }}>
            <StatusDot status={ollama?.status} latencyMs={ollama?.latency_ms}/>
            {ollama?.version && (
              <div style={{ fontSize: 10, color: "#8888aa", marginTop: 4, fontFamily: "'Share Tech Mono', monospace" }}>
                v{ollama.version}
              </div>
            )}
            {ollama?.status === "offline" && ollama?.error && (
              <div style={{ fontSize: 10, color: "#aa6666", marginTop: 4, maxWidth: 280, fontFamily: "'Barlow', sans-serif" }}>
                {ollama.error}
              </div>
            )}
          </div>
        }
      />
      <Row
        label="POSTGRESQL"
        hostLine={pg?.status === "online" ? "select 1 ✓" : pg?.error ? "" : "—"}
        info={
          <div style={{ textAlign: "right" }}>
            <StatusDot status={pg?.status} latencyMs={pg?.latency_ms}/>
            {pg?.status === "offline" && pg?.error && (
              <div style={{ fontSize: 10, color: "#aa6666", marginTop: 4, maxWidth: 280, fontFamily: "'Barlow', sans-serif" }}>
                {pg.error}
              </div>
            )}
          </div>
        }
      />
    </Section>
  );
}

// ---------- Main settings form ----------

function ConfigurationForm({ onSavedModelChange }) {
  const [draft, setDraft] = useState(null);
  const [original, setOriginal] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const loadSettings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchJSON("/api/settings");
      setDraft(body);
      setOriginal(body);
    } catch (e) {
      setError(e.message || "Failed to load settings");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModels = useCallback(async () => {
    setModelsError(null);
    try {
      const body = await fetchJSON("/api/system/ollama-models");
      setModels(body?.models || []);
    } catch (e) {
      setModelsError(e.message || "Failed to load models");
      setModels([]);
    }
  }, []);

  useEffect(() => { loadSettings(); }, [loadSettings]);
  useEffect(() => { loadModels(); }, [loadModels]);

  const dirty = useMemo(() => {
    if (!draft || !original) return false;
    return draft.ollama_model !== original.ollama_model
      || draft.schedule_preset !== original.schedule_preset;
  }, [draft, original]);

  const onSave = async () => {
    if (!draft || !dirty) return;
    setSaving(true);
    const promise = fetchJSON("/api/settings", {
      method: "PUT",
      body: {
        ollama_model: draft.ollama_model,
        schedule_preset: draft.schedule_preset,
      },
    });
    try {
      const body = await toast.promise(promise, {
        loading: "Saving settings…",
        success: "Settings saved",
        error: (e) => e.message || "Save failed",
      }, TOAST_OPTS);
      setOriginal(body);
      setDraft(body);
      onSavedModelChange?.(body.ollama_model);
    } catch { /* toast surfaced */ }
    finally { setSaving(false); }
  };

  // ---- Model dropdown options: union of available + currently-selected ----
  const modelOptions = useMemo(() => {
    const names = new Set(models.map((m) => m.name));
    if (draft?.ollama_model) names.add(draft.ollama_model);
    return Array.from(names).sort();
  }, [models, draft?.ollama_model]);

  return (
    <Section
      title="LLM MODEL"
      subtitle="The local Ollama model used for validation, planning, and report generation."
      action={modelsError ? null : (
        <SecondaryButton onClick={loadModels}>↻ Refresh</SecondaryButton>
      )}
    >
      {loading ? (
        <Shimmer width="100%" height={36}/>
      ) : error ? (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <div style={{ fontSize: 11, color: "#aa8888", fontFamily: "'Barlow', sans-serif" }}>{error}</div>
          <SecondaryButton onClick={loadSettings}>↻ Retry</SecondaryButton>
        </div>
      ) : draft && (
        <>
          <label style={{ display: "block", marginBottom: 14 }}>
            <span style={{
              display: "block", fontSize: 9, color: "#8888aa",
              letterSpacing: "0.15em", marginBottom: 6,
              fontFamily: "'Share Tech Mono', monospace",
            }}>OLLAMA MODEL</span>
            <select
              value={draft.ollama_model}
              onChange={(e) => setDraft({ ...draft, ollama_model: e.target.value })}
              disabled={saving}
              style={{
                background: "#07070f", border: "1px solid #1a1a2e",
                color: "#ccccdd", padding: "9px 11px",
                fontFamily: "'Share Tech Mono', monospace", fontSize: 12,
                width: "100%", outline: "none",
              }}
            >
              {modelOptions.length === 0 ? (
                <option value={draft.ollama_model}>{draft.ollama_model}</option>
              ) : (
                modelOptions.map((name) => {
                  const m = models.find((mm) => mm.name === name);
                  const sizeGB = m?.size ? (m.size / 1e9).toFixed(1) : null;
                  return (
                    <option key={name} value={name}>
                      {name}{sizeGB ? ` (${sizeGB} GB)` : ""}
                      {!m ? " · not pulled" : ""}
                    </option>
                  );
                })
              )}
            </select>
            {modelsError && (
              <span style={{ display: "block", marginTop: 6, fontSize: 10, color: "#aa8888", fontFamily: "'Barlow', sans-serif" }}>
                Couldn't list models from Ollama: {modelsError}. The currently-saved model still appears above.
              </span>
            )}
          </label>

          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            paddingTop: 14, borderTop: "1px solid #111122", marginTop: 8,
          }}>
            <span style={{ fontSize: 10, color: "#8888aa", fontFamily: "'Share Tech Mono', monospace", letterSpacing: "0.12em" }}>
              {dirty ? "UNSAVED CHANGES" : "SAVED"}
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              <SecondaryButton onClick={() => setDraft(original)} disabled={!dirty || saving}>Discard</SecondaryButton>
              <PrimaryButton onClick={onSave} disabled={!dirty || saving}>
                {saving && <Spinner size={11}/>}
                {saving ? "Saving…" : "Save"}
              </PrimaryButton>
            </div>
          </div>
        </>
      )}

      {/* Schedule preset is part of the same settings document — render as a
          second sub-section sharing the same dirty/save flow. */}
      {draft && (
        <div style={{ marginTop: 20, paddingTop: 18, borderTop: "1px solid #111122" }}>
          <div style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 700, color: "#eeeeff", letterSpacing: "0.06em", marginBottom: 4 }}>
            BASELINE COLLECTION SCHEDULE
          </div>
          <div style={{ fontSize: 10, color: "#9999bb", marginBottom: 14, fontFamily: "'Barlow', sans-serif" }}>
            How often the appliance SSHes into every enrolled VM and stores a fresh baseline snapshot. All times are UTC.
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {SCHEDULE_PRESETS.map((p) => {
              const checked = draft.schedule_preset === p.value;
              return (
                <label key={p.value} style={{
                  display: "flex", alignItems: "flex-start", gap: 12,
                  padding: "12px 14px",
                  border: `1px solid ${checked ? "#4488ff" : "#1a1a2e"}`,
                  background: checked ? "rgba(68,136,255,0.06)" : "#07070f",
                  cursor: "pointer",
                }}>
                  <input
                    type="radio" name="schedule_preset"
                    value={p.value} checked={checked}
                    onChange={() => setDraft({ ...draft, schedule_preset: p.value })}
                    disabled={saving}
                    style={{ marginTop: 4, accentColor: "#4488ff" }}
                  />
                  <div>
                    <div style={{ fontSize: 12, fontFamily: "'Barlow', sans-serif", fontWeight: 600, color: "#ccccee" }}>{p.label}</div>
                    <div style={{ fontSize: 10, color: "#8888aa", marginTop: 3, fontFamily: "'Barlow', sans-serif" }}>{p.hint}</div>
                  </div>
                </label>
              );
            })}
          </div>
        </div>
      )}
    </Section>
  );
}

// ---------- Page ----------

export default function Settings() {
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
        @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.4 } }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        @keyframes fadeIn { from { opacity:0; transform: translateY(8px); } to { opacity:1; transform: translateY(0); } }
        .fade-in { animation: fadeIn 0.25s ease forwards; }
        .scanline {
          position: fixed; top: 0; left: 0; right: 0; bottom: 0;
          background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
          pointer-events: none; z-index: 9999;
        }
      `}</style>

      <Toaster position="bottom-right" toastOptions={TOAST_OPTS}/>
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
                VIRTVALIDATE / SETTINGS
              </div>
              <div style={{ fontSize: 9, color: "#6666aa", letterSpacing: "0.2em" }}>SYSTEM CONFIGURATION</div>
            </div>
          </div>

          <Link to="/" style={{
            display: "inline-flex", alignItems: "center", gap: 8,
            background: "transparent", border: "1px solid #2a2a44",
            color: "#8888aa", textDecoration: "none",
            padding: "8px 16px", fontSize: 10,
            fontFamily: "'Share Tech Mono', monospace",
            letterSpacing: "0.15em", textTransform: "uppercase", fontWeight: 700,
          }}>
            ← Back to Dashboard
          </Link>
        </div>
      </div>

      <div style={{ maxWidth: 880, margin: "0 auto", padding: 24 }} className="fade-in">
        <SSHKeyViewer />
        <ConnectionStatus />
        <ConfigurationForm />
      </div>
    </div>
  );
}
