// Design reviews — network and storage, in one index.
//
// Was the dashboard's "design review" useState tab. The two review kinds
// have identical API shapes (/api/network-reviews and
// /api/storage-reviews), so they share a table and are distinguished by
// a Kind column rather than being split into two screens.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bullseye,
  Button,
  Flex,
  FlexItem,
  Label,
  LabelGroup,
  PageSection,
  Skeleton,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import {
  ErrorEmptyState,
  GuidedEmptyState,
  NO_DESIGN_REVIEWS,
} from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

const KINDS = [
  { kind: "network", label: "Network", endpoint: "/api/network-reviews", path: "/design-reviews" },
  {
    kind: "storage",
    label: "Storage",
    endpoint: "/api/storage-reviews",
    path: "/design-reviews/storage",
  },
];

// Severity order for the per-review chip row — worst first, so a review
// with criticals reads as urgent at a glance.
const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];
const SEVERITY_COLOR = {
  critical: "red",
  high: "red",
  medium: "orange",
  low: "blue",
  info: "grey",
};

export default function DesignReviewsPage() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const results = await Promise.allSettled(
        KINDS.map((k) => fetchJSON(k.endpoint)),
      );

      // If every kind failed the backend is the problem; if only one did,
      // show what loaded rather than blanking the page.
      if (results.every((r) => r.status === "rejected")) throw results[0].reason;

      const merged = results.flatMap((r, i) =>
        r.status === "fulfilled" && Array.isArray(r.value)
          ? r.value.map((row) => ({ ...row, _kind: KINDS[i] }))
          : [],
      );
      merged.sort(
        (a, b) => new Date(b?.updated_at ?? 0) - new Date(a?.updated_at ?? 0),
      );
      setRows(merged);
      setError(null);
    } catch (e) {
      setError(e);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const actions = (
    <Flex spaceItems={{ default: "spaceItemsSm" }}>
      <FlexItem>
        <Button
          variant="secondary"
          component={(p) => <Link to="/design-reviews/network/new" {...p} />}
        >
          New network review
        </Button>
      </FlexItem>
      <FlexItem>
        <Button
          variant="secondary"
          component={(p) => <Link to="/design-reviews/storage/new" {...p} />}
        >
          New storage review
        </Button>
      </FlexItem>
    </Flex>
  );

  const body = () => {
    if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;

    if (loading && rows.length === 0) {
      return Array.from({ length: 4 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={6}>
            <Skeleton screenreaderText="Loading design reviews" />
          </Td>
        </Tr>
      ));
    }

    if (rows.length === 0) {
      return (
        <Tr>
          <Td colSpan={6}>
            <Bullseye>
              <GuidedEmptyState
                title={NO_DESIGN_REVIEWS.title}
                body={NO_DESIGN_REVIEWS.body}
                primary={{ label: "New network review", to: "/design-reviews/network/new" }}
                secondary={{ label: "New storage review", to: "/design-reviews/storage/new" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return rows.map((row) => {
      const counts = row?.severity_counts ?? {};
      return (
        <Tr key={`${row._kind.kind}-${row.id}`}>
          <Td dataLabel="Name">
            <Link to={`${row._kind.path}/${row.id}`}>{row?.name || `Review #${row.id}`}</Link>
          </Td>
          <Td dataLabel="Kind">{row._kind.label}</Td>
          <Td dataLabel="Status">
            <StatusLabel kind="record" value={row?.status} />
          </Td>
          <Td dataLabel="Findings">{row?.finding_count ?? 0}</Td>
          <Td dataLabel="Severity">
            {Object.keys(counts).length === 0 ? (
              "—"
            ) : (
              <LabelGroup numLabels={5}>
                {SEVERITY_ORDER.filter((s) => counts[s]).map((s) => (
                  <Label key={s} isCompact color={SEVERITY_COLOR[s] ?? "grey"}>
                    {`${s} ${counts[s]}`}
                  </Label>
                ))}
              </LabelGroup>
            )}
          </Td>
          <Td dataLabel="Last analyzed">
            {row?.last_analyzed_at
              ? new Date(row.last_analyzed_at).toLocaleString()
              : "Not yet analyzed"}
          </Td>
        </Tr>
      );
    });
  };

  return (
    <PageFrame
      title="Design reviews"
      description="Analyze a proposed OpenShift network or storage design against the source estate, and see the gaps before you migrate."
      breadcrumbs={[{ label: "Discover" }, { label: "Design reviews" }]}
      actions={actions}
    >
      <PageSection hasBodyWrapper={false}>
        <Table aria-label="Design reviews" variant="compact">
          <Thead>
            <Tr>
              <Th width={25}>Name</Th>
              <Th width={10}>Kind</Th>
              <Th width={15}>Status</Th>
              <Th width={10}>Findings</Th>
              <Th width={25}>Severity</Th>
              <Th width={15}>Last analyzed</Th>
            </Tr>
          </Thead>
          <Tbody>{body()}</Tbody>
        </Table>
      </PageSection>
    </PageFrame>
  );
}
