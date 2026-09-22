// The assessment is only useful if the operator can read it: what is wrong,
// what to do, and — just as important — what could NOT be checked.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import VMDetailPage from "../pages/VMDetailPage";

const VM = { id: 5, name: "pacs-db-007", status: "discovered", lifecycle_state: "available", moref: "vm-1207" };

function stub(assessment) {
  global.fetch = vi.fn(async (url) => {
    const href = String(url);
    let payload = [];
    if (/\/api\/vms\/5\/assessment$/.test(href)) payload = assessment;
    else if (/\/api\/vms\/5$/.test(href)) payload = VM;
    else if (/validation\/latest/.test(href)) payload = { validation: null };
    else if (/baseline\/profile/.test(href)) payload = {};
    return { ok: true, status: 200, json: async () => payload, text: async () => "" };
  });
}

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/vms/5"]}>
      <Routes>
        <Route path="/vms/:id" element={<VMDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

describe("VM migratability tab", () => {
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows each finding with its fix, MTV concern id, and what couldn't be checked", async () => {
    stub({
      status: "warning",
      findings: [
        {
          id: "vmware.disk.rdm.detected",
          category: "Warning",
          label: "Raw Device Mapped disk detected",
          assessment: "RDM disks are not supported when using VDDK transfer.",
          remediation: "Convert the RDM to a VMDK before migration.",
          evidence: { disks: ["Hard disk 3"] },
          applies_to: "all",
        },
      ],
      not_evaluated: [{ id: "vmware.tpm.detected", label: "TPM detected", reason: "no vTPM flag in RVTools" }],
    });
    renderPage();
    await screen.findByText("Raw Device Mapped disk detected");
    expect(screen.getByText(/Convert the RDM to a VMDK/)).toBeTruthy();
    expect(screen.getByText("vmware.disk.rdm.detected")).toBeTruthy();
    expect(screen.getByText(/Hard disk 3/)).toBeTruthy();

    fireEvent.click(screen.getByText("1 check(s) could not be evaluated from the export"));
    expect(await screen.findByText("no vTPM flag in RVTools")).toBeTruthy();
  });

  it("says plainly when a hand-added VM has not been assessed", async () => {
    stub({ status: "unknown", findings: [], not_evaluated: [] });
    renderPage();
    await screen.findByText("This VM has not been assessed");
  });
});
