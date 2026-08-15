// Generate a migration plan.
//
// Three steps on PF's Wizard: name it, pick the mappings that cover the
// estate, pick the VMs. Generation is async, so the last step polls the
// plan's pipeline status and reports which stage it's on — the stages are
// named in plan_pipeline._PIPELINE_STAGE_TO_STATUS and the LLM annotation
// step is slow enough that a bare spinner reads as stuck.
//
// The selection cap (settings.max_vms_per_plan, default 250) is enforced
// in the UI as well as the API, so the operator finds out before waiting
// on a 422.

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
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  PageSection,
  Pagination,
  Progress,
  SearchInput,
  Skeleton,
  TextInput,
  Wizard,
  WizardStep,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { GuidedEmptyState, NoResultsEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

const MAX_VMS = 250;
const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;
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

export default function PlanWizardPage() {
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [mappings, setMappings] = useState([]);
  const [mappingIds, setMappingIds] = useState(() => new Set());
  const [mappingsLoading, setMappingsLoading] = useState(true);

  const [vms, setVms] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [vmsLoading, setVmsLoading] = useState(true);

  const [selected, setSelected] = useState(() => new Map());
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchJSON("/api/mappings")
      .then((rows) => setMappings(Array.isArray(rows) ? rows : []))
      .catch(() => setMappings([]))
      .finally(() => setMappingsLoading(false));
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchDraft);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchDraft]);

  const loadVms = useCallback(async () => {
    setVmsLoading(true);
    try {
      const qs = new URLSearchParams({
        skip: String((page - 1) * PAGE_SIZE),
        limit: String(PAGE_SIZE),
        sort_by: "name",
        sort_order: "asc",
        // Only VMs not already committed to another plan are selectable.
        lifecycle_state: "available",
      });
      if (search) qs.set("search", search);
      const data = await fetchJSON(`/api/vms?${qs}`);
      setVms(asArray(data));
      setTotal(data?.total ?? 0);
    } catch {
      setVms([]);
      setTotal(0);
    } finally {
      setVmsLoading(false);
    }
  }, [page, search]);

  useEffect(() => {
    loadVms();
  }, [loadVms]);

  const toggle = (vm, on) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (on) {
        if (next.size >= MAX_VMS && !next.has(vm.id)) return prev;
        next.set(vm.id, vm);
      } else {
        next.delete(vm.id);
      }
      return next;
    });
  };

  const toggleMapping = (id, on) =>
    setMappingIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const atCap = selected.size >= MAX_VMS;
  const canGenerate = name.trim().length > 0 && selected.size > 0 && !generating;

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
        },
      });

      toast("Generating plan…", { icon: "🤖" });
      const deadline = Date.now() + POLL_TIMEOUT_MS;

      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const status = await fetchJSON(`/api/plans/${plan.id}`);
        setProgress(status);

        if (status?.status === "complete") {
          toast.success("Migration plan ready");
          navigate(`/plans/${status.id}`);
          return;
        }
        if (status?.status === "failed") {
          setError(status?.error_message || "Plan generation failed.");
          setGenerating(false);
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

  const vmStep = (
    <>
      {atCap && (
        <Alert
          variant="warning"
          isInline
          title={`Selection cap reached (${MAX_VMS} VMs)`}
          className="pf-v6-u-mb-md"
        >
          Narrow the filters or split this into multiple plans. One huge plan
          is harder to review and to sequence than several scoped ones.
        </Alert>
      )}

      <SearchInput
        aria-label="Search available VMs"
        placeholder="Search by name, IP, or owner"
        value={searchDraft}
        onChange={(_e, v) => setSearchDraft(v)}
        onClear={() => setSearchDraft("")}
        className="pf-v6-u-mb-md"
      />

      <Content component="p" className="pf-v6-u-color-200 pf-v6-u-mb-sm">
        {`${selected.size} selected of a ${MAX_VMS} maximum. Only VMs not already committed to a plan are listed.`}
      </Content>

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
            <Th screenReaderText="Select" />
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
                  {search ? (
                    <NoResultsEmptyState onClearFilters={() => setSearchDraft("")} />
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
                      // Block new selections at the cap, but always allow
                      // deselecting so the operator isn't stuck.
                      isDisabled: atCap && !isSelected,
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
                    title={stageLabel(progress?.status)}
                    aria-label="Plan generation progress"
                  />
                  <Content component="small" className="pf-v6-u-color-200">
                    {`${selected.size} VM(s). Stages 0–5 and 7 are deterministic and fast; the LLM annotation step dominates the wall clock.`}
                  </Content>
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
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
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
            footer={{ nextButtonText: "Generate plan", isNextDisabled: !canGenerate }}
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
