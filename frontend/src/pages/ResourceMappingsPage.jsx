// Resource mappings — the index.
//
// A mapping translates one vCenter's networks and datastores into one
// OpenShift cluster's NADs and StorageClasses. Plan generation resolves
// every selected VM through a mapping at stage 0, so an incomplete
// mapping is the most common reason plan generation fails.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import {
  Bullseye,
  Button,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  PageSection,
  Skeleton,
  TextInput,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState, GuidedEmptyState, NO_MAPPINGS } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

function CreateModal({ isOpen, vcenters, targets, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [vcenterId, setVcenterId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setName("");
    setVcenterId(vcenters?.[0]?.id ? String(vcenters[0].id) : "");
    setTargetId(targets?.[0]?.id ? String(targets[0].id) : "");
    setError(null);
    setBusy(false);
  }, [isOpen, vcenters, targets]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await fetchJSON("/api/mappings", {
        method: "POST",
        body: {
          name: name.trim(),
          vcenter_source_id: Number(vcenterId),
          ocp_target_id: Number(targetId),
        },
      });
      toast.success("Mapping created");
      onCreated(created);
      onClose();
    } catch (e) {
      setError(e?.message ?? "Could not create the mapping");
      setBusy(false);
    }
  };

  const canSave = name.trim() && vcenterId && targetId && !busy;

  return (
    <Modal isOpen={isOpen} onClose={busy ? undefined : onClose} variant="medium">
      <ModalHeader
        title="Create a resource mapping"
        description="One mapping covers one vCenter to one OpenShift cluster. Add the per-network and per-datastore rows after creating it."
      />
      <ModalBody>
        {error && (
          <HelperText className="pf-v6-u-mb-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}
        <Form>
          <FormGroup label="Name" isRequired fieldId="m-name">
            <TextInput
              id="m-name"
              value={name}
              onChange={(_e, v) => setName(v)}
              isRequired
              placeholder="prod-vcenter-01 → ocp-virt-prod"
            />
          </FormGroup>
          <FormGroup label="Source vCenter" isRequired fieldId="m-vc">
            <FormSelect id="m-vc" value={vcenterId} onChange={(_e, v) => setVcenterId(v)}>
              {(vcenters ?? []).length === 0 && (
                <FormSelectOption value="" label="No vCenters registered" />
              )}
              {(vcenters ?? []).map((v) => (
                <FormSelectOption key={v.id} value={String(v.id)} label={v.name} />
              ))}
            </FormSelect>
          </FormGroup>
          <FormGroup label="Target cluster" isRequired fieldId="m-target">
            <FormSelect id="m-target" value={targetId} onChange={(_e, v) => setTargetId(v)}>
              {(targets ?? []).length === 0 && (
                <FormSelectOption value="" label="No target clusters registered" />
              )}
              {(targets ?? []).map((t) => (
                <FormSelectOption key={t.id} value={String(t.id)} label={t.name} />
              ))}
            </FormSelect>
          </FormGroup>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={save} isDisabled={!canSave} isLoading={busy}>
          Create
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}

export default function ResourceMappingsPage() {
  const [rows, setRows] = useState([]);
  const [vcenters, setVcenters] = useState([]);
  const [targets, setTargets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [m, v, t] = await Promise.allSettled([
        fetchJSON("/api/mappings"),
        fetchJSON("/api/sources/vcenters"),
        fetchJSON("/api/sources/targets"),
      ]);
      if (m.status === "rejected") throw m.reason;
      setRows(Array.isArray(m.value) ? m.value : []);
      setVcenters(v.status === "fulfilled" && Array.isArray(v.value) ? v.value : []);
      setTargets(t.status === "fulfilled" && Array.isArray(t.value) ? t.value : []);
      setError(null);
    } catch (e) {
      setError(e);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const remove = async () => {
    await fetchJSON(`/api/mappings/${deleting.id}`, { method: "DELETE" });
    toast.success("Mapping deleted");
    setDeleting(null);
    load();
  };

  const nameOf = (list, id) => list.find((x) => x.id === id)?.name ?? `#${id}`;

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && rows.length === 0) {
      return Array.from({ length: 3 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={7}>
            <Skeleton screenreaderText="Loading mappings" />
          </Td>
        </Tr>
      ));
    }

    if (rows.length === 0) {
      const blocked = vcenters.length === 0 || targets.length === 0;
      return (
        <Tr>
          <Td colSpan={7}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_MAPPINGS.title}
                body={
                  blocked
                    ? "A mapping connects a registered vCenter to a registered target cluster. Register both first."
                    : NO_MAPPINGS.body
                }
                primary={
                  blocked
                    ? { label: "Register a vCenter", to: "/sources/vcenters" }
                    : { label: "Create a mapping", onClick: () => setCreateOpen(true) }
                }
                secondary={
                  blocked ? { label: "Register a target", to: "/sources/targets" } : undefined
                }
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return rows.map((row) => (
      <Tr key={row.id}>
        <Td dataLabel="Name">
          <Link to={`/mappings/${row.id}`}>{row.name}</Link>
        </Td>
        <Td dataLabel="Source">{nameOf(vcenters, row.vcenter_source_id)}</Td>
        <Td dataLabel="Target">{nameOf(targets, row.ocp_target_id)}</Td>
        <Td dataLabel="Networks">{(row.network_mappings ?? []).length}</Td>
        <Td dataLabel="Storage">{(row.storage_mappings ?? []).length}</Td>
        <Td dataLabel="Status">
          <StatusLabel kind="record" value={row.status} />
        </Td>
        <Td isActionCell>
          <ActionsColumn
            items={[{ title: "Delete", isDanger: true, onClick: () => setDeleting(row) }]}
          />
        </Td>
      </Tr>
    ));
  };

  return (
    <PageFrame
      title="Resource mappings"
      description="Translate vSphere networks and datastores into OpenShift NADs and StorageClasses. Plan generation resolves every VM through one of these."
      breadcrumbs={[{ label: "Configure" }, { label: "Resource mappings" }]}
      actions={
        <Button
          variant="primary"
          onClick={() => setCreateOpen(true)}
          isDisabled={vcenters.length === 0 || targets.length === 0}
        >
          Create mapping
        </Button>
      }
    >
      <PageSection hasBodyWrapper={false}>
        <Table aria-label="Resource mappings" variant="compact">
          <Thead>
            <Tr>
              <Th width={25}>Name</Th>
              <Th width={20}>Source vCenter</Th>
              <Th width={20}>Target cluster</Th>
              <Th width={10}>Networks</Th>
              <Th width={10}>Storage</Th>
              <Th width={15}>Status</Th>
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>

      <CreateModal
        isOpen={createOpen}
        vcenters={vcenters}
        targets={targets}
        onClose={() => setCreateOpen(false)}
        onCreated={load}
      />

      <ConfirmModal
        isOpen={Boolean(deleting)}
        title="Delete this mapping?"
        confirmLabel="Delete"
        isDanger
        onConfirm={remove}
        onClose={() => setDeleting(null)}
      >
        {`${deleting?.name} will be removed. Plans already generated keep their emitted YAML, but new plans covering this vCenter and cluster will have nothing to resolve through.`}
      </ConfirmModal>
    </PageFrame>
  );
}
