// Shared RVTools / CSV parser. Two surfaces consume this:
//
//   - The global Enroll modal in VirtValidate.jsx (CSV + XLSX path).
//   - The per-vCenter upload flow in VCenterSources.jsx (XLSX only).
//
// Header aliases are case + non-alnum-insensitive so customer
// CSV exports (column titles like "VM Name" or "Primary IP Address")
// land on the same field as RVTools' canonical "vInfo" sheet headers.

// Map any of these header aliases to the VM payload field.
export const HEADER_ALIASES = {
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
  // The vCenter column on the vInfo sheet — used to auto-detect
  // multi-vCenter uploads. Lives separately from source_hostname
  // (which is the *VM's* hostname, not the vCenter's).
  source_vcenter_hostname:    ["vcenter", "vc", "vcenterserver", "vcserver"],
};

// vsphere_networks and vsphere_datastores are list-typed; CSV operators put
// multiple values in one cell separated by ";" (and tolerate a stray ","
// since RVTools sometimes uses that).
export const splitList = (raw) => {
  if (!raw) return [];
  return String(raw)
    .split(/[;,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
};

export const normKey = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");

export function shortenOSFamily(raw) {
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

// Schema-side max_length for each scalar field. RVTools annotations
// frequently exceed 64 chars; truncating in the parser keeps the
// payload schema-valid instead of rejecting the entire upload over
// one verbose customer note.
const FIELD_MAX = {
  name: 255,
  source_hostname: 255,
  ip_address: 45,
  os_family: 64,
  role: 64,
  ssh_user: 64,
  notes: 1024,
  target_namespace: 253,
  target_storage_class: 253,
  target_network_attachment: 253,
};

const cap = (v, field) => {
  if (v == null) return v;
  const max = FIELD_MAX[field];
  return max && v.length > max ? v.slice(0, max) : v;
};

export function rowToPayload(rawRow) {
  const lookup = {};
  for (const [k, v] of Object.entries(rawRow)) lookup[normKey(k)] = v;
  const get = (field) => {
    for (const a of HEADER_ALIASES[field]) {
      const k = normKey(a);
      if (lookup[k] != null && String(lookup[k]).trim() !== "") return String(lookup[k]).trim();
    }
    return null;
  };
  const sourceHost = get("source_hostname") || "";
  const name = get("name") || (sourceHost ? sourceHost.split(".")[0] : "");
  if (!name && !sourceHost) return null;
  const vcenterHost = get("source_vcenter_hostname");
  return {
    name: cap(name || sourceHost, "name"),
    source_hostname: cap(sourceHost || name, "source_hostname"),
    ip_address: cap(get("ip_address"), "ip_address"),
    os_family: cap(shortenOSFamily(get("os_family")), "os_family"),
    role: cap(get("role"), "role"),
    ssh_user: cap(get("ssh_user"), "ssh_user"),
    notes: cap(get("notes"), "notes"),
    vsphere_networks: splitList(get("vsphere_networks")),
    vsphere_datastores: splitList(get("vsphere_datastores")),
    target_namespace: cap(get("target_namespace"), "target_namespace"),
    target_storage_class: cap(get("target_storage_class"), "target_storage_class"),
    target_network_attachment: cap(get("target_network_attachment"), "target_network_attachment"),
    // RVTools per-VM vCenter — drives the auto-link flow. Normalized
    // to lowercase + trailing-dot-stripped so equivalent values
    // ("vc-east-01.dha.mil." vs "VC-EAST-01.DHA.MIL") cluster
    // together when grouping.
    source_vcenter_hostname: normalizeHostname(vcenterHost),
  };
}


export function normalizeHostname(raw) {
  if (!raw) return null;
  return String(raw).trim().toLowerCase().replace(/\.+$/, "") || null;
}


/**
 * Group a parsed VM list by the source_vcenter_hostname field.
 *
 * Returns an array of { hostname, vm_count, sample_vm_names } in
 * decreasing-count order so the UI's "which vCenter is this?" prompt
 * surfaces the biggest cluster first. VMs without a vCenter column
 * land under the synthetic "_unspecified_" bucket — the operator
 * picks where they go.
 */
export function groupByVCenter(vms) {
  const buckets = new Map();
  for (const vm of vms) {
    const key = vm.source_vcenter_hostname || "_unspecified_";
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(vm.name);
  }
  return [...buckets.entries()]
    .map(([hostname, names]) => ({
      hostname,
      vm_count: names.length,
      sample_vm_names: names.slice(0, 5),
    }))
    .sort((a, b) => b.vm_count - a.vm_count);
}

// Minimal CSV parser supporting quoted fields with embedded commas + escaped
// double-quotes. Strips a leading BOM if present.
export function parseCSV(text) {
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

// Lazy-load SheetJS so the bundle stays small for users who never upload XLSX.
async function _loadXLSX() {
  return await import("xlsx");
}

// Returns { sheetName, rows } — the raw row dicts that rowToPayload consumes.
export async function parseXLSXRows(file) {
  const XLSX = await _loadXLSX();
  const ab = await file.arrayBuffer();
  const wb = XLSX.read(ab, { type: "array" });
  // RVTools' VM sheet is "vInfo" — prefer it, otherwise fall back to first.
  const sheetName =
    wb.SheetNames.find((n) => n.toLowerCase() === "vinfo") || wb.SheetNames[0];
  const sheet = wb.Sheets[sheetName];
  const rows = XLSX.utils.sheet_to_json(sheet, { defval: "" });
  return { sheetName, rows };
}

// One-call helper for the per-vCenter upload flow: reads the file,
// runs every row through rowToPayload, and returns the cleaned VM list
// plus parsing metadata. Skips rows that lack both name and hostname
// (consistent with the global Enroll modal).
export async function parseRVToolsXLSX(file) {
  const { sheetName, rows } = await parseXLSXRows(file);
  const vms = [];
  const skipped = [];
  rows.forEach((row, idx) => {
    const p = rowToPayload(row);
    if (!p) {
      skipped.push({ row_index: idx + 2, reason: "missing both name and hostname" });
    } else {
      vms.push(p);
    }
  });
  return { sheetName, vms, skipped, totalRows: rows.length };
}
