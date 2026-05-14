import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import { fetchJSON } from "../utils/fetchJSON";

// Multi-key SSH catalog UI for the wave-scoped baseline + validation flow.
// Coexists with the existing singleton "Appliance SSH Key" section above
// (see SSHKeyViewerWithFIPS in Settings.jsx); the wave flow uses these
// keys, the appliance still uses the singleton.
//
// Private key bytes NEVER appear here — the backend doesn't return them
// in any response, and there's no way for the UI to ask for them.

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

const ALGORITHMS = [
  { value: "ed25519", label: "Ed25519 (recommended; non-FIPS)" },
  { value: "rsa", label: "RSA 3072 (FIPS 186-5)" },
  { value: "ecdsa", label: "ECDSA P-384 (FIPS 186-5)" },
];

export default function ValidationKeysSection({ fipsMode = false }) {
  const [keys, setKeys] = useState([]);
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [lastCreated, setLastCreated] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [keysData, plansData] = await Promise.all([
        fetchJSON("/api/ssh-keys?limit=200"),
        fetchJSON("/api/plans?limit=200").catch(() => ({ items: [] })),
      ]);
      setKeys(Array.isArray(keysData?.items) ? keysData.items : []);
      // Plans endpoint returns a list directly in some legacy versions;
      // tolerate both shapes.
      const plansList = Array.isArray(plansData)
        ? plansData
        : Array.isArray(plansData?.items)
          ? plansData.items
          : [];
      setPlans(plansList);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const retire = async (keyId) => {
    try {
      await fetchJSON(`/api/ssh-keys/${keyId}/retire`, { method: "POST" });
      toast.success("Key retired", TOAST_OPTS);
      load();
    } catch (e) {
      toast.error(e.message || "Retire failed", TOAST_OPTS);
    }
  };

  return (
    <div
      style={{
        border: "1px solid #1a1a2e",
        background: "#0a0a18",
        padding: 24,
        marginBottom: 20,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: 18,
        }}
      >
        <div>
          <div
            style={{
              fontSize: 16,
              fontFamily: "'Barlow', sans-serif",
              fontWeight: 700,
              color: "#eeeeff",
              letterSpacing: "0.04em",
            }}
          >
            Validation Keys
          </div>
          <div
            style={{
              fontSize: 14,
              color: "#aaaacc",
              marginTop: 6,
              fontFamily: "'Barlow', sans-serif",
              lineHeight: 1.6,
              maxWidth: 720,
            }}
          >
            Named SSH keys used by the wave-scoped baseline + validation flow.
            Distinct from the singleton appliance key above. The private key
            stays on the VirtValidate appliance and is never shown or
            downloadable — copy the public key to your VMs, or download the
            setup playbook.
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          style={btnPrimary(false)}
        >
          Generate key
        </button>
      </div>

      {loading && <div style={{ color: "#aaaacc", fontSize: 13 }}>Loading keys…</div>}
      {error && (
        <div style={{ color: "#ff9999", fontSize: 13, marginBottom: 12 }}>
          {error}
        </div>
      )}
      {!loading && keys.length === 0 && !error && (
        <div
          style={{
            fontSize: 13,
            color: "#888899",
            padding: "20px 14px",
            border: "1px dashed #2a2a44",
            textAlign: "center",
          }}
        >
          No validation keys yet. Generate one to use with wave-scoped baseline
          captures and validations.
        </div>
      )}
      {keys.length > 0 && (
        <div style={{ borderTop: "1px solid #1a1a2e" }}>
          <div style={headerRow}>
            <span style={{ flex: 2 }}>Name</span>
            <span style={{ flex: 3 }}>Fingerprint</span>
            <span style={{ flex: 1 }}>Algorithm</span>
            <span style={{ flex: 1 }}>Status</span>
            <span style={{ flex: 1 }}>Scope</span>
            <span style={{ flex: 1, textAlign: "right" }}>Actions</span>
          </div>
          {keys.map((k) => (
            <KeyRow
              key={k.id}
              k={k}
              plans={plans}
              onRetire={() => retire(k.id)}
            />
          ))}
        </div>
      )}

      {lastCreated && (
        <div style={{ marginTop: 22 }}>
          <PostCreatePanel
            keyRow={lastCreated}
            onDismiss={() => setLastCreated(null)}
          />
        </div>
      )}

      <CreateKeyModal
        open={showCreate}
        fipsMode={fipsMode}
        plans={plans}
        onClose={() => setShowCreate(false)}
        onCreated={(row) => {
          setShowCreate(false);
          setLastCreated(row);
          load();
        }}
      />
    </div>
  );
}

function KeyRow({ k, plans, onRetire }) {
  const planLabel = (() => {
    if (k.plan_id == null) return "unscoped";
    const plan = plans.find((p) => p?.id === k.plan_id);
    return plan ? plan.name || `plan #${k.plan_id}` : `plan #${k.plan_id}`;
  })();
  const statusColor = k.status === "active" ? "#00ff88" : "#aaaacc";
  return (
    <div style={dataRow}>
      <span style={{ flex: 2, color: "#eeeeff" }}>{k.name}</span>
      <span
        style={{
          flex: 3,
          color: "#aaaacc",
          fontFamily: "'Share Tech Mono', monospace",
          fontSize: 12,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={k.fingerprint}
      >
        {k.fingerprint}
      </span>
      <span style={{ flex: 1, color: "#aaaacc", fontSize: 12 }}>{k.algorithm}</span>
      <span
        style={{
          flex: 1,
          color: statusColor,
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          fontFamily: "'Share Tech Mono', monospace",
        }}
      >
        {k.status}
      </span>
      <span style={{ flex: 1, color: "#aaaacc", fontSize: 12 }}>{planLabel}</span>
      <span style={{ flex: 1, textAlign: "right", display: "flex", justifyContent: "flex-end", gap: 6 }}>
        <a
          href={`/api/ssh-keys/${k.id}/public`}
          target="_blank"
          rel="noreferrer"
          style={btnGhost(false)}
        >
          Public
        </a>
        <a
          href={`/api/ssh-keys/${k.id}/playbook`}
          style={btnGhost(false)}
          download={`virtvalidate-key-${k.id}-setup.yml`}
        >
          Playbook
        </a>
        {k.status === "active" && (
          <button onClick={onRetire} style={btnGhost(false)}>
            Retire
          </button>
        )}
      </span>
    </div>
  );
}

function PostCreatePanel({ keyRow, onDismiss }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(keyRow.public_key);
      toast.success("Public key copied", TOAST_OPTS);
    } catch {
      toast.error("Clipboard access denied", TOAST_OPTS);
    }
  };
  return (
    <div
      style={{
        border: "1px solid #00ff8855",
        background: "rgba(0,255,136,0.05)",
        padding: 18,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 12,
        }}
      >
        <div
          style={{
            color: "#00ff88",
            fontSize: 12,
            fontWeight: 700,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
          }}
        >
          Key generated · id {keyRow.id} · {keyRow.fingerprint}
        </div>
        <button onClick={onDismiss} style={btnGhost(false)}>
          Dismiss
        </button>
      </div>
      <div
        style={{
          color: "#aaaacc",
          fontSize: 13,
          marginBottom: 12,
          lineHeight: 1.6,
        }}
      >
        Copy the public key below into your VMs, or download the Ansible
        playbook (which has the key already substituted in). The private key
        bytes stay on the appliance and are never downloadable.
      </div>
      <pre
        style={{
          background: "#07070f",
          padding: 12,
          border: "1px solid #1a1a2e",
          color: "#ccccee",
          fontSize: 12,
          fontFamily: "'Share Tech Mono', monospace",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          marginBottom: 12,
        }}
      >
        {keyRow.public_key}
      </pre>
      <div style={{ display: "flex", gap: 10 }}>
        <button onClick={copy} style={btnPrimary(false)}>
          Copy public key
        </button>
        <a
          href={`/api/ssh-keys/${keyRow.id}/playbook`}
          download={`virtvalidate-key-${keyRow.id}-setup.yml`}
          style={btnPrimary(false)}
        >
          Download setup playbook
        </a>
      </div>
    </div>
  );
}

function CreateKeyModal({ open, fipsMode, plans, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [algorithm, setAlgorithm] = useState("ed25519");
  const [planId, setPlanId] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setAlgorithm(fipsMode ? "rsa" : "ed25519");
      setPlanId("");
      setBusy(false);
    }
  }, [open, fipsMode]);

  if (!open) return null;

  const submit = async () => {
    if (!name.trim()) {
      toast.error("Name is required", TOAST_OPTS);
      return;
    }
    setBusy(true);
    try {
      const body = { name: name.trim(), algorithm };
      if (planId) body.plan_id = Number(planId);
      const r = await fetchJSON("/api/ssh-keys", { method: "POST", body });
      onCreated(r);
    } catch (e) {
      toast.error(e.message || "Generate failed", TOAST_OPTS);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        backdropFilter: "blur(2px)",
      }}
    >
      <div
        style={{
          background: "#0a0a18",
          border: "1px solid #4488ff",
          padding: 28,
          width: 560,
          maxWidth: "95vw",
        }}
      >
        <div
          style={{
            fontSize: 17,
            color: "#eeeeff",
            fontFamily: "'Barlow', sans-serif",
            fontWeight: 700,
            marginBottom: 14,
          }}
        >
          Generate validation key
        </div>
        <FieldLabel>Name</FieldLabel>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. wave-1-prod"
          style={inputStyle}
          autoFocus
        />
        <FieldLabel>Algorithm</FieldLabel>
        <select
          value={algorithm}
          onChange={(e) => setAlgorithm(e.target.value)}
          style={inputStyle}
        >
          {ALGORITHMS.map((a) => (
            <option
              key={a.value}
              value={a.value}
              disabled={fipsMode && a.value === "ed25519"}
            >
              {a.label}
              {fipsMode && a.value === "ed25519" ? " · not FIPS-approved" : ""}
            </option>
          ))}
        </select>
        <FieldLabel>Scope (optional)</FieldLabel>
        <select
          value={planId}
          onChange={(e) => setPlanId(e.target.value)}
          style={inputStyle}
        >
          <option value="">Unscoped — usable for any plan</option>
          {plans.map((p) => (
            <option key={p?.id} value={p?.id}>
              {p?.name || `plan #${p?.id}`}
            </option>
          ))}
        </select>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
          <button onClick={onClose} disabled={busy} style={btnSecondaryStyle(busy)}>
            Cancel
          </button>
          <button onClick={submit} disabled={busy} style={btnPrimary(busy)}>
            {busy ? "Generating…" : "Generate"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FieldLabel({ children }) {
  return (
    <div
      style={{
        fontSize: 11,
        color: "#aaaacc",
        letterSpacing: "0.08em",
        fontFamily: "'Barlow', sans-serif",
        textTransform: "uppercase",
        fontWeight: 700,
        marginTop: 12,
        marginBottom: 6,
      }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
const headerRow = {
  display: "flex",
  gap: 12,
  padding: "10px 12px",
  fontSize: 11,
  color: "#888899",
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  fontWeight: 700,
  fontFamily: "'Barlow', sans-serif",
  background: "#07070f",
};

const dataRow = {
  display: "flex",
  gap: 12,
  padding: "12px",
  fontSize: 13,
  alignItems: "center",
  borderTop: "1px solid #1a1a2e",
  fontFamily: "'Barlow', sans-serif",
};

const inputStyle = {
  width: "100%",
  background: "#07070f",
  border: "1px solid #2a2a44",
  color: "#eeeeff",
  padding: "10px 12px",
  fontSize: 13,
  fontFamily: "'Share Tech Mono', monospace",
  outline: "none",
};

function btnPrimary(disabled) {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    background: disabled ? "#14142a" : "#1d3a8a",
    border: `1px solid ${disabled ? "#2a2a44" : "#4488ff"}`,
    color: disabled ? "#888899" : "#eef2ff",
    padding: "10px 18px",
    fontSize: 12,
    fontFamily: "'Barlow', sans-serif",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
    textDecoration: "none",
  };
}

function btnGhost(disabled) {
  return {
    background: "transparent",
    border: `1px solid ${disabled ? "#2a2a44" : "#3a3a55"}`,
    color: disabled ? "#888899" : "#ccccee",
    padding: "6px 10px",
    fontSize: 11,
    fontFamily: "'Barlow', sans-serif",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
    textDecoration: "none",
  };
}

function btnSecondaryStyle(busy) {
  return {
    background: "transparent",
    border: "1px solid #3a3a55",
    color: "#aaaacc",
    padding: "10px 18px",
    fontSize: 12,
    fontFamily: "'Barlow', sans-serif",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    fontWeight: 700,
    cursor: busy ? "wait" : "pointer",
  };
}
