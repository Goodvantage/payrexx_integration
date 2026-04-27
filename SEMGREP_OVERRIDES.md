# SEMGREP_OVERRIDES

## `guest-whitelisted-method` in `payrexx_integration/api.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest-readable endpoints that expose or mutate private payment data.
- Why this override is safe: `pay_invoice` is intentionally public because invoice emails link to it. The endpoint rejects every request without a deterministic HMAC token generated from the site encryption key and the Sales Invoice name, and it only creates or reuses a Payment Request for that verified invoice.

## `guest-whitelisted-method` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest webhook endpoints that accept unauthenticated payment status changes.
- Why this override is safe: `callback` is intentionally public for Payrexx webhooks, but it verifies `X-Webhook-Signature` with the per-webhook signing key before touching any Integration Request. Unknown or unsigned payloads are rejected or ignored without applying payment side effects.
