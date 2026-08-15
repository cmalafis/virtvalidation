// A single design-review finding.
//
// NetworkReviewDetail and StorageReviewDetail each carried a near-verbatim
// copy of this plus ConfidenceTag, EvidenceBlock, StatusPill and Caveats.
// One component now serves both.
//
// The evidence blocks are the important part: a finding an operator can't
// trace back to specific source and proposed config is one they can't act
// on, so evidence is always available rather than hidden behind a detail
// route.

import {
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Content,
  ExpandableSection,
  Flex,
  FlexItem,
  Label,
  LabelGroup,
} from "@patternfly/react-core";

import StatusLabel from "./StatusLabel";

const CONFIDENCE_COLOR = { high: "green", medium: "orange", low: "red" };

const TRIAGE_ACTIONS = [
  { triage: "accepted", label: "Accept", variant: "secondary" },
  { triage: "dismissed", label: "Dismiss", variant: "link" },
  { triage: "open", label: "Reopen", variant: "link" },
];

function EvidenceBlock({ label, content }) {
  if (!content) return null;
  return (
    <div className="pf-v6-u-mb-md">
      <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-font-size-sm">
        {label}
      </Content>
      <pre
        className="pf-v6-u-font-size-sm"
        style={{
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          margin: 0,
          padding: "var(--pf-t--global--spacer--sm)",
          background: "var(--pf-t--global--background--color--secondary--default)",
          borderRadius: "var(--pf-t--global--border--radius--small)",
        }}
      >
        {content}
      </pre>
    </div>
  );
}

export default function FindingCard({ finding, onTriage, isBusy }) {
  const {
    title,
    description,
    category,
    severity,
    confidence,
    triage,
    source_evidence: sourceEvidence,
    proposed_evidence: proposedEvidence,
    recommendation,
  } = finding ?? {};

  const hasEvidence = Boolean(sourceEvidence || proposedEvidence);

  // Offer the transitions that make sense from the current state.
  const actions = TRIAGE_ACTIONS.filter((a) =>
    triage === "open" ? a.triage !== "open" : a.triage === "open",
  );

  return (
    <Card isCompact className="pf-v6-u-mb-md">
      <CardHeader>
        <Flex
          justifyContent={{ default: "justifyContentSpaceBetween" }}
          alignItems={{ default: "alignItemsFlexStart" }}
          flexWrap={{ default: "wrap" }}
          spaceItems={{ default: "spaceItemsSm" }}
          style={{ width: "100%" }}
        >
          <FlexItem flex={{ default: "flex_1" }}>
            <CardTitle>{title ?? "Untitled finding"}</CardTitle>
          </FlexItem>
          <FlexItem>
            <LabelGroup numLabels={4}>
              <StatusLabel kind="severity" value={severity} />
              {confidence && (
                <Label isCompact color={CONFIDENCE_COLOR[confidence] ?? "grey"}>
                  {`${confidence} confidence`}
                </Label>
              )}
              {category && <Label isCompact>{category}</Label>}
              <StatusLabel kind="record" value={triage} />
            </LabelGroup>
          </FlexItem>
        </Flex>
      </CardHeader>

      <CardBody>
        {description && <Content component="p">{description}</Content>}

        {recommendation && (
          <div className="pf-v6-u-mt-md">
            <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-font-size-sm">
              Recommendation
            </Content>
            <Content component="p">{recommendation}</Content>
          </div>
        )}

        {hasEvidence && (
          <ExpandableSection toggleText="Show evidence" className="pf-v6-u-mt-md">
            <EvidenceBlock label="Source (current estate)" content={sourceEvidence} />
            <EvidenceBlock label="Proposed (your design)" content={proposedEvidence} />
          </ExpandableSection>
        )}

        {onTriage && (
          <Flex className="pf-v6-u-mt-md" spaceItems={{ default: "spaceItemsSm" }}>
            {actions.map((action) => (
              <FlexItem key={action.triage}>
                <Button
                  variant={action.variant}
                  isDisabled={isBusy}
                  onClick={() => onTriage(finding.id, action.triage)}
                >
                  {action.label}
                </Button>
              </FlexItem>
            ))}
          </Flex>
        )}
      </CardBody>
    </Card>
  );
}
