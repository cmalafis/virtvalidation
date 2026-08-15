// Migration plan detail — the waves and how to run them.
//
// A wave is the unit of work: one MTV Plan CR, one baseline run, one
// validation run. Each wave card carries its LLM rationale, its risk
// assessment, and the execution panel.
//
// The `method` field on each wave is surfaced rather than hidden. Per
// CLAUDE.md every LLM feature records whether the result came from the
// model, a retry, or the deterministic fallback — an operator reading a
// wave rationale should know which they're looking at.

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Label,
  LabelGroup,
  PageSection,
  Skeleton,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import ConfirmModal from "../common/ConfirmModal";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import WaveRunPanel from "./plan/WaveRunPanel";

// How a wave's provenance should read. The auth and guardrail fallbacks
// are deliberately distinct from a plain one: they mean a key was
// rejected or a detector fired, not that the model had an off day.
const METHOD_META = {
  llm: { color: "green", label: "LLM" },
  mechanical_fallback: { color: "orange", label: "Deterministic fallback" },
  mechanical_fallback_auth: { color: "red", label: "Fallback — LLM auth rejected" },
  mechanical_fallback_guardrail: { color: "red", label: "Fallback — guardrail fired" },
};

function methodLabel(method) {
  if (!method) return null;
  const meta =
    METHOD_META[method] ??
    (String(method).startsWith("llm_retry")
      ? { color: "orange", label: `LLM (${method.replace("llm_retry_", "retry ")})` }
      : { color: "grey", label: method });
  return (
    <Label isCompact color={meta.color}>
      {meta.label}
    </Label>
  );
}

function riskColor(score) {
  if (score == null) return "grey";
  if (score >= 7) return "red";
  if (score >= 4) return "orange";
  return "green";
}

function WaveCard({ plan, wave }) {
  const number = wave?.wave_number ?? wave?.wave ?? 0;
  const vmIds = wave?.vm_ids ?? [];
  const concerns = wave?.notable_concerns ?? [];

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

  return (
    <Card className="pf-v6-u-mb-lg">
      <CardHeader>
        <Flex
          justifyContent={{ default: "justifyContentSpaceBetween" }}
          alignItems={{ default: "alignItemsCenter" }}
          flexWrap={{ default: "wrap" }}
          style={{ width: "100%" }}
        >
          <FlexItem>
            <CardTitle>{`Wave ${number}`}</CardTitle>
          </FlexItem>
          <FlexItem>
            <LabelGroup numLabels={4}>
              <Label isCompact>{`${vmIds.length} VM${vmIds.length === 1 ? "" : "s"}`}</Label>
              {wave?.risk_score != null && (
                <Label isCompact color={riskColor(wave.risk_score)}>
                  {`Risk ${wave.risk_score}/10`}
                </Label>
              )}
              {wave?.concurrency_group_id != null && (
                <Label isCompact color="blue">
                  {`Parallel group ${wave.concurrency_group_id}`}
                </Label>
              )}
              {methodLabel(wave?.method)}
            </LabelGroup>
          </FlexItem>
        </Flex>
      </CardHeader>

      <CardBody>
        {wave?.description && <Content component="p">{wave.description}</Content>}

        {wave?.risk_rationale && (
          <div className="pf-v6-u-mt-md">
            <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-font-size-sm">
              Risk rationale
            </Content>
            <Content component="p">{wave.risk_rationale}</Content>
          </div>
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

        <div className="pf-v6-u-mt-md">
          <Button variant="tertiary" onClick={downloadYaml}>
            Download MTV YAML
          </Button>
        </div>

        <div className="pf-v6-u-mt-md">
          <WaveRunPanel planId={plan.id} waveNumber={number} />
        </div>
      </CardBody>
    </Card>
  );
}

export default function PlanDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [marking, setMarking] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON(`/api/plans/${id}`);
      setPlan(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // Poll while generation is still running.
  const inFlight = plan && !["complete", "failed", "migrated"].includes(plan.status);
  useEffect(() => {
    if (!inFlight) return undefined;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [inFlight, load]);

  const markSucceeded = async () => {
    setMarking(true);
    try {
      await fetchJSON(`/api/plans/${id}/mark-succeeded`, { method: "POST" });
      toast.success("Plan marked as succeeded");
      load();
    } catch (e) {
      toast.error(e?.message ?? "Could not mark the plan");
    } finally {
      setMarking(false);
    }
  };

  const remove = async () => {
    await fetchJSON(`/api/plans/${id}`, { method: "DELETE" });
    toast.success("Plan deleted — its VMs are available again");
    navigate("/plans");
  };

  const title = plan?.name || `Plan #${id}`;
  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Migrate" },
      { label: "Migration plans", to: "/plans" },
      { label: title },
    ],
  };

  if (error) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
        </PageSection>
      </PageFrame>
    );
  }

  if (loading && !plan) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="40%" className="pf-v6-u-mb-md" />
          <Skeleton width="100%" height="200px" />
        </PageSection>
      </PageFrame>
    );
  }

  const waves = plan?.waves ?? [];

  return (
    <PageFrame
      {...frameProps}
      description={plan?.summary || undefined}
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <Button
              variant="secondary"
              component="a"
              href={`/api/plans/${id}/yaml`}
              download
            >
              Download all YAML
            </Button>
          </FlexItem>
          <FlexItem>
            <Button
              variant="secondary"
              onClick={markSucceeded}
              isDisabled={marking || plan?.status === "migrated"}
              isLoading={marking}
            >
              Mark succeeded
            </Button>
          </FlexItem>
          <FlexItem>
            <Button variant="link" isDanger onClick={() => setConfirmDelete(true)}>
              Delete plan
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      {plan?.status === "failed" && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="danger" isInline title="Plan generation failed">
            {plan?.error_message ?? "No detail was recorded."}
          </Alert>
        </PageSection>
      )}

      {inFlight && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="info" isInline title={`Generating — ${plan.status}`}>
            Waves appear as the pipeline completes each stage.
          </Alert>
        </PageSection>
      )}

      <PageSection hasBodyWrapper={false}>
        <Card>
          <CardBody>
            <DescriptionList isCompact isHorizontal columnModifier={{ default: "3Col" }}>
              <DescriptionListGroup>
                <DescriptionListTerm>Waves</DescriptionListTerm>
                <DescriptionListDescription>{waves.length}</DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>VMs</DescriptionListTerm>
                <DescriptionListDescription>
                  {(plan?.vm_ids ?? []).length}
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Model</DescriptionListTerm>
                <DescriptionListDescription>{plan?.model ?? "—"}</DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Created</DescriptionListTerm>
                <DescriptionListDescription>
                  {plan?.created_at ? new Date(plan.created_at).toLocaleString() : "—"}
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Mappings</DescriptionListTerm>
                <DescriptionListDescription>
                  {(plan?.mapping_ids ?? []).length === 0
                    ? "—"
                    : plan.mapping_ids.map((mid) => (
                        <Link key={mid} to={`/mappings/${mid}`} className="pf-v6-u-mr-sm">
                          {`#${mid}`}
                        </Link>
                      ))}
                </DescriptionListDescription>
              </DescriptionListGroup>
            </DescriptionList>
          </CardBody>
        </Card>
      </PageSection>

      <PageSection hasBodyWrapper={false}>
        {waves.length === 0 ? (
          <Content component="p" className="pf-v6-u-color-200">
            {inFlight ? "No waves yet." : "This plan has no waves."}
          </Content>
        ) : (
          waves.map((wave, i) => (
            <WaveCard key={wave?.wave_number ?? i} plan={plan} wave={wave} />
          ))
        )}
      </PageSection>

      <ConfirmModal
        isOpen={confirmDelete}
        title="Delete this plan?"
        confirmLabel="Delete plan"
        isDanger
        onConfirm={remove}
        onClose={() => setConfirmDelete(false)}
      >
        Deleting the plan returns its VMs to the available state so they can
        be included in a new plan. The generated YAML is not deleted from
        anywhere you have already applied it.
      </ConfirmModal>
    </PageFrame>
  );
}
