// Behavior tests for authoring mapping rows without inventory.
//
// The smoke suite proves pages mount; these prove the thing the feature
// exists for. A site that has its network/datastore list but can't produce
// an RVTools export must be able to build a complete mapping by hand, and
// before this the UI had no control that could create a row at all.
//
// Uses fireEvent rather than user-event — the latter isn't a dependency
// and this suite isn't worth adding one for.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import toast from "react-hot-toast";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ResourceMappingDetailPage from "../pages/ResourceMappingDetailPage";

const MAPPING = {
  id: 1,
  name: "M",
  vcenter_source_id: 1,
  ocp_target_id: 1,
  status: "incomplete",
  network_mappings: [],
  storage_mappings: [],
  namespace_mappings: [],
};

const CATALOG_NETWORKS = [
  { id: 1, name: "prod-vlan-100", network_type: "nad", namespace: "openshift-multus" },
];

function stub({ mapping = MAPPING, signals = { networks: [], datastores: [] } } = {}) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const href = String(url);
    calls.push({ href, method: opts.method ?? "GET", body: opts.body });

    let payload = {};
    if (/\/source-signals/.test(href)) payload = signals;
    else if (/\/networks/.test(href)) payload = CATALOG_NETWORKS;
    else if (/\/storage-classes/.test(href)) payload = [];
    else if (/\/api\/mappings\/\d+$/.test(href)) {
      // PATCH echoes what it was sent, like the real handler does after
      // validating through the Pydantic model.
      payload = opts.method === "PATCH" ? { ...mapping, ...JSON.parse(opts.body) } : mapping;
    }
    return {
      ok: true,
      status: 200,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  });
  return calls;
}

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/mappings/1"]}>
      <Routes>
        <Route path="/mappings/:id" element={<ResourceMappingDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

const addRow = () => fireEvent.click(screen.getAllByRole("button", { name: "Add row" })[0]);
const type = (el, value) => fireEvent.change(el, { target: { value } });
const patchBody = (calls) => JSON.parse(calls.find((c) => c.method === "PATCH").body);

describe("authoring mapping rows without inventory", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    // The Toaster lives in AppLayout, which these tests don't mount, so
    // assert on the call rather than on rendered text.
    vi.spyOn(toast, "success").mockImplementation(() => {});
    vi.spyOn(toast, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("adds a row and saves a source name that matches no VM", async () => {
    const calls = stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    addRow();

    type(await screen.findByLabelText("Source network for row 1"), "never-seen-in-inventory");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = patchBody(calls);

    expect(patch.network_mappings).toHaveLength(1);
    expect(patch.network_mappings[0].source_network).toBe("never-seen-in-inventory");
    // Client-only row identity must never be sent.
    expect(patch.network_mappings[0]).not.toHaveProperty("_key");
    // An untouched namespace strategy must not be written — doing so would
    // silence preflight's "no namespace mapping configured" warning
    // without anything actually resolving.
    expect(patch).not.toHaveProperty("namespace_mappings");
  });

  it("does not remount the source input while it is edited", async () => {
    // Regression: rows were keyed by source name, so every keystroke
    // changed the key, React remounted the input, and focus was lost
    // after a single character.
    stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    addRow();

    const first = await screen.findByLabelText("Source network for row 1");
    type(first, "V");
    const afterOne = screen.getByLabelText("Source network for row 1");
    type(afterOne, "VM Network");
    const afterAll = screen.getByLabelText("Source network for row 1");

    expect(afterOne).toBe(first);
    expect(afterAll).toBe(first);
    expect(afterAll).toHaveValue("VM Network");
  });

  it("flags a duplicate source name", async () => {
    stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    addRow();
    type(await screen.findByLabelText("Source network for row 1"), "dup");
    addRow();
    type(await screen.findByLabelText("Source network for row 2"), "dup");

    expect(await screen.findAllByText(/Duplicate source/i)).toHaveLength(2);
  });

  it("removes a row", async () => {
    stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    addRow();
    type(await screen.findByLabelText("Source network for row 1"), "gone");

    fireEvent.click(screen.getByRole("button", { name: /Remove gone/i }));
    await screen.findByText(/No network rows yet/i);
  });

  it("seeds rows from inventory and skips ones already present", async () => {
    stub({
      signals: {
        networks: [
          { name: "VM Network", vm_count: 4 },
          { name: "DMZ", vm_count: 1 },
        ],
        datastores: [{ name: "tier1", vm_count: 4 }],
      },
    });
    renderPage();

    await screen.findByText(/No network rows yet/i);
    // A hand-authored row for one of the inventory names already exists.
    addRow();
    type(await screen.findByLabelText("Source network for row 1"), "VM Network");

    fireEvent.click(screen.getAllByRole("button", { name: "Pull from inventory" })[0]);

    // "VM Network" must not be duplicated; "DMZ" is added after it.
    await waitFor(() =>
      expect(screen.getByLabelText("Source network for row 2")).toHaveValue("DMZ"),
    );
    expect(screen.queryByLabelText("Source network for row 3")).toBeNull();
    expect(screen.queryByText(/Duplicate source/i)).toBeNull();

    // The count in the confirmation must match what was actually added
    // (1 network + 1 datastore). Regression: the counter was incremented
    // inside the setMapping updater, which React invokes lazily during
    // render — so it was still 0 when the toast read it and every
    // successful pull reported "no new sources".
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        expect.stringMatching(/2 source\(s\) added/i),
      ),
    );
  });

  it("writes a namespace strategy only once edited", async () => {
    const calls = stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    fireEvent.click(screen.getByRole("tab", { name: /Namespaces/i }));

    type(await screen.findByLabelText("Namespace for production"), "prod-vms");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(patchBody(calls).namespace_mappings).toEqual({
      strategy: "per_environment",
      single_namespace: null,
      per_env_namespaces: { production: "prod-vms" },
      per_app_prefix: "app",
    });
  });

  it("shows legacy namespace rules read-only rather than discarding them", async () => {
    stub({
      mapping: {
        ...MAPPING,
        namespace_mappings: [
          { criteria: "vcenter_folder", criteria_value: "/DC1/vm/Pay", target_namespace: "pay" },
        ],
      },
    });
    renderPage();

    await screen.findByText(/No network rows yet/i);
    fireEvent.click(screen.getByRole("tab", { name: /Namespaces/i }));

    expect(await screen.findByText(/older per-criteria namespace rules/i)).toBeTruthy();
    const table = screen.getByRole("grid", { name: "Legacy namespace rules" });
    expect(within(table).getByText("/DC1/vm/Pay")).toBeTruthy();
  });

  it("points at the target cluster when its catalog is empty", async () => {
    // The storage-classes stub returns [] — with no declared targets
    // nothing can be mapped to, and suggest-storage would 400.
    stub();
    renderPage();

    await screen.findByText(/No network rows yet/i);
    fireEvent.click(screen.getByRole("tab", { name: /Storage/i }));

    expect(
      await screen.findByText(/No storage classes declared on this cluster yet/i),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /Add them on the target cluster/i }),
    ).toHaveAttribute("href", "/sources/targets/1");
  });
});
