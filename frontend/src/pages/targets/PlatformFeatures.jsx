// What this cluster's OpenShift / MTV release supports.
//
// Read from GET /api/sources/targets/{id}/platform-features, which is
// app.core.platform_features — the same matrix the mapping checks and
// the suggestion prompt use, so what the operator reads here is what
// the checks enforce.

import { useCallback, useEffect, useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import { Alert, Button, Content, Label, Skeleton } from "@patternfly/react-core";
import ExternalLinkAltIcon from "@patternfly/react-icons/dist/esm/icons/external-link-alt-icon";

import { ErrorEmptyState } from "../../common/EmptyStates";
import { asArray } from "../../utils/asArray";
import { fetchJSON } from "../../utils/fetchJSON";

const STATUS_COLOR = { GA: "green", TP: "orange", DP: "red", unavailable: "grey" };
const STATUS_TEXT = {
  GA: "GA",
  TP: "Tech Preview",
  DP: "Developer Preview",
  unavailable: "Not in this release",
};

export default function PlatformFeatures({ targetId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchJSON(`/api/sources/targets/${targetId}/platform-features`));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, [targetId]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <ErrorEmptyState error={error} onRetry={load} isRetrying={loading} />;
  if (loading && !data) return <Skeleton width="100%" height="160px" />;

  const features = asArray(data?.features);
  const ocp = data?.ocp_version;
  const mtv = data?.mtv_version;

  return (
    <>
      {!ocp && (
        <Alert
          variant="info"
          isInline
          title="No OpenShift version recorded for this cluster"
          className="pf-v6-u-mb-md"
        >
          Support levels below are for the newest release VirtValidate knows. Set the version on
          the cluster so the mapping checks judge against the right release.
        </Alert>
      )}
      <Content component="p" className="pf-v6-u-mb-md">
        {`OpenShift ${ocp ?? "(unknown)"}${mtv ? ` · MTV ${mtv}${data?.mtv_version_inferred ? " (paired release)" : ""}` : ""}`}
      </Content>
      <Table aria-label="Platform features" variant="compact">
        <Thead>
          <Tr>
            <Th width={30}>Feature</Th>
            <Th width={15}>Status</Th>
            <Th>What it means for a migration</Th>
            <Th screenReaderText="Source" />
          </Tr>
        </Thead>
        <Tbody>
          {features.map((f) => (
            <Tr key={f?.key}>
              <Td dataLabel="Feature">{f?.title ?? "—"}</Td>
              <Td dataLabel="Status">
                <Label isCompact color={STATUS_COLOR[f?.status] ?? "grey"}>
                  {STATUS_TEXT[f?.status] ?? f?.status ?? "—"}
                </Label>
              </Td>
              <Td dataLabel="Summary">{f?.summary ?? ""}</Td>
              <Td dataLabel="Source" isActionCell>
                {f?.source && (
                  <Button
                    variant="link"
                    isInline
                    component="a"
                    href={f.source}
                    target="_blank"
                    rel="noopener noreferrer"
                    icon={<ExternalLinkAltIcon />}
                    iconPosition="end"
                  >
                    Docs
                  </Button>
                )}
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
    </>
  );
}
