// vCenter sources — the first step in the workflow.
//
// A vCenter is the partition key for migration plans (plans never span
// vCenters), and its classification level drives the authorization gate
// on production waves. That's why this is the first item under Configure.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import {
  ActionsColumn,
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
} from "@patternfly/react-table";
import {
  Bullseye,
  Button,
  Form,
  FormGroup,
  FormHelperText,
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
  FormSelect,
  FormSelectOption,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState, GuidedEmptyState, NO_VCENTERS } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

const CLASSIFICATIONS = ["unclassified", "cui", "secret"];
const STATUSES = ["active", "paused", "archived"];

const BLANK = {
  name: "",
  hostname: "",
  region: "",
  site: "",
  classification_level: "unclassified",
  status: "active",
  default_target_namespace: "",
  default_target_storage_class: "",
  notes: "",
};

// The API rejects "" for optional constrained fields in some cases and
// stores it as an empty string in others; null is the unambiguous
// "not set" for every one of them.
function toPayload(form) {
  const out = {};
  for (const [k, v] of Object.entries(form)) {
    out[k] = typeof v === "string" && v.trim() === "" ? null : v;
  }
  // These two are required — never send null.
  out.name = form.name.trim();
  out.hostname = form.hostname.trim();
  return out;
}

function VCenterModal({ isOpen, editing, onClose, onSaved }) {
  const [form, setForm] = useState(BLANK);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setForm(
      editing
        ? { ...BLANK, ...Object.fromEntries(
            Object.keys(BLANK).map((k) => [k, editing[k] ?? BLANK[k]]),
          ) }
        : BLANK,
    );
  }, [isOpen, editing]);

  const set = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));
  // FormSelect's handler has the opposite argument order to TextInput's.
  const setSelect = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));

  const canSave = form.name.trim() && form.hostname.trim() && !saving;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await fetchJSON(
        editing ? `/api/sources/vcenters/${editing.id}` : "/api/sources/vcenters",
        { method: editing ? "PATCH" : "POST", body: toPayload(form) },
      );
      toast.success(editing ? "vCenter updated" : "vCenter registered");
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.message ?? "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} variant="medium" aria-label="vCenter source">
      <ModalHeader
        title={editing ? "Edit vCenter source" : "Register a vCenter source"}
        description="VirtValidate reads VM inventory and state from this vCenter. Migration plans never span vCenters, so this is also a plan partition boundary."
      />
      <ModalBody>
        {error && (
          <HelperText className="pf-v6-u-mb-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}
        <Form id="vcenter-form">
          <FormGroup label="Name" isRequired fieldId="vc-name">
            <TextInput
              id="vc-name"
              value={form.name}
              onChange={set("name")}
              isRequired
              placeholder="prod-vcenter-01"
            />
          </FormGroup>

          <FormGroup label="Hostname" isRequired fieldId="vc-hostname">
            <TextInput
              id="vc-hostname"
              value={form.hostname}
              onChange={set("hostname")}
              isRequired
              placeholder="vcenter-prod-01.corp.local"
            />
            <FormHelperText>
              <HelperText>
                <HelperTextItem>
                  Must match the hostname in your RVTools export so imports
                  attach to the right source.
                </HelperTextItem>
              </HelperText>
            </FormHelperText>
          </FormGroup>

          <FormGroup label="Classification" fieldId="vc-class">
            <FormSelect
              id="vc-class"
              value={form.classification_level}
              onChange={setSelect("classification_level")}
            >
              {CLASSIFICATIONS.map((c) => (
                <FormSelectOption key={c} value={c} label={c} />
              ))}
            </FormSelect>
            <FormHelperText>
              <HelperText>
                <HelperTextItem>
                  CUI and above require a named authorizer before any wave
                  touching this vCenter can run.
                </HelperTextItem>
              </HelperText>
            </FormHelperText>
          </FormGroup>

          <FormGroup label="Status" fieldId="vc-status">
            <FormSelect id="vc-status" value={form.status} onChange={setSelect("status")}>
              {STATUSES.map((s) => (
                <FormSelectOption key={s} value={s} label={s} />
              ))}
            </FormSelect>
          </FormGroup>

          <FormGroup label="Region" fieldId="vc-region">
            <TextInput id="vc-region" value={form.region ?? ""} onChange={set("region")} />
          </FormGroup>

          <FormGroup label="Site" fieldId="vc-site">
            <TextInput id="vc-site" value={form.site ?? ""} onChange={set("site")} />
          </FormGroup>

          <FormGroup label="Default target namespace" fieldId="vc-ns">
            <TextInput
              id="vc-ns"
              value={form.default_target_namespace ?? ""}
              onChange={set("default_target_namespace")}
            />
          </FormGroup>

          <FormGroup label="Default target storage class" fieldId="vc-sc">
            <TextInput
              id="vc-sc"
              value={form.default_target_storage_class ?? ""}
              onChange={set("default_target_storage_class")}
            />
          </FormGroup>

          <FormGroup label="Notes" fieldId="vc-notes">
            <TextArea
              id="vc-notes"
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

export default function VCenterSourcesPage() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON("/api/sources/vcenters");
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
    await fetchJSON(`/api/sources/vcenters/${deleting.id}`, { method: "DELETE" });
    toast.success(`Removed ${deleting.name}`);
    setDeleting(null);
    load();
  };

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  const openEdit = (row) => {
    setEditing(row);
    setModalOpen(true);
  };

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && rows.length === 0) {
      return Array.from({ length: 3 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={7}>
            <Skeleton screenreaderText="Loading vCenter sources" />
          </Td>
        </Tr>
      ));
    }

    if (rows.length === 0) {
      return (
        <Tr>
          <Td colSpan={7}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_VCENTERS.title}
                body={NO_VCENTERS.body}
                primary={{ label: "Register a vCenter", onClick: openCreate }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return rows.map((row) => (
      <Tr key={row.id}>
        <Td dataLabel="Name">{row.name}</Td>
        <Td dataLabel="Hostname">{row.hostname}</Td>
        <Td dataLabel="Classification">
          <StatusLabel
            kind="record"
            value={row.classification_level === "unclassified" ? "draft" : "open"}
          >
            {row.classification_level}
          </StatusLabel>
        </Td>
        <Td dataLabel="Status">
          <StatusLabel
            kind="status"
            value={row.status === "active" ? "healthy" : "pending"}
          >
            {row.status}
          </StatusLabel>
        </Td>
        <Td dataLabel="VMs">
          {row.vm_count > 0 ? (
            <Link to={`/inventory?vcenter_source_id=${row.id}`}>{row.vm_count}</Link>
          ) : (
            0
          )}
        </Td>
        <Td dataLabel="Site">{row.site || row.region || "—"}</Td>
        <Td isActionCell>
          <ActionsColumn
            items={[
              { title: "Edit", onClick: () => openEdit(row) },
              { isSeparator: true },
              {
                title: "Import RVTools export",
                onClick: () => toast("Use Import VMs on the inventory page"),
              },
              { isSeparator: true },
              {
                title: "Delete",
                isDanger: true,
                onClick: () => setDeleting(row),
              },
            ]}
          />
        </Td>
      </Tr>
    ));
  };

  return (
    <PageFrame
      title="vCenter sources"
      description="Where VirtValidate reads VM inventory from. Plans never span vCenters, so each source is also a plan partition boundary."
      breadcrumbs={[{ label: "Configure" }, { label: "vCenter sources" }]}
      actions={
        <Button variant="primary" onClick={openCreate}>
          Register vCenter
        </Button>
      }
    >
      <PageSection hasBodyWrapper={false}>
        <Table aria-label="vCenter sources" variant="compact">
          <Thead>
            <Tr>
              <Th width={20}>Name</Th>
              <Th width={25}>Hostname</Th>
              <Th width={15}>Classification</Th>
              <Th width={10}>Status</Th>
              <Th width={10}>VMs</Th>
              <Th width={15}>Site</Th>
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>

      <VCenterModal
        isOpen={modalOpen}
        editing={editing}
        onClose={() => setModalOpen(false)}
        onSaved={load}
      />

      <ConfirmModal
        isOpen={Boolean(deleting)}
        title="Delete vCenter source?"
        confirmLabel="Delete"
        isDanger
        onConfirm={remove}
        onClose={() => setDeleting(null)}
      >
        {deleting?.vm_count > 0
          ? `${deleting.name} still has ${deleting.vm_count} VM(s) attached. Deleting the source does not delete those VMs, but they will no longer resolve to a vCenter.`
          : `${deleting?.name} will be removed. This cannot be undone.`}
      </ConfirmModal>
    </PageFrame>
  );
}
