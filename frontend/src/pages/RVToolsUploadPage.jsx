// Import inventory from an RVTools export.
//
// The browser uploads the file and polls a server-side job; it never
// parses. Stages follow the job's status:
//
//   pick → (uploaded | scanning) → awaiting_mapping → importing → done
//
// The routing step is the one that matters. An RVTools export can span
// several vCenters, and a VM whose vCenter hostname isn't routed to a
// registered source has nowhere to go — so unrouted hostnames are called
// out before anything is written rather than silently dropped.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  FormSelect,
  FormSelectOption,
  Label,
  PageSection,
  Progress,
  ProgressMeasureLocation,
  Radio,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import { asArray } from "../utils/asArray";
import { fetchJSON } from "../utils/fetchJSON";

const UNROUTED = "__none__";
const POLL_MS = 1000;
const ACTIVE = ["uploaded", "scanning", "importing"];
const REJECT_PREVIEW = 25;

const fmtElapsed = (s) => {
  if (s == null) return "—";
  const total = Math.round(s);
  return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${total % 60}s`;
};

export default function RVToolsUploadPage() {
  const navigate = useNavigate();
  const fileRef = useRef(null);

  const [job, setJob] = useState(null);
  const [vcenters, setVcenters] = useState([]);
  const [mapping, setMapping] = useState({});
  const [mode, setMode] = useState("upsert");
  const [issues, setIssues] = useState({ items: [], total: 0 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const status = job?.status ?? "pick";
  const detected = asArray(job?.detected_vcenters);
  const routedCount = detected
    .filter((d) => (mapping[d?.hostname ?? ""] ?? UNROUTED) !== UNROUTED)
    .reduce((n, d) => n + (d?.vm_count ?? 0), 0);
  const totalVms = detected.reduce((n, d) => n + (d?.vm_count ?? 0), 0);

  // Poll while the server is working.
  useEffect(() => {
    if (!job?.id || !ACTIVE.includes(job.status)) return undefined;
    const timer = setTimeout(async () => {
      try {
        setJob(await fetchJSON(`/api/imports/${job.id}`));
      } catch (e) {
        // A missed poll is not a failed import — say so and keep trying.
        setError(`Lost contact while polling (${e?.message ?? "unknown"}). Retrying…`);
        setJob((j) => (j ? { ...j } : j));
      }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [job]);

  // Pre-fill routing from the scan's suggestions, once.
  useEffect(() => {
    if (job?.status !== "awaiting_mapping") return;
    setMapping((current) => {
      if (Object.keys(current).length > 0) return current;
      const next = {};
      for (const d of asArray(job?.detected_vcenters)) {
        next[d?.hostname ?? ""] = d?.suggested_vcenter_id ? String(d.suggested_vcenter_id) : UNROUTED;
      }
      return next;
    });
  }, [job]);

  // Load the problem rows when the job finishes.
  useEffect(() => {
    if (!job?.id || job.status !== "completed") return;
    if ((job.rows_rejected ?? 0) + (job.rows_warned ?? 0) === 0) return;
    fetchJSON(`/api/imports/${job.id}/rejects?limit=${REJECT_PREVIEW}`)
      .then((r) => setIssues({ items: asArray(r?.items), total: r?.total ?? 0 }))
      .catch(() => setIssues({ items: [], total: 0 }));
  }, [job?.id, job?.status, job?.rows_rejected, job?.rows_warned]);

  const pick = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const registered = await fetchJSON("/api/sources/vcenters").catch(() => []);
      setVcenters(asArray(registered));
      const form = new FormData();
      form.append("file", file);
      // Uploading a large workbook over a slow link can outlast the default.
      setJob(await fetchJSON("/api/imports/rvtools", { method: "POST", body: form, timeoutMs: 300000 }));
      setMapping({});
      setIssues({ items: [], total: 0 });
    } catch (e) {
      setError(e?.message ?? "Upload failed");
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const vcenter_mapping = Object.fromEntries(
        Object.entries(mapping)
          .filter(([, id]) => id && id !== UNROUTED)
          .map(([host, id]) => [host, Number(id)]),
      );
      setJob(
        await fetchJSON(`/api/imports/${job.id}/start`, {
          method: "POST",
          body: { vcenter_mapping, mode },
        }),
      );
    } catch (e) {
      setError(e?.message ?? "Could not start the import");
    } finally {
      setBusy(false);
    }
  };

  const cancel = useCallback(async () => {
    if (!job?.id) return;
    try {
      setJob(await fetchJSON(`/api/imports/${job.id}/cancel`, { method: "POST" }));
      toast("Cancel requested");
    } catch (e) {
      setError(e?.message ?? "Could not cancel");
    }
  }, [job?.id]);

  const reset = () => {
    setJob(null);
    setMapping({});
    setError(null);
    setIssues({ items: [], total: 0 });
  };

  const frameProps = {
    title: "Import VM inventory",
    description:
      "Upload an RVTools export (.xlsx) or a CSV. The file is parsed on the appliance; nothing leaves it.",
    breadcrumbs: [
      { label: "Discover" },
      { label: "Virtual machines", to: "/inventory" },
      { label: "Import" },
    ],
  };

  const percent = job?.progress_percent;
  const working = ACTIVE.includes(status);
  const result = job?.result ?? {};
  const envDist = Object.entries(result?.environment_distribution ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <PageFrame {...frameProps}>
      <PageSection hasBodyWrapper={false}>
        {error && (
          <Alert variant="danger" isInline title="Import problem" className="pf-v6-u-mb-md">
            {error}
          </Alert>
        )}

        {status === "pick" && (
          <Card>
            <CardTitle>Choose a file</CardTitle>
            <CardBody>
              <Content component="p" className="pf-v6-u-mb-md">
                Use RVTools&apos; <strong>Export all to Excel</strong>. The <code>vInfo</code> sheet
                is required; <code>vDisk</code>, <code>vSnapshot</code>, <code>vNetwork</code>,{" "}
                <code>vCPU</code> and <code>vMemory</code> are read when present and are what let
                VirtValidate spot RDMs, independent and shared disks, snapshots and hot-add before
                migration day. A CSV needs at least a VM name column.
              </Content>
              <Button
                variant="primary"
                onClick={() => fileRef.current?.click()}
                isLoading={busy}
                isDisabled={busy}
              >
                {busy ? "Uploading…" : "Select file"}
              </Button>
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.csv"
                onChange={pick}
                hidden
                aria-label="Inventory file"
              />
            </CardBody>
          </Card>
        )}

        {working && (
          <Card>
            <CardTitle>
              {status === "importing" ? "Importing" : "Reading"} {job?.filename}
            </CardTitle>
            <CardBody>
              <Progress
                value={percent ?? 0}
                title={job?.progress_message ?? "Working"}
                measureLocation={percent == null ? ProgressMeasureLocation.none : ProgressMeasureLocation.outside}
                aria-label="Import progress"
              />
              <DescriptionList isCompact isHorizontal className="pf-v6-u-mt-md">
                <DescriptionListGroup>
                  <DescriptionListTerm>Sheet</DescriptionListTerm>
                  <DescriptionListDescription>{job?.current_sheet ?? "—"}</DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Rows processed</DescriptionListTerm>
                  <DescriptionListDescription>
                    {(job?.rows_read ?? 0).toLocaleString()}
                    {job?.rows_total ? ` of ${job.rows_total.toLocaleString()}` : ""}
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>VMs written</DescriptionListTerm>
                  <DescriptionListDescription>
                    {(job?.rows_valid ?? 0).toLocaleString()} ({job?.rows_rejected ?? 0} rejected)
                  </DescriptionListDescription>
                </DescriptionListGroup>
                <DescriptionListGroup>
                  <DescriptionListTerm>Elapsed</DescriptionListTerm>
                  <DescriptionListDescription>{fmtElapsed(job?.elapsed_seconds)}</DescriptionListDescription>
                </DescriptionListGroup>
              </DescriptionList>
              <Button
                variant="secondary"
                className="pf-v6-u-mt-md"
                onClick={cancel}
                isDisabled={job?.cancel_requested}
              >
                {job?.cancel_requested ? "Cancelling after this batch…" : "Cancel import"}
              </Button>
            </CardBody>
          </Card>
        )}

        {status === "awaiting_mapping" && (
          <>
            <Card className="pf-v6-u-mb-md">
              <CardTitle>Scanned {job?.filename}</CardTitle>
              <CardBody>
                <DescriptionList isCompact isHorizontal>
                  <DescriptionListGroup>
                    <DescriptionListTerm>VM rows</DescriptionListTerm>
                    <DescriptionListDescription>{totalVms.toLocaleString()}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Sheets found</DescriptionListTerm>
                    <DescriptionListDescription>
                      {asArray(result?.sheets_found).join(", ") || "—"}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Will import</DescriptionListTerm>
                    <DescriptionListDescription>
                      {routedCount.toLocaleString()} of {totalVms.toLocaleString()}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>

            {!asArray(result?.sheets_found).includes("vDisk") && (
              <Alert variant="info" isInline title="No vDisk sheet in this file" className="pf-v6-u-mb-md">
                VMs will import, but disk-level migration blockers (RDM, independent and shared
                disks) can&apos;t be assessed. Re-export with all sheets to include them.
              </Alert>
            )}

            {routedCount < totalVms && (
              <Alert
                variant="warning"
                isInline
                title={`${(totalVms - routedCount).toLocaleString()} VM(s) have nowhere to go`}
                className="pf-v6-u-mb-md"
              >
                Their vCenter hostname is not routed to a registered source. Route it below, or{" "}
                <Link to="/sources/vcenters">register that vCenter</Link> first. Unrouted VMs are
                skipped, not guessed at.
              </Alert>
            )}

            <Card className="pf-v6-u-mb-md">
              <CardTitle>Route detected vCenters</CardTitle>
              <CardBody>
                <Table aria-label="vCenter routing" variant="compact">
                  <Thead>
                    <Tr>
                      <Th width={40}>Detected hostname</Th>
                      <Th width={15}>VMs</Th>
                      <Th width={45}>Import into</Th>
                    </Tr>
                  </Thead>
                  <Tbody>
                    {detected.map((d) => {
                      const host = d?.hostname ?? "";
                      return (
                        <Tr key={host || "(blank)"}>
                          <Td dataLabel="Detected hostname">{host || <em>(no vCenter column)</em>}</Td>
                          <Td dataLabel="VMs">{(d?.vm_count ?? 0).toLocaleString()}</Td>
                          <Td dataLabel="Import into">
                            <FormSelect
                              aria-label={`Target for ${host || "rows without a vCenter"}`}
                              value={mapping[host] ?? UNROUTED}
                              onChange={(_e, v) => setMapping((m) => ({ ...m, [host]: v }))}
                            >
                              <FormSelectOption value={UNROUTED} label="Skip these VMs" />
                              {vcenters.map((v) => (
                                <FormSelectOption
                                  key={v?.id}
                                  value={String(v?.id)}
                                  label={`${v?.name} (${v?.hostname})`}
                                />
                              ))}
                            </FormSelect>
                          </Td>
                        </Tr>
                      );
                    })}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>

            <Card className="pf-v6-u-mb-md">
              <CardTitle>If a VM is already in inventory</CardTitle>
              <CardBody>
                <Radio
                  id="mode-upsert"
                  name="import-mode"
                  label="Update it, and flag VMs that are no longer in the export"
                  description="Re-importing the same file changes nothing. Environment labels you set by hand are kept."
                  isChecked={mode === "upsert"}
                  onChange={() => setMode("upsert")}
                />
                <Radio
                  id="mode-create"
                  name="import-mode"
                  label="Leave it alone — only add new VMs"
                  isChecked={mode === "create_only"}
                  onChange={() => setMode("create_only")}
                  className="pf-v6-u-mt-sm"
                />
              </CardBody>
            </Card>

            <Button variant="primary" onClick={start} isDisabled={busy || routedCount === 0} isLoading={busy}>
              {`Import ${routedCount.toLocaleString()} VM(s)`}
            </Button>
            <Button variant="link" onClick={cancel} isDisabled={busy}>
              Discard this upload
            </Button>
          </>
        )}

        {(status === "failed" || status === "cancelled") && (
          <Card>
            <CardTitle>{status === "failed" ? "Import failed" : "Import cancelled"}</CardTitle>
            <CardBody>
              <Alert
                variant={status === "failed" ? "danger" : "warning"}
                isInline
                title={job?.error_message ?? job?.progress_message ?? "The import did not finish."}
              >
                {(job?.rows_valid ?? 0) > 0 && (
                  <>
                    {(job?.rows_valid ?? 0).toLocaleString()} VM(s) were written before it stopped
                    and are kept. Uploading the same file again finishes the job without creating
                    duplicates.
                  </>
                )}
              </Alert>
              <Button variant="primary" className="pf-v6-u-mt-md" onClick={reset}>
                Choose a file
              </Button>
            </CardBody>
          </Card>
        )}

        {status === "completed" && (
          <>
            <Card className="pf-v6-u-mb-md">
              <CardTitle>Import complete — {fmtElapsed(job?.elapsed_seconds)}</CardTitle>
              <CardBody>
                <Table aria-label="Import results" variant="compact">
                  <Thead>
                    <Tr>
                      <Th width={40}>vCenter</Th>
                      <Th>Created</Th>
                      <Th>Updated</Th>
                      <Th>Unchanged</Th>
                      <Th>No longer in export</Th>
                    </Tr>
                  </Thead>
                  <Tbody>
                    {asArray(result?.per_vcenter).map((r) => (
                      <Tr key={r?.vcenter_id ?? r?.vcenter_name}>
                        <Td dataLabel="vCenter">{r?.vcenter_name}</Td>
                        <Td dataLabel="Created">{r?.created ?? 0}</Td>
                        <Td dataLabel="Updated">{r?.updated ?? 0}</Td>
                        <Td dataLabel="Unchanged">{r?.unchanged ?? 0}</Td>
                        <Td dataLabel="No longer in export">{r?.marked_missing ?? 0}</Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>

                {asArray(result?.skipped_unrouted).length > 0 && (
                  <Alert variant="warning" isInline isPlain className="pf-v6-u-mt-md" title="Skipped — not routed">
                    {asArray(result?.skipped_unrouted)
                      .map((s) => `${s?.hostname || "(no vCenter column)"}: ${s?.vm_count ?? 0}`)
                      .join(" · ")}
                  </Alert>
                )}

                {envDist.length > 0 && (
                  <Content component="p" className="pf-v6-u-mt-md">
                    <strong>Environments detected:</strong>{" "}
                    {envDist.map(([env, n]) => (
                      <Label key={env} isCompact color={env === "unset" ? "orange" : "blue"} className="pf-v6-u-mr-sm">
                        {env}: {n}
                      </Label>
                    ))}
                  </Content>
                )}
              </CardBody>
            </Card>

            {(job?.rows_rejected ?? 0) + (job?.rows_warned ?? 0) > 0 && (
              <Card className="pf-v6-u-mb-md">
                <CardTitle>
                  {job?.rows_rejected ?? 0} row(s) not imported, {job?.rows_warned ?? 0} imported with a warning
                </CardTitle>
                <CardBody>
                  <Table aria-label="Problem rows" variant="compact">
                    <Thead>
                      <Tr>
                        <Th>Sheet</Th>
                        <Th>Row</Th>
                        <Th>VM</Th>
                        <Th>Outcome</Th>
                        <Th width={50}>Reason</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {issues.items.map((i, n) => (
                        <Tr key={`${i?.sheet}-${i?.row_number}-${n}`}>
                          <Td dataLabel="Sheet">{i?.sheet}</Td>
                          <Td dataLabel="Row">{i?.row_number}</Td>
                          <Td dataLabel="VM">{i?.vm_name ?? "—"}</Td>
                          <Td dataLabel="Outcome">
                            <Label isCompact color={i?.severity === "warning" ? "orange" : "red"}>
                              {i?.severity === "warning" ? "imported" : "rejected"}
                            </Label>
                          </Td>
                          <Td dataLabel="Reason">{i?.reason}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                  {issues.total > issues.items.length && (
                    <Content component="small">
                      Showing {issues.items.length} of {issues.total}.
                    </Content>
                  )}
                  <div className="pf-v6-u-mt-md">
                    <Button variant="secondary" component="a" href={`/api/imports/${job?.id}/rejects.csv`} download>
                      Download full report (CSV)
                    </Button>
                  </div>
                </CardBody>
              </Card>
            )}

            <Button variant="primary" onClick={() => navigate("/inventory")}>
              Go to inventory
            </Button>
            <Button variant="link" onClick={reset}>
              Import another file
            </Button>
          </>
        )}
      </PageSection>
    </PageFrame>
  );
}
