// Per-wave SSH key catalog.
//
// The appliance SSHes into production, so keys are scoped and disposable
// rather than one permanent key with standing access: create a key for a
// wave, distribute it, run the wave, revoke it. This panel is the
// catalog; the private key never leaves the appliance.

import { useCallback, useEffect, useState } from "react";
import toast from "react-hot-toast";
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import {
  Alert,
  Bullseye,
  Button,
  ClipboardCopy,
  ClipboardCopyVariant,
  Content,
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
  Skeleton,
  TextInput,
} from "@patternfly/react-core";

import StatusLabel from "../../common/StatusLabel";
import ConfirmModal from "../../common/ConfirmModal";
import { ErrorEmptyState, GuidedEmptyState } from "../../common/EmptyStates";
import { fetchJSON } from "../../utils/fetchJSON";
import { asArray } from "../../utils/asArray";

function CreateKeyModal({ isOpen, plans, fipsMode, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [planId, setPlanId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [created, setCreated] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    setName("");
    setPlanId("");
    setError(null);
    setCreated(null);
    setBusy(false);
  }, [isOpen]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const key = await fetchJSON("/api/ssh-keys", {
        method: "POST",
        body: {
          name: name.trim(),
          ...(planId ? { plan_id: Number(planId) } : {}),
        },
      });
      // Keep the dialog open on success: the public key needs
      // distributing, and closing here would make the operator hunt for
      // it afterwards.
      setCreated(key);
      onCreated();
      toast.success("SSH key created");
    } catch (e) {
      setError(e?.message ?? "Could not create the key");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} variant="medium">
      <ModalHeader
        title={created ? "Distribute this key" : "Create an SSH key"}
        description={
          created
            ? "Add the public key to ~/.ssh/authorized_keys on every VM in the wave. The private key stays on the appliance and is never shown."
            : "Scope a key to one wave so it can be revoked when that wave is done."
        }
      />
      <ModalBody>
        {error && (
          <Alert variant="danger" isInline title="Could not create" className="pf-v6-u-mb-md">
            {error}
          </Alert>
        )}

        {created ? (
          <>
            <ClipboardCopy
              isReadOnly
              isCode
              variant={ClipboardCopyVariant.expansion}
              hoverTip="Copy"
              clickTip="Copied"
            >
              {created.public_key}
            </ClipboardCopy>
            <Content component="small" className="pf-v6-u-color-200 pf-v6-u-mt-md pf-v6-u-display-block">
              Fingerprint: {created.fingerprint}
            </Content>
            <Content component="p" className="pf-v6-u-mt-md">
              An Ansible playbook that distributes this key is available at{" "}
              <code>/api/ssh-keys/{created.id}/playbook</code>.
            </Content>
          </>
        ) : (
          <Form>
            <FormGroup label="Key name" isRequired fieldId="key-name">
              <TextInput
                id="key-name"
                value={name}
                onChange={(_e, v) => setName(v)}
                isRequired
                placeholder="payments-wave-1"
              />
            </FormGroup>

            <FormGroup label="Scope to a plan" fieldId="key-plan">
              <FormSelect
                id="key-plan"
                value={planId}
                onChange={(_e, v) => setPlanId(v)}
                aria-label="Plan"
              >
                <FormSelectOption value="" label="Not scoped to a plan" />
                {(plans ?? []).map((p) => (
                  <FormSelectOption
                    key={p.id}
                    value={String(p.id)}
                    label={p.name || `Plan #${p.id}`}
                  />
                ))}
              </FormSelect>
              <FormHelperText>
                <HelperText>
                  <HelperTextItem>
                    A scoped key can be revoked from the wave&apos;s VMs in one
                    action once the wave is validated.
                  </HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>

            {fipsMode && (
              <Alert variant="info" isInline title="FIPS mode is active">
                Keys are generated with a FIPS-approved algorithm.
              </Alert>
            )}
          </Form>
        )}
      </ModalBody>
      <ModalFooter>
        {created ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button
              variant="primary"
              onClick={submit}
              isDisabled={!name.trim() || busy}
              isLoading={busy}
            >
              Create key
            </Button>
            <Button variant="link" onClick={onClose} isDisabled={busy}>
              Cancel
            </Button>
          </>
        )}
      </ModalFooter>
    </Modal>
  );
}

export default function ValidationKeys({ fipsMode }) {
  const [keys, setKeys] = useState([]);
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [retiring, setRetiring] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [k, p] = await Promise.allSettled([
        fetchJSON("/api/ssh-keys?limit=200"),
        fetchJSON("/api/plans?limit=200"),
      ]);
      if (k.status === "rejected") throw k.reason;
      const kv = k.value;
      setKeys(asArray(kv));
      setPlans(p.status === "fulfilled" && Array.isArray(p.value) ? p.value : []);
      setError(null);
    } catch (e) {
      setError(e);
      setKeys([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const retire = async () => {
    await fetchJSON(`/api/ssh-keys/${retiring.id}/retire`, { method: "POST" });
    toast.success(`Retired ${retiring.name}`);
    setRetiring(null);
    load();
  };

  if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;
  if (loading && keys.length === 0) return <Skeleton width="100%" height="120px" />;

  return (
    <>
      <div className="pf-v6-u-mb-md">
        <Button variant="secondary" onClick={() => setCreateOpen(true)}>
          Create SSH key
        </Button>
      </div>

      {keys.length === 0 ? (
        <Bullseye>
          <GuidedEmptyState
            title="No SSH keys yet"
            body="VirtValidate authenticates to managed VMs with Ed25519 keys it generates and holds. Create one per wave so access can be revoked when the wave is done."
            primary={{ label: "Create SSH key", onClick: () => setCreateOpen(true) }}
          />
        </Bullseye>
      ) : (
        <Table aria-label="SSH keys" variant="compact">
          <Thead>
            <Tr>
              <Th width={25}>Name</Th>
              <Th width={15}>Status</Th>
              <Th width={15}>Algorithm</Th>
              <Th width={30}>Fingerprint</Th>
              <Th width={15}>Created</Th>
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>
            {keys.map((k) => (
              <Tr key={k.id}>
                <Td dataLabel="Name">{k.name}</Td>
                <Td dataLabel="Status">
                  <StatusLabel
                    kind="status"
                    value={k.status === "active" ? "healthy" : "pending"}
                  >
                    {k.status}
                  </StatusLabel>
                </Td>
                <Td dataLabel="Algorithm">{k.algorithm}</Td>
                <Td dataLabel="Fingerprint">
                  <code className="pf-v6-u-font-size-sm">{k.fingerprint}</code>
                </Td>
                <Td dataLabel="Created">
                  {k?.created_at ? new Date(k.created_at).toLocaleDateString() : "—"}
                </Td>
                <Td isActionCell>
                  <ActionsColumn
                    items={[
                      {
                        title: "Retire",
                        isDisabled: k.status !== "active",
                        onClick: () => setRetiring(k),
                      },
                    ]}
                  />
                </Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      <CreateKeyModal
        isOpen={createOpen}
        plans={plans}
        fipsMode={fipsMode}
        onClose={() => setCreateOpen(false)}
        onCreated={load}
      />

      <ConfirmModal
        isOpen={Boolean(retiring)}
        title="Retire this key?"
        confirmLabel="Retire"
        onConfirm={retire}
        onClose={() => setRetiring(null)}
      >
        {`${retiring?.name} will no longer be usable for new runs. Retiring does NOT remove it from any VM's authorized_keys — use the revoke action on a wave for that.`}
      </ConfirmModal>
    </>
  );
}
