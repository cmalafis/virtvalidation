// Resource mapping detail — the row-by-row editor.
//
// Three tabs: source network → target network, source datastore → target
// StorageClass, and the namespace strategy that decides where VMs land.
//
// Rows can be authored two ways, and both matter:
//
//   * "Pull from inventory" seeds them from the VMs already imported for
//     this vCenter.
//   * "Add row" takes a source name typed by hand. A site that has its
//     network and datastore list but can't produce an RVTools export
//     still needs to build a mapping, and the source side was never
//     constrained to inventory — it's free text all the way down.
//
// Nothing is written until Save: AI suggestions, added rows, deletions and
// the namespace strategy all mutate local state and go out as one PATCH.
// That distinction matters — a wrong network mapping sends a production VM
// onto the wrong VLAN.

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
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
  Grid,
  GridItem,
  Label,
  List,
  ListItem,
  PageSection,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
  TextInput,
  Tooltip,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";
import TrashIcon from "@patternfly/react-icons/dist/esm/icons/trash-icon";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

const CONFIDENCE_COLOR = { high: "green", medium: "orange", low: "red" };
const ACCESS_MODES = ["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"];

// Mirrors app.core.environment.Environment, minus UNKNOWN — there is no
// point offering a namespace for VMs whose environment never resolved.
const ENVIRONMENTS = [
  "production",
  "development",
  "test",
  "staging",
  "dr",
  "infrastructure",
  "db_only",
  "non_prod",
];

const DEFAULT_STRATEGY = {
  strategy: "per_environment",
  single_namespace: null,
  per_env_namespaces: {},
  per_app_prefix: "app",
};

// Rows are keyed by an ephemeral client id rather than by source name.
// Keying on the source name breaks the moment the name is editable: the
// key changes on every keystroke, React remounts the input, and focus is
// lost after one character.
let _rowSeq = 0;
const withKeys = (rows) =>
  asArray(rows).map((r) => ({ ...(r ?? {}), _key: (_rowSeq += 1) }));

// _key is bookkeeping — Pydantic would drop it anyway (update_mapping
// rebuilds rows from the validated model), but not sending it keeps the
// request honest about what it means.
const stripKeys = (rows) => asArray(rows).map(({ _key, ...rest }) => rest);

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
  const [pulling, setPulling] = useState(false);
  const [signals, setSignals] = useState(null);
  const [preflight, setPreflight] = useState(null);
  const [tab, setTab] = useState("networks");

  // Namespace strategy is edited in a draft so an untouched mapping isn't
  // silently given a strategy on save. Writing an empty-but-truthy dict
  // would suppress preflight's "no namespace mapping configured" warning
  // without actually resolving anything.
  const [nsDraft, setNsDraft] = useState(DEFAULT_STRATEGY);
  const nsDirty = useRef(false);

  const load = useCallback(async () => {
    try {
      const m = await fetchJSON(`/api/mappings/${id}`);
      setMapping({
        ...m,
        network_mappings: withKeys(m?.network_mappings),
        storage_mappings: withKeys(m?.storage_mappings),
      });
      nsDirty.current = false;
      const ns = m?.namespace_mappings;
      setNsDraft(ns && !Array.isArray(ns) ? { ...DEFAULT_STRATEGY, ...ns } : DEFAULT_STRATEGY);
      setError(null);

      // Catalogs drive the dropdowns; a mapping to something the cluster
      // doesn't have is exactly what preflight is meant to catch.
      const [nets, scs] = await Promise.allSettled([
        fetchJSON(`/api/ocp-targets/${m.ocp_target_id}/networks?limit=500`),
        fetchJSON(`/api/ocp-targets/${m.ocp_target_id}/storage-classes?limit=500`),
      ]);
      const items = (r) => (r.status === "fulfilled" ? asArray(r.value) : []);
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

  const KEYS = {
    networks: { list: "network_mappings", src: "source_network" },
    storage: { list: "storage_mappings", src: "source_datastore" },
  };

  const patchRow = (kind, index, patch) =>
    setMapping((m) => {
      const key = KEYS[kind].list;
      const rows = [...(m?.[key] ?? [])];
      rows[index] = { ...rows[index], ...patch };
      return { ...m, [key]: rows };
    });

  const addRow = (kind) =>
    setMapping((m) => {
      const { list, src } = KEYS[kind];
      return {
        ...m,
        [list]: [...(m?.[list] ?? []), { [src]: "", _key: (_rowSeq += 1) }],
      };
    });

  const removeRow = (kind, index) =>
    setMapping((m) => {
      const key = KEYS[kind].list;
      return { ...m, [key]: (m?.[key] ?? []).filter((_, i) => i !== index) };
    });

  const save = async () => {
    setSaving(true);
    try {
      const body = {
        network_mappings: stripKeys(mapping?.network_mappings),
        storage_mappings: stripKeys(mapping?.storage_mappings),
      };
      if (nsDirty.current) body.namespace_mappings = nsDraft;

      const updated = await fetchJSON(`/api/mappings/${id}`, { method: "PATCH", body });
      setMapping({
        ...updated,
        network_mappings: withKeys(updated?.network_mappings),
        storage_mappings: withKeys(updated?.storage_mappings),
      });
      nsDirty.current = false;
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
      const suggestions = asArray(result?.suggestions);

      // Merge onto existing rows by source key. Suggestions are proposals
      // — they populate the form but are not persisted until Save.
      setMapping((m) => {
        const { list, src } = KEYS[kind];
        const bySource = new Map(suggestions.map((s) => [s?.[src], s]));
        const rows = (m?.[list] ?? []).map((row) => {
          const hit = bySource.get(row[src]);
          return hit ? { ...row, ...hit, _key: row._key } : row;
        });
        return { ...m, [list]: rows };
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

  // Seed rows from the VMs already imported for this vCenter. Only adds
  // sources that aren't already present, so it's safe to press twice and
  // safe to mix with hand-authored rows.
  const pullFromInventory = async () => {
    setPulling(true);
    try {
      const data = await fetchJSON(`/api/mappings/${id}/source-signals`);
      setSignals(data ?? { networks: [], datastores: [] });

      // Computed here rather than inside the setMapping updater: React
      // invokes updaters lazily during render, so a counter incremented in
      // there is still zero when the toast below reads it.
      const next = { ...mapping };
      let added = 0;
      for (const [kind, field] of [
        ["networks", "networks"],
        ["storage", "datastores"],
      ]) {
        const { list, src } = KEYS[kind];
        const rows = [...(next[list] ?? [])];
        const seen = new Set(rows.map((r) => (r?.[src] ?? "").trim()).filter(Boolean));
        for (const sig of asArray(data?.[field])) {
          const name = (sig?.name ?? "").trim();
          if (!name || seen.has(name)) continue;
          seen.add(name);
          rows.push({ [src]: name, _key: (_rowSeq += 1) });
          added += 1;
        }
        next[list] = rows;
      }
      setMapping(next);

      toast.success(
        added > 0
          ? `${added} source(s) added to the form — set targets, then save.`
          : "No new sources found in inventory for this vCenter.",
      );
    } catch (e) {
      toast.error(e?.message ?? "Could not read inventory");
    } finally {
      setPulling(false);
    }
  };

  const runPreflight = async () => {
    try {
      setPreflight(await fetchJSON(`/api/mappings/${id}/preflight`, { method: "POST" }));
    } catch (e) {
      toast.error(e?.message ?? "Preflight failed");
    }
  };

  const patchNs = (patch) => {
    nsDirty.current = true;
    setNsDraft((d) => ({ ...d, ...patch }));
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
  const legacyNs = Array.isArray(mapping?.namespace_mappings)
    ? mapping.namespace_mappings
    : [];
  const hasLegacyNs = legacyNs.length > 0 && !nsDirty.current;

  // A source name that appears twice is silently deduped by every
  // consumer (coverage validation, status, preflight all build sets), so
  // the second row would look saved but do nothing.
  const duplicateSources = (rows, key) => {
    const counts = new Map();
    for (const r of rows) {
      const v = (r?.[key] ?? "").trim();
      if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
    }
    return counts;
  };
  const dupNet = duplicateSources(networks, "source_network");
  const dupStore = duplicateSources(storage, "source_datastore");

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

  // The target dropdowns are built from the operator-declared catalog. If
  // it's empty the select offers only "Not mapped", so the mapping can
  // never be completed and suggest-* returns 400. Say so, and link to the
  // page where those names are entered.
  const catalogAlert = (kindLabel, entries, anchor) =>
    entries.length === 0 ? (
      <Alert
        variant="info"
        isInline
        className="pf-v6-u-mb-md"
        title={`No ${kindLabel} declared on this cluster yet`}
      >
        Rows can be added here, but nothing can be mapped to until the cluster&apos;s{" "}
        {kindLabel} are declared.{" "}
        {mapping?.ocp_target_id ? (
          <Link to={`/sources/targets/${mapping.ocp_target_id}`}>
            Add them on the target cluster
          </Link>
        ) : (
          "Add them on the target cluster"
        )}
        , then come back. {anchor}
      </Alert>
    ) : null;

  const pullButton = (
    <Button
      variant="link"
      isInline
      onClick={pullFromInventory}
      isLoading={pulling}
      isDisabled={pulling || Boolean(suggesting)}
    >
      Pull from inventory
    </Button>
  );

  const rowActions = (kind, i, row, srcKey) => (
    <Td dataLabel="Actions" isActionCell>
      <Tooltip content={`Remove ${row?.[srcKey] || "this row"}`}>
        <Button
          variant="plain"
          aria-label={`Remove ${row?.[srcKey] || `row ${i + 1}`}`}
          icon={<TrashIcon />}
          onClick={() => removeRow(kind, i)}
        />
      </Tooltip>
    </Td>
  );

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
                {(preflight.warnings ?? []).length > 0 &&
                  gapList("Warnings", preflight.warnings)}
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
                  <Flex spaceItems={{ default: "spaceItemsLg" }}>
                    <FlexItem>
                      <Button variant="link" isInline onClick={() => addRow("networks")}>
                        Add row
                      </Button>
                    </FlexItem>
                    <FlexItem>{pullButton}</FlexItem>
                    <FlexItem>
                      <Button
                        variant="link"
                        isInline
                        onClick={() => suggest("networks")}
                        isLoading={suggesting === "networks"}
                        isDisabled={Boolean(suggesting) || networks.length === 0}
                      >
                        Suggest with AI
                      </Button>
                    </FlexItem>
                  </Flex>
                </Flex>
              </CardTitle>
              <CardBody>
                {catalogAlert("networks", catalog.networks, "")}
                {networks.length === 0 ? (
                  <Content component="p" className="pf-v6-u-color-200">
                    No network rows yet. Use <b>Add row</b> to type a source network by hand, or{" "}
                    <b>Pull from inventory</b> to seed them from the VMs already imported for
                    this vCenter.
                  </Content>
                ) : (
                  <Table aria-label="Network mappings" variant="compact">
                    <Thead>
                      <Tr>
                        <Th width={25}>Source network</Th>
                        <Th width={25}>Target network</Th>
                        <Th width={20}>Namespace</Th>
                        <Th width={20}>Confidence</Th>
                        <Th width={10} screenReaderText="Row actions" />
                      </Tr>
                    </Thead>
                    <Tbody>
                      {networks.map((row, i) => {
                        const src = (row.source_network ?? "").trim();
                        const isDup = src && dupNet.get(src) > 1;
                        return (
                          <Tr key={row._key ?? i}>
                            <Td dataLabel="Source network">
                              <TextInput
                                aria-label={`Source network for row ${i + 1}`}
                                value={row.source_network ?? ""}
                                placeholder="e.g. VM Network"
                                validated={isDup ? "error" : "default"}
                                onChange={(_e, v) =>
                                  patchRow("networks", i, { source_network: v })
                                }
                              />
                              {isDup && (
                                <Content
                                  component="small"
                                  className="pf-v6-u-display-block pf-v6-u-color-200"
                                >
                                  Duplicate source — only one row per source name takes effect.
                                </Content>
                              )}
                            </Td>
                            <Td dataLabel="Target network">
                              <FormSelect
                                aria-label={`Target for ${row.source_network || `row ${i + 1}`}`}
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
                                aria-label={`Namespace for ${row.source_network || `row ${i + 1}`}`}
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
                            {rowActions("networks", i, row, "source_network")}
                          </Tr>
                        );
                      })}
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
                  <Flex spaceItems={{ default: "spaceItemsLg" }}>
                    <FlexItem>
                      <Button variant="link" isInline onClick={() => addRow("storage")}>
                        Add row
                      </Button>
                    </FlexItem>
                    <FlexItem>{pullButton}</FlexItem>
                    <FlexItem>
                      <Button
                        variant="link"
                        isInline
                        onClick={() => suggest("storage")}
                        isLoading={suggesting === "storage"}
                        isDisabled={Boolean(suggesting) || storage.length === 0}
                      >
                        Suggest with AI
                      </Button>
                    </FlexItem>
                  </Flex>
                </Flex>
              </CardTitle>
              <CardBody>
                {catalogAlert("storage classes", catalog.storageClasses, "")}
                {storage.length === 0 ? (
                  <Content component="p" className="pf-v6-u-color-200">
                    No storage rows yet. Use <b>Add row</b> to type a source datastore by hand,
                    or <b>Pull from inventory</b> to seed them from the VMs already imported for
                    this vCenter.
                  </Content>
                ) : (
                  <Table aria-label="Storage mappings" variant="compact">
                    <Thead>
                      <Tr>
                        <Th width={25}>Source datastore</Th>
                        <Th width={25}>Target storage class</Th>
                        <Th width={20}>Access mode</Th>
                        <Th width={20}>Confidence</Th>
                        <Th width={10} screenReaderText="Row actions" />
                      </Tr>
                    </Thead>
                    <Tbody>
                      {storage.map((row, i) => {
                        const src = (row.source_datastore ?? "").trim();
                        const isDup = src && dupStore.get(src) > 1;
                        return (
                          <Tr key={row._key ?? i}>
                            <Td dataLabel="Source datastore">
                              <TextInput
                                aria-label={`Source datastore for row ${i + 1}`}
                                value={row.source_datastore ?? ""}
                                placeholder="e.g. tier1-ssd"
                                validated={isDup ? "error" : "default"}
                                onChange={(_e, v) =>
                                  patchRow("storage", i, { source_datastore: v })
                                }
                              />
                              {isDup && (
                                <Content
                                  component="small"
                                  className="pf-v6-u-display-block pf-v6-u-color-200"
                                >
                                  Duplicate source — only one row per source name takes effect.
                                </Content>
                              )}
                            </Td>
                            <Td dataLabel="Target storage class">
                              <FormSelect
                                aria-label={`Storage class for ${
                                  row.source_datastore || `row ${i + 1}`
                                }`}
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
                                aria-label={`Access mode for ${
                                  row.source_datastore || `row ${i + 1}`
                                }`}
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
                            {rowActions("storage", i, row, "source_datastore")}
                          </Tr>
                        );
                      })}
                    </Tbody>
                  </Table>
                )}
              </CardBody>
            </Card>
          </Tab>

          <Tab eventKey="namespaces" title={<TabTitleText>Namespaces</TabTitleText>}>
            <Card className="pf-v6-u-mt-md">
              <CardTitle>Namespace strategy</CardTitle>
              <CardBody>
                {hasLegacyNs ? (
                  <>
                    <Alert
                      variant="info"
                      isInline
                      className="pf-v6-u-mb-md"
                      title="This mapping uses the older per-criteria namespace rules"
                    >
                      They still resolve normally. Converting replaces them with a single
                      strategy — the rules below are shown for reference and are not editable
                      here.
                    </Alert>
                    <Table aria-label="Legacy namespace rules" variant="compact">
                      <Thead>
                        <Tr>
                          <Th>Criteria</Th>
                          <Th>Value</Th>
                          <Th>Target namespace</Th>
                        </Tr>
                      </Thead>
                      <Tbody>
                        {legacyNs.map((r, i) => (
                          <Tr key={i}>
                            <Td dataLabel="Criteria">{r?.criteria ?? "—"}</Td>
                            <Td dataLabel="Value">{r?.criteria_value ?? "—"}</Td>
                            <Td dataLabel="Target namespace">{r?.target_namespace ?? "—"}</Td>
                          </Tr>
                        ))}
                      </Tbody>
                    </Table>
                    <Button
                      variant="secondary"
                      className="pf-v6-u-mt-md"
                      onClick={() => patchNs({})}
                    >
                      Convert to a strategy
                    </Button>
                  </>
                ) : (
                  <Grid hasGutter>
                    <GridItem span={12}>
                      <Content component="p" className="pf-v6-u-color-200">
                        Decides which OpenShift namespace each VM lands in. Without one, VMs
                        fall back to the cluster default.
                      </Content>
                    </GridItem>
                    <GridItem span={6}>
                      <FormSelect
                        aria-label="Namespace strategy"
                        value={nsDraft.strategy ?? "per_environment"}
                        onChange={(_e, v) => patchNs({ strategy: v })}
                      >
                        <FormSelectOption value="single" label="Single namespace for every VM" />
                        <FormSelectOption
                          value="per_environment"
                          label="One namespace per environment"
                        />
                        <FormSelectOption
                          value="per_application"
                          label="One namespace per application"
                        />
                      </FormSelect>
                    </GridItem>

                    {nsDraft.strategy === "single" && (
                      <GridItem span={6}>
                        <TextInput
                          aria-label="Single namespace"
                          placeholder="migrated-vms"
                          value={nsDraft.single_namespace ?? ""}
                          onChange={(_e, v) => patchNs({ single_namespace: v || null })}
                        />
                      </GridItem>
                    )}

                    {nsDraft.strategy === "per_application" && (
                      <GridItem span={6}>
                        <TextInput
                          aria-label="Namespace prefix"
                          placeholder="app"
                          value={nsDraft.per_app_prefix ?? ""}
                          onChange={(_e, v) => patchNs({ per_app_prefix: v })}
                        />
                        <Content
                          component="small"
                          className="pf-v6-u-display-block pf-v6-u-color-200"
                        >
                          Namespaces are named &lt;prefix&gt;-&lt;application&gt;-vms. VMs with
                          no application hint land in unassigned-vms.
                        </Content>
                      </GridItem>
                    )}

                    {nsDraft.strategy === "per_environment" && (
                      <GridItem span={12}>
                        <Table aria-label="Namespace per environment" variant="compact">
                          <Thead>
                            <Tr>
                              <Th width={30}>Environment</Th>
                              <Th width={70}>Target namespace</Th>
                            </Tr>
                          </Thead>
                          <Tbody>
                            {ENVIRONMENTS.map((env) => (
                              <Tr key={env}>
                                <Td dataLabel="Environment">{env}</Td>
                                <Td dataLabel="Target namespace">
                                  <TextInput
                                    aria-label={`Namespace for ${env}`}
                                    placeholder="leave blank to skip"
                                    value={nsDraft.per_env_namespaces?.[env] ?? ""}
                                    onChange={(_e, v) => {
                                      const next = { ...(nsDraft.per_env_namespaces ?? {}) };
                                      if (v) next[env] = v;
                                      else delete next[env];
                                      patchNs({ per_env_namespaces: next });
                                    }}
                                  />
                                </Td>
                              </Tr>
                            ))}
                          </Tbody>
                        </Table>
                      </GridItem>
                    )}
                  </Grid>
                )}
              </CardBody>
            </Card>
          </Tab>
        </Tabs>

        {signals && (
          <Content
            component="small"
            className="pf-v6-u-color-200 pf-v6-u-mt-md pf-v6-u-display-block"
          >
            Inventory for this vCenter references {asArray(signals.networks).length} network(s)
            and {asArray(signals.datastores).length} datastore(s).
          </Content>
        )}

        <Content component="small" className="pf-v6-u-color-200 pf-v6-u-mt-md pf-v6-u-display-block">
          Added rows, deletions, AI suggestions and the namespace strategy populate the form
          only. Nothing is written until you press Save — review the confidence and rationale on
          each row first.
        </Content>
      </PageSection>
    </PageFrame>
  );
}
