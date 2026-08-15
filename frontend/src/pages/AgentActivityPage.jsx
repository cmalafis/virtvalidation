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
//
// Both use expandable ROWS rather than an expander inside a cell: the
// payloads here are command output, prompts and JSON detections, and
// wrapping those into a 20%-wide column makes them unreadable.

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Bullseye,
  Content,
  Label,
  PageSection,
  Pagination,
  Skeleton,
  Tab,
  TabTitleText,
  Tabs,
} from "@patternfly/react-core";
import {
  ExpandableRowContent,
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
} from "@patternfly/react-table";

import PageFrame from "../common/PageFrame";
import StatusLabel from "../common/StatusLabel";
import { ErrorEmptyState, GuidedEmptyState } from "../common/EmptyStates";
import { fetchJSON } from "../utils/fetchJSON";
import { asArray } from "../utils/asArray";

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

const PRE_STYLE = {
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  margin: 0,
  padding: "var(--pf-t--global--spacer--sm)",
  background: "var(--pf-t--global--background--color--secondary--default)",
  borderRadius: "var(--pf-t--global--border--radius--small)",
};

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
        items: asArray(data),
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

/**
 * Shared frame for both logs.
 *
 * `columns` describes the header; `renderRow` returns the visible cells;
 * `renderDetail` returns the full-width expanded content (or null when a
 * row has nothing to expand).
 */
function LogTable({ log, label, columns, emptyState, renderRow, renderDetail }) {
  const [expanded, setExpanded] = useState(() => new Set());
  const colCount = columns.length + 1; // + the expand toggle column

  const toggle = (id) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const message = (node) => (
    <Tbody>
      <Tr>
        <Td colSpan={colCount}>{node}</Td>
      </Tr>
    </Tbody>
  );

  let content;
  if (log.error) {
    content = message(
      <ErrorEmptyState error={log.error} onRetry={log.reload} isRetrying={log.loading} />,
    );
  } else if (log.loading && log.items.length === 0) {
    content = (
      <Tbody>
        {Array.from({ length: 6 }).map((_, i) => (
          <Tr key={`sk-${i}`}>
            <Td colSpan={colCount}>
              <Skeleton screenreaderText={`Loading ${label}`} />
            </Td>
          </Tr>
        ))}
      </Tbody>
    );
  } else if (log.items.length === 0) {
    content = message(<Bullseye>{emptyState}</Bullseye>);
  } else {
    content = log.items.map((row, rowIndex) => {
      const detail = renderDetail(row);
      const isExpanded = expanded.has(row.id);
      return (
        <Tbody key={row.id} isExpanded={isExpanded}>
          <Tr>
            <Td
              expand={
                detail
                  ? {
                      rowIndex,
                      isExpanded,
                      onToggle: () => toggle(row.id),
                      expandId: `log-${row.id}`,
                    }
                  : undefined
              }
            />
            {renderRow(row)}
          </Tr>
          {detail && (
            <Tr isExpanded={isExpanded}>
              {/* Span every column so prompts, command output and JSON
                  detections get the full table width. */}
              <Td colSpan={colCount}>
                <ExpandableRowContent>{detail}</ExpandableRowContent>
              </Td>
            </Tr>
          )}
        </Tbody>
      );
    });
  }

  return (
    <>
      <LogPagination log={log} />
      <Table aria-label={label} variant="compact" isExpandable>
        <Thead>
          <Tr>
            <Th screenReaderText="Row expansion" />
            {columns.map((c) => (
              <Th key={c.label} width={c.width}>
                {c.label}
              </Th>
            ))}
          </Tr>
        </Thead>
        {content}
      </Table>
    </>
  );
}

function CommandsTab() {
  const log = useLog("/api/command-audits");

  return (
    <LogTable
      log={log}
      label="SSH command audit"
      columns={[
        { label: "Started", width: 20 },
        { label: "Host", width: 20 },
        { label: "Command", width: 35 },
        { label: "Result", width: 15 },
        { label: "Duration", width: 10 },
      ]}
      emptyState={
        <GuidedEmptyState
          title="No SSH commands recorded"
          body="Every command VirtValidate runs on a managed host is recorded here, including any that the read-only guard blocked. The log fills in once you capture a baseline or run a validation."
          primary={{ label: "Go to bulk operations", to: "/operations" }}
        />
      }
      renderRow={(row) => (
        <>
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
              <StatusLabel kind="status" value={row?.exit_status === 0 ? "healthy" : "failed"}>
                {row?.exit_status === 0 ? "OK" : `exit ${row?.exit_status ?? "?"}`}
              </StatusLabel>
            )}
          </Td>
          <Td dataLabel="Duration">
            {row?.duration_ms != null ? `${row.duration_ms} ms` : "—"}
          </Td>
        </>
      )}
      renderDetail={(row) =>
        row?.stdout_truncated ? (
          <>
            <pre className="pf-v6-u-font-size-sm" style={PRE_STYLE}>
              {row.stdout_truncated}
            </pre>
            <Content component="small" className="pf-v6-u-color-200 pf-v6-u-mt-sm pf-v6-u-display-block">
              {row.stdout_byte_count} bytes captured
              {row.stdout_sha256 ? ` · sha256 ${row.stdout_sha256.slice(0, 16)}…` : ""}
            </Content>
          </>
        ) : null
      }
    />
  );
}

function InferenceTab() {
  const log = useLog("/api/inference-logs");

  return (
    <LogTable
      log={log}
      label="LLM inference log"
      columns={[
        { label: "Time", width: 20 },
        { label: "Operation", width: 20 },
        { label: "Model", width: 25 },
        { label: "Method", width: 20 },
        { label: "Latency", width: 15 },
      ]}
      emptyState={
        <GuidedEmptyState
          title="No LLM calls recorded"
          body="Every inference call is captured here with its full input, output, and the method that produced the result — so you can tell an LLM answer from a mechanical fallback after the fact."
          primary={{ label: "Generate a plan", to: "/plans/new" }}
        />
      }
      renderRow={(row) => (
        <>
          <Td dataLabel="Time">
            {row?.created_at ? new Date(row.created_at).toLocaleString() : "—"}
          </Td>
          <Td dataLabel="Operation">{row?.operation ?? "—"}</Td>
          <Td dataLabel="Model">
            {row?.model || "—"}
            {row?.backend_type && (
              <Content component="small" className="pf-v6-u-display-block pf-v6-u-color-200">
                {row.backend_type}
              </Content>
            )}
          </Td>
          <Td dataLabel="Method">{methodLabel(row?.method)}</Td>
          <Td dataLabel="Latency">{row?.latency_ms != null ? `${row.latency_ms} ms` : "—"}</Td>
        </>
      )}
      renderDetail={(row) => {
        const detections = row?.detections ?? null;
        const hasDetections = Boolean(detections) && Object.keys(detections).length > 0;
        const isGuardrail = String(row?.method ?? "").includes("guardrail");

        return (
          <>
            {hasDetections ? (
              <>
                <Content component="p" className="pf-v6-u-font-weight-bold">
                  Guardrail detections
                </Content>
                <pre className="pf-v6-u-font-size-sm pf-v6-u-mb-md" style={PRE_STYLE}>
                  {JSON.stringify(detections, null, 2)}
                </pre>
              </>
            ) : (
              isGuardrail && (
                // Without this a blocked call expands to a bare
                // "(empty)", which reads as a broken UI rather than as
                // missing evidence.
                <Alert
                  variant="warning"
                  isInline
                  isPlain
                  title="No detection payload was recorded for this call"
                  className="pf-v6-u-mb-md"
                >
                  A detector blocked this call, but the structured detections
                  were not persisted on the log row.
                </Alert>
              )
            )}

            <Content component="p" className="pf-v6-u-font-weight-bold">
              Output
            </Content>
            <pre className="pf-v6-u-font-size-sm" style={PRE_STYLE}>
              {row?.output_text ||
                (isGuardrail
                  ? "No output — the call was blocked before the model answered."
                  : "(empty)")}
            </pre>
          </>
        );
      }}
    />
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
            <div className="pf-v6-u-mt-md">{tab === 0 && <CommandsTab />}</div>
          </Tab>
          <Tab eventKey={1} title={<TabTitleText>LLM inference</TabTitleText>}>
            <div className="pf-v6-u-mt-md">{tab === 1 && <InferenceTab />}</div>
          </Tab>
        </Tabs>
      </PageSection>
    </PageFrame>
  );
}
