// Row model for the plan detail page.
//
// Wave membership comes from the plan document (`waves[].vm_ids` +
// `vm_names`), which is what the page already polls. The per-VM facts —
// environment, IP, HA family, baseline coverage — come from
// GET /api/plans/{id}/vms and are merged on by id. Keeping the two apart
// means VM names are on screen as soon as the plan loads, and stay there
// if the detail call fails.

import { asArray } from "../../utils/asArray";

export function waveNumberOf(wave) {
  return wave?.wave_number ?? wave?.wave ?? 0;
}

/** One row per VM in the plan, in wave order. */
export function buildRows(plan, vmState) {
  const details = new Map(asArray(vmState?.items).map((item) => [item?.vm_id, item]));
  const rows = [];
  asArray(plan?.waves).forEach((wave) => {
    const number = waveNumberOf(wave);
    const names = asArray(wave?.vm_names);
    asArray(wave?.vm_ids).forEach((id, i) => {
      const detail = details.get(id);
      rows.push({
        ...detail,
        vm_id: id,
        name: detail?.name ?? names[i] ?? `VM #${id}`,
        // The plan document is authoritative for placement; the detail
        // call can be a poll behind right after an edit.
        wave_number: number,
        hasDetail: Boolean(detail),
      });
    });
  });
  return rows;
}

/** Case-insensitive match on the fields an operator would search by. */
export function rowMatches(row, query) {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return true;
  return [row?.name, row?.ip_address, row?.application_hint, row?.ha_family, row?.environment]
    .filter(Boolean)
    .some((field) => String(field).toLowerCase().includes(q));
}

// How a wave's provenance should read. The auth and guardrail fallbacks
// are deliberately distinct from a plain one: they mean a key was
// rejected or a detector fired, not that the model had an off day.
const METHOD_META = {
  llm: { color: "green", label: "LLM" },
  mechanical_interim: { color: "blue", label: "Deterministic — LLM rationale pending" },
  mechanical_fallback: { color: "orange", label: "Deterministic fallback" },
  mechanical_fallback_auth: { color: "red", label: "Fallback — LLM auth rejected" },
  mechanical_fallback_guardrail: { color: "red", label: "Fallback — guardrail fired" },
};

export function methodMeta(method) {
  if (!method) return null;
  if (METHOD_META[method]) return METHOD_META[method];
  if (String(method).startsWith("llm_retry")) {
    return { color: "orange", label: `LLM (${String(method).replace("llm_retry_", "retry ")})` };
  }
  return { color: "grey", label: String(method) };
}

// Wave risk scores are 1-5 (app.core.wave_annotation.WaveAnnotation).
export function riskColor(score) {
  if (score == null) return "grey";
  if (score >= 4) return "red";
  if (score >= 3) return "orange";
  return "green";
}

// A manual edit hands its waves to a background LLM pass. Poll for the
// result only while it could plausibly still arrive — a task lost to a
// restart must not keep the page polling forever.
const REANNOTATION_WINDOW_MS = 5 * 60 * 1000;

export function isReannotationPending(wave, now = Date.now()) {
  const state = wave?.reannotation;
  if (state?.state !== "queued") return false;
  const queuedAt = Date.parse(state?.queued_at ?? "");
  return Number.isFinite(queuedAt) && now - queuedAt < REANNOTATION_WINDOW_MS;
}

/** "vCenter → cluster / namespace" for a plan's derived partition. */
export function partitionLabel(partition) {
  if (!partition?.target_namespace) return null;
  const source = partition?.source_vcenter_name ?? "vCenter";
  const cluster = partition?.target_cluster_name ?? "cluster";
  return `${source} → ${cluster} / ${partition.target_namespace}`;
}
