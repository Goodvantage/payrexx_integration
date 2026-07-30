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
| `payrexx_integration/payrexx_integration/doctype/payrexx_settings/` | The settings DocType. One row per environment (`Sandbox` / `Live`). `on_update` auto-creates the matching `Payment Gateway` row (`Payrexx-<gateway_name>`). |
| `payrexx_integration/payrexx_integration/payrexx/payrexx_client.py` | Thin REST client. **Auth: `x-api-key: <api_secret>` header** — current Payrexx scheme (per the official PHP SDK). The legacy `ApiSignature` body field is no longer used. Canonical `*.payrexx.com` API hosts are trusted by default; custom Platform hosts require exact `payrexx_allowed_api_hosts` site-config entries and are validated before secret access. |
| `payrexx_integration/payrexx_integration/payrexx/webhook_validator.py` | HMAC-SHA256 verification of `X-Webhook-Signature`. Tries base64 first, falls back to hex. The signing key is **separate** from the API secret (configured per webhook in the Payrexx dashboard). |
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
After signature verification, callback side effects run as the configured
`Non Profit Settings.creation_user` when available, else `Administrator`, and
transient `QueryDeadlockError` failures around `on_payment_authorized` are
retried.

### Success redirect URL (generated per Gateway)

```
{{ host_name }}/api/method/payrexx_integration.api.payment_success
  ?ir=<Integration Request name>
  &gateway_name=<Payrexx Settings name>
```

This endpoint is a fallback reconciliation path. It retrieves the Payrexx
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

---

## Payrexx API quick reference

| | |
|---|---|
| Base URL | `https://api.<api_base_domain>/v1.14/`; default `https://api.payrexx.com/v1.14/`; custom final hosts require exact `payrexx_allowed_api_hosts` entries |
| Auth | `x-api-key: <api_secret>` header |
| Required query param | `?instance=<instance_name>` on every call |
| POST body format | `application/x-www-form-urlencoded` |
| Response envelope | `{"status":"success", "data":[ … ]}` |
| Amount unit | Canonical integer hundredths for supported two-decimal currencies (CHF 2.00 → `200`); other fraction units and sub-cent amounts are rejected |
| Webhook signature header | `X-Webhook-Signature` — HMAC-SHA256 of raw body, signed with the per-webhook signing key (NOT the API secret) |

A "no gateway found" GET against `/Gateway/0/` is the cheap auth-check used
by `_ping()` — HTTP 200 with `status: error` means creds are valid.

For Payrexx Platform / partner accounts, split the checkout/login domain:
`customer.pay.goodvantage.ch` means `instance_name = "customer"` and
`api_base_domain = "pay.goodvantage.ch"`. Do not put the full login domain in
`instance_name`. Add the exact final host (`api.pay.goodvantage.ch`) to the
site-config JSON list `payrexx_allowed_api_hosts` before saving. URL-like values,
IP literals, malformed hosts, wildcards, and non-HTTPS ports are rejected before
the Password field is read. If an allowed custom API domain returns 401/403/404,
the client retries the same request once against `api.payrexx.com` so instance
API keys still work when only the checkout/login surface uses a custom domain.

Checkout creation must use the app-owned `_create_integration_request()` path,
never core `create_request_log()` (it commits). Provider metadata and the
Payment Request persist atomically with the caller transaction. Provider success
immediately journals `[Payrexx Gateway recovery] state=local_commit_pending`;
commit adds `local_commit_confirmed` and ordinary rollback adds
`[Payrexx possible orphan Gateway] state=local_rollback_confirmed`. Frappe clears
rollback callbacks before SQL commit, so an unpaired pending record is the exact
commit-failure residual. Operators search by `referenceId`/Gateway id and delete
only a transaction-free external orphan; never add an internal commit or blind
provider teardown to close this gap.

### Status mapping (webhook)

| Payrexx `transaction.status` | `Integration Request.status` | Side effect |
|---|---|---|
| `confirmed` | `Completed` or terminal `Failed` conflict | Settles an active submitted inward Payment Request backed by a submitted Sales Invoice, or an explicitly registered extension source, with exact canonical amount/currency evidence |
| `authorized` | `Authorized` | Records provider state only; this app does not implement later charging |
| `reserved` | `Authorized` | Records provider state only; this app does not implement capture |
| `waiting` | unchanged | In-progress; wait for next webhook |
| `cancelled` / `declined` / `error` / `expired` | `Failed` | Records error string; no provider-side cancellation is initiated |
| `chargeback` | `Failed` | Preserves submitted ledger rows and creates one accounting-review ToDo |
| `refunded` or another unknown status | unchanged | Stores the transaction only; refund reconciliation is not implemented |

These mappings apply to requests that have not completed. Once Completed, all
delayed or replayed non-chargeback statuses are ignored so neither the status nor
the confirmed transaction evidence can be downgraded. A verified `chargeback`
remains allowed and moves the request to Failed for accounting review.
After chargeback evidence exists, all non-chargeback statuses, including
`confirmed`, are terminally ignored; preserve Failed status, the chargeback
error, and the first chargeback transaction. Only duplicate chargeback delivery
may re-enter the idempotent review-ToDo path.

The integration creates hosted Gateways and reads Gateway/Transaction state. It
does not expose capture, later-charge, void/cancel, or refund operations. Those
provider actions and their ERPNext accounting reversals are manual operational
workflows until an explicit, tested contract is implemented.

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

# Playwright e2e (core specs plus an optional existing-booking email check)
cd playwright
npm install && npx playwright install chromium
TEST_BOOKING_NAME=<booking> npx playwright test
```

`TEST_BOOKING_NAME` must identify an existing eligible Good Event Booking
created through Good Event's own fixtures or operator workflow. Payrexx
Integration does not create cross-app event test data.

Hosted sandbox settlement uses the separately documented protected CLI. Keep
the provider page human-operated, require provider `TEST` evidence, and disable
`payrexx_hosted_qa_enabled` after the run.

---

## Gotchas

- **Settings save calls Payrexx live.** `validate()._ping()` hits
  `GET /Gateway/0/` to verify credentials. With bogus creds the save
  fails with "Payrexx rejected the API Secret". The ping is skipped when
  `frappe.flags.in_test` or `frappe.flags.in_install` is set.
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
  back before replaying the complete unit, at most three times. Checkout retries
  stop permanently once a provider POST has been attempted. Never catch a
  deadlock and continue inside the failed transaction or replay Gateway creation.
- **Custom API hosts are opt-in.** Keep `api_base_domain` host-only and add the
  final `api.<base-domain>` hostname exactly to `payrexx_allowed_api_hosts`; do
  not weaken the parser or move `get_password("api_secret")` before validation.
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

## Recent commit

- `f6d0499 feat: Payrexx payment gateway with email integration + tests` —
  initial complete implementation.
