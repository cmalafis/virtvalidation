// Import inventory from an RVTools export.
//
// Four stages: pick the file, review what was parsed, confirm how
// detected vCenter hostnames route to registered sources, import.
//
// The routing step is the one that matters. An RVTools export can span
// several vCenters, and a VM whose hostname doesn't resolve to a
// registered source has nowhere to go — so unroutable rows are called
// out before the import rather than silently dropped.

import { useMemo, useRef, useState } from "react";
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
  PageSection,
  Progress,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import { fetchJSON } from "../utils/fetchJSON";
import { parseCSV, parseXLSXRows, rowToPayload } from "../utils/parseRVTools";

const UNROUTED = "__none__";

export default function RVToolsUploadPage() {
  const navigate = useNavigate();
  const fileRef = useRef(null);

  const [stage, setStage] = useState("pick");
  const [filename, setFilename] = useState(null);
  const [vms, setVms] = useState([]);
  const [vcenters, setVcenters] = useState([]);
  const [mapping, setMapping] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  // Distinct source hostnames the parser detected, with counts.
  const detected = useMemo(() => {
    const counts = {};
    for (const vm of vms) {
      const host = vm.source_vcenter_hostname || "";
      counts[host] = (counts[host] ?? 0) + 1;
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [vms]);

  const routableCount = useMemo(
    () =>
      vms.filter((vm) => {
        const target = mapping[vm.source_vcenter_hostname || ""];
        return target && target !== UNROUTED;
      }).length,
    [vms, mapping],
  );

  const pick = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const isCsv = /\.csv$/i.test(file.name);
      const rows = isCsv ? parseCSV(await file.text()) : parseXLSXRows(await file.arrayBuffer());
      const payloads = rows.map(rowToPayload).filter((p) => p?.name);
      if (payloads.length === 0) {
        throw new Error("No usable VM rows found. Check that this is an RVTools vInfo export.");
      }

      const registered = await fetchJSON("/api/sources/vcenters").catch(() => []);
      setVcenters(Array.isArray(registered) ? registered : []);

      // Pre-route by exact hostname match; anything else the operator picks.
      const auto = {};
      for (const vm of payloads) {
        const host = vm.source_vcenter_hostname || "";
        if (auto[host] !== undefined) continue;
        const hit = (registered ?? []).find(
          (v) => v.hostname?.toLowerCase() === host.toLowerCase(),
        );
        auto[host] = hit ? String(hit.id) : UNROUTED;
      }

      setVms(payloads);
      setMapping(auto);
      setFilename(file.name);
      setStage("confirm");
    } catch (e) {
      setError(e?.message ?? "Could not read that file");
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  };

  const doImport = async () => {
    setBusy(true);
    setError(null);
    try {
      const vcenter_mapping = Object.fromEntries(
        Object.entries(mapping)
          .filter(([, id]) => id && id !== UNROUTED)
          .map(([host, id]) => [host, Number(id)]),
      );
      const routable = vms.filter(
        (vm) => vcenter_mapping[vm.source_vcenter_hostname || ""] !== undefined,
      );

      const imported = await fetchJSON("/api/rvtools/upload-multi-vcenter", {
        method: "POST",
        // Large imports take a while server-side; give this more room
        // than the default.
        timeoutMs: 180000,
        body: { vms: routable, vcenter_mapping },
      });
      setResult(imported);
      setStage("done");
      toast.success("Import complete");
    } catch (e) {
      // Never strand the operator in an "importing" state — drop back to
      // confirm with the error visible so they can adjust and retry
      // without re-uploading.
      setError(e?.message ?? "Import failed");
      setStage("confirm");
    } finally {
      setBusy(false);
    }
  };

  const frameProps = {
    title: "Import VM inventory",
    description:
      "Load an RVTools export (.xlsx) or a CSV. Parsing happens in your browser; only the parsed rows are sent.",
    breadcrumbs: [
      { label: "Discover" },
      { label: "Virtual machines", to: "/inventory" },
      { label: "Import" },
    ],
  };

  return (
    <PageFrame {...frameProps}>
      <PageSection hasBodyWrapper={false}>
        {error && (
          <Alert variant="danger" isInline title="Import problem" className="pf-v6-u-mb-md">
            {error}
          </Alert>
        )}

        {stage === "pick" && (
          <Card>
            <CardTitle>Choose a file</CardTitle>
            <CardBody>
              <Content component="p" className="pf-v6-u-mb-md">
                An RVTools <code>vInfo</code> export works directly. A CSV must
                carry at least a VM name column; see{" "}
                <Link to="/inventory">the inventory guide</Link> for the full
                column list.
              </Content>
              <Button
                variant="primary"
                onClick={() => fileRef.current?.click()}
                isLoading={busy}
                isDisabled={busy}
              >
                Select file
              </Button>
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xls,.csv"
                onChange={pick}
                style={{ display: "none" }}
              />
            </CardBody>
          </Card>
        )}

        {stage === "confirm" && (
          <>
            <Card className="pf-v6-u-mb-md">
              <CardTitle>Parsed {filename}</CardTitle>
              <CardBody>
                <DescriptionList isCompact isHorizontal>
                  <DescriptionListGroup>
                    <DescriptionListTerm>VM rows</DescriptionListTerm>
                    <DescriptionListDescription>{vms.length}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Detected vCenters</DescriptionListTerm>
                    <DescriptionListDescription>{detected.length}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Will import</DescriptionListTerm>
                    <DescriptionListDescription>
                      {routableCount} of {vms.length}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>

            {routableCount < vms.length && (
              <Alert
                variant="warning"
                isInline
                title={`${vms.length - routableCount} VM(s) have nowhere to go`}
                className="pf-v6-u-mb-md"
              >
                Their detected vCenter hostname is not routed to a registered
                source. Route it below, or{" "}
                <Link to="/sources/vcenters">register that vCenter</Link> first.
                Unrouted VMs are skipped, not guessed at.
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
                    {detected.map(([host, count]) => (
                      <Tr key={host || "(blank)"}>
                        <Td dataLabel="Detected hostname">
                          {host || <em>(not detected)</em>}
                        </Td>
                        <Td dataLabel="VMs">{count}</Td>
                        <Td dataLabel="Import into">
                          <FormSelect
                            aria-label={`Target for ${host}`}
                            value={mapping[host] ?? UNROUTED}
                            onChange={(_e, v) => setMapping((m) => ({ ...m, [host]: v }))}
                          >
                            <FormSelectOption value={UNROUTED} label="Skip these VMs" />
                            {vcenters.map((v) => (
                              <FormSelectOption
                                key={v.id}
                                value={String(v.id)}
                                label={`${v.name} (${v.hostname})`}
                              />
                            ))}
                          </FormSelect>
                        </Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>

            {busy && <Progress value={100} isIndeterminate={false} title="Importing…" />}

            <Button
              variant="primary"
              onClick={doImport}
              isDisabled={busy || routableCount === 0}
              isLoading={busy}
            >
              {`Import ${routableCount} VM(s)`}
            </Button>
            <Button
              variant="link"
              onClick={() => {
                setStage("pick");
                setVms([]);
                setError(null);
              }}
              isDisabled={busy}
            >
              Choose a different file
            </Button>
          </>
        )}

        {stage === "done" && (
          <Card>
            <CardTitle>Import complete</CardTitle>
            <CardBody>
              <Table aria-label="Import results" variant="compact">
                <Thead>
                  <Tr>
                    <Th width={40}>vCenter</Th>
                    <Th width={20}>Created</Th>
                    <Th width={20}>Updated</Th>
                    <Th width={20}>Skipped</Th>
                  </Tr>
                </Thead>
                <Tbody>
                  {(result?.imported_per_vcenter ?? []).map((r) => (
                    <Tr key={r.vcenter_id ?? r.vcenter_name}>
                      <Td dataLabel="vCenter">{r.vcenter_name}</Td>
                      <Td dataLabel="Created">{r.created ?? 0}</Td>
                      <Td dataLabel="Updated">{r.updated ?? 0}</Td>
                      <Td dataLabel="Skipped">{r.skipped ?? 0}</Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>

              <Button
                variant="primary"
                className="pf-v6-u-mt-md"
                onClick={() => navigate("/inventory")}
              >
                Go to inventory
              </Button>
            </CardBody>
          </Card>
        )}
      </PageSection>
    </PageFrame>
  );
}
