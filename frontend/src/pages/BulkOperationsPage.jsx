// Bulk operations — capture baselines or validate across a selection.
//
// Selection is by scope rather than by picking rows: the axes stack, and
// the backend resolves them. That's what makes this usable at 1,000 VMs
// where a checkbox list is not.
//
// Validation offers a tier preview first, because a bulk validation is
// the most expensive thing this product does and the operator should see
// the cost before paying it.

import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Checkbox,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  PageSection,
  Progress,
  Tab,
  TabTitleText,
  Tabs,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import { fetchJSON } from "../utils/fetchJSON";

const POLL_MS = 3000;

function ScopeForm({ scope, setScope, vcenters, facets, showValidationAxes }) {
  const set = (key) => (_e, value) =>
    setScope((s) => ({ ...s, [key]: value === "" ? null : value }));

  return (
    <Form>
      <FormGroup label="vCenter source" fieldId="scope-vc">
        <FormSelect
          id="scope-vc"
          value={scope.source_vcenter_id ?? ""}
          onChange={(_e, v) =>
            setScope((s) => ({ ...s, source_vcenter_id: v === "" ? null : Number(v) }))
          }
        >
          <FormSelectOption value="" label="All vCenters" />
          {(vcenters ?? []).map((v) => (
            <FormSelectOption key={v.id} value={String(v.id)} label={v.name} />
          ))}
        </FormSelect>
      </FormGroup>

      <FormGroup label="Environment" fieldId="scope-env">
        <FormSelect
          id="scope-env"
          value={scope.environment ?? ""}
          onChange={set("environment")}
        >
          <FormSelectOption value="" label="All environments" />
          {Object.keys(facets?.environment ?? {}).map((e) => (
            <FormSelectOption key={e} value={e} label={`${e} (${facets.environment[e]})`} />
          ))}
        </FormSelect>
      </FormGroup>

      <FormGroup label="Application" fieldId="scope-app">
        <FormSelect
          id="scope-app"
          value={scope.application_hint ?? ""}
          onChange={set("application_hint")}
        >
          <FormSelectOption value="" label="All applications" />
          {Object.keys(facets?.application_hint ?? {}).map((a) => (
            <FormSelectOption
              key={a}
              value={a}
              label={`${a} (${facets.application_hint[a]})`}
            />
          ))}
        </FormSelect>
      </FormGroup>

      {showValidationAxes && (
        <>
          <FormGroup fieldId="scope-migrated">
            <Checkbox
              id="scope-migrated"
              label="Only VMs already observed on OpenShift"
              description="Validating a VM that hasn't migrated compares it against itself."
              isChecked={Boolean(scope.only_migrated)}
              onChange={(_e, v) => setScope((s) => ({ ...s, only_migrated: v }))}
            />
          </FormGroup>
          <FormGroup fieldId="scope-cache">
            <Checkbox
              id="scope-cache"
              label="Reuse cached LLM verdicts where the diff is unchanged"
              description="Turn off to force a fresh reasoning pass on every VM."
              isChecked={scope.use_cache !== false}
              onChange={(_e, v) => setScope((s) => ({ ...s, use_cache: v }))}
            />
          </FormGroup>
        </>
      )}
    </Form>
  );
}

function OperationTab({ kind, vcenters, facets }) {
  const isValidation = kind === "validation";
  const [scope, setScope] = useState({
    source_vcenter_id: null,
    environment: null,
    application_hint: null,
    only_migrated: isValidation,
    use_cache: true,
  });
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [task, setTask] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  const body = () => {
    const out = { ...scope };
    for (const k of Object.keys(out)) if (out[k] === null) delete out[k];
    return out;
  };

  const runPreview = async () => {
    setPreviewing(true);
    setError(null);
    try {
      setPreview(await fetchJSON("/api/validations/preview-tiers", { method: "POST", body: body() }));
    } catch (e) {
      setError(e?.message ?? "Preview failed");
      setPreview(null);
    } finally {
      setPreviewing(false);
    }
  };

  const poll = useCallback(async (taskId) => {
    const url = isValidation
      ? `/api/validations/run-bulk/${taskId}`
      : `/api/snapshots/capture-bulk/${taskId}`;
    for (;;) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      let status;
      try {
        status = await fetchJSON(url);
      } catch {
        return;
      }
      setTask(status);
      if (["completed", "failed"].includes(status?.status)) {
        setRunning(false);
        if (status.status === "completed") toast.success(`Bulk ${kind} complete`);
        else toast.error(`Bulk ${kind} failed`);
        return;
      }
    }
  }, [isValidation, kind]);

  const start = async () => {
    setRunning(true);
    setError(null);
    setTask(null);
    try {
      const endpoint = isValidation
        ? "/api/validations/run-bulk"
        : "/api/snapshots/capture-bulk";
      const spawned = await fetchJSON(endpoint, { method: "POST", body: body() });
      const taskId = spawned?.task_id;
      if (!taskId) throw new Error("The run started but returned no task id");
      toast(`Bulk ${kind} started`, { icon: "⏳" });
      setTask({ status: "running" });
      poll(taskId);
    } catch (e) {
      setError(e?.message ?? "Could not start");
      setRunning(false);
    }
  };

  const done = (task?.completed ?? 0) + (task?.failed ?? 0);
  const total = task?.total ?? 0;

  return (
    <>
      <Card className="pf-v6-u-mb-md">
        <CardTitle>Scope</CardTitle>
        <CardBody>
          <Content component="p" className="pf-v6-u-color-200 pf-v6-u-mb-md">
            Axes stack. Leave everything on &quot;all&quot; to target the whole
            fleet.
          </Content>
          <ScopeForm
            scope={scope}
            setScope={setScope}
            vcenters={vcenters}
            facets={facets}
            showValidationAxes={isValidation}
          />
        </CardBody>
      </Card>

      {error && (
        <Alert variant="danger" isInline title="Something went wrong" className="pf-v6-u-mb-md">
          {error}
        </Alert>
      )}

      <Flex spaceItems={{ default: "spaceItemsSm" }} className="pf-v6-u-mb-md">
        {isValidation && (
          <FlexItem>
            <Button
              variant="secondary"
              onClick={runPreview}
              isLoading={previewing}
              isDisabled={previewing || running}
            >
              Preview cost
            </Button>
          </FlexItem>
        )}
        <FlexItem>
          <Button variant="primary" onClick={start} isDisabled={running} isLoading={running}>
            {isValidation ? "Run validation" : "Capture baselines"}
          </Button>
        </FlexItem>
      </Flex>

      {preview && (
        <Card className="pf-v6-u-mb-md">
          <CardTitle>Estimated work</CardTitle>
          <CardBody>
            <DescriptionList isCompact isHorizontal>
              {Object.entries(preview)
                .filter(([, v]) => typeof v !== "object")
                .map(([k, v]) => (
                  <DescriptionListGroup key={k}>
                    <DescriptionListTerm>{k.replace(/_/g, " ")}</DescriptionListTerm>
                    <DescriptionListDescription>{String(v)}</DescriptionListDescription>
                  </DescriptionListGroup>
                ))}
            </DescriptionList>
          </CardBody>
        </Card>
      )}

      {task && (
        <Card>
          <CardTitle>Progress</CardTitle>
          <CardBody>
            <Progress
              value={total === 0 ? 0 : Math.round((done / total) * 100)}
              title={task?.status ?? "running"}
              aria-label={`Bulk ${kind} progress`}
            />
            <Content component="small" className="pf-v6-u-color-200">
              {total > 0
                ? `${task?.completed ?? 0} succeeded, ${task?.failed ?? 0} failed of ${total}`
                : "Starting…"}
            </Content>
          </CardBody>
        </Card>
      )}
    </>
  );
}

export default function BulkOperationsPage() {
  const [vcenters, setVcenters] = useState([]);
  const [facets, setFacets] = useState({});
  const [tab, setTab] = useState("baseline");

  useEffect(() => {
    Promise.allSettled([
      fetchJSON("/api/sources/vcenters"),
      fetchJSON("/api/vms/facets"),
    ]).then(([v, f]) => {
      if (v.status === "fulfilled" && Array.isArray(v.value)) setVcenters(v.value);
      if (f.status === "fulfilled") setFacets(f.value ?? {});
    });
  }, []);

  return (
    <PageFrame
      title="Bulk operations"
      description="Capture baselines or run validation across a scoped selection, rather than one VM at a time."
      breadcrumbs={[{ label: "Migrate" }, { label: "Bulk operations" }]}
    >
      <PageSection hasBodyWrapper={false}>
        <Alert
          variant="info"
          isInline
          title="These operations SSH into managed hosts"
          className="pf-v6-u-mb-md"
        >
          Every command is read-only and recorded in agent activity. If the
          SSH kill-switch is off in Settings, these requests are refused.
        </Alert>

        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Bulk operations">
          <Tab eventKey="baseline" title={<TabTitleText>Capture baselines</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              {tab === "baseline" && (
                <OperationTab kind="capture" vcenters={vcenters} facets={facets} />
              )}
            </div>
          </Tab>
          <Tab eventKey="validate" title={<TabTitleText>Validate</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              {tab === "validate" && (
                <OperationTab kind="validation" vcenters={vcenters} facets={facets} />
              )}
            </div>
          </Tab>
        </Tabs>
      </PageSection>
    </PageFrame>
  );
}
