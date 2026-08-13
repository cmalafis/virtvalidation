import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchJSON } from "../utils/fetchJSON";

// Agent Activity — the operator's "what is the agent actually doing?" surface.
//
// Two append-only audit streams, both read-only:
//   - Command audit   → every SSH command run (or blocked) on a host
//                       (GET /api/command-audits)
//   - Inference log   → every LLM call's input/output/method/detections
//                       (GET /api/inference-logs, + /stats)
//
// Defensive throughout per CLAUDE.md: every nested field may be missing.

const PAGE_SIZE = 25;

const METHOD_COLOR = {
  llm: "#00ff88",
  manual_review: "#ffaa00",
  mechanical_fallback: "#88aaff",
  mechanical_fallback_auth: "#ff8855",
  mechanical_fallback_guardrail: "#ff5577",
};

const card = {
  background: "#0a0a18",
  border: "1px solid #1a1a2e",
  padding: 20,
  marginBottom: 24,
};
const th = {
  textAlign: "left",
  fontSize: 11,
  color: "#888899",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  fontWeight: 700,
  padding: "8px 10px",
  borderBottom: "1px solid #2a2a44",
};
const td = {
  fontSize: 12,
  color: "#ccccee",
  padding: "8px 10px",
  borderBottom: "1px solid #14142a",
  verticalAlign: "top",
};
const mono = { fontFamily: "'Share Tech Mono', monospace" };

function Pager({ skip, total, onPrev, onNext }) {
  const from = total === 0 ? 0 : skip + 1;
  const to = Math.min(skip + PAGE_SIZE, total);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12 }}>
      <button onClick={onPrev} disabled={skip === 0} style={pagerBtn(skip === 0)}>
        ← Prev
      </button>
      <span style={{ fontSize: 12, color: "#888899" }}>
        {from}–{to} of {total}
      </span>
      <button onClick={onNext} disabled={to >= total} style={pagerBtn(to >= total)}>
        Next →
      </button>
    </div>
  );
}

function pagerBtn(disabled) {
  return {
    background: "transparent",
    border: "1px solid #2a2a44",
    color: disabled ? "#555566" : "#aaccff",
    padding: "6px 12px",
    fontSize: 12,
    cursor: disabled ? "default" : "pointer",
  };
}

function Badge({ text, color }) {
  return (
    <span
      style={{
        ...mono,
        fontSize: 11,
        color: color || "#aaaacc",
        border: `1px solid ${color || "#2a2a44"}55`,
        padding: "1px 6px",
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function CommandAuditTable() {
  const [data, setData] = useState({ items: [], total: 0, skip: 0 });
  const [skip, setSkip] = useState(0);
  const [blockedOnly, setBlockedOnly] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ skip: String(skip), limit: String(PAGE_SIZE) });
      if (blockedOnly) params.set("blocked", "true");
      const body = await fetchJSON(`/api/command-audits?${params.toString()}`);
      setData({
        items: Array.isArray(body?.items) ? body.items : [],
        total: body?.total ?? 0,
        skip: body?.skip ?? 0,
      });
    } catch (e) {
      setError(e.message || "Failed to load command audit");
      setData({ items: [], total: 0, skip: 0 });
    } finally {
      setLoading(false);
    }
  }, [skip, blockedOnly]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data.items ?? [];

  return (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "#eeeeff" }}>Command audit</div>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "#ccccee", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={blockedOnly}
            onChange={(e) => {
              setBlockedOnly(e.target.checked);
              setSkip(0);
            }}
            style={{ accentColor: "#ff3355" }}
          />
          Blocked only
        </label>
      </div>
      <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 14, lineHeight: 1.6 }}>
        Every command the agent ran on a host — exit status, stdout hash, timing, and any
        the read-only gate refused before execution.
      </div>
      {error ? (
        <div style={{ color: "#ffccdd", fontSize: 13 }}>{error}</div>
      ) : loading ? (
        <div style={{ color: "#888899", fontSize: 13 }}>Loading…</div>
      ) : items.length === 0 ? (
        <div style={{ color: "#888899", fontSize: 13 }}>No commands recorded yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>When</th>
                <th style={th}>Host</th>
                <th style={th}>Run</th>
                <th style={th}>Command</th>
                <th style={th}>Exit</th>
                <th style={th}>Out</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id}>
                  <td style={td}>
                    {c.started_at ? new Date(c.started_at).toLocaleString() : "—"}
                  </td>
                  <td style={{ ...td, ...mono }}>{c.host || "—"}</td>
                  <td style={td}>
                    {(c.run_type || "—")}
                    {c.run_id != null ? ` #${c.run_id}` : ""}
                  </td>
                  <td style={{ ...td, ...mono, color: c.blocked ? "#ff8899" : "#ccccee", maxWidth: 380, wordBreak: "break-all" }}>
                    {c.blocked && <Badge text="BLOCKED" color="#ff3355" />}{" "}
                    {c.command}
                  </td>
                  <td style={td}>{c.blocked ? "—" : (c.exit_status ?? "—")}</td>
                  <td style={{ ...td, ...mono, color: "#7788aa" }}>
                    {c.blocked ? "—" : `${c.stdout_byte_count ?? 0}B`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager
        skip={skip}
        total={data.total ?? 0}
        onPrev={() => setSkip((s) => Math.max(0, s - PAGE_SIZE))}
        onNext={() => setSkip((s) => s + PAGE_SIZE)}
      />
    </div>
  );
}

function InferenceLogTable() {
  const [data, setData] = useState({ items: [], total: 0 });
  const [stats, setStats] = useState(null);
  const [skip, setSkip] = useState(0);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ skip: String(skip), limit: String(PAGE_SIZE) });
      const [body, st] = await Promise.all([
        fetchJSON(`/api/inference-logs?${params.toString()}`),
        fetchJSON(`/api/inference-logs/stats`).catch(() => null),
      ]);
      setData({
        items: Array.isArray(body?.items) ? body.items : [],
        total: body?.total ?? 0,
      });
      setStats(st);
    } catch (e) {
      setError(e.message || "Failed to load inference log");
      setData({ items: [], total: 0 });
    } finally {
      setLoading(false);
    }
  }, [skip]);

  useEffect(() => {
    load();
  }, [load]);

  const items = data.items ?? [];
  const byMethod = stats?.by_method ?? {};

  return (
    <div style={card}>
      <div style={{ fontSize: 16, fontWeight: 700, color: "#eeeeff", marginBottom: 14 }}>
        Inference log
      </div>
      <div style={{ fontSize: 13, color: "#aaaacc", marginBottom: 14, lineHeight: 1.6 }}>
        Every LLM call — the exact prompt, the raw response, the fallback path taken, and any
        guardrail detections. The basis for auditing and explaining the agent&apos;s reasoning.
      </div>
      {stats && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 14 }}>
          <Badge text={`${stats.total ?? 0} total`} color="#aaccff" />
          {Object.entries(byMethod).map(([m, n]) => (
            <Badge key={m} text={`${m}: ${n}`} color={METHOD_COLOR[m]} />
          ))}
        </div>
      )}
      {error ? (
        <div style={{ color: "#ffccdd", fontSize: 13 }}>{error}</div>
      ) : loading ? (
        <div style={{ color: "#888899", fontSize: 13 }}>Loading…</div>
      ) : items.length === 0 ? (
        <div style={{ color: "#888899", fontSize: 13 }}>No LLM calls recorded yet.</div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={th}>When</th>
                <th style={th}>Operation</th>
                <th style={th}>Backend</th>
                <th style={th}>Method</th>
                <th style={th}>Verdict</th>
                <th style={th}></th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td style={td}>{r.created_at ? new Date(r.created_at).toLocaleString() : "—"}</td>
                    <td style={td}>{r.operation || "—"}</td>
                    <td style={{ ...td, ...mono }}>
                      {r.backend_type || "—"}
                      {r.model ? ` · ${r.model}` : ""}
                    </td>
                    <td style={td}>
                      <Badge text={r.method || "llm"} color={METHOD_COLOR[r.method]} />
                    </td>
                    <td style={td}>{r.verdict || "—"}</td>
                    <td style={td}>
                      <button
                        onClick={() => setExpanded(expanded === r.id ? null : r.id)}
                        style={{ background: "none", border: "none", color: "#88aaff", cursor: "pointer", fontSize: 12 }}
                      >
                        {expanded === r.id ? "Hide" : "View"}
                      </button>
                    </td>
                  </tr>
                  {expanded === r.id && (
                    <tr>
                      <td style={{ ...td, background: "#06060f" }} colSpan={6}>
                        {(r.detections && Object.keys(r.detections).length > 0) && (
                          <div style={{ marginBottom: 10 }}>
                            <div style={{ fontSize: 11, color: "#ff8899", marginBottom: 4 }}>
                              GUARDRAIL DETECTIONS
                            </div>
                            <pre style={{ ...mono, fontSize: 11, color: "#ffccdd", whiteSpace: "pre-wrap", margin: 0 }}>
                              {JSON.stringify(r.detections, null, 2)}
                            </pre>
                          </div>
                        )}
                        <div style={{ fontSize: 11, color: "#888899", marginBottom: 4 }}>INPUT MESSAGES</div>
                        <pre style={{ ...mono, fontSize: 11, color: "#aabbcc", whiteSpace: "pre-wrap", maxHeight: 200, overflowY: "auto", margin: "0 0 10px" }}>
                          {JSON.stringify(r.input_messages ?? [], null, 2)}
                        </pre>
                        <div style={{ fontSize: 11, color: "#888899", marginBottom: 4 }}>OUTPUT</div>
                        <pre style={{ ...mono, fontSize: 11, color: "#aabbcc", whiteSpace: "pre-wrap", maxHeight: 200, overflowY: "auto", margin: 0 }}>
                          {r.output_text ?? "(none)"}
                        </pre>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager
        skip={skip}
        total={data.total ?? 0}
        onPrev={() => setSkip((s) => Math.max(0, s - PAGE_SIZE))}
        onNext={() => setSkip((s) => s + PAGE_SIZE)}
      />
    </div>
  );
}

export default function AgentActivity() {
  return (
    <div style={{ minHeight: "100vh", background: "#07070f", color: "#eeeeff", fontFamily: "'Barlow', sans-serif" }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;400;600;700&display=swap');`}</style>
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "32px 24px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, letterSpacing: "0.04em" }}>Agent Activity</h1>
          <Link to="/" style={{ color: "#88aaff", textDecoration: "none", fontSize: 13 }}>
            ← Dashboard
          </Link>
        </div>
        <div style={{ fontSize: 14, color: "#aaaacc", marginBottom: 24, lineHeight: 1.6 }}>
          Exactly what the agent did — every SSH command run against a host and every LLM
          inference, both append-only.
        </div>
        <CommandAuditTable />
        <InferenceLogTable />
      </div>
    </div>
  );
}
