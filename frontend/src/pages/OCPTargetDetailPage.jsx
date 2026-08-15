// Target cluster catalogs — networks, storage classes, namespaces.
//
// These three lists are what plan generation resolves against when it
// emits MTV YAML. A gap here surfaces as a stage-0 mapping failure with a
// per-VM report, so it's worth getting complete.
//
// The three entities have the same CRUD shape, so one generic table +
// modal drives all three; only the field definitions differ.

import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import toast from "react-hot-toast";
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import {
  Alert,
  Bullseye,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Label,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
  TextArea,
  TextInput,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState, GuidedEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

// StorageAccessMode has member names that differ from their values
// (rwo = "ReadWriteOnce"). The API emits and accepts the VALUE, so these
// options carry values, not names — see the enum I/O rules in CLAUDE.md.
const ACCESS_MODES = [
  ["ReadWriteOnce", "ReadWriteOnce (RWO)"],
  ["ReadWriteMany", "ReadWriteMany (RWX)"],
  ["ReadOnlyMany", "ReadOnlyMany (ROX)"],
];

const NETWORK_TYPES = [
  ["nad", "NetworkAttachmentDefinition"],
  ["cudn", "ClusterUserDefinedNetwork"],
  ["udn", "UserDefinedNetwork"],
  ["pod", "Pod network"],
];

const CATALOGS = {
  networks: {
    label: "Networks",
    path: "networks",
    blank: { name: "", network_type: "nad", namespace: "", is_default: false, notes: "" },
    columns: [
      { key: "name", label: "Name", width: 30 },
      { key: "network_type", label: "Type", width: 20 },
      { key: "namespace", label: "Namespace", width: 25 },
      { key: "is_default", label: "Default", width: 15 },
    ],
    empty: {
      title: "No networks in this cluster's catalog",
      body: "Add the NetworkAttachmentDefinitions (or UDN/CUDN) that migrated VMs will attach to. Resource mappings translate vSphere port groups into these.",
    },
    fields: (form, set) => (
      <>
        <FormGroup label="Name" isRequired fieldId="n-name">
          <TextInput id="n-name" value={form.name} onChange={set("name")} isRequired />
        </FormGroup>
        <FormGroup label="Type" fieldId="n-type">
          <FormSelect id="n-type" value={form.network_type} onChange={set("network_type")}>
            {NETWORK_TYPES.map(([v, l]) => (
              <FormSelectOption key={v} value={v} label={l} />
            ))}
          </FormSelect>
        </FormGroup>
        <FormGroup label="Namespace" fieldId="n-ns">
          <TextInput id="n-ns" value={form.namespace ?? ""} onChange={set("namespace")} />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Where the NAD lives. Leave blank for cluster-scoped types.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
      </>
    ),
  },

  "storage-classes": {
    label: "Storage classes",
    path: "storage-classes",
    blank: { name: "", access_mode: "ReadWriteOnce", is_default: false, notes: "" },
    columns: [
      { key: "name", label: "Name", width: 40 },
      { key: "access_mode", label: "Access mode", width: 30 },
      { key: "is_default", label: "Default", width: 20 },
    ],
    empty: {
      title: "No storage classes in this cluster's catalog",
      body: "Add the StorageClasses available on this cluster. Mappings translate vSphere datastores into these when emitting the StorageMap.",
    },
    fields: (form, set) => (
      <>
        <FormGroup label="Name" isRequired fieldId="s-name">
          <TextInput id="s-name" value={form.name} onChange={set("name")} isRequired />
        </FormGroup>
        <FormGroup label="Access mode" fieldId="s-mode">
          <FormSelect id="s-mode" value={form.access_mode} onChange={set("access_mode")}>
            {ACCESS_MODES.map(([v, l]) => (
              <FormSelectOption key={v} value={v} label={l} />
            ))}
          </FormSelect>
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                Live migration between nodes needs ReadWriteMany.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
      </>
    ),
  },

  namespaces: {
    label: "Namespaces",
    path: "namespaces",
    blank: { name: "", description: "" },
    columns: [
      { key: "name", label: "Name", width: 40 },
      { key: "description", label: "Description", width: 60 },
    ],
    empty: {
      title: "No namespaces in this cluster's catalog",
      body: "Add the namespaces migrated VMs will land in. Plans partition by target namespace, so this also shapes how waves are grouped.",
    },
    fields: (form, set) => (
      <>
        <FormGroup label="Name" isRequired fieldId="ns-name">
          <TextInput id="ns-name" value={form.name} onChange={set("name")} isRequired />
        </FormGroup>
        <FormGroup label="Description" fieldId="ns-desc">
          <TextArea
            id="ns-desc"
            value={form.description ?? ""}
            onChange={set("description")}
            rows={3}
            resizeOrientation="vertical"
          />
        </FormGroup>
      </>
    ),
  },
};

function EntityModal({ isOpen, catalog, editing, targetId, onClose, onSaved }) {
  const [form, setForm] = useState(catalog.blank);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setBusy(false);
    setForm(
      editing
        ? Object.fromEntries(
            Object.keys(catalog.blank).map((k) => [k, editing[k] ?? catalog.blank[k]]),
          )
        : catalog.blank,
    );
  }, [isOpen, editing, catalog]);

  const set = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = Object.fromEntries(
        Object.entries(form).map(([k, v]) => [
          k,
          typeof v === "string" && v.trim() === "" ? null : v,
        ]),
      );
      body.name = form.name.trim();

      await fetchJSON(
        editing
          ? `/api/ocp-targets/${targetId}/${catalog.path}/${editing.id}`
          : `/api/ocp-targets/${targetId}/${catalog.path}`,
        { method: editing ? "PATCH" : "POST", body },
      );
      toast.success(editing ? "Saved" : "Added");
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.message ?? "Save failed");
      setBusy(false);
    }
  };

  const hasDefaultFlag = "is_default" in catalog.blank;

  return (
    <Modal isOpen={isOpen} onClose={busy ? undefined : onClose} variant="medium">
      <ModalHeader title={`${editing ? "Edit" : "Add"} ${catalog.label.toLowerCase()}`} />
      <ModalBody>
        {error && (
          <Alert variant="danger" isInline title="Could not save" className="pf-v6-u-mb-md">
            {error}
          </Alert>
        )}
        <Form>
          {catalog.fields(form, set)}

          {hasDefaultFlag && (
            <FormGroup fieldId="is-default">
              <Checkbox
                id="is-default"
                label="Use as the default for this cluster"
                description="Only one entry can be the default; setting this clears it elsewhere."
                isChecked={Boolean(form.is_default)}
                onChange={(_e, v) => setForm((f) => ({ ...f, is_default: v }))}
              />
            </FormGroup>
          )}

          {"notes" in catalog.blank && (
            <FormGroup label="Notes" fieldId="notes">
              <TextArea
                id="notes"
                value={form.notes ?? ""}
                onChange={set("notes")}
                rows={2}
                resizeOrientation="vertical"
              />
            </FormGroup>
          )}
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={save} isDisabled={!form.name.trim() || busy} isLoading={busy}>
          {editing ? "Save" : "Add"}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}

function CatalogTab({ catalog, targetId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchJSON(`/api/ocp-targets/${targetId}/${catalog.path}?limit=500`);
      setRows(asArray(data));
      setError(null);
    } catch (e) {
      setError(e);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [targetId, catalog.path]);

  useEffect(() => {
    load();
  }, [load]);

  const remove = async () => {
    // A 409 here carries `referenced_by` — apiError formats it into a
    // readable list, so ConfirmModal surfaces which mappings block it.
    await fetchJSON(`/api/ocp-targets/${targetId}/${catalog.path}/${deleting.id}`, {
      method: "DELETE",
    });
    toast.success("Removed");
    setDeleting(null);
    load();
  };

  if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;
  if (loading && rows.length === 0) return <Skeleton width="100%" height="120px" />;

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  return (
    <>
      <div className="pf-v6-u-mb-md">
        <Button variant="secondary" onClick={openCreate}>
          {`Add ${catalog.label.toLowerCase().replace(/e?s$/, "")}`}
        </Button>
      </div>

      {rows.length === 0 ? (
        <Bullseye>
          <GuidedEmptyState
            title={catalog.empty.title}
            body={catalog.empty.body}
            primary={{ label: "Add the first one", onClick: openCreate }}
          />
        </Bullseye>
      ) : (
        <Table aria-label={catalog.label} variant="compact">
          <Thead>
            <Tr>
              {catalog.columns.map((c) => (
                <Th key={c.key} width={c.width}>
                  {c.label}
                </Th>
              ))}
              <Td screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>
            {rows.map((row) => (
              <Tr key={row.id}>
                {catalog.columns.map((c) => (
                  <Td key={c.key} dataLabel={c.label}>
                    {c.key === "is_default" ? (
                      row.is_default ? (
                        <Label isCompact color="blue">
                          Default
                        </Label>
                      ) : (
                        "—"
                      )
                    ) : (
                      (row[c.key] ?? "—")
                    )}
                  </Td>
                ))}
                <Td isActionCell>
                  <ActionsColumn
                    items={[
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
            ))}
          </Tbody>
        </Table>
      )}

      <EntityModal
        isOpen={modalOpen}
        catalog={catalog}
        editing={editing}
        targetId={targetId}
        onClose={() => setModalOpen(false)}
        onSaved={load}
      />

      <ConfirmModal
        isOpen={Boolean(deleting)}
        title="Delete this entry?"
        confirmLabel="Delete"
        isDanger
        onConfirm={remove}
        onClose={() => setDeleting(null)}
      >
        {`${deleting?.name} will be removed from this cluster's catalog. If a resource mapping still references it the delete is refused, and the mappings holding it are listed.`}
      </ConfirmModal>
    </>
  );
}

export default function OCPTargetDetailPage() {
  const { id } = useParams();
  const [target, setTarget] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("networks");

  const load = useCallback(async () => {
    try {
      setTarget(await fetchJSON(`/api/sources/targets/${id}`));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const title = target?.name || `Target #${id}`;
  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Configure" },
      { label: "OpenShift targets", to: "/sources/targets" },
      { label: title },
    ],
  };

  if (error) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <ErrorEmptyState error={error} onRetry={load} />
        </PageSection>
      </PageFrame>
    );
  }

  return (
    <PageFrame
      {...frameProps}
      description={
        target?.api_endpoint
          ? `${target.api_endpoint} — catalogs that plan generation resolves against.`
          : undefined
      }
    >
      <PageSection hasBodyWrapper={false}>
        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Cluster catalogs">
          {Object.entries(CATALOGS).map(([key, catalog]) => (
            <Tab key={key} eventKey={key} title={<TabTitleText>{catalog.label}</TabTitleText>}>
              <div className="pf-v6-u-mt-md">
                {tab === key && <CatalogTab catalog={catalog} targetId={id} />}
              </div>
            </Tab>
          ))}
        </Tabs>
      </PageSection>
    </PageFrame>
  );
}
