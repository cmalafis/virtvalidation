# Demo assets

Everything needed to rebuild the simulated VMware → OCP-Virt migration
validation scenario described in [`../../docs/DEMO_WALKTHROUGH.md`](../../docs/DEMO_WALKTHROUGH.md).

These are **demo scaffolding, not product code.** Nothing here ships in a
container image or is referenced by the application.

| File | What it is |
|---|---|
| `testvm.yaml` | RHEL 9 VM + ClusterIP Service exposing sshd, sized for the Developer Sandbox |
| `workload-setup.sh` | Builds the fake payments tier — 4 systemd units, a cron entry, a bind-mounted spool |
| `inject-faults.sh` | The six post-cutover faults, one per diff dimension |
| `guardrails_sim.py` | **Simulated** TrustyAI Guardrails Orchestrator (see below) |
| `guardrails-simulator.yaml` | Deployment + Service for the simulator |

## The workload is Python, deliberately

The sandbox RHEL 9 image has no yum repos and no subscription, so packaged
daemons cannot be installed. The units run `python3 -m http.server`. The
collector reads `systemctl list-units --state=running` and `ss -tulnH`, and by
those measures a Python listener and a real daemon are identical — same unit
state, same socket, same diff behavior.

## The orchestrator is a simulator

`guardrails_sim.py` is **not** RHOAI TrustyAI. The Developer Sandbox has no
`GuardrailsOrchestrator` CRD and this namespace cannot create
`TrustyAIService` resources, so there is nothing real to point at.

What is real: the wire protocol. It serves
`POST /api/v2/chat/completions-detection` per the FMS-Guardrails contract, so
VirtValidate's `trustyai` backend runs completely unmodified against it. Clean
requests are proxied to the genuine MaaS Granite endpoint, so model answers are
real. A detector hit returns a 200 with a `warnings` array and structured
`detections`, which is exactly what triggers `LLMGuardrailError`.

What is simulated: the detector's judgement. A real orchestrator runs a trained
classifier; this runs a heuristic pattern set. Every detection payload carries

```
"detector_backend": "SIMULATED - heuristic pattern matcher, not RHOAI TrustyAI"
```

so any evidence persisted downstream is self-labelling and cannot later be
mistaken for a genuine orchestrator result. The same string is returned in an
`X-Guardrails-Backend` response header.

## Rebuilding the scenario

Assumes VirtValidate is already deployed and the `virtvalidate-maas` Secret
exists.

```bash
NS=cmalafis-dev

# 1. VM. The sandbox permits only runStrategy: Manual, and its webhook
#    rewrites cloud-init to inject its own key — hence the explicit start.
oc apply -f testvm.yaml -n $NS
oc replace --raw "/apis/subresources.kubevirt.io/v1/namespaces/$NS/virtualmachines/vv-testvm-01/start" -f /dev/null

# 2. Put the wave-scoped SSH public key (GET /api/ssh-keys/{id}/public) into
#    the VM's authorized_keys, then build the workload:
#    scp workload-setup.sh && sudo bash workload-setup.sh

# 3. Register the estate and baseline through the API — see the walkthrough.

# 4. Break it, then validate:
#    sudo bash inject-faults.sh

# 5. Guardrail demo
oc create configmap guardrails-simulator-code -n $NS --from-file=guardrails_sim.py
oc apply -f guardrails-simulator.yaml -n $NS

helm upgrade virtvalidate ../helm/virtvalidate/ -n $NS --reuse-values \
  --set-json 'extraEnv=[
    {"name":"LLM_TRUSTYAI_BASE_URL","value":"http://guardrails-simulator:8080"},
    {"name":"LLM_TRUSTYAI_MODEL","value":"granite-3-2-8b-instruct"},
    {"name":"LLM_TRUSTYAI_INPUT_DETECTORS","value":"prompt_injection"}]'
```

`extraEnv` is required because the chart has no `llm.trustyai` block — that is
Finding 12 in the deployment analysis.

Then set a VM's `application_hint` to an injection payload and generate a plan.

## Teardown

```bash
oc delete -f guardrails-simulator.yaml -n $NS
oc delete configmap guardrails-simulator-code -n $NS
oc delete -f testvm.yaml -n $NS
oc delete dv vv-testvm-01-root -n $NS
```
