// OCP 4.21/4.22 mapping features in the UI: dropping a NIC, the "lands as"
// destination, the catalog import preview → apply flow, and the wave
// card's "where this wave lands" summary.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import toast from "react-hot-toast";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ResourceMappingDetailPage from "../pages/ResourceMappingDetailPage";
import ImportCatalogModal from "../pages/targets/ImportCatalogModal";
import WaveMappingSummary, { waveMappingBadges } from "../pages/plan/WaveMappingSummary";

const MAPPING = {
  id: 1,
  name: "M",
  vcenter_source_id: 1,
  ocp_target_id: 1,
  status: "complete",
  network_mappings: [
    { source_network: "App", target_network_name: "tenant-blue", target_network_type: "cudn" },
    { source_network: "Backup", target_network_name: "db-vlan-100", target_network_type: "nad" },
  ],
  storage_mappings: [],
  namespace_mappings: [],
};
const NETWORKS = [
  { id: 1, name: "tenant-blue", network_type: "cudn", role: "primary", topology: "layer2" },
  { id: 2, name: "db-vlan-100", network_type: "nad", topology: "bridge", vlan_id: 100, namespace: "fin" },
];

function stubFetch(route) {
  const calls = [];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const href = String(url);
    calls.push({ href, method: opts.method ?? "GET", body: opts.body });
    const payload = route(href, opts);
    return {
      ok: true,
      status: 200,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    };
  });
  return calls;
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(toast, "success").mockImplementation(() => {});
  vi.spyOn(toast, "error").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("mapping editor", () => {
  const renderPage = () =>
    render(
      <MemoryRouter initialEntries={["/mappings/1"]}>
        <Routes>
          <Route path="/mappings/:id" element={<ResourceMappingDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

  it("shows where each network lands and saves a dropped NIC as ignored", async () => {
    const calls = stubFetch((href, opts) => {
      if (/\/networks/.test(href)) return NETWORKS;
      if (/\/storage-classes/.test(href)) return [];
      if (/\/api\/mappings\/\d+$/.test(href))
        return opts.method === "PATCH" ? { ...MAPPING, ...JSON.parse(opts.body) } : MAPPING;
      return {};
    });
    renderPage();
    await screen.findByText("Primary UDN");
    expect(screen.getByText("Multus")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Target for Backup"), {
      target: { value: "__ignored__" },
    });
    expect(screen.getByText("Ignored")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "PATCH").body);
    const backup = body.network_mappings.find((r) => r.source_network === "Backup");
    expect(backup.ignored).toBe(true);
    expect(backup.target_network_name).toBeNull();
  });

  it("renders platform-check findings from preflight", async () => {
    stubFetch((href) => {
      if (/\/preflight/.test(href))
        return {
          ok: false,
          findings: [
            {
              code: "udn.static_ip_outside_subnet",
              severity: "blocking",
              message: "Static IP outside the UDN subnet",
              vm_count: 1,
              vm_names: ["app-02"],
            },
          ],
        };
      if (/\/networks/.test(href)) return NETWORKS;
      if (/\/storage-classes/.test(href)) return [];
      if (/\/api\/mappings\/\d+$/.test(href)) return MAPPING;
      return {};
    });
    renderPage();
    await screen.findByText("Primary UDN");
    fireEvent.click(screen.getByRole("button", { name: "Run preflight" }));
    await screen.findByText("Static IP outside the UDN subnet");
    expect(screen.getByText(/app-02/)).toBeTruthy();
  });
});

describe("catalog import", () => {
  it("previews with dry_run, then applies", async () => {
    const preview = {
      dry_run: true,
      networks: [{ name: "tenant-blue", network_type: "cudn", role: "primary", action: "create" }],
      storage_classes: [],
      namespaces: [],
      warnings: ["Namespace 'x' carries the label but no network"],
    };
    const calls = stubFetch((href, opts) =>
      JSON.parse(opts.body ?? "{}").dry_run === false ? { ...preview, applied: true } : preview,
    );
    const onApplied = vi.fn();
    render(<ImportCatalogModal isOpen targetId={1} onClose={() => {}} onApplied={onApplied} />);
    fireEvent.change(screen.getByLabelText("Pasted cluster YAML"), {
      target: { value: "kind: List\nitems: []" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("tenant-blue");
    expect(screen.getByText(/carries the label/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    const bodies = calls.map((c) => JSON.parse(c.body));
    expect(bodies.map((b) => b.dry_run)).toEqual([true, false]);
  });

  it("surfaces a parse error", async () => {
    global.fetch = vi.fn(async () => ({
      ok: false,
      status: 422,
      json: async () => ({ detail: "Not valid YAML/JSON" }),
      text: async () => JSON.stringify({ detail: "Not valid YAML/JSON" }),
    }));
    render(<ImportCatalogModal isOpen targetId={1} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText("Pasted cluster YAML"), { target: { value: "a: [" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText(/Not valid YAML/);
  });
});

describe("wave mapping summary", () => {
  const WAVE = {
    mapping_summary: {
      networks: [
        { source: "App", kind: "primary_udn", target: "tenant-blue", vm_count: 3 },
        { source: "Backup", kind: "ignored", target: null, vm_count: 3 },
      ],
      storage: [
        {
          source: "ds1",
          storage_class: "ontap-san",
          access_mode: "ReadWriteMany",
          volume_mode: "Block",
          offload: "ontap",
          vm_count: 3,
        },
      ],
    },
    mapping_findings: [
      { code: "storage.x", severity: "warning", message: "Decide this", vm_count: 0 },
    ],
  };

  it("renders networks, storage and findings", () => {
    render(<WaveMappingSummary wave={WAVE} />);
    expect(screen.getByText("tenant-blue")).toBeTruthy();
    expect(screen.getByText("Dropped")).toBeTruthy();
    expect(screen.getByText("Offload: ontap")).toBeTruthy();
    expect(screen.getByText("Decide this")).toBeTruthy();
  });

  it("renders nothing for plans generated before the summary existed", () => {
    const { container } = render(<WaveMappingSummary wave={{}} />);
    expect(container.innerHTML).toBe("");
    expect(waveMappingBadges(undefined)).toEqual([]);
  });

  it("derives header badges", () => {
    const texts = waveMappingBadges(WAVE).map((b) => b.text);
    expect(texts).toEqual(["Primary UDN", "1 network(s) dropped", "Copy offload"]);
  });
});
