// One wave of a migration plan.
//
// A wave is the unit of work: one MTV Plan CR, one baseline run, one
// validation run. Collapsed, the header answers "what is in this wave" —
// VM count, the first few names, risk, and whether an operator has
// rearranged it. Expanded, it lists every VM (selectable, for moving)
// and carries the rationale, the YAML download and the execution panel.
//
// The body mounts only when expanded. A 100-wave plan used to fetch 100
// previews on load; now it fetches the ones the operator opens.
//
// The `method` label is surfaced rather than hidden. Per CLAUDE.md every
// LLM feature records whether the text came from the model, a retry, or
// the deterministic fallback — an operator reading a wave rationale
// should know which they're looking at.

import { useState } from "react";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardExpandableContent,
  CardHeader,
  CardTitle,
  Content,
  Flex,
  FlexItem,
  Label,
  LabelGroup,
  List,
  ListItem,
} from "@patternfly/react-core";

import { asArray } from "../../utils/asArray";
import { fetchJSON } from "../../utils/fetchJSON";
import PlanVMTable from "./PlanVMTable";
import WaveRunPanel from "./WaveRunPanel";
import { methodMeta, riskColor } from "./planRows";

const PREVIEW_NAMES = 4;

function overrideText(o) {
  if (o?.code === "wave_size") {
    return `Holds ${o?.count ?? "?"} VMs — the planner keeps waves to ${o?.limit ?? "?"}.`;
  }
  if (o?.code === "ha_family_cap") {
    return `${o?.count ?? "?"} VMs of the '${o?.family ?? "?"}' family share this wave — the planner allows ${o?.limit ?? "?"}.`;
  }
  return `Planner rule '${o?.code ?? "?"}' was overridden.`;
}

/**
 * @param rows        every VM row of this wave
 * @param visibleRows the rows to list (narrowed by the page's search)
 * @param waveState   this wave's entry from GET /api/plans/{id}/vms
 * @param isExpanded  operator opened the wave — show everything
 * @param isMatchOnly search hit in a wave the operator has not opened —
 *                    show only the matching VMs, not the run panel
 */
export default function WaveSection({
  plan,
  wave,
  number,
  rows,
  visibleRows,
  waveState,
  isExpanded,
  isMatchOnly,
  onToggleExpand,
  selected,
  onToggle,
  onToggleMany,
  canEdit,
  highlightId,
  onChanged,
}) {
  const [asking, setAsking] = useState(false);

  const all = asArray(rows);
  const shown = asArray(visibleRows);
  const concerns = asArray(wave?.notable_concerns);
  // Deterministic migratability findings rolled up across this wave's VMs
  // (not LLM output) — what MTV will flag when this plan is created.
  const considerations = asArray(wave?.considerations);
  const overrides = asArray(wave?.overrides);
  const baselineMissing = asArray(waveState?.baseline?.missing_vm_ids);
  const method = methodMeta(wave?.method);
  const selectedHere = all.filter((r) => selected?.has(r.vm_id)).length;
  const isOpen = isExpanded || isMatchOnly;

  const downloadYaml = async () => {
    try {
      const res = await fetch(`/api/plans/${plan.id}/waves/${number}/mtv-yaml`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const text = await res.text();
      const blob = new Blob([text], { type: "application/yaml" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `plan-${plan.id}-wave-${number}.yaml`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`Could not download the YAML: ${e.message}`);
    }
  };

  const askForRationale = async () => {
    setAsking(true);
    try {
      await fetchJSON(`/api/plans/${plan.id}/waves/${number}/reannotate`, { method: "POST" });
      toast.success(`Asked the LLM to explain wave ${number}`);
      onChanged?.();
    } catch (e) {
      toast.error(e?.message ?? "Could not request a rationale");
    } finally {
      setAsking(false);
    }
  };

  const names = all.slice(0, PREVIEW_NAMES).map((r) => r.name);
  const more = all.length - names.length;
  // The model's text is absent: offer to ask again. Not while a pass is
  // already queued, and not for a plan that can no longer change.
  const canAsk =
    canEdit && String(wave?.method ?? "").startsWith("mechanical_fallback");

  return (
    <Card isCompact isExpanded={isOpen} className="pf-v6-u-mb-md" id={`wave-${number}`}>
      <CardHeader
        onExpand={() => onToggleExpand?.(number)}
        toggleButtonProps={{
          "aria-label": `Wave ${number} details`,
          "aria-expanded": isOpen,
        }}
      >
        <Flex
          alignItems={{ default: "alignItemsCenter" }}
          spaceItems={{ default: "spaceItemsMd" }}
          flexWrap={{ default: "wrap" }}
        >
          <FlexItem>
            <CardTitle component="h3">{`Wave ${number}`}</CardTitle>
          </FlexItem>
          <FlexItem>
            <LabelGroup numLabels={8}>
              <Label isCompact>{`${all.length} VM${all.length === 1 ? "" : "s"}`}</Label>
              {selectedHere > 0 && (
                <Label isCompact color="blue">{`${selectedHere} selected`}</Label>
              )}
              {wave?.risk_score != null && (
                <Label isCompact color={riskColor(wave.risk_score)}>
                  {`Risk ${wave.risk_score}/5`}
                </Label>
              )}
              {wave?.concurrency_group_id != null && (
                <Label isCompact variant="outline">
                  {`Parallel group ${wave.concurrency_group_id}`}
                </Label>
              )}
              {method && (
                <Label isCompact color={method.color}>
                  {method.label}
                </Label>
              )}
              {wave?.manual_edit && (
                <Label isCompact color="teal">
                  Manually edited
                </Label>
              )}
              {overrides.length > 0 && (
                <Label isCompact color="orange">
                  Rule overridden
                </Label>
              )}
              {baselineMissing.length > 0 && (
                <Label isCompact color="orange">
                  Baseline out of date
                </Label>
              )}
            </LabelGroup>
          </FlexItem>
          {!isOpen && names.length > 0 && (
            <FlexItem>
              <Content component="small" className="pf-v6-u-color-200">
                {names.join(", ")}
                {more > 0 ? ` +${more} more` : ""}
              </Content>
            </FlexItem>
          )}
        </Flex>
      </CardHeader>

      <CardExpandableContent>
        <CardBody>
          {isExpanded && (
            <>
              {wave?.manual_edit && (
                <Alert
                  variant="info"
                  isInline
                  isPlain
                  title={`Arranged manually by ${wave.manual_edit?.edited_by ?? "an operator"}${
                    wave.manual_edit?.edited_at
                      ? ` on ${new Date(wave.manual_edit.edited_at).toLocaleString()}`
                      : ""
                  }`}
                  className="pf-v6-u-mb-md"
                >
                  {wave.manual_edit?.note ? `Note: ${wave.manual_edit.note}` : null}
                </Alert>
              )}

              {overrides.length > 0 && (
                <Alert
                  variant="warning"
                  isInline
                  title="An operator overrode a planner rule for this wave"
                  className="pf-v6-u-mt-md"
                >
                  <List>
                    {overrides.map((o, i) => (
                      <ListItem key={`${o?.code}-${o?.family ?? i}`}>
                        {overrideText(o)}
                        {o?.by ? ` Confirmed by ${o.by}.` : ""}
                      </ListItem>
                    ))}
                  </List>
                </Alert>
              )}

              {baselineMissing.length > 0 && (
                <Alert
                  variant="warning"
                  isInline
                  title={`${baselineMissing.length} VM${
                    baselineMissing.length === 1 ? " is" : "s are"
                  } not in this wave's latest baseline`}
                  className="pf-v6-u-mt-md"
                >
                  Validation compares each VM against that baseline and would report these as
                  unreachable. Capture the baseline again before validating.
                </Alert>
              )}
            </>
          )}

          <div className={isExpanded ? "pf-v6-u-mt-md" : undefined}>
            {shown.length === 0 ? (
              <Content component="p" className="pf-v6-u-color-200">
                No VMs in this wave match the search.
              </Content>
            ) : (
              <PlanVMTable
                rows={shown}
                selected={selected}
                onToggle={onToggle}
                onToggleMany={onToggleMany}
                canSelect={canEdit}
                highlightId={highlightId}
                ariaLabel={`Wave ${number} VMs`}
              />
            )}
          </div>

          {isExpanded && (
            <div className="pf-v6-u-mt-md">
              {wave?.description && <Content component="p">{wave.description}</Content>}

              {wave?.risk_rationale && (
                <div className="pf-v6-u-mt-md">
                  <Content
                    component="p"
                    className="pf-v6-u-font-weight-bold pf-v6-u-font-size-sm"
                  >
                    Risk rationale
                  </Content>
                  <Content component="p">{wave.risk_rationale}</Content>
                </div>
              )}

              {canAsk && (
                <Button
                  variant="link"
                  isInline
                  onClick={askForRationale}
                  isLoading={asking}
                  isDisabled={asking}
                  className="pf-v6-u-mt-sm"
                >
                  Ask the LLM for a rationale
                </Button>
              )}

              {considerations.length > 0 && (
                <Alert
                  variant={
                    considerations.some((c) => c?.category === "Critical") ? "danger" : "warning"
                  }
                  isInline
                  title="Before you start this wave"
                  className="pf-v6-u-mt-md"
                >
                  <ul>
                    {considerations.map((c) => (
                      <li key={c?.id}>
                        <strong>{c?.label}</strong> — {c?.vm_count ?? 0} VM(s):{" "}
                        {asArray(c?.vm_names).slice(0, 6).join(", ")}
                        {asArray(c?.vm_names).length > 6
                          ? ` (+${asArray(c?.vm_names).length - 6} more)`
                          : ""}
                      </li>
                    ))}
                  </ul>
                  <Content component="small">
                    From the migratability assessment. Open a VM for the fix; MTV reports the
                    same concerns when the plan is created on the cluster.
                  </Content>
                </Alert>
              )}

              {concerns.length > 0 && (
                <Alert
                  variant="warning"
                  isInline
                  isPlain
                  title="Notable concerns"
                  className="pf-v6-u-mt-md"
                >
                  <ul>
                    {concerns.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </Alert>
              )}
            </div>
          )}

          {isExpanded && (
            <>
              <div className="pf-v6-u-mt-md">
                <Button variant="tertiary" onClick={downloadYaml}>
                  Download MTV YAML
                </Button>
              </div>

              <div className="pf-v6-u-mt-md">
                {/* Keyed on membership: an edit changes which hosts a run
                    would touch, so the dry-run preview must be refetched. */}
                <WaveRunPanel
                  key={all.map((r) => r.vm_id).join(",")}
                  planId={plan.id}
                  waveNumber={number}
                />
              </div>
            </>
          )}
        </CardBody>
      </CardExpandableContent>
    </Card>
  );
}
