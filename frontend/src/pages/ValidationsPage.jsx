// Validations.
//
// Was the dashboard's "validation" useState tab, which showed a progress
// bar, a VM list, and an inline detail panel for the selected VM.
//
// As a route it reports fleet validation progress and lists the VMs that
// have been through validation, linking to the VM detail page for the
// per-VM findings rather than duplicating that panel here.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bullseye,
  Button,
  Card,
  CardBody,
  Flex,
  FlexItem,
  PageSection,
  Progress,
  ProgressMeasureLocation,
  Skeleton,
  Content,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import {
  ErrorEmptyState,
  GuidedEmptyState,
  NO_VALIDATIONS,
} from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

// VM.status values that mean the VM has been through validation.
const VALIDATED_STATUSES = ["validated", "failed"];

export default function ValidationsPage() {
  const [stats, setStats] = useState(null);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ limit: "50", sort_by: "name", sort_order: "asc" });
      for (const s of VALIDATED_STATUSES) qs.append("status", s);

      const [statsRes, listRes] = await Promise.allSettled([
        fetchJSON("/api/vms/stats"),
        fetchJSON(`/api/vms?${qs}`),
      ]);

      if (listRes.status === "rejected") throw listRes.reason;

      setStats(statsRes.status === "fulfilled" ? statsRes.value : null);
      setItems(listRes.value?.items ?? []);
      setTotal(listRes.value?.total ?? 0);
      setError(null);
    } catch (e) {
      setError(e);
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const byStatus = stats?.by_status ?? {};
  const fleetTotal = stats?.total ?? 0;
  const validatedCount = (byStatus.validated ?? 0) + (byStatus.failed ?? 0);
  const pct = fleetTotal === 0 ? 0 : Math.round((validatedCount / fleetTotal) * 100);

  const actions = (
    <Flex spaceItems={{ default: "spaceItemsSm" }}>
      <FlexItem>
        <Button variant="secondary" onClick={load} isDisabled={loading}>
          Refresh
        </Button>
      </FlexItem>
      <FlexItem>
        <Button variant="primary" component={(p) => <Link to="/operations" {...p} />}>
          Run validation
        </Button>
      </FlexItem>
    </Flex>
  );

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && items.length === 0) {
      return Array.from({ length: 6 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={4}>
            <Skeleton screenreaderText="Loading validations" />
          </Td>
        </Tr>
      ));
    }

    if (items.length === 0) {
      return (
        <Tr>
          <Td colSpan={4}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_VALIDATIONS.title}
                body={NO_VALIDATIONS.body}
                primary={{ label: "Capture baselines", to: "/operations" }}
                secondary={{ label: "Review inventory", to: "/inventory" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return items.map((vm) => (
      <Tr key={vm.id}>
        <Td dataLabel="VM">
          <Link to={`/vms/${vm.id}`}>{vm.name}</Link>
        </Td>
        <Td dataLabel="Result">
          <StatusLabel kind="status" value={vm.status} />
        </Td>
        <Td dataLabel="Environment">{vm.environment ?? "—"}</Td>
        <Td dataLabel="Last modified">
          {vm?.updated_at ? new Date(vm.updated_at).toLocaleString() : "—"}
        </Td>
      </Tr>
    ));
  };

  return (
    <PageFrame
      title="Validations"
      description="After a wave migrates, validation SSHes into each VM and diffs its live state against the pre-migration baseline."
      breadcrumbs={[{ label: "Verify" }, { label: "Validations" }]}
      actions={actions}
    >
      <PageSection hasBodyWrapper={false}>
        <Card>
          <CardBody>
            <Progress
              value={pct}
              title="Fleet validation progress"
              measureLocation={ProgressMeasureLocation.outside}
              aria-label="Fleet validation progress"
            />
            <Content component="small" className="pf-v6-u-color-200">
              {loading && !stats
                ? "Loading…"
                : `${validatedCount} of ${fleetTotal} VMs validated`}
            </Content>
          </CardBody>
        </Card>
      </PageSection>

      <PageSection hasBodyWrapper={false}>
        {total > items.length && (
          <Content component="small" className="pf-v6-u-color-200">
            Showing the first {items.length} of {total}. Use{" "}
            <Link to="/inventory?status=validated&status=failed">inventory</Link> to
            filter and page through the rest.
          </Content>
        )}
        <Table aria-label="Validated virtual machines" variant="compact">
          <Thead>
            <Tr>
              <Th width={40}>VM</Th>
              <Th width={20}>Result</Th>
              <Th width={20}>Environment</Th>
              <Th width={20}>Last modified</Th>
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>
    </PageFrame>
  );
}
