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
import ImportCatalogModal from "./targets/ImportCatalogModal";
import PlatformFeatures from "./targets/PlatformFeatures";

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

// Value sets mirror app.core.platform_features (pinned there by a test).
const ROLES = [
  ["", "—"],
  ["primary", "Primary (replaces the pod network)"],
  ["secondary", "Secondary"],
];
const TOPOLOGIES = [
  ["", "—"],
  ["layer2", "Layer2"],
  ["layer3", "Layer3"],
  ["localnet", "Localnet (physical VLAN)"],
  ["bridge", "Linux bridge"],
  ["sriov", "SR-IOV"],
  ["other", "Other"],
];
const IPAM_MODES = [
  ["", "—"],
  ["persistent", "Persistent (keeps IP across live migration)"],
  ["disabled", "Disabled"],
  ["none", "None"],
];
const BINDINGS = [
  ["", "—"],
  ["l2bridge", "l2bridge"],
  ["passt", "passt (Tech Preview)"],
  ["bridge", "bridge"],
  ["sriov", "sriov"],
];
const TRANSPORTS = [
  ["", "—"],
  ["geneve", "Geneve (default overlay)"],
  ["evpn", "BGP EVPN (4.22+, bare metal)"],
];
const VOLUME_MODES = [
  ["", "Let the StorageProfile decide"],
  ["Block", "Block"],
  ["Filesystem", "Filesystem"],
];
const OFFLOAD_VENDORS = [
  ["", "No copy offload"],
  ["ontap", "NetApp ONTAP"],
  ["pureFlashArray", "Pure FlashArray"],
  ["powerstore", "Dell PowerStore"],
  ["powermax", "Dell PowerMax"],
  ["powerflex", "Dell PowerFlex (host copy)"],
  ["flashsystem", "IBM FlashSystem"],
  ["vantara", "Hitachi Vantara"],
  ["primera3par", "HPE Primera / 3PAR (Developer Preview)"],
  ["infinibox", "Infinidat InfiniBox (Developer Preview)"],
];

const options = (pairs) =>
  pairs.map(([v, l]) => <FormSelectOption key={v || "none"} value={v} label={l} />);

const helper = (text) => (
  <FormHelperText>
    <HelperText>
      <HelperTextItem>{text}</HelperTextItem>
    </HelperText>
  </FormHelperText>
);

const intOrNull = (v) => {
  if (v === null || v === undefined || String(v).trim() === "") return null;
  const n = Number.parseInt(String(v), 10);
  return Number.isNaN(n) ? null : n;
};

// "Primary · Layer2" / "Localnet · VLAN 200" — what a network row means
// for MTV at a glance.
function networkShape(row) {
  if (!row) return "—";
  if (row.network_type === "pod") return "Cluster default network";
  const parts = [];
  if (row.role) parts.push(row.role === "primary" ? "Primary" : "Secondary");
  const topo = TOPOLOGIES.find(([v]) => v && v === row.topology);
  if (topo) parts.push(topo[1].replace(/ \(.*\)$/, ""));
  if (row.vlan_id !== null && row.vlan_id !== undefined) parts.push(`VLAN ${row.vlan_id}`);
  if (row.transport === "evpn") parts.push("EVPN");
  return parts.length ? parts.join(" · ") : "—";
}

const CATALOGS = {
  networks: {
    label: "Networks",
    path: "networks",
    blank: {
      name: "",
      network_type: "nad",
      namespace: "",
      role: "",
      topology: "",
      vlan_id: "",
      physical_network_name: "",
      subnets: "",
      ipam_mode: "",
      binding: "",
      transport: "",
      mtu: "",
      is_default: false,
      notes: "",
    },
    fromRow: (row) => ({ ...row, subnets: asArray(row?.subnets).join(", ") }),
    toBody: (body) => ({
      ...body,
      vlan_id: intOrNull(body.vlan_id),
      mtu: intOrNull(body.mtu),
      subnets: body.subnets
        ? String(body.subnets)
            .split(/[\s,]+/)
            .filter(Boolean)
        : null,
    }),
    columns: [
      { key: "name", label: "Name", width: 25 },
      { key: "network_type", label: "Type", width: 10 },
      { key: "shape", label: "Role / topology", width: 25, render: networkShape },
      { key: "namespace", label: "Namespace", width: 20 },
      { key: "is_default", label: "Default", width: 10 },
    ],
    empty: {
      title: "No networks in this cluster's catalog",
      body: "Add the NetworkAttachmentDefinitions and UDN/CUDNs that migrated VMs will attach to — or use Import from cluster. Resource mappings translate vSphere port groups into these.",
    },
    fields: (form, set) => {
      const isUdn = form.network_type === "udn" || form.network_type === "cudn";
      const isPod = form.network_type === "pod";
      const isPrimary = isUdn && form.role === "primary";
      const isLocalnet = form.topology === "localnet";
      return (
        <>
          <FormGroup label="Name" isRequired fieldId="n-name">
            <TextInput id="n-name" value={form.name} onChange={set("name")} isRequired />
          </FormGroup>
          <FormGroup label="Type" fieldId="n-type">
            <FormSelect id="n-type" value={form.network_type} onChange={set("network_type")}>
              {options(NETWORK_TYPES)}
            </FormSelect>
          </FormGroup>
          {!isPod && (
            <FormGroup label="Namespace" fieldId="n-ns">
              <TextInput id="n-ns" value={form.namespace ?? ""} onChange={set("namespace")} />
              {helper(
                form.network_type === "cudn"
                  ? "Leave blank: a CUDN is cluster-scoped and its NAD appears in every selected namespace."
                  : "Where the NAD / UDN lives.",
              )}
            </FormGroup>
          )}
          {isUdn && (
            <FormGroup label="Role" fieldId="n-role">
              <FormSelect id="n-role" value={form.role ?? ""} onChange={set("role")}>
                {options(ROLES)}
              </FormSelect>
              {helper(
                "A primary UDN is emitted as NetworkMap type pod — MTV detects it from the namespace. Secondary networks are emitted as multus.",
              )}
            </FormGroup>
          )}
          {!isPod && (
            <FormGroup label="Topology" fieldId="n-topo">
              <FormSelect id="n-topo" value={form.topology ?? ""} onChange={set("topology")}>
                {options(TOPOLOGIES)}
              </FormSelect>
            </FormGroup>
          )}
          {!isPod && !isPrimary && (
            <FormGroup label="VLAN ID" fieldId="n-vlan">
              <TextInput
                id="n-vlan"
                type="number"
                value={form.vlan_id ?? ""}
                onChange={set("vlan_id")}
              />
              {helper("Used to match vSphere port groups by VLAN when suggesting mappings.")}
            </FormGroup>
          )}
          {isLocalnet && (
            <FormGroup label="Physical network name" fieldId="n-phys">
              <TextInput
                id="n-phys"
                value={form.physical_network_name ?? ""}
                onChange={set("physical_network_name")}
              />
              {helper("Must match a localnet in a NodeNetworkConfigurationPolicy bridge mapping.")}
            </FormGroup>
          )}
          {isUdn && (
            <FormGroup label="Subnets" fieldId="n-subnets">
              <TextInput id="n-subnets" value={form.subnets ?? ""} onChange={set("subnets")} />
              {helper(
                "Comma-separated CIDRs. VMs that keep a static IP must have it inside these.",
              )}
            </FormGroup>
          )}
          {!isPod && (
            <FormGroup label="IPAM" fieldId="n-ipam">
              <FormSelect id="n-ipam" value={form.ipam_mode ?? ""} onChange={set("ipam_mode")}>
                {options(IPAM_MODES)}
              </FormSelect>
            </FormGroup>
          )}
          {isPrimary && (
            <>
              <FormGroup label="Binding" fieldId="n-binding">
                <FormSelect id="n-binding" value={form.binding ?? ""} onChange={set("binding")}>
                  {options(BINDINGS)}
                </FormSelect>
              </FormGroup>
              <FormGroup label="Transport" fieldId="n-transport">
                <FormSelect
                  id="n-transport"
                  value={form.transport ?? ""}
                  onChange={set("transport")}
                >
                  {options(TRANSPORTS)}
                </FormSelect>
              </FormGroup>
            </>
          )}
          {!isPod && (
            <FormGroup label="MTU" fieldId="n-mtu">
              <TextInput id="n-mtu" type="number" value={form.mtu ?? ""} onChange={set("mtu")} />
            </FormGroup>
          )}
        </>
      );
    },
  },

  "storage-classes": {
    label: "Storage classes",
    path: "storage-classes",
    blank: {
      name: "",
      access_mode: "ReadWriteOnce",
      volume_mode: "",
      provisioner: "",
      copy_offload_vendor: "",
      copy_offload_secret: "",
      is_default: false,
      notes: "",
    },
    columns: [
      { key: "name", label: "Name", width: 30 },
      { key: "access_mode", label: "Access mode", width: 15 },
      { key: "volume_mode", label: "Volume mode", width: 15 },
      { key: "copy_offload_vendor", label: "Copy offload", width: 15 },
      { key: "is_default", label: "Default", width: 10 },
    ],
    empty: {
      title: "No storage classes in this cluster's catalog",
      body: "Add the StorageClasses available on this cluster — or use Import from cluster. Mappings translate vSphere datastores into these when emitting the StorageMap.",
    },
    fields: (form, set) => (
      <>
        <FormGroup label="Name" isRequired fieldId="s-name">
          <TextInput id="s-name" value={form.name} onChange={set("name")} isRequired />
        </FormGroup>
        <FormGroup label="Provisioner" fieldId="s-prov">
          <TextInput id="s-prov" value={form.provisioner ?? ""} onChange={set("provisioner")} />
        </FormGroup>
        <FormGroup label="Access mode" fieldId="s-mode">
          <FormSelect id="s-mode" value={form.access_mode} onChange={set("access_mode")}>
            {options(ACCESS_MODES)}
          </FormSelect>
          {helper("Live migration between nodes needs ReadWriteMany.")}
        </FormGroup>
        <FormGroup label="Volume mode" fieldId="s-vmode">
          <FormSelect id="s-vmode" value={form.volume_mode ?? ""} onChange={set("volume_mode")}>
            {options(VOLUME_MODES)}
          </FormSelect>
        </FormGroup>
        <FormGroup label="Storage copy offload" fieldId="s-offload">
          <FormSelect
            id="s-offload"
            value={form.copy_offload_vendor ?? ""}
            onChange={set("copy_offload_vendor")}
          >
            {options(OFFLOAD_VENDORS)}
          </FormSelect>
          {helper(
            "MTV clones disks on the array instead of copying them over VDDK. GA for cold migration from MTV 2.11, warm from 2.12. Not from NFS datastores.",
          )}
        </FormGroup>
        {form.copy_offload_vendor && (
          <FormGroup label="Array credentials Secret" isRequired fieldId="s-secret">
            <TextInput
              id="s-secret"
              value={form.copy_offload_secret ?? ""}
              onChange={set("copy_offload_secret")}
            />
            {helper("Secret in the MTV namespace holding the storage array credentials.")}
          </FormGroup>
        )}
      </>
    ),
  },

  namespaces: {
    label: "Namespaces",
    path: "namespaces",
    blank: { name: "", description: "", primary_udn_network: "" },
    columns: [
      { key: "name", label: "Name", width: 30 },
      { key: "primary_udn_network", label: "Primary UDN", width: 25 },
      { key: "description", label: "Description", width: 45 },
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
        <FormGroup label="Primary UDN" fieldId="ns-udn">
          <TextInput
            id="ns-udn"
            value={form.primary_udn_network ?? ""}
            onChange={set("primary_udn_network")}
          />
          {helper(
            "The primary UDN/CUDN serving this namespace, if any. The namespace label is set at creation and cannot be added later.",
          )}
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
    const source = editing && catalog.fromRow ? catalog.fromRow(editing) : editing;
    setForm(
      source
        ? Object.fromEntries(
            Object.keys(catalog.blank).map((k) => [k, source[k] ?? catalog.blank[k]]),
          )
        : catalog.blank,
    );
  }, [isOpen, editing, catalog]);

  const set = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      let body = Object.fromEntries(
        Object.entries(form).map(([k, v]) => [
          k,
          typeof v === "string" && v.trim() === "" ? null : v,
        ]),
      );
      body.name = form.name.trim();
      if (catalog.toBody) body = catalog.toBody(body);

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
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>
            {rows.map((row) => (
              <Tr key={row.id}>
                {catalog.columns.map((c) => (
                  <Td key={c.key} dataLabel={c.label}>
                    {c.render ? (
                      c.render(row)
                    ) : c.key === "is_default" ? (
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
  const [importOpen, setImportOpen] = useState(false);
  // Bumped after an import so the open catalog tab remounts and reloads.
  const [catalogVersion, setCatalogVersion] = useState(0);

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
        <div className="pf-v6-u-mb-md">
          <Button variant="primary" onClick={() => setImportOpen(true)}>
            Import from cluster
          </Button>
        </div>
        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Cluster catalogs">
          {Object.entries(CATALOGS).map(([key, catalog]) => (
            <Tab key={key} eventKey={key} title={<TabTitleText>{catalog.label}</TabTitleText>}>
              <div className="pf-v6-u-mt-md">
                {tab === key && (
                  <CatalogTab key={catalogVersion} catalog={catalog} targetId={id} />
                )}
              </div>
            </Tab>
          ))}
          <Tab eventKey="platform" title={<TabTitleText>Platform support</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              {tab === "platform" && <PlatformFeatures key={catalogVersion} targetId={id} />}
            </div>
          </Tab>
        </Tabs>
      </PageSection>
      <ImportCatalogModal
        isOpen={importOpen}
        targetId={id}
        onClose={() => setImportOpen(false)}
        onApplied={() => {
          setCatalogVersion((v) => v + 1);
          load();
        }}
      />
    </PageFrame>
  );
}
