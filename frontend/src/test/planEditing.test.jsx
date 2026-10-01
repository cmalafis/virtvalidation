// Behavior tests for seeing and changing which VMs are in which wave.
//
// The smoke suite proves the plan pages mount; these prove what the
// feature exists for:
//
//   * an operator can see every VM of every wave by name, find one, and
//     land on it from a link elsewhere
//   * a move is dry-run first, a planner-rule override needs an explicit
//     acknowledgement, and a blocking rule cannot be confirmed
//   * a plan that was marked succeeded is read-only
//   * the plans list groups a batched series and can say which plan a VM
//     is in
//
// Uses fireEvent rather than user-event — the latter isn't a dependency.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import toast from "react-hot-toast";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PlanDetailPage from "../pages/PlanDetailPage";
import PlansPage from "../pages/PlansPage";

const PARTITION = {
  source_vcenter_id: 1,
  source_vcenter_name: "vc01",
  target_cluster_id: 1,
  target_cluster_name: "ocp-prod",
  target_namespace: "prod",
};

const PLAN = {
  id: 1,
  name: "Q4 wave plan",
  status: "complete",
  model: "m",
  mapping_ids: [],
  created_at: "2026-01-01T00:00:00Z",
  vm_ids: [11, 12, 21],
  partition: PARTITION,
  waves: [
    {
      wave_number: 1,
      vm_ids: [11, 12],
      vm_names: ["alpha-01", "bravo-01"],
      description: "First wave.",
      risk_score: 2,
      method: "llm",
    },
    {
      wave_number: 2,
      vm_ids: [21],
      vm_names: ["charlie-01"],
      description: "Second wave.",
      risk_score: 4,
      method: "llm",
    },
  ],
};

const VMS = {
  plan_id: 1,
  status: "complete",
  editable: true,
  partition: PARTITION,
  items: [
    { vm_id: 11, name: "alpha-01", wave_number: 1, environment: "production", ip_address: "10.0.0.11" },
    { vm_id: 12, name: "bravo-01", wave_number: 1, environment: "production", ip_address: "10.0.0.12" },
    { vm_id: 21, name: "charlie-01", wave_number: 2, environment: "production", ip_address: "10.0.0.21" },
  ],
  total: 3,
  waves: [
    { wave_number: 1, vm_count: 2, baseline: { missing_vm_ids: [] } },
    { wave_number: 2, vm_count: 1, baseline: { missing_vm_ids: [] } },
  ],
};

const CLEAN_DRY_RUN = {
  applied: false,
  dry_run: true,
  operation: "move_to_wave",
  requires_confirmation: false,
  violations: [],
  moves: [{ vm_id: 11, vm_name: "alpha-01", from_plan_id: 1, from_wave: 1, to_plan_id: 1, to_wave: 2 }],
  plans: [{ plan_id: 1, name: "Q4 wave plan", vm_count: 3, wave_count: 2, touched_waves: [1, 2] }],
};

const OVERRIDE = {
  code: "wave_size",
  severity: "overridable",
  message: "Wave 2 would hold 11 VMs; the planner keeps waves to 10.",
  wave_number: 2,
  count: 11,
  limit: 10,
};

const BLOCKING = {
  code: "wave_run_in_progress",
  severity: "blocking",
  message: "Wave 2 has a baseline or validation run in progress.",
  wave_number: 2,
};

function stub({ plan = PLAN, vms = VMS, dryRun = CLEAN_DRY_RUN, plans = [plan], vmSearch = null } = {}) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const href = String(url);
    const method = opts.method ?? "GET";
    calls.push({ href, method, body: opts.body ? JSON.parse(opts.body) : null });

    let payload = [];
    if (/\/api\/plans\/\d+\/edits/.test(href)) {
      const sent = JSON.parse(opts.body);
      payload = sent.dry_run
        ? dryRun
        : { ...dryRun, applied: true, dry_run: false, operation: sent.operation, reannotation: "queued" };
    } else if (/\/api\/plans\/\d+\/vms/.test(href)) payload = vms;
    else if (/\/api\/plans\/\d+\/edit-targets/.test(href)) {
      payload = { partition: PARTITION, targets: [], ineligible: [] };
    } else if (/\/api\/plans\/\d+\/waves\/\d+\/preview/.test(href)) {
      payload = { vm_count: 0, vms: [], ssh_operations_enabled: true, requires_authorization: false };
    } else if (/\/api\/plans\/\d+$/.test(href)) payload = plan;
    else if (/\/api\/plans/.test(href)) payload = plans;
    else if (/\/api\/vms/.test(href)) payload = vmSearch ?? { items: [], total: 0 };

    return {
      ok: true,
      status: 200,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  });
  return calls;
}

const renderPlan = (path = "/plans/1") =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/plans/:id" element={<PlanDetailPage />} />
        <Route path="/plans" element={<PlansPage />} />
      </Routes>
    </MemoryRouter>,
  );

const edits = (calls) => calls.filter((c) => /\/edits$/.test(c.href));
const choose = (label, value) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("plan detail — who is in which wave", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(toast, "success").mockImplementation(() => {});
    vi.spyOn(toast, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("lists the VMs of the open wave and previews the closed ones", async () => {
    stub();
    renderPlan();

    // Wave 1 opens by default: its VMs are rows that link to the VM page.
    const alpha = await screen.findByRole("link", { name: "alpha-01" });
    expect(alpha).toHaveAttribute("href", "/vms/11");
    expect(screen.getByRole("link", { name: "bravo-01" })).toBeInTheDocument();
    // Wave 2 is closed, but its membership is still readable in the header.
    expect(screen.queryByRole("link", { name: "charlie-01" })).not.toBeInTheDocument();
    expect(screen.getByText("charlie-01")).toBeInTheDocument();
    expect(screen.getByText("vc01 → ocp-prod / prod")).toBeInTheDocument();
  });

  it("finds a VM across waves", async () => {
    stub();
    renderPlan();
    await screen.findByRole("link", { name: "alpha-01" });

    fireEvent.change(screen.getByLabelText("Find a VM in this plan"), {
      target: { value: "charlie" },
    });

    expect(await screen.findByText("1 match in wave 2")).toBeInTheDocument();
    // The match is revealed without the operator opening wave 2 ...
    expect(screen.getByRole("link", { name: "charlie-01" })).toBeInTheDocument();
    // ... and the waves with no match step out of the way.
    expect(screen.queryByRole("link", { name: "alpha-01" })).not.toBeInTheDocument();
  });

  it("opens the wave a deep link points at", async () => {
    stub();
    renderPlan("/plans/1?vm=21");

    expect(await screen.findByRole("link", { name: "charlie-01" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "alpha-01" })).not.toBeInTheDocument();
  });

  it("shows every VM with its wave in the All VMs view", async () => {
    stub();
    renderPlan();
    await screen.findByRole("link", { name: "alpha-01" });

    fireEvent.click(screen.getByRole("button", { name: "All VMs" }));

    const table = await screen.findByRole("grid", { name: "All VMs in this plan" });
    expect(within(table).getByRole("link", { name: "charlie-01" })).toBeInTheDocument();
    expect(within(table).getByText("Wave 2")).toBeInTheDocument();
    expect(within(table).getAllByText("Wave 1")).toHaveLength(2);
  });

  it("keeps VM names on screen when the detail call fails", async () => {
    stub();
    const base = global.fetch;
    global.fetch = vi.fn(async (url, opts) => {
      if (/\/api\/plans\/\d+\/vms/.test(String(url))) {
        return { ok: false, status: 500, statusText: "err", json: async () => ({ detail: "boom" }) };
      }
      return base(url, opts);
    });
    renderPlan();

    expect(await screen.findByRole("link", { name: "alpha-01" })).toBeInTheDocument();
    expect(await screen.findByText("VM details could not be loaded")).toBeInTheDocument();
  });
});

describe("plan detail — moving VMs", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(toast, "success").mockImplementation(() => {});
    vi.spyOn(toast, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const selectAlpha = async () => {
    await screen.findByRole("link", { name: "alpha-01" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select row 11" }));
    expect(await screen.findByText("1 VM selected")).toBeInTheDocument();
  };

  it("offers no actions until something is selected", async () => {
    stub();
    renderPlan();
    await screen.findByRole("link", { name: "alpha-01" });
    expect(screen.queryByRole("button", { name: "Move to wave…" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add VMs" })).toBeInTheDocument();
  });

  it("dry-runs the move, then commits it", async () => {
    const calls = stub();
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Move to wave…" }));
    const confirm = await screen.findByRole("button", { name: "Move 1 VM" });
    // Nothing to confirm before a destination is chosen.
    expect(confirm).toBeDisabled();
    expect(edits(calls)).toHaveLength(0);

    choose("Destination wave", "2");
    await waitFor(() => expect(edits(calls)).toHaveLength(1));
    expect(edits(calls)[0].body).toEqual({
      operation: "move_to_wave",
      vm_ids: [11],
      target_wave: 2,
      dry_run: true,
    });

    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Reason for this change"), {
      target: { value: "moves with charlie" },
    });
    fireEvent.click(confirm);

    await waitFor(() => expect(edits(calls)).toHaveLength(2));
    expect(edits(calls)[1].body).toEqual({
      operation: "move_to_wave",
      vm_ids: [11],
      target_wave: 2,
      confirm_overrides: false,
      note: "moves with charlie",
    });
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Moved 1 VM"));
    // The page reloads the plan after an applied edit.
    await waitFor(() =>
      expect(calls.filter((c) => /\/api\/plans\/1$/.test(c.href)).length).toBeGreaterThan(1),
    );
  });

  it("asks for a new wave from the selection", async () => {
    const calls = stub();
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "New wave from selection" }));
    await waitFor(() => expect(edits(calls)).toHaveLength(1));
    expect(edits(calls)[0].body.target_wave).toBe("new");
  });

  it("requires an acknowledgement to override a planner rule", async () => {
    const calls = stub({
      dryRun: { ...CLEAN_DRY_RUN, requires_confirmation: true, violations: [OVERRIDE] },
    });
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Move to wave…" }));
    choose("Destination wave", "2");

    expect(await screen.findByText(OVERRIDE.message)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Move 1 VM" });
    expect(confirm).toBeDisabled();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "I know this overrides a planner rule and want to make the change anyway",
      }),
    );
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(edits(calls)).toHaveLength(2));
    expect(edits(calls)[1].body.confirm_overrides).toBe(true);
  });

  it("cannot confirm a blocked move", async () => {
    const calls = stub({ dryRun: { ...CLEAN_DRY_RUN, violations: [BLOCKING], moves: [] } });
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Move to wave…" }));
    choose("Destination wave", "2");

    expect(await screen.findByText(BLOCKING.message)).toBeInTheDocument();
    expect(screen.getByText("This change cannot be applied")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move 1 VM" })).toBeDisabled();
    // No acknowledgement is offered for a rule that cannot be overridden.
    expect(screen.queryByRole("checkbox", { name: /overrides a planner rule/ })).not.toBeInTheDocument();
    expect(edits(calls).every((c) => c.body.dry_run)).toBe(true);
  });

  it("removes VMs from the plan", async () => {
    const calls = stub({ dryRun: { ...CLEAN_DRY_RUN, operation: "remove" } });
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Remove from plan" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(edits(calls)).toHaveLength(1));
    expect(edits(calls)[0].body).toEqual({ operation: "remove", vm_ids: [11], dry_run: true });

    const confirm = within(dialog).getByRole("button", { name: "Remove from plan" });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);

    await waitFor(() => expect(edits(calls)).toHaveLength(2));
    expect(edits(calls)[1].body.operation).toBe("remove");
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Removed 1 VM — available in inventory again"),
    );
  });

  it("explains why no other plan is offered", async () => {
    stub();
    const base = global.fetch;
    global.fetch = vi.fn(async (url, opts) => {
      if (/edit-targets/.test(String(url))) {
        const payload = {
          partition: PARTITION,
          targets: [],
          ineligible: [{ plan_id: 9, name: "Dev plan", reason: "migrates to vc01 → ocp-prod / dev" }],
        };
        return { ok: true, status: 200, json: async () => payload };
      }
      return base(url, opts);
    });
    renderPlan();
    await selectAlpha();

    fireEvent.click(screen.getByRole("button", { name: "Move to plan…" }));
    expect(await screen.findByText("No other plan can take these VMs")).toBeInTheDocument();
    expect(screen.getByText("Dev plan")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move 1 VM" })).toBeDisabled();
  });

  it("is read-only once the plan is marked succeeded", async () => {
    stub({ plan: { ...PLAN, status: "migrated" }, vms: { ...VMS, status: "migrated", editable: false } });
    renderPlan();

    expect(await screen.findByRole("link", { name: "alpha-01" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Select row 11" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add VMs" })).not.toBeInTheDocument();
    expect(screen.getByText("This plan was marked succeeded")).toBeInTheDocument();
  });

  it("marks waves an operator rearranged", async () => {
    const edited = {
      ...PLAN,
      waves: [
        {
          ...PLAN.waves[0],
          method: "mechanical_interim",
          manual_edit: { edited_by: "alice", edited_at: "2026-01-02T00:00:00Z", note: "with finance" },
          overrides: [{ code: "wave_size", count: 11, limit: 10, by: "alice" }],
        },
        PLAN.waves[1],
      ],
    };
    stub({
      plan: edited,
      vms: {
        ...VMS,
        waves: [{ wave_number: 1, vm_count: 2, baseline: { missing_vm_ids: [12] } }, VMS.waves[1]],
      },
    });
    renderPlan();

    expect(await screen.findByText("Manually edited")).toBeInTheDocument();
    expect(screen.getByText("Rule overridden")).toBeInTheDocument();
    expect(screen.getByText("Baseline out of date")).toBeInTheDocument();
    expect(screen.getByText("Deterministic — LLM rationale pending")).toBeInTheDocument();
    expect(screen.getByText("Note: with finance")).toBeInTheDocument();
    expect(screen.getByText("Risk 2/5")).toBeInTheDocument();
  });
});

describe("plans list", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const SERIES = [
    { id: 1, name: "q4-1", status: "complete", vm_ids: [1, 2], waves: [{ wave_number: 1 }], series_id: "s1", series_index: 1, series_total: 2, partition: PARTITION },
    { id: 2, name: "q4-2", status: "complete", vm_ids: [3], waves: [{ wave_number: 1 }], series_id: "s1", series_index: 2, series_total: 2, partition: PARTITION },
    { id: 3, name: "Standalone", status: "complete", vm_ids: [4], waves: [] },
  ];

  const renderList = () =>
    render(
      <MemoryRouter initialEntries={["/plans"]}>
        <Routes>
          <Route path="/plans" element={<PlansPage />} />
        </Routes>
      </MemoryRouter>,
    );

  it("groups the batches of a series under one row", async () => {
    stub({ plans: SERIES });
    renderList();

    expect(await screen.findByText("2 batches")).toBeInTheDocument();
    expect(screen.getByText("q4")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "q4-1" })).toHaveAttribute("href", "/plans/1");
    expect(screen.getByRole("link", { name: "Standalone" })).toBeInTheDocument();
    expect(screen.getAllByText("vc01 → ocp-prod / prod").length).toBeGreaterThan(0);

    // Collapsing the series hides its batches but not the standalone plan.
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.queryByRole("link", { name: "q4-1" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Standalone" })).toBeInTheDocument();
  });

  it("says which plan a VM is in", async () => {
    const calls = stub({
      plans: SERIES,
      vmSearch: {
        items: [{ id: 3, name: "billing-db-01", lifecycle_state: "planned", plan_id: 2, plan_name: "q4-2" }],
        total: 1,
      },
    });
    renderList();
    await screen.findByText("2 batches");

    fireEvent.change(screen.getByLabelText("Find a VM in a plan"), { target: { value: "billing" } });

    expect(await screen.findByText("billing-db-01")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "q4-2" });
    expect(links.some((a) => a.getAttribute("href") === "/plans/2?vm=3")).toBe(true);
    const search = calls.find((c) => /\/api\/vms\?/.test(c.href));
    expect(search.href).toContain("search=billing");
    expect(search.href).toContain("lifecycle_state=planned");
  });
});
