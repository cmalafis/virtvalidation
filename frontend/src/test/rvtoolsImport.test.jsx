// Behavior tests for the server-side import flow.
//
// The smoke suite only ever sees the "choose a file" stage. The previous
// version of this page threw a TypeError the moment a file was picked and
// nothing caught it, because no test went past mount. These walk the whole
// job lifecycle: upload → route → import → report.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RVToolsUploadPage from "../pages/RVToolsUploadPage";

const JOB = {
  id: "job-1",
  filename: "export.xlsx",
  status: "awaiting_mapping",
  detected_vcenters: [
    { hostname: "vc-a.example", vm_count: 600, suggested_vcenter_id: 7 },
    { hostname: "vc-unknown.example", vm_count: 400, suggested_vcenter_id: null },
  ],
  result: { sheets_found: ["vInfo"] },
};

const DONE = {
  ...JOB,
  status: "completed",
  progress_percent: 100,
  elapsed_seconds: 4.2,
  rows_rejected: 1,
  rows_warned: 1,
  result: {
    sheets_found: ["vInfo"],
    per_vcenter: [{ vcenter_id: 7, vcenter_name: "East", created: 600, updated: 0, unchanged: 0, marked_missing: 0 }],
    skipped_unrouted: [{ hostname: "vc-unknown.example", vm_count: 400 }],
    environment_distribution: { production: 500, unset: 100 },
  },
};

function stub(sequence) {
  const calls = [];
  const polls = [...sequence];
  global.fetch = vi.fn(async (url, opts = {}) => {
    const href = String(url);
    calls.push({ href, method: opts.method ?? "GET", body: opts.body });
    let payload = {};
    if (/\/api\/sources\/vcenters/.test(href)) payload = [{ id: 7, name: "East", hostname: "vc-a.example" }];
    else if (/\/rejects/.test(href))
      payload = {
        total: 2,
        items: [
          { sheet: "vInfo", row_number: 9, vm_name: null, severity: "rejected", reason: "missing VM name" },
          { sheet: "vInfo", row_number: 12, vm_name: "orphan", severity: "warning", reason: "no datastore" },
        ],
      };
    else if (/\/api\/imports\/rvtools$/.test(href)) payload = JOB;
    else if (/\/start$/.test(href)) payload = { ...JOB, status: "importing", rows_read: 10, rows_total: 1000, progress_percent: 1 };
    else if (/\/api\/imports\/job-1$/.test(href)) payload = polls.shift() ?? DONE;
    return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) };
  });
  return calls;
}

const pickFile = () => {
  const file = new File(["x"], "export.xlsx");
  fireEvent.change(screen.getByLabelText("Inventory file"), { target: { files: [file] } });
};

describe("RVTools import flow", () => {
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("uploads the file as multipart and never parses it in the browser", async () => {
    const calls = stub([]);
    render(<MemoryRouter><RVToolsUploadPage /></MemoryRouter>);
    pickFile();
    await screen.findByText("Route detected vCenters");
    const upload = calls.find((c) => c.href.endsWith("/api/imports/rvtools"));
    expect(upload.method).toBe("POST");
    expect(upload.body).toBeInstanceOf(FormData);
    expect(upload.body.get("file").name).toBe("export.xlsx");
  });

  it("pre-routes suggested vCenters and warns loudly about the rest", async () => {
    stub([]);
    render(<MemoryRouter><RVToolsUploadPage /></MemoryRouter>);
    pickFile();
    await screen.findByText("400 VM(s) have nowhere to go");
    expect(screen.getByLabelText("Target for vc-a.example").value).toBe("7");
    expect(screen.getByRole("button", { name: "Import 600 VM(s)" })).toBeTruthy();
    // vInfo-only export: say what that costs
    expect(screen.getByText("No vDisk sheet in this file")).toBeTruthy();
  });

  it("starts with only the routed hostnames, shows progress, then the report", async () => {
    const calls = stub([DONE]);
    render(<MemoryRouter><RVToolsUploadPage /></MemoryRouter>);
    pickFile();
    fireEvent.click(await screen.findByRole("button", { name: "Import 600 VM(s)" }));

    await screen.findByText("Cancel import");
    const start = calls.find((c) => c.href.endsWith("/start"));
    expect(JSON.parse(start.body)).toEqual({ vcenter_mapping: { "vc-a.example": 7 }, mode: "upsert" });

    await screen.findByText(/Import complete/, {}, { timeout: 3000 });
    expect(screen.getByText("1 row(s) not imported, 1 imported with a warning")).toBeTruthy();
    await screen.findByText("missing VM name");
    expect(screen.getByText("vc-unknown.example: 400")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Download full report/ }).getAttribute("href")).toBe(
      "/api/imports/job-1/rejects.csv",
    );
  });

  it("explains a failed job and that partial work is kept", async () => {
    stub([]);
    global.fetch = vi.fn(async (url) => {
      const href = String(url);
      const payload = /vcenters/.test(href)
        ? []
        : { id: "job-1", filename: "bad.xlsx", status: "failed", rows_valid: 500, error_message: "Unexpected error: boom" };
      return { ok: true, status: 200, json: async () => payload, text: async () => "" };
    });
    render(<MemoryRouter><RVToolsUploadPage /></MemoryRouter>);
    pickFile();
    await screen.findByText("Unexpected error: boom");
    await waitFor(() => expect(screen.getByText(/500 VM\(s\) were written/)).toBeTruthy());
  });
});
