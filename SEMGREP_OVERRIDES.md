# SEMGREP_OVERRIDES

## `guest-whitelisted-method` in `payrexx_integration/api.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest-readable endpoints that expose or mutate private payment data.
- Why this override is safe: `pay_invoice` and `payment_success` are intentionally public GET redirect endpoints because invoice emails and Payrexx checkout returns link to them. `pay_invoice` rejects every request without a deterministic HMAC token generated from the site encryption key, Sales Invoice name, and selected gateway. Under current locking reads, it only creates or reuses a checkout while the invoice is wholly unpaid, the submitted Payment Request remains `Requested` and fully outstanding, and persisted amount/currency/source/gateway/provider metadata match exactly; changed receivables fail before provider contact. Legacy invoice-only tokens still require unambiguous server-side gateway resolution. New `payment_success` URLs carry a purpose-bound HMAC over Integration Request and gateway, and new Integration Requests carry an explicit marker requiring that token; only unmarked legacy requests accept already-issued unsigned URLs. Invalid tokens fail before request lookup and invalid/unknown returns share one permission response. Reconciliation still requires a server-side Gateway retrieval and an actual confirmed transaction whose provider `referenceId` belongs to the expected Integration Request; Gateway status and cross-reference transactions are insufficient.

## `guest-whitelisted-method` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest webhook endpoints that accept unauthenticated payment status changes.
- Why this override is safe: `callback` is intentionally public for Payrexx POST webhooks, but it verifies `X-Webhook-Signature` with the per-webhook signing key before touching any Integration Request, and rejects webhooks whose verifying settings row does not match the gateway recorded on the Integration Request (`payrexx_settings` / `payment_gateway`). Unknown, unsigned, or cross-gateway payloads are rejected or ignored without applying payment side effects.

## `frappe-setuser` in `payrexx_integration/session_utils.py`

- Rule: `frappe-setuser`
- What it prevents: Unsafe privilege switching that can leave requests running under the wrong user.
- Why this override is safe: `as_automation_user` is the single privilege-switch context manager for payment side effects. It requires the owning Payrexx Settings row's configured enabled System User, has no Administrator or cross-app fallback, and restores the original Frappe user, session id, and session data in `finally`.
