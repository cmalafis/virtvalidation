# Mock LLM Backend

## Purpose

Local development testing without a real LLM. Returns canned JSON
responses instantly so you can validate architecture and workflows
without waiting on inference. A 1,000-VM categorization that takes
~30 minutes on Ollama+Llama 3 8B runs in well under 30 seconds
through MockBackend — fast enough that the inner dev loop stays
interactive.

## When to use

- Iterating on the chunker / planner code paths
- Testing the inventory pagination + filter UI without real LLM
  latency in the loop
- Running CI without a model server
- Smoke-testing the validation, categorization, and planning flows
  end-to-end without staging an Ollama install
- Verifying schema changes to LLM-consuming code (does the parser
  still accept the canned shape?)

## When NOT to use

- Validating LLM **output quality** — every response is a fixed
  template
- Customer-facing demos (mock leaks "canned" warnings in every
  health / info response so this would be visible anyway)
- Production deployments (the factory accepts `mock` because tests
  set `LLM_BACKEND_TYPE=mock`, but the deploy pipeline should never
  ship a config that sets it)
- Judging plan / verdict / grouping quality — by construction the
  mock can't deliver insight beyond what the templates contain

## Configuration

```bash
# .env
LLM_BACKEND_TYPE=mock
```

That's the only knob. There are no separate timeouts, model
selectors, or endpoint URLs — the mock is in-process and stateless.

When the app starts with mock mode, `/api/health/llm` reports:

```json
{
  "status": "online",
  "backend": "mock",
  "model": "mock-llm",
  "endpoint": "memory://mock",
  "latency_ms": 0,
  "details": {
    "purpose": "local development testing",
    "warning": "responses are canned, not real LLM reasoning — do not use for customer demos or quality validation"
  }
}
```

The warning is intentional and unavoidable — if someone runs the
appliance in mock mode by accident the operator-facing health
panel surfaces it loudly.

## Capabilities

Mock backend advertises generous capacity attributes so the
hierarchical planner doesn't artificially chunk during dev runs:

| Attribute                  | Value     |
|----------------------------|-----------|
| `max_planning_chunk_size`  | 50        |
| `max_context_tokens`       | 32,000    |
| `supports_concurrent_calls`| True      |
| `max_concurrent_calls`     | 5         |

Compare with Ollama's 15-VM chunk + 8192-token context, which is
the real ceiling on a single-host appliance.

## Intent detection

Mock inspects the concatenated prompt content and routes to one of
five response templates:

| Intent       | Keywords matched (case-insensitive)               | Template |
|--------------|---------------------------------------------------|----------|
| categorize   | "level 1 categorizer", "categorize", "business_unit", "application group", `kind": "application` | Groups: application + environment + business_unit, every input vm_id covered |
| plan         | "wave_number", "wave plan", "migration plan", "planner", "waves to migrate" | One wave per vm_id (foundation first, dependents second) |
| topology     | "topology", "load_balanced", "ha pair", "cluster pattern" | detected_patterns: load_balanced with all visible vm_ids |
| validate     | "post-migration", "verdict", "diff vs baseline", "validate", "findings" | status: pass, empty findings + remediation |
| generic      | (fallback)                                        | Permissive notes / summary dict |

The detector scans the prompt for `vm_id` integers and `name`
strings so the canned responses reference whatever the orchestrator
fed in — the planner won't reject a wave with hallucinated ids.

## Limitations

- Responses are deterministic templates, not real reasoning. Same
  input always yields the same output (helpful for tests, useless
  for quality validation).
- No actual analysis of VM state — validation always returns "pass"
  with no findings.
- No prompt-engineering exercise. If you're testing whether a new
  system prompt makes Llama behave better, use the Ollama backend.
- Topology / categorization templates collapse all inputs into a
  single canned group; the mock does not infer real groupings.

## Switching to a real backend

Change `LLM_BACKEND_TYPE` and restart:

| Value     | Backend |
|-----------|---------|
| `ollama`  | Local Ollama at `OLLAMA_HOST` (default: `http://ollama:11434`) |
| `kserve`  | RHOAI / KServe InferenceService |
| `vllm`    | Standalone vLLM endpoint |
| `mock`    | This dev-only backend |

See `docs/CONFIGURATION.md` for the per-backend env vars (Ollama
model name, KServe token file, etc.).

## Extending: adding a new intent

When you add a new LLM-consuming flow:

1. Pick a stable keyword that the new flow's prompt always
   contains (e.g. `"capacity advisor"` for a hypothetical capacity
   planning use case).
2. Add it to `_INTENT_KEYWORDS` in
   `backend/app/core/llm/mock_backend.py` — order matters; more
   specific keywords come first.
3. Write a `_<intent>_response()` method that returns the **same
   JSON shape the real orchestrator's parser expects**. If the
   parser changes later, this method needs to track.
4. Add a test in `backend/tests/test_mock_backend.py` that
   round-trips the response through the real parser. This is the
   only way to keep mock + real responses in lockstep.

## Hard rule

`LLM_BACKEND_TYPE=mock` must never be set in any committed
production config (Helm values, Quadlet units, customer-facing
.env.example). The factory accepts it because tests need it; the
deploy pipeline should not.
