// Single VM detail.
//
// Reads are independent and settled separately: a VM whose baseline
// profile 404s should still render its identity and validation, so one
// failing panel never blanks the page.
//
// Capture and validate are async (202 + task id), so both poll their task
// endpoint and report the terminal state rather than optimistically
// claiming success.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
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
  Flex,
  FlexItem,
  Gallery,
  GalleryItem,
  Grid,
  GridItem,
  Label,
  LabelGroup,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import ConfirmModal from "../common/ConfirmModal";
import FindingCard from "../common/FindingCard";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
  return String(value);
}

function Rows({ items }) {
  return (
    <DescriptionList isCompact isHorizontal>
      {items.map(([term, value]) => (
        <DescriptionListGroup key={term}>
          <DescriptionListTerm>{term}</DescriptionListTerm>
          <DescriptionListDescription>{value}</DescriptionListDescription>
        </DescriptionListGroup>
      ))}
    </DescriptionList>
  );
}

export default function VMDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();

  const [data, setData] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tab, setTab] = useState("validation");

  // Cleared on unmount so a poll started for this VM can't keep running
  // after the operator has navigated away.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const [vm, snapshots, profile, validation, audit] = await Promise.allSettled([
        fetchJSON(`/api/vms/${id}`),
        fetchJSON(`/api/vms/${id}/snapshots`),
        fetchJSON(`/api/vms/${id}/baseline/profile`),
        fetchJSON(`/api/vms/${id}/validation/latest`),
        fetchJSON(`/api/audit?resource_type=vm&limit=50`),
      ]);

      // The VM itself is the only hard requirement.
      if (vm.status === "rejected") throw vm.reason;

      const val = (r) => (r.status === "fulfilled" ? r.value : null);
      setData({
        vm: vm.value,
        snapshots: asArray(val(snapshots)),
        profile: val(profile),
        validation: val(validation)?.validation ?? null,
        audit: asArray(val(audit)).filter((a) => String(a?.resource_id) === String(id)),
      });
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // Shared driver for the two async task endpoints. Both return
  // {task_id} and expose a matching poll route.
  const runTask = async (kind) => {
    const label = kind === "capture" ? "Baseline capture" : "Validation";
    setBusy(kind);
    try {
      const started = await fetchJSON(`/api/vms/${id}/${kind}`, { method: "POST" });
      const taskId = started?.task_id;
      if (!taskId) throw new Error(`${label} did not return a task id`);

      toast(`${label} started`, { icon: "⏳" });
      const deadline = Date.now() + POLL_TIMEOUT_MS;

      while (true) {
        if (!alive.current) return;
        if (Date.now() > deadline) {
          toast.error(`${label} is still running after 10 minutes — check agent activity.`);
          return;
        }
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (!alive.current) return;

        const status = await fetchJSON(`/api/vms/${id}/${kind}/${taskId}`);
        if (status?.status === "completed") {
          toast.success(`${label} complete`);
          load();
          return;
        }
        if (status?.status === "failed") {
          toast.error(`${label} failed: ${status?.error ?? "no detail given"}`);
          load();
          return;
        }
      }
    } catch (e) {
      toast.error(`${label} could not start: ${e.message}`);
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const remove = async () => {
    await fetchJSON(`/api/vms/${id}`, { method: "DELETE" });
    toast.success("VM removed from inventory");
    navigate("/inventory");
  };

  const vm = data.vm;
  const title = vm?.name || `VM #${id}`;
  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Discover" },
      { label: "Virtual machines", to: "/inventory" },
      { label: title },
    ],
  };

  if (error) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
        </PageSection>
      </PageFrame>
    );
  }

  if (loading && !vm) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="50%" className="pf-v6-u-mb-md" />
          <Skeleton width="100%" height="160px" />
        </PageSection>
      </PageFrame>
    );
  }

  const validation = data.validation;
  const snapshots = data.snapshots ?? [];

  return (
    <PageFrame
      {...frameProps}
      description={fmt(vm?.notes) === "—" ? undefined : vm.notes}
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={() => runTask("capture")}
              isDisabled={Boolean(busy)}
              isLoading={busy === "capture"}
            >
              Capture baseline
            </Button>
          </FlexItem>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={() => runTask("validate")}
              isDisabled={Boolean(busy) || snapshots.length === 0}
              isLoading={busy === "validate"}
              // Validation diffs against a baseline; without one there is
              // nothing to compare and the run would be meaningless.
              title={snapshots.length === 0 ? "Capture a baseline first" : undefined}
            >
              Validate
            </Button>
          </FlexItem>
          <FlexItem>
            <Button variant="link" isDanger onClick={() => setConfirmDelete(true)}>
              Remove
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      <PageSection hasBodyWrapper={false}>
        <Gallery hasGutter minWidths={{ default: "320px" }}>
          <GalleryItem>
            <Card isFullHeight>
              <CardTitle>Identity</CardTitle>
              <CardBody>
                <Rows
                  items={[
                    ["Status", <StatusLabel key="s" kind="status" value={vm?.status} />],
                    [
                      "Plan state",
                      <StatusLabel key="l" kind="lifecycle" value={vm?.lifecycle_state} />,
                    ],
                    ["Source hostname", fmt(vm?.source_hostname)],
                    ["Target hostname", fmt(vm?.target_hostname)],
                    ["IP address", fmt(vm?.ip_address)],
                    ["OS family", fmt(vm?.os_family)],
                    ["Role", fmt(vm?.role)],
                    ["Environment", fmt(vm?.environment)],
                    ["Owner", fmt(vm?.owner)],
                    ["Application", fmt(vm?.application_hint)],
                  ]}
                />
              </CardBody>
            </Card>
          </GalleryItem>

          <GalleryItem>
            <Card isFullHeight>
              <CardTitle>Source (vSphere)</CardTitle>
              <CardBody>
                <Rows
                  items={[
                    ["Platform", fmt(vm?.current_platform)],
                    ["Cluster", fmt(vm?.vsphere_cluster)],
                    ["Folder", fmt(vm?.vsphere_folder)],
                    ["Networks", fmt(vm?.vsphere_networks)],
                    ["Datastores", fmt(vm?.vsphere_datastores)],
                    ["SSH", `${fmt(vm?.ssh_user)}@${fmt(vm?.ip_address)}:${fmt(vm?.ssh_port)}`],
                  ]}
                />
              </CardBody>
            </Card>
          </GalleryItem>

          <GalleryItem>
            <Card isFullHeight>
              <CardTitle>Target (OpenShift)</CardTitle>
              <CardBody>
                <Rows
                  items={[
                    ["Cluster", fmt(vm?.resolved_target_cluster_name)],
                    ["Namespace", fmt(vm?.resolved_target_namespace)],
                    ["Network", fmt(vm?.target_network_name)],
                    ["Network type", fmt(vm?.target_network_type)],
                    ["Storage class", fmt(vm?.target_storage_class_name)],
                    ["Access mode", fmt(vm?.access_mode)],
                  ]}
                />
              </CardBody>
            </Card>
          </GalleryItem>
        </Gallery>
      </PageSection>

      <PageSection hasBodyWrapper={false}>
        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="VM detail">
          <Tab eventKey="validation" title={<TabTitleText>Validation</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              {!validation ? (
                <Alert
                  variant="info"
                  isInline
                  title="This VM has not been validated"
                  actionLinks={
                    snapshots.length === 0 ? (
                      <Content component="small">
                        Capture a baseline first — validation compares live state against it.
                      </Content>
                    ) : null
                  }
                >
                  Run validation after the VM has migrated to compare its live
                  state against the pre-migration baseline.
                </Alert>
              ) : (
                <>
                  <Card className="pf-v6-u-mb-md">
                    <CardBody>
                      <Grid hasGutter>
                        <GridItem span={12}>
                          <Flex spaceItems={{ default: "spaceItemsSm" }}>
                            <FlexItem>
                              <StatusLabel kind="status" value={validation?.status} />
                            </FlexItem>
                            <FlexItem>
                              <Content component="small" className="pf-v6-u-color-200">
                                {validation?.validated_at
                                  ? new Date(validation.validated_at).toLocaleString()
                                  : ""}
                              </Content>
                            </FlexItem>
                          </Flex>
                        </GridItem>
                        <GridItem span={12}>
                          <Content component="p">{fmt(validation?.summary)}</Content>
                        </GridItem>
                      </Grid>
                    </CardBody>
                  </Card>

                  {(validation?.findings ?? []).length === 0 ? (
                    <Content component="p" className="pf-v6-u-color-200">
                      No findings recorded for this validation.
                    </Content>
                  ) : (
                    (validation.findings ?? []).map((f, i) => (
                      <FindingCard key={f.id ?? i} finding={f} />
                    ))
                  )}
                </>
              )}
            </div>
          </Tab>

          <Tab
            eventKey="baselines"
            title={<TabTitleText>{`Baselines (${snapshots.length})`}</TabTitleText>}
          >
            <div className="pf-v6-u-mt-md">
              {snapshots.length === 0 ? (
                <Alert variant="info" isInline title="No baseline captured yet">
                  A baseline is the pre-migration state validation compares
                  against. Capture several over a few days so normal variation
                  isn&apos;t mistaken for migration damage.
                </Alert>
              ) : (
                <Table aria-label="Baseline snapshots" variant="compact">
                  <Thead>
                    <Tr>
                      <Th width={10}>#</Th>
                      <Th width={30}>Collected</Th>
                      <Th width={20}>SSH user</Th>
                      <Th width={40}>Captured sections</Th>
                    </Tr>
                  </Thead>
                  <Tbody>
                    {snapshots.map((s) => {
                      // raw_data is the collector's section map; its keys
                      // are what was actually captured, which is the useful
                      // summary without dumping the whole payload here.
                      const sections = Object.keys(s?.raw_data ?? {});
                      return (
                        <Tr key={s.id}>
                          <Td dataLabel="#">{fmt(s?.snapshot_number)}</Td>
                          <Td dataLabel="Collected">
                            {s?.collected_at ? new Date(s.collected_at).toLocaleString() : "—"}
                          </Td>
                          <Td dataLabel="SSH user">{fmt(s?.ssh_user)}</Td>
                          <Td dataLabel="Captured sections">
                            {sections.length === 0 ? (
                              "—"
                            ) : (
                              <LabelGroup numLabels={5}>
                                {sections.map((k) => (
                                  <Label key={k} isCompact>
                                    {k}
                                  </Label>
                                ))}
                              </LabelGroup>
                            )}
                          </Td>
                        </Tr>
                      );
                    })}
                  </Tbody>
                </Table>
              )}
            </div>
          </Tab>

          <Tab eventKey="activity" title={<TabTitleText>Activity</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              {(data.audit ?? []).length === 0 ? (
                <Content component="p" className="pf-v6-u-color-200">
                  No recorded activity for this VM.
                </Content>
              ) : (
                <Table aria-label="VM activity" variant="compact">
                  <Thead>
                    <Tr>
                      <Th width={30}>Time</Th>
                      <Th width={40}>Action</Th>
                      <Th width={30}>Actor</Th>
                    </Tr>
                  </Thead>
                  <Tbody>
                    {data.audit.map((a) => (
                      <Tr key={a.id}>
                        <Td dataLabel="Time">
                          {a?.timestamp ? new Date(a.timestamp).toLocaleString() : "—"}
                        </Td>
                        <Td dataLabel="Action">{fmt(a?.action)}</Td>
                        <Td dataLabel="Actor">{fmt(a?.actor)}</Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              )}
            </div>
          </Tab>
        </Tabs>
      </PageSection>

      <ConfirmModal
        isOpen={confirmDelete}
        title="Remove this VM from inventory?"
        confirmLabel="Remove"
        isDanger
        requireTyped={vm?.name}
        onConfirm={remove}
        onClose={() => setConfirmDelete(false)}
      >
        {`${vm?.name} and its baselines and validation results will be deleted from VirtValidate. The VM itself is not touched. This cannot be undone.`}
      </ConfirmModal>

      {vm?.lifecycle_state === "planned" && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="info" isInline title="This VM belongs to a migration plan">
            Its plan state is <strong>planned</strong>, so it won&apos;t appear in the
            selector for new plans. See <Link to="/plans">migration plans</Link>.
          </Alert>
        </PageSection>
      )}
    </PageFrame>
  );
}
