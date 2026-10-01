// Smoke tests: every page mounts against empty data, an API error, and a
// populated fixture.
//
// This is deliberately shallow. It is not trying to assert behavior — it
// exists because CLAUDE.md's defensive-coding rules (optional chaining on
// every nested field, `?? []` array defaults, explicit empty states) are
// the kind of thing that only breaks in the exact conditions nobody
// clicks through by hand. A page that throws on `data.items.map` when
// `items` is missing fails here instead of in front of an operator.
//
// Every fetch is stubbed, so nothing here touches a backend.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AgentActivityPage from "../pages/AgentActivityPage";
import AuditLogPage from "../pages/AuditLogPage";
import BulkOperationsPage from "../pages/BulkOperationsPage";
import DesignReviewDetailPage from "../pages/DesignReviewDetailPage";
import DesignReviewNewPage from "../pages/DesignReviewNewPage";
import DesignReviewsPage from "../pages/DesignReviewsPage";
import InventoryPage from "../pages/InventoryPage";
import OCPTargetDetailPage from "../pages/OCPTargetDetailPage";
import OCPTargetsPage from "../pages/OCPTargetsPage";
import OverviewPage from "../pages/OverviewPage";
import PlanDetailPage from "../pages/PlanDetailPage";
import PlansPage from "../pages/PlansPage";
import PlanWizardPage from "../pages/PlanWizardPage";
import ReportsPage from "../pages/ReportsPage";
import ReportViewPage from "../pages/ReportViewPage";
import ResourceMappingDetailPage from "../pages/ResourceMappingDetailPage";
import ResourceMappingsPage from "../pages/ResourceMappingsPage";
import RVToolsUploadPage from "../pages/RVToolsUploadPage";
import SettingsPage from "../pages/SettingsPage";
import ValidationsPage from "../pages/ValidationsPage";
import VCenterSourcesPage from "../pages/VCenterSourcesPage";
import VMDetailPage from "../pages/VMDetailPage";

// ---------------------------------------------------------------------
// Fetch stubs
// ---------------------------------------------------------------------

// A populated response per endpoint pattern. Shapes mirror the real
// schemas — if one drifts, the page that reads it should be updated too.
const POPULATED = [
  [/\/api\/vms\/facets/, { status: { discovered: 3 }, environment: { prod: 2 } }],
  [/\/api\/vms\/stats/, { total: 3, by_status: { discovered: 2, validated: 1 } }],
  [/\/api\/vms\/\d+\/snapshots/, [
    { id: 1, snapshot_number: 1, ssh_user: "svc", raw_data: { os: {} }, collected_at: "2026-01-01T00:00:00Z" },
  ]],
  [/\/api\/vms\/\d+\/validation\/latest/, {
    validation: {
      id: 1, vm_id: 1, status: "passed", summary: "Clean", validated_at: "2026-01-01T00:00:00Z",
      findings: [{ id: 1, title: "F", description: "d", severity: "low", confidence: "high" }],
      remediation: [], diff: {},
    },
  }],
  [/\/api\/vms\/\d+\/baseline\/profile/, { vm_id: 1, snapshot_count: 1 }],
  [/\/api\/vms\/\d+$/, {
    id: 1, name: "vm-1", status: "validated", lifecycle_state: "planned",
    vsphere_networks: ["net"], vsphere_datastores: ["ds"],
    plan_id: 1, plan_name: "Plan", plan_wave_number: 2,
  }],
  [/\/api\/vms\/ids/, { items: [{ id: 1, name: "vm-1" }, { id: 2, name: "vm-2" }], total: 2 }],
  [/\/api\/vms/, {
    items: [{ id: 1, name: "vm-1", status: "discovered", lifecycle_state: "available" }],
    total: 1, skip: 0, limit: 20,
  }],
  [/\/api\/plans\/\d+\/waves\/\d+\/preview/, {
    plan_id: 1, wave_number: 1, vm_count: 1, requires_authorization: false,
    authorization_reason: "", ssh_operations_enabled: true,
    vms: [{ vm_id: 1, name: "vm-1", host: "h", username: "u", commands: ["uname -r"] }],
  }],
  [/\/api\/plans\/preview/, {
    total_vms: 2, resolvable: 2, unresolvable: 0, groups: [], unresolved: [],
    plans: [{ name: "plan-1", vm_count: 1, series_index: 1 }, { name: "plan-2", vm_count: 1, series_index: 2 }],
    plan_count: 2, batch_size: 1,
    excluded: [{ vm_id: 3, vm_name: "vm-3", reason: "blocked for cold: vv.disk.nvme.detected" }],
  }],
  [/\/api\/plans\/\d+\/vms/, {
    plan_id: 1, status: "complete", editable: true,
    partition: { source_vcenter_id: 1, source_vcenter_name: "vc", target_cluster_id: 1, target_cluster_name: "ocp", target_namespace: "prod" },
    limits: { max_vms_per_wave: 10, max_vms_per_plan: 1000 },
    items: [
      { vm_id: 1, name: "vm-1", wave_number: 1, environment: "production", ip_address: "10.0.0.1", role: "app", ha_family: "vm", ha_family_size: 2, assessment_status: "ok", lifecycle_state: "planned", baseline_covered: false, missing: false },
      { vm_id: 2, name: "vm-2", wave_number: 2, baseline_covered: null, missing: true },
    ],
    total: 2,
    waves: [
      { wave_number: 1, vm_count: 1, baseline: { last_run_id: 1, last_completed_at: "2026-01-01T00:00:00Z", missing_vm_ids: [1] }, active_run: false, validated: false },
      { wave_number: 2, vm_count: 1, baseline: { last_run_id: null, last_completed_at: null, missing_vm_ids: [] }, active_run: false, validated: false },
    ],
  }],
  [/\/api\/plans\/\d+\/edit-targets/, {
    partition: { target_namespace: "prod" },
    targets: [{ plan_id: 2, name: "Plan-2", same_series: true, series_index: 3, series_total: 3, migration_type: "cold", vm_count: 4, waves: [{ wave_number: 1, vm_count: 4 }] }],
    ineligible: [{ plan_id: 3, name: "Other", reason: "migrates to vc → ocp / dev" }],
  }],
  [/\/api\/plans\/\d+$/, {
    id: 1, name: "Plan", vm_ids: [1, 2], mapping_ids: [], model: "m", status: "complete",
    created_at: "2026-01-01T00:00:00Z", series_id: "abc", series_index: 2, series_total: 3,
    partition: { source_vcenter_name: "vc", target_cluster_name: "ocp", target_namespace: "prod" },
    excluded_vms: [{ vm_id: 3, vm_name: "vm-3", reason: "no target cluster/namespace resolved" }],
    waves: [
      { wave_number: 1, vm_ids: [1], vm_names: ["vm-1"], description: "d", risk_score: 3, method: "llm" },
      {
        wave_number: 2, vm_ids: [2], vm_names: ["vm-2"], description: "d2", risk_score: 4,
        method: "mechanical_interim",
        manual_edit: { edited_at: "2026-01-02T00:00:00Z", edited_by: "user", edit_count: 1, note: "n" },
        overrides: [{ code: "wave_size", count: 11, limit: 10, by: "user" }],
        reannotation: { state: "done", token: "t" },
      },
    ],
  }],
  // A two-batch series plus a standalone plan: the list groups the former.
  [/\/api\/plans/, [
    {
      id: 1, name: "q4-1", vm_ids: [1], waves: [{ wave_number: 1, vm_ids: [1], manual_edit: { edit_count: 1 } }],
      model: "m", status: "complete", mapping_ids: [], created_at: "2026-01-01T00:00:00Z",
      series_id: "abc", series_index: 1, series_total: 2,
      partition: { source_vcenter_name: "vc", target_cluster_name: "ocp", target_namespace: "prod" },
    },
    {
      id: 2, name: "q4-2", vm_ids: [2], waves: [], model: "m", status: "annotating",
      mapping_ids: [], created_at: "2026-01-01T00:00:00Z",
      series_id: "abc", series_index: 2, series_total: 2, partition: null,
    },
    {
      id: 3, name: "Solo", vm_ids: [3], waves: [], model: "m", status: "complete",
      mapping_ids: [], created_at: "2026-01-01T00:00:00Z",
    },
  ]],
  [/\/api\/(network|storage)-reviews\/\d+/, {
    id: 1, name: "R", status: "complete", customer_notes: "", proposed_yaml: "",
    analysis_results: { executive_summary: "s" }, created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    findings: [{ id: 1, title: "t", description: "d", severity: "high", confidence: "high", triage: "open" }],
  }],
  [/\/api\/(network|storage)-reviews/, [{
    id: 1, name: "R", status: "complete", finding_count: 1, severity_counts: { high: 1 },
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  }]],
  [/\/api\/ocp-targets\/\d+\/(networks|storage-classes|namespaces)/, [
    { id: 1, name: "entry", network_type: "nad", access_mode: "ReadWriteOnce", is_default: true },
  ]],
  [/\/api\/sources\/targets\/\d+/, { id: 1, name: "ocp", api_endpoint: "https://x", status: "active" }],
  [/\/api\/sources\/targets/, [{ id: 1, name: "ocp", api_endpoint: "https://x", status: "active", classification_level: "unclassified" }]],
  [/\/api\/sources\/vcenters/, [{ id: 1, name: "vc", hostname: "vc.local", status: "active", classification_level: "unclassified", vm_count: 3 }]],
  // Must precede the /api/mappings/\d+ entry below — that pattern is a
  // substring match and would otherwise swallow this URL, handing the page
  // a mapping object where it expects {networks, datastores}.
  [/\/api\/mappings\/\d+\/source-signals/, {
    networks: [{ name: "n", vm_count: 2 }],
    datastores: [{ name: "d", vm_count: 1 }],
  }],
  [/\/api\/mappings\/\d+/, {
    id: 1, name: "M", vcenter_source_id: 1, ocp_target_id: 1, status: "draft",
    network_mappings: [{ source_network: "n", confidence: "high", rationale: "r" }],
    storage_mappings: [{ source_datastore: "d" }],
    namespace_mappings: { strategy: "per_environment", per_env_namespaces: { production: "prod-vms" } },
  }],
  [/\/api\/mappings/, [{ id: 1, name: "M", vcenter_source_id: 1, ocp_target_id: 1, status: "draft", network_mappings: [], storage_mappings: [] }]],
  [/\/api\/health\/full/, {
    api: { status: "online", version: "0.1.0" },
    components: { database: { status: "online" }, llm: { status: "online" }, schema: { is_up_to_date: true, pending_migrations: [] } },
    fips: { configured: false, detected: false, effective: false, operations: [] },
  }],
  [/\/api\/settings\/llm/, {
    active_llm_backend: "mock",
    available_backends: [{ type: "mock", label: "Mock", configured: true, dev_only: true, missing_config: [] }],
    last_llm_error: null,
  }],
  [/\/api\/settings/, { id: 1, ollama_model: "m", schedule_preset: "twice_daily", ssh_host_key_policy: "auto_accept", ssh_operations_enabled: true, max_vms_per_plan: 1000, max_vms_per_plan_request: 10000 }],
  [/\/api\/system\/fips-status/, { configured: false, detected: false, effective: false, operations: [] }],
  [/\/api\/system\/ssh-key/, { status: "present", public_key: "ssh-ed25519 AAAA", fingerprint: "SHA256:x", algorithm: "ed25519" }],
  [/\/api\/ssh-keys/, [{ id: 1, name: "k", public_key: "p", fingerprint: "f", algorithm: "ed25519", status: "active", plan_id: null, created_at: "2026-01-01T00:00:00Z" }]],
  [/\/api\/command-audits/, { items: [{ id: 1, host: "h", command: "uname -r", exit_status: 0, blocked: false, duration_ms: 5, stdout_byte_count: 10, started_at: "2026-01-01T00:00:00Z" }], total: 1, skip: 0, limit: 20 }],
  [/\/api\/inference-logs/, { items: [{ id: 1, operation: "op", backend_type: "mock", model: "m", method: "llm", latency_ms: 5, created_at: "2026-01-01T00:00:00Z" }], total: 1, skip: 0, limit: 20 }],
  [/\/api\/audit/, [{ id: 1, timestamp: "2026-01-01T00:00:00Z", action: "a", actor: "u", resource_type: "vm", resource_id: "1", details: { k: "v" } }]],
  [/\/api\/reports\//, { report_type: "executive_summary", generated_at: "2026-01-01T00:00:00Z", summary: { total_vms: 3 }, executive_summary: "text", key_risks: [{ risk: "r" }] }],
];

// Deliberately hostile: every field a page might read is absent. This is
// the case the defensive-coding rules exist for.
const EMPTY = [
  [/\/api\/vms\/\d+\/validation\/latest/, { validation: null }],
  [/\/api\/vms\/\d+$/, { id: 1, name: "vm-1" }],
  [/\/api\/vms\/facets/, {}],
  [/\/api\/vms\/stats/, {}],
  [/\/api\/vms/, {}],
  [/\/api\/plans\/\d+$/, { id: 1 }],
  [/\/api\/mappings\/\d+/, { id: 1, ocp_target_id: 1 }],
  [/\/api\/sources\/targets\/\d+/, {}],
  [/\/api\/reports\//, {}],
  [/\/api\/health\/full/, {}],
  [/\/api\/settings\/llm/, {}],
  [/\/api\/settings/, {}],
  [/\/api\//, []],
];

function stubFetch(table) {
  global.fetch = vi.fn(async (url) => {
    const href = String(url);
    const hit = table.find(([pattern]) => pattern.test(href));
    return {
      ok: true,
      status: 200,
      json: async () => (hit ? hit[1] : []),
      text: async () => JSON.stringify(hit ? hit[1] : []),
    };
  });
}

function stubFetchError() {
  global.fetch = vi.fn(async () => ({
    ok: false,
    status: 500,
    statusText: "Internal Server Error",
    json: async () => ({ detail: "boom" }),
    text: async () => "boom",
  }));
}

// ---------------------------------------------------------------------

const PAGES = [
  ["OverviewPage", () => <OverviewPage />, "/"],
  ["InventoryPage", () => <InventoryPage />, "/inventory"],
  ["VMDetailPage", () => <VMDetailPage />, "/vms/1"],
  ["ValidationsPage", () => <ValidationsPage />, "/validations"],
  ["PlansPage", () => <PlansPage />, "/plans"],
  ["PlanDetailPage", () => <PlanDetailPage />, "/plans/1"],
  ["PlanWizardPage", () => <PlanWizardPage />, "/plans/new"],
  ["ReportsPage", () => <ReportsPage />, "/reports"],
  ["ReportViewPage", () => <ReportViewPage />, "/reports/executive-summary"],
  ["DesignReviewsPage", () => <DesignReviewsPage />, "/design-reviews"],
  ["DesignReviewNewPage", () => <DesignReviewNewPage kind="network" />, "/design-reviews/network/new"],
  ["DesignReviewDetailPage", () => <DesignReviewDetailPage kind="network" />, "/design-reviews/1"],
  ["VCenterSourcesPage", () => <VCenterSourcesPage />, "/sources/vcenters"],
  ["OCPTargetsPage", () => <OCPTargetsPage />, "/sources/targets"],
  ["OCPTargetDetailPage", () => <OCPTargetDetailPage />, "/sources/targets/1"],
  ["ResourceMappingsPage", () => <ResourceMappingsPage />, "/mappings"],
  ["ResourceMappingDetailPage", () => <ResourceMappingDetailPage />, "/mappings/1"],
  ["RVToolsUploadPage", () => <RVToolsUploadPage />, "/rvtools/upload"],
  ["BulkOperationsPage", () => <BulkOperationsPage />, "/operations"],
  ["AgentActivityPage", () => <AgentActivityPage />, "/agent-activity"],
  ["AuditLogPage", () => <AuditLogPage />, "/audit"],
  ["SettingsPage", () => <SettingsPage />, "/settings"],
];

// Route params have to resolve, so each page is mounted under a matching
// path pattern rather than bare.
const ROUTE_PATTERNS = [
  "/", "/inventory", "/vms/:id", "/validations", "/plans", "/plans/new", "/plans/:id",
  "/reports", "/reports/:type", "/design-reviews", "/design-reviews/network/new",
  "/design-reviews/:id", "/sources/vcenters", "/sources/targets", "/sources/targets/:id",
  "/mappings", "/mappings/:id", "/rvtools/upload", "/operations", "/agent-activity",
  "/audit", "/settings",
];

// `makeElement` is a factory rather than a stored JSX element: an array
// of elements trips react/jsx-key even though these are never rendered as
// a list.
function renderAt(makeElement, path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {ROUTE_PATTERNS.map((pattern) => (
          <Route key={pattern} path={pattern} element={makeElement()} />
        ))}
      </Routes>
    </MemoryRouter>,
  );
}

/** A page "mounted" if it put a heading on screen and didn't throw. */
async function expectMounted() {
  await waitFor(() => {
    expect(screen.getAllByRole("heading").length).toBeGreaterThan(0);
  });
}

describe("pages mount", () => {
  const errors = [];

  beforeEach(() => {
    errors.length = 0;
    // React logs render errors rather than rethrowing in some paths, so
    // capture console.error and fail on it — otherwise a broken page can
    // "pass" by rendering nothing.
    vi.spyOn(console, "error").mockImplementation((...args) => {
      errors.push(args.join(" "));
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  describe.each(PAGES)("%s", (name, makeElement, path) => {
    it("mounts with populated data", async () => {
      stubFetch(POPULATED);
      renderAt(makeElement, path);
      await expectMounted();
    });

    it("mounts with empty/missing fields", async () => {
      stubFetch(EMPTY);
      renderAt(makeElement, path);
      await expectMounted();
    });

    it("mounts when every request fails", async () => {
      stubFetchError();
      renderAt(makeElement, path);
      await expectMounted();
    });
  });
});
