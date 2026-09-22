# Migratability Assessment

Answers, from an RVTools export and before anyone books a cutover window:
**can this VM migrate with MTV, and what will it lose on the way?**

Deterministic Python — `backend/app/core/assessment.py`. There is no LLM in
this path: a verdict that lands in a change record must trace to a rule.

## Where the rules come from

The rules mirror the VMware validation policies MTV itself runs
(`kubev2v/forklift` v2.12.1 — see [MTV-GROUNDING.md §10](MTV-GROUNDING.md)),
and reuse MTV's **concern ids, categories and assessment text**. A finding
here is the same finding the operator will see in the MTV console when the
Plan is created, under the same name — just weeks earlier.

`vv.*` ids are VirtValidate's own, each derived from a documented MTV
limitation that has no forklift policy:

| id | Source |
|---|---|
| `vv.disk.nvme.detected` (Critical) | MTV docs: "does not support migrating VMware NVMe disks" |
| `vv.firmware.secure_boot` (Warning) | MTV-1548 — Secure Boot VMs may not boot on the destination |
| `vv.os.windows2012.no_virtio` (Warning) | MTV 2.12 known issue — no virtio drivers for Server 2012 R2 |

## Status

| Status | Meaning | Shown as |
|---|---|---|
| `blocked` | any **Critical** finding — MTV will refuse the VM as-is | Cannot migrate |
| `warning` | any **Warning** — migrates, but something is lost or needs work | Needs attention |
| `ok` | nothing above Information | Ready |
| `unknown` | no RVTools facts (VM added by hand) | Not assessed |

`ok` means *no rule that could run found anything*. It does not mean MTV
will find nothing — see the next section.

## "Not evaluated" is part of the result

An RVTools export cannot answer every MTV policy. Each assessment lists the
rules that could not run and why, per VM, and `GET /api/assessment/summary`
totals them. Today:

- **Never evaluable from the sheets read:** vTPM, PCI/SCSI passthrough, USB
  controllers, SR-IOV, CPU / NUMA affinity, DRS / DPM, and the
  vSphere-6/7-to-FIPS-cluster rule (needs `vSource` + a FIPS flag on the target).
- **Evaluable only with the right sheet:** RDM, independent, shared and NVMe
  disks need `vDisk`; hot-add needs `vCPU` / `vMemory`. A `vInfo`-only export
  reports these as not evaluated rather than clean.

MTV still checks all of them on the cluster. The bundle README tells the
operator to review MTV's concerns before starting each wave.

## Where it shows up

| Surface | What |
|---|---|
| Import | every VM is assessed as it is written; re-import re-assesses |
| Inventory | **Migratability** column + filter; `?assessment_status=blocked`, `?assessment_finding=<id>` |
| VM page → Migratability tab | each finding: MTV's text, **what to do**, evidence, concern id; expandable not-evaluated list |
| `GET /api/assessment/summary` | fleet counts by status and by finding (filter by vCenter / environment) |
| `POST /api/assessment/run` | re-assess everything after a rule change (idempotent) |
| `POST /api/plans` | **gate**: a Critical finding — or, for a warm plan, a warm-only finding such as CBT disabled — returns 422 naming the VMs. `override_assessment: true` bypasses it and is audited (`plan.assessment_overridden`) |
| Plan page, per wave | "Before you start this wave" — findings rolled up across the wave's VMs. Warm-only findings are omitted from a cold plan |

## Adding a rule

1. Confirm the behaviour in a source and add it to `MTV-GROUNDING.md` §10.
2. If it needs a new RVTools column, add the alias in
   `app/core/rvtools_parser.py` and put the value in `hardware_facts`.
3. Add a function in `app/core/assessment.py`:

   ```python
   @_rule
   def my_rule(f: VMFacts):
       if <cannot tell from the data>:
           return Skipped("<id>", "<label>", "<why>")
       if <condition>:
           return Finding("<id>", WARNING, "<label>", "<MTV's text>",
                          "<what the operator should do>", {<evidence>})
       return None   # evaluated, clean
   ```

   Use MTV's concern id when one exists. Every finding needs a remediation.
   Set `applies_to="warm"` if it only matters for warm migration.
4. Bump `RULESET_VERSION`, add a row to the parametrized test in
   `tests/test_assessment.py`, and run `POST /api/assessment/run` after deploy.

No migration is needed — findings are a JSON document on the VM.
