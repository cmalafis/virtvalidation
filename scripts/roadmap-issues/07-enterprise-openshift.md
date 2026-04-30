Optional enterprise deployment mode for customers running OpenShift
with RHOAI.

## Scope

- Deploy as OpenShift workloads via Helm chart.
- KServe model serving instead of Ollama (multi-user, GPU-shared).
- ArgoCD GitOps for VirtValidate's own configuration.
- Optional Elyra pipeline visualization for workflow transparency.
- Multi-tenancy via OCP namespaces.

## Constraints

- The podman-compose appliance remains the **default** deployment
  model. Enterprise mode is an upgrade path, not a replacement.
- Single source of truth for the API + UI: the same backend and
  frontend images run in both modes. Differences are only in
  packaging, scheduler placement, and inference backend.
- Air-gap parity: enterprise mode must work with internal registries
  + offline Helm charts; no upstream calls at runtime.

## Why

Customers already running RHOAI / OpenShift AI prefer to consolidate
on platform-native primitives — KServe for inference, ArgoCD for
config drift detection, namespaces for tenant isolation. The
appliance form-factor doesn't fit that mental model; this issue
captures the bridge.

## Acceptance criteria

- [ ] Helm chart in `infra/helm/` deploys backend + frontend +
  Postgres + (optionally) Ollama as a fallback when KServe is not
  available.
- [ ] LLM client supports both Ollama (default) and KServe-served
  endpoints behind a feature flag.
- [ ] ArgoCD `Application` manifest example in `infra/argocd/`.
- [ ] Documentation: `docs/ENTERPRISE.md` covers the upgrade path
  from appliance → enterprise without re-enrolling VMs.
- [ ] Tests gate the OCP-mode wiring with a kind-based integration
  test (not part of the regular CI matrix; a separate workflow).
