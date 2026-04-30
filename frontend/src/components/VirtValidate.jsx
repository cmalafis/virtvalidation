import { useCallback, useEffect, useMemo, useState } from "react";
import toast, { Toaster } from "react-hot-toast";
import { Link } from "react-router-dom";

const STATUS_CONFIG = {
  healthy:  { color: "#00ff88", bg: "rgba(0,255,136,0.08)", label: "HEALTHY",  dot: "#00ff88" },
  degraded: { color: "#ffaa00", bg: "rgba(255,170,0,0.08)",  label: "DEGRADED", dot: "#ffaa00" },
  failed:   { color: "#ff3355", bg: "rgba(255,51,85,0.08)",  label: "FAILED",   dot: "#ff3355" },
  captured: { color: "#4488ff", bg: "rgba(68,136,255,0.08)", label: "CAPTURED", dot: "#4488ff" },
  pending:  { color: "#8888aa", bg: "rgba(102,102,119,0.08)",label: "PENDING",  dot: "#8888aa" },
};

const SEVERITY_COLOR = {
  critical: "#ff3355",
  warn:     "#ffaa00",
  info:     "#4488ff",
  high:     "#ffaa00",
  medium:   "#ffdd44",
  low:      "#4488ff",
};

const RISK_COLOR = { low: "#00ff88", medium: "#ffaa00", high: "#ff3355" };

const VM_STATUS_MAP = {
  discovered:         { preStatus: "pending",  postStatus: "pending" },
  baseline_captured:  { preStatus: "captured", postStatus: "pending" },
  migrated:           { preStatus: "captured", postStatus: "pending" },
  validated:          { preStatus: "captured", postStatus: "healthy" },
  failed:             { preStatus: "captured", postStatus: "failed"  },
};

const VERDICT_TO_STATUS = { pass: "healthy", warn: "degraded", fail: "failed" };

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
    cpu: null, mem: null, disk: null,
    vsphereNetworks: Array.isArray(vm.vsphere_networks) ? vm.vsphere_networks : [],
    vsphereDatastores: Array.isArray(vm.vsphere_datastores) ? vm.vsphere_datastores : [],
    targetNamespace: vm.target_namespace ?? null,
    targetStorageClass: vm.target_storage_class ?? null,
    targetNetworkAttachment: vm.target_network_attachment ?? null,
  };
}

const fmt = (v) => (v === null || v === undefined || v === "" ? "—" : v);

async function fetchJSON(url, { signal, method = "GET", body } = {}) {
  const opts = { signal, method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 404) return { status: 404, data: null };
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail ?? ""; } catch { /* ignore */ }
    throw new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`);
  }
  if (res.status === 204) return { status: 204, data: null };
  return { status: res.status, data: await res.json() };
}

// ---------- Presentational components ----------

const StatusBadge = ({ status }) => {
  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.pending;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 8,
      padding: "4px 12px", borderRadius: 2,
      background: cfg.bg, border: `1px solid ${cfg.color}55`,
      fontSize: 12, fontFamily: "'Share Tech Mono', monospace",
      color: cfg.color, letterSpacing: "0.06em", fontWeight: 700,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: cfg.color, boxShadow: `0 0 6px ${cfg.color}` }} />
      {cfg.label}
    </span>
  );
};

const SeverityTag = ({ s }) => {
  const color = SEVERITY_COLOR[s] || "#aaaacc";
  return (
    <span style={{
      fontSize: 11, fontFamily: "'Share Tech Mono', monospace",
      color, border: `1px solid ${color}66`,
      padding: "3px 9px", borderRadius: 2, letterSpacing: "0.06em",
      textTransform: "uppercase", fontWeight: 700,
    }}>{s || "—"}</span>
  );
};

// Metric labels are short human descriptors → Barlow. Values stay monospace
// because they're almost always technical data (IPs, IDs, sizes, OS names).
const Metric = ({ label, value }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
    <span style={{
      fontSize: 11, color: "#8888aa", fontFamily: "'Barlow', sans-serif",
      letterSpacing: "0.08em", textTransform: "uppercase", fontWeight: 600,
    }}>{label}</span>
    <span style={{ fontSize: 14, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{value}</span>
  </div>
);

// Per-wave Download MTV YAML button + applied-with popover. The download is
// streamed straight from the API endpoint (which sets Content-Disposition),
// and the popover surfaces the kubectl/oc one-liner so operators don't have
// to bounce to the docs.
function WaveMTVDownload({ planId, waveNumber }) {
  const [downloading, setDownloading] = useState(false);
  const [showHint, setShowHint] = useState(false);
  const filename = `wave-${waveNumber}-plan-${planId}.yaml`;

  const onDownload = async () => {
    setDownloading(true);
    try {
      const res = await fetch(`/api/plans/${planId}/waves/${waveNumber}/mtv-yaml`);
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json())?.detail ?? ""; } catch { /* ignore */ }
        throw new Error(detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setShowHint(true);
    } catch (e) {
      toast.error(e.message || "Failed to download MTV YAML", TOAST_OPTS);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div style={{ position: "relative", display: "inline-flex", alignItems: "center", gap: 6 }}>
      <button onClick={onDownload} disabled={downloading}
        style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          background: "transparent", border: "1px solid #4488ff",
          color: downloading ? "#7788aa" : "#aaccff",
          padding: "7px 14px", fontSize: 12,
          fontFamily: "'Barlow', sans-serif",
          letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          cursor: downloading ? "wait" : "pointer", whiteSpace: "nowrap",
        }}>
        {downloading ? <Spinner size={12}/> : "↓"} MTV YAML
      </button>
      <button
        type="button"
        onClick={() => setShowHint((s) => !s)}
        onMouseEnter={() => setShowHint(true)}
        onMouseLeave={() => setShowHint(false)}
        aria-label="How to apply this YAML"
        style={{
          background: "transparent", border: "1px solid #3a3a55",
          color: "#aaaacc", width: 24, height: 24, padding: 0,
          fontSize: 13, fontFamily: "'Barlow', sans-serif",
          cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center",
        }}>
        ⓘ
      </button>
      {showHint && (
        <div style={{
          position: "absolute", top: "100%", right: 0, marginTop: 8,
          background: "#0a0a18", border: "1px solid #2a2a44",
          padding: "14px 16px", zIndex: 50, minWidth: 320,
          boxShadow: "0 12px 28px rgba(0,0,0,0.6)",
        }}>
          <div style={{
            fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
            fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
            fontWeight: 700, marginBottom: 8,
          }}>
            Apply with
          </div>
          <code style={{
            display: "block",
            fontSize: 13, color: "#eeeeff", fontFamily: "'Share Tech Mono', monospace",
            background: "#07070f", padding: "9px 12px", border: "1px solid #1a1a2e",
            wordBreak: "break-all", lineHeight: 1.5,
          }}>
            oc apply -f {filename}
          </code>
          <div style={{
            fontSize: 13, color: "#aaaacc", marginTop: 10,
            fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
          }}>
            Review the rendered NetworkMap, StorageMap, and Plan resources
            before applying. The Plan defaults to a warm migration.
          </div>
        </div>
      )}
    </div>
  );
}

// MTV mapping rendered as a compact section under the inventory metric grid.
// Renders nothing when the VM has no source/target mapping at all — keeps
// the card height stable for VMs operators haven't filled out yet.
const VMMTVMapping = ({ vm }) => {
  const networks = vm.vsphereNetworks || [];
  const datastores = vm.vsphereDatastores || [];
  const hasSource = networks.length > 0 || datastores.length > 0;
  const hasTarget = vm.targetNamespace || vm.targetStorageClass || vm.targetNetworkAttachment;
  if (!hasSource && !hasTarget) return null;

  const row = (label, value) => (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginTop: 6 }}>
      <span style={{
        fontSize: 11, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
        letterSpacing: "0.08em", textTransform: "uppercase",
        fontWeight: 600, flexShrink: 0,
      }}>{label}</span>
      <span style={{
        fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace",
        textAlign: "right", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
      }} title={value}>{value || "—"}</span>
    </div>
  );

  return (
    <div style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid #14142a" }}>
      <div style={{
        fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
        fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
        fontWeight: 700, marginBottom: 6,
      }}>
        MTV Mapping
      </div>
      {networks.length > 0 && row("Networks", networks.join(", "))}
      {datastores.length > 0 && row("Datastores", datastores.join(", "))}
      {vm.targetNamespace && row("→ Namespace", vm.targetNamespace)}
      {vm.targetStorageClass && row("→ StorageClass", vm.targetStorageClass)}
      {vm.targetNetworkAttachment && row("→ NAD", vm.targetNetworkAttachment)}
    </div>
  );
};

// Skeleton bar — animated shimmer for loading rows
const Shimmer = ({ width = "100%", height = 12 }) => (
  <div style={{
    height, width,
    background: "linear-gradient(90deg, #14142a 0%, #2a2a44 50%, #14142a 100%)",
    backgroundSize: "200% 100%",
    animation: "shimmer 1.4s ease-in-out infinite",
    borderRadius: 2,
  }}/>
);

const SkeletonRow = ({ widths }) => (
  <div style={{
    display: "grid",
    gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
    padding: "14px 16px", borderBottom: "1px solid #0f0f1e",
    alignItems: "center", gap: 12,
  }}>
    {widths.map((w, i) => <Shimmer key={i} width={w}/>)}
  </div>
);

const TableSkeleton = ({ rows = 5 }) => {
  const widthSets = [
    ["75%", "60%", "70%", "40%", "40%", "50%", "65%"],
    ["60%", "70%", "55%", "35%", "45%", "40%", "55%"],
    ["80%", "55%", "65%", "45%", "40%", "55%", "65%"],
    ["65%", "65%", "70%", "40%", "50%", "45%", "60%"],
    ["72%", "62%", "60%", "35%", "40%", "55%", "65%"],
  ];
  return (
    <div style={{ border: "1px solid #1a1a2e" }}>
      <div style={{
        display: "grid", gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
        padding: "12px 18px", borderBottom: "1px solid #1a1a2e",
        background: "#0a0a16",
      }}>
        {["VM Name", "Role", "IP Address", "vCPU", "Memory", "Disk", "Status"].map(h => (
          <span key={h} style={{
            fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
            fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
            fontWeight: 700,
          }}>{h}</span>
        ))}
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <SkeletonRow key={i} widths={widthSets[i % widthSets.length]} />
      ))}
    </div>
  );
};

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
      transition: "all 0.15s",
    }}>
    {children}
  </button>
);

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

const EmptyState = ({ icon = "◌", title, description, ctaLabel, onCta }) => (
  <div style={{
    border: "1px dashed #2a2a44",
    background: "linear-gradient(180deg, #0a0a18 0%, #07070f 100%)",
    padding: "56px 32px",
    textAlign: "center",
  }}>
    <div style={{ fontSize: 44, color: "#3a3a55", marginBottom: 16, lineHeight: 1 }}>{icon}</div>
    <div style={{
      fontSize: 18, color: "#eeeeff", fontFamily: "'Barlow', sans-serif",
      fontWeight: 700, marginBottom: 10, letterSpacing: "0.02em",
    }}>{title}</div>
    <div style={{
      fontSize: 15, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
      maxWidth: 520, margin: "0 auto 24px", lineHeight: 1.6,
    }}>{description}</div>
    {ctaLabel && (
      <PrimaryButton onClick={onCta}>+ {ctaLabel}</PrimaryButton>
    )}
  </div>
);

const ErrorState = ({ title = "Something broke", message, onRetry, retrying }) => (
  <div style={{
    border: "1px solid #ff335555",
    background: "rgba(255,51,85,0.06)",
    padding: "24px 26px",
    display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 20,
  }}>
    <div style={{ flex: 1 }}>
      <div style={{
        fontSize: 11, color: "#ff5577", letterSpacing: "0.08em",
        fontFamily: "'Barlow', sans-serif", fontWeight: 700, marginBottom: 8,
      }}>ERROR</div>
      <div style={{
        fontSize: 18, color: "#eeeeff", fontFamily: "'Barlow', sans-serif",
        fontWeight: 700, marginBottom: 6,
      }}>{title}</div>
      <div style={{
        fontSize: 14, color: "#ccaaaa", fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
      }}>{message}</div>
    </div>
    {onRetry && (
      <button onClick={onRetry} disabled={retrying}
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          background: "transparent", border: "1px solid #ff335588",
          color: retrying ? "#996666" : "#ff99aa",
          padding: "10px 18px", fontSize: 12,
          fontFamily: "'Barlow', sans-serif",
          letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          cursor: retrying ? "wait" : "pointer", whiteSpace: "nowrap",
          flexShrink: 0,
        }}>
        {retrying ? <Spinner size={12} color="#ff3355"/> : "↻"} Retry
      </button>
    )}
  </div>
);

const Notice = ({ children, tone = "info" }) => {
  const color = tone === "error" ? "#ff5577" : tone === "warn" ? "#ffbb33" : "#5599ff";
  const labelText = tone === "error" ? "ERROR" : tone === "warn" ? "NOTICE" : "INFO";
  return (
    <div style={{
      padding: "14px 18px", border: `1px solid ${color}55`,
      background: `${color}14`, fontSize: 14, color: "#ccccee",
      fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
    }}>
      <span style={{
        color, letterSpacing: "0.08em", marginRight: 10,
        fontFamily: "'Barlow', sans-serif", fontSize: 11, fontWeight: 700,
      }}>
        {labelText}
      </span>
      {children}
    </div>
  );
};

// ---------- Modal ----------

function Modal({ open, onClose, title, children, footer, width = 520 }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <div onClick={onClose} style={{
        position: "fixed", inset: 0, background: "rgba(2,2,8,0.78)",
        backdropFilter: "blur(2px)", zIndex: 1000,
      }}/>
      <div role="dialog" aria-modal="true" style={{
        position: "fixed", top: "50%", left: "50%",
        transform: "translate(-50%, -50%)",
        width, maxWidth: "calc(100vw - 32px)",
        maxHeight: "calc(100vh - 64px)",
        background: "#0a0a18", border: "1px solid #2a2a44",
        boxShadow: "0 40px 80px rgba(0,0,0,0.6)",
        zIndex: 1001, display: "flex", flexDirection: "column",
      }}>
        <div style={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          padding: "20px 24px", borderBottom: "1px solid #1a1a2e",
        }}>
          <div style={{
            fontSize: 16, fontFamily: "'Barlow', sans-serif",
            fontWeight: 700, color: "#eeeeff", letterSpacing: "0.04em",
          }}>{title}</div>
          <button onClick={onClose} aria-label="Close"
            style={{
              background: "transparent", border: "none", color: "#aaaacc",
              fontSize: 22, cursor: "pointer", lineHeight: 1, padding: 4,
            }}>×</button>
        </div>
        <div style={{ padding: 24, overflowY: "auto", flex: 1 }}>
          {children}
        </div>
        {footer && (
          <div style={{
            padding: "16px 24px", borderTop: "1px solid #1a1a2e",
            display: "flex", justifyContent: "flex-end", gap: 10,
            background: "#080814",
          }}>{footer}</div>
        )}
      </div>
    </>
  );
}

const FormField = ({ label, hint, children, required }) => (
  <label style={{ display: "flex", flexDirection: "column", gap: 7, marginBottom: 18 }}>
    <span style={{
      fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
      fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
    }}>
      {label}{required && <span style={{ color: "#ff5577", marginLeft: 4 }}>*</span>}
    </span>
    {children}
    {hint && (
      <span style={{
        fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
        lineHeight: 1.5,
      }}>{hint}</span>
    )}
  </label>
);

// Inputs accept technical-ish data (hostnames, IPs, usernames) so values
// stay monospace; the placeholder/empty state uses a slightly lighter shade
// to keep contrast comfortable against #07070f.
const inputStyle = {
  background: "#07070f",
  border: "1px solid #2a2a44",
  color: "#eeeeff",
  fontFamily: "'Share Tech Mono', monospace",
  fontSize: 14,
  padding: "11px 13px",
  outline: "none",
  width: "100%",
};

// ---------- Enroll VMs modal (manual / CSV / RVTools XLSX) ----------

// Map any of these header aliases (case + non-alnum-insensitive) to the
// VM payload field. Tuned to handle both VirtValidate-native CSV and the
// RVTools "vInfo" sheet column names.
const HEADER_ALIASES = {
  name:                       ["name", "vm", "vmname"],
  source_hostname:            ["hostname", "sourcehostname", "dnsname", "fqdn"],
  ip_address:                 ["ip", "ipaddress", "primaryipaddress"],
  os_family:                  ["os", "osfamily", "osaccordingtotheconfigurationfile", "guestos", "guestosfullname"],
  role:                       ["role", "tag", "annotation"],
  ssh_user:                   ["sshuser", "sshusername", "username", "user"],
  notes:                      ["notes", "comment", "comments", "annotation_notes"],
  vsphere_networks:           ["vspherenetworks", "networks", "portgroups", "portgroup", "network"],
  vsphere_datastores:         ["vspheredatastores", "datastores", "datastore"],
  target_namespace:           ["targetnamespace", "namespace"],
  target_storage_class:       ["targetstorageclass", "storageclass"],
  target_network_attachment:  ["targetnetworkattachment", "networkattachment", "nad"],
};

// vsphere_networks and vsphere_datastores are list-typed; CSV operators put
// multiple values in one cell separated by ";" (and tolerate a stray ","
// since RVTools sometimes uses that).
const _splitList = (raw) => {
  if (!raw) return [];
  return String(raw)
    .split(/[;,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
};

const _normKey = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

function _shortenOSFamily(raw) {
  if (!raw) return null;
  const lower = raw.toLowerCase();
  if (lower.includes("red hat") || lower.includes("rhel")) return "rhel";
  if (lower.includes("ubuntu")) return "ubuntu";
  if (lower.includes("centos")) return "centos";
  if (lower.includes("rocky")) return "rocky";
  if (lower.includes("alma")) return "almalinux";
  if (lower.includes("suse") || lower.includes("sles")) return "sles";
  if (lower.includes("debian")) return "debian";
  if (lower.includes("windows")) return "windows";
  if (lower.includes("oracle")) return "oracle";
  return raw.slice(0, 32);
}

function rowToPayload(rawRow) {
  const lookup = {};
  for (const [k, v] of Object.entries(rawRow)) lookup[_normKey(k)] = v;
  const get = (field) => {
    for (const a of HEADER_ALIASES[field]) {
      const k = _normKey(a);
      if (lookup[k] != null && String(lookup[k]).trim() !== "") return String(lookup[k]).trim();
    }
    return null;
  };
  const sourceHost = get("source_hostname") || "";
  const name = get("name") || (sourceHost ? sourceHost.split(".")[0] : "");
  if (!name && !sourceHost) return null;
  return {
    name: (name || sourceHost).slice(0, 255),
    source_hostname: (sourceHost || name).slice(0, 255),
    ip_address: get("ip_address"),
    os_family: _shortenOSFamily(get("os_family")),
    role: get("role"),
    ssh_user: get("ssh_user"),
    notes: get("notes"),
    vsphere_networks: _splitList(get("vsphere_networks")),
    vsphere_datastores: _splitList(get("vsphere_datastores")),
    target_namespace: get("target_namespace"),
    target_storage_class: get("target_storage_class"),
    target_network_attachment: get("target_network_attachment"),
  };
}

// Minimal CSV parser supporting quoted fields with embedded commas + escaped
// double-quotes. Strips a leading BOM if present.
function parseCSV(text) {
  const trimmed = text.replace(/^\uFEFF/, "");
  const lines = trimmed.split(/\r?\n/).filter((l) => l.length > 0);
  if (lines.length < 2) return { headers: [], rows: [] };
  const parseLine = (line) => {
    const out = [];
    let cur = "", inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"') {
          if (line[i + 1] === '"') { cur += '"'; i++; }
          else { inQ = false; }
        } else cur += c;
      } else if (c === ",") { out.push(cur); cur = ""; }
      else if (c === '"' && cur === "") { inQ = true; }
      else cur += c;
    }
    out.push(cur);
    return out;
  };
  const headers = parseLine(lines[0]).map((h) => h.trim());
  const rows = lines.slice(1).map((line) => {
    const cells = parseLine(line);
    const o = {};
    headers.forEach((h, i) => { o[h] = (cells[i] ?? "").trim(); });
    return o;
  });
  return { headers, rows };
}

async function parseXLSX(file) {
  // Lazy-load the SheetJS bundle so it isn't included in the initial chunk.
  const XLSX = await import("xlsx");
  const ab = await file.arrayBuffer();
  const wb = XLSX.read(ab, { type: "array" });
  // RVTools' VM sheet is "vInfo" — prefer it, otherwise fall back to first.
  const sheetName =
    wb.SheetNames.find((n) => n.toLowerCase() === "vinfo") || wb.SheetNames[0];
  const sheet = wb.Sheets[sheetName];
  const rows = XLSX.utils.sheet_to_json(sheet, { defval: "" });
  return { sheetName, rows };
}

// "Download example CSV template" — streams the file from the API and saves
// it via a transient anchor. Disabled while the parent tab is submitting so
// operators can't accidentally fetch mid-enroll.
function CSVTemplateDownload({ disabled }) {
  const [busy, setBusy] = useState(false);

  const onDownload = async () => {
    setBusy(true);
    try {
      const res = await fetch("/api/templates/csv");
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json())?.detail ?? ""; } catch { /* ignore */ }
        throw new Error(detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "vm-inventory-template.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e.message || "Failed to download template", TOAST_OPTS);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
      marginBottom: 16, padding: "16px 20px",
      border: "1px solid #4488ff66", background: "rgba(68,136,255,0.06)",
    }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{
          fontSize: 14, color: "#eeeeff", fontFamily: "'Barlow', sans-serif", fontWeight: 600,
          lineHeight: 1.5,
        }}>
          New here? Start with the example template.
        </div>
        <div style={{
          fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
          lineHeight: 1.5,
        }}>
          Five realistic VM rows pre-filled with all the columns the planner and MTV exporter need.
        </div>
      </div>
      <button onClick={onDownload} disabled={disabled || busy}
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          background: "transparent", border: "1px solid #4488ff",
          color: busy ? "#7788aa" : "#aaccff",
          padding: "10px 18px", fontSize: 12,
          fontFamily: "'Barlow', sans-serif",
          letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          cursor: (disabled || busy) ? "not-allowed" : "pointer", whiteSpace: "nowrap",
        }}>
        {busy ? <Spinner size={12}/> : "↓"} Download example CSV
      </button>
    </div>
  );
}

const CSV_REQUIRED_COLS = ["hostname", "ip_address", "ssh_username"];

const CSV_OPTIONAL_COLS = [
  ["ssh_port", "default 22"],
  ["current_platform", "vmware | ocp-virt | other"],
  ["role", "database, app, lb, …"],
  ["environment", "prod | staging | dev"],
  ["owner", "team or contact"],
  ["vsphere_networks", "semicolon-separated list"],
  ["vsphere_datastores", "semicolon-separated list"],
  ["target_namespace", "destination OCP namespace"],
  ["target_storage_class", "destination StorageClass"],
  ["target_network_attachment", "destination NAD"],
  ["notes", "free-form"],
];

// Column reference shown below the upload picker on the CSV tab. Mirrors
// what's documented in README.md under "VM inventory CSV format" so that
// operators don't have to bounce out of the modal.
function CSVColumnDocs() {
  const sectionLabel = (label) => (
    <div style={{
      fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
      fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
      fontWeight: 700, marginBottom: 10,
    }}>
      {label}
    </div>
  );

  const col = (name, hint, required) => (
    <div key={name} style={{
      display: "flex", justifyContent: "space-between", gap: 12,
      padding: "7px 0", borderBottom: "1px solid #14142a", alignItems: "baseline",
    }}>
      <code style={{
        fontSize: 13, color: required ? "#eeeeff" : "#ccccee",
        fontFamily: "'Share Tech Mono', monospace", fontWeight: required ? 700 : 400,
      }}>
        {name}
      </code>
      {hint && (
        <span style={{
          fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
          textAlign: "right",
        }}>
          {hint}
        </span>
      )}
    </div>
  );

  return (
    <div style={{
      marginBottom: 18, padding: "18px 20px",
      border: "1px solid #1a1a2e", background: "#07070f",
    }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 28 }}>
        <div>
          {sectionLabel("Required")}
          {CSV_REQUIRED_COLS.map((name) => col(name, "", true))}
        </div>
        <div>
          {sectionLabel("Optional")}
          {CSV_OPTIONAL_COLS.map(([name, hint]) => col(name, hint, false))}
        </div>
      </div>
      <div style={{
        marginTop: 14, fontSize: 13, color: "#aaaacc",
        fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
      }}>
        Lists in <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>vsphere_networks</code> and{" "}
        <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>vsphere_datastores</code>{" "}
        go inside one CSV cell, separated by{" "}
        <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>;</code> — e.g.{" "}
        <code style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>VM Network;DB Backend</code>.
      </div>
    </div>
  );
}

const PREVIEW_COLS = "1.2fr 1.4fr 0.9fr 0.7fr 0.8fr 1.2fr 1.2fr 1.1fr";

const PreviewTable = ({ payloads }) => (
  <div style={{ border: "1px solid #1a1a2e", maxHeight: 320, overflowY: "auto" }}>
    <div style={{
      display: "grid",
      gridTemplateColumns: PREVIEW_COLS,
      padding: "10px 14px", borderBottom: "1px solid #1a1a2e",
      background: "#0a0a16", fontSize: 11, color: "#aaaacc",
      letterSpacing: "0.08em", fontFamily: "'Barlow', sans-serif",
      textTransform: "uppercase", fontWeight: 700,
    }}>
      <span>Name</span><span>Hostname</span><span>IP</span>
      <span>OS</span><span>Role</span><span>Networks</span>
      <span>Datastores</span><span>Target NS</span>
    </div>
    {payloads.slice(0, 50).map((p, i) => (
      <div key={i} style={{
        display: "grid",
        gridTemplateColumns: PREVIEW_COLS,
        padding: "10px 14px", borderBottom: "1px solid #0f0f1e",
        fontSize: 13, color: "#ccccee",
        fontFamily: "'Share Tech Mono', monospace",
      }}>
        <span style={{ color: "#eeeeff", fontWeight: 600 }}>{p.name || "—"}</span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.source_hostname || "—"}</span>
        <span>{p.ip_address || "—"}</span>
        <span>{p.os_family || "—"}</span>
        <span>{p.role || "—"}</span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={(p.vsphere_networks || []).join(", ")}>
          {(p.vsphere_networks || []).join(", ") || "—"}
        </span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={(p.vsphere_datastores || []).join(", ")}>
          {(p.vsphere_datastores || []).join(", ") || "—"}
        </span>
        <span>{p.target_namespace || "—"}</span>
      </div>
    ))}
    {payloads.length > 50 && (
      <div style={{ padding: "10px 14px", fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif" }}>
        … and {payloads.length - 50} more
      </div>
    )}
  </div>
);

const TabButton = ({ active, onClick, children }) => (
  <button onClick={onClick} type="button"
    style={{
      flex: 1, background: "none", border: "none", cursor: "pointer",
      padding: "12px 16px", fontSize: 13,
      fontFamily: "'Barlow', sans-serif", letterSpacing: "0.08em",
      textTransform: "uppercase", fontWeight: 700,
      color: active ? "#88aaff" : "#aaaacc",
      borderBottom: active ? "2px solid #4488ff" : "2px solid transparent",
      transition: "all 0.15s",
    }}>
    {children}
  </button>
);

// --- Manual single-VM tab ---

function ManualTab({ onSubmit, submitting }) {
  const [hostname, setHostname] = useState("");
  const [ip, setIp] = useState("");
  const [user, setUser] = useState("");
  const [role, setRole] = useState("");

  const canSubmit = hostname.trim().length > 0;

  const submit = (e) => {
    e?.preventDefault();
    if (!canSubmit) return;
    const host = hostname.trim();
    onSubmit({
      name: host.split(".")[0] || host,
      source_hostname: host,
      ip_address: ip.trim() || null,
      ssh_user: user.trim() || null,
      role: role.trim() || null,
    });
  };

  return (
    <form id="enroll-manual-form" onSubmit={submit}>
      <FormField label="Hostname" required hint="Used as both the VM name and the SSH target. e.g. db-01.vmware.local">
        <input style={inputStyle} value={hostname} onChange={(e) => setHostname(e.target.value)} autoFocus required maxLength={255}/>
      </FormField>
      <FormField label="IP Address" hint="Optional — IPv4 or IPv6">
        <input style={inputStyle} value={ip} onChange={(e) => setIp(e.target.value)} maxLength={45}/>
      </FormField>
      <FormField label="SSH Username" hint="Defaults to virtvalidate when blank">
        <input style={inputStyle} value={user} onChange={(e) => setUser(e.target.value)} maxLength={64}/>
      </FormField>
      <FormField label="Role" hint="database, app, lb, cache, …">
        <input style={inputStyle} value={role} onChange={(e) => setRole(e.target.value)} maxLength={64}/>
      </FormField>
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
        <PrimaryButton type="submit" disabled={submitting || !canSubmit}>
          {submitting && <Spinner size={11}/>}
          {submitting ? "Enrolling…" : "Enroll VM"}
        </PrimaryButton>
      </div>
    </form>
  );
}

// --- Bulk upload tab (shared by CSV + XLSX) ---

function BulkTab({
  kind,            // "csv" | "xlsx"
  onSubmit,
  submitting,
}) {
  const [filename, setFilename] = useState("");
  const [payloads, setPayloads] = useState([]);
  const [errors, setErrors] = useState([]);
  const [parsing, setParsing] = useState(false);
  const [meta, setMeta] = useState(null); // {sheetName} for xlsx

  const reset = () => { setFilename(""); setPayloads([]); setErrors([]); setMeta(null); };

  const onFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file
    if (!file) return;
    setParsing(true);
    setFilename(file.name);
    try {
      let rawRows = [];
      let metaInfo = null;
      if (kind === "csv") {
        const text = await file.text();
        const { rows } = parseCSV(text);
        rawRows = rows;
      } else {
        const { sheetName, rows } = await parseXLSX(file);
        rawRows = rows;
        metaInfo = { sheetName };
      }
      const errs = [];
      const parsed = [];
      rawRows.forEach((row, idx) => {
        const p = rowToPayload(row);
        if (!p) {
          errs.push(`Row ${idx + 2}: missing both name and hostname — skipped`);
          return;
        }
        parsed.push(p);
      });
      setPayloads(parsed);
      setErrors(errs);
      setMeta(metaInfo);
    } catch (err) {
      toast.error(err.message || `Failed to parse ${kind.toUpperCase()} file`, TOAST_OPTS);
      reset();
    } finally {
      setParsing(false);
    }
  };

  const accept = kind === "csv" ? ".csv,text/csv" : ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  const inputId = `enroll-file-${kind}`;

  return (
    <div>
      {kind === "csv" && <CSVTemplateDownload disabled={submitting}/>}

      <div style={{
        display: "flex", alignItems: "center", gap: 14, marginBottom: 18,
        padding: "16px 18px", border: "1px dashed #3a3a55", background: "#07070f",
      }}>
        <input id={inputId} type="file" accept={accept} onChange={onFile} style={{ display: "none" }} disabled={submitting}/>
        <label htmlFor={inputId} style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          background: "#1a1a2e", border: "1px solid #3a3a55", color: "#eeeeff",
          padding: "10px 18px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
          letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
          cursor: submitting ? "not-allowed" : "pointer", whiteSpace: "nowrap",
        }}>
          {parsing ? <Spinner size={12}/> : "📁"} Choose {kind.toUpperCase()} file
        </label>
        <div style={{
          flex: 1, fontSize: 14, color: "#aaaacc",
          fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>
          {filename || (kind === "csv"
            ? "Drop a CSV that matches the template below"
            : "RVTools export — vInfo sheet preferred")}
        </div>
        {payloads.length > 0 && (
          <SecondaryButton onClick={reset} disabled={submitting}>Clear</SecondaryButton>
        )}
      </div>

      {kind === "csv" && <CSVColumnDocs/>}

      {kind === "xlsx" && meta?.sheetName && (
        <div style={{
          fontSize: 13, color: "#aaaacc", marginBottom: 14,
          fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
        }}>
          Reading sheet ·{" "}
          <span style={{ color: "#eeeeff", fontFamily: "'Share Tech Mono', monospace" }}>
            {meta.sheetName}
          </span>
        </div>
      )}

      {errors.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <Notice tone="warn">
            {errors.length} row{errors.length === 1 ? "" : "s"} skipped during parse — see details below.
          </Notice>
          <div style={{
            marginTop: 8, padding: "10px 14px", maxHeight: 120, overflowY: "auto",
            background: "#07070f", border: "1px solid #1a1a2e",
            fontSize: 13, color: "#ccaaaa", fontFamily: "'Share Tech Mono', monospace",
            lineHeight: 1.6,
          }}>
            {errors.slice(0, 20).map((e, i) => <div key={i}>{e}</div>)}
            {errors.length > 20 && <div>… and {errors.length - 20} more</div>}
          </div>
        </div>
      )}

      {payloads.length > 0 ? (
        <>
          <div style={{
            fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
            marginBottom: 8, fontFamily: "'Barlow', sans-serif",
            textTransform: "uppercase", fontWeight: 700,
          }}>
            Preview · {payloads.length} VM{payloads.length === 1 ? "" : "s"}
          </div>
          <PreviewTable payloads={payloads}/>

          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 18 }}>
            <PrimaryButton onClick={() => onSubmit(payloads)} disabled={submitting || payloads.length === 0}>
              {submitting && <Spinner size={12}/>}
              {submitting ? "Enrolling…" : `Enroll ${payloads.length} VM${payloads.length === 1 ? "" : "s"}`}
            </PrimaryButton>
          </div>
        </>
      ) : (
        !parsing && filename && (
          <Notice tone="warn">No usable rows found in this file.</Notice>
        )
      )}
    </div>
  );
}

function EnrollVMsModal({ open, onClose, onCreated }) {
  const [tab, setTab] = useState("manual");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) { setTab("manual"); setSubmitting(false); }
  }, [open]);

  const submitSingle = async (payload) => {
    setSubmitting(true);
    const promise = fetchJSON("/api/vms", { method: "POST", body: payload });
    try {
      await toast.promise(promise, {
        loading: "Enrolling VM…",
        success: (r) => `VM "${r.data.name}" enrolled`,
        error: (e) => e.message || "Failed to enroll VM",
      }, TOAST_OPTS);
      onCreated();
      onClose();
    } catch { /* toast surfaced */ }
    finally { setSubmitting(false); }
  };

  const submitBulk = async (payloads) => {
    setSubmitting(true);
    const promise = fetchJSON("/api/vms/bulk", { method: "POST", body: { vms: payloads } });
    try {
      await toast.promise(promise, {
        loading: `Enrolling ${payloads.length} VMs…`,
        success: (r) => {
          const c = r.data.created.length;
          const s = r.data.skipped.length;
          return s === 0 ? `Enrolled ${c} VMs` : `Enrolled ${c} of ${r.data.total} (${s} skipped)`;
        },
        error: (e) => e.message || "Bulk enrollment failed",
      }, TOAST_OPTS);
      onCreated();
      onClose();
    } catch { /* toast surfaced */ }
    finally { setSubmitting(false); }
  };

  return (
    <Modal
      open={open}
      onClose={submitting ? () => {} : onClose}
      title="Add VMs"
      width={760}
      footer={
        <SecondaryButton onClick={onClose} disabled={submitting}>Close</SecondaryButton>
      }
    >
      <div style={{ display: "flex", borderBottom: "1px solid #1a1a2e", marginBottom: 20 }}>
        <TabButton active={tab === "manual"} onClick={() => setTab("manual")}>Manual</TabButton>
        <TabButton active={tab === "csv"}    onClick={() => setTab("csv")}>CSV upload</TabButton>
        <TabButton active={tab === "xlsx"}   onClick={() => setTab("xlsx")}>RVTools XLSX</TabButton>
      </div>

      {tab === "manual" && <ManualTab onSubmit={submitSingle} submitting={submitting}/>}
      {tab === "csv"    && <BulkTab kind="csv"  onSubmit={submitBulk} submitting={submitting}/>}
      {tab === "xlsx"   && <BulkTab kind="xlsx" onSubmit={submitBulk} submitting={submitting}/>}
    </Modal>
  );
}

// ---------- Generate Plan modal ----------

function GeneratePlanModal({ open, onClose, vms, onCreated }) {
  const [selected, setSelected] = useState(new Set());
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setSelected(new Set(vms.map((v) => v.id)));
      setSubmitting(false);
    }
  }, [open, vms]);

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const onSubmit = async (e) => {
    e.preventDefault();
    if (selected.size === 0) return;
    setSubmitting(true);
    const promise = fetchJSON("/api/plans", {
      method: "POST",
      body: { vm_ids: Array.from(selected) },
    });
    try {
      await toast.promise(promise, {
        loading: "Generating plan via Ollama…",
        success: (r) => `Plan #${r.data.id} generated (${r.data.waves.length} waves)`,
        error: (e) => e.message || "Plan generation failed",
      }, TOAST_OPTS);
      onCreated();
      onClose();
    } catch {
      // toast surfaced
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={submitting ? () => {} : onClose}
      title="Generate Migration Plan"
      footer={
        <>
          <SecondaryButton onClick={onClose} disabled={submitting}>Cancel</SecondaryButton>
          <PrimaryButton onClick={onSubmit} disabled={submitting || selected.size === 0}>
            {submitting && <Spinner size={12}/>}
            {submitting ? "Generating…" : `Generate (${selected.size})`}
          </PrimaryButton>
        </>
      }
    >
      <div style={{
        fontSize: 14, color: "#aaaacc", fontFamily: "'Barlow', sans-serif",
        lineHeight: 1.6, marginBottom: 18,
      }}>
        Select the VMs to include. The local Ollama model will infer roles and dependencies, then group them into ordered migration waves.
      </div>
      {vms.length === 0 ? (
        <Notice tone="warn">No VMs available. Enroll at least one before generating a plan.</Notice>
      ) : (
        <div style={{ border: "1px solid #1a1a2e", maxHeight: 380, overflowY: "auto" }}>
          {vms.map((vm) => {
            const checked = selected.has(vm.id);
            return (
              <label key={vm.id} style={{
                display: "flex", alignItems: "center", gap: 14,
                padding: "14px 18px", borderBottom: "1px solid #0f0f1e",
                cursor: "pointer",
                background: checked ? "rgba(68,136,255,0.06)" : "transparent",
              }}>
                <input type="checkbox" checked={checked} onChange={() => toggle(vm.id)}
                  style={{ accentColor: "#4488ff", width: 16, height: 16 }}/>
                <div style={{ flex: 1 }}>
                  <div style={{
                    fontSize: 14, color: "#eeeeff", fontFamily: "'Barlow', sans-serif",
                    fontWeight: 600,
                  }}>{vm.name}</div>
                  <div style={{
                    fontSize: 12, color: "#aaaacc", marginTop: 4,
                    fontFamily: "'Barlow', sans-serif",
                  }}>
                    {vm.role} ·{" "}
                    <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>{vm.os}</span> ·{" "}
                    <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#ccccee" }}>{vm.ip}</span>
                  </div>
                </div>
                <StatusBadge status={vm.postStatus}/>
              </label>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

// ---------- Main component ----------

export default function VirtValidate() {
  const [activeTab, setActiveTab] = useState("validation");
  const [selectedVMId, setSelectedVMId] = useState(null);
  const [addVMOpen, setAddVMOpen] = useState(false);
  const [planModalOpen, setPlanModalOpen] = useState(false);

  // Inventory
  const [vms, setVms] = useState([]);
  const [vmsLoading, setVmsLoading] = useState(true);
  const [vmsError, setVmsError] = useState(null);
  const [vmsRetrying, setVmsRetrying] = useState(false);

  // Detail
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(null);

  // Validation
  const [validation, setValidation] = useState(null);
  const [validationLoading, setValidationLoading] = useState(false);
  const [validationError, setValidationError] = useState(null);
  const [validationMissing, setValidationMissing] = useState(false);
  const [validationRunning, setValidationRunning] = useState(false);

  // Plan
  const [plan, setPlan] = useState(null);
  const [planLoading, setPlanLoading] = useState(true);
  const [planError, setPlanError] = useState(null);
  const [planRetrying, setPlanRetrying] = useState(false);

  // Audit log
  const [auditEntries, setAuditEntries] = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState(null);
  const [auditRetrying, setAuditRetrying] = useState(false);
  const [auditActionFilter, setAuditActionFilter] = useState("");
  const [auditResourceFilter, setAuditResourceFilter] = useState("");

  const loadVMs = useCallback(async ({ retry = false } = {}) => {
    if (retry) setVmsRetrying(true); else setVmsLoading(true);
    try {
      const { data } = await fetchJSON("/api/vms");
      setVms((data || []).map(mapVM));
      setVmsError(null);
    } catch (e) {
      setVmsError(e.message || "Failed to load VMs");
      if (retry) toast.error(e.message || "Retry failed", TOAST_OPTS);
    } finally {
      setVmsLoading(false);
      setVmsRetrying(false);
    }
  }, []);

  const loadPlan = useCallback(async ({ retry = false } = {}) => {
    if (retry) setPlanRetrying(true); else setPlanLoading(true);
    try {
      const { data } = await fetchJSON("/api/plans?limit=1");
      setPlan(Array.isArray(data) && data.length > 0 ? data[0] : null);
      setPlanError(null);
    } catch (e) {
      setPlanError(e.message || "Failed to load plans");
      if (retry) toast.error(e.message || "Retry failed", TOAST_OPTS);
    } finally {
      setPlanLoading(false);
      setPlanRetrying(false);
    }
  }, []);

  const loadAudit = useCallback(async ({ retry = false, action, resourceType } = {}) => {
    if (retry) setAuditRetrying(true); else setAuditLoading(true);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (action) params.set("action", action);
      if (resourceType) params.set("resource_type", resourceType);
      const { data } = await fetchJSON(`/api/audit?${params.toString()}`);
      setAuditEntries(Array.isArray(data) ? data : []);
      setAuditError(null);
    } catch (e) {
      setAuditError(e.message || "Failed to load audit log");
      if (retry) toast.error(e.message || "Retry failed", TOAST_OPTS);
    } finally {
      setAuditLoading(false);
      setAuditRetrying(false);
    }
  }, []);

  const loadVMDetail = useCallback(async (id, ctrl) => {
    setDetailLoading(true);
    setValidationLoading(true);
    setDetailError(null);
    setValidationError(null);
    setValidationMissing(false);
    try {
      const [detailRes, valRes] = await Promise.all([
        fetchJSON(`/api/vms/${id}`, { signal: ctrl?.signal }).catch((e) => ({ error: e })),
        fetchJSON(`/api/vms/${id}/validation/latest`, { signal: ctrl?.signal }).catch((e) => ({ error: e })),
      ]);

      if (detailRes.error) {
        if (detailRes.error.name !== "AbortError") {
          setDetailError(detailRes.error.message || "Failed to load VM detail");
        }
      } else {
        setDetail(detailRes.data);
      }

      if (valRes.error) {
        if (valRes.error.name !== "AbortError") {
          setValidationError(valRes.error.message || "Failed to load validation");
        }
      } else if (valRes.status === 404) {
        setValidationMissing(true);
        setValidation(null);
      } else {
        setValidation(valRes.data);
        setValidationMissing(false);
      }
    } finally {
      setDetailLoading(false);
      setValidationLoading(false);
    }
  }, []);

  // Initial loads
  useEffect(() => { loadVMs(); }, [loadVMs]);
  useEffect(() => { loadPlan(); }, [loadPlan]);

  // Audit log: fetch lazily when tab opens, and re-fetch on filter change.
  useEffect(() => {
    if (activeTab !== "audit log") return;
    loadAudit({ action: auditActionFilter, resourceType: auditResourceFilter });
  }, [activeTab, auditActionFilter, auditResourceFilter, loadAudit]);

  // Refetch on selection change
  useEffect(() => {
    if (selectedVMId == null) {
      setDetail(null);
      setValidation(null);
      setValidationMissing(false);
      setDetailError(null);
      setValidationError(null);
      return;
    }
    const ctrl = new AbortController();
    loadVMDetail(selectedVMId, ctrl);
    return () => ctrl.abort();
  }, [selectedVMId, loadVMDetail]);

  // Clear selection if VM removed
  useEffect(() => {
    if (selectedVMId != null && !vms.some((v) => v.id === selectedVMId)) {
      setSelectedVMId(null);
    }
  }, [vms, selectedVMId]);

  // Sidebar action: simulate kicking off a validation refresh
  const onRunValidation = async () => {
    if (selectedVMId == null) {
      toast("Select a VM first", { ...TOAST_OPTS, icon: "ℹ️" });
      return;
    }
    setValidationRunning(true);
    try {
      // Validation runs are scheduled server-side; here we just refresh
      // the latest result and surface a clear in-progress indicator.
      await loadVMDetail(selectedVMId);
      toast.success("Validation refreshed", TOAST_OPTS);
    } catch (e) {
      toast.error(e.message || "Validation refresh failed", TOAST_OPTS);
    } finally {
      setValidationRunning(false);
    }
  };

  const onCaptureBaseline = () => {
    toast("Baselines are captured by the scheduler at 06:00 / 18:00 UTC", { ...TOAST_OPTS, icon: "ℹ️", duration: 5000 });
  };

  const selectedVM = useMemo(() => vms.find((v) => v.id === selectedVMId) || null, [vms, selectedVMId]);
  const vmNameById = useMemo(() => {
    const m = new Map();
    for (const v of vms) m.set(v.id, v.name);
    return m;
  }, [vms]);

  const { healthy, degraded, failed, pending, total } = useMemo(() => {
    const counts = { healthy: 0, degraded: 0, failed: 0, pending: 0 };
    for (const v of vms) counts[v.postStatus] = (counts[v.postStatus] || 0) + 1;
    return { ...counts, total: vms.length };
  }, [vms]);

  const validatedPct = total === 0 ? 0 : Math.round(((healthy + degraded + failed) / total) * 100);
  const tabs = ["validation", "migration plan", "inventory", "reports", "audit log"];

  const detailPostStatus = validation
    ? VERDICT_TO_STATUS[validation.status] || "pending"
    : selectedVM?.postStatus;

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
        .quick-action:hover:not(:disabled) { border-color: #4488ff44 !important; color: #4488ff !important; }
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
                VirtValidate
              </div>
              <div style={{
                fontSize: 11, color: "#aaaacc", letterSpacing: "0.1em",
                fontFamily: "'Barlow', sans-serif", marginTop: 2,
              }}>
                VM Migration Validation Platform
              </div>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 28 }}>
            <div style={{ display: "flex", gap: 24 }}>
              {[
                { label: "Healthy", val: healthy, color: "#00ff88" },
                { label: "Degraded", val: degraded, color: "#ffaa00" },
                { label: "Failed", val: failed, color: "#ff3355" },
                { label: "Pending", val: pending, color: "#aaaacc" },
              ].map(s => (
                <div key={s.label} style={{ textAlign: "center" }}>
                  <div style={{
                    fontSize: 22, fontWeight: 700, color: s.color,
                    fontFamily: "'Barlow', sans-serif", lineHeight: 1,
                  }}>{s.val}</div>
                  <div style={{
                    fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                    marginTop: 4, fontFamily: "'Barlow', sans-serif",
                    textTransform: "uppercase", fontWeight: 600,
                  }}>{s.label}</div>
                </div>
              ))}
            </div>
            <div style={{ width: 1, height: 36, background: "#1a1a2e" }} />
            <div style={{ textAlign: "right" }}>
              <div style={{
                fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                fontWeight: 600,
              }}>Cluster</div>
              <div style={{
                fontSize: 14, color: "#eeeeff", marginTop: 2,
                fontFamily: "'Share Tech Mono', monospace",
              }}>ocp-virt-prod-01</div>
            </div>
            <div style={{ width: 1, height: 36, background: "#1a1a2e" }} />
            <button
              onClick={() => setAddVMOpen(true)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8,
                background: "#1d3a8a", border: "1px solid #4488ff",
                color: "#eef2ff",
                padding: "10px 18px", fontSize: 12,
                fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: "pointer", whiteSpace: "nowrap",
                transition: "all 0.15s",
              }}
              title="Enroll VMs — manual, CSV, or RVTools XLSX"
            >
              + Add VMs
            </button>
            <Link to="/settings" title="System configuration" style={{
              display: "inline-flex", alignItems: "center", gap: 8,
              background: "transparent", border: "1px solid #3a3a55",
              color: "#aaaacc", textDecoration: "none",
              padding: "10px 16px", fontSize: 12,
              fontFamily: "'Barlow', sans-serif",
              letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
            }}>
              ⚙ Settings
            </Link>
          </div>
        </div>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 0, marginTop: 0 }}>
          {tabs.map(tab => (
            <button key={tab} className="tab-btn" onClick={() => setActiveTab(tab)} style={{
              background: "none", border: "none", cursor: "pointer",
              padding: "12px 22px", fontSize: 13,
              fontFamily: "'Barlow', sans-serif", letterSpacing: "0.08em",
              textTransform: "uppercase", fontWeight: 700,
              color: activeTab === tab ? "#88aaff" : "#aaaacc",
              borderBottom: activeTab === tab ? "2px solid #4488ff" : "2px solid transparent",
              transition: "all 0.15s",
            }}>{tab}</button>
          ))}
        </div>
      </div>

      <div style={{ display: "flex", height: "calc(100vh - 118px)" }}>

        {/* Main content */}
        <div style={{ flex: 1, overflow: "auto", padding: 24 }}>

          {/* VALIDATION TAB */}
          {activeTab === "validation" && (
            <div className="fade-in">
              {/* Progress bar — stays visible even during loading, just empty */}
              <div style={{ marginBottom: 28, padding: "20px 24px", border: "1px solid #1a1a2e", background: "#0a0a18" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                  <span style={{
                    fontSize: 13, color: "#aaaacc", letterSpacing: "0.08em",
                    fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                    fontWeight: 700,
                  }}>Migration Validation Progress</span>
                  <span style={{
                    fontSize: 14, color: "#88aaff",
                    fontFamily: "'Share Tech Mono', monospace", fontWeight: 700,
                  }}>{validatedPct}% validated</span>
                </div>
                <div style={{ height: 6, background: "#111122", borderRadius: 2, overflow: "hidden" }}>
                  <div style={{ display: "flex", height: "100%" }}>
                    <div style={{ width: `${total === 0 ? 0 : (healthy / total) * 100}%`, background: "#00ff88", transition: "width 0.5s" }} />
                    <div style={{ width: `${total === 0 ? 0 : (degraded / total) * 100}%`, background: "#ffaa00", transition: "width 0.5s" }} />
                    <div style={{ width: `${total === 0 ? 0 : (failed / total) * 100}%`, background: "#ff3355", transition: "width 0.5s" }} />
                  </div>
                </div>
              </div>

              {vmsError ? (
                <ErrorState
                  title="Couldn't load VM inventory"
                  message={`${vmsError}. Verify the API is reachable at /api/vms.`}
                  onRetry={() => loadVMs({ retry: true })}
                  retrying={vmsRetrying}
                />
              ) : vmsLoading ? (
                <TableSkeleton rows={5} />
              ) : total === 0 ? (
                <EmptyState
                  icon="◌"
                  title="No VMs enrolled yet"
                  description="VirtValidate validates each VM after migration by SSHing in and diffing live state against the captured baseline. Enroll your first VM to begin."
                  ctaLabel="Add Your First VM"
                  onCta={() => setAddVMOpen(true)}
                />
              ) : (
                <>
                  <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12, gap: 8 }}>
                    <SecondaryButton onClick={() => loadVMs({ retry: true })} disabled={vmsRetrying}>
                      {vmsRetrying ? <Spinner size={11}/> : "↻"} Refresh
                    </SecondaryButton>
                    <PrimaryButton onClick={() => setAddVMOpen(true)}>+ Add VM</PrimaryButton>
                  </div>

                  <div style={{ border: "1px solid #1a1a2e" }}>
                    <div style={{
                      display: "grid", gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
                      padding: "12px 18px", borderBottom: "1px solid #1a1a2e",
                      background: "#0a0a16",
                    }}>
                      {["VM Name", "Role", "IP Address", "vCPU", "Memory", "Disk", "Status"].map(h => (
                        <span key={h} style={{
                          fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                          fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                          fontWeight: 700,
                        }}>{h}</span>
                      ))}
                    </div>
                    {vms.map((vm, i) => (
                      <div key={vm.id} className="vm-row" onClick={() => setSelectedVMId(selectedVMId === vm.id ? null : vm.id)}
                        style={{
                          display: "grid", gridTemplateColumns: "2fr 1.2fr 1fr 0.8fr 0.8fr 0.8fr 1fr",
                          padding: "14px 18px", alignItems: "center",
                          borderBottom: i < vms.length - 1 ? "1px solid #0f0f1e" : "none",
                          background: selectedVMId === vm.id ? "rgba(68,136,255,0.06)" : "transparent",
                          transition: "background 0.15s",
                        }}>
                        <div>
                          <div style={{
                            fontSize: 14, color: "#eeeeff", fontFamily: "'Barlow', sans-serif", fontWeight: 600,
                          }}>{vm.name}</div>
                          <div style={{
                            fontSize: 12, color: "#aaaacc", marginTop: 3,
                            fontFamily: "'Share Tech Mono', monospace",
                          }}>{vm.os}</div>
                        </div>
                        <span style={{ fontSize: 13, color: "#aaaacc", fontFamily: "'Barlow', sans-serif" }}>{vm.role}</span>
                        <span style={{ fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{vm.ip}</span>
                        <span style={{ fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{fmt(vm.cpu)}</span>
                        <span style={{ fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{vm.mem == null ? "—" : `${vm.mem}GB`}</span>
                        <span style={{ fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace" }}>{fmt(vm.disk)}</span>
                        <div><StatusBadge status={vm.postStatus} /></div>
                      </div>
                    ))}
                  </div>
                </>
              )}

              {/* Selected VM detail */}
              {selectedVM && (
                <div className="fade-in" style={{ marginTop: 20, border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
                    <div>
                      <div style={{
                        fontSize: 18, fontFamily: "'Barlow', sans-serif",
                        fontWeight: 700, color: "#eeeeff",
                      }}>{selectedVM.name}</div>
                      <div style={{
                        fontSize: 13, color: "#aaaacc", marginTop: 4,
                        fontFamily: "'Barlow', sans-serif", letterSpacing: "0.04em",
                      }}>
                        AI Validation Report · {selectedVM.role || "Unassigned"}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                      {validationRunning && (
                        <span style={{
                          display: "inline-flex", alignItems: "center", gap: 8,
                          fontSize: 12, color: "#88aaff", letterSpacing: "0.06em",
                          fontFamily: "'Barlow', sans-serif", fontWeight: 700,
                          textTransform: "uppercase",
                        }}>
                          <Spinner size={12}/> Running
                        </span>
                      )}
                      <StatusBadge status={detailPostStatus} />
                    </div>
                  </div>

                  {detailError ? (
                    <ErrorState
                      title="Couldn't load VM detail"
                      message={detailError}
                      onRetry={() => loadVMDetail(selectedVMId)}
                      retrying={detailLoading}
                    />
                  ) : detailLoading ? (
                    <div style={{ marginBottom: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                      <Shimmer width="40%" height={14}/>
                      <Shimmer width="60%" height={11}/>
                      <Shimmer width="50%" height={11}/>
                    </div>
                  ) : (
                    <div style={{ display: "flex", gap: 36, marginBottom: 24, paddingBottom: 20, borderBottom: "1px solid #111122", flexWrap: "wrap" }}>
                      <Metric label="Pre-Migration" value={<StatusBadge status={selectedVM.preStatus} />} />
                      <Metric label="Post-Migration" value={<StatusBadge status={detailPostStatus} />} />
                      <Metric label="OS" value={detail?.os_family ?? selectedVM.os} />
                      <Metric label="IP" value={detail?.ip_address ?? selectedVM.ip} />
                      <Metric
                        label="Validated At"
                        value={validation?.validated_at ? new Date(validation.validated_at).toLocaleString() : "—"}
                      />
                    </div>
                  )}

                  {validationLoading || validationRunning ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 12, color: "#88aaff", fontSize: 13 }}>
                        <Spinner size={14}/>
                        <span style={{
                          letterSpacing: "0.06em", fontWeight: 700,
                          fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                        }}>
                          {validationRunning ? "Running validation" : "Loading validation"}…
                        </span>
                      </div>
                      <Shimmer width="100%" height={12}/>
                      <Shimmer width="85%" height={12}/>
                      <Shimmer width="92%" height={12}/>
                    </div>
                  ) : validationError ? (
                    <ErrorState
                      title="Couldn't load validation result"
                      message={validationError}
                      onRetry={() => loadVMDetail(selectedVMId)}
                      retrying={validationLoading}
                    />
                  ) : validationMissing ? (
                    <EmptyState
                      icon="◌"
                      title="No validation results yet"
                      description="Validation runs are scheduled server-side after the VM is migrated. Use Run Validation in the sidebar to refresh, or wait for the next scheduled pass."
                      ctaLabel="Run Validation"
                      onCta={onRunValidation}
                    />
                  ) : validation ? (
                    <div>
                      {validation.summary && (
                        <div style={{
                          fontSize: 14, color: "#ccccee", fontFamily: "'Barlow', sans-serif",
                          lineHeight: 1.6, marginBottom: 20, paddingBottom: 16,
                          borderBottom: "1px solid #111122",
                        }}>
                          {validation.summary}
                        </div>
                      )}

                      <div style={{
                        fontSize: 13, color: "#aaaacc", letterSpacing: "0.08em",
                        marginBottom: 14, fontFamily: "'Barlow', sans-serif",
                        textTransform: "uppercase", fontWeight: 700,
                      }}>AI Findings</div>
                      {validation.findings.length === 0 ? (
                        <div style={{
                          fontSize: 14, color: "#aaaacc", marginBottom: 18,
                          fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
                        }}>No findings recorded.</div>
                      ) : (
                        validation.findings.map((f, i) => (
                          <div key={i} className="finding-row" style={{ borderLeftColor: SEVERITY_COLOR[f.severity] || "#8888aa" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                              <SeverityTag s={f.severity} />
                              {f.category && (
                                <span style={{
                                  fontSize: 11, color: "#aaaacc", letterSpacing: "0.06em",
                                  textTransform: "uppercase", fontFamily: "'Barlow', sans-serif",
                                  fontWeight: 600,
                                }}>
                                  {f.category}
                                </span>
                              )}
                            </div>
                            <div style={{
                              fontSize: 14, color: "#ccccee", fontFamily: "'Barlow', sans-serif",
                              lineHeight: 1.6,
                            }}>
                              {f.message}
                            </div>
                          </div>
                        ))
                      )}

                      {validation.remediation.length > 0 && (
                        <>
                          <div style={{
                            fontSize: 13, color: "#aaaacc", letterSpacing: "0.08em",
                            margin: "24px 0 14px", fontFamily: "'Barlow', sans-serif",
                            textTransform: "uppercase", fontWeight: 700,
                          }}>Remediation</div>
                          {validation.remediation.map((r, i) => (
                            <div key={i} style={{ marginBottom: 14, fontFamily: "'Barlow', sans-serif" }}>
                              <div style={{ fontSize: 14, color: "#ccccee", lineHeight: 1.6 }}>
                                <span style={{ color: "#88aaff", marginRight: 8, fontWeight: 700 }}>
                                  {r.step ?? i + 1}.
                                </span>
                                {r.action}
                              </div>
                              {r.command && (
                                <div style={{
                                  fontSize: 13, color: "#ccccee", marginTop: 6, padding: "9px 12px",
                                  background: "#07070f", border: "1px solid #1a1a2e", borderRadius: 2,
                                  fontFamily: "'Share Tech Mono', monospace", lineHeight: 1.5,
                                }}>
                                  $ {r.command}
                                </div>
                              )}
                            </div>
                          ))}
                        </>
                      )}
                    </div>
                  ) : null}
                </div>
              )}
            </div>
          )}

          {/* MIGRATION PLAN TAB */}
          {activeTab === "migration plan" && (
            <div className="fade-in">
              {planError ? (
                <ErrorState
                  title="Couldn't load migration plans"
                  message={planError}
                  onRetry={() => loadPlan({ retry: true })}
                  retrying={planRetrying}
                />
              ) : planLoading ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  <Shimmer width="100%" height={64}/>
                  <Shimmer width="100%" height={120}/>
                  <Shimmer width="100%" height={120}/>
                </div>
              ) : !plan ? (
                <EmptyState
                  icon="◎"
                  title="No migration plans yet"
                  description="The local Ollama model groups your enrolled VMs into dependency-ordered migration waves — stateful services first, edge tier last. Generate your first plan to see the recommended sequence."
                  ctaLabel={vms.length === 0 ? "Add a VM First" : "Generate First Plan"}
                  onCta={() => vms.length === 0 ? setAddVMOpen(true) : setPlanModalOpen(true)}
                />
              ) : (
                <>
                  <div style={{ marginBottom: 20, padding: "20px 24px", border: "1px solid #1a1a2e", background: "#0a0a18" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                      <span style={{
                        fontSize: 13, color: "#aaaacc", letterSpacing: "0.06em",
                        fontFamily: "'Barlow', sans-serif", fontWeight: 700,
                      }}>
                        Plan{" "}
                        <span style={{ fontFamily: "'Share Tech Mono', monospace", color: "#eeeeff" }}>
                          #{plan.id}
                        </span>
                        {" · "}{plan.waves.length} wave{plan.waves.length === 1 ? "" : "s"}
                        {" · "}{plan.vm_ids.length} VMs
                      </span>
                      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                        <span style={{
                          fontSize: 12, color: "#aaaacc",
                          fontFamily: "'Share Tech Mono', monospace",
                        }}>
                          {new Date(plan.created_at).toLocaleString()} · {plan.model}
                        </span>
                        <SecondaryButton onClick={() => setPlanModalOpen(true)}>+ New Plan</SecondaryButton>
                      </div>
                    </div>
                    {plan.summary && (
                      <div style={{
                        fontSize: 14, color: "#ccccee", fontFamily: "'Barlow', sans-serif", lineHeight: 1.6,
                      }}>
                        {plan.summary}
                      </div>
                    )}
                  </div>

                  {plan.waves.map((wave) => (
                    <div key={wave.wave_number} style={{ border: "1px solid #1a1a2e", marginBottom: 14 }}>
                      <div className="wave-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "18px 22px", background: "#0a0a16" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                          <div style={{
                            width: 28, height: 28, border: "1px solid #4488ff66",
                            display: "flex", alignItems: "center", justifyContent: "center",
                            fontSize: 13, color: "#aaccff", fontWeight: 700,
                            fontFamily: "'Share Tech Mono', monospace",
                          }}>{wave.wave_number}</div>
                          <span style={{
                            fontSize: 16, fontFamily: "'Barlow', sans-serif",
                            fontWeight: 700, color: "#eeeeff",
                          }}>
                            Wave {wave.wave_number}
                          </span>
                          <span style={{
                            fontSize: 13, color: "#aaaacc",
                            fontFamily: "'Barlow', sans-serif",
                          }}>{wave.vm_ids.length} VMs</span>
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                          <WaveMTVDownload planId={plan.id} waveNumber={wave.wave_number} />
                          <span style={{
                            fontSize: 11, letterSpacing: "0.06em", fontWeight: 700,
                            color: RISK_COLOR[wave.estimated_risk] || "#aaaacc",
                            border: `1px solid ${(RISK_COLOR[wave.estimated_risk] || "#aaaacc")}66`,
                            padding: "4px 10px", borderRadius: 2, textTransform: "uppercase",
                            fontFamily: "'Barlow', sans-serif",
                          }}>
                            Risk · {wave.estimated_risk}
                          </span>
                        </div>
                      </div>
                      {wave.rationale && (
                        <div style={{
                          padding: "16px 22px", borderTop: "1px solid #0f0f1e",
                          fontSize: 14, color: "#ccccee", fontFamily: "'Barlow', sans-serif",
                          lineHeight: 1.6,
                        }}>
                          {wave.rationale}
                        </div>
                      )}
                      <div style={{ padding: "10px 22px 18px", borderTop: "1px solid #0f0f1e" }}>
                        {wave.vm_ids.map((vid) => (
                          <div key={vid} style={{
                            display: "flex", alignItems: "center", justifyContent: "space-between",
                            padding: "12px 0", borderBottom: "1px solid #0f0f1e",
                          }}>
                            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                              <div style={{ width: 7, height: 7, background: "#3a3a55", borderRadius: "50%" }} />
                              <div style={{
                                fontSize: 14, color: "#eeeeff",
                                fontFamily: "'Barlow', sans-serif", fontWeight: 600,
                              }}>
                                {vmNameById.get(vid) || `vm_id=${vid}`}
                              </div>
                            </div>
                            <span style={{
                              fontSize: 12, color: "#aaaacc",
                              fontFamily: "'Share Tech Mono', monospace",
                            }}>id {vid}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}

          {/* INVENTORY TAB */}
          {activeTab === "inventory" && (
            <div className="fade-in">
              {vmsError ? (
                <ErrorState
                  title="Couldn't load VM inventory"
                  message={vmsError}
                  onRetry={() => loadVMs({ retry: true })}
                  retrying={vmsRetrying}
                />
              ) : vmsLoading ? (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
                  {Array.from({ length: 6 }).map((_, i) => (
                    <div key={i} style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
                      <Shimmer width="60%" height={14}/>
                      <Shimmer width="40%" height={10}/>
                      <Shimmer width="100%" height={10}/>
                      <Shimmer width="85%" height={10}/>
                    </div>
                  ))}
                </div>
              ) : total === 0 ? (
                <EmptyState
                  icon="◌"
                  title="No VMs in inventory"
                  description="Enroll a VM by submitting its source hostname and SSH details. VirtValidate will collect baselines on the next scheduled pass."
                  ctaLabel="Add Your First VM"
                  onCta={() => setAddVMOpen(true)}
                />
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
                  {vms.map(vm => (
                    <div key={vm.id} style={{ border: "1px solid #1a1a2e", background: "#0a0a18", padding: 24 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
                        <div>
                          <div style={{
                            fontSize: 16, fontFamily: "'Barlow', sans-serif",
                            fontWeight: 700, color: "#eeeeff",
                          }}>{vm.name}</div>
                          <div style={{
                            fontSize: 13, color: "#aaaacc", marginTop: 4,
                            fontFamily: "'Barlow', sans-serif",
                          }}>{vm.role}</div>
                        </div>
                        <StatusBadge status={vm.postStatus} />
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                        <Metric label="OS" value={vm.os} />
                        <Metric label="IP" value={vm.ip} />
                        <Metric label="vCPU" value={fmt(vm.cpu)} />
                        <Metric label="Memory" value={vm.mem == null ? "—" : `${vm.mem}GB`} />
                        <Metric label="Disk" value={fmt(vm.disk)} />
                        <Metric label="Status" value={vm.rawStatus} />
                      </div>
                      <VMMTVMapping vm={vm} />
                    </div>
                  ))}
                </div>
              )}
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
                    padding: "20px 24px", border: "1px solid #1a1a2e", background: "#0a0a18",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
                      <span style={{ fontSize: 22, color: "#3a3a55" }}>{r.icon}</span>
                      <div>
                        <div style={{
                          fontSize: 15, fontFamily: "'Barlow', sans-serif",
                          fontWeight: 600, color: "#eeeeff",
                        }}>{r.name}</div>
                        <div style={{
                          fontSize: 13, color: "#aaaacc", marginTop: 4,
                          fontFamily: "'Barlow', sans-serif", lineHeight: 1.5,
                        }}>{r.desc}</div>
                      </div>
                    </div>
                    <button onClick={() => toast("Report export wires up to /api/plans/{id}/waves/{n}/report/pdf", { ...TOAST_OPTS, icon: "ℹ️" })}
                      style={{
                        background: "transparent", border: "1px solid #3a3a55", color: "#ccccee",
                        padding: "9px 18px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
                        letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                        cursor: "pointer",
                      }}>Export PDF</button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* AUDIT LOG TAB */}
          {activeTab === "audit log" && (
            <div className="fade-in">
              <div style={{ marginBottom: 20, padding: "20px 24px", border: "1px solid #1a1a2e", background: "#0a0a18", display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: 18 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                  <span style={{
                    fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                    fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
                  }}>Action</span>
                  <select
                    value={auditActionFilter}
                    onChange={(e) => setAuditActionFilter(e.target.value)}
                    style={{
                      background: "#07070f", border: "1px solid #2a2a44",
                      color: "#eeeeff", padding: "10px 12px", fontSize: 13,
                      fontFamily: "'Share Tech Mono', monospace", outline: "none", minWidth: 220,
                    }}
                  >
                    <option value="">All actions</option>
                    <option value="vm.create">vm.create</option>
                    <option value="vm.bulk_create">vm.bulk_create</option>
                    <option value="vm.update">vm.update</option>
                    <option value="vm.delete">vm.delete</option>
                    <option value="baseline.create">baseline.create</option>
                    <option value="baseline.collected">baseline.collected</option>
                    <option value="plan.create">plan.create</option>
                    <option value="settings.update">settings.update</option>
                    <option value="report.export">report.export</option>
                  </select>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                  <span style={{
                    fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                    fontFamily: "'Barlow', sans-serif", textTransform: "uppercase", fontWeight: 700,
                  }}>Resource Type</span>
                  <select
                    value={auditResourceFilter}
                    onChange={(e) => setAuditResourceFilter(e.target.value)}
                    style={{
                      background: "#07070f", border: "1px solid #2a2a44",
                      color: "#eeeeff", padding: "10px 12px", fontSize: 13,
                      fontFamily: "'Share Tech Mono', monospace", outline: "none", minWidth: 180,
                    }}
                  >
                    <option value="">All resources</option>
                    <option value="vm">vm</option>
                    <option value="baseline">baseline</option>
                    <option value="plan">plan</option>
                    <option value="settings">settings</option>
                  </select>
                </div>
                <div style={{ flex: 1 }}/>
                <SecondaryButton
                  onClick={() => loadAudit({ retry: true, action: auditActionFilter, resourceType: auditResourceFilter })}
                  disabled={auditLoading || auditRetrying}
                >
                  {auditRetrying ? <Spinner size={12}/> : "↻"} Refresh
                </SecondaryButton>
              </div>

              {auditError ? (
                <ErrorState
                  title="Couldn't load audit log"
                  message={auditError}
                  onRetry={() => loadAudit({ retry: true, action: auditActionFilter, resourceType: auditResourceFilter })}
                  retrying={auditRetrying}
                />
              ) : auditLoading ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {Array.from({ length: 6 }).map((_, i) => (
                    <Shimmer key={i} width="100%" height={42}/>
                  ))}
                </div>
              ) : auditEntries.length === 0 ? (
                <EmptyState
                  icon="◌"
                  title={auditActionFilter || auditResourceFilter ? "No matching audit entries" : "No audit entries yet"}
                  description={
                    auditActionFilter || auditResourceFilter
                      ? "Try clearing the filters above. The audit trail records every mutating API call as soon as one fires."
                      : "Every meaningful action — VM enrollment, baseline collection, plan generation, settings update, report export — appends a row here."
                  }
                  ctaLabel={auditActionFilter || auditResourceFilter ? "Clear Filters" : null}
                  onCta={() => { setAuditActionFilter(""); setAuditResourceFilter(""); }}
                />
              ) : (
                <div style={{ border: "1px solid #1a1a2e" }}>
                  <div style={{
                    display: "grid",
                    gridTemplateColumns: "1.4fr 1fr 1.2fr 1fr 0.6fr 1.4fr",
                    padding: "12px 18px", borderBottom: "1px solid #1a1a2e",
                    background: "#0a0a16",
                  }}>
                    {["Timestamp", "Actor", "Action", "Resource", "Status", "Details"].map(h => (
                      <span key={h} style={{
                        fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
                        fontFamily: "'Barlow', sans-serif", textTransform: "uppercase",
                        fontWeight: 700,
                      }}>{h}</span>
                    ))}
                  </div>
                  {auditEntries.map((e, i) => {
                    const status = e.details?.status_code;
                    const statusColor =
                      status == null ? "#aaaacc" :
                      status >= 500 ? "#ff5577" :
                      status >= 400 ? "#ffbb33" :
                      status >= 200 ? "#00ff88" : "#aaaacc";
                    const detailJson = JSON.stringify(e.details ?? {});
                    return (
                      <div key={e.id} style={{
                        display: "grid",
                        gridTemplateColumns: "1.4fr 1fr 1.2fr 1fr 0.6fr 1.4fr",
                        padding: "14px 18px",
                        borderBottom: i < auditEntries.length - 1 ? "1px solid #0f0f1e" : "none",
                        fontSize: 13, color: "#ccccee", fontFamily: "'Share Tech Mono', monospace",
                        alignItems: "center",
                      }}>
                        <span style={{ color: "#aaaacc" }}>{new Date(e.timestamp).toLocaleString()}</span>
                        <span style={{ color: "#eeeeff" }}>{e.actor}</span>
                        <span style={{ color: "#88aaff" }}>{e.action}</span>
                        <span style={{ color: "#aaaacc" }}>
                          {e.resource_type ?? "—"}
                          {e.resource_id ? <span style={{ color: "#aaaacc" }}> · {e.resource_id}</span> : null}
                        </span>
                        <span style={{ color: statusColor, fontWeight: 700 }}>{status ?? "—"}</span>
                        <span title={detailJson} style={{
                          color: "#aaaacc", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        }}>{detailJson}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Right sidebar — system info */}
        <div style={{ width: 260, borderLeft: "1px solid #1a1a2e", padding: 22, background: "#080814", flexShrink: 0 }}>
          <div style={{
            fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
            marginBottom: 18, fontFamily: "'Barlow', sans-serif",
            textTransform: "uppercase", fontWeight: 700,
          }}>System</div>

          <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            {[
              { label: "Appliance", value: "v0.1.0-alpha", mono: true },
              { label: "Model",     value: plan?.model || "llama3:8b", mono: true },
              { label: "Inference", value: "Local · Ollama", mono: false },
              { label: "Cluster",   value: "ocp-virt-prod-01", mono: true },
              { label: "Total VMs", value: vmsLoading ? "…" : String(total), mono: true },
              { label: "Validated", value: vmsLoading ? "…" : `${healthy + degraded + failed} / ${total}`, mono: true },
              { label: "Latest Plan", value: planLoading ? "…" : (plan ? `#${plan.id}` : "—"), mono: true },
            ].map(item => (
              <div key={item.label}>
                <div style={{
                  fontSize: 11, color: "#8888aa", letterSpacing: "0.08em",
                  marginBottom: 4, fontFamily: "'Barlow', sans-serif",
                  textTransform: "uppercase", fontWeight: 600,
                }}>{item.label}</div>
                <div style={{
                  fontSize: 14, color: "#ccccee",
                  fontFamily: item.mono ? "'Share Tech Mono', monospace" : "'Barlow', sans-serif",
                }}>{item.value}</div>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 28, paddingTop: 20, borderTop: "1px solid #1a1a2e" }}>
            <div style={{
              fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
              marginBottom: 14, fontFamily: "'Barlow', sans-serif",
              textTransform: "uppercase", fontWeight: 700,
            }}>Quick Actions</div>

            <button className="quick-action"
              onClick={onRunValidation}
              disabled={validationRunning || selectedVMId == null}
              title={selectedVMId == null ? "Select a VM first" : "Refresh latest validation"}
              style={{
                display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", marginBottom: 10,
                background: "none", border: "1px solid #2a2a44",
                color: validationRunning || selectedVMId == null ? "#888899" : "#ccccee",
                padding: "11px 14px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: validationRunning || selectedVMId == null ? "not-allowed" : "pointer", textAlign: "left",
                transition: "all 0.15s",
              }}>
              <span>Run Validation</span>
              {validationRunning && <Spinner size={12}/>}
            </button>

            <button className="quick-action"
              onClick={onCaptureBaseline}
              style={{
                display: "block", width: "100%", marginBottom: 10,
                background: "none", border: "1px solid #2a2a44", color: "#ccccee",
                padding: "11px 14px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: "pointer", textAlign: "left",
                transition: "all 0.15s",
              }}>Capture Baseline</button>

            <button className="quick-action"
              onClick={() => setPlanModalOpen(true)}
              disabled={vmsLoading}
              style={{
                display: "block", width: "100%", marginBottom: 10,
                background: "none", border: "1px solid #2a2a44",
                color: vmsLoading ? "#888899" : "#ccccee",
                padding: "11px 14px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: vmsLoading ? "wait" : "pointer", textAlign: "left",
                transition: "all 0.15s",
              }}>Generate Plan</button>

            <button className="quick-action"
              onClick={() => setAddVMOpen(true)}
              style={{
                display: "block", width: "100%", marginBottom: 10,
                background: "none", border: "1px solid #2a2a44", color: "#ccccee",
                padding: "11px 14px", fontSize: 12, fontFamily: "'Barlow', sans-serif",
                letterSpacing: "0.06em", textTransform: "uppercase", fontWeight: 700,
                cursor: "pointer", textAlign: "left",
                transition: "all 0.15s",
              }}>+ Add VM</button>
          </div>

          <div style={{ marginTop: 28, paddingTop: 20, borderTop: "1px solid #1a1a2e" }}>
            <div style={{
              fontSize: 11, color: "#aaaacc", letterSpacing: "0.08em",
              marginBottom: 10, fontFamily: "'Barlow', sans-serif",
              textTransform: "uppercase", fontWeight: 700,
            }}>AI Engine</div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#00ff88", boxShadow: "0 0 8px #00ff88", animation: "pulse 2s infinite" }} />
              <span style={{
                fontSize: 13, color: "#00ff88", fontFamily: "'Barlow', sans-serif",
                fontWeight: 600, letterSpacing: "0.04em",
              }}>Online · Air-gapped</span>
            </div>
          </div>
        </div>
      </div>

      <EnrollVMsModal
        open={addVMOpen}
        onClose={() => setAddVMOpen(false)}
        onCreated={() => loadVMs()}
      />
      <GeneratePlanModal
        open={planModalOpen}
        onClose={() => setPlanModalOpen(false)}
        vms={vms}
        onCreated={() => loadPlan()}
      />
    </div>
  );
}
