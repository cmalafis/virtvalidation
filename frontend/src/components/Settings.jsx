import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";
import { fetchJSON } from "../utils/fetchJSON";

const TOAST_OPTS = {
  style: {
    background: "#0a0a18",
    border: "1px solid #2a2a44",
    color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif",
    fontSize: 14,
    letterSpacing: "0",
    lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error:   { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};

const SCHEDULE_PRESETS = [
  { value: "twice_daily", label: "Twice daily",  hint: "06:00 and 18:00 UTC — recommended for steady-state environments." },
  { value: "once_daily",  label: "Once daily",   hint: "06:00 UTC — lowest SSH overhead." },
  { value: "hourly",      label: "Hourly",       hint: "Every hour at :00 — best for active migration windows." },
];

const HOST_KEY_POLICIES = [
  {
    value: "auto_accept",
    label: "Auto-accept new hosts (TOFU)",
    hint:
      "Trust on first use — VirtValidate auto-accepts a VM's host key the " +
      "first time it connects, persists it to /app/keys/known_hosts, and " +
      "verifies it on every subsequent connection. Recommended for most use cases.",
  },
  {
    value: "strict",
    label: "Strict — reject unknown hosts",
    hint:
      "Federal classified mode. Refuses to connect to any host whose key " +
      "isn't already in /app/keys/known_hosts. Requires distributing host " +
      "keys out-of-band (ssh-keyscan) before enrollment.",
  },
];


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

// Live "Next collection: in 3h 42m (06:00 UTC)" indicator. Pulled from the
// settings response (which queries the running APScheduler for next_run_at).
// Re-renders every 30s so the relative time stays accurate without a poll
// against the backend.
function NextRunIndicator({ nextRunAt }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  if (!nextRunAt) {
    return (
      <div style={{
        padding: "10px 14px", border: "1px solid #1a1a2e", background: "#07070f",
        fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
      }}>
        Scheduler is not running yet — next collection time will appear once the appliance backend boots.
      </div>
    );
  }

  const target = new Date(nextRunAt);
  const now = new Date();
  let deltaMs = target.getTime() - now.getTime();
  let prefix = "in";
  if (deltaMs < 0) { prefix = "due"; deltaMs = -deltaMs; }
  const totalMinutes = Math.round(deltaMs / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  const rel = hours > 0 ? `${hours}h ${mins}m` : `${mins}m`;
  const utc = target.toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });

  return (
    <div style={{
      padding: "12px 16px", border: "1px solid #4488ff66", background: "rgba(68,136,255,0.06)",
      display: "flex", alignItems: "center", gap: 10,
    }}>
      <span style={{ fontSize: 14, color: "#88aaff" }}>⟳</span>
      <span style={{
        fontSize: 14, color: "#eeeeff", fontFamily: "'Barlow', sans-serif",
      }}>
        Next collection {prefix === "due" ? "is due now" : `in ${rel}`} ·{" "}
        <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>
          {utc} UTC
        </span>
      </span>
    </div>
  );
}

const Section = ({ title, subtitle, children, action }) => (
  <div style={{
    border: "1px solid #1a1a2e", background: "#0a0a18",
    padding: 24, marginBottom: 20,
  }}>
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
      <div>
        <div style={{
          fontSize: 16, fontFamily: "'Barlow', sans-serif",
          fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em",
        }}>{title}</div>
        {subtitle && (
          <div style={{
            fontSize: 14, color: "#aaaacc", marginTop: 6,
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
            maxWidth: 720,
          }}>{subtitle}</div>
        )}
      </div>
      {action}
    </div>
    {children}
  </div>
);

const StatusDot = ({ status, latencyMs }) => {
  const color = status === "online" ? "#00ff88" : status === "offline" ? "#ff3355" : "#aaaacc";
  const label = status === "online" ? "Online" : status === "offline" ? "Offline" : "Checking…";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 10,
      fontSize: 13, fontFamily: "'Barlow', sans-serif",
      color, letterSpacing: "0.06em", fontWeight: 700,
      textTransform: "uppercase",
    }}>
      <span style={{
        width: 8, height: 8, borderRadius: "50%",
        background: color, boxShadow: `0 0 8px ${color}`,
        animation: status === undefined ? "pulse 1.2s infinite" : "none",
      }}/>
      {label}
      {status === "online" && typeof latencyMs === "number" && (
        <span style={{
          color: "#aaaacc", marginLeft: 4,
          fontFamily: "'Share Tech Mono', monospace", letterSpacing: 0,
          textTransform: "none", fontWeight: 400,
        }}>· {latencyMs}ms</span>
      )}
    </span>
  );
};

const SecondaryButton = ({ children, onClick, disabled, type = "button" }) => (
  <button type={type} onClick={onClick} disabled={disabled}
    style={{
      background: "transparent",
      border: `1px solid ${disabled ? "#2a2a44" : "#3a3a55"}`,
      color: disabled ? "#888899" : "#aaaacc",
      padding: "10px 16px", fontSize: 12,
      fontFamily: "'Barlow', sans-serif",
      letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
      cursor: disabled ? "not-allowed" : "pointer",
    }}>
    {children}
  </button>
);

const PrimaryButton = ({ children, onClick, disabled, type = "button" }) => (
  <button type={type} onClick={onClick} disabled={disabled}
    style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      background: disabled ? "#14142a" : "#1d3a8a",
      border: `1px solid ${disabled ? "#2a2a44" : "#4488ff"}`,
      color: disabled ? "#888899" : "#eef2ff",
      padding: "10px 20px", fontSize: 12,
      fontFamily: "'Barlow', sans-serif",
      letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
      cursor: disabled ? "not-allowed" : "pointer",
    }}>
    {children}
  </button>
);

// ---------- SSH key viewer / generator ----------

// Algorithm options surfaced in the generate-key dropdown. FIPS mode
// disables ed25519 inline (with a tooltip explanation) so federal
// customers don't have to know the compliance ruleset to pick the
// right option.
const SSH_ALGORITHMS = [
  {
    value: "ed25519",
    label: "Ed25519 (recommended for non-FIPS)",
    fipsApproved: false,
  },
  {
    value: "rsa",
    label: "RSA 3072 (FIPS 186-5)",
    fipsApproved: true,
  },
  {
    value: "ecdsa",
    label: "ECDSA P-384 (FIPS 186-5)",
    fipsApproved: true,
  },
];

function ConfirmModal({ open, title, body, confirmLabel, danger, busy, onClose, onConfirm }) {
  if (!open) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
    }}>
      <div style={{
        background: "#0a0a18",
        border: `1px solid ${danger ? "#ff5577" : "#4488ff"}`,
        padding: 28, width: 540, maxWidth: "95vw",
      }}>
        <div style={{
          fontSize: 17, color: "#eeeeff",
          fontFamily: "'Barlow', sans-serif", fontWeight: 700, marginBottom: 12,
        }}>{title}</div>
        <div style={{
          fontSize: 13, color: "#aaaacc", marginBottom: 18,
          fontFamily: "'Barlow', sans-serif", lineHeight: 1.6, whiteSpace: "pre-wrap",
        }}>{body}</div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" onClick={onClose} disabled={busy} style={{
            background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
            padding: "10px 18px", fontSize: 12,
            fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
            textTransform: "uppercase", fontWeight: 700,
            cursor: busy ? "wait" : "pointer",
          }}>Cancel</button>
          <button type="button" onClick={onConfirm} disabled={busy} style={{
            background: "transparent",
            border: `1px solid ${danger ? "#ff5577" : "#4488ff"}`,
            color: danger ? "#ff99aa" : "#aaccff",
            padding: "10px 18px", fontSize: 12,
            fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
            textTransform: "uppercase", fontWeight: 700,
            cursor: busy ? "wait" : "pointer",
          }}>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

// Enrollment instruction tabs — pre-populates each snippet with the
// actual generated public key so the operator can copy-paste straight
// into their automation framework of choice. Same key, four output
// formats; the tabs keep noise low for operators who only use one.
function EnrollmentInstructions({ publicKey }) {
  const [active, setActive] = useState("manual");
  if (!publicKey) return null;
  const keyBody = publicKey.split(" ").slice(1, 2)[0] || ""; // strip type + comment
  const keyType = publicKey.split(" ")[0] || "ssh-ed25519";

  const snippets = {
    manual:
      `echo '${publicKey}' | ssh user@vm.example.com 'cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'`,
    ansible:
      `- name: Add VirtValidate key to authorized_keys\n` +
      `  ansible.posix.authorized_key:\n` +
      `    user: "{{ ansible_user }}"\n` +
      `    state: present\n` +
      `    key: "${publicKey}"`,
    puppet:
      `ssh_authorized_key { 'virtvalidate-appliance':\n` +
      `  ensure => present,\n` +
      `  user   => 'root',\n` +
      `  type   => '${keyType}',\n` +
      `  key    => '${keyBody}',\n` +
      `}`,
    terraform:
      `resource "tls_authorized_key" "virtvalidate" {\n` +
      `  user      = "root"\n` +
      `  algorithm = "${keyType}"\n` +
      `  key       = "${keyBody}"\n` +
      `}`,
  };
  const tabs = [
    { id: "manual", label: "Manual" },
    { id: "ansible", label: "Ansible" },
    { id: "puppet", label: "Puppet" },
    { id: "terraform", label: "Terraform" },
  ];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippets[active]);
      toast.success("Snippet copied to clipboard", TOAST_OPTS);
    } catch {
      toast.error("Clipboard access denied", TOAST_OPTS);
    }
  };

  return (
    <div style={{ marginTop: 18 }}>
      <div style={{
        fontSize: 12, color: "#aaaacc", letterSpacing: "0.08em",
        fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
        fontWeight: 700, marginBottom: 10,
      }}>Add key to your VMs</div>
      <div style={{ display: "flex", gap: 4, marginBottom: 10 }}>
        {tabs.map((t) => (
          <button key={t.id} type="button" onClick={() => setActive(t.id)}
            style={{
              background: active === t.id ? "rgba(68,136,255,0.06)" : "transparent",
              border: `1px solid ${active === t.id ? "#4488ff" : "#1a1a2e"}`,
              color: active === t.id ? "#aaccff" : "#aaaacc",
              padding: "6px 14px", fontSize: 11,
              fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
              textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
            }}>{t.label}</button>
        ))}
        <div style={{ flex: 1 }}/>
        <button type="button" onClick={copy} style={{
          background: "transparent", border: "1px solid #3a3a55",
          color: "#aaaacc", padding: "6px 14px", fontSize: 11,
          fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
          textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
        }}>📋 Copy snippet</button>
      </div>
      <pre style={{
        background: "#07070f", border: "1px solid #1a1a2e", padding: "14px 16px",
        fontSize: 12, fontFamily: "'Share Tech Mono', monospace",
        color: "#ccccee", lineHeight: 1.6, margin: 0,
        whiteSpace: "pre-wrap", wordBreak: "break-all",
        maxHeight: 220, overflowY: "auto",
      }}>{snippets[active]}</pre>
    </div>
  );
}

// Wraps SSHKeyViewer with the FIPS status fetch so the algorithm
// dropdown's defaults + the disabled-state logic stay in lockstep
// with the rest of the Settings page (FIPSCompliancePanel renders
// the same underlying status separately).
function SSHKeyViewerWithFIPS() {
  const [fipsMode, setFipsMode] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetchJSON("/api/system/fips-status")
      .then((data) => { if (!cancelled) setFipsMode(Boolean(data?.effective)); })
      .catch(() => { /* SSHKeyViewer falls back to non-FIPS defaults */ });
    return () => { cancelled = true; };
  }, []);
  return <SSHKeyViewer fipsMode={fipsMode}/>;
}

function SSHKeyViewer({ fipsMode = false }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Used for both the missing-state algorithm chooser and the rotate
  // workflow's pre-fill. Defaults to RSA when FIPS_MODE is on so the
  // first-time-generate button matches the federal compliance default.
  const [algorithm, setAlgorithm] = useState(fipsMode ? "rsa" : "ed25519");
  const [generating, setGenerating] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [rotateOpen, setRotateOpen] = useState(false);
  const [showFingerprint, setShowFingerprint] = useState(false);

  // Re-sync the default if FIPS_MODE flips after first render (the
  // FIPS status fetch happens in parallel to the SSH key fetch).
  useEffect(() => {
    setAlgorithm(fipsMode ? "rsa" : "ed25519");
  }, [fipsMode]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchJSON("/api/system/ssh-key");
      setData(body);
    } catch (e) {
      setError(e?.message || "Failed to load SSH key");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const onGenerate = useCallback(async () => {
    setGenerating(true);
    try {
      const body = await fetchJSON("/api/system/ssh-key/generate", {
        method: "POST",
        body: { algorithm },
      });
      toast.success(`Generated ${body.algorithm.toUpperCase()} key — copy the public key below`, TOAST_OPTS);
      setConfirmOpen(false);
      await load();
      // Scroll the rendered key into view so the operator's next move
      // (copy) is one click away.
      setTimeout(() => {
        const el = document.getElementById("ssh-public-key-block");
        if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 100);
    } catch (e) {
      toast.error(e?.message || "Failed to generate SSH key", TOAST_OPTS);
    } finally {
      setGenerating(false);
    }
  }, [algorithm, load]);

  const onRotate = useCallback(async () => {
    setGenerating(true);
    try {
      const body = await fetchJSON("/api/system/ssh-key/rotate", {
        method: "POST",
        body: { algorithm },
      });
      toast.success(`Key rotated — new fingerprint ${body.fingerprint.slice(0, 23)}…`, TOAST_OPTS);
      setRotateOpen(false);
      await load();
    } catch (e) {
      toast.error(e?.message || "Failed to rotate SSH key", TOAST_OPTS);
    } finally {
      setGenerating(false);
    }
  }, [algorithm, load]);

  const onCopy = async () => {
    if (!data?.public_key) return;
    try {
      await navigator.clipboard.writeText(data.public_key);
      toast.success("Public key copied to clipboard", TOAST_OPTS);
    } catch {
      toast.error("Clipboard access denied", TOAST_OPTS);
    }
  };

  const onDownload = () => {
    if (!data?.public_key) return;
    const blob = new Blob([data.public_key + "\n"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "virtvalidate-appliance.pub";
    a.click();
    URL.revokeObjectURL(url);
  };

  const algoOptions = SSH_ALGORITHMS.map((opt) => ({
    ...opt,
    disabled: fipsMode && !opt.fipsApproved,
  }));

  return (
    <Section
      title="SSH Public Key"
      subtitle="VirtValidate uses this key to SSH into managed VMs for baseline capture and post-migration validation. The private key never leaves the appliance."
    >
      <ConfirmModal
        open={confirmOpen}
        title={`Generate a new ${algorithm.toUpperCase()} SSH key?`}
        body={`This is a one-time operation. The keypair will be written to the appliance's keys volume; the private key never leaves the pod.\n\nAfter generation, you'll need to add the public key to ~/.ssh/authorized_keys on every VM you plan to validate.`}
        confirmLabel="Generate"
        busy={generating}
        onClose={() => setConfirmOpen(false)}
        onConfirm={onGenerate}
      />
      <ConfirmModal
        open={rotateOpen}
        title="Rotate the SSH key?"
        danger
        body={
          "Rotating will:\n" +
          "  • Generate a new keypair\n" +
          "  • Back up the old key (recoverable from the keys volume)\n" +
          "  • Require updating authorized_keys on every managed VM\n\n" +
          "Existing baselines and validations are not affected, but future SSH connections will fail until the new key is rolled out.\n\n" +
          "Continue?"
        }
        confirmLabel="Rotate Key"
        busy={generating}
        onClose={() => setRotateOpen(false)}
        onConfirm={onRotate}
      />

      {fipsMode && (
        <div style={{
          padding: "10px 14px", marginBottom: 14,
          border: "1px solid #4488ff66", background: "rgba(68,136,255,0.04)",
          fontSize: 12, color: "#aaccff",
          fontFamily: "'Barlow', sans-serif",
        }}>
          FIPS_MODE active — only FIPS 186-5 algorithms (RSA, ECDSA) are
          available. Ed25519 is disabled.
        </div>
      )}

      {loading ? (
        <Shimmer width="100%" height={56}/>
      ) : error ? (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div style={{
            fontSize: 14, color: "#ccaaaa", fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
          }}>{error}</div>
          <SecondaryButton onClick={load}>↻ Retry</SecondaryButton>
        </div>
      ) : data?.status === "missing" ? (
        <div>
          <div style={{
            padding: "16px 18px", border: "1px dashed #3a3a55", background: "#07070f",
            fontSize: 14, color: "#ccccdd",
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.6, marginBottom: 16,
          }}>
            No SSH key has been generated yet. VirtValidate needs an SSH key to
            connect to your VMs for baseline capture and validation.
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <label style={{
              display: "flex", alignItems: "center", gap: 10, flex: "1 1 280px",
            }}>
              <span style={{
                fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                fontWeight: 700, minWidth: 72,
              }}>Algorithm</span>
              <select
                value={algorithm}
                onChange={(e) => setAlgorithm(e.target.value)}
                style={{
                  flex: 1, background: "#0a0a18",
                  border: "1px solid #1a1a2e", color: "#eeeeff",
                  padding: "8px 10px", fontSize: 13,
                  fontFamily: "'Share Tech Mono', monospace",
                }}>
                {algoOptions.map((opt) => (
                  <option key={opt.value} value={opt.value} disabled={opt.disabled}
                    title={opt.disabled ? "Not FIPS-approved" : ""}>
                    {opt.label}{opt.disabled ? " — disabled (FIPS)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => setConfirmOpen(true)}
              disabled={generating}
              style={{
                background: "transparent", border: "1px solid #4488ff",
                color: generating ? "#7788aa" : "#aaccff",
                padding: "10px 22px", fontSize: 13,
                fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: generating ? "wait" : "pointer",
              }}>
              {generating ? "Generating…" : "Generate SSH Key"}
            </button>
          </div>
          <div style={{
            marginTop: 12, fontSize: 12, color: "#7788aa",
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
          }}>
            Ed25519 is recommended for most deployments. For FIPS 140-3
            compliance, choose RSA 3072 or ECDSA P-384.
          </div>
        </div>
      ) : data?.status === "exists" ? (
        <>
          <div id="ssh-public-key-block" style={{
            background: "#07070f", border: "1px solid #1a1a2e", padding: "14px 16px",
            fontSize: 13, fontFamily: "'Share Tech Mono', monospace",
            color: "#eeeeff", lineHeight: 1.6,
            wordBreak: "break-all", whiteSpace: "pre-wrap",
            maxHeight: 140, overflowY: "auto",
          }}>{data.public_key}</div>

          <div style={{
            display: "flex", flexWrap: "wrap", gap: 10, marginTop: 14, alignItems: "center",
          }}>
            <SecondaryButton onClick={onCopy}>📋 Copy</SecondaryButton>
            <SecondaryButton onClick={onDownload}>⬇ Download .pub</SecondaryButton>
            <button type="button" onClick={() => setShowFingerprint((v) => !v)} style={{
              background: "transparent", border: "1px solid #3a3a55",
              color: "#aaaacc", padding: "8px 14px", fontSize: 11,
              fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
              textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
            }}>
              {showFingerprint ? "Hide" : "View"} Fingerprint
            </button>
            <span style={{ flex: 1 }}/>
            <button type="button" onClick={() => setRotateOpen(true)} style={{
              background: "transparent", border: "1px solid #ff5577",
              color: "#ff99aa", padding: "8px 14px", fontSize: 11,
              fontFamily: "'Barlow', sans-serif", letterSpacing: "0.06em",
              textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
            }}>↻ Rotate Key</button>
          </div>

          {showFingerprint && data.fingerprint && (
            <div style={{
              marginTop: 12, padding: "10px 14px",
              border: "1px solid #1a1a2e", background: "#07070f",
              fontSize: 12, color: "#ccccee",
              fontFamily: "'Share Tech Mono', monospace",
              wordBreak: "break-all",
            }}>
              <span style={{ color: "#aaaacc" }}>Fingerprint: </span>
              {data.fingerprint}
            </div>
          )}

          <div style={{
            marginTop: 16, padding: "10px 14px",
            border: "1px solid #2a2a44", background: "#0a0a18",
            fontSize: 12, color: "#aaaacc",
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
          }}>
            Algorithm <strong style={{ color: "#ccccee" }}>{(data.algorithm || "").toUpperCase()}</strong>
            {data.created_at && (
              <> · Generated {new Date(data.created_at).toLocaleString()}</>
            )}
          </div>

          <EnrollmentInstructions publicKey={data.public_key}/>
        </>
      ) : null}
    </Section>
  );
}

// ---------- Connection status section ----------

// Friendly labels for the backend types the API returns. Settings
// page renders these dynamically from /api/system/llm-info so the
// operator sees the actual configured backend — never a hard-coded
// "Ollama" string when the deployment is running KServe.
const BACKEND_LABELS = {
  ollama: "Ollama (local)",
  kserve: "KServe (RHOAI / OpenShift)",
  vllm: "vLLM (direct)",
  mock:  "Mock (dev / CI — see docs/MOCK_BACKEND.md)",
};

function ConnectionStatus() {
  const [pg, setPg] = useState(undefined);
  const [llm, setLlm] = useState(undefined);
  const [refreshing, setRefreshing] = useState(false);

  const probe = useCallback(async () => {
    setRefreshing(true);
    const [pgRes, llmRes] = await Promise.all([
      fetchJSON("/api/health/postgres").catch((e) => ({ status: "offline", error: e.message })),
      fetchJSON("/api/health/llm").catch((e) => ({ status: "offline", error: e.message })),
    ]);
    setPg(pgRes);
    setLlm(llmRes);
    setRefreshing(false);
  }, []);

  useEffect(() => { probe(); }, [probe]);

  const Row = ({ label, hostLine, info }) => (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "16px 18px", border: "1px solid #1a1a2e", background: "#07070f",
      marginBottom: 12,
    }}>
      <div>
        <div style={{
          fontSize: 14, fontFamily: "'Barlow', sans-serif",
          fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em",
        }}>{label}</div>
        {hostLine && (
          <div style={{
            fontSize: 13, color: "#aaaacc", marginTop: 4,
            fontFamily: "'Share Tech Mono', monospace",
            wordBreak: "break-all",
          }}>{hostLine}</div>
        )}
      </div>
      {info}
    </div>
  );

  // Backend label: prefer the friendly name; fall back to whatever the API
  // returned so brand-new backend types still render something sensible.
  const backendLabel = llm?.backend ? (BACKEND_LABELS[llm.backend] || llm.backend) : "LLM";
  const llmError = llm?.details?.error || llm?.error;

  return (
    <Section
      title="Connection Status"
      subtitle="Live status of the configured LLM backend and PostgreSQL."
      action={<SecondaryButton onClick={probe} disabled={refreshing}>{refreshing ? <Spinner size={12}/> : "↻"} Refresh</SecondaryButton>}
    >
      <Row
        label={backendLabel}
        hostLine={llm?.endpoint || "—"}
        info={
          <div style={{ textAlign: "right" }}>
            <StatusDot status={llm?.status} latencyMs={llm?.latency_ms >= 0 ? llm.latency_ms : undefined}/>
            {llm?.model && (
              <div style={{
                fontSize: 13, color: "#ccccee", marginTop: 6,
                fontFamily: "'Share Tech Mono', monospace",
              }}>
                {llm.model}
              </div>
            )}
            {llm?.status === "offline" && llmError && (
              <div style={{
                fontSize: 13, color: "#ccaaaa", marginTop: 6, maxWidth: 320,
                fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
              }}>
                {llmError}
              </div>
            )}
          </div>
        }
      />
      <Row
        label="PostgreSQL"
        hostLine={pg?.status === "online" ? "select 1 ✓" : pg?.error ? "" : "—"}
        info={
          <div style={{ textAlign: "right" }}>
            <StatusDot status={pg?.status} latencyMs={pg?.latency_ms}/>
            {pg?.status === "offline" && pg?.error && (
              <div style={{
                fontSize: 13, color: "#ccaaaa", marginTop: 6, maxWidth: 320,
                fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
              }}>
                {pg.error}
              </div>
            )}
          </div>
        }
      />
    </Section>
  );
}


// Read-only FIPS 140-3 compliance panel. Federal customers (DoD,
// civilian agencies, FedRAMP) need to attest that the appliance
// operates inside a FIPS boundary; this surface lets them see the
// posture without shelling into the host. The panel is read-only —
// FIPS_MODE is a deployment env var, not a runtime toggle.
function FIPSCompliancePanel() {
  const [info, setInfo] = useState(undefined);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchJSON("/api/system/fips-status");
      setInfo(body);
    } catch (e) {
      setError(e.message || "Failed to load FIPS status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return (
      <Section title="FIPS 140-3 Compliance" subtitle="Read-only — controlled at deployment time via the FIPS_MODE env var.">
        <Shimmer width="100%" height={64}/>
      </Section>
    );
  }
  if (error || !info) {
    return (
      <Section title="FIPS 140-3 Compliance" subtitle="Read-only — controlled at deployment time via the FIPS_MODE env var.">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div style={{ fontSize: 14, color: "#ccaaaa", fontFamily: "'Barlow', sans-serif", lineHeight: 1.5 }}>
            {error || "No FIPS status available."}
          </div>
          <SecondaryButton onClick={load}>↻ Retry</SecondaryButton>
        </div>
      </Section>
    );
  }

  // Headline status: green only when both configured AND detected line up.
  // Mismatches surface in amber so the operator can't miss them.
  const headline = info.effective
    ? { color: "#00ff88", label: "ACTIVE" }
    : info.configured || info.detected
    ? { color: "#ffaa00", label: "MISMATCH" }
    : { color: "#aaaacc", label: "NOT CONFIGURED" };

  const Pill = ({ ok, label, color }) => (
    <span style={{
      fontSize: 11, fontFamily: "'Share Tech Mono', monospace",
      letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
      color: color || (ok ? "#00ff88" : "#ff5577"),
      border: `1px solid ${(color || (ok ? "#00ff88" : "#ff5577"))}66`,
      padding: "3px 9px",
    }}>{label}</span>
  );

  return (
    <Section
      title="FIPS 140-3 Compliance"
      subtitle="Read-only — controlled at deployment time via the FIPS_MODE env var."
      action={<SecondaryButton onClick={load}>↻ Refresh</SecondaryButton>}
    >
      <div style={{
        padding: "16px 18px", border: "1px solid #1a1a2e", background: "#07070f", marginBottom: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 16 }}>
          <div style={{
            fontSize: 15, color: "#eeeeff", fontWeight: 700,
            fontFamily: "'Barlow', sans-serif", letterSpacing: "0.04em",
          }}>Compliance Posture</div>
          <Pill color={headline.color} label={headline.label}/>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: info.mismatch_warning ? 14 : 0 }}>
          <div>
            <div style={{
              fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
              fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
              marginBottom: 6,
            }}>Configured (FIPS_MODE)</div>
            <Pill ok={info.configured} label={info.configured ? "TRUE" : "FALSE"}/>
          </div>
          <div>
            <div style={{
              fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
              fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
              marginBottom: 6,
            }}>Detected (host OS)</div>
            <Pill ok={info.detected} label={info.detected ? "TRUE" : "FALSE"}/>
          </div>
        </div>
        {info.mismatch_warning && (
          <div style={{
            marginTop: 4, padding: "12px 14px", background: "#0a0a18",
            border: "1px solid #ffaa0066", color: "#ffe9aa",
            fontSize: 13, lineHeight: 1.6, fontFamily: "'Barlow', sans-serif",
          }}>
            <strong style={{ color: "#ffaa00", letterSpacing: "0.04em" }}>⚠ Mismatch:</strong>{" "}
            {info.mismatch_warning}
          </div>
        )}
      </div>

      {Array.isArray(info.operations) && info.operations.length > 0 && (
        <div style={{ border: "1px solid #1a1a2e", background: "#07070f" }}>
          <div style={{
            display: "grid", gridTemplateColumns: "2fr 1.5fr 0.8fr 0.8fr",
            padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
          }}>
            {["Operation", "Configured", "FIPS-Approved", "Enforced"].map((h) => (
              <span key={h} style={{
                fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                fontWeight: 700, textTransform: "uppercase",
                fontFamily: "'Barlow', sans-serif",
              }}>{h}</span>
            ))}
          </div>
          {info.operations.map((op, i) => (
            <div key={i} style={{
              display: "grid", gridTemplateColumns: "2fr 1.5fr 0.8fr 0.8fr",
              padding: "12px 14px", borderBottom: "1px solid #0f0f1e",
              alignItems: "center",
              fontSize: 13, color: "#ccccee",
            }}>
              <span style={{ fontFamily: "'Barlow', sans-serif", fontWeight: 600 }}>
                {op.name}
              </span>
              <span style={{
                fontFamily: "'Share Tech Mono', monospace",
                color: "#aaaacc", wordBreak: "break-all",
              }}>{op.configured}</span>
              <Pill ok={op.fips_approved} label={op.fips_approved ? "YES" : "NO"}/>
              <Pill ok={op.enforced} label={op.enforced ? "YES" : "NO"}/>
            </div>
          ))}
        </div>
      )}

      <div style={{
        fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
        lineHeight: 1.6, marginTop: 14,
      }}>
        Set <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>FIPS_MODE=true</code> in
        the deployment environment AND boot the host with FIPS enabled
        (RHEL: <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>fips-mode-setup --enable</code>;
        RHCOS: <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>fips: true</code> in
        install-config). See <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>docs/FIPS_DEPLOYMENT.md</code>.
      </div>
    </Section>
  );
}


// Read-only backend identification panel. Surfaces the configured
// backend type, model, endpoint, and live status so operators can see
// what their deployment is wired up against. Switching backends is a
// deployment decision (env var) — there is no edit affordance here.
function LLMBackendPanel() {
  const [info, setInfo] = useState(undefined);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const body = await fetchJSON("/api/system/llm-info");
      setInfo(body);
    } catch (e) {
      setError(e.message || "Failed to load LLM backend info");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return (
      <Section title="LLM Backend" subtitle="Read-only — configured at deployment time via environment variables.">
        <Shimmer width="100%" height={64}/>
      </Section>
    );
  }
  if (error || !info) {
    return (
      <Section title="LLM Backend" subtitle="Read-only — configured at deployment time via environment variables.">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div style={{ fontSize: 14, color: "#ccaaaa", fontFamily: "'Barlow', sans-serif", lineHeight: 1.5 }}>
            {error || "No backend info available."}
          </div>
          <SecondaryButton onClick={load}>↻ Retry</SecondaryButton>
        </div>
      </Section>
    );
  }

  const cfg = info.config || {};
  const health = info.health || {};
  const backendLabel = BACKEND_LABELS[cfg.backend] || cfg.backend || "—";
  const detailsErr = health.details?.error;

  const KV = ({ label, value, mono = true }) => (
    <div style={{ marginBottom: 10 }}>
      <div style={{
        fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
        fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
        marginBottom: 4,
      }}>{label}</div>
      <div style={{
        fontSize: 14, color: "#ccccee",
        fontFamily: mono ? "'Share Tech Mono', monospace" : "'Barlow', sans-serif",
        wordBreak: "break-all",
      }}>{value || "—"}</div>
    </div>
  );

  return (
    <Section
      title="LLM Backend"
      subtitle="Read-only — configured at deployment time via environment variables."
      action={<SecondaryButton onClick={load}>↻ Refresh</SecondaryButton>}
    >
      <div style={{
        padding: "16px 18px", border: "1px solid #1a1a2e", background: "#07070f", marginBottom: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 14 }}>
          <div style={{
            fontSize: 15, color: "#eeeeff", fontWeight: 700,
            fontFamily: "'Barlow', sans-serif", letterSpacing: "0.04em",
          }}>{backendLabel}</div>
          <StatusDot status={health.status} latencyMs={health.latency_ms >= 0 ? health.latency_ms : undefined}/>
        </div>
        <KV label="Model" value={cfg.model}/>
        <KV label="Endpoint" value={cfg.endpoint}/>
        {Array.isArray(health.details?.available_models) && health.details.available_models.length > 0 && (
          <KV
            label="Available Models"
            value={health.details.available_models.join(", ")}
          />
        )}
        {health.status === "offline" && detailsErr && (
          <div style={{
            marginTop: 4, padding: "10px 12px", background: "#0a0a18",
            border: "1px solid #ff557755", color: "#ccaaaa",
            fontSize: 13, lineHeight: 1.5, fontFamily: "'Barlow', sans-serif",
          }}>
            {detailsErr}
          </div>
        )}
      </div>
      <div style={{
        fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
        lineHeight: 1.6, marginTop: 4,
      }}>
        Backend selection is set by <code style={{
          fontFamily: "'Share Tech Mono', monospace", color: "#ccccee",
        }}>LLM_BACKEND_TYPE</code> in the deployment&apos;s environment.
        See <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>docs/CONFIGURATION.md</code> for
        the full list of supported backends and their required variables.
      </div>
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
      || draft.schedule_preset !== original.schedule_preset
      || draft.ssh_host_key_policy !== original.ssh_host_key_policy;
  }, [draft, original]);

  const onSave = async () => {
    if (!draft || !dirty) return;
    setSaving(true);
    const promise = fetchJSON("/api/settings", {
      method: "PUT",
      body: {
        ollama_model: draft.ollama_model,
        schedule_preset: draft.schedule_preset,
        ssh_host_key_policy: draft.ssh_host_key_policy,
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
      title="LLM Model"
      subtitle="The configured LLM model used for validation, planning, and report generation. The backend type (Ollama / KServe / vLLM / Mock) is set via LLM_BACKEND_TYPE at deployment time and shown in the Connection Status panel above."
      action={modelsError ? null : (
        <SecondaryButton onClick={loadModels}>↻ Refresh</SecondaryButton>
      )}
    >
      {loading ? (
        <Shimmer width="100%" height={48}/>
      ) : error ? (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
          <div style={{
            fontSize: 14, color: "#ccaaaa", fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
          }}>{error}</div>
          <SecondaryButton onClick={loadSettings}>↻ Retry</SecondaryButton>
        </div>
      ) : draft && (
        <>
          <label style={{ display: "block", marginBottom: 18 }}>
            <span style={{
              display: "block", fontSize: 11, color: "#aaaacc",
              letterSpacing: "0.08em", marginBottom: 8,
              fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
            }}>LLM Model</span>
            <select
              value={draft.ollama_model}
              onChange={(e) => setDraft({ ...draft, ollama_model: e.target.value })}
              disabled={saving}
              style={{
                background: "#07070f", border: "1px solid #2a2a44",
                color: "#eeeeff", padding: "11px 13px",
                fontFamily: "'Share Tech Mono', monospace", fontSize: 14,
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
              <span style={{
                display: "block", marginTop: 8, fontSize: 13, color: "#ccaaaa",
                fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
              }}>
                Couldn&apos;t list models from the configured LLM backend: {modelsError}. The currently-saved model still appears above.
              </span>
            )}
          </label>

          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            paddingTop: 18, borderTop: "1px solid #1a1a2e", marginTop: 10,
          }}>
            <span style={{
              fontSize: 12, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
              letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
            }}>
              {dirty ? "Unsaved changes" : "Saved"}
            </span>
            <div style={{ display: "flex", gap: 10 }}>
              <SecondaryButton onClick={() => setDraft(original)} disabled={!dirty || saving}>Discard</SecondaryButton>
              <PrimaryButton onClick={onSave} disabled={!dirty || saving}>
                {saving && <Spinner size={12}/>}
                {saving ? "Saving…" : "Save"}
              </PrimaryButton>
            </div>
          </div>
        </>
      )}

      {/* Schedule preset is part of the same settings document — render as a
          second sub-section sharing the same dirty/save flow. */}
      {draft && (
        <div style={{ marginTop: 28, paddingTop: 22, borderTop: "1px solid #1a1a2e" }}>
          <div style={{
            fontSize: 16, fontFamily: "'Barlow', sans-serif",
            fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em", marginBottom: 6,
          }}>
            Baseline Collection Schedule
          </div>
          <div style={{
            fontSize: 14, color: "#aaaacc", marginBottom: 14,
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
          }}>
            How often the appliance SSHes into every enrolled VM and stores a fresh baseline snapshot. All times are UTC.
          </div>
          <NextRunIndicator nextRunAt={original?.next_run_at} />
          <div style={{ height: 14 }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {SCHEDULE_PRESETS.map((p) => {
              const checked = draft.schedule_preset === p.value;
              return (
                <label key={p.value} style={{
                  display: "flex", alignItems: "flex-start", gap: 14,
                  padding: "16px 18px",
                  border: `1px solid ${checked ? "#4488ff" : "#1a1a2e"}`,
                  background: checked ? "rgba(68,136,255,0.08)" : "#07070f",
                  cursor: "pointer",
                }}>
                  <input
                    type="radio" name="schedule_preset"
                    value={p.value} checked={checked}
                    onChange={() => setDraft({ ...draft, schedule_preset: p.value })}
                    disabled={saving}
                    style={{ marginTop: 5, accentColor: "#4488ff", width: 16, height: 16 }}
                  />
                  <div>
                    <div style={{
                      fontSize: 15, fontFamily: "'Barlow', sans-serif",
                      fontWeight: 600, color: "#eeeeff",
                    }}>{p.label}</div>
                    <div style={{
                      fontSize: 13, color: "#aaaacc", marginTop: 4,
                      fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
                    }}>{p.hint}</div>
                  </div>
                </label>
              );
            })}
          </div>

          {/* ----- SSH host key verification ----- */}
          <div style={{ marginTop: 28, paddingTop: 22, borderTop: "1px solid #1a1a2e" }}>
            <div style={{
              fontSize: 16, fontFamily: "'Barlow', sans-serif",
              fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em", marginBottom: 6,
            }}>
              SSH Host Key Verification
            </div>
            <div style={{
              fontSize: 14, color: "#aaaacc", marginBottom: 18,
              fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
            }}>
              Controls how the SSH collector handles unknown host keys when it
              connects to a VM. Keys are persisted to{" "}
              <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>
                /app/keys/known_hosts
              </code>
              {" "}so verification works across appliance restarts.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {HOST_KEY_POLICIES.map((p) => {
                const checked = draft.ssh_host_key_policy === p.value;
                return (
                  <label key={p.value} style={{
                    display: "flex", alignItems: "flex-start", gap: 14,
                    padding: "16px 18px",
                    border: `1px solid ${checked ? "#4488ff" : "#1a1a2e"}`,
                    background: checked ? "rgba(68,136,255,0.08)" : "#07070f",
                    cursor: "pointer",
                  }}>
                    <input
                      type="radio" name="ssh_host_key_policy"
                      value={p.value} checked={checked}
                      onChange={() => setDraft({ ...draft, ssh_host_key_policy: p.value })}
                      disabled={saving}
                      style={{ marginTop: 5, accentColor: "#4488ff", width: 16, height: 16 }}
                    />
                    <div>
                      <div style={{
                        fontSize: 15, fontFamily: "'Barlow', sans-serif",
                        fontWeight: 600, color: "#eeeeff",
                      }}>{p.label}</div>
                      <div style={{
                        fontSize: 13, color: "#aaaacc", marginTop: 4,
                        fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
                      }}>{p.hint}</div>
                    </div>
                  </label>
                );
              })}
            </div>
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
      fontFamily: "'Barlow', sans-serif",
      color: "#eeeeff",
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
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", height: 64 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
            <div style={{
              width: 32, height: 32, border: "1px solid #4488ff44",
              display: "flex", alignItems: "center", justifyContent: "center",
              position: "relative",
            }}>
              <div style={{ width: 11, height: 11, background: "#4488ff", clipPath: "polygon(50% 0%, 100% 100%, 0% 100%)" }} />
              <div style={{ position: "absolute", inset: -3, border: "1px solid #4488ff22" }} />
            </div>
            <div>
              <div style={{
                fontSize: 18, fontFamily: "'Barlow', sans-serif",
                fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em",
              }}>
                VirtValidate / Settings
              </div>
              <div style={{
                fontSize: 11, color: "#aaaacc", letterSpacing: "0.1em",
                fontFamily: "'Barlow', sans-serif", marginTop: 2,
              }}>System Configuration</div>
            </div>
          </div>

          <Link to="/" style={{
            display: "inline-flex", alignItems: "center", gap: 8,
            background: "transparent", border: "1px solid #3a3a55",
            color: "#aaaacc", textDecoration: "none",
            padding: "10px 18px", fontSize: 12,
            fontFamily: "'Barlow', sans-serif",
            letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          }}>
            ← Back to Dashboard
          </Link>
        </div>
      </div>

      <div style={{ maxWidth: 960, margin: "0 auto", padding: 32 }} className="fade-in">
        <SSHKeyViewerWithFIPS />
        <ConnectionStatus />
        <FIPSCompliancePanel />
        <LLMBackendPanel />
        <ConfigurationForm />
      </div>
    </div>
  );
}
