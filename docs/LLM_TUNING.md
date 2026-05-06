# LLM Tuning

VirtValidate runs every reasoning task — VM categorization, plan
generation, design review, validation verdicts, MTV YAML rationales —
through a local LLM. On the default appliance (Ollama + Llama 3 8B on
CPU) those calls dominate end-to-end latency. This guide is what to
turn when the model is failing for one of three reasons:

  - prompts are getting truncated,
  - calls are timing out,
  - output quality is degrading.

If you're operating in production and the categorizer never finishes,
start with [Symptom: prompts truncate at 4096](#symptom-prompts-truncate-at-4096)
below.

---

## How the appliance talks to the LLM

```
                                    ┌─────────────────────┐
                                    │  Ollama / KServe /  │
                                    │       vLLM          │
                                    └──────────┬──────────┘
                                               │ HTTP
                          ┌────────────────────┴───────────────┐
                          │  app.core.llm.ollama_backend       │
                          │   - num_ctx                        │
                          │   - 600s read timeout              │
                          │   - 5s/30s retry backoff           │
                          │   - prompt-size guardrail logging  │
                          └────────────────────┬───────────────┘
                                               │ chat_sync
              ┌────────────────────────────────┼──────────────────────┐
              │ Categorizer (Level 1)          │ Strategy planner     │
              │   batch_size = 10 VMs/call     │   one call per plan  │
              └────────────────────────────────┴──────────────────────┘
```

Every reasoning module funnels through `app/core/llm/<backend>.py` so
tuning happens in one place.

---

## Why prompt size matters with Llama 3 8B

Llama 3 8B has a 8192-token context. Ollama's *default* serves 4096 —
it's a memory-pressure compromise for small hosts, but it silently
truncates anything bigger.

**Truncation is the worst failure mode** because:

  - The model still produces fluent JSON, so the wrapper doesn't see
    an error.
  - But the back half of the prompt was never seen, so VMs at the tail
    are missing from the output.
  - The categorizer wraps that into "groups" and the operator sees a
    quietly-incomplete result.

In Ollama logs the giveaway is:

```
truncating input prompt limit=4096 prompt=10420
```

The fix has two halves:

  1. Bump the configured `num_ctx` on every chat call (the appliance
     does this automatically — `OLLAMA_NUM_CTX=8192` by default).
  2. Keep batches small enough that the prompt + response fits.
     Categorizer ships with `CATEGORIZER_BATCH_SIZE=10`, sized for
     8192-token context.

### Token budget rule of thumb

| Component | ~tokens |
|-----------|--------:|
| System prompt (categorizer) | ~600 |
| 1 VM (lite) in JSON | ~50–80 |
| 10-VM batch payload | ~700–900 |
| Total prompt | ~1300–1500 |
| Headroom for JSON response | ~6700 |

Comfortable. A 100-VM batch (the v0.1.x default) lands around 7K
tokens of input alone — fine on paper, but the model's attention
degrades on long lists, and any extra system-prompt revisions push
over the limit.

---

## Batch sizes — quality vs speed

The categorizer ships with `CATEGORIZER_BATCH_SIZE=10` because that's
the sweet spot on Llama 3 8B / CPU. Tradeoffs:

| Batch size | Speed (5K-VM fleet) | Quality | When |
|-----------:|--------------------:|---------|------|
| 5 | ~6 hours | High — model has full attention | LLM is hallucinating tail entries |
| 10 (default) | ~3 hours | Good | Llama 3 8B / Ollama / CPU |
| 25 | ~1.5 hours | OK on Llama 3.1+ | KServe with Llama 3.1 8B (32K context) |
| 50 | ~45 min | Risky on small models | Larger models (70B), GPU inference |
| 100+ | ~25 min | Truncates on default Ollama | Don't, unless you've raised num_ctx and tested |

**Halve the batch size whenever you bump model size.** Larger models
have more parameters to keep coherent — they don't necessarily handle
larger lists better.

---

## When local LLM hits its limits

Signs the appliance has outgrown Llama 3 8B:

  1. **Categorizer hallucination rate climbs.** Operators see groups
     named "default" or "unclassified" for VMs whose `application_hint`
     is clearly set. The 8B model is dropping evidence in long lists.

  2. **Plan generation rationales repeat.** A 5K-VM plan produces
     identical wave rationales — the model is running out of unique
     prose for so many waves.

  3. **Design review confidence drops.** Findings drift toward "low"
     confidence even when the YAML clearly contradicts customer notes.

For these, swap to KServe-served Llama 3.1 70B or Mistral Large.
The integration is a config-only change:

```env
LLM_BACKEND_TYPE=kserve
KSERVE_ENDPOINT=https://llama-3-1-70b.openshift-ai.cluster.local
KSERVE_MODEL_NAME=llama-3.1-70b-instruct
KSERVE_TIMEOUT_SECONDS=600
CATEGORIZER_BATCH_SIZE=25
```

The orchestrators don't change. Quality lifts noticeably; throughput
depends on cluster sizing.

---

## Recommended settings by deployment scale

### Lab / single operator / under 500 VMs

```env
OLLAMA_NUM_CTX=8192
LLM_READ_TIMEOUT=600
LLM_MAX_RETRIES=2
CATEGORIZER_BATCH_SIZE=10
OLLAMA_MODEL=llama3:8b
```

Default. Categorizer finishes in minutes for typical lab fleets.

### Federal customer / 1K–10K VMs / single appliance

```env
OLLAMA_NUM_CTX=8192
LLM_READ_TIMEOUT=900
LLM_MAX_RETRIES=2
CATEGORIZER_BATCH_SIZE=10
OLLAMA_MODEL=llama3:8b
```

Same as lab — but with the longer timeout to absorb cold-model loads
between work sessions. Categorization for 10K VMs runs ~6 hours
(1000 batches × ~20s/batch on warm Ollama). Run it on a Friday
afternoon; the operator picks up Monday with categorized inventory.

### KServe / RHOAI / 70B model

```env
LLM_BACKEND_TYPE=kserve
KSERVE_ENDPOINT=https://...
KSERVE_MODEL_NAME=llama-3.1-70b-instruct
KSERVE_TIMEOUT_SECONDS=600
LLM_READ_TIMEOUT=600
LLM_MAX_RETRIES=2
CATEGORIZER_BATCH_SIZE=25
```

KServe scales horizontally — multiple pods serve concurrent calls.
Bump `CATEGORIZER_BATCH_SIZE` once you've validated quality on a
representative slice.

### Air-gapped / FIPS-mode

```env
OLLAMA_NUM_CTX=8192
LLM_READ_TIMEOUT=900
LLM_MAX_RETRIES=2
CATEGORIZER_BATCH_SIZE=10
FIPS_MODE=true
```

No external calls anyway. The same Ollama profile applies; FIPS only
constrains key material, not model selection.

---

## Symptom: prompts truncate at 4096

```
ollama  | level=WARN msg="truncating input prompt limit=4096 prompt=10420"
backend | LLMBackendError: Ollama returned an empty message
```

Check, in order:

  1. **Is `OLLAMA_NUM_CTX=8192` set?** Look at
     `GET /api/health/full` → `components.llm.details.num_ctx`. If
     it's missing or 4096, set the env var and restart the backend.
  2. **Is the batch size right?** `app.core.categorizer.DEFAULT_BATCH_SIZE`
     should be 10 (or whatever you set via
     `CATEGORIZER_BATCH_SIZE`). If you see an *old* trigger using a
     larger size, retrigger after the restart.
  3. **Are operators passing custom `batch_size`?** The trigger
     endpoint accepts `?batch_size=...` so an over-eager setting from
     the UI can override the default. Verify via the audit log
     entry `categorization.triggered`.

---

## Symptom: 120-second timeouts

```
backend | httpx.ReadTimeout: timed out
backend | LLMBackendError: Ollama request failed after 1 attempts
```

The default `LLM_READ_TIMEOUT=600` should cover most CPU-Llama-3-8B
calls. If you're seeing timeouts above that:

  1. **Is the Ollama host swapping?** Check `podman stats ollama`
     for sustained 100% CPU and high memory. The 8B model wants
     ~6GB RAM resident; sub-8GB hosts will swap and double LLM
     latency.
  2. **Is there a queue?** Ollama is single-stream — multiple
     orchestrators (categorizer + planner + reviewer) hitting it
     concurrently serialize. Plan around it: trigger one long
     categorization, *then* the planner.
  3. **Did the model just cold-load?** First call after a restart
     takes 5–15s longer than steady-state. The retry backoff (5s,
     30s) absorbs this — but if every call is cold-starting, the
     Ollama container is being killed between requests; check
     memory limits on the Pod / Quadlet.

---

## Symptom: long-running categorization dies mid-run on dev

The dev compose mounts source from the host with uvicorn `--reload`.
Any change to a Python file under `app/` triggers a worker restart,
which terminates the in-flight `BackgroundTask` and discards the
partially-built groups. Symptoms:

  - Task status frozen at `current_step=calling_llm` with the same
    `batches_complete` for several minutes.
  - Ollama logs a 5xx for the last in-flight `/api/chat`.
  - `/api/sources/vcenters/{id}/categorize/{task_id}` 404s after
    backend restart (in-memory task store is wiped on reload).

In dev: avoid editing app code while the categorizer is running, or
trigger from a separate session and don't push code until the task
completes. In prod (Quadlet / Helm) auto-reload isn't enabled, so
the issue doesn't recur — but a forced container restart still
discards the in-memory task. Re-trigger after restarts.

---

## Symptom: categorizer reports `batches_total=1` for many VMs

The fix shipped: `DEFAULT_BATCH_SIZE` is now 10 in code, sourced
from `Settings.categorizer_batch_size` which reads
`CATEGORIZER_BATCH_SIZE` from env.

If you still see `batches_total=1` for a 50+ VM run after upgrading:

  - **Stale container.** `podman restart virtvalidation_backend_1`
    and retrigger.
  - **Operator override.** Check the trigger audit log:
    `GET /api/audit?action=categorization.triggered` → look at
    `details.batch_size`. If it's >50, the UI / API caller is
    overriding the default. Drop the override.
  - **Single-batch fleet.** A vCenter with under 11 VMs *will*
    produce 1 batch — that's correct.

---

## Tuning checklist for new deployments

When standing up a new appliance against a real customer fleet:

  - [ ] `GET /api/health/full` returns `components.llm.details.num_ctx ≥ 8192`.
  - [ ] Trigger categorization on a 50-VM scope and confirm
        `batches_total ≥ 5` in the task status.
  - [ ] Review the first batch's groups in the dashboard — every
        VM should land in exactly one application + one environment +
        one business_unit group.
  - [ ] Check `ollama` container logs for `truncating input prompt`
        warnings during the run. None should appear.
  - [ ] On a representative plan generation, confirm the rationale
        text differs across waves (no copy-paste). If rationales
        repeat, batch size is too high or the model is too small.

---

## Related docs

- `docs/CONFIGURATION.md` — full env-var reference.
- `docs/SCALE.md` — how the categorizer / planner pipeline scales.
- `docs/LLM_PROMPTS.md` — system prompts the orchestrators use.
- `docs/PLANNING_GUIDE.md` — strategy-driven plan generation.
