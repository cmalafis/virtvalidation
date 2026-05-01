# Network Design Review

The Network Design Review feature surfaces gaps between the source VMware
networking environment and a proposed OpenShift Virtualization design,
running entirely on the appliance's local Llama 3 model.

This is **decision support, not authoritative validation.** Every finding
is meant to land in front of a network engineer for review. The analyzer
is honest about its limits — every finding carries a confidence level so
operators can prioritize the high-confidence items and triage the rest.

## What it does

For each review you submit, VirtValidate aggregates three inputs and runs
a single LLM pass that produces structured findings:

| Input | Source |
|---|---|
| Source VMware networks | Pulled from `VM.vsphere_networks` — populated by RVTools imports + baseline collection |
| Customer notes | Plain-English markdown / text describing zones, DMZ boundaries, traffic-flow rules, anything not in RVTools |
| Proposed OCP design | YAML manifests: `ClusterUserDefinedNetwork`, `NetworkAttachmentDefinition`, `NetworkPolicy`, Multus config |

The LLM produces findings in four categories:

- **Coverage gaps** — VLANs / portgroups in the source not represented in the proposed design.
- **Configuration mismatches** — MTU drift, VLAN tagging differences, IP space conflicts, security-zone separation lost in translation.
- **Missing resources** — Customer mentions DMZ but no DMZ NetworkPolicy; multi-NIC source VMs but the proposed design only has a single NAD.
- **Positive confirmations** — Things the design correctly handles. Pure-negative reports erode trust; this category exists so operators see what they got right.

Each finding includes a severity, source-evidence quote, proposed-evidence quote (or `(absent)` when nothing in the proposed design relates), specific recommendation, and a confidence level the LLM is encouraged to mark `low` whenever the inputs are too thin to reason cleanly.

## How to use it

### From the dashboard

1. Open the **Design Review** tab in the dashboard navigation.
2. Click **+ New review**.
3. Name the review (e.g. `Phase 1 cutover — DC-East prod tier`).
4. Upload or paste customer notes (markdown or plain text).
5. Upload or paste the proposed YAML manifests.
6. Leave **Run analysis after creating** checked → click **Create + analyze**.

The analysis runs as a background task — usually 30–90 seconds against a
local Llama 3 8B on a GPU. The detail page polls until the run completes,
then renders the findings grouped by category and ordered within each
category by severity.

### Triaging findings

Each finding has three triage states:

- **Open** — initial state; the operator hasn't looked at it yet.
- **Accepted** — the operator agrees with the finding and plans to act.
- **Dismissed** — the operator decided this doesn't apply (false positive, already addressed elsewhere). Dimmed in the UI but kept on the record so re-running the analysis doesn't surface the same finding twice.

Triage state persists across re-runs of the analysis if the finding is regenerated. (Today re-running replaces the entire finding set; future versions will preserve triage on identical title+category pairs.)

### Re-running analysis

The detail page's **Re-run analysis** button kicks off a fresh pass.
Common reasons to re-run:

- You updated the customer notes or proposed YAML.
- The analyzer model was upgraded (older Llama 3 → newer).
- A finding's recommendation surfaced new context worth re-checking.

Old findings are deleted before the new ones are written so the report
always reflects the most recent run.

### Exporting

The detail page renders the report in a print-friendly layout. Use the
**Export PDF** button — it triggers `window.print()`, and the embedded
`@media print` CSS swaps the dark theme for ink-saving black-on-white.
The chrome (back button, status pill, action buttons) hides during print.

## Limitations

These are surfaced in the UI in addition to being listed here:

- This is decision support, not authoritative validation.
- Cannot validate runtime networking behavior, only configuration.
- Customer notes quality determines analysis quality.
- Should be reviewed by a network engineer before action.
- The LLM may miss context not present in the provided artifacts.

## Explicitly out of scope

- **Auto-generating OpenShift YAML from the source environment.** Use the existing MTV YAML generator for the parts of the migration that translate cleanly; network design is too judgment-laden to fully automate.
- **Applying changes to clusters.** VirtValidate never touches the destination cluster. Operators apply YAML themselves with `oc apply`.
- **Runtime network testing.** No reachability probes, no packet captures.
- **BGP / dynamic routing analysis.** Static configuration only.

## Example workflows

### Greenfield migration with strong customer notes

1. RVTools export already loaded — VMs have `vsphere_networks` populated.
2. Customer hands you a 2-page markdown doc describing their zones, MTUs, and the DMZ boundary.
3. Your team drafts the proposed CUDN + NAD + NetworkPolicy YAML.
4. Create the review with all three inputs.
5. The analysis finds 8 findings — 1 critical (DMZ NetworkPolicy missing), 2 high (two portgroups uncovered), 3 medium (MTU drift), 2 positive confirmations.
6. The architect addresses the critical, accepts the highs as known gaps to fix in phase 2, and dismisses one medium that turned out to be intentional (jumbo frames on storage segment was already negotiated with the network team).

### Audit an inherited design

1. You're handed an existing CUDN/NAD set from a previous engagement.
2. Customer notes are minimal — VirtValidate's analysis runs at mostly `medium` / `low` confidence.
3. The report surfaces what the design *does* cover and, more importantly, what's *uncertain*.
4. The architect walks the customer through the low-confidence items in a follow-up call, captures the answers as updated notes, re-runs.
5. Second pass produces high-confidence findings and a clean report.

## API

For programmatic access:

```
POST   /api/network-reviews                       # create
GET    /api/network-reviews                       # list (with finding + severity counts)
GET    /api/network-reviews/{id}                  # full review with findings
PUT    /api/network-reviews/{id}/notes            # upload/update notes
PUT    /api/network-reviews/{id}/yaml             # upload/update YAML
POST   /api/network-reviews/{id}/analyze          # 202 + background analysis
PATCH  /api/network-reviews/{id}/findings/{fid}   # update triage state
DELETE /api/network-reviews/{id}                  # cascades findings
```

Audit log entries: `network_review.create`, `network_review.analyze_triggered`, `network_review.delete`.

See [`docs/architecture-diagram.html`](architecture-diagram.html) for the
full module map; the analyzer lives in `app/core/network_review.py`,
endpoints in `app/api/network_reviews.py`.
