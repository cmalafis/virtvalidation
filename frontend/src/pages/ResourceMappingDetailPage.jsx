// Resource mapping detail — the row-by-row editor.
//
// Two tables: source network → target network, source datastore → target
// StorageClass. Both can be filled from LLM suggestions, and preflight
// reports what is still unresolved.
//
// The LLM suggestions are proposals, not commitments: each carries a
// confidence and a rationale, and nothing is saved until the operator
// saves. That distinction matters — a wrong network mapping sends a
// production VM onto the wrong VLAN.

import { useCallback, useEffect, useState } from "react";
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
  FormSelect,
  FormSelectOption,
  Label,
  List,
  ListItem,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
  TextInput,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

const CONFIDENCE_COLOR = { high: "green", medium: "orange", low: "red" };
const ACCESS_MODES = ["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"];

function ConfidenceCell({ confidence, rationale }) {
  if (!confidence) return "—";
  return (
    <>
      <Label isCompact color={CONFIDENCE_COLOR[confidence] ?? "grey"}>
        {confidence}
      </Label>
      {rationale && (
        <Content component="small" className="pf-v6-u-display-block pf-v6-u-color-200">
          {rationale}
        </Content>
      )}
    </>
  );
}

export default function ResourceMappingDetailPage() {
  const { id } = useParams();
  const [mapping, setMapping] = useState(null);
  const [catalog, setCatalog] = useState({ networks: [], storageClasses: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [tab, setTab] = useState("networks");

  const load = useCallback(async () => {
    try {
      const m = await fetchJSON(`/api/mappings/${id}`);
      setMapping(m);
      setError(null);

      // Catalogs drive the dropdowns; a mapping to something the cluster
      // doesn't have is exactly what preflight is meant to catch.
      const [nets, scs] = await Promise.allSettled([
        fetchJSON(`/api/ocp-targets/${m.ocp_target_id}/networks?limit=500`),
        fetchJSON(`/api/ocp-targets/${m.ocp_target_id}/storage-classes?limit=500`),
      ]);
      const items = (r) =>
        r.status === "fulfilled"
          ? Array.isArray(r.value)
            ? r.value
            : (r.value?.items ?? [])
          : [];
      setCatalog({ networks: items(nets), storageClasses: items(scs) });
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

  const patchRow = (kind, index, patch) =>
    setMapping((m) => {
      const key = kind === "networks" ? "network_mappings" : "storage_mappings";
      const rows = [...(m?.[key] ?? [])];
      rows[index] = { ...rows[index], ...patch };
      return { ...m, [key]: rows };
    });

  const save = async () => {
    setSaving(true);
    try {
      const updated = await fetchJSON(`/api/mappings/${id}`, {
        method: "PATCH",
        body: {
          network_mappings: mapping?.network_mappings ?? [],
          storage_mappings: mapping?.storage_mappings ?? [],
        },
      });
      setMapping(updated);
      toast.success("Mapping saved");
    } catch (e) {
      toast.error(e?.message ?? "Could not save");
    } finally {
      setSaving(false);
    }
  };

  const suggest = async (kind) => {
    setSuggesting(kind);
    try {
      const path = kind === "networks" ? "suggest-network" : "suggest-storage";
      const result = await fetchJSON(`/api/mappings/${id}/${path}`, { method: "POST" });
      const suggestions = result?.suggestions ?? [];

      // Merge onto existing rows by source key. Suggestions are proposals
      // — they populate the form but are not persisted until Save.
      setMapping((m) => {
        const key = kind === "networks" ? "network_mappings" : "storage_mappings";
        const srcKey = kind === "networks" ? "source_network" : "source_datastore";
        const bySource = new Map(suggestions.map((s) => [s[srcKey], s]));
        const rows = (m?.[key] ?? []).map((row) => {
          const hit = bySource.get(row[srcKey]);
          return hit ? { ...row, ...hit } : row;
        });
        return { ...m, [key]: rows };
      });

      toast.success(
        `${suggestions.length} suggestion(s) applied to the form — review, then save.`,
      );
    } catch (e) {
      toast.error(e?.message ?? "Could not get suggestions");
    } finally {
      setSuggesting(null);
    }
  };

  const runPreflight = async () => {
    try {
      setPreflight(await fetchJSON(`/api/mappings/${id}/preflight`, { method: "POST" }));
    } catch (e) {
      toast.error(e?.message ?? "Preflight failed");
    }
  };

  const title = mapping?.name || `Mapping #${id}`;
  const frameProps = {
    title,
    breadcrumbs: [
      { label: "Configure" },
      { label: "Resource mappings", to: "/mappings" },
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

  if (loading && !mapping) {
    return (
      <PageFrame {...frameProps}>
        <PageSection>
          <Skeleton width="100%" height="200px" />
        </PageSection>
      </PageFrame>
    );
  }

  const networks = mapping?.network_mappings ?? [];
  const storage = mapping?.storage_mappings ?? [];

  const gapList = (label, values) =>
    (values ?? []).length > 0 ? (
      <>
        <Content component="p" className="pf-v6-u-font-weight-bold pf-v6-u-mt-sm">
          {label}
        </Content>
        <List>
          {values.map((v) => (
            <ListItem key={v}>{v}</ListItem>
          ))}
        </List>
      </>
    ) : null;

  return (
    <PageFrame
      {...frameProps}
      description="Every VM in a plan must resolve through this mapping. Preflight reports what is still unresolved."
      actions={
        <Flex spaceItems={{ default: "spaceItemsSm" }}>
          <FlexItem>
            <StatusLabel kind="record" value={mapping?.status} />
          </FlexItem>
          <FlexItem>
            <Button variant="secondary" onClick={runPreflight}>
              Run preflight
            </Button>
          </FlexItem>
          <FlexItem>
            <Button variant="primary" onClick={save} isLoading={saving} isDisabled={saving}>
              Save
            </Button>
          </FlexItem>
        </Flex>
      }
    >
      {preflight && (
        <PageSection hasBodyWrapper={false}>
          <Alert
            variant={preflight.ok ? "success" : "warning"}
            isInline
            title={
              preflight.ok
                ? "Preflight passed — this mapping can drive plan generation"
                : "Preflight found gaps"
            }
          >
            {preflight.ok ? (
              "Every source network and datastore resolves to something that exists on the target cluster."
            ) : (
              <>
                {gapList("Unmapped networks", preflight.unmapped_networks)}
                {gapList("Unmapped datastores", preflight.unmapped_datastores)}
                {gapList(
                  "Networks missing from the target catalog",
                  preflight.missing_networks_on_target,
                )}
                {gapList(
                  "Storage classes missing from the target catalog",
                  preflight.missing_storage_classes_on_target,
                )}
                {gapList(
                  "Namespaces missing from the target catalog",
                  preflight.missing_namespaces_on_target,
                )}
              </>
            )}
          </Alert>
        </PageSection>
      )}

      <PageSection hasBodyWrapper={false}>
        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} aria-label="Mapping rows">
          <Tab
            eventKey="networks"
            title={<TabTitleText>{`Networks (${networks.length})`}</TabTitleText>}
          >
            <Card className="pf-v6-u-mt-md">
              <CardTitle>
                <Flex justifyContent={{ default: "justifyContentSpaceBetween" }}>
                  <FlexItem>Network mappings</FlexItem>
                  <FlexItem>
                    <Button
                      variant="link"
                      isInline
                      onClick={() => suggest("networks")}
                      isLoading={suggesting === "networks"}
                      isDisabled={Boolean(suggesting)}
                    >
                      Suggest with AI
                    </Button>
                  </FlexItem>
                </Flex>
              </CardTitle>
              <CardBody>
                {networks.length === 0 ? (
                  <Content component="p" className="pf-v6-u-color-200">
                    No source networks recorded yet. They appear once VM
                    inventory for this vCenter has been imported.
                  </Content>
                ) : (
                  <Table aria-label="Network mappings" variant="compact">
                    <Thead>
                      <Tr>
                        <Th width={25}>Source network</Th>
                        <Th width={30}>Target network</Th>
                        <Th width={20}>Namespace</Th>
                        <Th width={25}>Confidence</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {networks.map((row, i) => (
                        <Tr key={row.source_network ?? i}>
                          <Td dataLabel="Source network">{row.source_network}</Td>
                          <Td dataLabel="Target network">
                            <FormSelect
                              aria-label={`Target for ${row.source_network}`}
                              value={row.target_network_name ?? ""}
                              onChange={(_e, v) => {
                                const hit = catalog.networks.find((n) => n.name === v);
                                patchRow("networks", i, {
                                  target_network_name: v || null,
                                  target_network_type: hit?.network_type ?? null,
                                  target_namespace: hit?.namespace ?? row.target_namespace,
                                });
                              }}
                            >
                              <FormSelectOption value="" label="Not mapped" />
                              {catalog.networks.map((n) => (
                                <FormSelectOption
                                  key={n.id}
                                  value={n.name}
                                  label={`${n.name} (${n.network_type})`}
                                />
                              ))}
                            </FormSelect>
                          </Td>
                          <Td dataLabel="Namespace">
                            <TextInput
                              aria-label={`Namespace for ${row.source_network}`}
                              value={row.target_namespace ?? ""}
                              onChange={(_e, v) =>
                                patchRow("networks", i, { target_namespace: v || null })
                              }
                            />
                          </Td>
                          <Td dataLabel="Confidence">
                            <ConfidenceCell
                              confidence={row.confidence}
                              rationale={row.rationale}
                            />
                          </Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                )}
              </CardBody>
            </Card>
          </Tab>

          <Tab
            eventKey="storage"
            title={<TabTitleText>{`Storage (${storage.length})`}</TabTitleText>}
          >
            <Card className="pf-v6-u-mt-md">
              <CardTitle>
                <Flex justifyContent={{ default: "justifyContentSpaceBetween" }}>
                  <FlexItem>Storage mappings</FlexItem>
                  <FlexItem>
                    <Button
                      variant="link"
                      isInline
                      onClick={() => suggest("storage")}
                      isLoading={suggesting === "storage"}
                      isDisabled={Boolean(suggesting)}
                    >
                      Suggest with AI
                    </Button>
                  </FlexItem>
                </Flex>
              </CardTitle>
              <CardBody>
                {storage.length === 0 ? (
                  <Content component="p" className="pf-v6-u-color-200">
                    No source datastores recorded yet. They appear once VM
                    inventory for this vCenter has been imported.
                  </Content>
                ) : (
                  <Table aria-label="Storage mappings" variant="compact">
                    <Thead>
                      <Tr>
                        <Th width={25}>Source datastore</Th>
                        <Th width={30}>Target storage class</Th>
                        <Th width={20}>Access mode</Th>
                        <Th width={25}>Confidence</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {storage.map((row, i) => (
                        <Tr key={row.source_datastore ?? i}>
                          <Td dataLabel="Source datastore">{row.source_datastore}</Td>
                          <Td dataLabel="Target storage class">
                            <FormSelect
                              aria-label={`Storage class for ${row.source_datastore}`}
                              value={row.target_storage_class ?? ""}
                              onChange={(_e, v) => {
                                const hit = catalog.storageClasses.find((s) => s.name === v);
                                patchRow("storage", i, {
                                  target_storage_class: v || null,
                                  access_mode: hit?.access_mode ?? row.access_mode,
                                });
                              }}
                            >
                              <FormSelectOption value="" label="Not mapped" />
                              {catalog.storageClasses.map((s) => (
                                <FormSelectOption key={s.id} value={s.name} label={s.name} />
                              ))}
                            </FormSelect>
                          </Td>
                          <Td dataLabel="Access mode">
                            <FormSelect
                              aria-label={`Access mode for ${row.source_datastore}`}
                              value={row.access_mode ?? ""}
                              onChange={(_e, v) =>
                                patchRow("storage", i, { access_mode: v || null })
                              }
                            >
                              <FormSelectOption value="" label="Inherit" />
                              {ACCESS_MODES.map((m) => (
                                <FormSelectOption key={m} value={m} label={m} />
                              ))}
                            </FormSelect>
                          </Td>
                          <Td dataLabel="Confidence">
                            <ConfidenceCell
                              confidence={row.confidence}
                              rationale={row.rationale}
                            />
                          </Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                )}
              </CardBody>
            </Card>
          </Tab>
        </Tabs>

        <Content component="small" className="pf-v6-u-color-200 pf-v6-u-mt-md pf-v6-u-display-block">
          AI suggestions populate the form only. Nothing is written until you
          press Save — review the confidence and rationale on each row first.
        </Content>
      </PageSection>
    </PageFrame>
  );
}
