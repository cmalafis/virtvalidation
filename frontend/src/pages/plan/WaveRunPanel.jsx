// Per-wave execution: dry-run preview, baseline, validate.
//
// This is where the appliance actually SSHes into other people's
// production machines, so the safety posture is surfaced rather than
// buried:
//
//  * The dry run is offered FIRST and lists the exact hosts and the exact
//    read-only commands that would run, without connecting.
//  * The global SSH kill-switch, when off, disables the run buttons here
//    instead of letting the operator discover it as a 503.
//  * Waves that need authorization (production VMs, or a CUI+ source)
//    collect the authorizer and reason up front — the API returns 422
//    without them, and a form is a better answer than an error.

import { useCallback, useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  Content,
  ExpandableSection,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Progress,
  Skeleton,
  TextArea,
  TextInput,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import StatusLabel from "../../common/StatusLabel";
import { fetchJSON } from "../../utils/fetchJSON";

const POLL_MS = 3000;
const TERMINAL = new Set(["completed", "failed", "partial"]);

// The two run shapes count differently: a baseline run reports
// captured_vms, a validation run splits into passed/warned/failed/
// unreachable. Normalize to succeeded/failed for the progress line.
function runCounts(run) {
  const total = run?.total_vms ?? 0;
  const failed = (run?.failed_vms ?? 0) + (run?.unreachable_vms ?? 0);
  const succeeded =
    run?.captured_vms ?? (run?.passed_vms ?? 0) + (run?.warned_vms ?? 0);
  return { total, failed, succeeded };
}

function RunProgress({ run, label }) {
  if (!run) return null;
  const { total, failed, succeeded } = runCounts(run);
  const pct = total === 0 ? 0 : Math.round(((succeeded + failed) / total) * 100);

  return (
    <div className="pf-v6-u-mt-md">
      <Progress
        value={pct}
        title={`${label} — ${run.status}`}
        variant={failed > 0 ? "warning" : undefined}
        aria-label={`${label} progress`}
      />
      <Content component="small" className="pf-v6-u-color-200">
        {run.progress_message ||
          `${succeeded} succeeded, ${failed} failed of ${total}`}
      </Content>
    </div>
  );
}

function RunModal({ isOpen, kind, preview, sshKeys, onClose, onStart }) {
  const [keyId, setKeyId] = useState("");
  const [authorizedBy, setAuthorizedBy] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setBusy(false);
    setKeyId(sshKeys?.[0]?.id ? String(sshKeys[0].id) : "");
  }, [isOpen, sshKeys]);

  const needsAuth = Boolean(preview?.requires_authorization);
  const canStart =
    keyId && (!needsAuth || (authorizedBy.trim() && reason.trim())) && !busy;

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      await onStart({
        ssh_key_id: Number(keyId),
        ...(needsAuth
          ? { authorized_by: authorizedBy.trim(), authorization_reason: reason.trim() }
          : {}),
      });
      onClose();
    } catch (e) {
      setError(e?.message ?? "Could not start the run");
      setBusy(false);
    }
  };

  const label = kind === "baseline" ? "baseline capture" : "validation";

  return (
    <Modal isOpen={isOpen} onClose={busy ? undefined : onClose} variant="medium">
      <ModalHeader
        title={`Start ${label}`}
        description={`${preview?.vm_count ?? 0} host(s) will be contacted over SSH with a fixed set of read-only commands.`}
      />
      <ModalBody>
        {needsAuth && (
          <Alert
            variant="warning"
            isInline
            title="This wave requires named authorization"
            className="pf-v6-u-mb-md"
          >
            {preview?.authorization_reason ||
              "This wave targets production or classified systems."}{" "}
            Both fields below are recorded on the run and cannot be changed
            afterwards.
          </Alert>
        )}

        {error && (
          <Alert variant="danger" isInline title="Could not start" className="pf-v6-u-mb-md">
            {error}
          </Alert>
        )}

        <Form>
          <FormGroup label="SSH key" isRequired fieldId="run-key">
            <FormSelect
              id="run-key"
              value={keyId}
              onChange={(_e, v) => setKeyId(v)}
              aria-label="SSH key"
            >
              {(sshKeys ?? []).length === 0 && (
                <FormSelectOption value="" label="No active SSH keys — create one in Settings" />
              )}
              {(sshKeys ?? []).map((k) => (
                <FormSelectOption
                  key={k.id}
                  value={String(k.id)}
                  label={`${k.name ?? `Key #${k.id}`} (${k.algorithm ?? "ed25519"})`}
                />
              ))}
            </FormSelect>
          </FormGroup>

          {needsAuth && (
            <>
              <FormGroup label="Authorized by" isRequired fieldId="run-auth">
                <TextInput
                  id="run-auth"
                  value={authorizedBy}
                  onChange={(_e, v) => setAuthorizedBy(v)}
                  placeholder="Name of the person authorizing this run"
                  isRequired
                />
              </FormGroup>
              <FormGroup label="Authorization reason" isRequired fieldId="run-reason">
                <TextArea
                  id="run-reason"
                  value={reason}
                  onChange={(_e, v) => setReason(v)}
                  rows={3}
                  placeholder="Change ticket, maintenance window, or approval reference"
                  isRequired
                />
              </FormGroup>
            </>
          )}
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={start} isDisabled={!canStart} isLoading={busy}>
          {`Start ${label}`}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}

export default function WaveRunPanel({ planId, waveNumber }) {
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [sshKeys, setSshKeys] = useState([]);
  const [modalKind, setModalKind] = useState(null);
  const [runs, setRuns] = useState({ baseline: null, validation: null });
  const [loading, setLoading] = useState(true);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      fetchJSON(`/api/plans/${planId}/waves/${waveNumber}/preview`),
      fetchJSON("/api/ssh-keys?status=active&limit=200"),
    ]).then(([p, k]) => {
      if (cancelled) return;
      if (p.status === "fulfilled") setPreview(p.value);
      else setPreviewError(p.reason);
      if (k.status === "fulfilled") {
        const val = k.value;
        setSshKeys(Array.isArray(val) ? val : (val?.items ?? []));
      }
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [planId, waveNumber]);

  const pollRun = useCallback(async (kind, runId) => {
    const endpoint = kind === "baseline" ? "baseline-runs" : "validation-runs";
    while (alive.current) {
      let data;
      try {
        data = await fetchJSON(`/api/${endpoint}/${runId}`);
      } catch {
        return; // transient; the operator can refresh
      }
      if (!alive.current) return;
      setRuns((r) => ({ ...r, [kind]: data }));
      if (TERMINAL.has(data?.status)) {
        const { failed } = runCounts(data);
        if (failed > 0) toast.error(`${kind} finished with ${failed} failure(s)`);
        else toast.success(`${kind} complete`);
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }, []);

  const startRun = async (kind, body) => {
    const path = kind === "baseline" ? "baseline" : "validate";
    const accepted = await fetchJSON(`/api/plans/${planId}/waves/${waveNumber}/${path}`, {
      method: "POST",
      body,
    });
    // The two accept-bodies name their id differently:
    // BaselineRunAccepted.baseline_run_id / ValidationRunAccepted.validation_run_id.
    const runId =
      kind === "baseline" ? accepted?.baseline_run_id : accepted?.validation_run_id;
    if (!runId) throw new Error("The run started but returned no run id");
    toast(`${kind} run started`, { icon: "⏳" });
    setRuns((r) => ({ ...r, [kind]: { id: runId, status: "running", total_vms: preview?.vm_count ?? 0 } }));
    pollRun(kind, runId);
  };

  if (loading) return <Skeleton width="100%" height="80px" />;

  if (previewError) {
    return (
      <Alert variant="warning" isInline title="Could not load the wave preview">
        {previewError.message}
      </Alert>
    );
  }

  const killSwitchOff = preview?.ssh_operations_enabled === false;
  const vms = preview?.vms ?? [];
  // Commands are identical across hosts in a wave; show the set once
  // rather than repeating it per row.
  const commands = vms[0]?.commands ?? [];

  return (
    <Card isCompact isPlain>
      <CardBody>
        {killSwitchOff && (
          <Alert
            variant="warning"
            isInline
            title="SSH operations are disabled"
            className="pf-v6-u-mb-md"
          >
            The global kill-switch is off, so baseline and validation runs
            would be refused. Re-enable it in Settings.
          </Alert>
        )}

        {preview?.requires_authorization && (
          <Alert
            variant="info"
            isInline
            title="This wave requires named authorization"
            className="pf-v6-u-mb-md"
          >
            {preview.authorization_reason}
          </Alert>
        )}

        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={() => setModalKind("baseline")}
              isDisabled={killSwitchOff || vms.length === 0}
            >
              Capture baseline
            </Button>
          </FlexItem>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={() => setModalKind("validation")}
              isDisabled={killSwitchOff || vms.length === 0}
            >
              Validate wave
            </Button>
          </FlexItem>
        </Flex>

        <RunProgress run={runs.baseline} label="Baseline capture" />
        <RunProgress run={runs.validation} label="Validation" />

        <ExpandableSection
          toggleText={`Dry run — ${vms.length} host(s), ${commands.length} read-only command(s)`}
          className="pf-v6-u-mt-md"
        >
          <Content component="p" className="pf-v6-u-color-200">
            This is exactly what a run would do. Nothing below has been
            contacted.
          </Content>

          {commands.length > 0 && (
            <>
              <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-mt-md">
                Commands per host
              </Content>
              <pre
                className="pf-v6-u-font-size-sm"
                style={{
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  padding: "var(--pf-t--global--spacer--sm)",
                  background: "var(--pf-t--global--background--color--secondary--default)",
                  borderRadius: "var(--pf-t--global--border--radius--small)",
                }}
              >
                {commands.join("\n")}
              </pre>
            </>
          )}

          <Table aria-label="Wave hosts" variant="compact" className="pf-v6-u-mt-md">
            <Thead>
              <Tr>
                <Th width={30}>VM</Th>
                <Th width={30}>Host</Th>
                <Th width={20}>SSH user</Th>
                <Th width={20}>Environment</Th>
              </Tr>
            </Thead>
            <Tbody>
              {vms.map((vm) => (
                <Tr key={vm.vm_id}>
                  <Td dataLabel="VM">{vm.name}</Td>
                  <Td dataLabel="Host">{vm.host}</Td>
                  <Td dataLabel="SSH user">{vm.username}</Td>
                  <Td dataLabel="Environment">
                    {vm.environment ? (
                      <StatusLabel kind="record" value="draft">
                        {vm.environment}
                      </StatusLabel>
                    ) : (
                      "—"
                    )}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </ExpandableSection>
      </CardBody>

      <RunModal
        isOpen={Boolean(modalKind)}
        kind={modalKind}
        preview={preview}
        sshKeys={sshKeys}
        onClose={() => setModalKind(null)}
        onStart={(body) => startRun(modalKind, body)}
      />
    </Card>
  );
}
