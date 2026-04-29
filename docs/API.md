# VirtValidate API

**Auto-generated** from the FastAPI OpenAPI spec — re-run `python scripts/generate_api_docs.py` after route changes.

- **Version**: 0.1.0
- **Live spec**: `GET /openapi.json`
- **Swagger UI**: `/docs` (when the backend is running)

VM migration validation platform — air-gapped, local LLM

## Endpoints

### audit

#### `GET /audit`

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

### health

#### `GET /health/ollama`

_Ollama Health_

Probe the local Ollama server. Air-gapped — never calls external APIs.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `HealthStatus` | Successful Response |

---

#### `GET /health/postgres`

_Postgres Health_

Run a SELECT 1 against the configured database.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `HealthStatus` | Successful Response |

---

### misc

#### `GET /health`

_Health_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `—` | Successful Response |

---

### plans

#### `GET /plans`

_List Plans_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `limit` | query | no | integer *(default: `20`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[PlanRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /plans`

_Create Plan_

**Request body** (**required**): `PlanCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `PlanRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `GET /plans/{plan_id}`

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

#### `GET /plans/{plan_id}/waves/{wave_number}/report`

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

#### `GET /plans/{plan_id}/waves/{wave_number}/report/pdf`

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

### settings

#### `GET /settings`

_Get Settings_

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `AppSettingsRead` | Successful Response |

---

#### `PUT /settings`

_Update Settings_

**Request body** (**required**): `AppSettingsUpdate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `AppSettingsRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

### system

#### `GET /system/ollama-models`

_Ollama Models_

List models currently pulled in the local Ollama instance.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `OllamaModelsResponse` | Successful Response |

---

#### `GET /system/ssh-public-key`

_Ssh Public Key_

Return the OpenSSH public key VirtValidate uses. Never the private key.

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `SSHPublicKey` | Successful Response |

---

### vms

#### `GET /vms`

_List Vms_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `status` | query | no | object \| null |  |
| `limit` | query | no | integer *(default: `100`)* |  |
| `offset` | query | no | integer *(default: `0`)* |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `list[VMRead]` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /vms`

_Create Vm_

**Request body** (**required**): `VMCreate`


**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `201` | `VMRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

#### `POST /vms/bulk`

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

#### `DELETE /vms/{vm_id}`

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

#### `GET /vms/{vm_id}`

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

#### `PATCH /vms/{vm_id}`

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

#### `GET /vms/{vm_id}/baseline/history`

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

#### `GET /vms/{vm_id}/baseline/profile`

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

#### `GET /vms/{vm_id}/snapshots`

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

#### `POST /vms/{vm_id}/snapshots`

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

#### `GET /vms/{vm_id}/snapshots/{snapshot_id}`

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

#### `GET /vms/{vm_id}/validation/latest`

_Latest Validation_

**Parameters**

| Name | In | Required | Type | Description |
|------|----|----------|------|-------------|
| `vm_id` | path | yes | integer |  |

**Responses**

| Status | Body | Description |
|--------|------|-------------|
| `200` | `ValidationResultRead` | Successful Response |
| `422` | `HTTPValidationError` | Validation Error |

---

## Schemas

### `AppSettingsRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `ollama_model` | string | yes | Ollama Model |
| `schedule_preset` | SchedulePreset | yes |  |
| `updated_at` | string | yes | Updated At |

### `AppSettingsUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ollama_model` | string | null | no | Ollama Model |
| `schedule_preset` | SchedulePreset | null | no |  |

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

### `BulkVMCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vms` | list[VMCreate] | yes | Vms |

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

### `HTTPValidationError`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `detail` | list[ValidationError] | no | Detail |

### `HealthStatus`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | yes | Status |
| `host` | string | null | no | Host |
| `version` | string | null | no | Version |
| `latency_ms` | integer | null | no | Latency Ms |
| `error` | string | null | no | Error |

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

### `PlanCreate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `vm_ids` | list[integer] | yes | Vm Ids |

### `PlanRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `vm_ids` | list[integer] | yes | Vm Ids |
| `waves` | list[WaveRead] | yes | Waves |
| `summary` | string | null | yes | Summary |
| `model` | string | yes | Model |
| `created_at` | string | yes | Created At |

### `SSHPublicKey`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `public_key` | string | yes | Public Key |
| `fingerprint` | string | null | no | Fingerprint |
| `type` | string *(default: `ssh-ed25519`)* | no | Type |

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
| `notes` | string | null | no | Notes |

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
| `notes` | string | null | no | Notes |
| `id` | integer | yes | Id |
| `status` | VMStatus | yes |  |
| `created_at` | string | yes | Created At |
| `updated_at` | string | yes | Updated At |

### `VMUpdate`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `source_hostname` | string | null | no | Source Hostname |
| `target_hostname` | string | null | no | Target Hostname |
| `ip_address` | string | null | no | Ip Address |
| `os_family` | string | null | no | Os Family |
| `role` | string | null | no | Role |
| `ssh_user` | string | null | no | Ssh User |
| `status` | VMStatus | null | no |  |
| `notes` | string | null | no | Notes |

### `ValidationError`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `loc` | list[string | integer] | yes | Location |
| `msg` | string | yes | Message |
| `type` | string | yes | Error Type |
| `input` | object | no | Input |
| `ctx` | object | no | Context |

### `ValidationResultRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | yes | Id |
| `vm_id` | integer | yes | Vm Id |
| `status` | string ('pass', 'warn', 'fail') | yes | Status |
| `summary` | string | yes | Summary |
| `findings` | list[object] | yes | Findings |
| `remediation` | list[object] | yes | Remediation |
| `diff` | object | yes | Diff |
| `validated_at` | string | yes | Validated At |

### `WaveRead`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `wave_number` | integer | yes | Wave Number |
| `vm_ids` | list[integer] | yes | Vm Ids |
| `rationale` | string | yes | Rationale |
| `estimated_risk` | string ('low', 'medium', 'high') | yes | Estimated Risk |
