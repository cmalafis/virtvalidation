// Shared confirmation dialog.
//
// Replaces ~8 hand-rolled variants (ConfirmModal in Settings,
// DeleteVMModal / BulkDeleteVMsModal / RevertToVmwareModal /
// MakeAvailableModal in the old dashboard, ConfirmDeleteModal in
// ResourceMappings, DeleteAllModal in the inventory table,
// RevokeKeyModal in WaveRunsPanel), which had drifted in button order,
// wording and whether they showed the error at all.
//
// `requireTyped` reproduces the type-the-name-to-confirm gate the
// destructive ones used — worth keeping for irreversible bulk actions.

import { useEffect, useState } from "react";
import {
  Button,
  Content,
  HelperText,
  HelperTextItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextInput,
} from "@patternfly/react-core";

export default function ConfirmModal({
  isOpen,
  title,
  children,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  isDanger = false,
  requireTyped = null,
  onConfirm,
  onClose,
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    if (isOpen) {
      setError(null);
      setTyped("");
      setBusy(false);
    }
  }, [isOpen]);

  const gateSatisfied = !requireTyped || typed === requireTyped;

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
    } catch (e) {
      // Keep the dialog open on failure — closing it would leave the
      // operator unsure whether the action took effect.
      setError(e?.message ?? "Action failed");
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={busy ? undefined : onClose} variant="small" aria-label={title}>
      <ModalHeader title={title} titleIconVariant={isDanger ? "warning" : undefined} />
      <ModalBody>
        <Content component="p">{children}</Content>

        {requireTyped && (
          <div className="pf-v6-u-mt-md">
            <Content component="p" className="pf-v6-u-font-size-sm">
              Type <strong>{requireTyped}</strong> to confirm.
            </Content>
            <TextInput
              aria-label="Confirmation text"
              value={typed}
              onChange={(_e, v) => setTyped(v)}
              className="pf-v6-u-mt-sm"
            />
          </div>
        )}

        {error && (
          <HelperText className="pf-v6-u-mt-md">
            <HelperTextItem variant="error">{error}</HelperTextItem>
          </HelperText>
        )}
      </ModalBody>
      <ModalFooter>
        <Button
          variant={isDanger ? "danger" : "primary"}
          onClick={confirm}
          isLoading={busy}
          isDisabled={busy || !gateSatisfied}
        >
          {confirmLabel}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          {cancelLabel}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
