// The plan detail toolbar: find a VM, switch view, and act on a selection.
//
// Sticky, because the selection can span waves that are screens apart —
// the operator ticks VMs in wave 2 and wave 14 and the actions for them
// must still be within reach.

import {
  Button,
  Content,
  SearchInput,
  ToggleGroup,
  ToggleGroupItem,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
} from "@patternfly/react-core";

export default function PlanEditToolbar({
  search,
  onSearch,
  matchSummary,
  view,
  onView,
  onExpandAll,
  onCollapseAll,
  canEdit,
  selectedCount,
  onMoveToWave,
  onNewWave,
  onMoveToPlan,
  onRemove,
  onClear,
  onAdd,
}) {
  return (
    <Toolbar isSticky aria-label="Plan VMs toolbar" className="pf-v6-u-mb-md">
      <ToolbarContent>
        <ToolbarItem className="pf-v6-u-w-33">
          <SearchInput
            aria-label="Find a VM in this plan"
            placeholder="Find a VM by name, IP, app or family"
            value={search}
            onChange={(_e, v) => onSearch(v)}
            onClear={() => onSearch("")}
          />
        </ToolbarItem>
        <ToolbarItem>
          <ToggleGroup aria-label="How to list the plan's VMs">
            <ToggleGroupItem
              text="By wave"
              buttonId="plan-view-waves"
              isSelected={view === "waves"}
              onChange={() => onView("waves")}
            />
            <ToggleGroupItem
              text="All VMs"
              buttonId="plan-view-all"
              isSelected={view === "all"}
              onChange={() => onView("all")}
            />
          </ToggleGroup>
        </ToolbarItem>
        {view === "waves" && (
          <ToolbarGroup>
            <ToolbarItem>
              <Button variant="link" onClick={onExpandAll}>
                Expand all
              </Button>
            </ToolbarItem>
            <ToolbarItem>
              <Button variant="link" onClick={onCollapseAll}>
                Collapse all
              </Button>
            </ToolbarItem>
          </ToolbarGroup>
        )}
        {canEdit && (
          <ToolbarItem align={{ default: "alignEnd" }}>
            <Button variant="secondary" onClick={onAdd}>
              Add VMs
            </Button>
          </ToolbarItem>
        )}
      </ToolbarContent>

      {matchSummary && (
        <ToolbarContent>
          <ToolbarItem>
            <Content component="small" className="pf-v6-u-color-200">
              {matchSummary}
            </Content>
          </ToolbarItem>
        </ToolbarContent>
      )}

      {canEdit && selectedCount > 0 && (
        <ToolbarContent>
          <ToolbarItem>
            <Content component="p" className="pf-v6-u-font-weight-bold">
              {`${selectedCount} VM${selectedCount === 1 ? "" : "s"} selected`}
            </Content>
          </ToolbarItem>
          <ToolbarGroup>
            <ToolbarItem>
              <Button variant="primary" onClick={onMoveToWave}>
                Move to wave…
              </Button>
            </ToolbarItem>
            <ToolbarItem>
              <Button variant="secondary" onClick={onNewWave}>
                New wave from selection
              </Button>
            </ToolbarItem>
            <ToolbarItem>
              <Button variant="secondary" onClick={onMoveToPlan}>
                Move to plan…
              </Button>
            </ToolbarItem>
            <ToolbarItem>
              <Button variant="secondary" isDanger onClick={onRemove}>
                Remove from plan
              </Button>
            </ToolbarItem>
            <ToolbarItem>
              <Button variant="link" onClick={onClear}>
                Clear selection
              </Button>
            </ToolbarItem>
          </ToolbarGroup>
        </ToolbarContent>
      )}
    </Toolbar>
  );
}
