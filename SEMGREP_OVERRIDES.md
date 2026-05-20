# SEMGREP_OVERRIDES

## `guest-whitelisted-method` in `payrexx_integration/api.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest-readable endpoints that expose or mutate private payment data.
- Why this override is safe: `pay_invoice` is intentionally public because invoice emails link to it. The endpoint rejects every request without a deterministic HMAC token generated from the site encryption key and the Sales Invoice name, and it only creates or reuses a Payment Request for that verified invoice. `payment_success` is intentionally public because Payrexx redirects customers to it after checkout; it only completes an Integration Request after fetching the linked Gateway from Payrexx server-side and seeing a confirmed status, otherwise it redirects to the failed-payment page.

## `guest-whitelisted-method` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest webhook endpoints that accept unauthenticated payment status changes.
- Why this override is safe: `callback` is intentionally public for Payrexx webhooks, but it verifies `X-Webhook-Signature` with the per-webhook signing key before touching any Integration Request. Unknown or unsigned payloads are rejected or ignored without applying payment side effects.

## `frappe-setuser` in `payrexx_integration/api.py`

- Rule: `frappe-setuser`
- What it prevents: Unsafe privilege switching that can leave requests running under the wrong user.
- Why this override is safe: `pay_invoice` is a signed public email link. The automation-user block is limited to creating/reusing the `Payment Request` and Payrexx checkout URL for the HMAC-verified Sales Invoice, and `_as_automation_user` restores the original Frappe session in `finally`.
