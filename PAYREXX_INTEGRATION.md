# Payrexx Payment Gateway — `payrexx_integration` App

Design reference for the Payrexx gateway app: why it exists, how it plugs into
upstream `payments`, and the provider wire format it speaks.

This is the thinnest of the repo's docs on purpose — it does not repeat the
canonical material:

| Looking for | Read |
|---|---|
| Numbered requirements | [`REQUIREMENTS.md`](REQUIREMENTS.md) |
| Architecture, modules, URL contracts, security model, test commands | [`DOCUMENTATION.md`](DOCUMENTATION.md) |
| Operator setup, webhook configuration, troubleshooting, runbooks | [`HOW_TO.md`](HOW_TO.md) |
| Coding-agent rules, gotchas, API quick reference, status mapping | [`AGENTS.md`](AGENTS.md) |
| Installation | [`README.md`](README.md) |

| | |
|---|---|
| **App name** | `payrexx_integration` |
| **Title** | Payrexx Integration |
| **Publisher** | Goodvantage GmbH (`info@goodvanta.ge`) |
| **License** | unlicense |
| **Module** | `Payrexx Integration` |
| **Required apps** | `payments` |
| **Pattern** | Server-side Gateway create → hosted Payrexx checkout → webhook callback |

---

## 1. How the integration plugs in

Frappe's `payments` app uses a registry pattern: a `Payment Gateway` row maps a
gateway name to a settings DocType + controller. Any app can contribute a
gateway by:

1. Defining a `<X> Settings` DocType with controller methods (`get_payment_url`,
   `validate_transaction_currency`, etc.).
2. Calling `payments.utils.create_payment_gateway(...)` in `on_update` to insert
   the registry row.

That's exactly what this app does, which is why it needs neither a fork of
upstream `payments` nor a fixture: the `Payment Gateway` row is created
automatically the first time you save `Payrexx Settings`.

End-to-end flow:

```
Sales Invoice-backed Payment Request
        │
        │ get_payment_url()        ← controller method on Payrexx Settings
        ▼
Payrexx Settings.get_payment_url()
        │  • app-owned insert      → uncommitted Integration Request (Queued)
        │  • POST /Gateway/        → Payrexx hosted checkout URL
        │  • atomically store id/hash/link/amount/currency in request transaction
        ▼
Customer pays on https://<instance>.payrexx.com/...
        │
        ▼
Payrexx → POST callback URL (webhook)
        │  • verify X-Webhook-Signature (HMAC-SHA256 of raw body)
        │  • lookup Integration Request by referenceId
        │  • update status → Completed / Authorized / Failed
        │  • settle the Payment Request through set_as_paid()
        ▼
Customer returns through server-side reconciliation; an IR-bound confirmed
transaction redirects to same-site redirect_to when present, otherwise /payment-success
```

The locking, reuse, retry, and commit rules behind those steps are specified in
`DOCUMENTATION.md` ("URL Contracts", "Supported Payment Operations") and
`REQUIREMENTS.md` §2.

---

## 2. Repository layout

```
apps/payrexx_integration/
├── pyproject.toml                          # Goodvantage GmbH, Python 3.14, ruff
├── license.txt                             # unlicense
├── README.md
└── payrexx_integration/                    # Python package
    ├── hooks.py                            # required_apps = ["payments"], jinja methods
    ├── api.py                              # pay-by-email URL + guest endpoints
    ├── gateway_selection.py                # strict shared settings resolver
    ├── url_utils.py                        # public URL / same-site return URL helpers
    ├── session_utils.py                    # local automation-user context manager
    ├── hosted_qa.py                        # gated hosted sandbox acceptance
    ├── modules.txt                         # Payrexx Integration
    └── payrexx_integration/                # Frappe module folder
        ├── doctype/
        │   └── payrexx_settings/
        │       ├── payrexx_settings.json   # DocType definition
        │       ├── payrexx_settings.py     # Controller (gateway logic + webhook)
        │       ├── payrexx_settings.js     # Desk form helpers
        │       └── test_payrexx_settings.py
        └── payrexx/                        # Helper Python package
            ├── payrexx_client.py           # Thin Payrexx REST client
            └── webhook_validator.py        # HMAC-SHA256 signature check
```

The DocType is **not single** — you can have multiple `Payrexx Settings` rows
(e.g. `Live` and `Sandbox`), each producing its own `Payment Gateway`
(`Payrexx-Live`, `Payrexx-Sandbox`). The webhook URL takes a `?gateway_name=…`
query param to disambiguate which signing key to verify against.

Code that needs a settings controller should use
`payrexx_integration.gateway_selection.resolve_payrexx_settings()`. It resolves
an explicit gateway, an optional caller-owned site-config key, or the only row.
It raises on zero or multiple fallback candidates and never guesses based on
row order or names such as `Live` and `Sandbox`.

### `Payrexx Settings` fields

| Field | Type | Notes |
|---|---|---|
| `gateway_name` | Data, unique, reqd | `Live`, `Sandbox`, … — used to build `Payrexx-{name}` |
| `instance_name` | Data, reqd | Payrexx instance subdomain. For `customer.pay.goodvantage.ch`, use `customer` |
| `api_base_domain` | Data, default `payrexx.com`, reqd | Host-only API base domain. Normal accounts use `payrexx.com`; platform accounts use the remaining domain, e.g. `pay.goodvantage.ch`, with exact final host allowlisting |
| `api_secret` | Password, reqd | Sent as `x-api-key` header |
| `webhook_signing_key` | Password, reqd | HMAC key for `X-Webhook-Signature` |
| `automation_user` | Link (User), reqd | Owning enabled System User for checkout, settlement, and accounting-review side effects; no fallback user |
| `supported_currencies` | Small Text, default `CHF,EUR,USD,GBP` | Comma list, validated per transaction |
| `psp` | Small Text | Optional comma list of PSP IDs |
| `validity_minutes` | Int | Optional gateway TTL |
| `allow_test_transactions` | Check, default off | Allows signed TEST payments to settle; sandbox only |
| `enable_managed_subscriptions` | Check, default off | Allows new managed-subscription checkouts after signed sandbox verification; does not stop existing-mandate processing |
| `transaction_reconciliation_cursor` | Datetime, read-only | UTC high-water mark for bounded transaction recovery; each run re-reads an overlap |
| `success_redirect_url` / `failed_redirect_url` / `cancel_redirect_url` | Data | Optional global overrides; defaults bring success through the reconciliation endpoint and failed/cancelled returns to `/payment-failed`. Per-checkout `failed_redirect_to` / `cancel_redirect_to` kwargs override the generic failed/cancelled pages for branded flows. |

The REST API version is code-pinned at `v1.16` through
`payrexx_client.DEFAULT_API_VERSION`. It is intentionally not a settings field:
an API-version change is an integration release that must pass the complete
client and settlement suite rather than an unreviewed per-site toggle.
The official PHP SDK v2.0.15 is not the version baseline for this app. Its code
defaults to API `v1.15`; the communicator invokes its configured adapter once
and implements neither built-in custom-host fallback nor an idempotency key. An
injected adapter may still perform retries or multiple wire requests. The same
tag's README is stale/inconsistent: it says API `v1.11` and calls SDK v2.0.0
current stable, while tagged code declares v2.0.15 and supports/defaults through
`v1.15`. The app's `v1.16` pin and replay behavior are separate contracts.

Installation is in `README.md`; creating the settings row, the
`Payment Gateway Account`, and the Payrexx webhook is in `HOW_TO.md` §1–§4.

---

## 3. Provider wire format

The base URL, auth header, mandatory `?instance=` param, POST encoding, response
envelope, amount unit, and webhook signature header are tabulated in `AGENTS.md`
("Payrexx API quick reference"). The provider payload details below exist only
here.

### Client endpoints

| Verb | Path | Purpose |
|---|---|---|
| `POST` | `/Gateway/` | Create a hosted checkout. Returns `{id, link, hash, status}`. |
| `GET` | `/Gateway/{id}/` | Look up a gateway and its `invoices[].transactions[]`. |
| `GET` | `/Gateway/0/` | Credential ping — HTTP 200 with `status: error` means the credentials are valid. |
| `DELETE` | `/Gateway/{id}/` | Provider-supported Gateway deletion. This app exposes no wrapper; manual cleanup requires exact transaction-free evidence and provider permission. |
| `POST` | `/Subscription/` | Create a subscription for an already-known Payrexx contact. Normal signup uses Gateway subscription parameters instead. |
| `GET` | `/Subscription/` or `/Subscription/{id}/` | List or retrieve subscriptions; list pagination uses query parameters. |
| `GET` | `/Transaction/` | Recover real transaction evidence by bounded UTC/query pagination. |
| `PUT` | `/Subscription/{id}/` | Update future subscription terms. |
| `DELETE` | `/Subscription/{id}/` | Cancel a subscription immediately. |
| `POST` / `DELETE` | `/QrCode/` / `/QrCode/{uuid}/` | Create or delete a permanent static QR code. |

The provider has Gateway deletion, but this app exposes no Gateway-delete,
capture, later-charge, void, or refund-initiation method (`REQUIREMENTS.md`
REQ-PRX-BND-01). For zero discovered Gateways no deletion is needed; an operator
may conditionally use provider Gateway DELETE on each exact discovered Gateway
only when retrieval and transaction search prove it contains no transaction.

A POST sent directly to canonical `api.payrexx.com` has no rate-limit retry, and
the checkout transaction is not deadlock-retried after provider contact. The
custom-host fallback is a separate exception: every custom-host POST can repeat
once on canonical 401/403, and Gateway creation also repeats there on 404.
Therefore custom-host Gateway creation can make two POST requests for one app
call. Subscription and QR creation treat 404 as authoritative to avoid that
second-host creation. Neither the provider contract nor this app has an
idempotency key, and Payrexx describes `referenceId` only as an internal merchant
reference, not as unique or de-duplicating.

The app sends `application/x-www-form-urlencoded`; that remains an official
media type in Payrexx's Gateway OpenAPI. The official PHP SDK v2.0.15's bundled
cURL/Guzzle adapters send JSON for ordinary non-file creates, but that transport
choice does not make the provider's form encoding obsolete.

### `POST /Gateway/` parameters emitted by this app

| Param | Required | Notes |
|---|---|---|
| `amount` | yes | Smallest currency unit |
| `currency` | yes | ISO 4217 |
| `referenceId` | yes | Integration Request name echoed in the webhook |
| `purpose` | no | Shown on receipt |
| `successRedirectUrl` / `failedRedirectUrl` / `cancelRedirectUrl` | yes | Resolved defaults or configured/per-request overrides |
| `psp[0]`, `psp[1]`, … | no | Restrict to specific PSP IDs |
| `fields[email][value]` | no | Customer prefill |
| `validity` | no | Gateway TTL in minutes |
| `subscriptionState` | no | Turns the Gateway into a subscription signup |
| `subscriptionInterval` | with `subscriptionState` | Required validated billing interval |
| `subscriptionPeriod` / `subscriptionCancellationInterval` | no | Sent only when the caller explicitly supplies them |

### Webhook payload (JSON content type)

```json
{
  "transaction": {
    "id": 2012844,
    "status": "confirmed",
    "amount": 200,
    "currency": "CHF",
    "referenceId": "<Integration Request name>",
    "invoice": {
      "referenceId": "<Integration Request name>",
      "paymentLinkId": 17
    },
    "psp": "Stripe Connect",
    "payment": { "brand": "visa" },
    "contact": { "email": "...", "forename": "...", "surname": "..." }
  }
}
```

Payrexx documents `X-Webhook-Signature` as a lowercase-hex HMAC-SHA256 digest of
the raw body, keyed with the per-webhook signing key (separate from the API
secret). `webhook_validator.verify_webhook_signature` verifies hex first and
retains base64 only as a compatibility representation for previously observed /
account-configured deliveries.

The `transaction.status` → `Integration Request.status` mapping, including the
terminal-evidence and chargeback rules, is in `AGENTS.md` ("Status mapping") and
`DOCUMENTATION.md` ("Supported Payment Operations", "Chargebacks").

### Credential handling

The API secret is never held in a variable, argument, or attribute on the
request path — it lives only in the closure of the `requests` auth callable, and
requests are sent as session-prepared requests instead of through
`frappe.integrations.utils.make_*_request`. Frappe logs frame variables for
failed outbound requests and does not redact an `x-api-key` header key. See
`AGENTS.md` ("Never let the API secret become a variable") and REQ-PRX-SEC-05.

---

## 4. Manual acceptance checklist

Automated suites and their commands are listed in `DOCUMENTATION.md`
("Testing"). This checklist covers what only a human on a real (sandbox) tenant
can confirm:

The custom-host fallback is not part of this general checklist. Its safe live
run is deferred until a dedicated controllable custom API target and confirmed
Gateway DELETE permission exist. It uses a direct one-shot client harness that
does not save the source Settings row, not a normal checkout or local Integration
Request flow; validates settings before arming one fault; handles exact local
and zero/one/multiple external state; first proves a separate transaction-free
Gateway canary can be deleted and confirmed absent; and restores settings/allowlists
unconditionally in `finally`. Follow `HOW_TO.md` §12 and never use a shared host
or production credentials.

- [ ] `bench --site <site> migrate` runs cleanly; `Payrexx Settings` appears in
      the DocType list.
- [ ] Saving a settings row creates a `Payment Gateway` named
      `Payrexx-{gateway_name}` (visible in the desk).
- [ ] A `Payment Gateway Account` exists for the generated gateway, test
      company, currency, and receiving account.
- [ ] In `bench console`:
      ```python
      frappe.get_doc("Payment Request", "ACC-PRQ-...").get_payment_url()
      # → returns a https://<instance>.payrexx.com/...?... URL
      ```
- [ ] Completing a sandbox payment fires the webhook; the matching
      `Integration Request` flips to `Completed`.
- [ ] A managed-subscription sandbox run captures the exact signed lifecycle
      body. Compare it field-for-field with `webhook_payload.py`, then prove the
      after-commit `GET /Transaction/` recovery settles the first charge and a
      later charge creates exactly one installment. Only then enable **Enable
      Managed Subscriptions** on that settings row.
- [ ] Forging a request with a wrong `X-Webhook-Signature` is rejected
      (check `Error Log`).
- [ ] A failed provider call (e.g. a deliberately wrong API secret) writes an
      `Error Log` entry containing neither the API secret nor payer data.
- [ ] The Sales Invoice-backed Payment Request becomes Paid through
      `set_as_paid()`, with exactly one submitted Payment Entry.
- [ ] A `cancel` from the Payrexx checkout returns the user to
      `/payment-failed` and the Integration Request stays `Queued` (no webhook
      until status actually changes) or transitions to `Failed`.

---

## 5. External references

- Payrexx Gateway create API — <https://developers.payrexx.com/reference/create-a-gateway>
- Payrexx Gateway delete API — <https://developers.payrexx.com/reference/delete-a-gateway>
- Payrexx Webhook docs — <https://docs.payrexx.com/developer/guides/webhook>
- Payrexx Build Gateway guide — <https://docs.payrexx.com/developer/guides/gateway/build>
- Payrexx PHP SDK v2.0.15 comparison baseline — <https://github.com/payrexx/payrexx-php/tree/v2.0.15>
- Frappe `payments` app — <https://github.com/frappe/payments>
- In-bench analog — `apps/payments/payments/payment_gateways/doctype/paymob_settings/paymob_settings.py`
- Stripe-style `on_update` registration — `apps/payments/payments/payment_gateways/doctype/stripe_settings/stripe_settings.py`
- Gateway resolver — `apps/payments/payments/utils/utils.py::get_payment_gateway_controller`
