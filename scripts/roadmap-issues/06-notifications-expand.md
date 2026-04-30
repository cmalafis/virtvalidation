## Expansion — additional integrations + payloads

Folding more concrete scope onto the existing notification work:

### Email

- Email each wave's PDF report as an attachment to a stakeholder
  list configured per-plan (or globally, with per-plan overrides).
- SMTP-only (no third-party email service); air-gap-friendly.
- Subject line includes verdict status (HEALTHY / DEGRADED / FAILED)
  so triage queues can route from the inbox.
- Body has the wave summary inline; the PDF is the deep dive.

### Slack

- Send a notification on validation completion with summary
  statistics (verdict counts, wave number, plan ID).
- One channel per environment (`#vv-prod`, `#vv-dev`) configurable.
- Threaded follow-ups for individual VM failures (avoids channel
  noise — main message is the executive summary, thread is the detail).
- Webhook-only integration (no Slack OAuth app); keeps deployment
  simple and works behind air-gapped Slack Connect.

### Generic webhooks

- Already part of the original issue scope; this expansion adds a
  documented JSON schema for the webhook payload so customers can
  wire ServiceNow / PagerDuty / Splunk consumers without reverse-
  engineering the format.

## Why now

Operators currently pull-poll the dashboard. Push notifications close
the loop and let customers integrate VirtValidate verdicts into the
incident-management surfaces they already monitor.
