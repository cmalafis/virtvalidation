// What a proposed plan edit would break, grouped by what the operator
// can do about it.
//
//   blocking     the edit cannot be applied (a wave is one MTV Plan: one
//                vCenter, one cluster, one namespace)
//   overridable  a planner rule the operator may knowingly break
//   info         nothing to decide, but worth reading before confirming
//
// The list comes straight from the dry-run response of
// POST /api/plans/{id}/edits — the UI adds no rules of its own.

import { Alert, List, ListItem } from "@patternfly/react-core";

import { asArray } from "../../utils/asArray";

const GROUPS = [
  { severity: "blocking", variant: "danger", title: "This change cannot be applied" },
  { severity: "overridable", variant: "warning", title: "This overrides a planner rule" },
  { severity: "info", variant: "info", title: "Before you confirm" },
];

export default function EditViolations({ violations }) {
  const list = asArray(violations);
  if (list.length === 0) return null;

  return (
    <>
      {GROUPS.map(({ severity, variant, title }) => {
        const items = list.filter((v) => v?.severity === severity);
        if (items.length === 0) return null;
        return (
          <Alert key={severity} variant={variant} isInline title={title} className="pf-v6-u-mt-md">
            <List>
              {items.map((v, i) => (
                <ListItem key={`${v?.code ?? "rule"}-${i}`}>{v?.message ?? v?.code ?? "—"}</ListItem>
              ))}
            </List>
          </Alert>
        );
      })}
    </>
  );
}
