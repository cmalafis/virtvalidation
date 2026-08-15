// OpenShift target clusters.
//
// Registration is declarative: VirtValidate does NOT authenticate to the
// cluster. The operator states what exists, and the catalogs (networks,
// storage classes, namespaces) on the detail page are what plans resolve
// against when emitting MTV YAML.

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import {
  Alert,
  Bullseye,
  Button,
  Form,
  FormGroup,
  FormHelperText,
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
  TextArea,
  TextInput,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState, GuidedEmptyState, NO_TARGETS } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

const CLASSIFICATIONS = ["unclassified", "cui", "secret"];

const BLANK = {
  name: "",
  api_endpoint: "",
  region: "",
  site: "",
  classification_level: "unclassified",
  mtv_namespace: "",
  ocp_version: "",
  kubernetes_version: "",
  notes: "",
};

function toPayload(form) {
  const out = {};
  for (const [k, v] of Object.entries(form)) {
    out[k] = typeof v === "string" && v.trim() === "" ? null : v;
  }
  out.name = form.name.trim();
  out.api_endpoint = form.api_endpoint.trim();
  return out;
}

function TargetModal({ isOpen, editing, onClose, onSaved }) {
  const [form, setForm] = useState(BLANK);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setForm(
      editing
        ? {
            ...BLANK,
            ...Object.fromEntries(
              Object.keys(BLANK).map((k) => [k, editing[k] ?? BLANK[k]]),
            ),
          }
        : BLANK,
    );
  }, [isOpen, editing]);

  const set = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));

  const canSave = form.name.trim() && form.api_endpoint.trim() && !saving;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await fetchJSON(
        editing ? `/api/sources/targets/${editing.id}` : "/api/sources/targets",
        { method: editing ? "PATCH" : "POST", body: toPayload(form) },
      );
      toast.success(editing ? "Target updated" : "Target registered");
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.message ?? "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} variant="medium" aria-label="OpenShift target">
      <ModalHeader
        title={editing ? "Edit target cluster" : "Register a target cluster"}
        description="Where your VMs will land."
      />
      <ModalBody>
        <Alert
          variant="info"
          isInline
          title="VirtValidate does not connect to this cluster"
          className="pf-v6-u-mb-md"
        >
          Registration is declarative. You describe the cluster and its
          networks, storage classes and namespaces; VirtValidate uses that to
          emit MTV YAML. No credentials are stored and nothing is contacted.
        </Alert>

        {error && (
          <HelperText className="pf-v6-u-mb-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}

        <Form>
          <FormGroup label="Name" isRequired fieldId="t-name">
            <TextInput
              id="t-name"
              value={form.name}
              onChange={set("name")}
              isRequired
              placeholder="ocp-virt-prod-01"
            />
          </FormGroup>

          <FormGroup label="API endpoint" isRequired fieldId="t-api">
            <TextInput
              id="t-api"
              value={form.api_endpoint}
              onChange={set("api_endpoint")}
              isRequired
              placeholder="https://api.ocp-prod.corp.local:6443"
            />
            <FormHelperText>
              <HelperText>
                <HelperTextItem>
                  Recorded for operator reference and emitted into the MTV
                  manifests. Not contacted.
                </HelperTextItem>
              </HelperText>
            </FormHelperText>
          </FormGroup>

          <FormGroup label="Classification" fieldId="t-class">
            <FormSelect
              id="t-class"
              value={form.classification_level}
              onChange={set("classification_level")}
            >
              {CLASSIFICATIONS.map((c) => (
                <FormSelectOption key={c} value={c} label={c} />
              ))}
            </FormSelect>
          </FormGroup>

          <FormGroup label="MTV namespace" fieldId="t-mtv">
            <TextInput
              id="t-mtv"
              value={form.mtv_namespace ?? ""}
              onChange={set("mtv_namespace")}
              placeholder="openshift-mtv"
            />
          </FormGroup>

          <FormGroup label="OpenShift version" fieldId="t-ocp">
            <TextInput
              id="t-ocp"
              value={form.ocp_version ?? ""}
              onChange={set("ocp_version")}
              placeholder="4.16"
            />
          </FormGroup>

          <FormGroup label="Region" fieldId="t-region">
            <TextInput id="t-region" value={form.region ?? ""} onChange={set("region")} />
          </FormGroup>

          <FormGroup label="Site" fieldId="t-site">
            <TextInput id="t-site" value={form.site ?? ""} onChange={set("site")} />
          </FormGroup>

          <FormGroup label="Notes" fieldId="t-notes">
            <TextArea
              id="t-notes"
              value={form.notes ?? ""}
              onChange={set("notes")}
              rows={3}
              resizeOrientation="vertical"
            />
          </FormGroup>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={save} isDisabled={!canSave} isLoading={saving}>
          {editing ? "Save" : "Register"}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={saving}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}

export default function OCPTargetsPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON("/api/sources/targets");
      setRows(Array.isArray(data) ? data : []);
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
    await fetchJSON(`/api/sources/targets/${deleting.id}`, { method: "DELETE" });
    toast.success(`Removed ${deleting.name}`);
    setDeleting(null);
    load();
  };

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && rows.length === 0) {
      return Array.from({ length: 3 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={6}>
            <Skeleton screenreaderText="Loading target clusters" />
          </Td>
        </Tr>
      ));
    }

    if (rows.length === 0) {
      return (
        <Tr>
          <Td colSpan={6}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_TARGETS.title}
                body={NO_TARGETS.body}
                primary={{ label: "Register a target", onClick: openCreate }}
                secondary={{ label: "Register a vCenter first", to: "/sources/vcenters" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return rows.map((row) => (
      <Tr key={row.id}>
        <Td dataLabel="Name">
          <Link to={`/sources/targets/${row.id}`}>{row.name}</Link>
        </Td>
        <Td dataLabel="API endpoint">{row.api_endpoint}</Td>
        <Td dataLabel="Classification">{row.classification_level}</Td>
        <Td dataLabel="Version">{row.ocp_version || "—"}</Td>
        <Td dataLabel="Status">
          <StatusLabel kind="status" value={row.status === "active" ? "healthy" : "pending"}>
            {row.status}
          </StatusLabel>
        </Td>
        <Td isActionCell>
          <ActionsColumn
            items={[
              {
                title: "Manage catalogs",
                onClick: () => navigate(`/sources/targets/${row.id}`),
              },
              {
                title: "Edit",
                onClick: () => {
                  setEditing(row);
                  setModalOpen(true);
                },
              },
              { isSeparator: true },
              { title: "Delete", isDanger: true, onClick: () => setDeleting(row) },
            ]}
          />
        </Td>
      </Tr>
    ));
  };

  return (
    <PageFrame
      title="OpenShift targets"
      description="Where your VMs will land. Add each cluster's networks, storage classes and namespaces so plans can resolve real destinations."
      breadcrumbs={[{ label: "Configure" }, { label: "OpenShift targets" }]}
      actions={
        <Button variant="primary" onClick={openCreate}>
          Register target
        </Button>
      }
    >
      <PageSection hasBodyWrapper={false}>
        <Table aria-label="OpenShift target clusters" variant="compact">
          <Thead>
            <Tr>
              <Th width={20}>Name</Th>
              <Th width={35}>API endpoint</Th>
              <Th width={15}>Classification</Th>
              <Th width={10}>Version</Th>
              <Th width={10}>Status</Th>
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>

      <TargetModal
        isOpen={modalOpen}
        editing={editing}
        onClose={() => setModalOpen(false)}
        onSaved={load}
      />

      <ConfirmModal
        isOpen={Boolean(deleting)}
        title="Delete target cluster?"
        confirmLabel="Delete"
        isDanger
        onConfirm={remove}
        onClose={() => setDeleting(null)}
      >
        {`${deleting?.name} and its network, storage-class and namespace catalogs will be removed. Resource mappings that reference it will need updating.`}
      </ConfirmModal>
    </PageFrame>
  );
}
