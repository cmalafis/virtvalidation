# VirtValidate API

**Auto-generated** from the FastAPI OpenAPI spec — re-run `python scripts/generate_api_docs.py` after route changes.

- **Version**: 0.1.0
- **Live spec**: `GET /openapi.json`
- **Swagger UI**: `/docs` (when the backend is running)

VM migration validation platform — air-gapped, local LLM

## Endpoints

### assessment

#### `POST /api/assessment/run`

_Run Assessment_

Re-assess every VM against the current rule set. Cheap and
deterministic (pure Python over stored facts); safe to repeat.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/assessment/summary`

_Assessment Summary_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_source_id` | query | no | array \| null |  |
| `environment` | query | no | array \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### audit

#### `GET /api/audit`

_List Audit_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `limit` | query | no | integer *(default: `100`)* |  |
| `action` | query | no | string \| null |  |
| `resource_type` | query | no | string \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[AuditLogRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### baseline-runs

#### `GET /api/baseline-runs/{run_id}`

_Get Baseline Run_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `run_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BaselineRunRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/baseline-runs/{run_id}/retry-failed`

_Retry Failed Baseline_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `run_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `BaselineRunAccepted` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### command-audits

#### `GET /api/command-audits`

_List Command Audits_

Paginated, filterable command-audit listing — newest first.

Returns ``{items, total, skip, limit}``. ``blocked=true`` isolates the
security-relevant rows where the read-only gate refused a command.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |
| `run_type` | query | no | array \| null |  |
| `run_id` | query | no | integer \| null |  |
| `vm_id` | query | no | integer \| null |  |
| `blocked` | query | no | boolean \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `CommandAuditListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### health

#### `GET /api/health/full`

_Full Health_

Combined status across the API and every backing dependency.

The new ``components`` block is the canonical shape going forward;
the top-level ``ollama`` / ``postgres`` keys are kept temporarily
for the old UI build that still reads them directly.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/health/llm`

_Llm Health_

Probe the configured LLM backend.

Routes through :meth:`LLMBackend.health_check_sync` so Ollama,
KServe, and vLLM all report through the same shape.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/health/ollama`

_Ollama Health_

Back-compat alias for ``/health/llm``.

Pre-refactor clients (older Settings UI builds) hit this path. The
response is the same backend-agnostic envelope so they degrade
gracefully when the deployment isn't actually using Ollama.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/health/postgres`

_Postgres Health_

Run a SELECT 1 against the configured database.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/health/schema`

_Schema Health_

Report the database's Alembic migration state.

Returns ``current_revision`` (what the DB thinks it's at),
``head_revision`` (what the migrations dir says is latest),
``is_up_to_date``, and ``pending_migrations`` (revisions between
current → head). Helps debug "is the database current" without
shell access — particularly useful in OCP where ``kubectl exec``
isn't always available.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

### imports

#### `GET /api/imports`

_List Imports_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ImportJobListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/imports/rvtools`

_Upload Rvtools_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `ImportJobRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/imports/{job_id}`

_Get Import_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `job_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ImportJobRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/imports/{job_id}/cancel`

_Cancel Import_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `job_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ImportJobRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/imports/{job_id}/rejects`

_List Rejects_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `job_id` | path | yes | string |  |
| `severity` | query | no | string \| null |  |
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ImportRejectListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/imports/{job_id}/rejects.csv`

_Download Rejects_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `job_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/imports/{job_id}/start`

_Start Import_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `job_id` | path | yes | string |  |

**Request body** (**required**): `ImportStartRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `ImportJobRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### inference-logs

#### `GET /api/inference-logs`

_List Inference Logs_

Paginated, filterable inference-log listing — newest first.

Returns ``{items, total, skip, limit}``; ``total`` is the count after
filters but before pagination, so the UI can render "Showing X-Y of Z".

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |
| `operation` | query | no | array \| null |  |
| `method` | query | no | array \| null |  |
| `backend_type` | query | no | array \| null |  |
| `vm_id` | query | no | integer \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `InferenceLogListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/inference-logs/stats`

_Inference Log Stats_

Roll-up counts by method and operation — feeds the LLM-health panel.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `operation` | query | no | array \| null |  |
| `method` | query | no | array \| null |  |
| `backend_type` | query | no | array \| null |  |
| `vm_id` | query | no | integer \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `InferenceLogStats` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### network-reviews

#### `GET /api/network-reviews`

_List Reviews_

Index. Sorted newest first; embeds the finding+severity counts so
the dashboard tab can render badges without a follow-up fetch.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[NetworkReviewSummary]` | Successful Response |

---

#### `POST /api/network-reviews`

_Create Review_

**Request body** (**required**): `NetworkReviewCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `NetworkReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/network-reviews/{review_id}`

_Delete Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/network-reviews/{review_id}`

_Get Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `NetworkReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/network-reviews/{review_id}/analyze`

_Analyze Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `NetworkReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/network-reviews/{review_id}/findings/{finding_id}`

_Update Finding Triage_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |
| `finding_id` | path | yes | integer |  |

**Request body** (**required**): `NetworkFindingTriageUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `NetworkFindingRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PUT /api/network-reviews/{review_id}/notes`

_Update Notes_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Request body** (**required**): `NetworkReviewNotesUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `NetworkReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PUT /api/network-reviews/{review_id}/yaml`

_Update Yaml_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Request body** (**required**): `NetworkReviewYamlUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `NetworkReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### ocp-targets

#### `GET /api/sources/targets`

_List Targets_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[OCPTargetRead]` | Successful Response |

---

#### `POST /api/sources/targets`

_Create Target_

**Request body** (**required**): `OCPTargetCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `OCPTargetRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/sources/targets/{target_id}`

_Delete Target_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}`

_Get Target_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/sources/targets/{target_id}`

_Update Target_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `OCPTargetUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### planning-strategies

#### `GET /api/planning-strategies`

_List Strategies_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[PlanningStrategyRead]` | Successful Response |

---

#### `POST /api/planning-strategies`

_Create Strategy_

**Request body** (**required**): `PlanningStrategyCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `PlanningStrategyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/planning-strategies/{strategy_id}`

_Delete Strategy_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `strategy_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/planning-strategies/{strategy_id}`

_Get Strategy_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `strategy_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanningStrategyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/planning-strategies/{strategy_id}`

_Update Strategy_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `strategy_id` | path | yes | integer |  |

**Request body** (**required**): `PlanningStrategyUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanningStrategyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### plans

#### `GET /api/plans`

_List Plans_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `limit` | query | no | integer *(default: `20`)* |  |
| `series_id` | query | no | string \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[PlanRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans`

_Create Plan_

Kick off async migration plan generation.

There is no selection cap. The selection is fanned out by
``(source_vcenter_id, target_cluster_id, target_namespace)``
partition, and each partition is cut into batches of at most
``settings.max_vms_per_plan`` VMs (``app.core.plan_chunking``).
Every resulting MigrationPlan row gets its own background
generation task (run sequentially, in series order) and its own
MTV CR set at YAML-export time. Rows from one POST share
``series_id``; names are ``base-1``, ``base-2`` … series-wide, with
the partition slug kept in the middle when the selection spans
several partitions. A selection that yields exactly one plan keeps
the user's name verbatim.

The only ceiling is ``limits.MAX_VMS_PER_PLAN_SCOPE`` on the
request body — a DoS guard, not a planning limit.

With ``skip_ineligible`` the VMs that cannot be planned as selected
(assessment-blocked for ``migration_type``, or unresolvable target)
are left out and reported in ``excluded`` / persisted as
``excluded_vms`` on every row instead of failing the whole call.

The response surfaces the first plan's PlanRead with a ``plans``
list of every row created so callers see the full set. On LLM
failure the verbatim typed-exception message lands in each plan's
``error_message``.

**Request body** (**required**): `PlanCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `PlanRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/preview`

_Preview Plan Partitions_

Show how the selection will fan out into plan rows without
creating any. ``groups`` is the per-``(source_vcenter,
target_cluster, target_namespace)`` partition view; ``plans`` is
the row-level view after batching — the exact names and VM counts
POST /api/plans would create — and ``excluded`` is every VM that
``skip_ineligible`` would leave out (assessment-blocked for
``migration_type`` ∪ ``unresolved`` ∪ Stage 0 mapping gaps against
the same mapping rows POST would use — ``mapping_ids`` semantics
match the create call).

Estimated waves is a ceiling: ``ceil(vm_count / max_vms_per_wave)``;
actual wave count depends on the mechanical assigner's pack +
HA-family split + concurrency analysis, which we don't run here
to keep /preview cheap (no LLM calls, no DB writes).

**Request body** (**required**): `PlanCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanPreviewResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/preview-groups`

_Preview Groups_

Show how the pre-classifier WOULD group these VMs — no LLM, no plan.

The plan wizard calls this before submission so the operator sees
the mechanical grouping (e.g. "57 VMs → 8 groups, web tier first,
db tier last") without paying the LLM round-trip. Tells operators
why specific VMs were clustered together, which is useful for
debugging both unexpected groupings and missing metadata.

**Request body** (**required**): `PlanCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PreviewGroupsResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/plans/{plan_id}`

_Delete Plan_

Delete a plan and return its VMs to ``available``.

Permitted for plans in any status. The lifecycle service is
idempotent on VMs already moved out of ``planned`` (operator-driven
revert flows might have done so) — those rows are skipped silently
rather than rolled back from ``migrated``.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}`

_Get Plan_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/edit-targets`

_List Edit Targets_

Other plans this plan's VMs can be moved into: status ``complete``
and the same ``(vCenter, cluster, namespace)`` partition. Same-series
batches come first. ``ineligible`` says why every other plan is not
offered, so "why can't I pick plan X?" has an answer on screen.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `EditTargetsResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/{plan_id}/edits`

_Edit Plan_

Move VMs between waves or plans, remove them, or add available VMs.

One operation over many VMs, atomic. With ``dry_run`` nothing is
written and the response (always 200) lists what would move and every
rule the edit would break — the UI calls this first and shows the
result before committing.

On commit:

  - 409 — a plan involved is not ``complete``, or the edit breaks an
    ``overridable`` rule and ``confirm_overrides`` was not sent.
  - 422 — the edit breaks a ``blocking`` rule (cross-partition move,
    mapping gap, a run in progress, …) or is malformed.

The touched waves are rebuilt deterministically in this request and
handed to the LLM for a rationale in the background.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Request body** (**required**): `PlanEditRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanEditResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/{plan_id}/mark-succeeded`

_Mark Plan Succeeded_

Operator declares the cutover finished; VMs transition to ``migrated``.

Plan must be in status ``complete`` (the new pipeline's terminal
success state) — refusing on any other status prevents marking a
half-generated plan as done. Idempotent on plans already in
``migrated`` (operator may double-click).

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/vms`

_List Plan Vms_

Every VM in the plan with the wave it is in.

Membership comes from the plan's waves; the per-VM facts are read
live from inventory, so a rename or a re-import shows up here without
regenerating the plan. Kept off ``GET /api/plans/{id}`` because that
payload is polled every few seconds during generation.

Returns 200 with empty ``items`` for a plan that has no waves yet.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PlanVMsResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/waves/{wave_number}/mtv-yaml`

_Wave Mtv Yaml_

Render the wave as a multi-doc MTV/Forklift YAML for ``oc apply -f``.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/{plan_id}/waves/{wave_number}/reannotate`

_Reannotate Wave_

Ask the LLM for a fresh rationale for one wave. The recovery path
when a background re-annotation was lost (appliance restart) or fell
back to deterministic text.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `ReannotateAccepted` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/waves/{wave_number}/report`

_Wave Report_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |
| `format` | query | no | string *(default: `json`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/waves/{wave_number}/report/pdf`

_Wave Report Pdf_

Dedicated PDF endpoint — always returns Content-Type: application/pdf.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/yaml`

_Plan Yaml Bundle_

Bundle every wave's MTV YAML for this plan into a single zip
download. Each file is one wave's NetworkMap + StorageMap + Plan
multi-doc YAML — same shape as the per-wave endpoint, just
aggregated for one-click apply.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### reports

#### `GET /api/reports/baseline-snapshot`

_Baseline Snapshot_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/reports/executive-summary`

_Executive Summary_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/reports/failed-degraded`

_Failed Degraded_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/reports/full-validation`

_Full Validation_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

#### `GET /api/reports/wave-plan`

_Wave Plan_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |

---

### resource-mappings

#### `GET /api/mappings`

_List Mappings_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[ResourceMappingRead]` | Successful Response |

---

#### `POST /api/mappings`

_Create Mapping_

**Request body** (**required**): `ResourceMappingCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `ResourceMappingRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/mappings/{mapping_id}`

_Delete Mapping_

Delete a resource mapping.

Returns 409 with the list of referencing plans when the mapping
is still in use. Previously this relied on
``Plan.mapping_id ON DELETE SET NULL`` which silently orphaned
plans — operators couldn't tell why their plan's MTV YAML export
suddenly emitted placeholder names. The 409 body matches the same
shape used by ``target_entities.delete_network`` so the frontend
can render referencing rows consistently.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/mappings/{mapping_id}`

_Get Mapping_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ResourceMappingRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/mappings/{mapping_id}`

_Update Mapping_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Request body** (**required**): `ResourceMappingUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ResourceMappingRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/mappings/{mapping_id}/preflight`

_Preflight_

Validate a mapping is ready to drive plan generation.

Reports four classes of failure:
  - Source resources without a mapping entry (unmapped_*)
  - Target resources referenced by the mapping but missing on
    the cluster (missing_*_on_target — drift)
  - Plus general warnings (target inactive, mapping flagged
    needs_review, no namespace mapping configured, etc.)

The endpoint never modifies the mapping; it's read-only.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `PreflightCheckResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/mappings/{mapping_id}/source-signals`

_Mapping Source Signals_

The distinct source networks/datastores this mapping's vCenter
scope references, with VM counts.

The mapping editor uses this to offer "pull from inventory" as a
starting point for rows. Rows can equally be authored by hand — a
site that has its network/datastore list but hasn't imported an
RVTools export still needs to build a mapping — so an empty result
is a normal 200, not a 404.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `MappingSourceSignals` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/mappings/{mapping_id}/suggest-network`

_Suggest Networks_

Ask the LLM to match source vSphere networks onto the operator's
declared :class:`TargetNetwork` rows for the mapping's target
cluster. The operator catalog is the only source of truth — the
appliance does not authenticate to clusters for live discovery.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `MappingSuggestionResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/mappings/{mapping_id}/suggest-storage`

_Suggest Storage_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `mapping_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `MappingSuggestionResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### rvtools

#### `POST /api/rvtools/upload-multi-vcenter`

_Upload Multi Vcenter_

Auto-link RVTools VMs to vCenter sources based on the parser's
detected hostname.

The pipeline:

  1. Group VMs by their ``source_vcenter_hostname``.
  2. Resolve each hostname to a vCenter id via the mapping (or
     the ``default_vcenter_id`` fallback).
  3. Per vCenter, run the existing :func:`run_rvtools_import`
     delta-import core. Returns a per-vCenter breakdown.
  4. Any VM whose hostname can't be resolved lands in ``skipped``
     with a specific reason. Partial-success is the default
     contract: a missing mapping does NOT fail the whole upload.

**Request body** (**required**): `MultiVCenterImportRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `MultiVCenterImportResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### settings

#### `GET /api/settings`

_Get Settings_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `AppSettingsRead` | Successful Response |

---

#### `PUT /api/settings`

_Update Settings_

**Request body** (**required**): `AppSettingsUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `AppSettingsRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/settings/llm`

_Get Llm Settings_

Snapshot of the active backend + the operator-pickable options.

Includes ``last_llm_error`` so the Settings UI can render a banner
when an auth failure was recently observed — operators discover
"your MaaS key was rejected" here rather than by noticing
annotations got worse.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `LLMSettingsRead` | Successful Response |

---

#### `PUT /api/settings/llm`

_Update Llm Settings_

Flip the active backend.

Refuses (422) when the requested backend's connection config is
incomplete — the UI gates the option but a direct API call would
otherwise produce a 500 on the next LLM call.

**Request body** (**required**): `LLMSettingsUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `LLMSettingsRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/settings/llm/test-connection`

_Test Llm Connection_

Probe a backend's reachability + auth + model availability.

Doesn't change the active selection — operators verify a backend
works before flipping the switch. On a successful probe, clears
``last_llm_error`` so a stale auth-failure banner doesn't linger
after the operator fixed the key.

**Request body** (**required**): `ConnectionTestRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ConnectionTestResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### snapshots

#### `POST /api/snapshots/capture-all`

_Capture All_

Spawn an immediate capture against every enrolled VM.

Returns the list of (vm_id, task_id) handles the dashboard polls.
VMs without a host or IP address are skipped — they would fail the
SSH leg anyway, and surfacing them up-front beats burying the failure
in per-task error fields.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkCaptureResult` | Successful Response |

---

#### `POST /api/snapshots/capture-bulk`

_Capture Bulk_

Spawn a rate-limited bulk capture against a selected fleet.

Returns 202 + task_id. Progress is polled via
``GET /api/snapshots/capture-bulk/{task_id}``. Per-VM failures
surface in the per_vm list; the bulk task itself completes
successfully even when individual VMs fail.

**Request body** (**required**): `BulkCaptureScope`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/snapshots/capture-bulk/{task_id}`

_Get Bulk Capture Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### ssh-keys

#### `GET /api/ssh-keys`

_List Ssh Keys_

Paginated key catalog. Filterable by status + plan_id.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |
| `status` | query | no | object \| null |  |
| `plan_id` | query | no | integer \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ssh-keys`

_Create Ssh Key_

Generate a new keypair, persist the metadata row, return the row.

The private key is written to the appliance PVC; only the public key
and fingerprint are surfaced in the response. The PRIVATE KEY BYTES
NEVER APPEAR HERE — see ``tests/test_ssh_key_api.py::test_create_does_not_leak_private_key``.

**Request body** (**required**): `SSHKeyCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `SSHKeyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ssh-keys/{key_id}`

_Get Ssh Key_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `key_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ssh-keys/{key_id}/playbook`

_Get Ssh Key Playbook_

Return the Ansible setup playbook with this key's public material
templated in, as ``text/yaml``.

The playbook file is delivered as part of the same repo (see
``deploy/ansible/setup-virtvalidate-user.yml``). If the file is
missing from the deployment, we return 503 so the operator sees a
deployment-issue surface rather than a confusing 500.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `key_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ssh-keys/{key_id}/public`

_Get Ssh Key Public_

Return the public key as ``text/plain`` for easy copy/paste.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `key_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `string` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ssh-keys/{key_id}/retire`

_Retire Ssh Key_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `key_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ssh-keys/{key_id}/revoke-from-vms`

_Revoke Ssh Key From Vms_

Day-2 cleanup: remove the key's public component from each listed
VM's ``~/.ssh/authorized_keys``.

The wave route ``/api/plans/{id}/waves/{n}/revoke-validation-key``
delegates here after resolving the wave's VMs. Per-VM failures
surface in the response but never abort the batch.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `key_id` | path | yes | integer |  |

**Request body** (**required**): `SSHKeyRevokeRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyRevokeResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### storage-reviews

#### `GET /api/storage-reviews`

_List Reviews_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[StorageReviewSummary]` | Successful Response |

---

#### `POST /api/storage-reviews`

_Create Review_

**Request body** (**required**): `StorageReviewCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `StorageReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/storage-reviews/{review_id}`

_Delete Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/storage-reviews/{review_id}`

_Get Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `StorageReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/storage-reviews/{review_id}/analyze`

_Analyze Review_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `StorageReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/storage-reviews/{review_id}/findings/{finding_id}`

_Update Finding Triage_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |
| `finding_id` | path | yes | integer |  |

**Request body** (**required**): `StorageFindingTriageUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `StorageFindingRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PUT /api/storage-reviews/{review_id}/notes`

_Update Notes_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Request body** (**required**): `StorageReviewNotesUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `StorageReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PUT /api/storage-reviews/{review_id}/yaml`

_Update Yaml_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `review_id` | path | yes | integer |  |

**Request body** (**required**): `StorageReviewYamlUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `StorageReviewRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### system

#### `GET /api/system/fips-status`

_Fips Status Endpoint_

FIPS 140-3 compliance posture — configured + detected + per-op status.

Read-only — FIPS is a deployment decision controlled by the
``FIPS_MODE`` env var. The Settings UI surfaces this to operators
so federal reviewers can audit the appliance's posture without
shelling into the host.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `FIPSStatus` | Successful Response |

---

#### `GET /api/system/llm-info`

_Llm Info_

Active LLM backend snapshot — what's wired up + live status.

Reads through the runtime resolver so this snapshot reflects the
DB-backed active selection (changeable from /settings/llm), not
the env-var bootstrap value. ``health.status`` reflects a live
probe so operators see whether the configured endpoint is
actually reachable.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `LLMBackendInfo` | Successful Response |

---

#### `GET /api/system/llm-usage`

_Llm Usage_

Rolled-up usage metrics for the admin dashboard.

Returns:
    - operation-level call counts + token totals over the last
      ``hours`` window
    - validation-specific tier distribution + cache hit rate
    - estimated USD cost (configurable rate; 0 by default for
      locally-hosted Ollama)

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `hours` | query | no | integer *(default: `24`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/system/ollama-models`

_Ollama Models_

List models available on the active LLM backend.

Endpoint name predates the multi-backend refactor; the response now
routes through the backend's ``list_models()`` so KServe deployments
return their model and Ollama deployments return everything pulled.
Each entry carries just the ``name`` so the UI's existing dropdown
works without changes.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OllamaModelsResponse` | Successful Response |

---

#### `GET /api/system/ssh-key`

_Ssh Key Status_

Return the appliance SSH key in a wrapped ``{status, ...}`` shape.

Distinguishes "missing" from "exists" without forcing the UI to
parse error bodies — the previous 404-on-missing contract made the
Settings page render a CLI command instead of a generate button.
The Settings UI's three-state SSH panel keys off this response.

"ssh.key_viewed" is recorded at INFO level so federal audit trails
can show every time the public key was surfaced (the audit
middleware's default-action map would emit ``api.get``, which
isn't specific enough for compliance review).

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `app__schemas__settings__SSHKeyStatus` | Successful Response |

---

#### `POST /api/system/ssh-key/generate`

_Ssh Key Generate_

Materialize a new keypair on the appliance.

Refuses when a key already exists — operators rotate via the
sibling endpoint, which audits the old/new fingerprint pair
instead of silently replacing.

**Request body** (**required**): `SSHKeyGenerateRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `SSHKeyGenerateResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/system/ssh-key/rotate`

_Ssh Key Rotate_

Replace the existing appliance keypair with a freshly generated one.

The old key is renamed to ``.bak.<unix-ts>`` (both private + public)
so a botched rotation can be rolled back. The audit log captures
old + new fingerprints so federal reviewers can correlate the
rotation with the authorized_keys update on every managed VM.

**Request body** (**required**): `SSHKeyGenerateRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyRotateResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/system/ssh-public-key`

_Ssh Public Key_

Return the OpenSSH public key VirtValidate uses. Never the private key.

Polymorphic across Ed25519 / RSA / ECDSA — federal deployments
running in FIPS mode use RSA-3072 or ECDSA P-384 and must still see
their public key here, not just Ed25519.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHPublicKey` | Successful Response |

---

### target-entities

#### `GET /api/ocp-targets/{target_id}/namespaces`

_List Namespaces_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ocp-targets/{target_id}/namespaces`

_Create Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `OCPTargetNamespaceCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/ocp-targets/{target_id}/namespaces/{ns_id}`

_Delete Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ocp-targets/{target_id}/namespaces/{ns_id}`

_Get Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/ocp-targets/{target_id}/namespaces/{ns_id}`

_Update Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Request body** (**required**): `OCPTargetNamespaceUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ocp-targets/{target_id}/networks`

_List Networks_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[TargetNetworkRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ocp-targets/{target_id}/networks`

_Create Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `TargetNetworkCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/ocp-targets/{target_id}/networks/{net_id}`

_Delete Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ocp-targets/{target_id}/networks/{net_id}`

_Get Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/ocp-targets/{target_id}/networks/{net_id}`

_Update Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Request body** (**required**): `TargetNetworkUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ocp-targets/{target_id}/storage-classes`

_List Storage Classes_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[TargetStorageClassRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/ocp-targets/{target_id}/storage-classes`

_Create Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `TargetStorageClassCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/ocp-targets/{target_id}/storage-classes/{sc_id}`

_Delete Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/ocp-targets/{target_id}/storage-classes/{sc_id}`

_Get Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/ocp-targets/{target_id}/storage-classes/{sc_id}`

_Update Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Request body** (**required**): `TargetStorageClassUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/namespaces`

_List Namespaces_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/targets/{target_id}/namespaces`

_Create Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `OCPTargetNamespaceCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/sources/targets/{target_id}/namespaces/{ns_id}`

_Delete Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/namespaces/{ns_id}`

_Get Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/sources/targets/{target_id}/namespaces/{ns_id}`

_Update Namespace_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `ns_id` | path | yes | integer |  |

**Request body** (**required**): `OCPTargetNamespaceUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OCPTargetNamespaceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/networks`

_List Networks_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[TargetNetworkRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/targets/{target_id}/networks`

_Create Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `TargetNetworkCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/sources/targets/{target_id}/networks/{net_id}`

_Delete Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/networks/{net_id}`

_Get Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/sources/targets/{target_id}/networks/{net_id}`

_Update Network_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `net_id` | path | yes | integer |  |

**Request body** (**required**): `TargetNetworkUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetNetworkRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/storage-classes`

_List Storage Classes_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[TargetStorageClassRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/targets/{target_id}/storage-classes`

_Create Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |

**Request body** (**required**): `TargetStorageClassCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/sources/targets/{target_id}/storage-classes/{sc_id}`

_Delete Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/targets/{target_id}/storage-classes/{sc_id}`

_Get Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/sources/targets/{target_id}/storage-classes/{sc_id}`

_Update Storage Class_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `target_id` | path | yes | integer |  |
| `sc_id` | path | yes | integer |  |

**Request body** (**required**): `TargetStorageClassUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `TargetStorageClassRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### templates

#### `GET /api/templates/csv`

_Download Csv Template_

Serve the canonical VM inventory CSV template as a download.

The file is shipped inside the container at the path configured by
``CSV_TEMPLATE_PATH`` (default ``/app/templates/vm-inventory-template.csv``).
Operators running the backend outside a container can point the env var
at the repo's ``docs/vm-inventory-template.csv``.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |

---

### validation-runs

#### `GET /api/validation-runs/{run_id}`

_Get Validation Run_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `run_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ValidationRunRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### validation-schedules

#### `GET /api/validation-schedules`

_List Schedules_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[ScheduleRead]` | Successful Response |

---

#### `POST /api/validation-schedules`

_Create Schedule_

**Request body** (**required**): `ScheduleCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `ScheduleRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/validation-schedules/{schedule_id}`

_Delete Schedule_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `schedule_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/validation-schedules/{schedule_id}`

_Get Schedule_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `schedule_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ScheduleRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/validation-schedules/{schedule_id}`

_Update Schedule_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `schedule_id` | path | yes | integer |  |

**Request body** (**required**): `ScheduleUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ScheduleRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/validation-schedules/{schedule_id}/fire`

_Fire Schedule_

Trigger a schedule immediately, bypassing its cron timing.

Same code path APScheduler invokes — useful for "validate now"
one-off runs from the operator dashboard.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `schedule_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### validations

#### `POST /api/validations/preview-tiers`

_Preview Tiers_

Estimate the LLM-call cost of a bulk validation before running.

Walks each matched VM through SSH-collect + diff + classifier (no
LLM call) and reports how many would land in Tier 1 (no LLM),
Tier 2 (rules, no LLM), or Tier 3 (LLM). Surfaces a per-VM error
list for VMs that can't be previewed (e.g. no baseline).

**Request body** (**required**): `BulkValidationScope`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/validations/run-all`

_Validate All_

Spawn a validation against every enrolled VM that has a baseline.

VMs without a baseline are skipped — running the LLM on a VM with no
prior state would just produce a "no baseline" finding, which is more
useful surfaced as a skip reason here.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkValidationResult` | Successful Response |

---

#### `POST /api/validations/run-bulk`

_Run Bulk_

Spawn a tier-aware bulk validation.

Returns 202 with a task handle the UI polls. Tier 1 / 2 VMs
complete instantly (no LLM call); Tier 3 VMs hit the cache or
the LLM and report cached vs fresh in the per-VM status.

**Request body** (**required**): `BulkValidationScope`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/validations/run-bulk/{task_id}`

_Get Bulk Validation Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### vcenters

#### `GET /api/sources/vcenters`

_List Vcenters_

Return every registered vCenter with its current VM count.

A single GROUP BY query handles the count so we don't N+1 across
the listing — important once a customer registers a few dozen
vCenters.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[VCenterSourceRead]` | Successful Response |

---

#### `POST /api/sources/vcenters`

_Create Vcenter_

**Request body** (**required**): `VCenterSourceCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `VCenterSourceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/vcenters/auto-match`

_Auto Match Vcenters_

Resolve a list of RVTools-detected vCenter hostnames to registered
VCenterSource rows.

Matching strategy:

  1. **Exact** — normalized hostname equals row.hostname (normalized).
  2. **Fuzzy** — the detected hostname is a prefix of a registered
     hostname (or vice versa). Covers ``vc-east-01`` vs
     ``vc-east-01.corp.local`` mismatches the operator routinely
     creates by trimming the FQDN on registration.

Returns:
    ``{matches: [{hostname, normalized, matched_vcenter_id,
                   matched_vcenter_name, confidence}],
       unmatched: [...]}``

**Request body** (**required**): `object`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/sources/vcenters/{vcenter_id}`

_Delete Vcenter_

Delete a vCenter source. Member VMs keep existing — their
``source_vcenter_id`` flips to NULL. Operators who want a full
purge follow up with a bulk VM delete; that's a deliberate
two-step to avoid silent fleet wipes.

We explicitly NULL the FK in a single UPDATE rather than relying
on the schema's ``ON DELETE SET NULL`` clause — the clause is
correct, but SQLite (used in tests) only honors FK actions when
``PRAGMA foreign_keys=ON`` and we'd rather not rely on that being
set everywhere. Explicit UPDATE works the same on every backend.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/vcenters/{vcenter_id}`

_Get Vcenter_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VCenterSourceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/sources/vcenters/{vcenter_id}`

_Update Vcenter_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |

**Request body** (**required**): `VCenterSourceUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VCenterSourceRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/vcenters/{vcenter_id}/categorize`

_Trigger Categorization_

Spawn a Level 1 categorization run as a BackgroundTask.

Returns 202 immediately with the task handle; the LLM batches run
after the response is sent so the dashboard can poll for progress.
Re-running on the same vCenter replaces existing groups — the
categorizer is idempotent on the same scope.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |
| `batch_size` | query | no | integer *(default: `10`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `CategorizationTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/vcenters/{vcenter_id}/categorize/{task_id}`

_Get Categorization Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `CategorizationTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/vcenters/{vcenter_id}/groups`

_List Groups_

List the groups Level 1 categorization produced for a vCenter.

Optional ``kind`` filter restricts to one axis (application /
environment / business_unit / other) — the dashboard renders the
three axes side-by-side and pulls each one separately.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |
| `kind` | query | no | object \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[CategorizationGroupRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/vcenters/{vcenter_id}/rvtools/import`

_Rvtools Import_

Persist an RVTools delta against a vCenter scope.

Same payload shape as ``/preview``. Behavior:

  - Under :data:`ASYNC_IMPORT_THRESHOLD` VMs (default 500): run
    synchronously, return the import counts inline.
  - At or above the threshold: spawn a BackgroundTask, return 202
    with a ``task_id`` the operator polls.

"Removed" rows aren't auto-deleted — they're flagged
``missing_from_last_upload=True`` and the operator decides whether
to decommission. This is a deliberate two-step (the same shape
as :func:`delete_vcenter`) to avoid silent fleet wipes.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |

**Request body** (**required**): `RVToolsDeltaRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/sources/vcenters/{vcenter_id}/rvtools/import/{task_id}`

_Get Rvtools Import Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `RVToolsImportTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/sources/vcenters/{vcenter_id}/rvtools/preview`

_Rvtools Delta Preview_

Compare an uploaded RVTools VM list against current inventory.

Match key: VM name within the vCenter scope. RVTools' own UUID
column would be more reliable but most customer exports lose it
after spreadsheet edits, so name-within-scope is the durable key.

Returns four buckets the UI renders so the operator confirms what
they're about to commit:

  - **new**       — name not in the vCenter's existing VMs
  - **updated**   — name matches but at least one tracked field
                    changed (with a per-field diff)
  - **removed**   — VM exists in the vCenter scope but not in the
                    upload (operator decides whether to mark
                    decommissioned or skip)
  - **unchanged** — exact match, no diff

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vcenter_id` | path | yes | integer |  |

**Request body** (**required**): `RVToolsDeltaRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `RVToolsDeltaResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### vms

#### `DELETE /api/vms`

_Delete Vms Bulk_

Delete multiple VMs in one round-trip.

Returns the lists of deleted vs not-found ids. Each successfully
deleted VM gets its own ``vm.delete`` audit row so per-VM forensics
still work after a bulk operation.

**Request body** (**required**): `BulkVMDelete`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkVMDeleteResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms`

_List Vms_

Paginated, filterable inventory listing.

Returns ``{items, total, skip, limit}``. ``total`` is the count
AFTER filters but BEFORE pagination — the data table needs both
"rows visible now" and "rows matching the filter" to render the
"Showing X-Y of Z" line and the pager.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `skip` | query | no | integer *(default: `0`)* |  |
| `limit` | query | no | integer *(default: `50`)* |  |
| `sort_by` | query | no | string ('name', 'status', 'lifecycle_state', 'environment', 'os_family', 'application_hint', 'source_vcenter_id', 'vsphere_cluster', 'power_state', 'provisioned_mb', 'created_at', 'updated_at') *(default: `name`)* |  |
| `sort_order` | query | no | string ('asc', 'desc') *(default: `asc`)* |  |
| `status` | query | no | array \| null |  |
| `lifecycle_state` | query | no | array \| null |  |
| `vcenter_source_id` | query | no | array \| null |  |
| `environment` | query | no | array \| null |  |
| `application_hint` | query | no | array \| null |  |
| `os_family` | query | no | array \| null |  |
| `classification_level` | query | no | array \| null |  |
| `vsphere_cluster` | query | no | array \| null |  |
| `power_state` | query | no | array \| null |  |
| `esxi_host` | query | no | array \| null |  |
| `network` | query | no | array \| null |  |
| `datastore` | query | no | array \| null |  |
| `missing_from_last_upload` | query | no | boolean \| null |  |
| `assessment_status` | query | no | array \| null |  |
| `assessment_finding` | query | no | array \| null |  |
| `search` | query | no | string \| null |  |
| `offset` | query | no | integer \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms`

_Create Vm_

**Request body** (**required**): `VMCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `VMRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `DELETE /api/vms/all`

_Delete All Vms_

Bulk-delete every VM that matches the given filters.

``?confirm=true`` is mandatory — without it the call 400s before
touching the DB. Filters are optional: with none, the endpoint
clears the whole inventory; with some, it clears only the matching
subset (the UI passes the user's current filter set so "Delete
all" matches what the operator sees).

One audit row is recorded summarizing the deletion (count + filter
fingerprint). Per-VM forensics for bulk deletes already live on
individual ``vm.delete`` rows when smaller operations are used;
the federal compliance review accepts the summary row for "clear
inventory" sweeps as long as the trail names the actor + filters.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `confirm` | query | no | boolean *(default: `False`)* |  |
| `status` | query | no | array \| null |  |
| `lifecycle_state` | query | no | array \| null |  |
| `vcenter_source_id` | query | no | array \| null |  |
| `environment` | query | no | array \| null |  |
| `application_hint` | query | no | array \| null |  |
| `os_family` | query | no | array \| null |  |
| `classification_level` | query | no | array \| null |  |
| `vsphere_cluster` | query | no | array \| null |  |
| `power_state` | query | no | array \| null |  |
| `esxi_host` | query | no | array \| null |  |
| `network` | query | no | array \| null |  |
| `datastore` | query | no | array \| null |  |
| `missing_from_last_upload` | query | no | boolean \| null |  |
| `assessment_status` | query | no | array \| null |  |
| `assessment_finding` | query | no | array \| null |  |
| `search` | query | no | string \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `DeleteAllVMsResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk`

_Create Vms Bulk_

Best-effort batch enrollment.

Pre-loads existing VM names so duplicates don't trigger per-row
IntegrityErrors. Returns the created rows and a list of names that were
skipped (with reason). Within-batch duplicates are also caught.

**Request body** (**required**): `BulkVMCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkVMResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk-clear-target-cluster`

_Bulk Clear Target Cluster_

**Request body** (**required**): `BulkClearTargetRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkTargetOverrideResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk-clear-target-namespace`

_Bulk Clear Target Namespace_

**Request body** (**required**): `BulkClearTargetRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkTargetOverrideResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk-set-environment`

_Bulk Set Environment_

Atomically set the environment of N VMs.

All-or-nothing: if any vm_id is missing, the whole call fails
with 404 and nothing is persisted. The audit log records the
bulk operation + the rationale so federal reviewers can trace
why a chunk of inventory was relabeled.

**Request body** (**required**): `BulkEnvironmentSetRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkEnvironmentSetResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk-set-target-cluster`

_Bulk Set Target Cluster_

**Request body** (**required**): `BulkSetTargetClusterRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkTargetOverrideResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/bulk-set-target-namespace`

_Bulk Set Target Namespace_

**Request body** (**required**): `BulkSetTargetNamespaceRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BulkTargetOverrideResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/facets`

_Vm Facets_

Per-dimension counts so the filter UI can show "Production (600)".

Filters apply to every dimension uniformly — the filter UI gets
"what would each value's count become if I clicked it on top of
my current selection?" semantics. (This differs from the Kibana-
style "exclude self" facets; we accept the simpler model because
the inventory's filter dimensions are largely orthogonal.)

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `status` | query | no | array \| null |  |
| `lifecycle_state` | query | no | array \| null |  |
| `vcenter_source_id` | query | no | array \| null |  |
| `environment` | query | no | array \| null |  |
| `application_hint` | query | no | array \| null |  |
| `os_family` | query | no | array \| null |  |
| `classification_level` | query | no | array \| null |  |
| `vsphere_cluster` | query | no | array \| null |  |
| `power_state` | query | no | array \| null |  |
| `esxi_host` | query | no | array \| null |  |
| `network` | query | no | array \| null |  |
| `datastore` | query | no | array \| null |  |
| `missing_from_last_upload` | query | no | boolean \| null |  |
| `assessment_status` | query | no | array \| null |  |
| `assessment_finding` | query | no | array \| null |  |
| `search` | query | no | string \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMFacetsResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/ids`

_List Vm Ids_

Every ``{id, name}`` matching the filter set — unpaginated.

Backs "Select all N matching" in the plan wizard: the same
querystring that rendered the page returns the ids the operator
would get by paging through it and ticking every row. Ordered by
name then id so the result is stable across calls.

Bounded by ``MAX_VMS_PER_BULK_ACTION`` (422 above it) so a filter
that matches an entire 50k-VM estate can't ship a multi-megabyte
body; the hint tells the operator to narrow by vCenter.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `status` | query | no | array \| null |  |
| `lifecycle_state` | query | no | array \| null |  |
| `vcenter_source_id` | query | no | array \| null |  |
| `environment` | query | no | array \| null |  |
| `application_hint` | query | no | array \| null |  |
| `os_family` | query | no | array \| null |  |
| `classification_level` | query | no | array \| null |  |
| `vsphere_cluster` | query | no | array \| null |  |
| `power_state` | query | no | array \| null |  |
| `esxi_host` | query | no | array \| null |  |
| `network` | query | no | array \| null |  |
| `datastore` | query | no | array \| null |  |
| `missing_from_last_upload` | query | no | boolean \| null |  |
| `assessment_status` | query | no | array \| null |  |
| `assessment_finding` | query | no | array \| null |  |
| `search` | query | no | string \| null |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMIdListResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/redetect-environment`

_Redetect Environment_

Re-run the detection cascade on the fleet (or one vCenter).

Skips VMs marked ``user_set`` by default so operator overrides
survive bulk re-runs. ``force=true`` overrides that protection;
``dry_run=true`` returns the projected changes without
persisting. Federal audit trails record the redetect operation
summary so reviewers can reconstruct which labels are pre- vs
post-redetect.

**Request body** (**required**): `RedetectEnvironmentRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `RedetectEnvironmentResult` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/stats`

_Vm Stats_

Cheap dashboard counters — no filter set, no row reads.

The dashboard's "Total VMs" tile used to reuse the paginated list
response, which under the new contract returns only the current
page. This endpoint exists so the tile can stay accurate without
pulling every row.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMStats` | Successful Response |

---

#### `DELETE /api/vms/{vm_id}`

_Delete Vm_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `204` | `—` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}`

_Get Vm_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/vms/{vm_id}`

_Update Vm_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Request body** (**required**): `VMUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/assessment`

_Get Vm Assessment_

Full migratability findings for one VM: what MTV will flag, what
is lost, how to fix it, and which rules could not be evaluated.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `object` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/baseline/history`

_Baseline History_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[SnapshotRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/baseline/profile`

_Baseline Profile_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `BaselineProfile` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/{vm_id}/capture`

_Trigger Capture_

Spawn an immediate baseline collection in the background.

Returns 202 with the task handle immediately; the actual SSH session
runs after the response is sent so the dashboard can poll for status.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `CaptureTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/capture/{task_id}`

_Get Capture Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `CaptureTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `PATCH /api/vms/{vm_id}/environment`

_Set Vm Environment_

Operator override of one VM's environment.

Marks ``environment_source="user_set"`` so the next redetect
run leaves the value alone. Audit-logged for compliance.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Request body** (**required**): `EnvironmentSetRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `VMRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/snapshots`

_List Snapshots_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[SnapshotRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/{vm_id}/snapshots`

_Create Snapshot_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Request body** (**required**): `SnapshotCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `SnapshotRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/snapshots/{snapshot_id}`

_Get Snapshot_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |
| `snapshot_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SnapshotRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/vms/{vm_id}/validate`

_Trigger Validation_

Spawn an immediate post-migration validation in the background.

Returns 202 with the task handle. The actual SSH+LLM round-trip runs
after the response is sent so the UI can poll for progress. A VM with
no baseline is rejected up-front (400) — running the diff engine
against an empty baseline produces a meaningless verdict.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `ValidationTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/validate/{task_id}`

_Get Validation Status_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |
| `task_id` | path | yes | string |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ValidationTaskRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/vms/{vm_id}/validation/latest`

_Latest Validation_

Return the latest validation result for a VM.

"No validation yet" is a 200 with ``{"validation": null}`` — the VM
detail page reads this on every load, and a 404 would force the UI
to differentiate "VM doesn't exist" (real error) from "validation
hasn't been run yet" (empty state) at the response level.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `LatestValidationResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### waves

#### `POST /api/plans/{plan_id}/waves/{wave_number}/baseline`

_Kick Off Baseline Run_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Request body** (**required**): `BaselineRunCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `BaselineRunAccepted` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /api/plans/{plan_id}/waves/{wave_number}/preview`

_Preview Wave Run_

Operator dry-run: list the target hosts, the read-only commands that
would run on each, and the authorization/kill-switch posture — no SSH.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `WavePreviewResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/{plan_id}/waves/{wave_number}/revoke-validation-key`

_Revoke Validation Key_

Remove the key's public component from each VM in the wave's
``authorized_keys`` file. Gated on a clean validation run unless
``force=true``.

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Request body** (**required**): `RevokeValidationKeyRequest`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHKeyRevokeResponse` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /api/plans/{plan_id}/waves/{wave_number}/validate`

_Kick Off Validation Run_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `plan_id` | path | yes | integer |  |
| `wave_number` | path | yes | integer |  |

**Request body** (**required**): `ValidationRunCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `202` | `ValidationRunAccepted` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

## Schemas

### `AppSettingsRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `ollama_model` | string | yes | Ollama Model |
| `schedule_preset` | SchedulePreset | yes |  |
| `ssh_host_key_policy` | SSHHostKeyPolicy | yes |  |
| `ssh_operations_enabled` | boolean | yes | Ssh Operations Enabled |
| `updated_at` | string | yes | Updated At |
| `next_run_at` | string | null | no | Next Run At |
| `max_vms_per_plan` | integer *(default: `1000`)* | no | Max Vms Per Plan |
| `max_vms_per_plan_request` | integer *(default: `10000`)* | no | Max Vms Per Plan Request |

### `AppSettingsUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ollama_model` | string | null | no | Ollama Model |
| `schedule_preset` | SchedulePreset | null | no |  |
| `ssh_host_key_policy` | SSHHostKeyPolicy | null | no |  |
| `ssh_operations_enabled` | boolean | null | no | Ssh Operations Enabled |

### `AuditLogRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `timestamp` | string | yes | Timestamp |
| `action` | string | yes | Action |
| `actor` | string | yes | Actor |
| `resource_type` | string | null | yes | Resource Type |
| `resource_id` | string | null | yes | Resource Id |
| `details` | object | yes | Details |

### `BackendOption`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | LLMBackendType | yes |  |
| `label` | string | yes | Label |
| `configured` | boolean | yes | Configured |
| `dev_only` | boolean *(default: `False`)* | no | Dev Only |
| `missing_config` | list[string] | no | Missing Config |

### `BaselineProfile`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `snapshot_count` | integer | yes | Snapshot Count |
| `first_collected_at` | string | null | yes | First Collected At |
| `last_collected_at` | string | null | yes | Last Collected At |
| `latest_meta` | object | yes | Latest Meta |
| `services` | list[string] | yes | Services |
| `open_ports` | list[object] | yes | Open Ports |
| `stable_mounts` | list[object] | yes | Stable Mounts |
| `dns_servers` | list[string] | yes | Dns Servers |
| `interfaces` | object | yes | Interfaces |

### `BaselineRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `baseline_run_id` | integer | yes | Baseline Run Id |
| `vm_id` | integer | yes | Vm Id |
| `status` | VMCollectionStatus | yes |  |
| `collected_data` | object | null | no | Collected Data |
| `probe_catalog_version` | string | null | no | Probe Catalog Version |
| `probes_run` | list[string] | null | no | Probes Run |
| `host_key_fingerprint` | string | null | no | Host Key Fingerprint |
| `failure_category` | string | null | no | Failure Category |
| `failure_detail` | string | null | no | Failure Detail |
| `collection_started_at` | string | null | no | Collection Started At |
| `collection_completed_at` | string | null | no | Collection Completed At |
| `captured_at` | string | yes | Captured At |

### `BaselineRunAccepted`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `baseline_run_id` | integer | yes | Baseline Run Id |
| `status` | BaselineRunStatus | yes |  |
| `status_url` | string | yes | Status Url |

### `BaselineRunCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ssh_key_id` | integer | yes | Ssh Key Id |
| `authorized_by` | string | null | no | Authorized By |
| `authorization_reason` | string | null | no | Authorization Reason |

### `BaselineRunRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `plan_id` | integer | yes | Plan Id |
| `wave_number` | integer | yes | Wave Number |
| `ssh_key_id` | integer | yes | Ssh Key Id |
| `status` | BaselineRunStatus | yes |  |
| `total_vms` | integer | yes | Total Vms |
| `captured_vms` | integer | yes | Captured Vms |
| `failed_vms` | integer | yes | Failed Vms |
| `progress_message` | string | null | no | Progress Message |
| `authorized_by` | string | null | no | Authorized By |
| `authorization_reason` | string | null | no | Authorization Reason |
| `started_at` | string | null | no | Started At |
| `completed_at` | string | null | no | Completed At |
| `created_at` | string | yes | Created At |
| `baselines` | list[BaselineRead] | no | Baselines |

### `Body_upload_rvtools_api_imports_rvtools_post`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | string | yes | File |
| `mode` | string *(default: `upsert`)* | no | Mode |
| `vcenter_mapping` | string | null | no | JSON object: hostname → vCenter id |
| `default_vcenter_id` | integer | null | no | Default Vcenter Id |

### `BulkCaptureResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spawned` | list[BulkCaptureSpawn] | yes | Spawned |
| `skipped` | list[object] | yes | Skipped |

### `BulkCaptureScope`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | no | Vm Ids |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `environment` | string | null | no | Environment |
| `application_hint` | string | null | no | Application Hint |
| `only_status` | string | null | no | Only Status |
| `name_contains` | string | null | no | Name Contains |
| `max_parallel` | integer *(default: `10`)* | no | Max Parallel |
| `per_vcenter_parallel` | integer *(default: `5`)* | no | Per Vcenter Parallel |

### `BulkCaptureSpawn`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `task_id` | string | yes | Task Id |

### `BulkClearTargetRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |

### `BulkEnvironmentSetRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |
| `environment` | string | yes | Environment |
| `rationale` | string | null | no | Rationale |

### `BulkEnvironmentSetResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `updated` | integer | yes | Updated |
| `not_found` | list[integer] | yes | Not Found |

### `BulkSetTargetClusterRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |
| `target_cluster_id_override` | integer | null | no | Target Cluster Id Override |

### `BulkSetTargetNamespaceRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |
| `target_namespace_override` | string | null | no | Target Namespace Override |

### `BulkTargetOverrideResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `updated` | integer | yes | Updated |
| `not_found` | list[integer] | yes | Not Found |

### `BulkVMCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vms` | list[VMCreate] | yes | Vms |

### `BulkVMDelete`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |

### `BulkVMDeleteResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `requested` | integer | yes | Requested |
| `deleted` | list[integer] | yes | Deleted |
| `not_found` | list[integer] | yes | Not Found |

### `BulkVMResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | integer | yes | Total |
| `created` | list[VMRead] | yes | Created |
| `skipped` | list[BulkVMSkipped] | yes | Skipped |

### `BulkVMSkipped`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `reason` | string | yes | Reason |

### `BulkValidationResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spawned` | list[BulkValidationSpawn] | yes | Spawned |
| `skipped` | list[object] | yes | Skipped |

### `BulkValidationScope`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | no | Vm Ids |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `environment` | string | null | no | Environment |
| `application_hint` | string | null | no | Application Hint |
| `only_migrated` | boolean *(default: `False`)* | no | Only Migrated |
| `not_validated_within_hours` | integer | null | no | Not Validated Within Hours |
| `use_cache` | boolean *(default: `True`)* | no | Use Cache |

### `BulkValidationSpawn`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `task_id` | string | yes | Task Id |

### `CaptureTaskRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task Id |
| `vm_id` | integer | yes | Vm Id |
| `status` | string ('running', 'completed', 'failed') | yes | Status |
| `started_at` | string | yes | Started At |
| `completed_at` | string | null | no | Completed At |
| `snapshot_id` | integer | null | no | Snapshot Id |
| `error` | string | null | no | Error |

### `CategorizationGroupRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `kind` | string ('application', 'environment', 'business_unit', 'other') | yes | Kind |
| `name` | string | yes | Name |
| `description` | string | null | yes | Description |
| `vm_count` | integer | yes | Vm Count |

### `CategorizationTaskRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task Id |
| `source_vcenter_id` | integer | yes | Source Vcenter Id |
| `status` | string ('running', 'completed', 'failed') | yes | Status |
| `current_step` | string ('queued', 'loading_inventory', 'calling_llm', 'persisting', 'completed', 'failed') | yes | Current Step |
| `progress_percent` | integer | yes | Progress Percent |
| `batches_total` | integer | yes | Batches Total |
| `batches_complete` | integer | yes | Batches Complete |
| `started_at` | string | yes | Started At |
| `completed_at` | string | null | no | Completed At |
| `groups_created` | integer | null | no | Groups Created |
| `error` | string | null | no | Error |

### `CommandAuditListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[CommandAuditRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `CommandAuditRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `vm_id` | integer | null | yes | Vm Id |
| `host` | string | yes | Host |
| `run_type` | string | yes | Run Type |
| `run_id` | integer | null | yes | Run Id |
| `command` | string | yes | Command |
| `exit_status` | integer | null | yes | Exit Status |
| `stdout_byte_count` | integer | yes | Stdout Byte Count |
| `stdout_sha256` | string | null | yes | Stdout Sha256 |
| `stdout_truncated` | string | null | yes | Stdout Truncated |
| `duration_ms` | integer | yes | Duration Ms |
| `blocked` | boolean | yes | Blocked |
| `started_at` | string | yes | Started At |

### `ConnectionTestRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `backend_type` | LLMBackendType | yes |  |

### `ConnectionTestResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `backend_type` | LLMBackendType | yes |  |
| `reachable` | boolean | yes | Reachable |
| `authenticated` | boolean | yes | Authenticated |
| `model_available` | boolean | yes | Model Available |
| `latency_ms` | integer | null | no | Latency Ms |
| `error` | string | null | no | Error |

### `DeleteAllVMsResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `deleted_count` | integer | yes | Deleted Count |

### `EditMove`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `vm_name` | string | yes | Vm Name |
| `from_plan_id` | integer | null | no | From Plan Id |
| `from_wave` | integer | null | no | From Wave |
| `to_plan_id` | integer | null | no | To Plan Id |
| `to_wave` | integer | null | no | To Wave |

### `EditPlanSummary`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `name` | string | yes | Name |
| `vm_count` | integer | yes | Vm Count |
| `wave_count` | integer | yes | Wave Count |
| `touched_waves` | list[integer] | no | Touched Waves |
| `created_waves` | list[integer] | no | Created Waves |
| `removed_waves` | list[integer] | no | Removed Waves |

### `EditTargetPlan`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `name` | string | yes | Name |
| `same_series` | boolean *(default: `False`)* | no | Same Series |
| `series_index` | integer | null | no | Series Index |
| `series_total` | integer | null | no | Series Total |
| `migration_type` | string *(default: `cold`)* | no | Migration Type |
| `vm_count` | integer *(default: `0`)* | no | Vm Count |
| `waves` | list[EditTargetWave] | no | Waves |

### `EditTargetWave`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `wave_number` | integer | yes | Wave Number |
| `vm_count` | integer | yes | Vm Count |
| `estimated_risk` | string | null | no | Estimated Risk |

### `EditTargetsResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `partition` | PlanPartition | null | no |  |
| `targets` | list[EditTargetPlan] | no | Targets |
| `ineligible` | list[IneligiblePlan] | no | Ineligible |

### `EditViolation`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | yes | Code |
| `severity` | string | yes | Severity |
| `message` | string | yes | Message |
| `plan_id` | integer | null | no | Plan Id |
| `wave_number` | integer | null | no | Wave Number |
| `family` | string | null | no | Family |
| `vm_ids` | list[integer] | no | Vm Ids |
| `vm_names` | list[string] | no | Vm Names |
| `count` | integer | null | no | Count |
| `limit` | integer | null | no | Limit |

### `EnvironmentSetRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `environment` | string | yes | Environment |
| `rationale` | string | null | no | Rationale |

### `FIPSOperationStatus`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `configured` | string | yes | Configured |
| `fips_approved` | boolean | yes | Fips Approved |
| `enforced` | boolean | yes | Enforced |

### `FIPSStatus`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `configured` | boolean | yes | Configured |
| `detected` | boolean | yes | Detected |
| `effective` | boolean | yes | Effective |
| `mismatch_warning` | string | null | no | Mismatch Warning |
| `operations` | list[FIPSOperationStatus] | yes | Operations |

### `HTTPValidationError`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `detail` | list[ValidationError] | no | Detail |

### `ImportJobListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[ImportJobRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `ImportJobRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Id |
| `kind` | string | yes | Kind |
| `filename` | string | yes | Filename |
| `file_size` | integer | yes | File Size |
| `file_sha256` | string | null | no | File Sha256 |
| `mode` | string | yes | Mode |
| `status` | string | yes | Status |
| `current_sheet` | string | null | no | Current Sheet |
| `progress_message` | string | null | no | Progress Message |
| `rows_total` | integer | null | no | Rows Total |
| `rows_read` | integer *(default: `0`)* | no | Rows Read |
| `rows_valid` | integer *(default: `0`)* | no | Rows Valid |
| `rows_rejected` | integer *(default: `0`)* | no | Rows Rejected |
| `rows_warned` | integer *(default: `0`)* | no | Rows Warned |
| `created_count` | integer *(default: `0`)* | no | Created Count |
| `updated_count` | integer *(default: `0`)* | no | Updated Count |
| `unchanged_count` | integer *(default: `0`)* | no | Unchanged Count |
| `marked_missing_count` | integer *(default: `0`)* | no | Marked Missing Count |
| `detected_vcenters` | list[object] | no | Detected Vcenters |
| `vcenter_mapping` | object | no | Vcenter Mapping |
| `default_vcenter_id` | integer | null | no | Default Vcenter Id |
| `result` | object | no | Result |
| `error_message` | string | null | no | Error Message |
| `cancel_requested` | boolean *(default: `False`)* | no | Cancel Requested |
| `actor` | string | yes | Actor |
| `created_at` | string | yes | Created At |
| `started_at` | string | null | no | Started At |
| `completed_at` | string | null | no | Completed At |
| `progress_percent` | integer | null | yes | NULL when the workbook doesn't declare its row counts — the UI
then shows rows processed rather than inventing a percentage. |
| `elapsed_seconds` | number | null | yes | Elapsed Seconds |

### `ImportRejectListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[ImportRejectRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `ImportRejectRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sheet` | string | yes | Sheet |
| `row_number` | integer | yes | Row Number |
| `vm_name` | string | null | no | Vm Name |
| `severity` | string | yes | Severity |
| `reason` | string | yes | Reason |

### `ImportStartRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vcenter_mapping` | object | no | Vcenter Mapping |
| `default_vcenter_id` | integer | null | no | Default Vcenter Id |
| `mode` | string | null | no | Mode |

### `IneligiblePlan`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `name` | string | yes | Name |
| `reason` | string | yes | Reason |

### `InferenceLogListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[InferenceLogRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `InferenceLogRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `operation` | string | yes | Operation |
| `backend_type` | string | yes | Backend Type |
| `model` | string | yes | Model |
| `method` | string | yes | Method |
| `input_messages` | list[—] | null | yes | Input Messages |
| `output_text` | string | null | yes | Output Text |
| `detections` | object | null | yes | Detections |
| `verdict` | string | null | yes | Verdict |
| `latency_ms` | integer | yes | Latency Ms |
| `resource_type` | string | null | yes | Resource Type |
| `resource_id` | integer | null | yes | Resource Id |
| `vm_id` | integer | null | yes | Vm Id |
| `created_at` | string | yes | Created At |

### `InferenceLogStats`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | integer | yes | Total |
| `by_method` | object | yes | By Method |
| `by_operation` | object | yes | By Operation |

### `LLMBackendConfig`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `backend` | string | yes | Backend |
| `model` | string | null | no | Model |
| `endpoint` | string | null | no | Endpoint |

### `LLMBackendHealth`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | yes | Status |
| `backend` | string | yes | Backend |
| `model` | string | null | no | Model |
| `endpoint` | string | null | no | Endpoint |
| `latency_ms` | integer | null | no | Latency Ms |
| `details` | object | null | no | Details |

### `LLMBackendInfo`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `config` | LLMBackendConfig | yes |  |
| `health` | LLMBackendHealth | yes |  |

### `LLMSettingsRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `active_llm_backend` | LLMBackendType | yes |  |
| `available_backends` | list[BackendOption] | yes | Available Backends |
| `last_llm_error` | string | null | no | Last Llm Error |
| `last_llm_error_at` | string | null | no | Last Llm Error At |

### `LLMSettingsUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `active_llm_backend` | LLMBackendType | yes |  |

### `LatestValidationResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `validation` | ValidationResultRead | null | no |  |

### `MappingSourceSignals`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `networks` | list[SourceSignal] | no | Networks |
| `datastores` | list[SourceSignal] | no | Datastores |

### `MappingSuggestionResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `suggestions` | list[NetworkMappingItem | StorageMappingItem] | yes | Suggestions |
| `rationale_summary` | string *(default: ``)* | no | Rationale Summary |

### `MultiVCenterImportRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vms` | list[RVToolsVMRow] | yes | Vms |
| `vcenter_mapping` | object | no | Vcenter Mapping |
| `default_vcenter_id` | integer | null | no | Default Vcenter Id |

### `MultiVCenterImportResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `imported_per_vcenter` | list[PerVCenterResult] | yes | Imported Per Vcenter |
| `skipped` | list[object] | yes | Skipped |
| `errors` | list[string] | yes | Errors |
| `total_vms` | integer | yes | Total Vms |
| `elapsed_seconds` | number | yes | Elapsed Seconds |

### `NamespaceMappingItem`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `criteria` | string ('default', 'environment', 'application', 'vcenter_folder') *(default: `default`)* | no | Criteria |
| `criteria_value` | string | null | no | Criteria Value |
| `target_namespace` | string | yes | Target Namespace |

### `NamespaceStrategy`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `strategy` | string ('single', 'per_environment', 'per_application') *(default: `per_environment`)* | no | Strategy |
| `single_namespace` | string | null | no | Single Namespace |
| `per_env_namespaces` | object | no | Per Env Namespaces |
| `per_app_prefix` | string *(default: `app`)* | no | Per App Prefix |

### `NetworkFindingRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `category` | FindingCategory | yes |  |
| `severity` | FindingSeverity | yes |  |
| `confidence` | FindingConfidence | yes |  |
| `triage` | FindingTriage | yes |  |
| `title` | string | yes | Title |
| `description` | string | yes | Description |
| `source_evidence` | string | yes | Source Evidence |
| `proposed_evidence` | string | yes | Proposed Evidence |
| `recommendation` | string | yes | Recommendation |
| `created_at` | string | yes | Created At |

### `NetworkFindingTriageUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triage` | string ('open', 'accepted', 'dismissed') | yes | Triage |

### `NetworkMappingItem`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_network` | string | yes | Source Network |
| `target_network_name` | string | null | no | Target Network Name |
| `target_network_type` | string | null | no | Target Network Type |
| `target_namespace` | string | null | no | Target Namespace |
| `confidence` | string | null | no | Confidence |
| `rationale` | string | null | no | Rationale |

### `NetworkReviewCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `customer_notes` | string | null | no | Customer Notes |
| `proposed_yaml` | string | null | no | Proposed Yaml |

### `NetworkReviewNotesUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `customer_notes` | string *(default: ``)* | no | Customer Notes |

### `NetworkReviewRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `status` | NetworkReviewStatus | yes |  |
| `customer_notes` | string | yes | Customer Notes |
| `proposed_yaml` | string | yes | Proposed Yaml |
| `analysis_results` | object | yes | Analysis Results |
| `last_analyzed_at` | string | null | no | Last Analyzed At |
| `last_error` | string | null | no | Last Error |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `findings` | list[NetworkFindingRead] | no | Findings |

### `NetworkReviewSummary`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `status` | NetworkReviewStatus | yes |  |
| `finding_count` | integer *(default: `0`)* | no | Finding Count |
| `severity_counts` | object | no | Severity Counts |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `last_analyzed_at` | string | null | no | Last Analyzed At |

### `NetworkReviewYamlUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `proposed_yaml` | string *(default: ``)* | no | Proposed Yaml |

### `OCPTargetCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `api_endpoint` | string | yes | Api Endpoint |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | object *(default: `unclassified`)* | no |  |
| `mtv_namespace` | string | null | no | Mtv Namespace |
| `ocp_version` | string | null | no | Ocp Version |
| `kubernetes_version` | string | null | no | Kubernetes Version |
| `notes` | string | null | no | Notes |

### `OCPTargetNamespaceCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `description` | string | null | no | Description |

### `OCPTargetNamespaceListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[OCPTargetNamespaceRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `OCPTargetNamespaceRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `description` | string | null | no | Description |
| `id` | integer | yes | Id |
| `ocp_target_id` | integer | yes | Ocp Target Id |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `OCPTargetNamespaceUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `description` | string | null | no | Description |

### `OCPTargetRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `api_endpoint` | string | yes | Api Endpoint |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | object *(default: `unclassified`)* | no |  |
| `mtv_namespace` | string | null | no | Mtv Namespace |
| `ocp_version` | string | null | no | Ocp Version |
| `kubernetes_version` | string | null | no | Kubernetes Version |
| `notes` | string | null | no | Notes |
| `id` | integer | yes | Id |
| `status` | OCPTargetStatus | yes |  |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `OCPTargetUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `api_endpoint` | string | null | no | Api Endpoint |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | ClassificationLevel | null | no |  |
| `mtv_namespace` | string | null | no | Mtv Namespace |
| `ocp_version` | string | null | no | Ocp Version |
| `kubernetes_version` | string | null | no | Kubernetes Version |
| `notes` | string | null | no | Notes |

### `OllamaModel`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `size` | integer | null | no | Size |
| `modified_at` | string | null | no | Modified At |

### `OllamaModelsResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `models` | list[OllamaModel] | yes | Models |

### `PerVCenterResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vcenter_id` | integer | yes | Vcenter Id |
| `vcenter_name` | string | yes | Vcenter Name |
| `created` | integer | yes | Created |
| `updated` | integer | yes | Updated |
| `marked_missing` | integer | yes | Marked Missing |
| `unchanged` | integer | yes | Unchanged |

### `PlanCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |
| `name` | string *(default: `Untitled plan`)* | no | Name |
| `mapping_ids` | list[integer] | null | no | Mapping Ids |
| `migration_type` | string *(default: `cold`)* | no | Migration Type |
| `override_assessment` | boolean *(default: `False`)* | no | Override Assessment |
| `skip_ineligible` | boolean *(default: `False`)* | no | Skip Ineligible |
| `preclassification_enabled` | boolean *(default: `True`)* | no | Preclassification Enabled |
| `ha_strategy` | string *(default: `spread`)* | no | Ha Strategy |

### `PlanEditRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `operation` | string ('move_to_wave', 'move_to_plan', 'remove', 'add') | yes | Operation |
| `vm_ids` | list[integer] | yes | Vm Ids |
| `target_wave` | integer | string | null | no | Target Wave |
| `target_plan_id` | integer | null | no | Target Plan Id |
| `dry_run` | boolean *(default: `False`)* | no | Dry Run |
| `confirm_overrides` | boolean *(default: `False`)* | no | Confirm Overrides |
| `note` | string | null | no | Note |

### `PlanEditResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `applied` | boolean | yes | Applied |
| `dry_run` | boolean | yes | Dry Run |
| `operation` | string | yes | Operation |
| `requires_confirmation` | boolean | yes | Requires Confirmation |
| `violations` | list[EditViolation] | no | Violations |
| `moves` | list[EditMove] | no | Moves |
| `plans` | list[EditPlanSummary] | no | Plans |
| `reannotation` | string *(default: `none`)* | no | Reannotation |

### `PlanPartition`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `source_vcenter_name` | string | null | no | Source Vcenter Name |
| `target_cluster_id` | integer | null | no | Target Cluster Id |
| `target_cluster_name` | string | null | no | Target Cluster Name |
| `target_namespace` | string | null | no | Target Namespace |

### `PlanPreviewResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total_vms` | integer | yes | Total Vms |
| `resolvable` | integer | yes | Resolvable |
| `unresolvable` | integer | yes | Unresolvable |
| `groups` | list[PreviewPartitionGroup] | yes | Groups |
| `unresolved` | list[PreviewUnresolvedVM] | yes | Unresolved |
| `plans` | list[PreviewPlanEntry] | no | Plans |
| `plan_count` | integer *(default: `0`)* | no | Plan Count |
| `batch_size` | integer *(default: `0`)* | no | Batch Size |
| `excluded` | list[PreviewExcludedVM] | no | Excluded |

### `PlanRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string *(default: `Untitled plan`)* | no | Name |
| `migration_type` | string *(default: `cold`)* | no | Migration Type |
| `vm_ids` | list[integer] | yes | Vm Ids |
| `waves` | list[object] | yes | Waves |
| `summary` | string | null | no | Summary |
| `model` | string | yes | Model |
| `mapping_ids` | list[integer] | no | Mapping Ids |
| `created_at` | string | yes | Created At |
| `status` | string *(default: `complete`)* | no | Status |
| `progress_message` | string | null | no | Progress Message |
| `progress_percent` | integer *(default: `100`)* | no | Progress Percent |
| `error_message` | string | null | no | Error Message |
| `started_at` | string | null | no | Started At |
| `completed_at` | string | null | no | Completed At |
| `groups` | list[object] | no | Groups |
| `groups_formed` | integer *(default: `0`)* | no | Groups Formed |
| `method` | string *(default: ``)* | no | Method |
| `attempts` | integer *(default: `0`)* | no | Attempts |
| `plans` | list[object] | no | Plans |
| `plan_count` | integer *(default: `0`)* | no | Plan Count |
| `series_id` | string | null | no | Series Id |
| `series_index` | integer | null | no | Series Index |
| `series_total` | integer | null | no | Series Total |
| `excluded_vms` | list[object] | no | Excluded Vms |
| `partition` | object | null | no | Partition |
| `excluded` | list[object] | no | Excluded |

### `PlanVMRow`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `name` | string | yes | Name |
| `wave_number` | integer | yes | Wave Number |
| `environment` | string | null | no | Environment |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `application_hint` | string | null | no | Application Hint |
| `role` | string | null | no | Role |
| `ha_family` | string | null | no | Ha Family |
| `ha_family_size` | integer | null | no | Ha Family Size |
| `assessment_status` | string | null | no | Assessment Status |
| `lifecycle_state` | string | null | no | Lifecycle State |
| `baseline_covered` | boolean | null | no | Baseline Covered |
| `missing` | boolean *(default: `False`)* | no | Missing |

### `PlanVMsResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `status` | string | yes | Status |
| `editable` | boolean | yes | Editable |
| `partition` | PlanPartition | null | no |  |
| `limits` | object | no | Limits |
| `items` | list[PlanVMRow] | no | Items |
| `total` | integer *(default: `0`)* | no | Total |
| `waves` | list[PlanWaveState] | no | Waves |

### `PlanWaveBaseline`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `last_run_id` | integer | null | no | Last Run Id |
| `last_completed_at` | string | null | no | Last Completed At |
| `missing_vm_ids` | list[integer] | no | Missing Vm Ids |

### `PlanWaveState`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `wave_number` | integer | yes | Wave Number |
| `vm_count` | integer | yes | Vm Count |
| `baseline` | PlanWaveBaseline | no |  |
| `active_run` | boolean *(default: `False`)* | no | Active Run |
| `validated` | boolean *(default: `False`)* | no | Validated |

### `PlanningStrategyCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `primary_grouping` | object *(default: `application`)* | no |  |
| `wave_size_target` | object *(default: `medium_10_20`)* | no |  |
| `wave_size_custom` | integer | null | no | Wave Size Custom |
| `risk_approach` | object *(default: `mixed`)* | no |  |
| `production_handling` | object *(default: `non_prod_first`)* | no |  |
| `application_atomicity` | object *(default: `all_together`)* | no |  |
| `freeform_constraints` | string *(default: ``)* | no | Freeform Constraints |

### `PlanningStrategyRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `primary_grouping` | PrimaryGrouping | yes |  |
| `wave_size_target` | WaveSizeTarget | yes |  |
| `wave_size_custom` | integer | null | no | Wave Size Custom |
| `risk_approach` | RiskApproach | yes |  |
| `production_handling` | ProductionHandling | yes |  |
| `application_atomicity` | ApplicationAtomicity | yes |  |
| `freeform_constraints` | string | yes | Freeform Constraints |
| `created_by_actor` | string | yes | Created By Actor |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `PlanningStrategyUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `primary_grouping` | PrimaryGrouping | null | no |  |
| `wave_size_target` | WaveSizeTarget | null | no |  |
| `wave_size_custom` | integer | null | no | Wave Size Custom |
| `risk_approach` | RiskApproach | null | no |  |
| `production_handling` | ProductionHandling | null | no |  |
| `application_atomicity` | ApplicationAtomicity | null | no |  |
| `freeform_constraints` | string | null | no | Freeform Constraints |

### `PreflightCheckResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ok` | boolean | yes | Ok |
| `target_status` | string ('active', 'inactive', 'error', 'unknown') *(default: `unknown`)* | no | Target Status |
| `unmapped_networks` | list[string] | no | Unmapped Networks |
| `unmapped_datastores` | list[string] | no | Unmapped Datastores |
| `missing_storage_classes_on_target` | list[string] | no | Missing Storage Classes On Target |
| `missing_networks_on_target` | list[string] | no | Missing Networks On Target |
| `missing_namespaces_on_target` | list[string] | no | Missing Namespaces On Target |
| `warnings` | list[string] | no | Warnings |

### `PreviewExcludedVM`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `vm_name` | string | yes | Vm Name |
| `reason` | string | yes | Reason |

### `PreviewGroupsResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_count` | integer | yes | Vm Count |
| `groups_formed` | integer | yes | Groups Formed |
| `groups` | list[object] | yes | Groups |
| `over_ceiling` | boolean | yes | Over Ceiling |
| `ceiling` | integer | yes | Ceiling |

### `PreviewPartitionGroup`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_vcenter_id` | integer | null | yes | Source Vcenter Id |
| `source_vcenter_name` | string | null | yes | Source Vcenter Name |
| `target_cluster_id` | integer | null | yes | Target Cluster Id |
| `target_cluster_name` | string | null | yes | Target Cluster Name |
| `target_namespace` | string | null | yes | Target Namespace |
| `vm_count` | integer | yes | Vm Count |
| `estimated_waves` | integer | yes | Estimated Waves |
| `mapping_id` | integer | null | yes | Mapping Id |
| `mapping_name` | string | null | yes | Mapping Name |
| `network_targets` | list[string] | no | Network Targets |
| `storage_targets` | list[string] | no | Storage Targets |

### `PreviewPlanEntry`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `vm_count` | integer | yes | Vm Count |
| `series_index` | integer | yes | Series Index |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `source_vcenter_name` | string | null | no | Source Vcenter Name |
| `target_cluster_id` | integer | null | no | Target Cluster Id |
| `target_cluster_name` | string | null | no | Target Cluster Name |
| `target_namespace` | string | null | no | Target Namespace |

### `PreviewUnresolvedVM`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `vm_name` | string | yes | Vm Name |
| `reasons` | list[string] | no | Reasons |

### `RVToolsDeltaItem`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `diff` | object | null | no | Diff |

### `RVToolsDeltaRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vms` | list[RVToolsVMRow] | yes | Vms |

### `RVToolsDeltaResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `new` | list[RVToolsDeltaItem] | yes | New |
| `updated` | list[RVToolsDeltaItem] | yes | Updated |
| `removed` | list[RVToolsDeltaItem] | yes | Removed |
| `unchanged` | list[RVToolsDeltaItem] | yes | Unchanged |
| `summary` | object | yes | Summary |

### `RVToolsImportResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `created` | integer *(default: `0`)* | no | Created |
| `updated` | integer *(default: `0`)* | no | Updated |
| `marked_missing` | integer *(default: `0`)* | no | Marked Missing |
| `unchanged` | integer *(default: `0`)* | no | Unchanged |
| `errors` | list[string] | no | Errors |

### `RVToolsImportTaskRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task Id |
| `status` | string ('running', 'completed', 'failed') | yes | Status |
| `progress_percent` | integer *(default: `0`)* | no | Progress Percent |
| `started_at` | string | yes | Started At |
| `completed_at` | string | null | no | Completed At |
| `result` | RVToolsImportResult | null | no |  |
| `error` | string | null | no | Error |

### `RVToolsVMRow`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `source_hostname` | string | null | no | Source Hostname |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `role` | string | null | no | Role |
| `environment` | string | null | no | Environment |
| `owner` | string | null | no | Owner |
| `application_hint` | string | null | no | Application Hint |
| `vsphere_networks` | list[string] | no | Vsphere Networks |
| `vsphere_datastores` | list[string] | no | Vsphere Datastores |
| `source_vcenter_hostname` | string | null | no | Source Vcenter Hostname |
| `vsphere_cluster` | string | null | no | Vsphere Cluster |
| `vsphere_folder` | string | null | no | Vsphere Folder |
| `custom_attributes` | object | no | Custom Attributes |

### `ReannotateAccepted`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `wave_number` | integer | yes | Wave Number |
| `reannotation` | string *(default: `queued`)* | no | Reannotation |

### `RedetectEnvironmentRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vcenter_source_id` | integer | null | no | Vcenter Source Id |
| `force` | boolean *(default: `False`)* | no | Force |
| `dry_run` | boolean *(default: `False`)* | no | Dry Run |

### `RedetectEnvironmentResult`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `scanned` | integer | yes | Scanned |
| `updated` | integer | yes | Updated |
| `skipped_user_set` | integer | yes | Skipped User Set |
| `still_unknown` | integer | yes | Still Unknown |
| `summary_by_environment` | object | yes | Summary By Environment |
| `still_unknown_samples` | list[string] | yes | Still Unknown Samples |

### `ResolvedNetworkRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Source |
| `target_network_id` | integer | null | no | Target Network Id |
| `target_network_name` | string | null | no | Target Network Name |
| `target_network_namespace` | string | null | no | Target Network Namespace |
| `target_network_type` | string | null | no | Target Network Type |

### `ResolvedStorageRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source` | string | yes | Source |
| `target_storage_class_name` | string | null | no | Target Storage Class Name |
| `access_mode` | string | null | no | Access Mode |

### `ResourceMappingCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `vcenter_source_id` | integer | yes | Vcenter Source Id |
| `ocp_target_id` | integer | yes | Ocp Target Id |
| `network_mappings` | list[NetworkMappingItem] | no | Network Mappings |
| `storage_mappings` | list[StorageMappingItem] | no | Storage Mappings |
| `namespace_mappings` | list[NamespaceMappingItem] | NamespaceStrategy | no | Namespace Mappings |

### `ResourceMappingRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `vcenter_source_id` | integer | yes | Vcenter Source Id |
| `ocp_target_id` | integer | yes | Ocp Target Id |
| `network_mappings` | list[object] | no | Network Mappings |
| `storage_mappings` | list[object] | no | Storage Mappings |
| `namespace_mappings` | list[object] | object | no | Namespace Mappings |
| `status` | ResourceMappingStatus | yes |  |
| `last_used_at` | string | null | no | Last Used At |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `ResourceMappingUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `network_mappings` | list[NetworkMappingItem] | null | no | Network Mappings |
| `storage_mappings` | list[StorageMappingItem] | null | no | Storage Mappings |
| `namespace_mappings` | list[NamespaceMappingItem] | NamespaceStrategy | null | no | Namespace Mappings |

### `RevokeValidationKeyRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ssh_key_id` | integer | yes | Ssh Key Id |
| `force` | boolean *(default: `False`)* | no | Force |

### `SSHKeyCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `plan_id` | integer | null | no | Plan Id |
| `algorithm` | string | null | no | Algorithm |

### `SSHKeyGenerateRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `algorithm` | string | null | no | Algorithm |

### `SSHKeyGenerateResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `algorithm` | string | yes | Algorithm |
| `fingerprint` | string | yes | Fingerprint |
| `public_key` | string | yes | Public Key |
| `created_at` | string | yes | Created At |

### `SSHKeyListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[SSHKeyRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `SSHKeyRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `public_key` | string | yes | Public Key |
| `fingerprint` | string | yes | Fingerprint |
| `algorithm` | string | yes | Algorithm |
| `status` | app__models__ssh_key__SSHKeyStatus | yes |  |
| `plan_id` | integer | null | yes | Plan Id |
| `created_at` | string | yes | Created At |
| `retired_at` | string | null | yes | Retired At |

### `SSHKeyRevocationOutcome`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `succeeded` | boolean | yes | Succeeded |
| `detail` | string | null | no | Detail |

### `SSHKeyRevokeRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |
| `force` | boolean *(default: `False`)* | no | Force |

### `SSHKeyRevokeResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key_id` | integer | yes | Key Id |
| `total` | integer | yes | Total |
| `succeeded` | integer | yes | Succeeded |
| `failed` | integer | yes | Failed |
| `outcomes` | list[SSHKeyRevocationOutcome] | yes | Outcomes |

### `SSHKeyRotateResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `algorithm` | string | yes | Algorithm |
| `fingerprint` | string | yes | Fingerprint |
| `public_key` | string | yes | Public Key |
| `created_at` | string | yes | Created At |
| `previous_fingerprint` | string | yes | Previous Fingerprint |
| `backups` | list[string] | yes | Backups |
| `warning` | string | yes | Warning |

### `SSHPublicKey`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `public_key` | string | yes | Public Key |
| `fingerprint` | string | null | no | Fingerprint |
| `type` | string *(default: `ssh-ed25519`)* | no | Type |

### `ScheduleCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `cron_expression` | string | yes | Cron Expression |
| `timezone` | string *(default: `UTC`)* | no | Timezone |
| `scope` | ScheduleScope | no |  |
| `notes` | string | null | no | Notes |
| `status` | string ('enabled', 'paused', 'disabled') *(default: `enabled`)* | no | Status |

### `ScheduleRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `cron_expression` | string | yes | Cron Expression |
| `timezone` | string | yes | Timezone |
| `scope` | object | yes | Scope |
| `status` | ScheduleStatus | yes |  |
| `notes` | string | null | no | Notes |
| `last_fired_at` | string | null | no | Last Fired At |
| `last_task_id` | string | null | no | Last Task Id |
| `next_fire_at` | string | null | no | Next Fire At |
| `runtime_paused` | boolean | yes | Runtime Paused |
| `created_by_actor` | string | yes | Created By Actor |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `ScheduleScope`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | no | Vm Ids |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `environment` | string | null | no | Environment |
| `application_hint` | string | null | no | Application Hint |

### `ScheduleUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `cron_expression` | string | null | no | Cron Expression |
| `timezone` | string | null | no | Timezone |
| `scope` | ScheduleScope | null | no |  |
| `notes` | string | null | no | Notes |
| `status` | string | null | no | Status |
| `runtime_paused` | boolean | null | no | Runtime Paused |

### `SnapshotCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ssh_user` | string | yes | Ssh User |
| `raw_data` | object | yes | Raw Data |
| `checksum` | string | null | no | Checksum |

### `SnapshotRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `vm_id` | integer | yes | Vm Id |
| `snapshot_number` | integer | yes | Snapshot Number |
| `ssh_user` | string | yes | Ssh User |
| `raw_data` | object | yes | Raw Data |
| `checksum` | string | null | yes | Checksum |
| `collected_at` | string | yes | Collected At |

### `SourceSignal`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `vm_count` | integer *(default: `0`)* | no | Vm Count |

### `StorageFindingRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `category` | StorageFindingCategory | yes |  |
| `severity` | FindingSeverity | yes |  |
| `confidence` | FindingConfidence | yes |  |
| `triage` | FindingTriage | yes |  |
| `title` | string | yes | Title |
| `description` | string | yes | Description |
| `source_evidence` | string | yes | Source Evidence |
| `proposed_evidence` | string | yes | Proposed Evidence |
| `recommendation` | string | yes | Recommendation |
| `created_at` | string | yes | Created At |

### `StorageFindingTriageUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `triage` | string ('open', 'accepted', 'dismissed') | yes | Triage |

### `StorageMappingItem`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_datastore` | string | yes | Source Datastore |
| `target_storage_class` | string | null | no | Target Storage Class |
| `access_mode` | string | null | no | Access Mode |
| `confidence` | string | null | no | Confidence |
| `rationale` | string | null | no | Rationale |

### `StorageReviewCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `customer_notes` | string | null | no | Customer Notes |
| `proposed_yaml` | string | null | no | Proposed Yaml |

### `StorageReviewNotesUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `customer_notes` | string *(default: ``)* | no | Customer Notes |

### `StorageReviewRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `status` | StorageReviewStatus | yes |  |
| `customer_notes` | string | yes | Customer Notes |
| `proposed_yaml` | string | yes | Proposed Yaml |
| `analysis_results` | object | yes | Analysis Results |
| `last_analyzed_at` | string | null | no | Last Analyzed At |
| `last_error` | string | null | no | Last Error |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `findings` | list[StorageFindingRead] | no | Findings |

### `StorageReviewSummary`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |
| `status` | StorageReviewStatus | yes |  |
| `finding_count` | integer *(default: `0`)* | no | Finding Count |
| `severity_counts` | object | no | Severity Counts |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `last_analyzed_at` | string | null | no | Last Analyzed At |

### `StorageReviewYamlUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `proposed_yaml` | string *(default: ``)* | no | Proposed Yaml |

### `TargetNetworkCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `network_type` | TargetNetworkType | yes |  |
| `namespace` | string | null | no | Namespace |
| `is_default` | boolean *(default: `False`)* | no | Is Default |
| `notes` | string | null | no | Notes |

### `TargetNetworkRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `network_type` | TargetNetworkType | yes |  |
| `namespace` | string | null | no | Namespace |
| `is_default` | boolean *(default: `False`)* | no | Is Default |
| `notes` | string | null | no | Notes |
| `id` | integer | yes | Id |
| `ocp_target_id` | integer | yes | Ocp Target Id |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `TargetNetworkUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `network_type` | TargetNetworkType | null | no |  |
| `namespace` | string | null | no | Namespace |
| `is_default` | boolean | null | no | Is Default |
| `notes` | string | null | no | Notes |

### `TargetStorageClassCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `access_mode` | object *(default: `ReadWriteOnce`)* | no |  |
| `is_default` | boolean *(default: `False`)* | no | Is Default |
| `notes` | string | null | no | Notes |

### `TargetStorageClassRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `access_mode` | object *(default: `ReadWriteOnce`)* | no |  |
| `is_default` | boolean *(default: `False`)* | no | Is Default |
| `notes` | string | null | no | Notes |
| `id` | integer | yes | Id |
| `ocp_target_id` | integer | yes | Ocp Target Id |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `TargetStorageClassUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `access_mode` | StorageAccessMode | null | no |  |
| `is_default` | boolean | null | no | Is Default |
| `notes` | string | null | no | Notes |

### `VCenterSourceCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `hostname` | string | yes | Hostname |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | object *(default: `unclassified`)* | no |  |
| `status` | object *(default: `active`)* | no |  |
| `default_target_namespace` | string | null | no | Default Target Namespace |
| `default_target_storage_class` | string | null | no | Default Target Storage Class |
| `notes` | string | null | no | Notes |

### `VCenterSourceRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `hostname` | string | yes | Hostname |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | object *(default: `unclassified`)* | no |  |
| `status` | object *(default: `active`)* | no |  |
| `default_target_namespace` | string | null | no | Default Target Namespace |
| `default_target_storage_class` | string | null | no | Default Target Storage Class |
| `notes` | string | null | no | Notes |
| `id` | integer | yes | Id |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `vm_count` | integer *(default: `0`)* | no | Vm Count |

### `VCenterSourceUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | null | no | Name |
| `hostname` | string | null | no | Hostname |
| `region` | string | null | no | Region |
| `site` | string | null | no | Site |
| `classification_level` | ClassificationLevel | null | no |  |
| `status` | VCenterStatus | null | no |  |
| `default_target_namespace` | string | null | no | Default Target Namespace |
| `default_target_storage_class` | string | null | no | Default Target Storage Class |
| `notes` | string | null | no | Notes |

### `VMCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `source_hostname` | string | yes | Source Hostname |
| `target_hostname` | string | null | no | Target Hostname |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `role` | string | null | no | Role |
| `ssh_user` | string | null | no | Ssh User |
| `ssh_port` | integer *(default: `22`)* | no | Ssh Port |
| `current_platform` | string | null | no | Current Platform |
| `environment` | string | null | no | Environment |
| `owner` | string | null | no | Owner |
| `notes` | string | null | no | Notes |
| `vsphere_networks` | list[string] | no | Vsphere Networks |
| `vsphere_datastores` | list[string] | no | Vsphere Datastores |
| `target_cluster_id_override` | integer | null | no | Target Cluster Id Override |
| `target_namespace_override` | string | null | no | Target Namespace Override |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `application_hint` | string | null | no | Application Hint |
| `vsphere_cluster` | string | null | no | Vsphere Cluster |
| `vsphere_folder` | string | null | no | Vsphere Folder |
| `custom_attributes` | object | no | Custom Attributes |

### `VMFacetsResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | object | yes | Status |
| `lifecycle_state` | object | yes | Lifecycle State |
| `environment` | object | yes | Environment |
| `os_family` | object | yes | Os Family |
| `application_hint` | object | yes | Application Hint |
| `vcenter_source_id` | object | yes | Vcenter Source Id |
| `classification_level` | object | yes | Classification Level |
| `vsphere_cluster` | object | no | Vsphere Cluster |
| `power_state` | object | no | Power State |
| `assessment_status` | object | no | Assessment Status |
| `total` | integer | yes | Total |

### `VMIdListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[VMIdName] | yes | Items |
| `total` | integer | yes | Total |

### `VMIdName`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `name` | string | yes | Name |

### `VMListResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `items` | list[VMRead] | yes | Items |
| `total` | integer | yes | Total |
| `skip` | integer | yes | Skip |
| `limit` | integer | yes | Limit |

### `VMRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Name |
| `source_hostname` | string | yes | Source Hostname |
| `target_hostname` | string | null | no | Target Hostname |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `role` | string | null | no | Role |
| `ssh_user` | string | null | no | Ssh User |
| `ssh_port` | integer *(default: `22`)* | no | Ssh Port |
| `current_platform` | string | null | no | Current Platform |
| `environment` | string | null | no | Environment |
| `owner` | string | null | no | Owner |
| `notes` | string | null | no | Notes |
| `vsphere_networks` | list[string] | no | Vsphere Networks |
| `vsphere_datastores` | list[string] | no | Vsphere Datastores |
| `target_cluster_id_override` | integer | null | no | Target Cluster Id Override |
| `target_namespace_override` | string | null | no | Target Namespace Override |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `application_hint` | string | null | no | Application Hint |
| `vsphere_cluster` | string | null | no | Vsphere Cluster |
| `vsphere_folder` | string | null | no | Vsphere Folder |
| `custom_attributes` | object | no | Custom Attributes |
| `id` | integer | yes | Id |
| `status` | VMStatus | yes |  |
| `lifecycle_state` | VMLifecycleState | yes |  |
| `lifecycle_state_changed_at` | string | yes | Lifecycle State Changed At |
| `moref` | string | null | no | Moref |
| `vm_uuid` | string | null | no | Vm Uuid |
| `power_state` | string | null | no | Power State |
| `esxi_host` | string | null | no | Esxi Host |
| `vsphere_datacenter` | string | null | no | Vsphere Datacenter |
| `guest_os_full` | string | null | no | Guest Os Full |
| `num_cpus` | integer | null | no | Num Cpus |
| `memory_mb` | integer | null | no | Memory Mb |
| `disk_count` | integer | null | no | Disk Count |
| `nic_count` | integer | null | no | Nic Count |
| `provisioned_mb` | integer | null | no | Provisioned Mb |
| `missing_from_last_upload` | boolean *(default: `False`)* | no | Missing From Last Upload |
| `assessment_status` | string *(default: `unknown`)* | no | Assessment Status |
| `assessment_findings` | list[object] | no | Assessment Findings |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |
| `resolved_target_cluster_id` | integer | null | no | Resolved Target Cluster Id |
| `resolved_target_cluster_name` | string | null | no | Resolved Target Cluster Name |
| `resolved_target_namespace` | string | null | no | Resolved Target Namespace |
| `resolved_networks` | list[ResolvedNetworkRead] | no | Resolved Networks |
| `resolved_storage` | list[ResolvedStorageRead] | no | Resolved Storage |
| `resolution_is_complete` | boolean *(default: `False`)* | no | Resolution Is Complete |
| `resolution_reasons` | list[string] | no | Resolution Reasons |
| `resolution_mapping_id` | integer | null | no | Resolution Mapping Id |
| `plan_id` | integer | null | no | Plan Id |
| `plan_name` | string | null | no | Plan Name |
| `plan_wave_number` | integer | null | no | Plan Wave Number |

### `VMStats`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `total` | integer | yes | Total |
| `by_status` | object | yes | By Status |
| `missing_from_last_upload` | integer | yes | Missing From Last Upload |

### `VMUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_hostname` | string | null | no | Source Hostname |
| `target_hostname` | string | null | no | Target Hostname |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `role` | string | null | no | Role |
| `ssh_user` | string | null | no | Ssh User |
| `ssh_port` | integer | null | no | Ssh Port |
| `current_platform` | string | null | no | Current Platform |
| `environment` | string | null | no | Environment |
| `owner` | string | null | no | Owner |
| `status` | VMStatus | null | no |  |
| `lifecycle_state` | VMLifecycleState | null | no |  |
| `notes` | string | null | no | Notes |
| `vsphere_networks` | list[string] | null | no | Vsphere Networks |
| `vsphere_datastores` | list[string] | null | no | Vsphere Datastores |
| `target_cluster_id_override` | integer | null | no | Target Cluster Id Override |
| `target_namespace_override` | string | null | no | Target Namespace Override |
| `source_vcenter_id` | integer | null | no | Source Vcenter Id |
| `application_hint` | string | null | no | Application Hint |

### `VMValidationRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `validation_run_id` | integer | yes | Validation Run Id |
| `vm_id` | integer | yes | Vm Id |
| `baseline_id` | integer | null | no | Baseline Id |
| `verdict` | VMValidationVerdict | yes |  |
| `collected_data` | object | null | no | Collected Data |
| `diff_result` | object | null | no | Diff Result |
| `host_key_changed` | boolean | yes | Host Key Changed |
| `failure_category` | string | null | no | Failure Category |
| `failure_detail` | string | null | no | Failure Detail |
| `validated_at` | string | yes | Validated At |

### `ValidationError`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `loc` | list[string | integer] | yes | Location |
| `msg` | string | yes | Message |
| `type` | string | yes | Error Type |

### `ValidationResultRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `vm_id` | integer | yes | Vm Id |
| `status` | ValidationStatus | yes |  |
| `summary` | string | yes | Summary |
| `findings` | list[object] | yes | Findings |
| `remediation` | list[object] | yes | Remediation |
| `diff` | object | yes | Diff |
| `validated_at` | string | yes | Validated At |

### `ValidationRunAccepted`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `validation_run_id` | integer | yes | Validation Run Id |
| `status` | ValidationRunStatus | yes |  |
| `status_url` | string | yes | Status Url |

### `ValidationRunCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ssh_key_id` | integer | yes | Ssh Key Id |
| `authorized_by` | string | null | no | Authorized By |
| `authorization_reason` | string | null | no | Authorization Reason |

### `ValidationRunRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `plan_id` | integer | yes | Plan Id |
| `wave_number` | integer | yes | Wave Number |
| `ssh_key_id` | integer | yes | Ssh Key Id |
| `status` | ValidationRunStatus | yes |  |
| `total_vms` | integer | yes | Total Vms |
| `passed_vms` | integer | yes | Passed Vms |
| `warned_vms` | integer | yes | Warned Vms |
| `failed_vms` | integer | yes | Failed Vms |
| `unreachable_vms` | integer | yes | Unreachable Vms |
| `progress_message` | string | null | no | Progress Message |
| `authorized_by` | string | null | no | Authorized By |
| `authorization_reason` | string | null | no | Authorization Reason |
| `started_at` | string | null | no | Started At |
| `completed_at` | string | null | no | Completed At |
| `created_at` | string | yes | Created At |
| `validations` | list[VMValidationRead] | no | Validations |

### `ValidationTaskRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task Id |
| `vm_id` | integer | yes | Vm Id |
| `status` | string ('running', 'completed', 'failed') | yes | Status |
| `current_step` | string ('queued', 'ssh_collecting', 'llm_reasoning', 'storing', 'completed', 'failed') | yes | Current Step |
| `progress_percent` | integer | yes | Progress Percent |
| `started_at` | string | yes | Started At |
| `completed_at` | string | null | no | Completed At |
| `validation_id` | integer | null | no | Validation Id |
| `verdict` | string | null | no | Verdict |
| `error` | string | null | no | Error |

### `WaveBaselineGap`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `name` | string | yes | Name |

### `WavePreviewResponse`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | yes | Plan Id |
| `wave_number` | integer | yes | Wave Number |
| `vm_count` | integer | yes | Vm Count |
| `requires_authorization` | boolean | yes | Requires Authorization |
| `authorization_reason` | string | yes | Authorization Reason |
| `ssh_operations_enabled` | boolean | yes | Ssh Operations Enabled |
| `vms` | list[WaveVMPreview] | yes | Vms |
| `baseline_missing` | list[WaveBaselineGap] *(default: `[]`)* | no | Baseline Missing |
| `baseline_last_completed_at` | string | null | no | Baseline Last Completed At |

### `WaveVMPreview`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_id` | integer | yes | Vm Id |
| `name` | string | yes | Name |
| `host` | string | yes | Host |
| `username` | string | yes | Username |
| `os_family` | string | null | yes | Os Family |
| `environment` | string | null | yes | Environment |
| `commands` | list[string] | yes | Commands |

### `app__schemas__settings__SSHKeyStatus`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | yes | Status |
| `algorithm` | string | null | no | Algorithm |
| `fingerprint` | string | null | no | Fingerprint |
| `public_key` | string | null | no | Public Key |
| `created_at` | string | null | no | Created At |
| `configured_algorithm` | string | null | no | Configured Algorithm |
| `expected_path` | string | null | no | Expected Path |
