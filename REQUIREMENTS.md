# REQUIREMENTS.md — Payrexx Integration

Requirements for `payrexx_integration`. This file is the requirement-level source of
truth: requirements, `DOCUMENTATION.md`, `HOW_TO.md`, and the code must
match. Update this file whenever requirements change; keep requirement IDs
stable (never reuse a retired ID — mark it "Retired:" with the reason).

Status: retrofitted on 2026-07-17 from current code, existing docs, and
archived agent sessions (opencode/Claude/Codex). Describes what the app is
required to do today, not a historical design spec.

## 1. Purpose and Scope

`payrexx_integration` adds **Payrexx hosted checkout** as a payment gateway on
top of the upstream `frappe/payments` app (`required_apps = ["payments"]`),
following the same plug-in pattern as the gateways shipped inside `payments`
(Stripe, Paymob, …) but kept in its own repo so upstream `payments` is never
patched. It provides:

- the `Payrexx Settings` DocType (per-environment credentials) and automatic
  `Payment Gateway` registration,
- a thin Payrexx REST client for hosted Gateway creation and retrieval,
- HMAC-signed pay-by-email URLs for ERPNext Sales Invoices with a guest
  redirect endpoint that lazy-creates the checkout,
- the signed Payrexx webhook callback that settles confirmed payments, and a
  server-side success-redirect reconciliation fallback,
- an explicitly enabled, read-only hosted sandbox acceptance surface that
  verifies one site-configured invoice's provider-to-ledger settlement chain.

The app explicitly does **NOT**:

- modify upstream apps (`payments`, `frappe`, `erpnext`) — it plugs in via the
  `payments` registry pattern only;
- implement capture, later-charge, void/cancel, or refund operations — those
  provider actions and their accounting reversals are manual procedures
  (REQ-PRX-BND-01);
- seed the ERPNext `Payment Gateway Account` bridge or any cross-app
  event/booking test data;
- depend on `good_connector` or any other Goodvantage app — downstream apps
  (e.g. `good_event`, `good_demo`) consume its API without a reverse
  dependency.

## 2. Functional Requirements

### 2.1 Payrexx Settings and Gateway Registration

- REQ-PRX-SET-01: Provide a `Payrexx Settings` DocType (autonamed by `field:gateway_name`, not a Single) holding per-environment credentials and behavior: `gateway_name`, `instance_name`, `api_base_domain`, `api_version`, `api_secret`, `webhook_signing_key` (both Password), `supported_currencies`, optional `psp` whitelist, `validity_minutes`, and success/failed/cancel redirect overrides; multiple rows (e.g. `Sandbox` + `Live`) must be supported. [Trace: `payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.json`; Tests: `payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings`]
- REQ-PRX-SET-02: On save, verify credentials live against Payrexx via the cheap `GET /Gateway/0/` ping and reject the save with a clear error on 401/403, other HTTP errors, unparseable responses, or unreachable hosts; the ping is skipped when `ignore_mandatory`, `frappe.flags.in_test`, or `frappe.flags.in_install` is set. [Trace: `payrexx_settings.py::PayrexxSettings.validate/_ping`; Tests: `test_settings_ping_uses_client`, `test_settings_ping_rejects_http_auth_error`]
- REQ-PRX-SET-03: On update, create/update the upstream `Payment Gateway` row named `Payrexx-<gateway_name>` through `payments.utils.create_payment_gateway` and call the `payment_gateway_enabled` hook; no fixture must be required for the registry row. [Trace: `payrexx_settings.py::PayrexxSettings.on_update`; Tests: `test_save_creates_payment_gateway_row`]
- REQ-PRX-SET-04: Reject transactions whose currency is not in the row's comma-separated `supported_currencies` list via the standard `validate_transaction_currency` controller hook. [Trace: `payrexx_settings.py::PayrexxSettings.validate_transaction_currency`; Tests: `test_validate_transaction_currency_accepts_supported`, `test_validate_transaction_currency_rejects_unsupported`]
- REQ-PRX-SET-05: Show the gateway-specific webhook callback URL on the Desk form as soon as `gateway_name` is filled (including unsaved rows), computed server-side from the configured public `host_name` with a browser-origin fallback, replacing the existing hint instead of appending duplicates. [Trace: `payrexx_settings.js`, `payrexx_settings.py::get_webhook_url`; Tests: `playwright/tests/payrexx_settings.spec.ts`]

### 2.2 Gateway Selection

- REQ-PRX-SEL-01: Resolve the Payrexx Settings row through one canonical strict resolver with the precedence explicit `gateway_name` → caller-owned `site_config_key` → the single configured row; zero or multiple rows must throw clear errors and rows must never be preferred by name (e.g. `Live`) or creation order. [Trace: `gateway_selection.py::resolve_payrexx_settings`; Tests: `test_gateway_resolver_*` (6 tests)]
- REQ-PRX-SEL-02: Allow downstream apps to pass their own site-config key (`resolve_payrexx_settings(site_config_key=...)`) without this app importing or interpreting downstream configuration. [Trace: `gateway_selection.py`; Tests: `test_gateway_resolver_uses_caller_site_config`, `test_gateway_resolver_explicit_choice_precedes_caller_site_config`]

### 2.3 Pay-by-Email Links

- REQ-PRX-PAY-01: Provide `payrexx_pay_url(sales_invoice, gateway_name=None)` as a Jinja method returning a signed absolute pay URL for submitted Sales Invoices; return an empty string for blank, missing, draft, or cancelled invoices, and log then return an empty string for unresolvable gateway configuration, so email templates degrade gracefully. [Trace: `api.py::payrexx_pay_url`, `hooks.py` `jinja.methods`; Tests: `test_pay_url_token_round_trip`, `test_pay_url_explicit_gateway_name`, `test_pay_url_blank_invoice_returns_blank`, `test_pay_url_missing_invoice_returns_blank_without_resolving_gateway`, `test_pay_url_uses_configured_public_host_without_dev_port`]
- REQ-PRX-PAY-02: Sign pay-link tokens as the first 32 hex chars of HMAC-SHA256 over the site's `encryption_key` keyed on `si_name|gateway_name` (deterministic per invoice+gateway, constant-time verified); legacy tokens signed on `si_name` alone must keep verifying. [Trace: `api.py::sign_reference/verify_reference/_sign/_verify`; Tests: `test_pay_url_token_round_trip`, legacy-link tests]
- REQ-PRX-PAY-03: The guest `pay_invoice` GET endpoint must reject invalid or missing tokens with a permission error, unknown invoices with a not-found error, and cancelled/draft invoices with a clear error, and must resolve the gateway strictly before any redirect shortcut; all parameters stay optional kwargs so missing-param requests return clean 403s, not 500s. [Trace: `api.py::pay_invoice`; Tests: `test_pay_invoice_rejects_bad_token`, `test_pay_invoice_rejects_missing_invoice`, `test_legacy_gateway_unbound_link_rejects_ambiguity_before_paid_redirect`, `playwright/tests/pay_invoice_redirect.spec.ts`]
- REQ-PRX-PAY-04: On a valid click, redirect already-paid invoices to the payment-success page; otherwise reuse an existing pending Payment Request for the invoice+gateway or lazily create and submit one via ERPNext `make_payment_request` (muted email), serialized under a Sales Invoice row lock so concurrent first clicks create exactly one Payment Request and one provider checkout. Because an email link is an HTTP GET, a successful checkout setup must request Frappe's end-of-request commit only after both local records and the checkout URL are valid; failures must keep the default rollback behavior. [Trace: `api.py::pay_invoice/_get_or_create_payment_request`; Tests: `test_first_pay_invoice_click_creates_exactly_one_provider_checkout_and_request`]
- REQ-PRX-PAY-05: Redirect to the checkout URL stored on the Payment Request during submission; recover it from the active Integration Request when missing, and fail closed with a clean error (never creating a duplicate potentially chargeable checkout) when an active request has no recoverable URL; only legacy/manual Payment Requests without any checkout may generate one on demand under the row lock. [Trace: `api.py::_get_payment_request_checkout_url`; Tests: `test_payment_request_checkout_reuses_url_created_on_submission`, `test_payment_request_without_url_recovers_stored_checkout`, `test_payment_request_without_url_does_not_duplicate_unknown_active_checkout`]
- REQ-PRX-PAY-06: Require a `Payment Gateway Account` matching gateway + invoice company + invoice currency before creating a Payment Request (clear error otherwise). Never delete a pre-existing draft Payment Request: reuse a pending request for the resolved gateway, but preserve and fail closed when another draft exists because ERPNext would otherwise reuse it regardless of the requested gateway. Current-attempt failures must rely on the request transaction rollback rather than destructive cleanup. [Trace: `api.py::_gateway_account_filter`, `_get_or_create_payment_request`; Tests: `test_gateway_account_filter_is_company_and_currency_specific`, `test_pay_link_flow_preserves_conflicting_staff_draft_payment_request`]

### 2.4 Payrexx REST Client

- REQ-PRX-API-01: Communicate with the Payrexx v1.x REST API using the current `x-api-key: <api_secret>` header auth (no legacy `ApiSignature` body field), a mandatory `?instance=` query param, `application/x-www-form-urlencoded` POSTs, the `{status, data[]}` response envelope, and amounts in the smallest currency unit. [Trace: `payrexx/payrexx_client.py`, `payrexx_settings.py::_build_create_gateway_payload`; Tests: `test_payrexx_client_uses_default_api_domain`, `test_gateway_payload_uses_per_checkout_failure_return_url`]
- REQ-PRX-API-02: Support Payrexx Platform / partner accounts through `api_base_domain` (instance = first subdomain of the login domain), normalizing the stored value, and retry the identical request once against `api.payrexx.com` when a custom API domain answers 401/403/404. [Trace: `payrexx_client.py::_normalize_api_base_domain/_should_retry_default_domain`; Tests: `test_payrexx_client_uses_platform_api_domain`, `test_settings_client_passes_platform_api_domain`, `test_payrexx_client_falls_back_to_default_api_domain_on_custom_auth_reject`]
- REQ-PRX-API-03: `get_payment_url()` must create the `Integration Request` log first, post the Gateway with `referenceId` set to the Integration Request name, persist the Payrexx gateway id/hash, checkout URL, and the owning settings row name (`payrexx_settings`) into the request data, and raise a clean localized error on failure. [Trace: `payrexx_settings.py::PayrexxSettings.get_payment_url`; Tests: `test_get_payment_url_records_owning_settings_on_integration_request`]

### 2.5 Webhook Callback

- REQ-PRX-WHK-01: Expose a guest POST `callback` endpoint that verifies `X-Webhook-Signature` (HMAC-SHA256 of the raw body, base64 digest first with lowercase-hex fallback) against the resolved row's per-webhook signing key before any side effect and rejects invalid signatures with an authentication error. [Trace: `payrexx_settings.py::callback`, `payrexx/webhook_validator.py`; Tests: `test_webhook_signature_base64`, `test_webhook_signature_hex_fallback`, `test_webhook_signature_rejects_tampered`]
- REQ-PRX-WHK-02: Resolve `gateway_name` from the method kwargs, then the request query string, then the form dict, because Payrexx JSON webhooks keep the query string in `frappe.request.args`. [Trace: `payrexx_settings.py::_gateway_name_from_request`; Tests: `test_callback_reads_gateway_name_from_query_args_for_json_webhook`]
- REQ-PRX-WHK-03: Ignore (return `{"ok": True}` with a compact log entry) webhooks that miss `referenceId`, reference an unknown Integration Request, reference a non-Payrexx Integration Request, or were verified with a different settings row's key than the request's owning gateway (`payrexx_settings`, with the `payment_gateway` value as legacy fallback); logs must contain only a compact transaction summary, never full payer payloads. [Trace: `payrexx_settings.py::callback`, `_webhook_log_summary`; Tests: `test_callback_ignores_non_payrexx_integration_request`, `test_callback_rejects_gateway_mismatch`]
- REQ-PRX-WHK-04: Map Payrexx transaction statuses as follows: `confirmed` → Completed with settlement; `authorized`/`reserved` → Authorized (record only); `cancelled`/`declined`/`error`/`expired` → Failed with the status stored as error; `waiting` and unknown statuses (including `refunded`) → store the transaction with the Integration Request status unchanged. [Trace: `payrexx_settings.py::callback`; Tests: `test_callback_marks_integration_request_completed`]

### 2.6 Confirmation and Settlement

- REQ-PRX-SETL-01: Complete confirmed Integration Requests atomically: lock and reload the row, store the provider transaction, set Completed, settle the reference, and record the Payment Entry created by that settlement in one transaction, retrying the whole locked unit up to 3 attempts on `QueryDeadlockError`; duplicate confirmed callbacks and already-Completed rows must return without settling twice. [Trace: `payrexx_settings.py::_complete_integration_request/_complete_locked_integration_request`; Tests: `test_confirmation_retries_whole_locked_unit_after_deadlock`, `test_deadlock_retry_completes_request_and_creates_exactly_one_payment_entry`]
- REQ-PRX-SETL-02: For Integration Requests referencing an ERPNext Payment Request, settle by calling the standard `set_as_paid()` under a Payment Request row lock, producing exactly one submitted Payment Entry and letting ERPNext update request and invoice outstanding amounts; other reference types keep receiving their existing `on_payment_authorized("Completed")` hook. [Trace: `payrexx_settings.py::_on_payment_authorized/_set_payment_request_as_paid`; Tests: `test_deadlock_retry_completes_request_and_creates_exactly_one_payment_entry`, `test_non_payment_request_keeps_authorization_hook`]
- REQ-PRX-SETL-03: Run all payment side effects (webhook settlement and `pay_invoice` lazy creation) as the least-privilege automation user — the configured `Non Profit Settings.creation_user` when that DocType exists and names a valid user, else `Administrator` — via one context manager that always restores the original session. [Trace: `session_utils.py::as_automation_user/payment_authorization_user_name`; Tests: exercised through the settlement and first-click integration tests in `test_payrexx_settings`]
- REQ-PRX-SETL-04: Log and re-raise non-deadlock settlement failures so Payrexx retries the webhook, committing the Integration Request and downstream payment side effects together through Frappe's request transaction (no mid-callback manual commit). [Trace: `payrexx_settings.py::callback/_on_payment_authorized`; Tests: `test_confirmation_retries_whole_locked_unit_after_deadlock`]

### 2.7 Success-Redirect Reconciliation

- REQ-PRX-REC-01: The guest `payment_success` endpoint must reconcile the Integration Request by fetching the Gateway from Payrexx server-side and only completing it when Payrexx reports the Gateway or one of its transactions as `confirmed`, using the request's own stored gateway credentials (`payrexx_settings`, then the `payment_gateway` fallback); the caller-supplied `gateway_name` is honored only for legacy requests carrying neither value. Because this provider return is an HTTP GET, it must request Frappe's end-of-request commit after server verification reaches a Completed or Failed terminal state, while waiting/non-terminal results remain non-committing. [Trace: `payrexx_settings.py::reconcile_integration_request`, `api.py::payment_success`; Tests: `test_success_reconciliation_marks_integration_request_completed`, `test_reconcile_prefers_integration_requests_own_gateway`, `test_payment_success_redirects_directly_to_custom_return_url`, `test_payment_success_redirects_to_failed_page_when_not_confirmed`]
- REQ-PRX-REC-02: After reconciliation, redirect to the request's stored same-site `redirect_to` when present, else the standard `/payment-success` page; redirect to `/payment-failed` when Payrexx does not report a confirmed payment; settings-level and per-checkout (`failed_redirect_to`/`cancel_redirect_to`) redirect overrides must be supported, with caller-supplied return URLs validated same-site. [Trace: `api.py::_payment_success_redirect_url/_payment_failed_redirect_url`, `payrexx_settings.py::_return_url`; Tests: `test_payment_success_redirects_directly_to_custom_return_url`, `test_payment_success_redirects_to_failed_page_when_not_confirmed`]

### 2.8 Chargebacks

- REQ-PRX-CHG-01: A verified `chargeback` event must mark the Integration Request Failed via a direct transactional field update that preserves submitted ledger records (never cancels or deletes Payment Entries), and create one idempotent high-priority open ToDo assigned to the payment automation user and linked to the Integration Request for manual accounting reversal; repeated chargeback callbacks reuse the existing ToDo, and a later duplicate confirmation must not move a chargeback request back to Completed. [Trace: `payrexx_settings.py::_mark_chargeback`, `_complete_locked_integration_request` (chargeback guard), `CHARGEBACK_TODO_MARKER`; Tests: `test_payment_request_confirmation_and_chargeback_are_idempotent`]

### 2.9 Supported Operation Boundaries

- REQ-PRX-BND-01: Expose only hosted Gateway creation and Gateway/Transaction retrieval; capture of `reserved` transactions, later charging of `authorized` transactions, checkout cancellation/voids, and refund initiation or ERPNext refund reconciliation must not be implemented — provider-side actions and the corresponding accounting reversals stay documented manual procedures. [Trace: `payrexx_client.py` (client surface, `delete_gateway` stub), `DOCUMENTATION.md` "Supported Payment Operations", `HOW_TO.md` §9; Tests: none]

### 2.10 Hosted Sandbox Acceptance

- REQ-PRX-QA-01: Provide authenticated POST-only `preflight` and `inspect_settlement` methods for hosted sandbox acceptance. Both require System Manager plus Accounts Manager, developer mode, `payrexx_hosted_qa_enabled = 1`, a strict current-date run marker, and exact `payrexx_hosted_qa_gateway` / `payrexx_hosted_qa_invoice` site-config targets. They must never create a checkout, invoke reconciliation, replay a callback, or mutate accounting/provider state. [Trace: `hosted_qa.py`; Tests: `payrexx_integration.tests.test_hosted_qa`]
- REQ-PRX-QA-02: Preflight must require a submitted, fully unpaid, non-return invoice no larger than 500 currency units, comparing outstanding against ERPNext's rounded payable total when present, plus configured secrets, accepted currency, live API credential ping, HTTPS callback URL, and exactly one company/currency Gateway Account. It may resume exactly one submitted pending checkout with complete stored metadata, but must reject ambiguous or incomplete records and must never return a signed payment/provider URL. [Trace: `hosted_qa.py::preflight`; Tests: `test_preflight_*`, `test_invoice_validation_uses_erpnext_rounded_payable_total`]
- REQ-PRX-QA-03: Settlement inspection must bind the supplied Payment Request and Integration Request to the configured gateway/invoice and pass only when the provider transaction is confirmed in `TEST` mode with exact amount/currency/reference, the Integration Request is Completed and records the exact settlement-created Payment Entry, the Payment Request and Sales Invoice are Paid with zero outstanding, and exactly one submitted Payment Entry allocates its full account-currency paid amount to that invoice. The protected CLI must accept credentials only through environment variables, require an exact allowlisted HTTPS origin, bind state to the current run marker, and persist only redacted owner-readable state. [Trace: `hosted_qa.py::inspect_settlement`, `tests/hosted_settlement_qa.py`; Tests: `test_inspector_*`, `TestHostedSettlementRunner`]

### 2.11 Cross-App Surface

- REQ-PRX-INT-01: Keep `payrexx_integration.api.payrexx_pay_url` importable and failing soft (empty string on missing/ambiguous configuration) so downstream renderers such as Good Event's default invoice email degrade gracefully and still send without the online-pay button. [Trace: `api.py::payrexx_pay_url`, `AGENTS.md` "Cross-app integration"; Tests: `test_pay_url_blank_invoice_returns_blank`]
- REQ-PRX-INT-02: Ship German embedded-help content under `fixtures/help/payrexx_integration/` for the `good_help` Wiki sync (overview, settings, payment links and webhooks). [Trace: `payrexx_integration/fixtures/help/payrexx_integration/*.md`; Tests: none]

## 3. Non-Functional Requirements

### 3.1 Security and Permissions

- REQ-PRX-SEC-01: Every guest-reachable endpoint must carry full type hints and have its `nosemgrep: guest-whitelisted-method` override documented in `SEMGREP_OVERRIDES.md`. `pay_invoice` must verify its per-invoice HMAC token and `callback` must verify its webhook HMAC before side effects. `payment_success` is an unauthenticated provider-return endpoint; it must treat caller parameters only as identifiers and may complete payment only after server-to-server Payrexx retrieval confirms the stored Integration Request's Gateway or transaction. [Trace: `api.py`, `payrexx_settings.py`, `SEMGREP_OVERRIDES.md`; Tests: `test_pay_invoice_rejects_bad_token`, `test_webhook_signature_rejects_tampered`, `test_success_reconciliation_marks_integration_request_completed`]
- REQ-PRX-SEC-02: Keep the webhook signing key and the API secret as separate Password-field values; transmit the API secret only as the `x-api-key` header, and keep secrets and full payer/payment payloads out of logs (compact webhook summaries only). [Trace: `payrexx_settings.json`, `payrexx_client.py::_headers`, `payrexx_settings.py::_webhook_log_summary`; Tests: `test_callback_rejects_gateway_mismatch`]
- REQ-PRX-SEC-03: Validate every externally supplied return/redirect URL as same-site via `safe_return_url` before passing it to Payrexx or the browser. [Trace: `url_utils.py::safe_return_url`; Tests: `test_payment_success_redirects_directly_to_custom_return_url`]
- REQ-PRX-SEC-04: Restrict Desk access to `Payrexx Settings` to System Manager and Accounts Manager (full) and Accounts User (read-only). [Trace: `payrexx_settings.json` `permissions`; Tests: none]

### 3.2 Performance

- REQ-PRX-PERF-01: Keep webhook correlation O(1) by using the Integration Request name as the Payrexx `referenceId`, resolve settings rows via `frappe.get_cached_doc`, and reuse existing Payment Requests/checkouts on repeated link clicks instead of creating new provider objects. [Trace: `payrexx_settings.py::_build_create_gateway_payload`, `gateway_selection.py`, `api.py::_get_or_create_payment_request`; Tests: `test_payment_request_checkout_reuses_url_created_on_submission`]

### 3.3 Compatibility and Upgrade Safety

- REQ-PRX-COMPAT-01: Never patch upstream apps; integrate only through the `payments` registry pattern (`create_payment_gateway`, controller hooks `get_payment_url`/`validate_transaction_currency`) and framework utilities (`make_get_request`/`make_post_request`, `frappe.parse_json`), with `required_apps = ["payments"]` declared. [Trace: `hooks.py`, `payrexx_settings.py`, `payrexx_client.py`; Tests: none]
- REQ-PRX-COMPAT-02: Keep in-flight signed links verifying: the `sign_reference` payload compositions (`si_name`, `si_name|gateway_name`) are frozen, legacy gateway-unbound links stay valid while gateway resolution is unambiguous, and `sign_reference` remains importable as the shared signer (used by `good_demo`'s dummy checkout). [Trace: `api.py::sign_reference/_verify`; Tests: `test_legacy_gateway_unbound_link_is_valid_but_requires_unambiguous_resolution`, `test_legacy_gateway_unbound_link_accepts_single_gateway`]
- REQ-PRX-COMPAT-03: Keep the `Payrexx Settings` autoname (`field:gateway_name`) and the `Payrexx-<gateway_name>` Payment Gateway naming stable — existing Integration Requests and webhook URLs depend on both, and the legacy gateway fallback parses the `Payrexx-` prefix. [Trace: `payrexx_settings.json` `autoname`, `payrexx_settings.py::on_update/_settings_name_from_request_data`; Tests: `test_save_creates_payment_gateway_row`]

### 3.4 Operations

- REQ-PRX-OPS-01: Build every externally shared URL (pay links, Payrexx return URLs, the Desk webhook hint) from the configured `host_name` exactly as set, never appending the local bench `webserver_port` or leaking a tunnel origin; production deployments must set `host_name`. [Trace: `url_utils.py::get_public_url`, `HOW_TO.md` §6; Tests: `test_pay_url_uses_configured_public_host_without_dev_port`, `test_webhook_url_uses_configured_public_host_without_dev_port`]
- REQ-PRX-OPS-02: Leave creation of the ERPNext `Payment Gateway Account` (per company/currency using the gateway) to operators — the app must not seed it — and surface a clear error naming the missing combination when a pay link is clicked without it. [Trace: `api.py::_gateway_account_filter`, `HOW_TO.md` §2; Tests: `test_gateway_account_filter_is_company_and_currency_specific`]
- REQ-PRX-OPS-03: Keep all test surfaces runnable as documented: the Python integration module (`IntegrationTestCase`), the focused hosted-QA server/CLI tests, and the self-contained Playwright project (`playwright/`, npm) covering the Desk settings flow and endpoint auth, with the optional booking-email spec driven by `TEST_BOOKING_NAME` against an existing Good Event Booking (this app must not seed cross-app event fixtures). [Trace: `test_payrexx_settings.py`, `tests/test_hosted_qa.py`, `tests/hosted_settlement_qa.py`, `playwright/`, `AGENTS.md` "Testing"; Tests: the suites themselves]

## 4. Explicit Decisions and Constraints

Documented intentional behaviors a reader might otherwise mistake for bugs:

- **Guest endpoints are intentional.** `pay_invoice`, `payment_success`, and `callback` are public by design (email links and Payrexx returns have no session). `pay_invoice` and `callback` authenticate requests cryptographically. `payment_success` does not authenticate the browser return; it treats its arguments as identifiers and trusts only a server-to-server Payrexx confirmation before settlement. Documented in `SEMGREP_OVERRIDES.md`.
- **Chargebacks never touch the ledger.** A chargeback marks the Integration Request Failed and opens one review ToDo; submitted Payment Entries are deliberately preserved and reversal is a manual accounting procedure (`DOCUMENTATION.md` "Chargebacks", `HOW_TO.md` §9; audit finding H13).
- **No capture/void/refund operations.** `PayrexxClient.delete_gateway` is an intentional `NotImplementedError` stub and `refunded` webhooks leave the request status unchanged; provider-side actions stay manual (`DOCUMENTATION.md` "Supported Payment Operations").
- **Legacy gateway-unbound links.** Pre-gateway-binding tokens sign only the invoice name; they keep working with exactly one settings row and are intentionally rejected as ambiguous with several — resend the email to issue a gateway-bound link (`api.py::_verify`, `DOCUMENTATION.md` "URL Contracts").
- **Deterministic tokens.** Resending an invoice email with the same gateway reproduces the identical URL; rotating the site's `encryption_key` is the (rare) global invalidation mechanism (`AGENTS.md` "URL patterns").
- **Fail-closed checkout recovery.** An active Integration Request without a recoverable checkout URL raises an error instead of creating a second potentially chargeable checkout (audit finding H12; `api.py::_get_payment_request_checkout_url`).
- **Whole-unit deadlock retry.** Confirmation retries reload and re-settle the entire locked unit so a rollback cannot split Integration Request state from downstream settlement (audit findings C3/H14; `payrexx_settings.py::_complete_integration_request`).
- **Local automation-user helper.** `session_utils.as_automation_user` deliberately duplicates `good_connector`'s helper because this app must not depend on `good_connector`; there is exactly one implementation here (`session_utils.py` docstring).
- **Webhook `gateway_name` from the query string.** The callback intentionally reads request args/form dict in addition to kwargs because Payrexx posts JSON bodies (`payrexx_settings.py::_gateway_name_from_request`).
- **Not a Single, single-per-environment by convention.** `Payrexx Settings` supports multiple rows; environment separation is by naming convention (`Sandbox`/`Live`), and webhooks must carry `?gateway_name=` once more than one row exists (`AGENTS.md` "Gotchas").
- **Draft Payment Requests are never deleted by the pay-link flow (N11).** Ownership is not reliable provenance because the automation principal can also be used interactively and can fall back to `Administrator`. A pending request for the resolved gateway is reused; any other draft is preserved and blocks checkout creation until accounts staff resolve it. Failed current attempts roll back transactionally (`api.py::_get_or_create_payment_request`; `CUSTOM_APPS_AUDIT_2026-07-17.md` N11).
- **No cross-app test seeding.** The Playwright booking-email spec consumes an existing Good Event Booking via `TEST_BOOKING_NAME`; this app must not create Buzz/Event records (`AGENTS.md` "Architecture", `HOW_TO.md` §10).
- **Versioning.** App version tracks the Frappe major (`16.1.2` in `payrexx_integration/__init__.py`, `pyproject.toml` dynamic version), per the bench custom-app versioning policy.

## 5. Sources

- Code: `hooks.py`, `api.py`, `gateway_selection.py`, `session_utils.py`, `url_utils.py`, `hosted_qa.py`, `tests/hosted_settlement_qa.py`, `tests/test_hosted_qa.py` (15 focused methods), `payrexx/payrexx_client.py`, `payrexx/webhook_validator.py`, `doctype/payrexx_settings/` (`.json`, `.py`, `.js`, `test_payrexx_settings.py` — 49 test methods), `fixtures/help/payrexx_integration/`, `playwright/tests/` (3 specs).
- Repo docs: `AGENTS.md`, `DOCUMENTATION.md`, `HOW_TO.md`, `PAYREXX_INTEGRATION.md`, `SEMGREP_OVERRIDES.md`, `README.md`.
- Bench docs/audits: `/workspace/development/AGENTS.md`; `CUSTOM_APPS_AUDIT_2026-07-17.md` (payrexx: C3, H12, H13, H14, B21 verified still fixed; N11 remediated 2026-07-18); `AUDIT_REMEDIATION_WORKLIST_2026-07-14.md` (wave-1 settlement/duplicate-checkout/deadlock/chargeback remediation, H13 chargeback decision, 35–46 passing tests recorded).
- Session stores:
  - opencode SQLite: 4 payrexx-titled sessions; 2 mined in depth ("Implement Payrexx wave one" — exact acceptance criteria for C3/H12/H13/H14; "Add Payrexx regressions" — first-click and deadlock-settlement evidence requirements).
  - Claude transcripts: 33 files mention payrexx; top 2 mined (the 2026-07-14 custom-app audit run and the 2026-07-17 re-audit/fix run that produced the findings above).
  - Codex rollouts: 28 files mention payrexx; sampled — mostly tangential (post-crash dirty-repo recovery sessions referencing the same audit work), no additional requirement signals.
- Git history (read-only): `82682d6` atomic settlement, `455ca43` gateway-selection centralization, `79a4846`/`54b58a2` test fixture isolation, `39cce4c` regression evidence, `12375c9` ops-doc alignment, `5f58688` version 16.0.0.
