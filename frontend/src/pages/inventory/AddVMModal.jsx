// Add a single VM by hand.
//
// The RVTools upload is the bulk path; this is the one-off. It exists
// because exercising the resource-mapping features shouldn't require
// producing an RVTools export first — an operator testing a network or
// storage mapping just needs one VM carrying a source network name.
//
// Deliberately a minimal field set. `name` and `source_hostname` are the
// only fields POST /api/vms requires; the rest are here because they are
// what makes a hand-entered VM behave like an imported one:
//
//   * source_vcenter_id — mapping resolution routes VM → mapping by this
//     (_pick_mapping_for_vm). Leave it null and the VM falls back to the
//     union of every selected mapping's coverage.
//   * environment — a plan partition key (preclassifier.classify). Left
//     blank, the backend's detection cascade infers it from the name.
//   * vsphere_networks / vsphere_datastores — the source values a
//     mapping's rows are keyed on.
//
// Everything else on VMCreate stays available through the API.

import { useEffect, useState } from "react";
import toast from "react-hot-toast";
import {
  Button,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextInput,
} from "@patternfly/react-core";

import { fetchJSON } from "../../utils/fetchJSON";
import { asArray } from "../../utils/asArray";
import { splitList } from "../../utils/parseRVTools";

// Mirrors app.core.environment.Environment. UNKNOWN is omitted: leaving
// the field blank is how you ask for auto-detection, and picking
// "unknown" explicitly would mark the VM user_set and suppress it.
const ENVIRONMENTS = [
  "production",
  "development",
  "test",
  "staging",
  "dr",
  "infrastructure",
  "db_only",
  "non_prod",
];

const EMPTY = {
  name: "",
  source_hostname: "",
  source_vcenter_id: "",
  environment: "",
  vsphere_networks: "",
  vsphere_datastores: "",
};

export default function AddVMModal({ isOpen, onClose, onCreated }) {
  const [form, setForm] = useState(EMPTY);
  const [vcenters, setVcenters] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setForm(EMPTY);
    setError(null);
    setBusy(false);
    fetchJSON("/api/sources/vcenters")
      .then((data) => setVcenters(asArray(data)))
      .catch(() => setVcenters([]));
  }, [isOpen]);

  const set = (key) => (_e, value) => setForm((f) => ({ ...f, [key]: value }));

  const canSave = form.name.trim() && form.source_hostname.trim() && !busy;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await fetchJSON("/api/vms", {
        method: "POST",
        body: {
          name: form.name.trim(),
          source_hostname: form.source_hostname.trim(),
          source_vcenter_id: form.source_vcenter_id
            ? Number(form.source_vcenter_id)
            : null,
          environment: form.environment || null,
          vsphere_networks: splitList(form.vsphere_networks),
          vsphere_datastores: splitList(form.vsphere_datastores),
        },
      });
      toast.success(`Added ${created?.name ?? form.name.trim()}`);
      onCreated?.(created);
      onClose?.();
    } catch (e) {
      // 409 (duplicate name) is the common one and worth showing inline
      // rather than as a toast that disappears while the field is focused.
      setError(e?.message ?? "Could not add the VM");
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={busy ? undefined : onClose} variant="medium">
      <ModalHeader
        title="Add a VM"
        description="For one-off entries and testing resource mappings. Use Import VMs for a whole estate."
      />
      <ModalBody>
        {error && (
          <HelperText className="pf-v6-u-mb-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}
        <Form>
          <Grid hasGutter>
            <GridItem span={6}>
              <FormGroup label="Name" isRequired fieldId="vm-name">
                <TextInput
                  id="vm-name"
                  value={form.name}
                  onChange={set("name")}
                  isDisabled={busy}
                />
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>Must be unique across inventory.</HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>
            <GridItem span={6}>
              <FormGroup label="Source hostname" isRequired fieldId="vm-host">
                <TextInput
                  id="vm-host"
                  value={form.source_hostname}
                  onChange={set("source_hostname")}
                  isDisabled={busy}
                />
              </FormGroup>
            </GridItem>

            <GridItem span={6}>
              <FormGroup label="Source vCenter" fieldId="vm-vcenter">
                <FormSelect
                  id="vm-vcenter"
                  value={form.source_vcenter_id}
                  onChange={set("source_vcenter_id")}
                  isDisabled={busy}
                >
                  <FormSelectOption value="" label="Not assigned" />
                  {vcenters.map((v) => (
                    <FormSelectOption key={v?.id} value={String(v?.id)} label={v?.name ?? "—"} />
                  ))}
                </FormSelect>
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>
                      Resource mappings are matched to VMs by vCenter. Without one, this VM
                      resolves against every selected mapping.
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>
            <GridItem span={6}>
              <FormGroup label="Environment" fieldId="vm-env">
                <FormSelect
                  id="vm-env"
                  value={form.environment}
                  onChange={set("environment")}
                  isDisabled={busy}
                >
                  <FormSelectOption value="" label="Detect automatically" />
                  {ENVIRONMENTS.map((e) => (
                    <FormSelectOption key={e} value={e} label={e} />
                  ))}
                </FormSelect>
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>
                      Plans never mix environments. Left automatic, it is inferred from the name.
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>

            <GridItem span={6}>
              <FormGroup label="vSphere networks" fieldId="vm-nets">
                <TextInput
                  id="vm-nets"
                  value={form.vsphere_networks}
                  onChange={set("vsphere_networks")}
                  isDisabled={busy}
                  placeholder="VM Network, DMZ-VLAN-20"
                />
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>
                      Comma-separated. These are the source names a mapping&apos;s network rows
                      are keyed on.
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>
            <GridItem span={6}>
              <FormGroup label="vSphere datastores" fieldId="vm-ds">
                <TextInput
                  id="vm-ds"
                  value={form.vsphere_datastores}
                  onChange={set("vsphere_datastores")}
                  isDisabled={busy}
                  placeholder="tier1-ssd, tier2-sas"
                />
                <FormHelperText>
                  <HelperText>
                    <HelperTextItem>Comma-separated.</HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </GridItem>
          </Grid>
        </Form>
      </ModalBody>
      <ModalFooter>
        <Button variant="primary" onClick={save} isDisabled={!canSave} isLoading={busy}>
          Add VM
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}
