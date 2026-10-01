// Move the selected VMs to another wave or another plan, or remove them
// from the plan.
//
// The operator knows dependencies the estate data does not show, so this
// dialog never second-guesses a move — it shows what the planner would
// say about it and lets the operator decide:
//
//   1. pick a target
//   2. the backend dry-runs the edit and reports which rules it breaks
//   3. a blocking rule disables the button; an overridable one needs an
//      explicit acknowledgement, which is recorded in the audit log
//
// Three modes, one flow: "wave" (same plan), "plan" (another plan in the
// same vCenter / cluster / namespace), "remove".

import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Content,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  List,
  ListItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Skeleton,
  TextInput,
} from "@patternfly/react-core";

import { asArray } from "../../utils/asArray";
import { fetchJSON } from "../../utils/fetchJSON";
import EditViolations from "./EditViolations";
import { commitEdit, hasSeverity, useEditPreview, violationsFromError } from "./usePlanEdit";

const NEW_WAVE = "new";
const NAMES_SHOWN = 8;

const TITLES = {
  wave: "Move to another wave",
  plan: "Move to another plan",
  remove: "Remove from this plan",
};

function waveOptionLabel(w) {
  const count = w?.vm_count ?? 0;
  return `Wave ${w?.wave_number} — ${count} VM${count === 1 ? "" : "s"}`;
}

function planOptionLabel(t) {
  const batch =
    (t?.series_total ?? 1) > 1 ? ` · batch ${t?.series_index ?? "?"} of ${t.series_total}` : "";
  return `${t?.name ?? `Plan #${t?.plan_id}`}${batch} — ${t?.vm_count ?? 0} VMs${
    t?.same_series ? " (same series)" : ""
  }`;
}

/**
 * @param mode          "wave" | "plan" | "remove"
 * @param plan          the plan being edited
 * @param waves         [{wave_number, vm_count}] of this plan
 * @param rows          the selected VM rows
 * @param initialTarget preselected wave target ("new" for New wave from selection)
 * @param onApplied     (response, {targetPlanName}) => void
 */
export default function PlanEditModal({
  isOpen,
  mode,
  plan,
  waves,
  rows,
  initialTarget,
  onClose,
  onApplied,
}) {
  const [targetWave, setTargetWave] = useState("");
  const [targetPlanId, setTargetPlanId] = useState("");
  const [targets, setTargets] = useState(null);
  const [targetsError, setTargetsError] = useState(null);
  const [note, setNote] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [commitError, setCommitError] = useState(null);

  const selectedRows = asArray(rows);
  const vmIds = useMemo(() => selectedRows.map((r) => r.vm_id), [selectedRows]);
  const planId = plan?.id;

  // Reset every time the dialog opens: a stale target from the last use
  // would dry-run an edit the operator did not ask for.
  useEffect(() => {
    if (!isOpen) return;
    setTargetWave(initialTarget ?? "");
    setTargetPlanId("");
    setTargets(null);
    setTargetsError(null);
    setNote("");
    setAcknowledged(false);
    setBusy(false);
    setCommitError(null);
  }, [isOpen, initialTarget, mode]);

  useEffect(() => {
    if (!isOpen || mode !== "plan" || !planId) return undefined;
    let cancelled = false;
    fetchJSON(`/api/plans/${planId}/edit-targets`)
      .then((data) => {
        if (!cancelled) setTargets(data ?? {});
      })
      .catch((e) => {
        if (!cancelled) setTargetsError(e);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, mode, planId]);

  const targetPlans = asArray(targets?.targets);
  const ineligible = asArray(targets?.ineligible);
  const targetPlan = targetPlans.find((t) => String(t?.plan_id) === targetPlanId) ?? null;
  const waveChoices = mode === "plan" ? asArray(targetPlan?.waves) : asArray(waves);

  // The edit as the API takes it, or null until a target is chosen.
  const body = useMemo(() => {
    if (!isOpen || vmIds.length === 0) return null;
    if (mode === "remove") return { operation: "remove", vm_ids: vmIds };
    if (!targetWave) return null;
    const wave = targetWave === NEW_WAVE ? NEW_WAVE : Number(targetWave);
    if (mode === "plan") {
      if (!targetPlanId) return null;
      return {
        operation: "move_to_plan",
        vm_ids: vmIds,
        target_plan_id: Number(targetPlanId),
        target_wave: wave,
      };
    }
    return { operation: "move_to_wave", vm_ids: vmIds, target_wave: wave };
  }, [isOpen, mode, vmIds, targetWave, targetPlanId]);

  const preview = useEditPreview(planId, body);
  const result = preview.result;
  const isBlocked = hasSeverity(result, "blocking");
  const needsAck = hasSeverity(result, "overridable");

  // A new dry-run result is a new thing to acknowledge.
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
      onApplied?.(response, { targetPlanName: targetPlan?.name ?? null });
    } catch (e) {
      // Keep the dialog open — closing it would leave the operator
      // unsure whether the change took effect.
      setCommitError(e);
      setBusy(false);
    }
  };

  const count = selectedRows.length;
  const moves = asArray(result?.moves);
  const confirmLabel = mode === "remove" ? "Remove from plan" : `Move ${count} VM${count === 1 ? "" : "s"}`;

  return (
    <Modal
      isOpen={isOpen}
      onClose={busy ? undefined : onClose}
      variant="medium"
      aria-label={TITLES[mode] ?? "Edit plan"}
    >
      <ModalHeader
        title={TITLES[mode] ?? "Edit plan"}
        description={`${count} VM${count === 1 ? "" : "s"} selected in ${plan?.name ?? "this plan"}`}
      />
      <ModalBody>
        <Content component="p" className="pf-v6-u-color-200">
          {selectedRows
            .slice(0, NAMES_SHOWN)
            .map((r) => r.name)
            .join(", ")}
          {count > NAMES_SHOWN ? ` and ${count - NAMES_SHOWN} more` : ""}
        </Content>

        {mode === "remove" && (
          <Content component="p" className="pf-v6-u-mt-md">
            The VMs leave this plan and return to the available inventory, where they can be
            added to this or another plan later. Nothing is changed on the VMs themselves.
          </Content>
        )}

        <Form className="pf-v6-u-mt-md" onSubmit={(e) => e.preventDefault()}>
          {mode === "plan" && (
            <FormGroup label="Destination plan" fieldId="edit-target-plan" isRequired>
              {targetsError ? (
                <Alert variant="danger" isInline title="Could not load the plans">
                  {targetsError?.message}
                </Alert>
              ) : targets === null ? (
                <Skeleton screenreaderText="Loading plans" />
              ) : targetPlans.length === 0 ? (
                <Alert variant="info" isInline title="No other plan can take these VMs">
                  A wave is one MTV Plan, so VMs can only move between plans that migrate from
                  the same vCenter to the same cluster and namespace.
                  {ineligible.length > 0 && (
                    <List className="pf-v6-u-mt-sm">
                      {ineligible.slice(0, 6).map((p) => (
                        <ListItem key={p?.plan_id}>
                          <strong>{p?.name}</strong> — {p?.reason}
                        </ListItem>
                      ))}
                    </List>
                  )}
                </Alert>
              ) : (
                <>
                  <FormSelect
                    id="edit-target-plan"
                    aria-label="Destination plan"
                    value={targetPlanId}
                    onChange={(_e, v) => {
                      setTargetPlanId(v);
                      setTargetWave("");
                    }}
                  >
                    <FormSelectOption value="" label="Choose a plan" isPlaceholder />
                    {targetPlans.map((t) => (
                      <FormSelectOption
                        key={t?.plan_id}
                        value={String(t?.plan_id)}
                        label={planOptionLabel(t)}
                      />
                    ))}
                  </FormSelect>
                  {ineligible.length > 0 && (
                    <FormHelperText>
                      <HelperText>
                        <HelperTextItem>
                          {`${ineligible.length} other plan${
                            ineligible.length === 1 ? " is" : "s are"
                          } not listed: they migrate to a different vCenter, cluster or namespace, or are not ready.`}
                        </HelperTextItem>
                      </HelperText>
                    </FormHelperText>
                  )}
                </>
              )}
            </FormGroup>
          )}

          {mode !== "remove" && (mode === "wave" || targetPlan) && (
            <FormGroup label="Destination wave" fieldId="edit-target-wave" isRequired>
              <FormSelect
                id="edit-target-wave"
                aria-label="Destination wave"
                value={targetWave}
                onChange={(_e, v) => setTargetWave(v)}
              >
                <FormSelectOption value="" label="Choose a wave" isPlaceholder />
                {waveChoices.map((w) => (
                  <FormSelectOption
                    key={w?.wave_number}
                    value={String(w?.wave_number)}
                    label={waveOptionLabel(w)}
                  />
                ))}
                <FormSelectOption value={NEW_WAVE} label="New wave (added after the last one)" />
              </FormSelect>
            </FormGroup>
          )}

          <FormGroup label="Reason (optional)" fieldId="edit-note">
            <TextInput
              id="edit-note"
              aria-label="Reason for this change"
              value={note}
              maxLength={500}
              onChange={(_e, v) => setNote(v)}
              placeholder="e.g. shares a license server with the billing tier"
            />
            <FormHelperText>
              <HelperText>
                <HelperTextItem>
                  Recorded in the audit log and given to the LLM when it re-explains the wave.
                </HelperTextItem>
              </HelperText>
            </FormHelperText>
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

        {result && !isBlocked && moves.length > 0 && moves.length < count && (
          <Alert
            variant="info"
            isInline
            isPlain
            title={`${count - moves.length} of the selected VMs are already in that wave and stay where they are.`}
            className="pf-v6-u-mt-md"
          />
        )}

        <EditViolations violations={result?.violations} />

        {result && needsAck && !isBlocked && (
          <Checkbox
            id="edit-acknowledge"
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
        <Button
          variant={mode === "remove" ? "danger" : "primary"}
          onClick={confirm}
          isLoading={busy}
          isDisabled={!canConfirm}
        >
          {confirmLabel}
        </Button>
        <Button variant="link" onClick={onClose} isDisabled={busy}>
          Cancel
        </Button>
      </ModalFooter>
    </Modal>
  );
}
