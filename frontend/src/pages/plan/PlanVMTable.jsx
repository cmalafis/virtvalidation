// The VMs of a plan — one wave's worth, or all of them.
//
// Used twice on the plan detail page: inside each wave ("which VMs are in
// this wave") and as the flat "All VMs" view with a Wave column ("which
// wave is this VM in"). Both share one selection, owned by the page, so
// an operator can tick VMs across several waves and move them together.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Label, Pagination } from "@patternfly/react-core";
import { Table, Tbody, Td, Th, Thead, Tr } from "@patternfly/react-table";

import StatusLabel from "../../common/StatusLabel";
import { asArray } from "../../utils/asArray";

const PER_PAGE = 50;

// Sortable columns of the flat view, in column order (after the select
// cell). `null` = not sortable.
const SORT_KEYS = ["name", "wave_number", "environment", "ip_address", "ha_family", "role"];

function compare(a, b, key) {
  const x = a?.[key];
  const y = b?.[key];
  if (x == null && y == null) return 0;
  if (x == null) return 1; // blanks last
  if (y == null) return -1;
  if (typeof x === "number" && typeof y === "number") return x - y;
  return String(x).localeCompare(String(y), undefined, { numeric: true });
}

function BaselineCell({ row }) {
  if (row?.baseline_covered === true) {
    return (
      <Label isCompact color="green">
        Captured
      </Label>
    );
  }
  if (row?.baseline_covered === false) {
    return (
      <Label isCompact color="orange">
        Not in baseline
      </Label>
    );
  }
  return "—";
}

/**
 * @param rows         row objects from planRows.buildRows
 * @param selected     Map(vm_id → row), owned by the page
 * @param onToggle     (row, isOn) => void
 * @param onToggleMany (rows, isOn) => void  — header checkbox
 * @param canSelect    false hides the checkboxes (plan not editable)
 * @param showWave     add the Wave column and make the table sortable + paged
 * @param highlightId  vm id to mark and scroll to (deep link from elsewhere)
 */
export default function PlanVMTable({
  rows,
  selected,
  onToggle,
  onToggleMany,
  canSelect,
  showWave = false,
  highlightId = null,
  ariaLabel = "VMs",
}) {
  const [sort, setSort] = useState({ index: 0, direction: "asc" });
  const [page, setPage] = useState(1);
  const highlightRef = useRef(null);

  const ordered = useMemo(() => {
    const list = asArray(rows);
    if (!showWave) return list;
    const key = SORT_KEYS[sort.index] ?? "name";
    const sorted = [...list].sort((a, b) => compare(a, b, key));
    return sort.direction === "desc" ? sorted.reverse() : sorted;
  }, [rows, showWave, sort]);

  const pageCount = Math.max(1, Math.ceil(ordered.length / PER_PAGE));
  const currentPage = Math.min(page, pageCount);
  const visible = showWave
    ? ordered.slice((currentPage - 1) * PER_PAGE, currentPage * PER_PAGE)
    : ordered;

  // In the paged view, jump to the page holding the highlighted VM.
  useEffect(() => {
    if (!showWave || highlightId == null) return;
    const at = ordered.findIndex((r) => r?.vm_id === highlightId);
    if (at >= 0) setPage(Math.floor(at / PER_PAGE) + 1);
  }, [showWave, highlightId, ordered]);

  useEffect(() => {
    highlightRef.current?.scrollIntoView?.({ block: "center" });
  }, [highlightId, visible.length]);

  const selectable = visible.filter((r) => !r?.missing);
  const allSelected = selectable.length > 0 && selectable.every((r) => selected?.has(r.vm_id));

  const sortProps = (columnIndex) =>
    showWave
      ? {
          sort: {
            sortBy: sort,
            onSort: (_e, index, direction) => setSort({ index, direction }),
            columnIndex,
          },
        }
      : {};

  return (
    <>
      {showWave && ordered.length > PER_PAGE && (
        <Pagination
          itemCount={ordered.length}
          page={currentPage}
          perPage={PER_PAGE}
          onSetPage={(_e, p) => setPage(p)}
          isCompact
        />
      )}
      <Table aria-label={ariaLabel} variant="compact">
        <Thead>
          <Tr>
            {canSelect && (
              <Th
                select={{
                  onSelect: (_e, on) => onToggleMany?.(selectable, on),
                  isSelected: allSelected,
                }}
                aria-label={`Select all ${ariaLabel}`}
              />
            )}
            <Th {...sortProps(0)}>Name</Th>
            {showWave && <Th {...sortProps(1)}>Wave</Th>}
            <Th {...sortProps(2)}>Environment</Th>
            <Th {...sortProps(3)}>IP address</Th>
            <Th {...sortProps(4)}>HA family</Th>
            <Th {...sortProps(5)}>Role</Th>
            <Th>Assessment</Th>
            <Th>Baseline</Th>
          </Tr>
        </Thead>
        <Tbody>
          {visible.map((row) => {
            const isHighlighted = highlightId != null && row?.vm_id === highlightId;
            return (
              <Tr
                key={row.vm_id}
                isRowSelected={isHighlighted}
                ref={isHighlighted ? highlightRef : undefined}
              >
                {canSelect && (
                  <Td
                    select={{
                      rowIndex: row.vm_id,
                      isSelected: Boolean(selected?.has(row.vm_id)),
                      isDisabled: Boolean(row?.missing),
                      onSelect: (_e, on) => onToggle?.(row, on),
                    }}
                  />
                )}
                <Td dataLabel="Name">
                  {row?.missing ? (
                    <>
                      {row.name}{" "}
                      <Label isCompact color="red">
                        Deleted from inventory
                      </Label>
                    </>
                  ) : (
                    <Link to={`/vms/${row.vm_id}`}>{row.name}</Link>
                  )}
                </Td>
                {showWave && <Td dataLabel="Wave">{`Wave ${row?.wave_number ?? "?"}`}</Td>}
                <Td dataLabel="Environment">{row?.environment ?? "—"}</Td>
                <Td dataLabel="IP address">{row?.ip_address ?? "—"}</Td>
                <Td dataLabel="HA family">
                  {row?.ha_family ? (
                    <Label isCompact color="purple">
                      {`${row.ha_family} (${row?.ha_family_size ?? "?"})`}
                    </Label>
                  ) : (
                    "—"
                  )}
                </Td>
                <Td dataLabel="Role">{row?.role ?? "—"}</Td>
                <Td dataLabel="Assessment">
                  {row?.hasDetail && !row?.missing ? (
                    <StatusLabel kind="assessment" value={row?.assessment_status ?? "unknown"} />
                  ) : (
                    "—"
                  )}
                </Td>
                <Td dataLabel="Baseline">
                  <BaselineCell row={row} />
                </Td>
              </Tr>
            );
          })}
        </Tbody>
      </Table>
    </>
  );
}
