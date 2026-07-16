# SEMGREP_OVERRIDES

## `guest-whitelisted-method` in `payrexx_integration/api.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest-readable endpoints that expose or mutate private payment data.
- Why this override is safe: `pay_invoice` and `payment_success` are intentionally public GET redirect endpoints because invoice emails and Payrexx checkout returns link to them. `pay_invoice` rejects every request without a deterministic HMAC token generated from the site encryption key, Sales Invoice name, and selected gateway, and it only creates or reuses a Payment Request for that verified invoice and gateway. Legacy invoice-only tokens still require unambiguous server-side gateway resolution. `payment_success` only completes an Integration Request after fetching the linked Gateway from Payrexx server-side and seeing a confirmed status, otherwise it redirects to the failed-payment page.

## `guest-whitelisted-method` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest webhook endpoints that accept unauthenticated payment status changes.
- Why this override is safe: `callback` is intentionally public for Payrexx POST webhooks, but it verifies `X-Webhook-Signature` with the per-webhook signing key before touching any Integration Request, and rejects webhooks whose verifying settings row does not match the gateway recorded on the Integration Request (`payrexx_settings` / `payment_gateway`). Unknown, unsigned, or cross-gateway payloads are rejected or ignored without applying payment side effects.

## `frappe-setuser` in `payrexx_integration/session_utils.py`

- Rule: `frappe-setuser`
- What it prevents: Unsafe privilege switching that can leave requests running under the wrong user.
- Why this override is safe: `as_automation_user` is the single privilege-switch context manager for both guest payment paths. `pay_invoice` only reaches it after HMAC verification of the signed email link; the webhook path only after `X-Webhook-Signature` verification. It resolves the configured least-privilege `Non Profit Settings.creation_user` (falling back to Administrator), and restores the original Frappe session in `finally`.
