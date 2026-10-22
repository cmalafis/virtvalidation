// Migration plan detail — which VMs are in the plan, wave by wave, and
// how to change that.
//
// The planner decides wave structure from what the estate data shows.
// The operator knows things it does not — an undocumented dependency, a
// change window, two apps that must cut over together — so this page is
// also where a generated plan is rearranged: tick VMs (across waves),
// then move them to another wave, a new wave or another plan, remove
// them, or add available VMs. Every change is dry-run by the backend
// first; see pages/plan/PlanEditModal.jsx.
//
// Two views over the same rows and the same selection:
//   By wave — each wave lists its VMs (pages/plan/WaveSection.jsx)
//   All VMs — one sortable table with a Wave column, for big plans

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Flex,
  FlexItem,
  PageSection,
  Skeleton,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState } from "../common/EmptyStates";
import { asArray } from "../utils/asArray";
import { fetchJSON } from "../utils/fetchJSON";
import { saveResponseAsFile } from "../utils/download";
import AddVMsModal from "./plan/AddVMsModal";
import PlanEditModal from "./plan/PlanEditModal";
import PlanEditToolbar from "./plan/PlanEditToolbar";
import PlanVMTable from "./plan/PlanVMTable";
import WaveSection from "./plan/WaveSection";
import {
  buildRows,
  isReannotationPending,
  partitionLabel,
  rowMatches,
  waveNumberOf,
} from "./plan/planRows";

const POLL_MS = 3000;

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export default function PlanDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // Deep link from the plans list / a VM page: /plans/7?vm=123
  const focusVmId = Number(searchParams.get("vm")) || null;

  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [vmState, setVmState] = useState(null);
  const [vmError, setVmError] = useState(null);
  const [siblings, setSiblings] = useState([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [marking, setMarking] = useState(false);

  const [view, setView] = useState("waves");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState(() => new Set());
  const [selected, setSelected] = useState(() => new Map());
  const [editModal, setEditModal] = useState(null); // {mode, initialTarget}
  const [addOpen, setAddOpen] = useState(false);
  const didInitialExpand = useRef(false);

  const loadPlan = useCallback(async () => {
    try {
      const data = await fetchJSON(`/api/plans/${id}`);
      setPlan(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [id]);

  // Per-VM facts and per-wave run state. A failure here is not fatal:
  // the plan document already carries every VM name.
  const loadVms = useCallback(async () => {
    try {
      const data = await fetchJSON(`/api/plans/${id}/vms`);
      setVmState(data);
      setVmError(null);
    } catch (e) {
      setVmError(e);
    }
  }, [id]);

  const reload = useCallback(() => Promise.all([loadPlan(), loadVms()]), [loadPlan, loadVms]);

  useEffect(() => {
    setLoading(true);
    didInitialExpand.current = false;
    setSelected(new Map());
    setExpanded(new Set());
    reload();
  }, [reload]);

  const status = plan?.status;
  const generating = Boolean(plan) && !["complete", "failed", "migrated"].includes(status);
  const waves = useMemo(() => asArray(plan?.waves), [plan]);
  // After a manual edit the touched waves wait on a background LLM pass.
  const awaitingRationale = waves.some((w) => isReannotationPending(w));

  // Poll while generation is running, or while an LLM rationale is due.
  useEffect(() => {
    if (!generating && !awaitingRationale) return undefined;
    const t = setInterval(loadPlan, POLL_MS);
    return () => clearInterval(t);
  }, [generating, awaitingRationale, loadPlan]);

  // The per-VM detail is only meaningful once waves exist.
  const wasGenerating = useRef(false);
  useEffect(() => {
    if (wasGenerating.current && !generating) loadVms();
    wasGenerating.current = generating;
  }, [generating, loadVms]);

  const seriesId = plan?.series_id;
  const inSeries = (plan?.series_total ?? 1) > 1 && Boolean(seriesId);
  useEffect(() => {
    if (!inSeries) {
      setSiblings([]);
      return undefined;
    }
    let cancelled = false;
    fetchJSON(`/api/plans?limit=200&series_id=${encodeURIComponent(seriesId)}`)
      .then((rows) => {
        if (!cancelled) setSiblings(asArray(rows));
      })
      .catch(() => {
        if (!cancelled) setSiblings([]);
      });
    return () => {
      cancelled = true;
    };
  }, [inSeries, seriesId]);

  const rows = useMemo(() => buildRows(plan, vmState), [plan, vmState]);
  const rowsByWave = useMemo(() => {
    const map = new Map();
    rows.forEach((row) => {
      if (!map.has(row.wave_number)) map.set(row.wave_number, []);
      map.get(row.wave_number).push(row);
    });
    return map;
  }, [rows]);
  const waveStateByNumber = useMemo(
    () => new Map(asArray(vmState?.waves).map((w) => [w?.wave_number, w])),
    [vmState],
  );

  const query = search.trim();
  const matches = useMemo(
    () => (query ? rows.filter((r) => rowMatches(r, query)) : rows),
    [rows, query],
  );
  const matchWaves = useMemo(
    () => [...new Set(matches.map((r) => r.wave_number))].sort((a, b) => a - b),
    [matches],
  );
  let matchSummary = null;
  if (query && matches.length === 0) {
    matchSummary = `No VM in this plan matches "${query}".`;
  } else if (query) {
    matchSummary = `${matches.length} ${matches.length === 1 ? "match" : "matches"} in ${
      matchWaves.length === 1 ? "wave" : "waves"
    } ${matchWaves.slice(0, 12).join(", ")}${matchWaves.length > 12 ? "…" : ""}`;
  }

  // First render with waves: open the wave the deep link points at, or
  // the first wave, so the page never opens on a wall of closed cards.
  useEffect(() => {
    if (didInitialExpand.current || waves.length === 0) return;
    didInitialExpand.current = true;
    const focusRow = focusVmId != null ? rows.find((r) => r.vm_id === focusVmId) : null;
    setExpanded(new Set([focusRow ? focusRow.wave_number : waveNumberOf(waves[0])]));
  }, [waves, rows, focusVmId]);

  const canEdit = status === "complete";
  const selectedRows = useMemo(() => [...selected.values()], [selected]);

  // An edit (or a poll) can remove a selected VM from the plan.
  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev;
      const present = new Map(rows.map((r) => [r.vm_id, r]));
      const next = new Map();
      prev.forEach((_row, vmId) => {
        if (present.has(vmId)) next.set(vmId, present.get(vmId));
      });
      return next;
    });
  }, [rows]);

  const toggleRow = (row, on) =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (on) next.set(row.vm_id, row);
      else next.delete(row.vm_id);
      return next;
    });
  const toggleRows = (list, on) =>
    setSelected((prev) => {
      const next = new Map(prev);
      asArray(list).forEach((row) => {
        if (on) next.set(row.vm_id, row);
        else next.delete(row.vm_id);
      });
      return next;
    });
  const toggleExpand = (number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(number)) next.delete(number);
      else next.add(number);
      return next;
    });

  const markSucceeded = async () => {
    setMarking(true);
    try {
      await fetchJSON(`/api/plans/${id}/mark-succeeded`, { method: "POST" });
      toast.success("Plan marked as succeeded");
      reload();
    } catch (e) {
      toast.error(e?.message ?? "Could not mark the plan");
    } finally {
      setMarking(false);
    }
  };

  const remove = async () => {
    await fetchJSON(`/api/plans/${id}`, { method: "DELETE" });
    toast.success("Plan deleted — its VMs are available again");
    navigate("/plans");
  };

  const onEditApplied = (response, { targetPlanName } = {}) => {
    const moved = asArray(response?.moves).length;
    const mine = asArray(response?.plans).find((p) => p?.plan_id === plan?.id);
    const operation = response?.operation;
    if (operation === "remove") {
      toast.success(`Removed ${plural(moved, "VM")} — available in inventory again`);
    } else if (operation === "add") {
      toast.success(`Added ${plural(moved, "VM")} to the plan`);
    } else if (operation === "move_to_plan") {
      toast.success(`Moved ${plural(moved, "VM")} to ${targetPlanName ?? "the other plan"}`);
    } else {
      const created = asArray(mine?.created_waves);
      toast.success(
        created.length > 0
          ? `Moved ${plural(moved, "VM")} into new wave ${created[0]}`
          : `Moved ${plural(moved, "VM")}`,
      );
    }
    // Open the waves that changed so the result is on screen.
    setExpanded((prev) => {
      const next = new Set(prev);
      asArray(mine?.touched_waves).forEach((n) => next.add(n));
      return next;
    });
    setSelected(new Map());
    setEditModal(null);
    setAddOpen(false);
    reload();
  };

  const title = plan?.name || `Plan #${id}`;
  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Migrate" },
      { label: "Migration plans", to: "/plans" },
      { label: title },
    ],
  };

  if (error) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <ErrorEmptyState error={error} onRetry={loadPlan} isRetrying={loading} />
        </PageSection>
      </PageFrame>
    );
  }

  if (loading && !plan) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="40%" className="pf-v6-u-mb-md" />
          <Skeleton width="100%" height="200px" />
        </PageSection>
      </PageFrame>
    );
  }

  const partition = plan?.partition ?? vmState?.partition ?? null;
  const target = partitionLabel(partition);
  const excluded = asArray(plan?.excluded_vms);
  const waveChoices = waves.map((w) => ({
    wave_number: waveNumberOf(w),
    vm_count: asArray(w?.vm_ids).length,
  }));
  const visibleWaves = query
    ? waves.filter((w) => matchWaves.includes(waveNumberOf(w)))
    : waves;
  const otherBatches = siblings.filter((p) => p?.id !== plan?.id);

  // fetch + blob, not a bare href: on a 422 an href download saves the
  // JSON error body as the bundle instead of telling the operator why.
  const downloadAllYaml = async () => {
    try {
      const res = await fetch(`/api/plans/${id}/yaml`);
      await saveResponseAsFile(res, `plan-${id}-mtv-bundle.zip`);
    } catch (e) {
      toast.error(`Could not download the YAML: ${e?.message ?? "unknown error"}`);
    }
  };

  return (
    <PageFrame
      {...frameProps}
      description={plan?.summary || undefined}
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <Button variant="secondary" onClick={downloadAllYaml}>
              Download all YAML
            </Button>
          </FlexItem>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={markSucceeded}
              isDisabled={marking || status === "migrated"}
              isLoading={marking}
            >
              Mark succeeded
            </Button>
          </FlexItem>
          <FlexItem>
            <Button variant="link" isDanger onClick={() => setConfirmDelete(true)}>
              Delete plan
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      {status === "failed" && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="danger" isInline title="Plan generation failed">
            {plan?.error_message ?? "No detail was recorded."}
          </Alert>
        </PageSection>
      )}

      {generating && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="info" isInline title={`Generating — ${status}`}>
            Waves appear as the pipeline completes each stage.
          </Alert>
        </PageSection>
      )}

      {status === "migrated" && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="info" isInline isPlain title="This plan was marked succeeded">
            Its waves are a record of what was migrated and can no longer be rearranged.
          </Alert>
        </PageSection>
      )}

      {(plan?.series_total ?? 1) > 1 && (
        <PageSection hasBodyWrapper={false}>
          <Alert
            variant="info"
            isInline
            title={`Batch ${plan?.series_index ?? "?"} of ${plan.series_total}`}
          >
            This plan is one of {plan.series_total} generated from a single selection. Batches
            run in order; each is its own MTV Plan set.
            {otherBatches.length > 0 ? (
              <>
                {" "}
                Other batches:{" "}
                {otherBatches.map((p, i) => (
                  <span key={p.id}>
                    {i > 0 ? ", " : ""}
                    <Link to={`/plans/${p.id}`}>{p?.name ?? `Plan #${p.id}`}</Link>
                    {` (${asArray(p?.vm_ids).length} VMs)`}
                  </span>
                ))}
                .
              </>
            ) : (
              <>
                {" "}
                <Link to="/plans">See the whole series</Link>.
              </>
            )}
          </Alert>
        </PageSection>
      )}

      {excluded.length > 0 && (
        <PageSection hasBodyWrapper={false}>
          <Alert
            variant="warning"
            isInline
            title={`${excluded.length} VM${
              excluded.length === 1
                ? " from the original selection was"
                : "s from the original selection were"
            } not included`}
          >
            <ExpandableSection toggleText="Show which and why" isIndented>
              <Table aria-label="Excluded VMs" variant="compact" borders={false}>
                <Thead>
                  <Tr>
                    <Th>Name</Th>
                    <Th>Reason</Th>
                  </Tr>
                </Thead>
                <Tbody>
                  {excluded.map((e, i) => (
                    <Tr key={e?.vm_id ?? i}>
                      <Td dataLabel="Name">
                        {e?.vm_id != null ? (
                          <Link to={`/vms/${e.vm_id}`}>{e?.vm_name ?? `VM #${e.vm_id}`}</Link>
                        ) : (
                          e?.vm_name ?? "—"
                        )}
                      </Td>
                      <Td dataLabel="Reason">{e?.reason ?? "—"}</Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
            </ExpandableSection>
            They stay available in the inventory and can be planned once fixed.
          </Alert>
        </PageSection>
      )}

      <PageSection hasBodyWrapper={false}>
        <Card>
          <CardBody>
            <DescriptionList isCompact isHorizontal columnModifier={{ default: "3Col" }}>
              <DescriptionListGroup>
                <DescriptionListTerm>Waves</DescriptionListTerm>
                <DescriptionListDescription>{waves.length}</DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>VMs</DescriptionListTerm>
                <DescriptionListDescription>
                  {asArray(plan?.vm_ids).length}
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Migrates</DescriptionListTerm>
                <DescriptionListDescription>{target ?? "—"}</DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Model</DescriptionListTerm>
                <DescriptionListDescription>{plan?.model || "—"}</DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Created</DescriptionListTerm>
                <DescriptionListDescription>
                  {plan?.created_at ? new Date(plan.created_at).toLocaleString() : "—"}
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Mappings</DescriptionListTerm>
                <DescriptionListDescription>
                  {asArray(plan?.mapping_ids).length === 0
                    ? "—"
                    : asArray(plan.mapping_ids).map((mid) => (
                        <Link key={mid} to={`/mappings/${mid}`} className="pf-v6-u-mr-sm">
                          {`#${mid}`}
                        </Link>
                      ))}
                </DescriptionListDescription>
              </DescriptionListGroup>
            </DescriptionList>
          </CardBody>
        </Card>
      </PageSection>

      <PageSection hasBodyWrapper={false}>
        {waves.length === 0 ? (
          <Content component="p" className="pf-v6-u-color-200">
            {generating ? "No waves yet." : "This plan has no waves."}
          </Content>
        ) : (
          <>
            <PlanEditToolbar
              search={search}
              onSearch={setSearch}
              matchSummary={matchSummary}
              view={view}
              onView={setView}
              onExpandAll={() => setExpanded(new Set(waves.map((w) => waveNumberOf(w))))}
              onCollapseAll={() => setExpanded(new Set())}
              canEdit={canEdit}
              selectedCount={selected.size}
              onMoveToWave={() => setEditModal({ mode: "wave", initialTarget: "" })}
              onNewWave={() => setEditModal({ mode: "wave", initialTarget: "new" })}
              onMoveToPlan={() => setEditModal({ mode: "plan", initialTarget: "" })}
              onRemove={() => setEditModal({ mode: "remove", initialTarget: "" })}
              onClear={() => setSelected(new Map())}
              onAdd={() => setAddOpen(true)}
            />

            {vmError && (
              <Alert
                variant="warning"
                isInline
                title="VM details could not be loaded"
                className="pf-v6-u-mb-md"
                actionLinks={
                  <Button variant="link" isInline onClick={loadVms}>
                    Try again
                  </Button>
                }
              >
                Wave membership below is complete; environment, IP, HA family and baseline
                columns are blank until this loads.
              </Alert>
            )}

            {view === "all" ? (
              <Card>
                <CardBody>
                  <PlanVMTable
                    rows={matches}
                    selected={selected}
                    onToggle={toggleRow}
                    onToggleMany={toggleRows}
                    canSelect={canEdit}
                    showWave
                    highlightId={focusVmId}
                    ariaLabel="All VMs in this plan"
                  />
                </CardBody>
              </Card>
            ) : (
              visibleWaves.map((wave, i) => {
                const number = waveNumberOf(wave);
                const waveRows = rowsByWave.get(number) ?? [];
                const isExpanded = expanded.has(number);
                return (
                  <WaveSection
                    key={number || i}
                    plan={plan}
                    wave={wave}
                    number={number}
                    rows={waveRows}
                    visibleRows={query ? waveRows.filter((r) => rowMatches(r, query)) : waveRows}
                    waveState={waveStateByNumber.get(number)}
                    isExpanded={isExpanded}
                    isMatchOnly={Boolean(query) && !isExpanded}
                    onToggleExpand={toggleExpand}
                    selected={selected}
                    onToggle={toggleRow}
                    onToggleMany={toggleRows}
                    canEdit={canEdit}
                    highlightId={focusVmId}
                    onChanged={reload}
                  />
                );
              })
            )}
          </>
        )}
      </PageSection>

      <PlanEditModal
        isOpen={Boolean(editModal)}
        mode={editModal?.mode ?? "wave"}
        initialTarget={editModal?.initialTarget ?? ""}
        plan={plan}
        waves={waveChoices}
        rows={selectedRows}
        onClose={() => setEditModal(null)}
        onApplied={onEditApplied}
      />

      <AddVMsModal
        isOpen={addOpen}
        plan={plan}
        partition={partition}
        waves={waveChoices}
        onClose={() => setAddOpen(false)}
        onApplied={onEditApplied}
      />

      <ConfirmModal
        isOpen={confirmDelete}
        title="Delete this plan?"
        confirmLabel="Delete plan"
        isDanger
        onConfirm={remove}
        onClose={() => setConfirmDelete(false)}
      >
        Deleting the plan returns its VMs to the available state so they can be included in a
        new plan. The generated YAML is not deleted from anywhere you have already applied it.
      </ConfirmModal>
    </PageFrame>
  );
}
