// Behavior tests for the RVTools-only planning path: wizard → plan → YAML.
//
//   * the wizard ticks every mapping by default — `mapping_ids: []` means
//     "no mappings" to the backend, which skips every VM — and refuses to
//     go on with none ticked
//   * both YAML downloads surface the backend's error text instead of
//     saving the JSON error body or saying only "HTTP 422"
//
// Uses fireEvent rather than user-event — the latter isn't a dependency.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import toast from "react-hot-toast";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PlanDetailPage from "../pages/PlanDetailPage";
import PlanWizardPage from "../pages/PlanWizardPage";

const MAPPINGS = [
  { id: 7, name: "vc01 → ocp-prod", status: "complete" },
  { id: 8, name: "vc02 → ocp-prod", status: "complete" },
];

const PLAN = {
  id: 1,
  name: "Q4 wave plan",
  status: "complete",
  mapping_ids: [7],
  created_at: "2026-01-01T00:00:00Z",
  vm_ids: [11],
  waves: [{ wave_number: 1, vm_ids: [11], vm_names: ["alpha-01"], description: "w", method: "llm" }],
};

const YAML_ERROR = { detail: "Cannot generate plan: 1 source network(s) unmapped: 'VM Network'" };

function respond(payload, { ok = true, status = 200, headers = {} } = {}) {
  return {
    ok,
    status,
    headers: { get: (k) => headers[k.toLowerCase()] ?? null },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
    blob: async () => new Blob([JSON.stringify(payload)]),
  };
}

function stub({ yamlFails = false } = {}) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const href = String(url);
    calls.push({ href, method: opts.method ?? "GET", body: opts.body ? JSON.parse(opts.body) : null });
    if (/\/mtv-yaml$|\/yaml$/.test(href)) {
      return yamlFails
        ? respond(YAML_ERROR, { ok: false, status: 422 })
        : respond("kind: NetworkMap", {
            headers: { "content-disposition": 'attachment; filename="plan-1-wave-1.yaml"' },
          });
    }
    if (/\/api\/mappings$/.test(href)) return respond(MAPPINGS);
    if (/\/api\/plans\/\d+\/vms/.test(href)) {
      return respond({
        plan_id: 1,
        status: "complete",
        editable: true,
        items: [{ vm_id: 11, name: "alpha-01", wave_number: 1 }],
        total: 1,
        waves: [{ wave_number: 1, vm_count: 1, baseline: { missing_vm_ids: [] } }],
      });
    }
    if (/\/api\/plans\/\d+$/.test(href)) return respond(PLAN);
    if (/\/api\/plans/.test(href)) return respond([PLAN]);
    if (/\/api\/vms/.test(href)) return respond({ items: [], total: 0 });
    return respond([]);
  });
  return calls;
}

describe("plan wizard — resource mappings", () => {
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("ticks every mapping by default and refuses to continue with none", async () => {
    stub();
    render(
      <MemoryRouter>
        <PlanWizardPage />
      </MemoryRouter>,
    );

    fireEvent.change(await screen.findByLabelText(/plan name/i), { target: { value: "q4" } });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    const first = await screen.findByLabelText("vc01 → ocp-prod");
    const second = screen.getByLabelText("vc02 → ocp-prod");
    expect(first).toBeChecked();
    expect(second).toBeChecked();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();

    fireEvent.click(first);
    fireEvent.click(second);
    expect(await screen.findByText("Select at least one mapping")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("plan detail — YAML downloads", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(toast, "success").mockImplementation(() => {});
    vi.spyOn(toast, "error").mockImplementation(() => {});
    URL.createObjectURL = vi.fn(() => "blob:x");
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const renderPlan = () =>
    render(
      <MemoryRouter initialEntries={["/plans/1"]}>
        <Routes>
          <Route path="/plans/:id" element={<PlanDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

  it("shows why the bundle could not be built instead of saving the error", async () => {
    const calls = stub({ yamlFails: true });
    renderPlan();
    fireEvent.click(await screen.findByRole("button", { name: "Download all YAML" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][0]).toContain("source network(s) unmapped");
    expect(calls.some((c) => c.href === "/api/plans/1/yaml")).toBe(true);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("shows the generator's reason when one wave's YAML fails", async () => {
    stub({ yamlFails: true });
    renderPlan();
    fireEvent.click(await screen.findByRole("button", { name: "Download MTV YAML" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error.mock.calls[0][0]).toContain("source network(s) unmapped");
  });

  it("saves the wave YAML under the server's filename", async () => {
    stub();
    renderPlan();
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    fireEvent.click(await screen.findByRole("button", { name: "Download MTV YAML" }));

    await waitFor(() => expect(clicked).toHaveBeenCalled());
    expect(clicked.mock.contexts[0].download).toBe("plan-1-wave-1.yaml");
    expect(toast.error).not.toHaveBeenCalled();
  });
});
