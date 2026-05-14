import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { fetchJSON } from "../utils/fetchJSON";

// Wave-scoped baseline + validation actions, embedded inside a WaveCard.
//
// Three operator actions:
//   - Capture Baseline → POST /api/plans/{id}/waves/{n}/baseline
//   - Validate         → POST /api/plans/{id}/waves/{n}/validate
//                         (only after a completed baseline exists)
//   - Revoke Key       → POST /api/plans/{id}/waves/{n}/revoke-validation-key
//                         (only after a clean validation; force toggle for bail-out)
//
// All run state is polled from /api/baseline-runs/{id} or
// /api/validation-runs/{id} every POLL_MS — same pattern as VMDetail. Defensive
// coding throughout per CLAUDE.md: collected_data and diff_result may be null.

const POLL_MS = 3000;
const POLL_TIMEOUT_MS = 30 * 60 * 1000; // 30 min — long enough for a 1000-VM run

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

const VERDICT_COLOR = {
  pass: "#00ff88",
  warn: "#ffaa00",
  fail: "#ff5577",
  unreachable: "#aaaacc",
};

const COLLECTION_STATUS_COLOR = {
  pending: "#aaaacc",
  in_progress: "#88aaff",
  captured: "#00ff88",
  failed: "#ff5577",
};

export default function WaveRunsPanel({ planId, waveNumber }) {
  const [keys, setKeys] = useState([]);
  const [keysLoading, setKeysLoading] = useState(true);

  // Active run-id refs so polling effects can short-circuit when ids change.
  const [baselineRunId, setBaselineRunId] = useState(null);
  const [baselineRun, setBaselineRun] = useState(null);
  const [validationRunId, setValidationRunId] = useState(null);
  const [validationRun, setValidationRun] = useState(null);

  const [showCaptureModal, setShowCaptureModal] = useState(false);
  const [showValidateModal, setShowValidateModal] = useState(false);
  const [showRevokeModal, setShowRevokeModal] = useState(false);
  const [kicking, setKicking] = useState(false);

  // Load active keys for the modal pickers + the auto-discovery of any
  // existing runs for this wave. We do not surface retired keys to the
  // pickers — the server would 422 anyway.
  useEffect(() => {
    let alive = true;
    setKeysLoading(true);
    fetchJSON("/api/ssh-keys?status=active&limit=200")
      .then((data) => {
        if (!alive) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        // Only show keys that are unscoped or scoped to this plan.
        setKeys(items.filter((k) => k.plan_id == null || k.plan_id === planId));
      })
      .catch(() => alive && setKeys([]))
      .finally(() => alive && setKeysLoading(false));
    return () => {
      alive = false;
    };
  }, [planId]);

  // Poll the active baseline run until it reaches a terminal state.
  useEffect(() => {
    if (baselineRunId == null) return;
    let alive = true;
    const started = Date.now();
    const tick = async () => {
      if (!alive) return;
      try {
        const data = await fetchJSON(`/api/baseline-runs/${baselineRunId}`);
        if (!alive) return;
        setBaselineRun(data);
        if (data?.status === "completed" || data?.status === "failed") {
          return; // stop polling
        }
      } catch (e) {
        if (!alive) return;
        toast.error(`Baseline poll failed: ${e.message}`, TOAST_OPTS);
        return;
      }
      if (Date.now() - started > POLL_TIMEOUT_MS) {
        toast.error("Baseline polling timed out — refresh to see latest", TOAST_OPTS);
        return;
      }
      setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      alive = false;
    };
  }, [baselineRunId]);

  // Poll the active validation run until terminal.
  useEffect(() => {
    if (validationRunId == null) return;
    let alive = true;
    const started = Date.now();
    const tick = async () => {
      if (!alive) return;
      try {
        const data = await fetchJSON(`/api/validation-runs/${validationRunId}`);
        if (!alive) return;
        setValidationRun(data);
        if (data?.status === "completed" || data?.status === "failed") {
          return;
        }
      } catch (e) {
        if (!alive) return;
        toast.error(`Validation poll failed: ${e.message}`, TOAST_OPTS);
        return;
      }
      if (Date.now() - started > POLL_TIMEOUT_MS) {
        toast.error("Validation polling timed out — refresh to see latest", TOAST_OPTS);
        return;
      }
      setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      alive = false;
    };
  }, [validationRunId]);

  const captureBaseline = async (sshKeyId) => {
    setKicking(true);
    try {
      const r = await fetchJSON(`/api/plans/${planId}/waves/${waveNumber}/baseline`, {
        method: "POST",
        body: { ssh_key_id: sshKeyId },
      });
      setBaselineRunId(r.baseline_run_id);
      setBaselineRun(null);
      toast.success("Baseline capture started", TOAST_OPTS);
      setShowCaptureModal(false);
    } catch (e) {
      toast.error(e.message || "Baseline capture failed to start", TOAST_OPTS);
    } finally {
      setKicking(false);
    }
  };

  const runValidation = async (sshKeyId) => {
    setKicking(true);
    try {
      const r = await fetchJSON(`/api/plans/${planId}/waves/${waveNumber}/validate`, {
        method: "POST",
        body: { ssh_key_id: sshKeyId },
      });
      setValidationRunId(r.validation_run_id);
      setValidationRun(null);
      toast.success("Validation started", TOAST_OPTS);
      setShowValidateModal(false);
    } catch (e) {
      toast.error(e.message || "Validation failed to start", TOAST_OPTS);
    } finally {
      setKicking(false);
    }
  };

  const revokeKey = async ({ sshKeyId, force }) => {
    setKicking(true);
    try {
      const r = await fetchJSON(
        `/api/plans/${planId}/waves/${waveNumber}/revoke-validation-key`,
        {
          method: "POST",
          body: { ssh_key_id: sshKeyId, force: !!force },
        },
      );
      toast.success(
        `Revoked from ${r?.succeeded ?? 0} VMs (${r?.failed ?? 0} failed)`,
        TOAST_OPTS,
      );
      setShowRevokeModal(false);
    } catch (e) {
      toast.error(e.message || "Revoke failed", TOAST_OPTS);
    } finally {
      setKicking(false);
    }
  };

  const baselineRunning =
    baselineRun &&
    (baselineRun.status === "pending" || baselineRun.status === "running");
  const baselineCompleted = baselineRun?.status === "completed";
  const validationRunning =
    validationRun &&
    (validationRun.status === "pending" || validationRun.status === "running");
  const validationCompleted = validationRun?.status === "completed";

  const canValidate = !validationRunning && baselineCompleted;
  const canRevokeClean =
    validationCompleted && (validationRun?.failed_vms ?? 0) === 0;

  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 14,
        borderTop: "1px solid #1a1a2e",
      }}
    >
      <div
        style={{
          fontSize: 11,
          color: "#88aaff",
          letterSpacing: "0.08em",
          fontWeight: 700,
          textTransform: "uppercase",
          marginBottom: 10,
        }}
      >
        Baseline &amp; validation
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button
          onClick={() => setShowCaptureModal(true)}
          disabled={baselineRunning || keysLoading}
          style={btnPrimary(baselineRunning || keysLoading)}
        >
          {baselineRunning ? "Capturing…" : "Capture baseline"}
        </button>
        <button
          onClick={() => setShowValidateModal(true)}
          disabled={!canValidate || keysLoading}
          style={btnPrimary(!canValidate || keysLoading)}
          title={
            !baselineCompleted
              ? "Capture a baseline first"
              : validationRunning
                ? "A validation is already running"
                : ""
          }
        >
          {validationRunning ? "Validating…" : "Validate"}
        </button>
        <button
          onClick={() => setShowRevokeModal(true)}
          disabled={!canRevokeClean}
          style={btnGhost(!canRevokeClean)}
          title={
            !validationCompleted
              ? "Validate first"
              : (validationRun?.failed_vms ?? 0) > 0
                ? "Validation has fail verdicts — resolve or use force"
                : ""
          }
        >
          Revoke validation key
        </button>
      </div>

      {/* Progress card — visible whenever a run exists, terminal or not. */}
      {baselineRun && (
        <BaselineProgressCard
          run={baselineRun}
          onRetryFailed={async () => {
            try {
              const r = await fetchJSON(
                `/api/baseline-runs/${baselineRun.id}/retry-failed`,
                { method: "POST" },
              );
              setBaselineRunId(r.baseline_run_id);
              setBaselineRun(null);
              toast.success("Retrying failed VMs", TOAST_OPTS);
            } catch (e) {
              toast.error(e.message || "Retry failed", TOAST_OPTS);
            }
          }}
        />
      )}
      {validationRun && <ValidationProgressCard run={validationRun} />}

      {/* Modals */}
      <KeyPickerModal
        open={showCaptureModal}
        title="Capture baseline for this wave"
        body="Select the SSH key to use. The appliance will connect to every VM in this wave and capture a structured baseline."
        keys={keys}
        keysLoading={keysLoading}
        busy={kicking}
        confirmLabel="Start capture"
        onClose={() => setShowCaptureModal(false)}
        onConfirm={captureBaseline}
      />
      <KeyPickerModal
        open={showValidateModal}
        title="Validate this wave"
        body="Select the SSH key. The appliance will collect post-migration data from every VM in this wave and diff it against the baseline."
        keys={keys}
        keysLoading={keysLoading}
        busy={kicking}
        confirmLabel="Start validation"
        onClose={() => setShowValidateModal(false)}
        onConfirm={runValidation}
      />
      <RevokeKeyModal
        open={showRevokeModal}
        keys={keys}
        keysLoading={keysLoading}
        busy={kicking}
        cleanValidation={canRevokeClean}
        onClose={() => setShowRevokeModal(false)}
        onConfirm={revokeKey}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Progress cards
// ---------------------------------------------------------------------------
function BaselineProgressCard({ run, onRetryFailed }) {
  const total = run?.total_vms ?? 0;
  const captured = run?.captured_vms ?? 0;
  const failed = run?.failed_vms ?? 0;
  const done = captured + failed;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const [expanded, setExpanded] = useState(false);

  const baselines = Array.isArray(run?.baselines) ? run.baselines : [];

  return (
    <div
      style={{
        marginTop: 14,
        padding: "14px 16px",
        border: "1px solid #1a1a2e",
        background: "#07070f",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div
            style={{
              fontSize: 13,
              color: "#eeeeff",
              fontWeight: 600,
              marginBottom: 4,
            }}
          >
            Baseline run #{run?.id}
            <span
              style={{
                fontSize: 11,
                color: STATUS_COLOR[run?.status] || "#aaaacc",
                marginLeft: 10,
                letterSpacing: "0.06em",
                fontWeight: 700,
                textTransform: "uppercase",
                fontFamily: "'Share Tech Mono', monospace",
              }}
            >
              {run?.status}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "#aaaacc" }}>
            {captured}/{total} captured, {failed} failed
            {run?.progress_message && (
              <span style={{ marginLeft: 10, fontStyle: "italic", color: "#888899" }}>
                · {run.progress_message}
              </span>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {failed > 0 && run?.status === "completed" && (
            <button onClick={onRetryFailed} style={btnGhost(false)}>
              Retry failed
            </button>
          )}
          <button onClick={() => setExpanded((v) => !v)} style={btnGhost(false)}>
            {expanded ? "Hide" : "Per-VM"}
          </button>
        </div>
      </div>
      <ProgressBar pct={pct} />
      {expanded && (
        <div style={{ marginTop: 12 }}>
          {baselines.length === 0 ? (
            <div style={{ fontSize: 12, color: "#888899" }}>No per-VM rows yet.</div>
          ) : (
            baselines.map((b) => (
              <div
                key={b.id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "6px 0",
                  borderTop: "1px solid #0f0f1e",
                  fontSize: 12,
                }}
              >
                <span>
                  <span style={{ ...monoBadge, marginRight: 8 }}>vm:{b.vm_id}</span>
                  <span
                    style={{
                      color: COLLECTION_STATUS_COLOR[b.status] || "#aaaacc",
                      fontWeight: 700,
                      letterSpacing: "0.06em",
                      textTransform: "uppercase",
                      fontFamily: "'Share Tech Mono', monospace",
                    }}
                  >
                    {b.status}
                  </span>
                  {b.failure_category && (
                    <span style={{ color: "#ff9999", marginLeft: 10 }}>
                      ({b.failure_category})
                    </span>
                  )}
                </span>
                {b.failure_detail && (
                  <span
                    style={{
                      color: "#888899",
                      fontFamily: "'Share Tech Mono', monospace",
                      maxWidth: 360,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={b.failure_detail}
                  >
                    {b.failure_detail}
                  </span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function ValidationProgressCard({ run }) {
  const total = run?.total_vms ?? 0;
  const p = run?.passed_vms ?? 0;
  const w = run?.warned_vms ?? 0;
  const f = run?.failed_vms ?? 0;
  const u = run?.unreachable_vms ?? 0;
  const done = p + w + f + u;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const [expanded, setExpanded] = useState(false);
  const validations = Array.isArray(run?.validations) ? run.validations : [];

  return (
    <div
      style={{
        marginTop: 14,
        padding: "14px 16px",
        border: "1px solid #1a1a2e",
        background: "#07070f",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div
            style={{
              fontSize: 13,
              color: "#eeeeff",
              fontWeight: 600,
              marginBottom: 4,
            }}
          >
            Validation run #{run?.id}
            <span
              style={{
                fontSize: 11,
                color: STATUS_COLOR[run?.status] || "#aaaacc",
                marginLeft: 10,
                letterSpacing: "0.06em",
                fontWeight: 700,
                textTransform: "uppercase",
                fontFamily: "'Share Tech Mono', monospace",
              }}
            >
              {run?.status}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "#aaaacc" }}>
            <VerdictPill verdict="pass" count={p} />
            <VerdictPill verdict="warn" count={w} />
            <VerdictPill verdict="fail" count={f} />
            <VerdictPill verdict="unreachable" count={u} />
            {run?.progress_message && (
              <span style={{ marginLeft: 10, fontStyle: "italic", color: "#888899" }}>
                · {run.progress_message}
              </span>
            )}
          </div>
        </div>
        <button onClick={() => setExpanded((v) => !v)} style={btnGhost(false)}>
          {expanded ? "Hide" : "Per-VM"}
        </button>
      </div>
      <ProgressBar pct={pct} />
      {expanded && (
        <div style={{ marginTop: 12 }}>
          {validations.length === 0 ? (
            <div style={{ fontSize: 12, color: "#888899" }}>No per-VM rows yet.</div>
          ) : (
            validations.map((v) => <ValidationRow key={v.id} v={v} />)
          )}
        </div>
      )}
    </div>
  );
}

function ValidationRow({ v }) {
  const [open, setOpen] = useState(false);
  const verdict = v?.verdict || "unreachable";
  const dims = Array.isArray(v?.diff_result?.dimensions) ? v.diff_result.dimensions : [];
  return (
    <div style={{ borderTop: "1px solid #0f0f1e", padding: "6px 0" }}>
      <div
        onClick={() => setOpen((s) => !s)}
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 12,
          cursor: "pointer",
        }}
      >
        <span>
          <span style={{ ...monoBadge, marginRight: 8 }}>vm:{v.vm_id}</span>
          <span
            style={{
              color: VERDICT_COLOR[verdict] || "#aaaacc",
              fontWeight: 700,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              fontFamily: "'Share Tech Mono', monospace",
            }}
          >
            {verdict}
          </span>
          {v?.host_key_changed && (
            <span
              style={{
                color: "#88aaff",
                marginLeft: 10,
                fontSize: 11,
              }}
              title="Host key changed since baseline — informational"
            >
              host key changed
            </span>
          )}
          {v?.failure_category && (
            <span style={{ color: "#ff9999", marginLeft: 10 }}>({v.failure_category})</span>
          )}
        </span>
        <span style={{ color: "#888899" }}>{open ? "−" : "+"}</span>
      </div>
      {open && (
        <div style={{ marginTop: 6, paddingLeft: 12 }}>
          {dims.length === 0 ? (
            <div style={{ fontSize: 11, color: "#888899" }}>
              {v?.failure_detail || "No diff available."}
            </div>
          ) : (
            dims.map((d) => (
              <div key={d.name} style={{ fontSize: 11, marginBottom: 4 }}>
                <span
                  style={{
                    color: VERDICT_COLOR[d.verdict] || "#aaaacc",
                    fontWeight: 700,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                    marginRight: 8,
                    fontFamily: "'Share Tech Mono', monospace",
                  }}
                >
                  {d.verdict}
                </span>
                <span style={{ color: "#ccccee" }}>{d.name}</span>
                {Array.isArray(d.changes) && d.changes.length > 0 && (
                  <span style={{ color: "#888899", marginLeft: 8 }}>
                    {d.changes.length} change{d.changes.length === 1 ? "" : "s"}
                  </span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function VerdictPill({ verdict, count }) {
  return (
    <span
      style={{
        display: "inline-block",
        marginRight: 10,
        fontFamily: "'Share Tech Mono', monospace",
      }}
    >
      <span
        style={{
          color: VERDICT_COLOR[verdict] || "#aaaacc",
          fontWeight: 700,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
          fontSize: 11,
        }}
      >
        {verdict}
      </span>
      <span style={{ color: "#aaaacc", fontSize: 12, marginLeft: 4 }}>{count}</span>
    </span>
  );
}

function ProgressBar({ pct }) {
  return (
    <div
      style={{
        marginTop: 10,
        height: 4,
        background: "#1a1a2e",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          height: "100%",
          width: `${pct}%`,
          background: "linear-gradient(90deg, #4488ff 0%, #00ff88 100%)",
          transition: "width 0.4s ease",
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modals
// ---------------------------------------------------------------------------
function KeyPickerModal({ open, title, body, keys, keysLoading, busy, confirmLabel, onClose, onConfirm }) {
  const [selected, setSelected] = useState("");
  useEffect(() => {
    if (open) {
      setSelected(keys.length > 0 ? String(keys[0].id) : "");
    }
  }, [open, keys]);
  if (!open) return null;
  const noKeys = !keysLoading && keys.length === 0;
  return (
    <ModalShell title={title} onClose={onClose} busy={busy}>
      <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 14, lineHeight: 1.6 }}>{body}</div>
      {noKeys ? (
        <div
          style={{
            padding: "12px 14px",
            border: "1px solid #ffaa0066",
            background: "rgba(255,170,0,0.06)",
            color: "#ffe9aa",
            fontSize: 13,
          }}
        >
          No active SSH keys are available for this plan. Generate one in Settings →
          Validation Keys first.
        </div>
      ) : (
        <select
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
          style={inputStyle}
          disabled={keysLoading}
        >
          {keys.map((k) => (
            <option key={k.id} value={k.id}>
              {k.name} · {(k.fingerprint || "").slice(0, 27)}…{" "}
              {k.plan_id != null ? "(scoped)" : "(unscoped)"}
            </option>
          ))}
        </select>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
        <button onClick={onClose} disabled={busy} style={btnSecondaryStyle(busy)}>
          Cancel
        </button>
        <button
          onClick={() => onConfirm(Number(selected))}
          disabled={busy || noKeys || !selected}
          style={btnPrimary(busy || noKeys || !selected)}
        >
          {busy ? "Starting…" : confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}

function RevokeKeyModal({ open, keys, keysLoading, busy, cleanValidation, onClose, onConfirm }) {
  const [selected, setSelected] = useState("");
  const [force, setForce] = useState(false);
  useEffect(() => {
    if (open) {
      setSelected(keys.length > 0 ? String(keys[0].id) : "");
      setForce(false);
    }
  }, [open, keys]);
  if (!open) return null;
  return (
    <ModalShell title="Revoke validation key from wave" onClose={onClose} busy={busy}>
      <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 14, lineHeight: 1.6 }}>
        Removes the chosen key&apos;s public component from each VM in this wave&apos;s
        <code style={{ ...monoBadge, marginLeft: 4 }}>authorized_keys</code>.
        Per-VM failures (unreachable, etc.) surface in the response but don&apos;t
        abort the batch.
      </div>
      {!cleanValidation && (
        <div
          style={{
            padding: "10px 12px",
            border: "1px solid #ffaa0066",
            background: "rgba(255,170,0,0.06)",
            color: "#ffe9aa",
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          ⚠ Latest validation isn&apos;t clean. Enabling force overrides the safety gate.
        </div>
      )}
      <select
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
        style={inputStyle}
        disabled={keysLoading || keys.length === 0}
      >
        {keys.length === 0 && <option value="">No active keys</option>}
        {keys.map((k) => (
          <option key={k.id} value={k.id}>
            {k.name} · {(k.fingerprint || "").slice(0, 27)}…
          </option>
        ))}
      </select>
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginTop: 14,
          fontSize: 13,
          color: "#ccccee",
          cursor: "pointer",
        }}
      >
        <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
        Force revoke despite open fail verdicts (bail-out)
      </label>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>
        <button onClick={onClose} disabled={busy} style={btnSecondaryStyle(busy)}>
          Cancel
        </button>
        <button
          onClick={() => onConfirm({ sshKeyId: Number(selected), force })}
          disabled={busy || !selected}
          style={btnPrimary(busy || !selected)}
        >
          {busy ? "Revoking…" : "Revoke"}
        </button>
      </div>
    </ModalShell>
  );
}

function ModalShell({ title, busy, onClose, children }) {
  const escRef = useRef(onClose);
  escRef.current = onClose;
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape" && !busy) escRef.current?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy]);
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
          maxHeight: "90vh",
          overflowY: "auto",
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
          {title}
        </div>
        {children}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
const STATUS_COLOR = {
  pending: "#aaaacc",
  running: "#88aaff",
  completed: "#00ff88",
  failed: "#ff5577",
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

const monoBadge = {
  fontFamily: "'Share Tech Mono', monospace",
  color: "#aaaacc",
  border: "1px solid #2a2a44",
  padding: "2px 6px",
  fontSize: 11,
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
  };
}

function btnGhost(disabled) {
  return {
    background: "transparent",
    border: `1px solid ${disabled ? "#2a2a44" : "#3a3a55"}`,
    color: disabled ? "#888899" : "#ccccee",
    padding: "8px 12px",
    fontSize: 11,
    fontFamily: "'Barlow', sans-serif",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
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
