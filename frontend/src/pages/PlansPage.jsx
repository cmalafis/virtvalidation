// Migration plans — the index.
//
// Lists every plan with its status, what it migrates (vCenter → cluster /
// namespace), and its wave and VM counts, and links through to
// PlanDetailPage. Plans generated from one selection (a batched series)
// are grouped under one expandable row instead of being scattered through
// a flat list, and "Find a VM" answers "which plan is this VM in?"
// without opening plans one by one.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bullseye,
  Button,
  Card,
  CardBody,
  Content,
  Label,
  List,
  ListItem,
  PageSection,
  Skeleton,
  Progress,
  ProgressMeasureLocation,
  ProgressVariant,
  SearchInput,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState, GuidedEmptyState, NO_PLANS } from "../common/EmptyStates";
import { asArray } from "../utils/asArray";
import { fetchJSON } from "../utils/fetchJSON";
import { partitionLabel } from "./plan/planRows";

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

const COLUMNS = 8;
const SEARCH_DEBOUNCE_MS = 300;
const VM_HITS = 10;

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

/**
 * Group the flat plan list for display: a batched series (one POST that
 * produced several plans) becomes one group; everything else stays a
 * group of one. Order follows the list (newest first).
 */
function groupBySeries(plans) {
  const groups = [];
  const bySeries = new Map();
  asArray(plans).forEach((plan) => {
    const seriesId = (plan?.series_total ?? 1) > 1 ? plan?.series_id : null;
    if (!seriesId) {
      groups.push({ key: `plan-${plan?.id}`, seriesId: null, plans: [plan] });
      return;
    }
    let group = bySeries.get(seriesId);
    if (!group) {
      group = { key: `series-${seriesId}`, seriesId, plans: [] };
      bySeries.set(seriesId, group);
      groups.push(group);
    }
    group.plans.push(plan);
  });
  groups.forEach((g) => g.plans.sort((a, b) => (a?.series_index ?? 0) - (b?.series_index ?? 0)));
  return groups;
}

const SEPARATOR = /[-_\s]/;

/**
 * What the batches of a series have in common in their names: the
 * operator's base name. Batches are named `base-1`, `base-2` … or
 * `base-<partition>-N`, so the common prefix is cut back to a whole
 * token (`q4-1` + `q4-2` → `q4`, not `q`; `base-vc01-…` + `base-vc02-…`
 * → `base`, not `base-vc0`).
 */
function seriesName(plans) {
  const names = plans.map((p) => p?.name ?? "").filter(Boolean);
  if (names.length === 0) return "Plan series";
  let prefix = names[0];
  names.forEach((n) => {
    while (prefix && !n.startsWith(prefix)) prefix = prefix.slice(0, -1);
  });
  const endsMidToken =
    !SEPARATOR.test(prefix.slice(-1)) &&
    names.some((n) => n.length > prefix.length && !SEPARATOR.test(n.charAt(prefix.length)));
  if (endsMidToken) {
    const cut = Math.max(prefix.lastIndexOf("-"), prefix.lastIndexOf("_"), prefix.lastIndexOf(" "));
    if (cut > 0) prefix = prefix.slice(0, cut);
  }
  prefix = prefix.replace(/[-_\s]+$/, "");
  return prefix || names[0];
}

function wasEdited(plan) {
  return asArray(plan?.waves).some((w) => w?.manual_edit);
}

function PlanRow({ plan, isChild }) {
  const waves = asArray(plan?.waves);
  return (
    <Tr>
      <Td />
      <Td dataLabel="Plan">
        <span className={isChild ? "pf-v6-u-pl-md" : undefined}>
          <Link to={`/plans/${plan.id}`}>{plan?.name || `Plan #${plan.id}`}</Link>
        </span>
        {(plan?.series_total ?? 1) > 1 && (
          <>
            {" "}
            <Label isCompact color="blue">
              {`Batch ${plan?.series_index ?? "?"} of ${plan.series_total}`}
            </Label>
          </>
        )}
        {wasEdited(plan) && (
          <>
            {" "}
            <Label isCompact color="teal">
              Edited
            </Label>
          </>
        )}
      </Td>
      <Td dataLabel="Migrates">{partitionLabel(plan?.partition) ?? "—"}</Td>
      <Td dataLabel="Status">
        <PlanStatus plan={plan} />
      </Td>
      <Td dataLabel="Waves">{waves.length}</Td>
      <Td dataLabel="VMs">{asArray(plan?.vm_ids).length}</Td>
      <Td dataLabel="Model">{plan?.model || "—"}</Td>
      <Td dataLabel="Created">
        {plan?.created_at ? new Date(plan.created_at).toLocaleString() : "—"}
      </Td>
    </Tr>
  );
}

function SeriesRows({ group, isCollapsed, onToggle, rowIndex }) {
  const { plans } = group;
  const vmTotal = plans.reduce((n, p) => n + asArray(p?.vm_ids).length, 0);
  const waveTotal = plans.reduce((n, p) => n + asArray(p?.waves).length, 0);
  const done = plans.filter((p) => TERMINAL.has(p?.status ?? "complete")).length;
  return (
    <Tbody>
      <Tr>
        <Td
          expand={{
            rowIndex,
            isExpanded: !isCollapsed,
            onToggle: () => onToggle(group.seriesId),
          }}
        />
        <Td dataLabel="Plan">
          <strong>{seriesName(plans)}</strong>{" "}
          <Label isCompact color="blue">{`${plans.length} batches`}</Label>
        </Td>
        <Td dataLabel="Migrates">
          {new Set(plans.map((p) => partitionLabel(p?.partition))).size === 1
            ? (partitionLabel(plans[0]?.partition) ?? "—")
            : "Several targets"}
        </Td>
        <Td dataLabel="Status">
          {done === plans.length ? "All batches generated" : `${done} of ${plans.length} generated`}
        </Td>
        <Td dataLabel="Waves">{waveTotal}</Td>
        <Td dataLabel="VMs">{vmTotal}</Td>
        <Td dataLabel="Model">{plans[0]?.model || "—"}</Td>
        <Td dataLabel="Created">
          {plans[0]?.created_at ? new Date(plans[0].created_at).toLocaleString() : "—"}
        </Td>
      </Tr>
      {!isCollapsed && plans.map((plan) => <PlanRow key={plan.id} plan={plan} isChild />)}
    </Tbody>
  );
}

export default function PlansPage() {
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(() => new Set());
  const [vmSearchDraft, setVmSearchDraft] = useState("");
  const [vmSearch, setVmSearch] = useState("");
  const [vmHits, setVmHits] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON("/api/plans?limit=200");
      setPlans(asArray(data));
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

  useEffect(() => {
    const t = setTimeout(() => setVmSearch(vmSearchDraft.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [vmSearchDraft]);

  // "Which plan is this VM in?" — the inventory rows of planned and
  // migrated VMs carry the plan that holds them.
  useEffect(() => {
    if (!vmSearch) {
      setVmHits(null);
      return undefined;
    }
    let cancelled = false;
    const qs = new URLSearchParams({ search: vmSearch, limit: String(VM_HITS), sort_by: "name" });
    qs.append("lifecycle_state", "planned");
    qs.append("lifecycle_state", "migrated");
    fetchJSON(`/api/vms?${qs}`)
      .then((data) => {
        if (!cancelled) setVmHits({ items: asArray(data), total: data?.total ?? 0 });
      })
      .catch(() => {
        if (!cancelled) setVmHits({ items: [], total: 0, failed: true });
      });
    return () => {
      cancelled = true;
    };
  }, [vmSearch]);

  const groups = useMemo(() => groupBySeries(plans), [plans]);
  const toggleSeries = (seriesId) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(seriesId)) next.delete(seriesId);
      else next.add(seriesId);
      return next;
    });

  const actions = (
    <Button variant="primary" component={(p) => <Link to="/plans/new" {...p} />}>
      Generate plan
    </Button>
  );

  const body = () => {
    if (error) {
      return (
        <Tbody>
          <Tr>
            <Td colSpan={COLUMNS}>
              <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
            </Td>
          </Tr>
        </Tbody>
      );
    }

    if (loading && plans.length === 0) {
      return (
        <Tbody>
          {Array.from({ length: 4 }).map((_, i) => (
            <Tr key={`sk-${i}`}>
              <Td colSpan={COLUMNS}>
                <Skeleton screenreaderText="Loading plans" />
              </Td>
            </Tr>
          ))}
        </Tbody>
      );
    }

    if (plans.length === 0) {
      return (
        <Tbody>
          <Tr>
            <Td colSpan={COLUMNS}>
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
        </Tbody>
      );
    }

    return groups.map((group, i) =>
      group.seriesId ? (
        <SeriesRows
          key={group.key}
          group={group}
          rowIndex={i}
          isCollapsed={collapsed.has(group.seriesId)}
          onToggle={toggleSeries}
        />
      ) : (
        <Tbody key={group.key}>
          <PlanRow plan={group.plans[0]} />
        </Tbody>
      ),
    );
  };

  const hits = asArray(vmHits?.items);

  return (
    <PageFrame
      title="Migration plans"
      description="Each plan groups VMs into dependency-ordered waves and emits the MTV YAML to run them."
      breadcrumbs={[{ label: "Migrate" }, { label: "Migration plans" }]}
      actions={actions}
    >
      <PageSection hasBodyWrapper={false}>
        <Toolbar aria-label="Plans toolbar">
          <ToolbarContent>
            <ToolbarItem className="pf-v6-u-w-33">
              <SearchInput
                aria-label="Find a VM in a plan"
                placeholder="Find a VM — which plan is it in?"
                value={vmSearchDraft}
                onChange={(_e, v) => setVmSearchDraft(v)}
                onClear={() => setVmSearchDraft("")}
              />
            </ToolbarItem>
          </ToolbarContent>
        </Toolbar>

        {vmSearch && vmHits && (
          <Card isCompact className="pf-v6-u-mb-md">
            <CardBody>
              {hits.length === 0 ? (
                <Content component="p" className="pf-v6-u-color-200">
                  {vmHits?.failed
                    ? "The search could not be run."
                    : `No VM matching "${vmSearch}" is in a plan. It may still be available in the inventory.`}
                </Content>
              ) : (
                <>
                  <List isPlain>
                    {hits.map((vm) => (
                      <ListItem key={vm.id}>
                        <strong>{vm?.name ?? `VM #${vm.id}`}</strong>
                        {" — "}
                        {vm?.plan_id != null ? (
                          <Link to={`/plans/${vm.plan_id}?vm=${vm.id}`}>
                            {vm?.plan_name ?? `Plan #${vm.plan_id}`}
                          </Link>
                        ) : (
                          "not in a current plan"
                        )}{" "}
                        <StatusLabel kind="lifecycle" value={vm?.lifecycle_state} />
                      </ListItem>
                    ))}
                  </List>
                  {(vmHits?.total ?? 0) > hits.length && (
                    <Content component="small" className="pf-v6-u-color-200">
                      {`Showing ${hits.length} of ${vmHits.total} — refine the search to narrow it.`}
                    </Content>
                  )}
                </>
              )}
            </CardBody>
          </Card>
        )}

        <Table aria-label="Migration plans" variant="compact">
          <Thead>
            <Tr>
              <Th screenReaderText="Expand series" />
              <Th width={25}>Plan</Th>
              <Th width={20}>Migrates</Th>
              <Th width={15}>Status</Th>
              <Th width={10}>Waves</Th>
              <Th width={10}>VMs</Th>
              <Th width={10}>Model</Th>
              <Th width={10}>Created</Th>
            </Tr>
          </Thead>
          {body()}
        </Table>
      </PageSection>
    </PageFrame>
  );
}
