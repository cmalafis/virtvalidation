// Shared status / severity / lifecycle labels.
//
// Replaces four near-identical hand-rolled components that had drifted
// apart: StatusBadge + SeverityTag (VirtValidate), Pill (OCPTargets et
// al), StatusPill (Network/StorageReviewDetail), NetworkReviewStatusPill.
//
// All of them mapped a string to a hex color and drew a bordered span.
// PatternFly's Label already does that, and its `status`/`color` props
// carry the semantics through both themes — so the hex maps are gone.

import { Label } from "@patternfly/react-core";
import CheckCircleIcon from "@patternfly/react-icons/dist/esm/icons/check-circle-icon";
import ExclamationCircleIcon from "@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon";
import ExclamationTriangleIcon from "@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon";
import InProgressIcon from "@patternfly/react-icons/dist/esm/icons/in-progress-icon";
import InfoCircleIcon from "@patternfly/react-icons/dist/esm/icons/info-circle-icon";
import OutlinedClockIcon from "@patternfly/react-icons/dist/esm/icons/outlined-clock-icon";

// Validation verdicts (was STATUS_CONFIG).
const STATUS = {
  healthy: { color: "green", icon: CheckCircleIcon, label: "Healthy" },
  passed: { color: "green", icon: CheckCircleIcon, label: "Passed" },
  degraded: { color: "orange", icon: ExclamationTriangleIcon, label: "Degraded" },
  failed: { color: "red", icon: ExclamationCircleIcon, label: "Failed" },
  captured: { color: "blue", icon: CheckCircleIcon, label: "Captured" },
  running: { color: "blue", icon: InProgressIcon, label: "Running" },
  pending: { color: "grey", icon: OutlinedClockIcon, label: "Pending" },
};

// Finding severities (was SEVERITY_COLOR / SEVERITY_COLOR_DR).
const SEVERITY = {
  critical: { color: "red", icon: ExclamationCircleIcon },
  high: { color: "red", icon: ExclamationCircleIcon },
  warn: { color: "orange", icon: ExclamationTriangleIcon },
  medium: { color: "orange", icon: ExclamationTriangleIcon },
  low: { color: "blue", icon: InfoCircleIcon },
  info: { color: "blue", icon: InfoCircleIcon },
};

// VM plan-membership lifecycle (app.core.vm_lifecycle).
const LIFECYCLE = {
  available: { color: "green", label: "Available" },
  planned: { color: "blue", label: "Planned" },
  migrated: { color: "purple", label: "Migrated" },
  rolled_back: { color: "orange", label: "Rolled back" },
};

// Design-review + generic record states.
const RECORD = {
  complete: { color: "green", label: "Complete" },
  completed: { color: "green", label: "Complete" },
  analyzing: { color: "blue", label: "Analyzing" },
  draft: { color: "grey", label: "Draft" },
  error: { color: "red", label: "Error" },
  open: { color: "orange", label: "Open" },
  accepted: { color: "green", label: "Accepted" },
  dismissed: { color: "grey", label: "Dismissed" },
};

const TABLES = {
  status: STATUS,
  severity: SEVERITY,
  lifecycle: LIFECYCLE,
  record: RECORD,
};

/** Title-case an unknown value so it still reads as a label, not a slug. */
function humanize(value) {
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * One label for every status-ish string in the app.
 *
 * `kind` picks the lookup table. Unknown values degrade to a grey label
 * with a humanized version of the raw string rather than disappearing —
 * the backend accepts free-text in several of these fields.
 */
export default function StatusLabel({
  value,
  kind = "status",
  isCompact = true,
  children,
  ...rest
}) {
  if (value === null || value === undefined || value === "") {
    return (
      <Label color="grey" isCompact={isCompact} {...rest}>
        {children ?? "—"}
      </Label>
    );
  }

  const key = String(value).toLowerCase();
  const cfg = (TABLES[kind] ?? STATUS)[key];
  const Icon = cfg?.icon;

  return (
    <Label
      color={cfg?.color ?? "grey"}
      icon={Icon ? <Icon /> : undefined}
      isCompact={isCompact}
      {...rest}
    >
      {/* Callers may override the text while keeping the color/icon
          semantics — e.g. "Degraded: database" instead of "Degraded". */}
      {children ?? cfg?.label ?? humanize(value)}
    </Label>
  );
}
