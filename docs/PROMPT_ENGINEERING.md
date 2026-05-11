# Prompt Engineering — Validation

The validation LLM call lives in `app.core.llm.client.LLMClient.validate`.
This document is the rationale companion to that code — what each
section of the prompt is for, why it's structured the way it is, and
how the post-LLM parser enforces output quality.

The validation prompt is the most-tuned prompt in VirtValidate. It
runs against post-migration diffs in front of federal reviewers; the
output gets cached + reused across similar VMs; the parser rejects
non-conforming responses and triggers a retry-with-feedback. Three
forcing functions for prompt quality.

---

## Prompt structure (top → bottom)

### 1. Role + scope

> *"You are VirtValidate, an expert systems engineer validating a VM
> that was migrated from VMware to OpenShift Virtualization. The VM
> may be Linux (RHEL family, Debian/Ubuntu) or Windows Server
> (2019/2022/2025)."*

Sets the persona and pins the model to the migration context. The
"expert systems engineer" framing matters — it nudges the model
toward the level of detail an experienced sysadmin would write. The
explicit OS-coverage line warns it that Windows is in scope; without
it, Llama 3 8B tends to default to Linux-only terminology even when
the OS profile is Windows.

### 2. Input description

Enumerates exactly what the model is going to receive:

  - VM role / OS profile / hostname
  - pre-migration baseline (services, network, ports, mounts, cron)
  - post-migration current state (same shape)
  - precomputed structured diff

Without this section the model gets a wall of JSON and has to infer
which side is baseline vs current — wrong half the time on small
models. Stating the input order solves it.

### 3. Platform-appropriate terminology

> *"Use Linux terminology: systemd services, cron jobs, mounts,
> iptables/nftables. Use Windows terminology: Windows services,
> scheduled tasks, drives/volumes, Windows Firewall, AD membership,
> hotfixes."*

The user-message body injects an OS-specific reminder ("This is a
Windows VM — use Windows terminology in your findings:"). Belt +
suspenders: the system prompt sets the expectation, the user message
reinforces it per-call.

### 4. Severity ladder

```
CRITICAL: services that were running but aren't, missing network
  interfaces, missing mounts, missing application processes...
HIGH:     configuration drift on important services, missing cron jobs...
MEDIUM:   minor config differences, unexpected services running...
LOW:      expected migration artifacts (uptime, new PIDs, refreshed
  ssh host key)...
```

Without an explicit ladder, the model produces ratings that drift
across calls. Two operators looking at the same finding can disagree
about severity until you give the model the rubric.

### 5. Ignored-difference list

Names the artifacts the model should NOT flag: timestamps, uptime,
kernel patch level, PID changes, DHCP IPs in the same subnet, cosmetic
service churn. This dramatically reduces false-positive findings on
otherwise-clean migrations.

The Python diff layer already filters most of these (the `_diff_*`
helpers in ``app.core.llm.client``); the prompt section is a safety
net for anything that slips through.

### 6. Verdict mapping

```
"pass"  → no findings worse than LOW
"warn"  → one or more MEDIUM/HIGH findings, no CRITICAL
"fail"  → at least one CRITICAL finding
```

Makes the verdict deterministic given the findings. The structured
parser (see below) enforces this — a `pass` verdict with a critical
finding fails validation and triggers a retry.

### 7. Schema with required fields

The JSON schema specifies every field. The parser rejects responses
missing any of:

  - `findings[].source_evidence` — a quote or reference from the baseline
  - `findings[].current_evidence` — same from the current state
  - `findings[].remediation` — actionable next step
  - `findings[].confidence` — high / medium / low
  - `findings[].severity` — critical / high / medium / low / info
  - `findings[].title` — non-empty headline

These four required fields are what make a finding *useful* to a
sysadmin. Without `source_evidence` and `current_evidence` the
operator can't verify the LLM's claim. Without `remediation` the
finding becomes a complaint instead of a tool. Without `confidence`
the operator can't triage which findings to investigate first.

### 8. Few-shot examples (the load-bearing piece)

Three example outputs land at the end of the system prompt:

  - **Example A** — critical issue (database stopped) with two
    high-quality findings + concrete remediation commands.
  - **Example B** — expected post-migration change (kubevirt-agent
    appearing, eth0 → ens192 rename) with one *info*-severity
    finding and `status: pass`.
  - **Example C** — ambiguous case (service rename) with `low`
    confidence + uncertainty acknowledged in the remediation.

On Llama 3 8B, these few-shot examples roughly double the rate of
well-formed responses. Without them, the model produces
schema-conforming but vague findings ("a service has changed");
with them, findings reach the depth Example A shows.

The examples are deliberately **not** edge cases — they're the
shape of the most common verdicts. The model uses them as
templates; if the examples are weird, every output will be weird.

---

## Structured output validation

`LLMClient._parse_verdict` runs after every response. Validation
rules:

  - JSON well-formed (else: retry).
  - Top-level keys present (`status`, `summary`, `findings`,
    `remediation`).
  - `status ∈ {pass, warn, fail}` (else: retry).
  - Every finding has the 6 required fields above (else: retry).
  - Severity ∈ {critical, high, medium, low, info} (else: retry).
  - Confidence ∈ {high, medium, low} (else: retry).
  - **Verdict-severity consistency:** `pass` with `critical`/`high`/
    `medium` findings ⇒ reject; the LLM tried to underweight a
    real issue.

### The retry-with-feedback loop

When `_parse_verdict` raises, the validator catches and re-prompts
with the **original prompt + the rejected response + a corrective
message** ("Your response was missing X. Please re-emit the JSON
honoring the schema in the system prompt"). One retry, then give
up.

If both attempts fail, the verdict falls back to a *manual review
needed* shape:

```json
{
  "status": "warn",
  "summary": "LLM output did not match the required schema after retry. Manual operator review required.",
  "needs_manual_review": true,
  "findings": [],
  "remediation": []
}
```

The bulk pipeline records `needs_manual_review: true` in the audit
log; the admin dashboard surfaces a count so operators can
investigate the affected VMs without the whole bulk run aborting.

The bad response is **never cached** — only well-formed verdicts
land in `validation_llm_cache`. This avoids poisoning the cache
with garbage from a one-off model glitch.

---

## What we don't do

### We don't summarize the diff before sending it

The structured diff is already minimal — added/removed lists per
category. Asking the model to re-summarize ("now describe the diff
in plain English") would waste tokens without improving accuracy.

### We don't break the call into "what changed" + "why concerning"

A two-step decomposition (chain-of-thought) sounds good in theory but
on small models doubles token cost without lifting quality. The
few-shot examples already model the implicit reasoning ("X happened,
which means Y").

### We don't run multiple LLM calls per VM

The classifier in ``app.core.validation_tiers`` is the only routing
decision. If Tier 1 or Tier 2 covers the verdict, no LLM call. If
Tier 3, one LLM call with caching across VMs that share the diff.
We never invoke the LLM twice per VM.

---

## When to tune

### "Findings are too vague"

Inspect the few-shot examples. Llama 3 8B will mimic their depth;
if the examples show one-sentence findings, the model produces
one-sentence findings. Lengthen the examples (Example A's
remediation has multi-line commands and concrete file paths).

### "Findings flag every cosmetic difference"

Extend the ignored-difference list in section 5. Or — better —
extend the Python diff filter so the model never sees those changes
in the first place.

### "Severity ladder feels wrong for our environment"

Federal customers' definition of "critical" varies. The ladder is
in section 4; rewrite to match the customer's incident-severity
glossary. The parser doesn't care about specific ladder text — only
that the `severity` field on each finding is one of the allowed
values.

### "Manual-review fallback fires too often"

Check the audit log for the parser errors. The two common cases:

  - Model truncated mid-response (context window too small) —
    raise `OLLAMA_NUM_CTX` or shrink the diff.
  - Model omitted `source_evidence` — strengthen the few-shot
    Example A so it reads as a template for the schema fields.

---

## Related code + docs

- `app.core.llm.client.SYSTEM_PROMPT` — the live prompt.
- `app.core.llm.client.LLMClient._parse_verdict` — the validator.
- `docs/VALIDATION_SCALING.md` — tier classification + caching.
- `docs/LLM_TUNING.md` — context window + retry transport behavior.
