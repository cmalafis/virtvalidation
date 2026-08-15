// Design review detail — one component for both kinds.
//
// Replaces NetworkReviewDetail (460) and StorageReviewDetail (473), which
// were near-verbatim copies differing only in endpoint and wording.
//
// Findings are grouped by triage state with open ones first, because the
// point of the page is working the open list down to zero.

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  Flex,
  FlexItem,
  Label,
  LabelGroup,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import FindingCard from "../common/FindingCard";
import { ErrorEmptyState, GuidedEmptyState, LoadingEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { REVIEW_KINDS } from "./DesignReviewNewPage";

const SEVERITY_RANK = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

export default function DesignReviewDetailPage({ kind }) {
  const meta = REVIEW_KINDS[kind];
  const { id } = useParams();

  const [review, setReview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("open");

  const load = useCallback(async () => {
    try {
      const data = await fetchJSON(`${meta.endpoint}/${id}`);
      setReview(data);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [meta.endpoint, id]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  // Poll while the analysis is running so results appear without a
  // manual refresh. Analysis typically takes 30-90s.
  const isAnalyzing = review?.status === "analyzing";
  useEffect(() => {
    if (!isAnalyzing) return undefined;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [isAnalyzing, load]);

  const analyze = async () => {
    setBusy(true);
    try {
      await fetchJSON(`${meta.endpoint}/${id}/analyze`, { method: "POST" });
      toast("Analysis started — usually 30–90 seconds", { icon: "🧠" });
      load();
    } catch (e) {
      toast.error(e?.message ?? "Could not start analysis");
    } finally {
      setBusy(false);
    }
  };

  const triage = async (findingId, value) => {
    setBusy(true);
    try {
      await fetchJSON(`${meta.endpoint}/${id}/findings/${findingId}`, {
        method: "PATCH",
        body: { triage: value },
      });
      // Update in place so the list doesn't jump while working through it.
      setReview((r) => ({
        ...r,
        findings: (r?.findings ?? []).map((f) =>
          f.id === findingId ? { ...f, triage: value } : f,
        ),
      }));
    } catch (e) {
      toast.error(e?.message ?? "Could not update finding");
    } finally {
      setBusy(false);
    }
  };

  const grouped = useMemo(() => {
    const all = [...(review?.findings ?? [])].sort(
      (a, b) =>
        (SEVERITY_RANK[a?.severity] ?? 9) - (SEVERITY_RANK[b?.severity] ?? 9),
    );
    return {
      open: all.filter((f) => f.triage === "open"),
      accepted: all.filter((f) => f.triage === "accepted"),
      dismissed: all.filter((f) => f.triage === "dismissed"),
      all,
    };
  }, [review]);

  const title = review?.name || `${meta.label} review`;

  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Discover" },
      { label: "Design reviews", to: "/design-reviews" },
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

  if (loading && !review) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="60%" className="pf-v6-u-mb-md" />
          <Skeleton width="100%" height="120px" />
        </PageSection>
      </PageFrame>
    );
  }

  const results = review?.analysis_results ?? {};

  const renderList = (list) =>
    list.length === 0 ? (
      <Content component="p" className="pf-v6-u-color-200 pf-v6-u-mt-md">
        Nothing here.
      </Content>
    ) : (
      list.map((f) => (
        <FindingCard key={f.id} finding={f} onTriage={triage} isBusy={busy} />
      ))
    );

  return (
    <PageFrame
      {...frameProps}
      description={`${meta.label} design review. Findings compare your proposed design against the source estate.`}
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <StatusLabel kind="record" value={review?.status} />
          </FlexItem>
          <FlexItem>
            <Button variant="secondary" onClick={analyze} isDisabled={busy || isAnalyzing}>
              {review?.last_analyzed_at ? "Re-run analysis" : "Run analysis"}
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      {review?.last_error && (
        <PageSection hasBodyWrapper={false}>
          <Alert variant="danger" isInline title="The last analysis failed">
            {review.last_error}
          </Alert>
        </PageSection>
      )}

      {isAnalyzing && (
        <PageSection hasBodyWrapper={false}>
          <Card>
            <CardBody>
              <LoadingEmptyState title="Analysis running — usually 30 to 90 seconds" />
            </CardBody>
          </Card>
        </PageSection>
      )}

      {results?.executive_summary && (
        <PageSection hasBodyWrapper={false}>
          <Card>
            <CardTitle>Summary</CardTitle>
            <CardBody>
              <Content component="p">{results.executive_summary}</Content>
              <LabelGroup className="pf-v6-u-mt-md">
                {results?.model && <Label isCompact>{`Model: ${results.model}`}</Label>}
                {review?.last_analyzed_at && (
                  <Label isCompact>
                    {`Analyzed ${new Date(review.last_analyzed_at).toLocaleString()}`}
                  </Label>
                )}
              </LabelGroup>
              {results?.source_summary && (
                <Content component="small" className="pf-v6-u-display-block pf-v6-u-mt-md pf-v6-u-color-200">
                  {/* What the model actually saw. Without this an operator
                      can't judge whether a "no findings" result means the
                      design is clean or the inputs were thin. */}
                  Based on: {results.source_summary}
                </Content>
              )}
            </CardBody>
          </Card>
        </PageSection>
      )}

      <PageSection hasBodyWrapper={false}>
        {grouped.all.length === 0 && !isAnalyzing ? (
          <GuidedEmptyState
            title={review?.last_analyzed_at ? "No findings" : "Not analyzed yet"}
            body={
              review?.last_analyzed_at
                ? "The analysis completed without flagging anything. Check the summary above for what it actually compared before reading this as a clean bill of health."
                : "Run the analysis to compare your proposed design against the source estate."
            }
            primary={{
              label: review?.last_analyzed_at ? "Re-run analysis" : "Run analysis",
              onClick: analyze,
              isDisabled: busy,
            }}
          />
        ) : (
          <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Findings by triage">
            <Tab
              eventKey="open"
              title={<TabTitleText>{`Open (${grouped.open.length})`}</TabTitleText>}
            >
              <div className="pf-v6-u-mt-md">{renderList(grouped.open)}</div>
            </Tab>
            <Tab
              eventKey="accepted"
              title={<TabTitleText>{`Accepted (${grouped.accepted.length})`}</TabTitleText>}
            >
              <div className="pf-v6-u-mt-md">{renderList(grouped.accepted)}</div>
            </Tab>
            <Tab
              eventKey="dismissed"
              title={<TabTitleText>{`Dismissed (${grouped.dismissed.length})`}</TabTitleText>}
            >
              <div className="pf-v6-u-mt-md">{renderList(grouped.dismissed)}</div>
            </Tab>
          </Tabs>
        )}
      </PageSection>
    </PageFrame>
  );
}
