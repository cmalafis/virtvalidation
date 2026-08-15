// Report viewer.
//
// Reports are generated from live data on each open. The print path is
// the export path — there is no PDF renderer here, the browser's own
// print-to-PDF does it — so the print stylesheet is load-bearing rather
// than decorative.

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Content,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  PageSection,
  Skeleton,
  Spinner,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

const REPORTS = {
  "executive-summary": {
    title: "Executive summary",
    endpoint: "/api/reports/executive-summary",
    subtitle: "High-level overview suitable for leadership.",
  },
  "full-validation": {
    title: "Full migration validation report",
    endpoint: "/api/reports/full-validation",
    subtitle: "All VMs, all findings, remediation steps.",
  },
  "failed-degraded": {
    title: "Failed and degraded VMs",
    endpoint: "/api/reports/failed-degraded",
    subtitle: "Only the VMs requiring action.",
  },
  "wave-plan": {
    title: "Migration wave plan",
    endpoint: "/api/reports/wave-plan",
    subtitle: "Wave sequencing with rationale.",
  },
  "baseline-snapshot": {
    title: "Pre-migration baseline snapshot",
    endpoint: "/api/reports/baseline-snapshot",
    subtitle: "Full captured state of all VMs before migration.",
  },
};

// Print rules. Reports are exported by printing, so this needs to hide
// the app chrome and flatten the theme to ink-on-paper regardless of
// whether the operator is in dark mode.
function PrintStyles() {
  return (
    <style>{`
      @media print {
        .pf-v6-c-masthead,
        .pf-v6-c-page__sidebar,
        .pf-v6-c-breadcrumb,
        .report-no-print { display: none !important; }
        .pf-v6-c-page__main { padding: 0 !important; }
        html, body { background: #fff !important; }
        * { color: #000 !important; background: transparent !important;
            box-shadow: none !important; }
        .pf-v6-c-card { border: 1px solid #999 !important; break-inside: avoid; }
        table { border-collapse: collapse !important; }
        th, td { border: 1px solid #999 !important; }
        a { color: #003366 !important; text-decoration: underline !important; }
      }
    `}</style>
  );
}

// Reports return differently-shaped payloads. Rather than five bespoke
// renderers, render the shape: scalars as a description list, arrays of
// objects as a table, and prose as prose.
function Section({ title, value }) {
  if (value === null || value === undefined) return null;

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return (
        <Card className="pf-v6-u-mb-md">
          <CardTitle>{title}</CardTitle>
          <CardBody>
            <Content component="p" className="pf-v6-u-color-200">
              Nothing to report.
            </Content>
          </CardBody>
        </Card>
      );
    }

    if (typeof value[0] !== "object") {
      return (
        <Card className="pf-v6-u-mb-md">
          <CardTitle>{title}</CardTitle>
          <CardBody>
            <ul>
              {value.map((v, i) => (
                <li key={i}>{String(v)}</li>
              ))}
            </ul>
          </CardBody>
        </Card>
      );
    }

    // Union of keys across rows, so a row missing a field still lines up.
    const columns = [...new Set(value.flatMap((row) => Object.keys(row ?? {})))].filter(
      (k) => typeof value[0][k] !== "object" || value[0][k] === null,
    );

    return (
      <Card className="pf-v6-u-mb-md">
        <CardTitle>{`${title} (${value.length})`}</CardTitle>
        <CardBody>
          <Table aria-label={title} variant="compact">
            <Thead>
              <Tr>
                {columns.map((c) => (
                  <Th key={c}>{c.replace(/_/g, " ")}</Th>
                ))}
              </Tr>
            </Thead>
            <Tbody>
              {value.map((row, i) => (
                <Tr key={row?.id ?? i}>
                  {columns.map((c) => (
                    <Td key={c} dataLabel={c}>
                      {/status|verdict|severity/.test(c) && row?.[c] ? (
                        <StatusLabel
                          kind={/severity/.test(c) ? "severity" : "status"}
                          value={String(row[c])}
                        />
                      ) : (
                        String(row?.[c] ?? "—")
                      )}
                    </Td>
                  ))}
                </Tr>
              ))}
            </Tbody>
          </Table>
        </CardBody>
      </Card>
    );
  }

  if (typeof value === "object") {
    const entries = Object.entries(value).filter(([, v]) => typeof v !== "object");
    const nested = Object.entries(value).filter(
      ([, v]) => typeof v === "object" && v !== null,
    );
    return (
      <>
        {entries.length > 0 && (
          <Card className="pf-v6-u-mb-md">
            <CardTitle>{title}</CardTitle>
            <CardBody>
              <DescriptionList isCompact isHorizontal>
                {entries.map(([k, v]) => (
                  <DescriptionListGroup key={k}>
                    <DescriptionListTerm>{k.replace(/_/g, " ")}</DescriptionListTerm>
                    <DescriptionListDescription>{String(v)}</DescriptionListDescription>
                  </DescriptionListGroup>
                ))}
              </DescriptionList>
            </CardBody>
          </Card>
        )}
        {nested.map(([k, v]) => (
          <Section key={k} title={k.replace(/_/g, " ")} value={v} />
        ))}
      </>
    );
  }

  // Long strings are prose (the LLM-written summary); short ones are facts.
  return (
    <Card className="pf-v6-u-mb-md">
      <CardTitle>{title}</CardTitle>
      <CardBody>
        <Content component="p" style={{ whiteSpace: "pre-wrap" }}>
          {String(value)}
        </Content>
      </CardBody>
    </Card>
  );
}

export default function ReportViewPage() {
  const { type } = useParams();
  const meta = REPORTS[type];

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!meta) return;
    setLoading(true);
    try {
      setData(await fetchJSON(meta.endpoint));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [meta]);

  useEffect(() => {
    load();
  }, [load]);

  if (!meta) {
    return (
      <PageFrame
        title="Unknown report"
        breadcrumbs={[{ label: "Verify" }, { label: "Reports", to: "/reports" }]}
      >
        <PageSection>
          <Alert variant="warning" isInline title={`No report type "${type}"`}>
            <Link to="/reports">Back to the reports index</Link>
          </Alert>
        </PageSection>
      </PageFrame>
    );
  }

  const frameProps = {
    title: meta.title,
    description: meta.subtitle,
    breadcrumbs: [
      { label: "Verify" },
      { label: "Reports", to: "/reports" },
      { label: meta.title },
    ],
  };

  return (
    <PageFrame
      {...frameProps}
      actions={
        <Button variant="secondary" className="report-no-print" onClick={() => window.print()}>
          Print / save as PDF
        </Button>
      }
    >
      <PrintStyles />
      <PageSection hasBodyWrapper={false}>
        {error ? (
          <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
        ) : loading && !data ? (
          <Card>
            <CardBody>
              {/* Reports are generated on demand and the LLM-written ones
                  take ten seconds or more. A bare skeleton that long reads
                  as broken, so say what's happening. */}
              <Content component="p" className="pf-v6-u-mb-md">
                <Spinner size="md" className="pf-v6-u-mr-sm" />
                Gathering live data and writing the report. This usually takes
                a few seconds.
              </Content>
              <Skeleton width="100%" height="180px" screenreaderText="Loading report" />
            </CardBody>
          </Card>
        ) : (
          <>
            <Content component="small" className="pf-v6-u-color-200 pf-v6-u-mb-md pf-v6-u-display-block">
              {/* The server's generation time, not the browser's clock —
                  a printed report is evidence, and the time on it should
                  be when the data was gathered. */}
              {data?.generated_at
                ? `Generated ${new Date(data.generated_at).toLocaleString()} from live data.`
                : "Generated from live data."}
            </Content>
            {Object.entries(data ?? {})
              // Envelope metadata, not report content: the type is already
              // the page title and the timestamp is rendered above.
              .filter(([key]) => !["report_type", "generated_at"].includes(key))
              .map(([key, value]) => (
                <Section key={key} title={key.replace(/_/g, " ")} value={value} />
              ))}
          </>
        )}
      </PageSection>
    </PageFrame>
  );
}
