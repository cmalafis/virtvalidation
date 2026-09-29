// Generate a migration plan.
//
// Three steps on PF's Wizard: name it, pick the mappings that cover the
// estate, pick the VMs. Generation is async, so the last step polls the
// plan's pipeline status and reports which stage it's on — the stages are
// named in plan_pipeline._PIPELINE_STAGE_TO_STATUS and the LLM annotation
// step is slow enough that a bare spinner reads as stuck.
//
// There is no selection cap. A selection larger than the batch size
// (settings.max_vms_per_plan, default 1000, READ from GET /api/settings)
// is generated as a numbered series of plans — name-1, name-2, … — each
// holding at most that many VMs. The only ceiling is the per-request
// guard (settings.max_vms_per_plan_request). Before generating, the
// wizard asks POST /api/plans/preview for the exact plan names and for
// every VM that will be skipped (assessment-blocked, unresolvable
// target), so the operator knows what they will get before they wait.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Alert,
  Bullseye,
  Button,
  Card,
  CardBody,
  Checkbox,
  Content,
  ExpandableSection,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  List,
  ListItem,
  PageSection,
  Pagination,
  Progress,
  SearchInput,
  Skeleton,
  Radio,
  TextInput,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
  Wizard,
  WizardStep,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { GuidedEmptyState, NoResultsEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

// Fallbacks only. Both numbers are deploy-time config and are read from
// GET /api/settings — a hardcoded value here previously gated operators
// at 250 while the API happily accepted 1000.
const BATCH_SIZE_FALLBACK = 1000;
const REQUEST_CEILING_FALLBACK = 10000;
const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;
const PREVIEW_DEBOUNCE_MS = 500;
const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

// Human-readable pipeline stages, in order. Mirrors
// plan_pipeline._PIPELINE_STAGE_TO_STATUS.
const STAGES = [
  ["pending", "Queued"],
  ["partitioning", "Partitioning by vCenter, namespace and environment"],
  ["subpartitioning", "Sub-partitioning by network, datastore and role"],
  ["splitting", "Splitting over-concentrated HA families"],
  ["packing", "Packing VMs into waves"],
  ["analyzing_concurrency", "Working out which waves can run in parallel"],
  ["annotating", "Writing per-wave rationale (LLM)"],
  ["emitting_yaml", "Emitting MTV YAML"],
  ["complete", "Complete"],
];

function stageLabel(status) {
  return STAGES.find(([k]) => k === status)?.[1] ?? status ?? "Working";
}

function stagePercent(status) {
  const i = STAGES.findIndex(([k]) => k === status);
  return i < 0 ? 0 : Math.round((i / (STAGES.length - 1)) * 100);
}

// VMs the backend will leave out of the plan, with the reason. Rendered
// both in the pre-generation preview and on the single-plan progress
// screen; the plan detail page shows the persisted copy afterwards.
function ExcludedVMs({ excluded, title }) {
  const rows = asArray(excluded);
  if (rows.length === 0) return null;
  return (
    <Alert variant="warning" isInline title={title} className="pf-v6-u-mb-md">
      <ExpandableSection toggleText={`Show the ${rows.length} VM${rows.length === 1 ? "" : "s"}`} isIndented>
        <Table aria-label="Excluded VMs" variant="compact" borders={false}>
          <Thead>
            <Tr>
              <Th>Name</Th>
              <Th>Reason</Th>
            </Tr>
          </Thead>
          <Tbody>
            {rows.map((e, i) => (
              <Tr key={e?.vm_id ?? i}>
                <Td dataLabel="Name">{e?.vm_name ?? `VM #${e?.vm_id ?? "?"}`}</Td>
                <Td dataLabel="Reason">{e?.reason ?? "—"}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      </ExpandableSection>
      These VMs stay available in the inventory and can be planned separately once fixed.
    </Alert>
  );
}

export default function PlanWizardPage() {
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [migrationType, setMigrationType] = useState("cold");
  const [batchSize, setBatchSize] = useState(BATCH_SIZE_FALLBACK);
  const [requestCeiling, setRequestCeiling] = useState(REQUEST_CEILING_FALLBACK);
  const [mappings, setMappings] = useState([]);
  const [mappingIds, setMappingIds] = useState(() => new Set());
  const [mappingsLoading, setMappingsLoading] = useState(true);

  const [vcenters, setVcenters] = useState([]);
  const [environments, setEnvironments] = useState([]);
  const [vcenterId, setVcenterId] = useState("");
  const [environment, setEnvironment] = useState("");

  const [vms, setVms] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [vmsLoading, setVmsLoading] = useState(true);
  const [selectingAll, setSelectingAll] = useState(false);

  const [selected, setSelected] = useState(() => new Map());
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState(null);
  const [excludedAtSubmit, setExcludedAtSubmit] = useState([]);
  const [error, setError] = useState(null);

  // Batch size + request ceiling are deploy-time config, so read them
  // rather than assume them.
  useEffect(() => {
    fetchJSON("/api/settings")
      .then((cfg) => {
        if (Number.isFinite(cfg?.max_vms_per_plan)) setBatchSize(cfg.max_vms_per_plan);
        if (Number.isFinite(cfg?.max_vms_per_plan_request)) {
          setRequestCeiling(cfg.max_vms_per_plan_request);
        }
      })
      .catch(() => {
        /* Fallbacks already applied; missing config shouldn't block planning. */
      });
  }, []);

  useEffect(() => {
    fetchJSON("/api/mappings")
      .then((rows) => setMappings(Array.isArray(rows) ? rows : []))
      .catch(() => setMappings([]))
      .finally(() => setMappingsLoading(false));
  }, []);

  // Filter facets. vCenters come from the registry so the dropdown shows
  // names; environments come from the facet counts over available VMs.
  useEffect(() => {
    fetchJSON("/api/sources/vcenters")
      .then((rows) => setVcenters(asArray(rows)))
      .catch(() => setVcenters([]));
    fetchJSON("/api/vms/facets?lifecycle_state=available")
      .then((f) => setEnvironments(Object.keys(f?.environment ?? {}).sort()))
      .catch(() => setEnvironments([]));
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchDraft);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchDraft]);

  // The same filter set drives the page, the facet counts and "select
  // all matching", so what the operator sees is what they select.
  // NB: the list filter is `vcenter_source_id`, not `source_vcenter_id`
  // (the VM column name) — FastAPI silently ignores unknown params and
  // would return the unfiltered estate.
  const filterParams = useCallback(() => {
    const qs = new URLSearchParams({ lifecycle_state: "available" });
    if (search) qs.set("search", search);
    if (vcenterId) qs.set("vcenter_source_id", vcenterId);
    if (environment) qs.set("environment", environment);
    return qs;
  }, [search, vcenterId, environment]);

  const loadVms = useCallback(async () => {
    setVmsLoading(true);
    try {
      const qs = filterParams();
      qs.set("skip", String((page - 1) * PAGE_SIZE));
      qs.set("limit", String(PAGE_SIZE));
      qs.set("sort_by", "name");
      qs.set("sort_order", "asc");
      const data = await fetchJSON(`/api/vms?${qs}`);
      setVms(asArray(data));
      setTotal(data?.total ?? 0);
    } catch {
      setVms([]);
      setTotal(0);
    } finally {
      setVmsLoading(false);
    }
  }, [page, filterParams]);

  useEffect(() => {
    loadVms();
  }, [loadVms]);

  const toggle = (vm, on) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (on) next.set(vm.id, vm);
      else next.delete(vm.id);
      return next;
    });
  };

  const allOnPageSelected = vms.length > 0 && vms.every((vm) => selected.has(vm.id));
  const toggleAllOnPage = (on) => {
    setSelected((prev) => {
      const next = new Map(prev);
      vms.forEach((vm) => {
        if (on) next.set(vm.id, vm);
        else next.delete(vm.id);
      });
      return next;
    });
  };

  // Every VM matching the current filters, not just the page. The ids
  // endpoint returns {id, name}, which is all the selection needs.
  const selectAllMatching = async () => {
    setSelectingAll(true);
    try {
      const data = await fetchJSON(`/api/vms/ids?${filterParams()}`);
      const rows = asArray(data);
      setSelected((prev) => {
        const next = new Map(prev);
        rows.forEach((vm) => {
          if (vm?.id != null) next.set(vm.id, { id: vm.id, name: vm.name ?? `VM #${vm.id}` });
        });
        return next;
      });
      toast.success(`Selected ${rows.length} VM${rows.length === 1 ? "" : "s"}`);
    } catch (e) {
      toast.error(e?.message ?? "Could not select all matching VMs");
    } finally {
      setSelectingAll(false);
    }
  };

  const clearSelection = () => setSelected(new Map());

  const toggleMapping = (id, on) =>
    setMappingIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  // Ask the backend what this selection will turn into: the exact plan
  // names after partitioning + batching, and the VMs it would skip.
  useEffect(() => {
    if (selected.size === 0) {
      setPreview(null);
      return undefined;
    }
    let cancelled = false;
    const t = setTimeout(async () => {
      setPreviewLoading(true);
      try {
        const data = await fetchJSON("/api/plans/preview", {
          method: "POST",
          body: {
            name: name.trim() || "plan",
            vm_ids: [...selected.keys()],
            mapping_ids: [...mappingIds],
            migration_type: migrationType,
          },
        });
        if (!cancelled) setPreview(data && typeof data === "object" ? data : null);
      } catch {
        // The preview is advisory; the create call is authoritative.
        if (!cancelled) setPreview(null);
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [selected, name, mappingIds, migrationType]);

  const previewPlans = asArray(preview?.plans);
  const previewExcluded = asArray(preview?.excluded);
  const overCeiling = selected.size > requestCeiling;
  const canGenerate = name.trim().length > 0 && selected.size > 0 && !generating && !overCeiling;

  const submit = async () => {
    if (!canGenerate) return;
    setGenerating(true);
    setError(null);
    setProgress({ status: "pending" });

    try {
      const plan = await fetchJSON("/api/plans", {
        method: "POST",
        body: {
          name: name.trim(),
          vm_ids: [...selected.keys()],
          // Always send the list, even empty, so the backend can tell
          // "operator opted out" from "field omitted".
          mapping_ids: [...mappingIds],
          migration_type: migrationType,
          // Leave out VMs that can't be planned as selected instead of
          // failing the whole selection; they're reported back.
          skip_ineligible: true,
        },
      });

      const excluded = asArray(plan?.excluded);
      setExcludedAtSubmit(excluded);

      // A selection spanning several (vCenter, cluster, namespace) partitions,
      // or larger than the batch size, fans out into several plans. A series
      // can take hours on a single-stream LLM backend, so hand off to the
      // plans list (which polls) rather than sit on a 10-minute spinner.
      const planIds = asArray(plan?.plans).map((p) => p?.id).filter((id) => id != null);
      if (planIds.length === 0 && plan?.id != null) planIds.push(plan.id);

      if (planIds.length > 1) {
        toast.success(
          `Generating ${planIds.length} plans` +
            (excluded.length > 0 ? ` (${excluded.length} VMs skipped)` : ""),
          { icon: "🤖" },
        );
        navigate("/plans");
        return;
      }

      toast("Generating plan…", { icon: "🤖" });
      const deadline = Date.now() + POLL_TIMEOUT_MS;

      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const statuses = await Promise.all(planIds.map((id) => fetchJSON(`/api/plans/${id}`)));
        const failed = statuses.find((st) => st?.status === "failed");
        const pending = statuses.filter((st) => st?.status !== "complete" && st?.status !== "failed");
        setProgress({
          ...(pending[0] ?? statuses[0]),
          doneCount: statuses.length - pending.length,
          planCount: statuses.length,
        });

        if (failed) {
          setError(`${failed?.name ?? "The plan"} failed: ${failed?.error_message || "plan generation failed."}`);
          setGenerating(false);
          return;
        }
        if (pending.length === 0) {
          toast.success("Migration plan ready");
          navigate(`/plans/${statuses[0]?.id}`);
          return;
        }
      }

      // Don't strand the operator on a spinner — the plan may still land.
      setError(
        "Generation is taking longer than 10 minutes. The plan may still complete; check the plans list.",
      );
      setGenerating(false);
    } catch (e) {
      setError(e?.message ?? "Could not start plan generation");
      setGenerating(false);
    }
  };

  const selectedList = useMemo(() => [...selected.values()], [selected]);

  const filtersActive = Boolean(search || vcenterId || environment);
  const clearFilters = () => {
    setSearchDraft("");
    setVcenterId("");
    setEnvironment("");
    setPage(1);
  };

  const vmStep = (
    <>
      {overCeiling && (
        <Alert
          variant="danger"
          isInline
          title={`Selection exceeds the per-request ceiling (${requestCeiling} VMs)`}
          className="pf-v6-u-mb-md"
        >
          Narrow the filters — by vCenter or environment — and generate in
          separate selections. Each one will still be batched into plans of
          up to {batchSize} VMs.
        </Alert>
      )}

      <Toolbar id="plan-vm-filters" clearAllFilters={clearFilters} className="pf-v6-u-mb-sm">
        <ToolbarContent>
          <ToolbarGroup variant="filter-group">
            <ToolbarItem>
              <FormSelect
                id="plan-vm-vcenter"
                aria-label="Filter by vCenter"
                value={vcenterId}
                onChange={(_e, v) => {
                  setVcenterId(v);
                  setPage(1);
                }}
              >
                <FormSelectOption value="" label="All vCenters" />
                {vcenters.map((v) => (
                  <FormSelectOption key={v?.id} value={String(v?.id)} label={v?.name ?? `vCenter #${v?.id}`} />
                ))}
              </FormSelect>
            </ToolbarItem>
            <ToolbarItem>
              <FormSelect
                id="plan-vm-environment"
                aria-label="Filter by environment"
                value={environment}
                onChange={(_e, v) => {
                  setEnvironment(v);
                  setPage(1);
                }}
              >
                <FormSelectOption value="" label="All environments" />
                {environments.map((env) => (
                  <FormSelectOption key={env} value={env} label={env} />
                ))}
              </FormSelect>
            </ToolbarItem>
          </ToolbarGroup>
          <ToolbarItem>
            <SearchInput
              aria-label="Search available VMs"
              placeholder="Search by name, IP, or owner"
              value={searchDraft}
              onChange={(_e, v) => setSearchDraft(v)}
              onClear={() => setSearchDraft("")}
            />
          </ToolbarItem>
        </ToolbarContent>
      </Toolbar>

      <Content component="p" className="pf-v6-u-color-200 pf-v6-u-mb-sm">
        {`${selected.size} selected. Only VMs not already committed to a plan are listed. `}
        {total > 0 && (
          <Button
            variant="link"
            isInline
            onClick={selectAllMatching}
            isLoading={selectingAll}
            isDisabled={selectingAll}
          >
            {filtersActive ? `Select all ${total} matching` : `Select all ${total}`}
          </Button>
        )}
        {selected.size > 0 && (
          <>
            {" · "}
            <Button variant="link" isInline onClick={clearSelection}>
              Clear selection
            </Button>
          </>
        )}
      </Content>

      {selected.size > 0 && (
        <Content component="p" className="pf-v6-u-mb-sm">
          {`Plans are generated in batches of up to ${batchSize} VMs. `}
          {previewLoading && !preview
            ? "Working out how this selection will be split…"
            : preview
              ? `This selection creates ${preview?.plan_count ?? previewPlans.length} plan${
                  (preview?.plan_count ?? previewPlans.length) === 1 ? "" : "s"
                }.`
              : null}
        </Content>
      )}

      {previewPlans.length > 1 && (
        <ExpandableSection
          toggleText={`Show the ${previewPlans.length} plans`}
          isIndented
          className="pf-v6-u-mb-md"
        >
          <List isPlain isBordered>
            {previewPlans.map((p, i) => (
              <ListItem key={p?.name ?? i}>
                <strong>{p?.name ?? `Plan ${i + 1}`}</strong>
                {` — ${p?.vm_count ?? 0} VMs`}
                {p?.source_vcenter_name ? ` · ${p.source_vcenter_name}` : ""}
                {p?.target_namespace ? ` → ${p.target_namespace}` : ""}
              </ListItem>
            ))}
          </List>
        </ExpandableSection>
      )}

      <ExcludedVMs
        excluded={previewExcluded}
        title={`${previewExcluded.length} selected VM${previewExcluded.length === 1 ? "" : "s"} will be skipped`}
      />

      <Pagination
        itemCount={total}
        page={page}
        perPage={PAGE_SIZE}
        onSetPage={(_e, p) => setPage(p)}
        isCompact
      />

      <Table aria-label="Available VMs" variant="compact">
        <Thead>
          <Tr>
            <Th
              select={{
                onSelect: (_e, on) => toggleAllOnPage(on),
                isSelected: allOnPageSelected,
              }}
              aria-label="Select all on page"
            />
            <Th width={35}>Name</Th>
            <Th width={20}>Status</Th>
            <Th width={20}>Environment</Th>
            <Th width={25}>IP address</Th>
          </Tr>
        </Thead>
        <Tbody>
          {vmsLoading && vms.length === 0 ? (
            Array.from({ length: 6 }).map((_, i) => (
              <Tr key={`sk-${i}`}>
                <Td colSpan={5}>
                  <Skeleton screenreaderText="Loading VMs" />
                </Td>
              </Tr>
            ))
          ) : vms.length === 0 ? (
            <Tr>
              <Td colSpan={5}>
                <Bullseye>
                  {filtersActive ? (
                    <NoResultsEmptyState onClearFilters={clearFilters} />
                  ) : (
                    <GuidedEmptyState
                      title="No VMs available to plan"
                      body="Every VM is either already committed to a plan or your inventory is empty. Import inventory, or free VMs by deleting a plan."
                      primary={{ label: "Go to inventory", to: "/inventory" }}
                    />
                  )}
                </Bullseye>
              </Td>
            </Tr>
          ) : (
            vms.map((vm) => {
              const isSelected = selected.has(vm.id);
              return (
                <Tr key={vm.id}>
                  <Td
                    select={{
                      rowIndex: vm.id,
                      isSelected,
                      onSelect: (_e, on) => toggle(vm, on),
                    }}
                  />
                  <Td dataLabel="Name">{vm.name}</Td>
                  <Td dataLabel="Status">
                    <StatusLabel kind="status" value={vm.status} />
                  </Td>
                  <Td dataLabel="Environment">{vm.environment ?? "—"}</Td>
                  <Td dataLabel="IP address">{vm.ip_address ?? "—"}</Td>
                </Tr>
              );
            })
          )}
        </Tbody>
      </Table>
    </>
  );

  if (generating || progress) {
    return (
      <PageFrame
        title="Generating migration plan"
        breadcrumbs={[
          { label: "Migrate" },
          { label: "Migration plans", to: "/plans" },
          { label: "New plan" },
        ]}
      >
        <PageSection>
          <Card>
            <CardBody>
              {error ? (
                <>
                  <Alert variant="danger" isInline title="Plan generation failed">
                    {error}
                  </Alert>
                  <Button
                    variant="primary"
                    className="pf-v6-u-mt-md"
                    onClick={() => {
                      setProgress(null);
                      setError(null);
                    }}
                  >
                    Back to the wizard
                  </Button>
                </>
              ) : (
                <>
                  <Progress
                    value={stagePercent(progress?.status)}
                    title={
                      (progress?.planCount ?? 1) > 1
                        ? `${stageLabel(progress?.status)} — ${progress?.doneCount ?? 0} of ${progress?.planCount} plans done`
                        : stageLabel(progress?.status)
                    }
                    aria-label="Plan generation progress"
                  />
                  <Content component="small" className="pf-v6-u-color-200">
                    {`${selected.size - excludedAtSubmit.length} VM(s). Stages 0–5 and 7 are deterministic and fast; the LLM annotation step dominates the wall clock.`}
                  </Content>
                  <div className="pf-v6-u-mt-md">
                    <ExcludedVMs
                      excluded={excludedAtSubmit}
                      title={`${excludedAtSubmit.length} selected VM${excludedAtSubmit.length === 1 ? " was" : "s were"} not included`}
                    />
                  </div>
                </>
              )}
            </CardBody>
          </Card>
        </PageSection>
      </PageFrame>
    );
  }

  return (
    <PageFrame
      title="Generate a migration plan"
      description="A plan groups VMs into dependency-ordered waves and emits the MTV YAML to run them."
      breadcrumbs={[
        { label: "Migrate" },
        { label: "Migration plans", to: "/plans" },
        { label: "New plan" },
      ]}
    >
      <PageSection hasBodyWrapper={false}>
        <Wizard
          height={600}
          onClose={() => navigate("/plans")}
          onSave={submit}
          title="Generate a migration plan"
        >
          <WizardStep name="Name" id="step-name" footer={{ isNextDisabled: !name.trim() }}>
            <Form>
              <FormGroup label="Plan name" isRequired fieldId="plan-name">
                <TextInput
                  id="plan-name"
                  value={name}
                  onChange={(_e, v) => setName(v)}
                  isRequired
                  placeholder="Payments estate — wave 1"
                />
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>
                      Used on the plan list and in the generated MTV resource
                      names. Something you&apos;ll recognize in three weeks.
                      A selection larger than {batchSize} VMs becomes a
                      numbered series: name-1, name-2, …
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
              <FormGroup label="Migration type" role="radiogroup" fieldId="migration-type" isStack>
                <Radio
                  id="migration-type-cold"
                  name="migration-type"
                  label="Cold — VMs are shut down for the copy"
                  description="No source-side prerequisites. Downtime lasts for the whole disk transfer."
                  isChecked={migrationType === "cold"}
                  onChange={() => setMigrationType("cold")}
                />
                <Radio
                  id="migration-type-warm"
                  name="migration-type"
                  label="Warm — disks pre-copy while VMs run; short cutover"
                  description="Every VM needs Changed Block Tracking enabled on the VM and each disk, and VMware Tools running. Windows also needs the VSS and VMware Snapshot Provider services. A VM that lacks these fails at pre-copy."
                  isChecked={migrationType === "warm"}
                  onChange={() => setMigrationType("warm")}
                />
              </FormGroup>
            </Form>
          </WizardStep>

          <WizardStep name="Resource mappings" id="step-mappings">
            <Content component="p" className="pf-v6-u-mb-md">
              Mappings translate vSphere networks and datastores into
              OpenShift NADs and StorageClasses. Every selected VM must
              resolve through one, or generation fails at stage 0 with a
              per-VM gap report.
            </Content>

            {mappingsLoading ? (
              <Skeleton width="100%" height="80px" />
            ) : mappings.length === 0 ? (
              <GuidedEmptyState
                title="No resource mappings defined"
                body="Without a mapping, plan generation cannot resolve a target for any VM."
                primary={{ label: "Define a mapping", to: "/mappings" }}
              />
            ) : (
              <Form>
                {mappings.map((m) => (
                  <Checkbox
                    key={m.id}
                    id={`mapping-${m.id}`}
                    label={m.name || `Mapping #${m.id}`}
                    description={
                      m.status ? `Status: ${m.status}` : "No status reported"
                    }
                    isChecked={mappingIds.has(m.id)}
                    onChange={(_e, on) => toggleMapping(m.id, on)}
                  />
                ))}
              </Form>
            )}
          </WizardStep>

          <WizardStep
            name="Select VMs"
            id="step-vms"
            footer={{
              nextButtonText: previewPlans.length > 1 ? `Generate ${previewPlans.length} plans` : "Generate plan",
              isNextDisabled: !canGenerate,
            }}
          >
            {vmStep}
          </WizardStep>
        </Wizard>

        {selectedList.length > 0 && (
          <Content component="small" className="pf-v6-u-color-200">
            {`Selected: ${selectedList.slice(0, 5).map((v) => v.name).join(", ")}${
              selectedList.length > 5 ? ` and ${selectedList.length - 5} more` : ""
            }`}
          </Content>
        )}
      </PageSection>
    </PageFrame>
  );
}
