Show the validation workflow as a visual pipeline in the dashboard so
operators always know where they are in the end-to-end migration loop.

## Steps to render

1. Discover / Enroll VMs
2. Capture Baselines
3. Generate Migration Plan
4. Execute Migration *(external — operator runs `oc apply` themselves)*
5. Validate Post-Migration
6. Generate Reports
7. Notify Stakeholders

## Per-step affordances

- Status indicator (idle / in-progress / done / failed) keyed off the
  same data the existing tabs already pull (audit log + plan state).
- Timing: when did the step start, how long did it take.
- Click-through to the relevant dashboard tab pre-filtered to the
  current plan/wave.
- A "blocked on operator" state for step 4 with copy-to-clipboard
  for the `oc apply` command.

## Why

Today the dashboard is tab-oriented (validation, plan, inventory,
reports, audit). Operators in long-running migrations lose track of
what's done vs. pending. A pipeline view answers "where am I?" in one
glance and surfaces the manual hand-off step (4) explicitly.

## Acceptance criteria

- [ ] New top-level dashboard section above the tabs (or a new tab —
  to be decided in design).
- [ ] No new backend data — derive everything from existing audit
  log + plan/wave state.
- [ ] Renders sensibly when no plan exists yet (empty-state pipeline
  with only step 1 actionable).
- [ ] WCAG-AA compliant — pipeline state must be conveyed by more
  than color alone (icons + text).
