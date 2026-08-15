// Migration plans — the index.
//
// Was the dashboard's "migration plan" useState tab, which rendered only
// the single most recent plan's wave cards. As a route it lists every
// plan with its status and wave count, and links through to PlanView for
// the detail.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bullseye,
  Button,
  PageSection,
  Skeleton,
  Progress,
  ProgressMeasureLocation,
  ProgressVariant,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState, GuidedEmptyState, NO_PLANS } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

// Plan.status walks the pipeline: pending → partitioning →
// subpartitioning → splitting → packing → analyzing_concurrency →
// annotating → emitting_yaml → complete, or failed at any stage.
const TERMINAL = new Set(["complete", "failed", "migrated"]);

const STAGE_ORDER = [
  "pending",
  "partitioning",
  "subpartitioning",
  "splitting",
  "packing",
  "analyzing_concurrency",
  "annotating",
  "emitting_yaml",
  "complete",
];

function stageProgress(status) {
  const i = STAGE_ORDER.indexOf(status);
  if (i < 0) return 0;
  return Math.round((i / (STAGE_ORDER.length - 1)) * 100);
}

function humanize(s) {
  return String(s ?? "")
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

function PlanStatus({ plan }) {
  const status = plan?.status ?? "complete";

  if (status === "failed") {
    return (
      <>
        <StatusLabel kind="status" value="failed" />
        {plan?.error_message && (
          <div className="pf-v6-u-font-size-sm pf-v6-u-color-200 pf-v6-u-mt-xs">
            {plan.error_message}
          </div>
        )}
      </>
    );
  }

  if (TERMINAL.has(status)) {
    return <StatusLabel kind="record" value={status === "migrated" ? "complete" : status} />;
  }

  // Still generating — show which stage, since the LLM annotation step
  // can take a while and a bare spinner reads as "stuck".
  return (
    <Progress
      value={stageProgress(status)}
      title={humanize(status)}
      size="sm"
      measureLocation={ProgressMeasureLocation.none}
      variant={ProgressVariant.info}
      aria-label={`Plan ${plan.id} progress`}
    />
  );
}

export default function PlansPage() {
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON("/api/plans?limit=100");
      setPlans(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e) {
      setError(e);
      setPlans([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while any plan is mid-generation so the operator sees it land
  // without refreshing. Stops as soon as everything is terminal.
  const hasInFlight = plans.some((p) => !TERMINAL.has(p?.status ?? "complete"));
  useEffect(() => {
    if (!hasInFlight) return undefined;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [hasInFlight, load]);

  const actions = (
    <Button variant="primary" component={(p) => <Link to="/plans/new" {...p} />}>
      Generate plan
    </Button>
  );

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && plans.length === 0) {
      return Array.from({ length: 4 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={6}>
            <Skeleton screenreaderText="Loading plans" />
          </Td>
        </Tr>
      ));
    }

    if (plans.length === 0) {
      return (
        <Tr>
          <Td colSpan={6}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_PLANS.title}
                body={NO_PLANS.body}
                primary={{ label: "Generate a plan", to: "/plans/new" }}
                secondary={{ label: "Review inventory first", to: "/inventory" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return plans.map((plan) => {
      const waves = plan?.waves ?? [];
      return (
        <Tr key={plan.id}>
          <Td dataLabel="Plan">
            <Link to={`/plans/${plan.id}`}>{plan?.name || `Plan #${plan.id}`}</Link>
          </Td>
          <Td dataLabel="Status">
            <PlanStatus plan={plan} />
          </Td>
          <Td dataLabel="Waves">{waves.length}</Td>
          <Td dataLabel="VMs">{(plan?.vm_ids ?? []).length}</Td>
          <Td dataLabel="Model">{plan?.model ?? "—"}</Td>
          <Td dataLabel="Created">
            {plan?.created_at ? new Date(plan.created_at).toLocaleString() : "—"}
          </Td>
        </Tr>
      );
    });
  };

  return (
    <PageFrame
      title="Migration plans"
      description="Each plan groups VMs into dependency-ordered waves and emits the MTV YAML to run them."
      breadcrumbs={[{ label: "Migrate" }, { label: "Migration plans" }]}
      actions={actions}
    >
      <PageSection hasBodyWrapper={false}>
        <Table aria-label="Migration plans" variant="compact">
          <Thead>
            <Tr>
              <Th width={30}>Plan</Th>
              <Th width={25}>Status</Th>
              <Th width={10}>Waves</Th>
              <Th width={10}>VMs</Th>
              <Th width={10}>Model</Th>
              <Th width={15}>Created</Th>
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>
    </PageFrame>
  );
}
