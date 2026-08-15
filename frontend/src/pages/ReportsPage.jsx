// Reports — the index of available reports.
//
// Was the dashboard's "reports" useState tab: five tiles linking to
// /reports/:type. The types and their copy stay in sync with the
// REPORTS map in components/ReportView.jsx, which renders them.

import { Link } from "react-router-dom";
import {
  Card,
  CardBody,
  CardTitle,
  Gallery,
  GalleryItem,
  Content,
  PageSection,
} from "@patternfly/react-core";

import PageFrame from "../common/PageFrame";

const REPORTS = [
  {
    type: "executive-summary",
    title: "Executive summary",
    body: "High-level overview suitable for leadership. Written by the LLM, with a deterministic fallback.",
  },
  {
    type: "full-validation",
    title: "Full validation report",
    body: "Every VM, every finding, with remediation steps. The CISO-ready artifact.",
  },
  {
    type: "failed-degraded",
    title: "Failed and degraded VMs",
    body: "Filtered to only the VMs that need action, for working a remediation list.",
  },
  {
    type: "wave-plan",
    title: "Migration wave plan",
    body: "Wave sequencing with the rationale behind each wave's contents.",
  },
  {
    type: "baseline-snapshot",
    title: "Pre-migration baseline snapshot",
    body: "The full captured state of every VM before migration — the evidence validation compares against.",
  },
];

export default function ReportsPage() {
  return (
    <PageFrame
      title="Reports"
      description="Generated from live data each time you open one. Every report has a print view for export."
      breadcrumbs={[{ label: "Verify" }, { label: "Reports" }]}
    >
      <PageSection hasBodyWrapper={false}>
        <Gallery hasGutter minWidths={{ default: "320px" }}>
          {REPORTS.map((report) => (
            <GalleryItem key={report.type}>
              <Link
                to={`/reports/${report.type}`}
                style={{
                  textDecoration: "none",
                  color: "var(--pf-t--global--text--color--regular)",
                  display: "block",
                  height: "100%",
                }}
              >
                <Card isFullHeight>
                  <CardTitle>{report.title}</CardTitle>
                  <CardBody>
                    <Content component="p" className="pf-v6-u-color-200">
                      {report.body}
                    </Content>
                  </CardBody>
                </Card>
              </Link>
            </GalleryItem>
          ))}
        </Gallery>
      </PageSection>
    </PageFrame>
  );
}
