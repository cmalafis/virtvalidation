// Add available inventory VMs to an existing plan.
//
// The counterpart to "Remove from plan": a VM that was taken out (or was
// never planned) can be brought into a wave without generating a new
// plan. Only VMs that migrate from the plan's vCenter to its cluster and
// namespace can join — a wave is one MTV Plan — so rows that would not
// fit are listed but not selectable, with the reason. The backend dry
// run remains the authority: it also applies the mapping-coverage and
// migratability checks plan generation uses.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Content,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Pagination,
  SearchInput,
  Skeleton,
  TextInput,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import { asArray } from "../../utils/asArray";
import { fetchJSON } from "../../utils/fetchJSON";
import EditViolations from "./EditViolations";
import { partitionLabel } from "./planRows";
import { commitEdit, hasSeverity, useEditPreview, violationsFromError } from "./usePlanEdit";

const PAGE_SIZE = 10;
const SEARCH_DEBOUNCE_MS = 300;
const NEW_WAVE = "new";

/** Why this VM cannot join the plan, or null if it can. */
function ineligibleReason(vm, partition) {
  if (partition?.target_namespace) {
    if (!vm?.resolved_target_namespace || vm?.resolved_target_cluster_id == null) {
      return "No target cluster or namespace resolved";
    }
    if (
      vm.resolved_target_cluster_id !== partition.target_cluster_id ||
      vm.resolved_target_namespace !== partition.target_namespace
    ) {
      return `Migrates to ${vm?.resolved_target_cluster_name ?? "another cluster"} / ${vm.resolved_target_namespace}`;
    }
  }
  if (vm?.assessment_status === "blocked") return "Cannot migrate as-is (assessment)";
  return null;
}

export default function AddVMsModal({ isOpen, plan, partition, waves, onClose, onApplied }) {
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [vms, setVms] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [selected, setSelected] = useState(() => new Map());
  const [targetWave, setTargetWave] = useState("");
  const [note, setNote] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [commitError, setCommitError] = useState(null);

  const planId = plan?.id;
  const vcenterId = partition?.source_vcenter_id ?? null;

  useEffect(() => {
    if (!isOpen) return;
    setSearchDraft("");
    setSearch("");
    setPage(1);
    setSelected(new Map());
    setTargetWave("");
    setNote("");
    setAcknowledged(false);
    setBusy(false);
    setCommitError(null);
  }, [isOpen]);

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchDraft);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [searchDraft]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // NB: the list filter is `vcenter_source_id`, not the VM column
      // name — FastAPI silently ignores unknown params.
      const qs = new URLSearchParams({
        lifecycle_state: "available",
        sort_by: "name",
        sort_order: "asc",
        skip: String((page - 1) * PAGE_SIZE),
        limit: String(PAGE_SIZE),
      });
      if (search) qs.set("search", search);
      if (vcenterId != null) qs.set("vcenter_source_id", String(vcenterId));
      const data = await fetchJSON(`/api/vms?${qs}`);
      setVms(asArray(data));
      setTotal(data?.total ?? 0);
      setLoadError(null);
    } catch (e) {
      setVms([]);
      setTotal(0);
      setLoadError(e);
    } finally {
      setLoading(false);
    }
  }, [page, search, vcenterId]);

  useEffect(() => {
    if (isOpen) load();
  }, [isOpen, load]);

  const toggle = (vm, on) =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (on) next.set(vm.id, vm);
      else next.delete(vm.id);
      return next;
    });

  const vmIds = useMemo(() => [...selected.keys()], [selected]);
  const body = useMemo(() => {
    if (!isOpen || vmIds.length === 0 || !targetWave) return null;
    return {
      operation: "add",
      vm_ids: vmIds,
      target_wave: targetWave === NEW_WAVE ? NEW_WAVE : Number(targetWave),
    };
  }, [isOpen, vmIds, targetWave]);

  const preview = useEditPreview(planId, body);
  const result = preview.result;
  const isBlocked = hasSeverity(result, "blocking");
  const needsAck = hasSeverity(result, "overridable");

  useEffect(() => {
    setAcknowledged(false);
    setCommitError(null);
  }, [result]);

  const canConfirm =
    Boolean(body) &&
    Boolean(result) &&
    !preview.loading &&
    !preview.error &&
    !isBlocked &&
    (!needsAck || acknowledged) &&
    !busy;

  const confirm = async () => {
    setBusy(true);
    setCommitError(null);
    try {
      const response = await commitEdit(planId, {
        ...body,
        confirm_overrides: needsAck && acknowledged,
        note: note.trim() || null,
      });
      onApplied?.(response, {});
    } catch (e) {
      setCommitError(e);
      setBusy(false);
    }
  };

  const target = partitionLabel(partition);
  const count = selected.size;

  return (
    <Modal
      isOpen={isOpen}
      onClose={busy ? undefined : onClose}
      variant="large"
      aria-label="Add VMs to this plan"
    >
      <ModalHeader
        title="Add VMs to this plan"
        description={
          target
            ? `Available VMs that migrate ${target} can join ${plan?.name ?? "this plan"}.`
            : `Available VMs can join ${plan?.name ?? "this plan"}.`
        }
      />
      <ModalBody>
        <SearchInput
          aria-label="Search available VMs"
          placeholder="Search by name, IP, or owner"
          value={searchDraft}
          onChange={(_e, v) => setSearchDraft(v)}
          onClear={() => setSearchDraft("")}
        />

        {loadError ? (
          <Alert
            variant="danger"
            isInline
            title="Could not load the available VMs"
            className="pf-v6-u-mt-md"
          >
            {loadError?.message}
          </Alert>
        ) : (
          <>
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
                  <Th aria-label="Select" />
                  <Th width={35}>Name</Th>
                  <Th width={20}>Environment</Th>
                  <Th width={20}>IP address</Th>
                  <Th width={25}>Can join?</Th>
                </Tr>
              </Thead>
              <Tbody>
                {loading && vms.length === 0 ? (
                  <Tr>
                    <Td colSpan={5}>
                      <Skeleton screenreaderText="Loading VMs" />
                    </Td>
                  </Tr>
                ) : vms.length === 0 ? (
                  <Tr>
                    <Td colSpan={5}>
                      <Content component="p" className="pf-v6-u-color-200">
                        {search
                          ? "No available VM matches the search."
                          : "No VMs are available. VMs already in a plan must be removed from it first."}
                      </Content>
                    </Td>
                  </Tr>
                ) : (
                  vms.map((vm) => {
                    const reason = ineligibleReason(vm, partition);
                    return (
                      <Tr key={vm.id}>
                        <Td
                          select={{
                            rowIndex: vm.id,
                            isSelected: selected.has(vm.id),
                            isDisabled: Boolean(reason),
                            onSelect: (_e, on) => toggle(vm, on),
                          }}
                        />
                        <Td dataLabel="Name">{vm?.name ?? `VM #${vm.id}`}</Td>
                        <Td dataLabel="Environment">{vm?.environment ?? "—"}</Td>
                        <Td dataLabel="IP address">{vm?.ip_address ?? "—"}</Td>
                        <Td dataLabel="Can join?">{reason ?? "Yes"}</Td>
                      </Tr>
                    );
                  })
                )}
              </Tbody>
            </Table>
          </>
        )}

        <Form className="pf-v6-u-mt-md" onSubmit={(e) => e.preventDefault()}>
          <FormGroup label="Add to wave" fieldId="add-target-wave" isRequired>
            <FormSelect
              id="add-target-wave"
              aria-label="Add to wave"
              value={targetWave}
              onChange={(_e, v) => setTargetWave(v)}
            >
              <FormSelectOption value="" label="Choose a wave" isPlaceholder />
              {asArray(waves).map((w) => (
                <FormSelectOption
                  key={w?.wave_number}
                  value={String(w?.wave_number)}
                  label={`Wave ${w?.wave_number} — ${w?.vm_count ?? 0} VMs`}
                />
              ))}
              <FormSelectOption value={NEW_WAVE} label="New wave (added after the last one)" />
            </FormSelect>
          </FormGroup>
          <FormGroup label="Reason (optional)" fieldId="add-note">
            <TextInput
              id="add-note"
              aria-label="Reason for this change"
              value={note}
              maxLength={500}
              onChange={(_e, v) => setNote(v)}
            />
          </FormGroup>
        </Form>

        {body && preview.loading && !result && (
          <Skeleton className="pf-v6-u-mt-md" screenreaderText="Checking this change" />
        )}
        {preview.error && (
          <Alert
            variant="danger"
            isInline
            title="This change cannot be made"
            className="pf-v6-u-mt-md"
          >
            {preview.error?.detail || preview.error?.message}
          </Alert>
        )}
        <EditViolations violations={result?.violations} />
        {result && needsAck && !isBlocked && (
          <Checkbox
            id="add-acknowledge"
            className="pf-v6-u-mt-md"
            label="I know this overrides a planner rule and want to make the change anyway"
            isChecked={acknowledged}
            onChange={(_e, checked) => setAcknowledged(checked)}
          />
        )}
        {commitError && (
          <>
            <Alert
              variant="danger"
              isInline
              title="The change was not applied"
              className="pf-v6-u-mt-md"
            >
              {violationsFromError(commitError).length === 0 &&
                (commitError?.detail || commitError?.message)}
            </Alert>
            <EditViolations violations={violationsFromError(commitError)} />
          </>
        )}
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={confirm} isLoading={busy} isDisabled={!canConfirm}>
          {`Add ${count} VM${count === 1 ? "" : "s"}`}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}
