// Agent activity — what the appliance actually did.
//
// Two read-only logs behind tabs:
//
//  * Commands — every SSH command run against a managed host. The agent
//    SSHes into production, so this is the evidence trail: every command
//    passes app.core.ssh_guard.assert_read_only at a single choke point,
//    and blocked attempts are recorded too. A blocked row is the
//    interesting one, so it's called out rather than shown as a flag.
//
//  * Inference — every LLM call, with the method that produced the
//    result. "mechanical_fallback_auth" and "mechanical_fallback_guardrail"
//    are distinct from a plain fallback on purpose: they mean a key was
//    rejected or a detector fired, not that the model had a bad day.

import { useCallback, useEffect, useState } from "react";
import {
  Bullseye,
  ExpandableSection,
  Label,
  PageSection,
  Pagination,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
  Content,
} from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState, GuidedEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";

// How each inference `method` should read to an operator.
const METHOD_META = {
  llm: { color: "green", label: "LLM" },
  mechanical_fallback: { color: "orange", label: "Mechanical fallback" },
  mechanical_fallback_auth: { color: "red", label: "Fallback — auth rejected" },
  mechanical_fallback_guardrail: { color: "red", label: "Fallback — guardrail fired" },
};

function methodLabel(method) {
  const meta =
    METHOD_META[method] ??
    (String(method ?? "").startsWith("llm_retry")
      ? { color: "orange", label: method }
      : { color: "grey", label: method ?? "—" });
  return (
    <Label isCompact color={meta.color}>
      {meta.label}
    </Label>
  );
}

function useLog(endpoint) {
  const [state, setState] = useState({ items: [], total: 0, loading: true, error: null });
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true }));
    try {
      const qs = new URLSearchParams({
        skip: String((page - 1) * perPage),
        limit: String(perPage),
      });
      const data = await fetchJSON(`${endpoint}?${qs}`);
      setState({
        items: data?.items ?? [],
        total: data?.total ?? 0,
        loading: false,
        error: null,
      });
    } catch (error) {
      setState({ items: [], total: 0, loading: false, error });
    }
  }, [endpoint, page, perPage]);

  useEffect(() => {
    load();
  }, [load]);

  return { ...state, page, perPage, setPage, setPerPage, reload: load };
}

function LogPagination({ log }) {
  return (
    <Pagination
      itemCount={log.total}
      page={log.page}
      perPage={log.perPage}
      onSetPage={(_e, p) => log.setPage(p)}
      onPerPageSelect={(_e, pp) => {
        log.setPerPage(pp);
        log.setPage(1);
      }}
      isCompact
    />
  );
}

function CommandsTab() {
  const log = useLog("/api/command-audits");

  const body = () => {
    if (log.error) {
      return <ErrorEmptyState error={log.error} onRetry={log.reload} isRetrying={log.loading} />;
    }
    if (log.loading && log.items.length === 0) {
      return Array.from({ length: 6 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={6}>
            <Skeleton screenreaderText="Loading commands" />
          </Td>
        </Tr>
      ));
    }
    if (log.items.length === 0) {
      return (
        <Tr>
          <Td colSpan={6}>
            <Bullseye>
              <GuidedEmptyState
                title="No SSH commands recorded"
                body="Every command VirtValidate runs on a managed host is recorded here, including any that the read-only guard blocked. The log fills in once you capture a baseline or run a validation."
                primary={{ label: "Go to bulk operations", to: "/operations" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return log.items.map((row) => (
      <Tr key={row.id} isStriped={row.blocked}>
        <Td dataLabel="Started">
          {row?.started_at ? new Date(row.started_at).toLocaleString() : "—"}
        </Td>
        <Td dataLabel="Host">{row?.host ?? "—"}</Td>
        <Td dataLabel="Command">
          <code className="pf-v6-u-font-size-sm">{row?.command ?? "—"}</code>
        </Td>
        <Td dataLabel="Result">
          {row?.blocked ? (
            <Label isCompact color="red">
              Blocked by guard
            </Label>
          ) : (
            <StatusLabel
              kind="status"
              value={row?.exit_status === 0 ? "healthy" : "failed"}
            >
              {row?.exit_status === 0 ? "OK" : `exit ${row?.exit_status ?? "?"}`}
            </StatusLabel>
          )}
        </Td>
        <Td dataLabel="Duration">{row?.duration_ms != null ? `${row.duration_ms} ms` : "—"}</Td>
        <Td dataLabel="Output">
          {row?.stdout_truncated ? (
            <ExpandableSection toggleText="View" isIndented>
              <pre
                className="pf-v6-u-font-size-sm"
                style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}
              >
                {row.stdout_truncated}
              </pre>
              <Content component="small" className="pf-v6-u-color-200">
                {row.stdout_byte_count} bytes captured
                {row.stdout_sha256 ? ` · sha256 ${row.stdout_sha256.slice(0, 12)}…` : ""}
              </Content>
            </ExpandableSection>
          ) : (
            "—"
          )}
        </Td>
      </Tr>
    ));
  };

  return (
    <>
      <LogPagination log={log} />
      <Table aria-label="SSH command audit" variant="compact">
        <Thead>
          <Tr>
            <Th width={15}>Started</Th>
            <Th width={15}>Host</Th>
            <Th width={30}>Command</Th>
            <Th width={15}>Result</Th>
            <Th width={10}>Duration</Th>
            <Th width={15}>Output</Th>
          </Tr>
        </Thead>
        <Tbody>{body()}</Tbody>
      </Table>
    </>
  );
}

function InferenceTab() {
  const log = useLog("/api/inference-logs");

  const body = () => {
    if (log.error) {
      return <ErrorEmptyState error={log.error} onRetry={log.reload} isRetrying={log.loading} />;
    }
    if (log.loading && log.items.length === 0) {
      return Array.from({ length: 6 }).map((_, i) => (
        <Tr key={`sk-${i}`}>
          <Td colSpan={6}>
            <Skeleton screenreaderText="Loading inference log" />
          </Td>
        </Tr>
      ));
    }
    if (log.items.length === 0) {
      return (
        <Tr>
          <Td colSpan={6}>
            <Bullseye>
              <GuidedEmptyState
                title="No LLM calls recorded"
                body="Every inference call is captured here with its full input, output, and the method that produced the result — so you can tell an LLM answer from a mechanical fallback after the fact."
                primary={{ label: "Generate a plan", to: "/plans/new" }}
              />
            </Bullseye>
          </Td>
        </Tr>
      );
    }

    return log.items.map((row) => {
      const detections = row?.detections ?? null;
      return (
        <Tr key={row.id}>
          <Td dataLabel="Time">
            {row?.created_at ? new Date(row.created_at).toLocaleString() : "—"}
          </Td>
          <Td dataLabel="Operation">{row?.operation ?? "—"}</Td>
          <Td dataLabel="Model">
            {row?.model ?? "—"}
            <Content component="small" className="pf-v6-u-display-block pf-v6-u-color-200">
              {row?.backend_type ?? ""}
            </Content>
          </Td>
          <Td dataLabel="Method">{methodLabel(row?.method)}</Td>
          <Td dataLabel="Latency">{row?.latency_ms != null ? `${row.latency_ms} ms` : "—"}</Td>
          <Td dataLabel="Detail">
            <ExpandableSection toggleText="View" isIndented>
              {detections && Object.keys(detections).length > 0 && (
                <>
                  <Content component="p" className="pf-v6-u-font-weight-bold">
                    Guardrail detections
                  </Content>
                  <pre
                    className="pf-v6-u-font-size-sm"
                    style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
                  >
                    {JSON.stringify(detections, null, 2)}
                  </pre>
                </>
              )}
              <Content component="p" className="pf-v6-u-font-weight-bold">
                Output
              </Content>
              <pre
                className="pf-v6-u-font-size-sm"
                style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}
              >
                {row?.output_text || "(empty)"}
              </pre>
            </ExpandableSection>
          </Td>
        </Tr>
      );
    });
  };

  return (
    <>
      <LogPagination log={log} />
      <Table aria-label="LLM inference log" variant="compact">
        <Thead>
          <Tr>
            <Th width={15}>Time</Th>
            <Th width={20}>Operation</Th>
            <Th width={15}>Model</Th>
            <Th width={20}>Method</Th>
            <Th width={10}>Latency</Th>
            <Th width={20}>Detail</Th>
          </Tr>
        </Thead>
        <Tbody>{body()}</Tbody>
      </Table>
    </>
  );
}

export default function AgentActivityPage() {
  const [tab, setTab] = useState(0);

  return (
    <PageFrame
      title="Agent activity"
      description="Read-only evidence of what the appliance did — every SSH command it ran, and every LLM call it made."
      breadcrumbs={[{ label: "Administration" }, { label: "Agent activity" }]}
    >
      <PageSection hasBodyWrapper={false}>
        <Tabs activeKey={tab} onSelect={(_e, key) => setTab(key)} aria-label="Activity logs">
          <Tab eventKey={0} title={<TabTitleText>SSH commands</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <CommandsTab />
            </div>
          </Tab>
          <Tab eventKey={1} title={<TabTitleText>LLM inference</TabTitleText>}>
            <div className="pf-v6-u-mt-md">
              <InferenceTab />
            </div>
          </Tab>
        </Tabs>
      </PageSection>
    </PageFrame>
  );
}
