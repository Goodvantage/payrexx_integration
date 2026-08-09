# AGENTS.md — payrexx_integration

Guidance for coding agents working in `payrexx_integration`. Pairs with the
bench-root `AGENTS.md` (general Frappe rules) and the upstream
`apps/payments` (which this app extends without forking).

---

## What payrexx_integration is

A standalone Frappe app that adds **Payrexx** as a payment gateway on top of
the upstream `frappe/payments` app. Same plug-in pattern as the existing
gateways inside `payments` (Stripe, Paymob, PayPal, Razorpay, …) but kept
in its own repo so we don't fork upstream `payments`.

`required_apps = ["payments"]`. Module name `Payrexx Integration`.
Innermost package dir: `payrexx_integration/payrexx_integration/payrexx_integration/`
(the triple `payrexx_integration` is the standard Frappe scrub layout).

> **Why a separate app, not a patch into `apps/payments`?** Upstream
> `payments` is on `frappe/payments`. Patching it directly turns every
> upstream pull into a merge conflict. The `payrexx_integration` app
> contributes the same shape (a `<X> Settings` doctype + an `on_update`
> that calls `payments.utils.create_payment_gateway`) without touching
> upstream code.

## Documentation Requirements

- Keep root-level `HOW_TO.md` and `DOCUMENTATION.md` present and current.
- Update `HOW_TO.md` when operator/admin procedures change.
- Update `DOCUMENTATION.md` when doctypes, hooks, APIs, webhook contracts,
  security assumptions, setup/migration behavior, or test commands change.
- Keep `README.md` short; use these docs for runbooks and technical reference.

## Documentation Contract

This repo keeps four synchronized artifacts: `REQUIREMENTS.md` (what the app
must do), `DOCUMENTATION.md` (how it works), `HOW_TO.md` (operator
procedures), and the code. Record new or changed requirements in
`REQUIREMENTS.md` and keep all four in sync with every change.

---

## Architecture

| File | Purpose |
|---|---|
| `gateway_selection.py` | Canonical strict resolver shared by native flows and downstream apps. Supports explicit selection, a caller-owned site-config key, and unambiguous single-row fallback. |
| `payrexx_integration/payrexx_integration/doctype/payrexx_settings/` | The settings DocType. One row per environment (`Sandbox` / `Live`), each with its own required automation user. `on_update` auto-creates the matching `Payment Gateway` row (`Payrexx-<gateway_name>`). |
| `payrexx_integration/payrexx_integration/doctype/payrexx_subscription_event/` | Durable, non-PII recurring-transaction state for installments and later-installment reversals. Its deterministic gateway/event key advances monotonically; claimed same-status replays stop, while Unclaimed states remain retryable from a sanitized financial payload. A raised provider rolls back fully before that Unclaimed row is retained, and the daily worker redrives it before transaction/status reconciliation. |
| `payrexx_integration/payrexx_integration/payrexx/payrexx_client.py` | Thin REST client for Gateway, Subscription, and static-QR operations. **Auth: `x-api-key: <api_secret>` header** — current Payrexx scheme (per the official PHP SDK). The API version is code-pinned at `v1.16`; it is not a settings field. The legacy `ApiSignature` body field is no longer used. Canonical `*.payrexx.com` API hosts are trusted by default; custom Platform hosts require exact `payrexx_allowed_api_hosts` site-config entries and are validated before secret access. Canonical POSTs have no rate-limit retry, but custom-host Gateway creation can repeat once on `api.payrexx.com` after 401/403/404. The secret lives only in the closure of the `requests` auth callable — see "Never let the API secret become a variable" below. |
| `payrexx_integration/payrexx_integration/payrexx/webhook_validator.py` | HMAC-SHA256 verification of `X-Webhook-Signature`. Verifies Payrexx's documented hex encoding first and retains base64 as an observed compatibility form. The signing key is **separate** from the API secret (configured per webhook in the Payrexx dashboard). |
| `api.py::payrexx_pay_url(sales_invoice, gateway_name=None)` | Jinja helper (registered via `hooks.py.jinja`). Resolves the gateway and returns an HMAC-signed redirect URL keyed off the site's `encryption_key`. |
| `api.py::pay_invoice(si=None, token=None, gateway_name=None)` | Whitelisted GET redirect endpoint. Verifies the invoice-and-gateway-bound HMAC token, locks/revalidates the wholly unpaid invoice and exact submitted/Requested Payment Request plus Integration Request checkout metadata, lazy-creates through ERPNext only when safe, and 302s to Payrexx. Because Frappe otherwise rolls back GET transactions, it sets `frappe.local.flags.commit` only after atomic local setup and checkout URL resolution. **All args remain optional kwargs** so missing-param requests return clean 403, not 500. |
| `hosted_qa.py` | Explicitly gated, System-Manager-and-Accounts-Manager-only, read-only hosted sandbox preflight and settlement evidence. Exact invoice/gateway targets come from site config; never add checkout creation, callback replay, or reconciliation here. |
| `tests/hosted_settlement_qa.py` | Protected hosted CLI. Credentials and target allowlists are environment-only; persisted state contains no signed/provider URLs or transaction identifiers. |
| `playwright/` | Self-contained Playwright project (npm). Covers the Payrexx Settings desk flow, `pay_invoice` endpoint auth, and an optional existing Good Event Booking → invoice email flow. Test data remains owned by Good Event; this app must not seed Buzz/Event records. |

---

## URL patterns (operationally important)

### Pay-by-email URL (embedded in invoice emails)

```
{{ host_name }}/api/method/payrexx_integration.api.pay_invoice
  ?si=<Sales Invoice name>
  &gateway_name=<Payrexx Settings name>
  &token=<32 hex chars — first half of HMAC-SHA256(encryption_key, si_name|gateway_name)>
```

The token is deterministic per SI and gateway, so resends with the same gateway
produce the same URL. Legacy links omit `gateway_name` and sign only `si_name`;
they remain valid only while gateway resolution is unambiguous. To invalidate
all links, rotate the site's `encryption_key` (rare).

### Webhook URL (configured in Payrexx dashboard)

```
{{ host_name }}/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback
  ?gateway_name=<Payrexx Settings name>
```

The `gateway_name` query param is required when more than one Payrexx
Settings row exists. The desk form's dashboard surfaces this URL so admins
can paste it into the Payrexx webhook settings.
Payrexx JSON webhook requests keep the query string in `frappe.request.args`,
not in the whitelisted method kwargs, so the callback intentionally reads both.
Transaction deliveries use a `transaction` envelope. Subscription lifecycle
deliveries accept both Payrexx's documented bare object and the older observed /
SDK-derived `subscription` envelope. This compatibility is not proof of the
owned account's delivery shape; signed sandbox captures remain mandatory before
production subscription processing.
After signature verification, settlement and accounting-review side effects run
as the owning Payrexx Settings row's configured enabled System User. Missing,
disabled, or Website users fail closed; there is no Administrator or cross-app
settings fallback. Transient `QueryDeadlockError` failures around
`on_payment_authorized` are retried.
For unbound legacy Integration Requests on multi-gateway sites, refuse only
`confirmed` settlement as ambiguous; record authenticated non-confirmed evidence
under the settings row that verified the webhook.

### Success redirect URL (generated per Gateway)

```
{{ host_name }}/api/method/payrexx_integration.api.payment_success
  ?ir=<Integration Request name>
  &gateway_name=<Payrexx Settings name>
  &token=<HMAC bound to ir|gateway_name|payment_success>
```

New Integration Requests carry `payrexx_success_token_version` in their existing
metadata and require this token; only unmarked, already-issued legacy requests
may return unsigned. Key absence alone means legacy; every present marker must
equal the exact supported integer version. Authentication failures are checked before request lookup
where possible and do not reveal whether a reference exists. This endpoint is a
fallback reconciliation path. It retrieves the Payrexx
Gateway server-side and only completes the Integration Request when the Gateway
contains an actual confirmed transaction whose provider `referenceId` belongs
to the expected Integration Request; Gateway status alone and cross-reference
transactions are insufficient.
It then redirects directly to the
Integration Request's same-site `redirect_to` when present, otherwise to the
standard `/payment-success` page. Because the provider return is a GET, it sets
the end-of-request commit flag only after server verification reaches a
Completed or Failed terminal state; waiting results remain non-committing.

### Host URL — IMPORTANT for production

Payrexx-facing URLs use `payrexx_integration.url_utils.get_public_url()`,
which takes the site's `host_name` exactly as configured and avoids appending
the local bench `webserver_port` behind reverse proxies or ngrok. **No URL is
hard-coded.** In production, set `host_name` on the site (`bench --site <site>
set-config host_name "https://kursverwaltung.example.ch"`) and the embedded
URLs resolve correctly.

Caller-supplied absolute return URLs may also use an operator-configured
`*_public_base_url` origin. `safe_return_url()` compares normalized complete
HTTP(S) origins (scheme, canonical hostname, and effective port); userinfo,
malformed origins, scheme-relative forms, and HTTPS-to-HTTP downgrades fail
closed.

---

## Payrexx API quick reference

| | |
|---|---|
| Base URL | `https://api.<api_base_domain>/v1.16/`; default `https://api.payrexx.com/v1.16/`; custom final hosts require exact `payrexx_allowed_api_hosts` entries |
| Auth | `x-api-key: <api_secret>` header |
| Required query param | `?instance=<instance_name>` on every call; all GET filters and pagination are query parameters (never a GET JSON body) |
| POST body format | `application/x-www-form-urlencoded` |
| Response envelope | `{"status":"success", "data":[ … ]}` |
| Amount unit | Canonical integer hundredths for supported two-decimal currencies (CHF 2.00 → `200`); other fraction units and sub-cent amounts are rejected |
| Webhook signature header | `X-Webhook-Signature` — HMAC-SHA256 of raw body, signed with the per-webhook signing key (NOT the API secret) |
| Create idempotency | No idempotency-key field; `referenceId` is documented as an internal merchant reference, not as unique or de-duplicating |

A "no gateway found" GET against `/Gateway/0/` is the cheap auth-check used
by `_ping()` — HTTP 200 with `status: error` means creds are valid.

For Payrexx Platform / partner accounts, split the checkout/login domain:
`customer.pay.goodvantage.ch` means `instance_name = "customer"` and
`api_base_domain = "pay.goodvantage.ch"`. Do not put the full login domain in
`instance_name`. Add the exact final host (`api.pay.goodvantage.ch`) to the
site-config JSON list `payrexx_allowed_api_hosts` before saving. URL-like values,
IP literals, malformed hosts, wildcards, and non-HTTPS ports are rejected before
the Password field is read. A custom API domain's 401/403 response repeats the
same request once against `api.payrexx.com` for every supported operation. A 404
repeats only the credential probe and Gateway creation, where it can indicate an
unprovisioned custom API host; every other retrieval or POST 404 is authoritative.
Thus custom-host Gateway creation can issue two POSTs for one app call. This is a
host fallback, not a rate-limit or checkout-deadlock retry.

The official PHP SDK v2.0.15 communicator invokes its configured adapter once
per API call and has no built-in custom-host fallback or idempotency layer; an
injected adapter may add retries or multiple wire requests. Tagged code defaults
to API `v1.15`, but that tag's stale README says API `v1.11` and calls SDK v2.0.0
current stable. This app pins `v1.16`. The provider Gateway OpenAPI still
officially accepts `application/x-www-form-urlencoded`, so do not replace the
app's encoding merely because the SDK's bundled cURL/Guzzle adapters send JSON
for ordinary non-file POSTs.

Checkout creation must use the app-owned `_create_integration_request()` path,
never core `create_request_log()` (it commits). Provider metadata and the
Payment Request persist atomically with the caller transaction. Provider success
immediately journals `[Payrexx Gateway recovery] state=local_commit_pending`;
commit adds `local_commit_confirmed` and ordinary rollback adds
`[Payrexx possible orphan Gateway] state=local_rollback_confirmed`. Frappe clears
rollback callbacks before SQL commit, so an unpaired pending record is the exact
commit-failure residual. Payrexx documents `DELETE /Gateway/{id}/`, but this app
exposes no wrapper. Operators with explicit provider DELETE permission search by
exact host, `referenceId`, and every discovered Gateway id. If none exists, no
deletion is needed; each exact Gateway is deleted conditionally only after its
retrieval and transaction search prove every `invoices[].transactions[]`
collection empty. Paid or ambiguous Gateways remain for reconciliation. Never
add an internal commit or blind provider teardown to close this gap.

### Status mapping (webhook)

| Payrexx `transaction.status` | `Integration Request.status` | Side effect |
|---|---|---|
| `confirmed` | `Completed` or terminal `Failed` conflict | Settles an active submitted inward Payment Request backed by a submitted Sales Invoice, or an explicitly registered extension source, with exact canonical amount/currency evidence |
| `authorized` | `Authorized` | Records provider state only; this app does not implement later charging |
| `reserved` | `Authorized` | Records provider state only; this app does not implement capture |
| `waiting` | unchanged | In-progress; wait for next webhook |
| `cancelled` / `declined` / `error` / `expired` | `Failed` | Records error string; no provider-side cancellation is initiated |
| `chargeback` | `Failed` | Preserves submitted ledger rows and creates one accounting-review ToDo |
| `refund_pending` | unchanged | Records reversal evidence; no accounting ToDo until the refund becomes final |
| `refunded` / `partially-refunded` | unchanged | Records reversal evidence and creates one accounting-review ToDo; ledger reversal remains manual |
| `disputed` | unchanged | Records reversal evidence and creates one accounting-review ToDo |
| another unknown status | unchanged | Stores the transaction only |

These mappings apply to requests that have not completed. Once Completed,
delayed or replayed statuses are ignored except post-settlement reversal evidence
(`chargeback`, `disputed`, and the three refund statuses), and none may replace
the confirmed transaction evidence. A verified `chargeback` moves the request to
Failed for accounting review; refunds and disputes preserve Completed.
After chargeback evidence exists, all non-chargeback statuses, including
`confirmed`, are terminally ignored; preserve Failed status, the chargeback
error, and the first chargeback transaction. Only duplicate chargeback delivery
may re-enter the idempotent review-ToDo path.

The integration creates hosted Gateways and reads Gateway state (`create_gateway`,
`retrieve_gateway`, `ping_gateway`), exposes Subscription CRUD/reporting, and
manages non-payment static QR codes
(`create_qr_code`, `delete_qr_code` → `PayrexxSettings.create_static_qr` /
`delete_static_qr`; see DOCUMENTATION.md "Static QR Codes"). The provider
supports Gateway DELETE, but the app exposes no wrapper for it, capture,
later-charge, void/cancel, or refund operations. Those provider actions and
their ERPNext accounting reversals are manual operational workflows until an
explicit, tested contract is implemented.

Static-QR targets are origin-bound, not merely scheme-checked: `create_static_qr`
requires the URL's normalized origin to be one the operator published here
(`host_name` or a `*_public_base_url` key), reusing
`url_utils.is_allowed_public_origin` — the same allowlist as `safe_return_url`.
Upstream apps resolve their own public base (good_npo: `good_npo_public_base_url`
→ `good_demo_public_base_url` → `host_name`), so a URL they build passes only
while that base is configured on the site; a permanent printed code can never
point at a stale origin. QR deletion declares its tolerated 404 to the client
(`expected_statuses`), so an already-deleted code produces no Error Log row while
every undeclared status still logs.

Static-QR TWINT handoff: a TWINT-app scan appends `qr_code_session_id` and a
return-app value to the scanned URL. Pass them into `get_payment_url` as
`qr_code_session_id` / `return_app` — they are guest-controlled, sanitized, and
dropped silently when invalid — and the method returns the Gateway `appLink`
(deep link into TWINT) instead of the hosted `link` for that checkout.

---

## Testing

```bash
# Focused Python tests
bench --site <site> run-tests \
  --module payrexx_integration.tests.test_settlement_validation

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_checkout_security

bench --site <site> run-tests \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_hosted_qa

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_url_utils

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_static_qr

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_rate_limit

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_refunds

bench --site <site> run-tests \
  --module payrexx_integration.tests.test_subscriptions

# Playwright e2e (core specs plus an optional existing-booking email check)
cd playwright
npm install && npx playwright install chromium
TEST_BOOKING_NAME=<booking> npx playwright test
```

`test_rate_limit` directly proves GET backoff and the canonical-host POST 405
case. PUT and DELETE use the shared helper in runtime code but are not exercised
there; the module is not a universal one-request POST test.
`test_checkout_security` separately mocks the custom-host fallback, including
the second canonical Gateway POST after custom-host 404. Neither is live
provider evidence.

`TEST_BOOKING_NAME` must identify an existing eligible Good Event Booking
created through Good Event's own fixtures or operator workflow. Payrexx
Integration does not create cross-app event test data.
CI seeds only a dummy non-live `Sandbox` Payrexx Settings/Payment Gateway pair,
starts and waits for the site, and runs `payrexx_settings.spec.ts` plus
`pay_invoice_redirect.spec.ts`; it does not install Good Event for the optional
booking spec.

Hosted sandbox settlement uses the separately documented protected CLI. Keep
the provider page human-operated, require provider `TEST` evidence, and disable
`payrexx_hosted_qa_enabled` after the run.
Custom-host fallback verification is separate and deferred until a dedicated
controllable sandbox API target and confirmed Gateway DELETE permission exist.
It must use a direct one-shot client harness that does not save the source
Settings row and has no Payment Request/Integration Request flow, validate
settings and prove one separate transaction-free cleanup canary can be deleted
and confirmed absent before arming one fault, account for exact local
and zero/one/multiple external state, conditionally delete only proven-empty
Gateways, and restore settings/allowlists unconditionally in an outer `finally`.
Follow `HOW_TO.md` §12 and never test it on a shared or production host.

---

## Gotchas

- **Never let the API secret become a variable.** Frappe logs the frame
  variables of every failing outbound request (`frappe.log_error` →
  `frappe.get_traceback(with_context=True)`, plus Sentry when telemetry is on),
  and its sanitizer redacts only the exact dict keys
  `password/passwd/secret/token/key/pwd` — `x-api-key` is not matched, and the
  dump expands plain objects, so an attribute leaks exactly like a local. The
  client therefore does **not** call
  `frappe.integrations.utils.make_get_request`/`make_post_request` (they take
  the header dict and the form body as ordinary arguments). It keeps the secret
  in the closure of `_api_key_auth()` and sends a session-prepared request in
  `_execute_request()`, which also drops its reference to the POST payer payload
  before the network call. Response parsing and the error-reporting contract are
  copied from `make_request`; keep them in sync if upstream changes. Regression
  coverage: `tests/test_checkout_security.py::TestApiSecretNeverReachesLoggedTracebacks`
  (audit finding V-H1, 2026-07-30).
- **Settings save calls Payrexx live.** `validate()._ping()` hits
  `GET /Gateway/0/` to verify credentials. With bogus creds the save
  fails with "Payrexx rejected the API Secret". The ping is skipped when
  `frappe.flags.in_test` or `frappe.flags.in_install` is set.
- **Automation user is gateway-owned and mandatory.** Every Payrexx Settings
  row must name an enabled System User. Checkout, settlement, subscription
  lifecycle/installment/reconciliation, chargeback, and settlement-conflict ToDo
  paths resolve that row from explicit checkout state
  or Integration Request metadata and fail closed instead of guessing another
  app's setting or Administrator. Keep the full settings-controller checkout
  operation inside this context so direct downstream callers cannot bypass it;
  nested `pay_invoice` entry is intentionally reentrant.
- **New success returns are signed.** Keep
  `payrexx_success_token_version` on new Integration Requests and include the
  purpose-bound token in generated `payment_success` URLs. Do not broaden the
  unsigned compatibility branch beyond requests where the marker key is absent,
  and fail closed on every unsupported present value.
- **The DocType uses `autoname: field:gateway_name`** — the doc name = the
  `gateway_name` field. Don't add an `autoname` patch that breaks this.
- **Pay URL contains `&amp;` after rendering.** Tests asserting on the
  decoded body need to handle the HTML-entity encoding (the Playwright
  helper `decodeMime` does this).
- **Settings row is single per environment** but the doctype is NOT a
  Single — multiple rows are supported (e.g. `Sandbox` + `Live`). Webhooks
  must include `?gateway_name=...` once you have more than one row.
- **Gateway fallback is strict.** `resolve_payrexx_settings()` never prefers a
  row by name or creation order. Gateway-unbound legacy payment links work only
  when one settings row exists; resend them after selecting a gateway when the
  site has multiple rows.
- **Checkout URL reuse is strict.** A Payment Request URL is reused only when
  current locking reads prove submitted/Requested/fully outstanding state and
  exact invoice plus Integration Request amount/currency/source/provider
  metadata. Partial or changed receivables stop before provider contact.
- **One active Payrexx request per invoice.** Before every Gateway POST, lock
  the Sales Invoice and current submitted active `Payrexx-*` Payment Requests.
  Any other active request blocks provider contact; preserve draft and
  terminal/cancelled history.
- **Lock and hydrate in one read.** For mutable callback/settlement state, use
  `frappe.get_doc(..., for_update=True)` (or the app helper wrapping it). Never
  issue a scalar `for_update` query and then call ordinary `get_doc()`; under
  MariaDB `REPEATABLE READ` that reload can return an older snapshot. Keep the
  standard settlement and existing-checkout reuse order Integration Request →
  Payment Request → Sales Invoice. New creation may start from the Sales Invoice
  only while no active Integration Request exists; restart if one appears.
- **Retry only at transaction boundaries.** MariaDB error 1020 is exposed as
  `QueryDeadlockError`. Locked one-attempt helpers must propagate it; callback,
  reconciliation, chargeback, settlement, and pay-link checkout boundaries roll
  back before replaying the complete unit, at most three times. Checkout
  transaction retries stop permanently once a provider POST has been attempted,
  and canonical POSTs have no rate-limit replay. This does not suppress the
  client's separate custom-host Gateway fallback on 401/403/404. Never catch a
  deadlock and continue inside the failed transaction.
- **Custom API hosts are opt-in.** Keep `api_base_domain` host-only and add the
  final `api.<base-domain>` hostname exactly to `payrexx_allowed_api_hosts`; do
  not weaken the parser or move `get_password("api_secret")` before validation.
  Preserve operation-aware fallback: 401/403 may retry any supported operation,
  but a 404 must not retry concrete Gateway retrieval. Gateway creation is the
  risky exception that repeats its POST once on canonical 401/403/404, without a
  provider idempotency key or documented `referenceId` uniqueness.
- **No URL hard-coding** — externally shared URLs go through
  `payrexx_integration.url_utils.get_public_url()`, which respects
  `host_name` without leaking the local bench port. The Playwright config is
  the only place `localhost:8000` appears (test runner, not embedded).

---

## Cross-app integration

- Good Event's default invoice renderer imports
  `payrexx_integration.api.payrexx_pay_url`. Missing or ambiguous Payrexx
  configuration degrades gracefully: the invoice email still sends without
  the online-pay button.

---

## Release history

Version and release rationale live in `REQUIREMENTS.md` §4 ("Versioning"); the
commit log is the source of truth for individual changes (`git log --oneline`).
Do not restate specific commit hashes here — they go stale within a release.
