// Dry-run + commit for a manual plan edit.
//
// Every edit dialog works the same way: as soon as the operator has
// picked a target, ask the backend what the edit would do (`dry_run`),
// show the moves and any rule it breaks, and only then offer to commit.
// The dry run is the single source of truth for what is allowed — the
// dialogs do not re-implement planner rules.

import { useEffect, useState } from "react";

import { fetchJSON } from "../../utils/fetchJSON";
import { asArray } from "../../utils/asArray";

const DRY_RUN_DEBOUNCE_MS = 250;

/**
 * Debounced dry run of `body` against plan `planId`.
 *
 * `body` is the edit request without `dry_run` (or null while the
 * operator has not chosen a target yet). Returns `{ result, error,
 * loading }`; `result` is the PlanEditResponse.
 */
export function useEditPreview(planId, body) {
  const [state, setState] = useState({ result: null, error: null, loading: false });
  // Serialized so a re-render with an equal body does not refetch.
  const key = body ? JSON.stringify(body) : null;

  useEffect(() => {
    if (!planId || !key) {
      setState({ result: null, error: null, loading: false });
      return undefined;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    const timer = setTimeout(async () => {
      try {
        const result = await fetchJSON(`/api/plans/${planId}/edits`, {
          method: "POST",
          body: { ...JSON.parse(key), dry_run: true },
        });
        if (!cancelled) setState({ result, error: null, loading: false });
      } catch (e) {
        if (!cancelled) setState({ result: null, error: e, loading: false });
      }
    }, DRY_RUN_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [planId, key]);

  return state;
}

/** Apply an edit. Throws the fetchJSON error (with `.body`) on refusal. */
export function commitEdit(planId, body) {
  return fetchJSON(`/api/plans/${planId}/edits`, { method: "POST", body });
}

/** Violations carried by a refused commit (409 / 422), if any. */
export function violationsFromError(error) {
  return asArray(error?.body?.detail?.violations);
}

export function hasSeverity(result, severity) {
  return asArray(result?.violations).some((v) => v?.severity === severity);
}
