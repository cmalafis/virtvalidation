import { useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";

import { fetchJSON } from "../utils/fetchJSON";
import { groupByVCenter, parseRVToolsXLSX } from "../utils/parseRVTools";

// Top-level RVTools upload — auto-detects vCenter per VM, matches to
// registered sources, surfaces unmatched hostnames so the operator
// can map or create. Replaces the older flow that required selecting
// a vCenter upfront.
//
// Flow:
//   1. parse XLSX     →  vms[] with source_vcenter_hostname populated
//   2. /auto-match    →  matches[] + unmatched[]
//   3. operator confirms / creates / skips
//   4. /upload-multi-vcenter → per-vcenter import results

const TOAST_OPTS = {
  style: {
    background: "#0a0a18", border: "1px solid #2a2a44", color: "#eeeeff",
    fontFamily: "'Barlow', sans-serif", fontSize: 14, lineHeight: 1.5,
  },
  success: { iconTheme: { primary: "#00ff88", secondary: "#0a0a18" } },
  error: { iconTheme: { primary: "#ff3355", secondary: "#0a0a18" } },
};


export default function RVToolsUpload() {
  const [stage, setStage] = useState("pick"); // pick | matching | confirm | importing | done
  const [filename, setFilename] = useState("");
  const [parseStats, setParseStats] = useState(null);
  const [vms, setVms] = useState([]);
  const [groups, setGroups] = useState([]); // [{hostname, vm_count, sample_vm_names}]
  const [mapping, setMapping] = useState({}); // hostname -> {action: 'match'|'create'|'skip', vcenter_id?}
  const [matchResp, setMatchResp] = useState(null);
  const [registered, setRegistered] = useState([]);
  const [importResult, setImportResult] = useState(null);
  const [importError, setImportError] = useState(null);
  const [busy, setBusy] = useState(false);

  const onFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const result = await parseRVToolsXLSX(file);
      if (result.vms.length === 0) {
        toast.error("No VMs parsed from file — wrong sheet?", TOAST_OPTS);
        return;
      }
      setFilename(file.name);
      setParseStats({
        total: result.totalRows,
        parsed: result.vms.length,
        skipped: result.skipped.length,
        sheet: result.sheetName,
      });
      setVms(result.vms);

      const groupedHosts = groupByVCenter(result.vms);
      setGroups(groupedHosts);

      // Hit auto-match for the detected hostnames.
      const hostnames = groupedHosts
        .map((g) => g.hostname)
        .filter((h) => h && h !== "_unspecified_");
      const reg = await fetchJSON("/api/sources/vcenters");
      setRegistered(reg);
      let resp = { matches: [], unmatched: hostnames };
      if (hostnames.length > 0) {
        resp = await fetchJSON("/api/sources/vcenters/auto-match", {
          method: "POST",
          body: { hostnames },
        });
      }
      setMatchResp(resp);

      // Seed the mapping with auto-matched entries.
      const initial = {};
      for (const m of resp.matches) {
        initial[m.hostname] = {
          action: "match",
          vcenter_id: m.matched_vcenter_id,
          vcenter_name: m.matched_vcenter_name,
          confidence: m.confidence,
        };
      }
      for (const h of resp.unmatched) initial[h] = { action: "create" };
      if (groupedHosts.some((g) => g.hostname === "_unspecified_")) {
        initial["_unspecified_"] = { action: "skip" };
      }
      setMapping(initial);
      setStage("confirm");
    } catch (err) {
      toast.error(err.message || "Parse failed", TOAST_OPTS);
    } finally {
      setBusy(false);
    }
  };

  const updateMapping = (host, patch) => {
    setMapping((m) => ({ ...m, [host]: { ...m[host], ...patch } }));
  };

  const onCreateAndMap = async (host) => {
    const name = mapping[host]?.create_name || host;
    try {
      const created = await fetchJSON("/api/sources/vcenters", {
        method: "POST",
        body: { name, hostname: host },
      });
      setRegistered((r) => [...r, created]);
      updateMapping(host, { action: "match", vcenter_id: created.id, vcenter_name: created.name });
      toast.success(`Created ${name}`, TOAST_OPTS);
    } catch (err) { toast.error(err.message, TOAST_OPTS); }
  };

  const onConfirmImport = async () => {
    // Build the vcenter_mapping payload from the user's choices.
    const vcenter_mapping = {};
    const skipHosts = new Set();
    for (const [host, choice] of Object.entries(mapping)) {
      if (host === "_unspecified_") continue;
      if (choice.action === "match" && choice.vcenter_id) {
        vcenter_mapping[host] = choice.vcenter_id;
      } else {
        skipHosts.add(host);
      }
    }
    // Filter out VMs that would be skipped (caller can review counts).
    const routable = vms.filter((vm) => {
      const host = vm.source_vcenter_hostname || "_unspecified_";
      return !skipHosts.has(host) && host !== "_unspecified_";
    });
    if (routable.length === 0) {
      toast.error("No VMs are mapped to a vCenter", TOAST_OPTS);
      return;
    }

    setBusy(true);
    setImportError(null);
    setStage("importing");
    try {
      const result = await fetchJSON("/api/rvtools/upload-multi-vcenter", {
        method: "POST",
        body: { vms: routable, vcenter_mapping },
      });
      setImportResult(result);
      setStage("done");
      const summary = result.imported_per_vcenter
        .map((p) => `${p.vcenter_name}: ${p.created} new`)
        .join(", ");
      toast.success(`Imported · ${summary}`, TOAST_OPTS);
    } catch (err) {
      // Critical: never leave the UI stuck in "importing". Fall back
      // to the confirm stage with the structured error visible inline
      // so the operator can adjust the mapping or retry without
      // re-uploading. The toast is supplementary; the inline message
      // survives the toast dismissal.
      const message = err?.message || "Import failed";
      console.error("RVTools import failed", err);
      setImportError({
        status: err?.status,
        message,
        // Show up to 5 specific lines so a 422 with many field errors
        // is readable. fetchJSON has already flattened Pydantic
        // detail into "field.path: msg" entries joined by "; ".
        lines: message.split("; ").slice(0, 5),
      });
      toast.error(
        message.length > 120 ? `${message.slice(0, 120)}…` : message,
        TOAST_OPTS,
      );
      setStage("confirm");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell>
      <Toaster position="bottom-right" toastOptions={TOAST_OPTS} />
      <header style={headerStyle}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>Upload RVTools</h1>
          <p style={{ color: "#aaaacc", fontSize: 13, margin: "4px 0 0", lineHeight: 1.5 }}>
            Auto-detects vCenter per VM, matches to registered sources, imports each
            scope independently. Multi-vCenter files distribute VMs correctly without
            needing separate uploads.
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Link to="/sources/vcenters" style={btnSecondary}>← vCenters</Link>
          <Link to="/" style={btnSecondary}>Inventory</Link>
        </div>
      </header>

      <main style={{ maxWidth: 1100, margin: "0 auto", padding: 32 }}>
        {stage === "pick" && (
          <PickStage onFile={onFile} busy={busy} />
        )}
        {parseStats && (
          <ParseStatsCard filename={filename} parseStats={parseStats} groups={groups} />
        )}
        {importError && stage !== "importing" && (
          <ImportErrorBanner
            error={importError}
            onDismiss={() => setImportError(null)}
            onRetry={onConfirmImport}
            busy={busy}
          />
        )}
        {stage === "confirm" && matchResp && (
          <ConfirmStage
            groups={groups}
            mapping={mapping}
            registered={registered}
            updateMapping={updateMapping}
            onCreateAndMap={onCreateAndMap}
            onConfirm={onConfirmImport}
            busy={busy}
          />
        )}
        {stage === "importing" && (
          <div style={{ padding: 18, color: "#88aaff", fontSize: 14 }}>
            Importing… one moment.
          </div>
        )}
        {stage === "done" && importResult && (
          <DoneStage result={importResult} />
        )}
      </main>
    </Shell>
  );
}


function ImportErrorBanner({ error, onDismiss, onRetry, busy }) {
  // 422 = validation error (shows the per-field list). Other statuses
  // are server-side failures we can't auto-fix; suggest contacting
  // support / checking the audit log.
  const isValidation = error.status === 422;
  return (
    <div style={{
      marginBottom: 14, padding: "14px 18px",
      border: "1px solid #ff5577", background: "rgba(255,51,85,0.06)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 12, color: "#ff99aa", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          Import failed{error.status ? ` · HTTP ${error.status}` : ""}
        </div>
        <button onClick={onDismiss} style={{
          background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
          padding: "4px 10px", fontSize: 11, cursor: "pointer",
        }}>Dismiss</button>
      </div>
      {isValidation ? (
        <>
          <div style={{ marginTop: 8, fontSize: 13, color: "#ccaaaa" }}>
            The server rejected the payload. Specific issues:
          </div>
          <ul style={{ marginTop: 6, paddingLeft: 18, color: "#ccccee", fontSize: 12, lineHeight: 1.6 }}>
            {(error.lines || []).map((l, i) => <li key={i}><code>{l}</code></li>)}
          </ul>
        </>
      ) : (
        <div style={{ marginTop: 8, fontSize: 13, color: "#ccaaaa", lineHeight: 1.5 }}>
          {error.message}
        </div>
      )}
      <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
        <button onClick={onRetry} disabled={busy}
          style={{
            background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
            padding: "8px 14px", fontSize: 11, fontWeight: 700, textTransform: "uppercase",
            letterSpacing: "0.06em", cursor: busy ? "not-allowed" : "pointer",
          }}>
          {busy ? "Retrying…" : "Try again"}
        </button>
        {!isValidation && (
          <span style={{ fontSize: 11, color: "#888899", alignSelf: "center" }}>
            If the issue persists, check the appliance audit log or contact support.
          </span>
        )}
      </div>
    </div>
  );
}


function PickStage({ onFile, busy }) {
  return (
    <label style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      padding: 40, border: "1px dashed #3a3a55", cursor: busy ? "not-allowed" : "pointer",
      background: "#07070f",
    }}>
      <div style={{ fontSize: 28, color: "#aaaacc", marginBottom: 12 }}>↑</div>
      <div style={{ fontSize: 14, color: "#eeeeff", fontWeight: 600 }}>
        {busy ? "Parsing…" : "Click to choose RVTools .xlsx"}
      </div>
      <div style={{ fontSize: 12, color: "#888899", marginTop: 6 }}>
        Reads the vInfo sheet and routes VMs to vCenter sources by the file&apos;s
        own vCenter column.
      </div>
      <input
        type="file"
        accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        style={{ display: "none" }}
        onChange={onFile}
        disabled={busy}
      />
    </label>
  );
}


function ParseStatsCard({ filename, parseStats, groups }) {
  const realGroups = groups.filter((g) => g.hostname !== "_unspecified_");
  return (
    <div style={{ padding: "10px 14px", border: "1px solid #1a1a2e", background: "#0a0a16",
      fontSize: 12, color: "#ccccee", marginBottom: 16 }}>
      <div><strong>{filename}</strong> · sheet: {parseStats.sheet}</div>
      <div style={{ color: "#aaaacc", marginTop: 4 }}>
        Parsed {parseStats.parsed} of {parseStats.total} rows
        {parseStats.skipped > 0
          ? ` (${parseStats.skipped} skipped — missing both name and hostname)`
          : ""}
        {realGroups.length > 0 && ` · ${realGroups.length} vCenter${realGroups.length === 1 ? "" : "s"} detected`}
      </div>
    </div>
  );
}


function ConfirmStage({ groups, mapping, registered, updateMapping, onCreateAndMap, onConfirm, busy }) {
  const totalRoutable = groups
    .filter((g) => g.hostname !== "_unspecified_")
    .filter((g) => mapping[g.hostname]?.action === "match" && mapping[g.hostname]?.vcenter_id)
    .reduce((sum, g) => sum + g.vm_count, 0);

  return (
    <div>
      <h2 style={{ fontSize: 15, fontWeight: 700, marginBottom: 12 }}>
        Match detected vCenters to registered sources
      </h2>
      <div style={{ border: "1px solid #1a1a2e", background: "#0a0a18" }}>
        <div style={tableHeaderStyle}>
          <span>Detected hostname</span>
          <span>VMs</span>
          <span>Confidence</span>
          <span>Action</span>
          <span>Target / new name</span>
        </div>
        {groups.map((g) => {
          const choice = mapping[g.hostname] || {};
          const isUnspec = g.hostname === "_unspecified_";
          return (
            <div key={g.hostname} style={tableRowStyle}>
              <span style={{ fontFamily: "'Share Tech Mono', monospace", fontSize: 12, color: isUnspec ? "#ffaa00" : "#eeeeff" }}>
                {isUnspec ? "(no vCenter column)" : g.hostname}
              </span>
              <span style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{g.vm_count}</span>
              <span style={{ fontSize: 11 }}>
                {choice.confidence === "exact" && <em style={{ color: "#00ff88" }}>exact match</em>}
                {choice.confidence === "fuzzy" && <em style={{ color: "#ffaa00" }}>fuzzy</em>}
                {!choice.confidence && <em style={{ color: "#aaaacc" }}>—</em>}
              </span>
              <select style={inputStyle} value={choice.action || "skip"}
                onChange={(e) => updateMapping(g.hostname, { action: e.target.value })}>
                <option value="match">Match to existing</option>
                <option value="create">Create new vCenter</option>
                <option value="skip">Skip these VMs</option>
              </select>
              {choice.action === "match" && (
                <select style={inputStyle} value={choice.vcenter_id || ""}
                  onChange={(e) => {
                    const id = parseInt(e.target.value, 10);
                    const r = registered.find((x) => x.id === id);
                    updateMapping(g.hostname, { vcenter_id: id, vcenter_name: r?.name });
                  }}>
                  <option value="">— pick vCenter —</option>
                  {registered.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              )}
              {choice.action === "create" && !isUnspec && (
                <div style={{ display: "flex", gap: 6 }}>
                  <input style={{ ...inputStyle, flex: 1 }}
                    placeholder={g.hostname.split(".")[0] || "name"}
                    value={choice.create_name || ""}
                    onChange={(e) => updateMapping(g.hostname, { create_name: e.target.value })} />
                  <button style={btnGhost} onClick={() => onCreateAndMap(g.hostname)}>
                    + Create
                  </button>
                </div>
              )}
              {choice.action === "skip" && (
                <span style={{ color: "#888899", fontSize: 12 }}>VMs will be skipped</span>
              )}
            </div>
          );
        })}
      </div>

      <div style={{ marginTop: 14, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 13, color: "#ccccee" }}>
          <strong>{totalRoutable}</strong> VMs ready to import
        </div>
        <button onClick={onConfirm} disabled={busy || totalRoutable === 0}
          style={{ ...btnPrimary, opacity: (busy || totalRoutable === 0) ? 0.5 : 1 }}>
          {busy ? "Importing…" : "Confirm import"}
        </button>
      </div>
    </div>
  );
}


function DoneStage({ result }) {
  return (
    <div style={{ padding: "16px 18px", border: "1px solid #00ff8855", background: "rgba(0,255,136,0.06)" }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: "#00ff88", marginBottom: 8 }}>
        ✓ Import complete · {result.elapsed_seconds}s
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 10 }}>
        <thead>
          <tr style={{ color: "#aaaacc", fontSize: 11, textTransform: "uppercase" }}>
            <th style={{ textAlign: "left", padding: 6 }}>vCenter</th>
            <th style={{ textAlign: "right", padding: 6 }}>New</th>
            <th style={{ textAlign: "right", padding: 6 }}>Updated</th>
            <th style={{ textAlign: "right", padding: 6 }}>Missing</th>
            <th style={{ textAlign: "right", padding: 6 }}>Unchanged</th>
          </tr>
        </thead>
        <tbody>
          {(result.imported_per_vcenter || []).map((p) => (
            <tr key={p.vcenter_id} style={{ color: "#ccccee", fontFamily: "'Share Tech Mono', monospace", fontSize: 12 }}>
              <td style={{ padding: 6 }}>{p.vcenter_name}</td>
              <td style={{ padding: 6, textAlign: "right" }}>{p.created}</td>
              <td style={{ padding: 6, textAlign: "right" }}>{p.updated}</td>
              <td style={{ padding: 6, textAlign: "right" }}>{p.marked_missing}</td>
              <td style={{ padding: 6, textAlign: "right" }}>{p.unchanged}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(result.skipped || []).length > 0 && (
        <div style={{ marginTop: 10, color: "#ffaa00", fontSize: 12 }}>
          {result.skipped.length} VMs skipped — see audit log for hostnames.
        </div>
      )}
      {(result.errors || []).length > 0 && (
        <ul style={{ marginTop: 10, color: "#ff99aa", fontSize: 12, paddingLeft: 18 }}>
          {result.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
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

const headerStyle = {
  position: "sticky", top: 0, zIndex: 50,
  background: "rgba(7,7,15,0.96)", backdropFilter: "blur(8px)",
  borderBottom: "1px solid #1a1a2e",
  display: "flex", alignItems: "center", justifyContent: "space-between",
  padding: "16px 32px", gap: 24,
};
const tableHeaderStyle = {
  display: "grid", gridTemplateColumns: "2fr 0.5fr 1fr 1.4fr 2fr",
  padding: "10px 14px", borderBottom: "1px solid #1a1a2e", background: "#0a0a16",
  fontSize: 11, color: "#aaaacc", fontWeight: 700, textTransform: "uppercase",
};
const tableRowStyle = {
  display: "grid", gridTemplateColumns: "2fr 0.5fr 1fr 1.4fr 2fr",
  padding: "10px 14px", borderBottom: "1px solid #0f0f1e",
  alignItems: "center", fontSize: 13, gap: 10,
};
const inputStyle = {
  background: "#07070f", border: "1px solid #2a2a44", color: "#eeeeff",
  padding: "8px 10px", fontFamily: "'Share Tech Mono', monospace",
  fontSize: 12, width: "100%", outline: "none",
};
const btnPrimary = {
  background: "#1d3a8a", border: "1px solid #4488ff", color: "#eef2ff",
  padding: "10px 18px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};
const btnSecondary = {
  background: "transparent", border: "1px solid #3a3a55", color: "#aaaacc",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 12,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
  textDecoration: "none",
};
const btnGhost = {
  background: "transparent", border: "1px solid #2a2a44", color: "#ccccee",
  padding: "8px 14px", fontFamily: "'Barlow', sans-serif", fontSize: 11,
  letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700, cursor: "pointer",
};

