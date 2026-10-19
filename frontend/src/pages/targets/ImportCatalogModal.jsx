// Import a cluster's catalog from pasted `oc get … -o yaml` output.
//
// The appliance never connects to the cluster. The operator runs the
// commands below wherever they have cluster access, pastes the output,
// previews what would be created or updated, and applies. Parsing is
// deterministic Python (app.core.catalog_import) — no LLM involved.

import { useState } from "react";
import toast from "react-hot-toast";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import {
  Alert,
  Button,
  ClipboardCopy,
  ClipboardCopyVariant,
  Content,
  Form,
  FormGroup,
  Label,
  List,
  ListItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextArea,
  Title,
} from "@patternfly/react-core";

import { asArray } from "../../utils/asArray";
import { fetchJSON } from "../../utils/fetchJSON";

const COMMAND = [
  "{ oc get network-attachment-definitions,userdefinednetworks -A -o yaml",
  "  echo ---",
  "  oc get clusteruserdefinednetworks,storageclasses,storageprofiles,nodenetworkconfigurationpolicies -o yaml",
  "  echo ---",
  "  oc get namespaces -l k8s.ovn.org/primary-user-defined-network -o yaml; } > cluster-catalog.yaml",
].join("\n");

const ACTION_COLOR = { create: "green", update: "blue", unchanged: "grey" };

function ActionLabel({ action }) {
  return (
    <Label isCompact color={ACTION_COLOR[action] ?? "grey"}>
      {action ?? "—"}
    </Label>
  );
}

function PreviewTable({ title, rows, columns }) {
  const items = asArray(rows);
  if (items.length === 0) return null;
  return (
    <>
      <Title headingLevel="h4" className="pf-v6-u-mt-md pf-v6-u-mb-sm">
        {`${title} (${items.length})`}
      </Title>
      <Table aria-label={title} variant="compact">
        <Thead>
          <Tr>
            <Th width={15}>Action</Th>
            {columns.map((c) => (
              <Th key={c.key}>{c.label}</Th>
            ))}
          </Tr>
        </Thead>
        <Tbody>
          {items.map((r) => (
            <Tr key={r?.name}>
              <Td dataLabel="Action">
                <ActionLabel action={r?.action} />
              </Td>
              {columns.map((c) => (
                <Td key={c.key} dataLabel={c.label}>
                  {c.render ? c.render(r) : (r?.[c.key] ?? "—")}
                </Td>
              ))}
            </Tr>
          ))}
        </Tbody>
      </Table>
    </>
  );
}

const NETWORK_COLUMNS = [
  { key: "name", label: "Name" },
  { key: "network_type", label: "Type" },
  {
    key: "shape",
    label: "Role / topology",
    render: (r) =>
      [r?.role, r?.topology, r?.vlan_id != null ? `VLAN ${r.vlan_id}` : null]
        .filter(Boolean)
        .join(" · ") || "—",
  },
  { key: "namespace", label: "Namespace" },
];
const STORAGE_COLUMNS = [
  { key: "name", label: "Name" },
  { key: "provisioner", label: "Provisioner" },
  { key: "access_mode", label: "Access" },
  { key: "volume_mode", label: "Volume mode" },
  { key: "is_default", label: "Default", render: (r) => (r?.is_default ? "Yes" : "—") },
];
const NAMESPACE_COLUMNS = [
  { key: "name", label: "Namespace" },
  { key: "primary_udn_network", label: "Primary UDN" },
];

export default function ImportCatalogModal({ isOpen, targetId, onClose, onApplied }) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const close = () => {
    if (busy) return;
    setPreview(null);
    setError(null);
    onClose();
  };

  const run = async (dryRun) => {
    setBusy(true);
    setError(null);
    try {
      const body = await fetchJSON(`/api/ocp-targets/${targetId}/catalog/import`, {
        method: "POST",
        body: { yaml: text, dry_run: dryRun },
      });
      if (dryRun) {
        setPreview(body ?? null);
      } else {
        toast.success("Catalog imported");
        setPreview(null);
        setText("");
        onApplied?.();
        onClose();
      }
    } catch (e) {
      setError(e?.message ?? "Import failed");
    } finally {
      setBusy(false);
    }
  };

  const changes = preview
    ? [
        ...asArray(preview.networks),
        ...asArray(preview.storage_classes),
        ...asArray(preview.namespaces),
      ].filter((r) => r?.action && r.action !== "unchanged").length
    : 0;
  const warnings = asArray(preview?.warnings);
  const bridgeMappings = asArray(preview?.localnet_bridge_mappings);

  return (
    <Modal isOpen={isOpen} onClose={close} variant="large">
      <ModalHeader
        title="Import catalog from cluster"
        description="VirtValidate never connects to the cluster. Run this where you have access, then paste the output."
      />
      <ModalBody>
        <ClipboardCopy
          isReadOnly
          isExpanded
          variant={ClipboardCopyVariant.expansion}
          hoverTip="Copy"
          clickTip="Copied"
        >
          {COMMAND}
        </ClipboardCopy>

        {error && (
          <Alert variant="danger" isInline title="Could not import" className="pf-v6-u-mt-md">
            {error}
          </Alert>
        )}

        <Form className="pf-v6-u-mt-md">
          <FormGroup label="Pasted YAML or JSON" fieldId="catalog-yaml">
            <TextArea
              id="catalog-yaml"
              aria-label="Pasted cluster YAML"
              value={text}
              onChange={(_e, v) => {
                setText(v);
                setPreview(null);
              }}
              rows={10}
              resizeOrientation="vertical"
              className="pf-v6-u-font-family-monospace"
            />
          </FormGroup>
        </Form>

        {preview && (
          <div className="pf-v6-u-mt-md">
            <Content component="p">
              {changes === 0
                ? "The catalog already matches this export — nothing to change."
                : `${changes} entr${changes === 1 ? "y" : "ies"} will be created or updated. Nothing is deleted, and fields the export doesn't mention are left alone.`}
            </Content>
            {warnings.length > 0 && (
              <Alert variant="warning" isInline title="Check before applying" className="pf-v6-u-mt-sm">
                <List>
                  {warnings.map((w) => (
                    <ListItem key={w}>{w}</ListItem>
                  ))}
                </List>
              </Alert>
            )}
            <PreviewTable title="Networks" rows={preview.networks} columns={NETWORK_COLUMNS} />
            <PreviewTable
              title="Storage classes"
              rows={preview.storage_classes}
              columns={STORAGE_COLUMNS}
            />
            <PreviewTable
              title="Primary-UDN namespaces"
              rows={preview.namespaces}
              columns={NAMESPACE_COLUMNS}
            />
            {bridgeMappings.length > 0 && (
              <Content component="p" className="pf-v6-u-mt-md">
                {`Localnet bridge mappings: ${bridgeMappings.join(", ")}`}
              </Content>
            )}
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        {preview ? (
          <Button
            variant="primary"
            onClick={() => run(false)}
            isDisabled={busy || changes === 0}
            isLoading={busy}
          >
            Apply
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={() => run(true)}
            isDisabled={busy || !text.trim()}
            isLoading={busy}
          >
            Preview
          </Button>
        )}
        <Button variant="link" onClick={close} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}
