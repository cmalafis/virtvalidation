{{/*
Common helpers used across the chart's templates. Keep these short —
every helper here is a contract every template depends on, so rename
costs ripple.
*/}}

{{- define "virtvalidate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully-qualified name. Used as the prefix for every Kubernetes resource
the chart creates so multiple installs in one namespace can coexist.
*/}}
{{- define "virtvalidate.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "virtvalidate.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource. Keep selectorLabels separate
because Deployment/Service selectors are immutable post-creation —
mixing in version labels there breaks Helm upgrades.
*/}}
{{- define "virtvalidate.labels" -}}
helm.sh/chart: {{ include "virtvalidate.chart" . }}
{{ include "virtvalidate.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: virtvalidate
{{- end -}}

{{- define "virtvalidate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "virtvalidate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component selector labels — distinguishes the backend pod from the
frontend pod from postgres etc. Each Deployment passes its component
name; the Service uses the same name to target the right pod set.
*/}}
{{- define "virtvalidate.componentSelectorLabels" -}}
{{ include "virtvalidate.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "virtvalidate.componentLabels" -}}
{{ include "virtvalidate.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Image reference resolver. Falls back through:
  - per-component .image.tag
  - global .Values.image.tag
  - .Chart.AppVersion
*/}}
{{- define "virtvalidate.image" -}}
{{- $registry := default .root.Values.image.registry .imageRegistry -}}
{{- $repo := .imageRepo -}}
{{- $tag := default (default .root.Chart.AppVersion .root.Values.image.tag) .imageTag -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end -}}

{{/*
Service-account name — surfaced by every Deployment/StatefulSet so
existing-SA mode (serviceAccount.create=false + serviceAccount.name=…)
works without per-template branching.
*/}}
{{- define "virtvalidate.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "virtvalidate.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Postgres connection URL — assembled here so the backend Deployment and
the migration Job (when added) both reference the same logic. Reads
from the chart-managed secret; when an existing secret is referenced,
the URL is built with placeholder vars that envsubst at runtime.
*/}}
{{- define "virtvalidate.databaseUrl" -}}
{{- $host := printf "%s-postgres" (include "virtvalidate.fullname" .) -}}
{{- $port := .Values.postgres.service.port | toString -}}
{{- $db := .Values.postgres.auth.database -}}
postgresql+psycopg2://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@{{ $host }}:{{ $port }}/{{ $db }}
{{- end -}}

{{/*
Resolved Postgres secret name. Either the operator's existing secret
or the chart-generated one.
*/}}
{{- define "virtvalidate.postgresSecretName" -}}
{{- if .Values.postgres.auth.existingSecret -}}
{{- .Values.postgres.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-postgres" (include "virtvalidate.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
LLM backend env block — common across every LLM choice. Component-
specific blocks (KServe token mount, Ollama endpoint) live in the
backend Deployment template directly.
*/}}
{{- define "virtvalidate.llmEnv" -}}
- name: LLM_BACKEND_TYPE
  value: {{ .Values.llm.backend | quote }}
{{- if eq .Values.llm.backend "ollama" }}
- name: OLLAMA_HOST
  value: "http://{{ include "virtvalidate.fullname" . }}-ollama:{{ .Values.llm.ollama.service.port }}"
- name: OLLAMA_MODEL
  value: {{ .Values.llm.ollama.model | quote }}
{{- else if eq .Values.llm.backend "kserve" }}
- name: KSERVE_ENDPOINT
  value: {{ required "llm.kserve.endpoint is required when backend=kserve" .Values.llm.kserve.endpoint | quote }}
- name: KSERVE_MODEL_NAME
  value: {{ required "llm.kserve.modelName is required when backend=kserve" .Values.llm.kserve.modelName | quote }}
- name: KSERVE_VERIFY_SSL
  value: {{ .Values.llm.kserve.verifySsl | quote }}
- name: KSERVE_TIMEOUT_SECONDS
  value: {{ .Values.llm.kserve.timeoutSeconds | quote }}
{{- if .Values.llm.kserve.tokenSecret }}
- name: KSERVE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.llm.kserve.tokenSecret }}
      key: {{ .Values.llm.kserve.tokenSecretKey }}
{{- end }}
{{- else if eq .Values.llm.backend "vllm" }}
- name: VLLM_ENDPOINT
  value: {{ .Values.llm.vllm.endpoint | quote }}
- name: VLLM_MODEL_NAME
  value: {{ .Values.llm.vllm.modelName | quote }}
{{- end }}
{{- end -}}
