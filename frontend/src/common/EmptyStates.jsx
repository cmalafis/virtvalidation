// Guided empty states.
//
// The old empty states said things like "No VMs enrolled yet". These say
// what the resource IS, why it matters to the migration, and what to do
// next — so a first-run operator can learn the workflow by walking the
// app instead of reading the guide first.
//
// Each one names the NEXT step, which is what turns a set of screens
// into a sequence.

import { Link } from "react-router-dom";
import {
  Button,
  EmptyState,
  EmptyStateActions,
  EmptyStateBody,
  EmptyStateFooter,
  EmptyStateVariant,
  Spinner,
} from "@patternfly/react-core";
import CubesIcon from "@patternfly/react-icons/dist/esm/icons/cubes-icon";
import ExclamationCircleIcon from "@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon";
import SearchIcon from "@patternfly/react-icons/dist/esm/icons/search-icon";

/**
 * Generic guided empty state.
 *
 * @param primary   { label, to } or { label, onClick }
 * @param secondary same shape; rendered as a link-style button
 */
export function GuidedEmptyState({
  title,
  body,
  icon = CubesIcon,
  primary,
  secondary,
  variant = EmptyStateVariant.lg,
}) {
  const renderAction = (action, buttonVariant) => {
    if (!action) return null;
    if (action.to) {
      return (
        <Button variant={buttonVariant} component={(p) => <Link to={action.to} {...p} />}>
          {action.label}
        </Button>
      );
    }
    return (
      <Button variant={buttonVariant} onClick={action.onClick} isDisabled={action.isDisabled}>
        {action.label}
      </Button>
    );
  };

  return (
    <EmptyState titleText={title} headingLevel="h2" icon={icon} variant={variant}>
      <EmptyStateBody>{body}</EmptyStateBody>
      {(primary || secondary) && (
        <EmptyStateFooter>
          <EmptyStateActions>{renderAction(primary, "primary")}</EmptyStateActions>
          {secondary && (
            <EmptyStateActions>{renderAction(secondary, "link")}</EmptyStateActions>
          )}
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}

/** No rows because a filter excluded them — distinct from "nothing exists". */
export function NoResultsEmptyState({ onClearFilters }) {
  return (
    <EmptyState
      titleText="No results found"
      headingLevel="h2"
      icon={SearchIcon}
      variant={EmptyStateVariant.sm}
    >
      <EmptyStateBody>
        No items match the current filters. Try broadening or clearing them.
      </EmptyStateBody>
      {onClearFilters && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button variant="link" onClick={onClearFilters}>
              Clear all filters
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}

/** A request failed. Always offers a retry — never a dead end. */
export function ErrorEmptyState({ error, onRetry, isRetrying }) {
  return (
    <EmptyState
      titleText="Unable to load"
      headingLevel="h2"
      icon={ExclamationCircleIcon}
      status="danger"
      variant={EmptyStateVariant.sm}
    >
      <EmptyStateBody>
        {typeof error === "string" ? error : (error?.message ?? "Something went wrong.")}
      </EmptyStateBody>
      {onRetry && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button variant="primary" onClick={onRetry} isLoading={isRetrying} isDisabled={isRetrying}>
              {isRetrying ? "Retrying" : "Try again"}
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}

/** Centered loading state for a whole panel. */
export function LoadingEmptyState({ title = "Loading" }) {
  return (
    <EmptyState titleText={title} headingLevel="h2" icon={Spinner} variant={EmptyStateVariant.sm} />
  );
}

// ---------------------------------------------------------------------
// Per-resource copy. Centralized so the workflow narrative stays
// consistent — each one points at the next step in the sequence.
// ---------------------------------------------------------------------

export const NO_VCENTERS = {
  title: "No vCenter sources registered",
  body: "VirtValidate reads VM state over SSH and groups migration waves by source vCenter. Register the vCenter that owns the VMs you plan to migrate — this is the first step.",
};

export const NO_TARGETS = {
  title: "No OpenShift targets registered",
  body: "A target cluster is where your VMs land. Register the cluster, then add its networks, storage classes, and namespaces so migration plans can resolve real destinations.",
};

export const NO_MAPPINGS = {
  title: "No resource mappings defined",
  body: "A mapping translates vSphere networks and datastores into OpenShift NADs and StorageClasses. Plans cannot be generated until every selected VM resolves through a mapping.",
};

export const NO_VMS = {
  title: "No virtual machines in inventory",
  body: "Import your estate from an RVTools export, or add a VM by hand. Inventory is what everything else — baselines, plans, and validation — operates on.",
};

export const NO_PLANS = {
  title: "No migration plans yet",
  body: "A plan groups VMs into dependency-ordered waves you migrate together, and emits the MTV YAML to run them. Capture baselines first so validation has something to compare against.",
};

export const NO_VALIDATIONS = {
  title: "Nothing validated yet",
  body: "After a wave has migrated, validation SSHes into each VM and diffs its live state against the pre-migration baseline. Capture a baseline and migrate a wave to see results here.",
};

export const NO_DESIGN_REVIEWS = {
  title: "No design reviews yet",
  body: "A design review analyzes your proposed OpenShift network or storage design against the source estate and flags gaps before you migrate. Optional, but cheaper than finding the gap mid-cutover.",
};
