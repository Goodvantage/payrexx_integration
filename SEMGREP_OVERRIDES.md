# SEMGREP_OVERRIDES

## `guest-whitelisted-method` in `payrexx_integration/api.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest-readable endpoints that expose or mutate private payment data.
- Why this override is safe: `pay_invoice` and `payment_success` are intentionally public GET redirect endpoints because invoice emails and Payrexx checkout returns link to them. `pay_invoice` rejects every request without a deterministic HMAC token generated from the site encryption key and the Sales Invoice name, and it only creates or reuses a Payment Request for that verified invoice. `payment_success` only completes an Integration Request after fetching the linked Gateway from Payrexx server-side and seeing a confirmed status, otherwise it redirects to the failed-payment page.

## `guest-whitelisted-method` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `guest-whitelisted-method`
- What it prevents: Accidental guest webhook endpoints that accept unauthenticated payment status changes.
- Why this override is safe: `callback` is intentionally public for Payrexx POST webhooks, but it verifies `X-Webhook-Signature` with the per-webhook signing key before touching any Integration Request. Unknown or unsigned payloads are rejected or ignored without applying payment side effects.

## `frappe-setuser` in `payrexx_integration/api.py`

- Rule: `frappe-setuser`
- What it prevents: Unsafe privilege switching that can leave requests running under the wrong user.
- Why this override is safe: `pay_invoice` is a signed public email link. The automation-user block is limited to creating/reusing the `Payment Request` and Payrexx checkout URL for the HMAC-verified Sales Invoice, and `_as_automation_user` restores the original Frappe session in `finally`.

## `frappe-setuser` in `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.py`

- Rule: `frappe-setuser`
- What it prevents: Unsafe privilege switching that can leave requests running under the wrong user.
- Why this override is safe: `callback` verifies the Payrexx webhook signature before any privilege switch. The automation-user block is limited to running the reference document's post-payment `on_payment_authorized` hook, uses the configured `Non Profit Settings.creation_user` when available, and restores the original Frappe session in `finally`.

## `frappe-manual-commit` in `payrexx_integration/dev_e2e.py`

- Rule: `frappe-manual-commit`
- What it prevents: Manual commits inside request handlers or DocType hooks that can leave partial writes and bypass Frappe's transaction lifecycle.
- Why this override is safe: `dev_e2e.run_event_to_invoice_email` is a standalone smoke helper for `bench execute` or console use. The commit persists the generated booking/invoice/email test fixture so the follow-up browser tests can inspect it outside the helper's process.
