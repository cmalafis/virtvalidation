// Overview — the landing page.
//
// Absorbs three things the old dashboard scattered: the four header
// status counters, the fixed 260px right sidebar ("System" + "Quick
// Actions"), and the empty-state prompts.
//
// Two behavior changes worth naming:
//
//  1. The old header had a "Degraded" counter that was structurally
//     always zero — degraded is a verdict-level concept with no
//     corresponding VM.status value, as the old code's own comment
//     admitted. It's replaced with the real VM.status distribution.
//
//  2. The old sidebar hardcoded the appliance version ("v0.1.0-alpha")
//     and a cluster name ("ocp-virt-prod-01") that did not come from
//     anywhere. Those now read from /api/health/full and
//     /api/settings/llm, or say so when unknown.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Divider,
  Flex,
  FlexItem,
  Gallery,
  GalleryItem,
  PageSection,
  Skeleton,
  Content,
  Title,
} from "@patternfly/react-core";
import ArrowRightIcon from "@patternfly/react-icons/dist/esm/icons/arrow-right-icon";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

// VM.status values, in lifecycle order. Labels match what the docs and
// the inventory filters call them.
const STATUS_TILES = [
  { key: "discovered", label: "Discovered", hint: "In inventory, no baseline yet" },
  { key: "baseline_captured", label: "Baseline captured", hint: "Ready to migrate" },
  { key: "migrated", label: "Migrated", hint: "Observed on OpenShift" },
  { key: "validated", label: "Validated", hint: "Checked against baseline" },
  { key: "failed", label: "Failed", hint: "Needs attention" },
];

function StatTile({ label, value, hint, to, isLoading }) {
  const body = (
    <Card isCompact isFullHeight>
      <CardBody>
        {/* Inherit the surrounding text color explicitly: when the tile is
            wrapped in a <Link> the heading would otherwise pick up the
            link blue and read as clickable text rather than a figure. */}
        <Title headingLevel="h2" size="3xl" style={{ color: "inherit" }}>
          {isLoading ? <Skeleton width="50%" screenreaderText="Loading" /> : value}
        </Title>
        <Content component="p" className="pf-v6-u-mt-xs">
          {label}
        </Content>
        {hint && (
          <Content component="small" className="pf-v6-u-color-200">
            {hint}
          </Content>
        )}
      </CardBody>
    </Card>
  );

  if (!to) return body;

  return (
    <Link
      to={to}
      style={{
        textDecoration: "none",
        color: "var(--pf-t--global--text--color--regular)",
        display: "block",
        height: "100%",
      }}
    >
      {body}
    </Link>
  );
}

// The four setup steps, in order. Each reports whether it's done so a
// first-run operator can see where they are in the sequence rather than
// guessing which screen to open next.
function NextStepCard({ counts, sources, targets, mappings, isLoading }) {
  if (isLoading) {
    return (
      <Card isFullHeight>
        <CardTitle>Getting started</CardTitle>
        <CardBody>
          <Skeleton width="80%" />
        </CardBody>
      </Card>
    );
  }

  const steps = [
    {
      done: sources > 0,
      label: "Register a vCenter source",
      to: "/sources/vcenters",
      why: "VirtValidate groups migration waves by source vCenter.",
    },
    {
      done: targets > 0,
      label: "Register an OpenShift target",
      to: "/sources/targets",
      why: "Plans need somewhere for the VMs to land.",
    },
    {
      done: mappings > 0,
      label: "Define resource mappings",
      to: "/mappings",
      why: "Maps vSphere networks and datastores to NADs and StorageClasses.",
    },
    {
      done: (counts?.total ?? 0) > 0,
      label: "Import your VM inventory",
      to: "/inventory",
      why: "Everything else operates on inventory.",
    },
  ];

  const next = steps.find((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;

  return (
    <Card isFullHeight>
      <CardTitle>
        Getting started — {doneCount} of {steps.length} complete
      </CardTitle>
      <CardBody>
        <DescriptionList isCompact>
          {steps.map((step) => (
            <DescriptionListGroup key={step.label}>
              <DescriptionListTerm>
                <StatusLabel
                  kind="status"
                  value={step.done ? "healthy" : "pending"}
                  isCompact
                >
                  {step.done ? "Done" : "To do"}
                </StatusLabel>
              </DescriptionListTerm>
              <DescriptionListDescription>
                <Link to={step.to}>{step.label}</Link>
                {!step.done && (
                  <Content component="small" className="pf-v6-u-display-block pf-v6-u-color-200">
                    {step.why}
                  </Content>
                )}
              </DescriptionListDescription>
            </DescriptionListGroup>
          ))}
        </DescriptionList>

        {next && (
          <Button
            variant="primary"
            className="pf-v6-u-mt-md"
            icon={<ArrowRightIcon />}
            iconPosition="end"
            component={(p) => <Link to={next.to} {...p} />}
          >
            {next.label}
          </Button>
        )}
      </CardBody>
    </Card>
  );
}

function SystemCard({ health, llm }) {
  const components = health?.components ?? {};
  const apiVersion = health?.api?.version;
  const fips = health?.fips;

  const rows = [
    ["Appliance", apiVersion ? `v${apiVersion}` : "Unknown"],
    ["LLM backend", llm?.active_llm_backend ?? "Unknown"],
    [
      "LLM status",
      <StatusLabel
        key="llm"
        kind="status"
        value={components?.llm?.status === "online" ? "healthy" : "failed"}
      >
        {components?.llm?.status === "online" ? "Online" : "Offline"}
      </StatusLabel>,
    ],
    [
      "Database",
      <StatusLabel
        key="db"
        kind="status"
        value={components?.database?.status === "online" ? "healthy" : "failed"}
      >
        {components?.database?.status === "online" ? "Online" : "Offline"}
      </StatusLabel>,
    ],
    [
      // schema_status() returns {current_revision, head_revision,
      // is_up_to_date, pending_migrations, legacy_create_all}.
      "Schema",
      components?.schema ? (
        <StatusLabel
          key="schema"
          kind="status"
          value={components.schema.is_up_to_date ? "healthy" : "degraded"}
        >
          {components.schema.is_up_to_date
            ? "Up to date"
            : `${(components.schema.pending_migrations ?? []).length || "Some"} pending`}
        </StatusLabel>
      ) : (
        "Unknown"
      ),
    ],
    [
      // fips_status() returns {configured, detected, effective, ...}.
      // "effective" is the only one that means FIPS is actually active —
      // configured-without-detected is the classic misconfiguration.
      "FIPS",
      fips ? (
        <StatusLabel
          key="fips"
          kind="status"
          value={fips.effective ? "healthy" : "pending"}
        >
          {fips.effective
            ? "Active"
            : fips.configured
              ? "Configured, not active"
              : "Not enabled"}
        </StatusLabel>
      ) : (
        "Unknown"
      ),
    ],
  ];

  return (
    <Card isFullHeight>
      <CardTitle>System</CardTitle>
      <CardBody>
        <DescriptionList isCompact isHorizontal>
          {rows.map(([term, value]) => (
            <DescriptionListGroup key={term}>
              <DescriptionListTerm>{term}</DescriptionListTerm>
              <DescriptionListDescription>{value}</DescriptionListDescription>
            </DescriptionListGroup>
          ))}
        </DescriptionList>
        <Divider className="pf-v6-u-my-md" />
        <Link to="/settings">Manage settings</Link>
      </CardBody>
    </Card>
  );
}

export default function OverviewPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Independent reads — one failing shouldn't blank the page, so
      // each is settled separately and missing pieces render as unknown.
      //
      // These are all cheap status/count endpoints, so they get a shorter
      // leash than fetchJSON's 60s default: a landing page that sits on a
      // skeleton for a full minute reads as broken.
      const opts = { timeoutMs: 15000 };
      const [stats, health, llm, vcenters, targets, mappings] = await Promise.allSettled([
        fetchJSON("/api/vms/stats", opts),
        fetchJSON("/api/health/full", opts),
        fetchJSON("/api/settings/llm", opts),
        fetchJSON("/api/sources/vcenters", opts),
        fetchJSON("/api/sources/targets", opts),
        fetchJSON("/api/mappings", opts),
      ]);

      const val = (r) => (r.status === "fulfilled" ? r.value : null);
      const count = (r) => {
        const v = val(r);
        if (Array.isArray(v)) return v.length;
        return v?.items?.length ?? v?.total ?? 0;
      };

      if (stats.status === "rejected" && health.status === "rejected") {
        // Both core reads failed — the backend is likely down. Surface
        // it rather than rendering a page full of zeroes.
        throw stats.reason;
      }

      setData({
        stats: val(stats),
        health: val(health),
        llm: val(llm),
        sources: count(vcenters),
        targets: count(targets),
        mappings: count(mappings),
      });
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <PageFrame title="Overview">
        <PageSection>
          <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />
        </PageSection>
      </PageFrame>
    );
  }

  const byStatus = data?.stats?.by_status ?? {};
  const total = data?.stats?.total ?? 0;

  return (
    <PageFrame
      title="Overview"
      description="Validate VMs migrated from VMware to OpenShift Virtualization."
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <Button variant="secondary" component={(p) => <Link to="/inventory" {...p} />}>
              View inventory
            </Button>
          </FlexItem>
          <FlexItem>
            <Button variant="primary" component={(p) => <Link to="/plans/new" {...p} />}>
              Generate plan
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      <PageSection>
        {/* Six tiles. A 150px floor keeps them on one row at desktop
            widths instead of orphaning the last one onto its own line,
            and still wraps cleanly on narrow viewports. */}
        <Gallery hasGutter minWidths={{ default: "150px" }}>
          <GalleryItem>
            <StatTile
              label="Total VMs"
              value={total}
              hint="In inventory"
              to="/inventory"
              isLoading={loading}
            />
          </GalleryItem>
          {STATUS_TILES.map((tile) => (
            <GalleryItem key={tile.key}>
              <StatTile
                label={tile.label}
                value={byStatus?.[tile.key] ?? 0}
                hint={tile.hint}
                to={`/inventory?status=${tile.key}`}
                isLoading={loading}
              />
            </GalleryItem>
          ))}
        </Gallery>
      </PageSection>

      <PageSection>
        <Gallery hasGutter minWidths={{ default: "340px" }}>
          <GalleryItem>
            <NextStepCard
              counts={data?.stats}
              sources={data?.sources ?? 0}
              targets={data?.targets ?? 0}
              mappings={data?.mappings ?? 0}
              isLoading={loading}
            />
          </GalleryItem>
          <GalleryItem>
            <SystemCard health={data?.health} llm={data?.llm} />
          </GalleryItem>
        </Gallery>
      </PageSection>
    </PageFrame>
  );
}
