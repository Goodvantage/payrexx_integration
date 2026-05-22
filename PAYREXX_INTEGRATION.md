# Payrexx Payment Gateway — `payrexx_integration` App

A standalone Frappe app that adds **Payrexx** as a payment gateway, depending on
the `payments` app. Lives at `frappe-bench/apps/payrexx_integration/`.

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

That's exactly what this app does. The `Payment Gateway` row is created
automatically the first time you save `Payrexx Settings` — no fixture needed.

End-to-end flow:

```
Reference doc (Sales Invoice / Payment Request / Web Form)
        │
        │ get_payment_url()        ← controller method on Payrexx Settings
        ▼
Payrexx Settings.get_payment_url()
        │  • create_request_log()  → Integration Request (Queued)
        │  • POST /Gateway/        → Payrexx hosted checkout URL
        │  • stash gateway id on Integration Request.data
        ▼
Customer pays on https://<instance>.payrexx.com/...
        │
        ▼
Payrexx → POST callback URL (webhook)
        │  • verify X-Webhook-Signature (HMAC-SHA256 of raw body)
        │  • lookup Integration Request by referenceId
        │  • update status → Completed / Authorized / Failed
        │  • run reference_doc.on_payment_authorized("Completed")
        ▼
Customer redirected to redirect_to when present, otherwise /payment-success?doctype=...&docname=...
```

---

## 2. What's in the app

```
apps/payrexx_integration/
├── pyproject.toml                          # Goodvantage GmbH, Python 3.14, ruff
├── license.txt                             # unlicense
├── README.md
└── payrexx_integration/                    # Python package
    ├── hooks.py                            # required_apps = ["payments"]
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

### `Payrexx Settings` fields

| Field | Type | Notes |
|---|---|---|
| `gateway_name` | Data, unique, reqd | `Live`, `Sandbox`, … — used to build `Payrexx-{name}` |
| `instance_name` | Data, reqd | Payrexx instance subdomain. For `kibesuisse.pay.goodvantage.ch`, use `kibesuisse` |
| `api_base_domain` | Data, default `payrexx.com`, reqd | API base domain. Normal accounts use `payrexx.com`; platform accounts use the remaining domain, e.g. `pay.goodvantage.ch` |
| `api_version` | Data, default `v1.14` | Bump without code change |
| `api_secret` | Password, reqd | Sent as `x-api-key` header |
| `webhook_signing_key` | Password, reqd | HMAC key for `X-Webhook-Signature` |
| `supported_currencies` | Small Text, default `CHF,EUR,USD,GBP` | Comma list, validated per transaction |
| `psp` | Small Text | Optional comma list of PSP IDs |
| `validity_minutes` | Int | Optional gateway TTL |
| `success_redirect_url` / `failed_redirect_url` / `cancel_redirect_url` | Data | Optional global overrides; defaults bring success through the reconciliation endpoint and failed/cancelled returns to `/payment-failed`. Per-checkout `failed_redirect_to` / `cancel_redirect_to` kwargs override the generic failed/cancelled pages for branded flows. |

---

## 3. Installation

```bash
# from the bench root
cd /workspace/development/frappe-bench

# 1. Make sure the prerequisite app is on the site
bench --site <your-site> install-app payments

# 2. Install this app
bench --site <your-site> install-app payrexx_integration

# 3. Apply schema (creates the Payrexx Settings DocType)
bench --site <your-site> migrate
```

The app is already pip-installed in the bench (it was added to
`sites/apps.txt` automatically by `bench new-app`), so steps 2 and 3 are all
that's needed on each site.

---

## 4. Configuration

### 4.1 Create a `Payrexx Settings` row

In the desk, open **Payrexx Settings → New** and fill in:

| Field | Value |
|---|---|
| Gateway Name | `Live` (or `Sandbox`) |
| Instance Name | your Payrexx instance subdomain |
| API Base Domain | `payrexx.com`, or the partner/platform base domain such as `pay.goodvantage.ch` |
| API Secret | from Payrexx → Integrations → API & Plugins |
| Webhook Signing Key | from Payrexx → Webhooks → (signing key field) |

For a partner checkout domain such as `kibesuisse.pay.goodvantage.ch`, set
`Instance Name` to `kibesuisse` and `API Base Domain` to
`pay.goodvantage.ch`. The app then calls
`https://api.pay.goodvantage.ch/v1.14/...`.

Save. Two things happen automatically:
1. `validate()` pings `GET /Gateway/?limit=1` — if your credentials are wrong
   the save is rejected.
2. `on_update()` calls `create_payment_gateway("Payrexx-Live", …)` — a new
   `Payment Gateway` row appears in the desk.

### 4.2 Configure the webhook in Payrexx

In the Payrexx merchant dashboard go to **Settings → Webhooks → Add**:

- **URL** (single-row case):
  ```
  https://<your-site>/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback
  ```
- **URL** (multi-row, must include the `gateway_name`):
  ```
  https://<your-site>/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=Live
  ```
- **Content type**: `application/json`
- **Retry on failure**: enabled (recommended)
- Copy the signing key Payrexx shows you into the **Webhook Signing Key** field
  on the settings doc.

The webhook URL is also displayed in the dashboard of the settings form for
convenience (see `payrexx_settings.js`).

### 4.3 Use it

In a `Payment Request` (or any flow that takes a Payment Gateway), pick
`Payrexx-Live`. On submit, the customer gets a Payrexx hosted checkout URL.

---

## 5. Payrexx API quick reference

| | |
|---|---|
| **Base URL** | `https://api.<api_base_domain>/v1.14/`, default `https://api.payrexx.com/v1.14/` |
| **Auth** | `x-api-key: <api_secret>` header |
| **Required query param** | `?instance=<your_instance>` on every request |
| **POST body format** | `application/x-www-form-urlencoded` |
| **Response envelope** | `{"status":"success", "data":[ … ]}` |
| **Amount unit** | Smallest currency unit (CHF 2.00 → `200`) |

### Endpoints used by this app

| Verb | Path | Purpose |
|---|---|---|
| `POST` | `/Gateway/` | Create a hosted checkout. Returns `{id, link, hash, status}`. |
| `GET` | `/Gateway/{id}/` | Look up a gateway and its `invoices[].transactions[]`. |
| `GET` | `/Transaction/{id}/` | Look up a single transaction (used for reconciliation). |

### Key `POST /Gateway/` parameters

| Param | Required | Notes |
|---|---|---|
| `amount` | yes | Smallest currency unit |
| `currency` | yes | ISO 4217 |
| `referenceId` | recommended | We use the Integration Request name → echoed in webhook |
| `purpose` | no | Shown on receipt |
| `successRedirectUrl` / `failedRedirectUrl` / `cancelRedirectUrl` | no | URL-encode if they contain query params |
| `psp[0]`, `psp[1]`, … | no | Restrict to specific PSP IDs |
| `pm[0]`, `pm[1]`, … | no | Restrict to specific payment methods (`visa`, `twint`, …) |
| `fields[email][value]` | no | Customer prefill |
| `validity` | no | Gateway TTL in minutes |
| `preAuthorization` | no | `1` → tokenisation flow (charge later) |
| `reservation` | no | `1` → pre-auth flow (capture later) |
| `subscriptionState` | no | `1` → recurring; pair with `subscriptionInterval` etc. |

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

### Status mapping (controller logic)

| Payrexx `transaction.status` | Integration Request status | Notes |
|---|---|---|
| `confirmed` | `Completed` | Runs `on_payment_authorized` on the reference doc |
| `authorized` | `Authorized` | Tokenised — charge later via `/Transaction/{id}/` |
| `reserved` | `Authorized` | Pre-auth hold — capture later |
| `waiting` | unchanged | In-progress; expect another webhook |
| `cancelled`, `declined`, `error`, `expired`, `chargeback` | `Failed` | Records error string |

### Webhook signature

Payrexx delivers `X-Webhook-Signature: <base64 HMAC-SHA256 of raw body>`. The
key is the per-webhook signing key (separate from the API secret) configured in
the Payrexx dashboard. `webhook_validator.verify_webhook_signature` checks
base64 first and falls back to hex — confirm which encoding your account uses
on the first sandbox webhook and remove the unused branch if you want to be
strict.

---

## 6. Testing checklist

- [ ] `bench --site <site> migrate` runs cleanly; `Payrexx Settings` appears in
      the DocType list.
- [ ] Saving a settings row creates a `Payment Gateway` named
      `Payrexx-{gateway_name}` (visible in the desk).
- [ ] In `bench console`:
      ```python
      frappe.get_doc("Payrexx Settings", "Live").get_payment_url(
          amount=10, currency="CHF",
          reference_doctype="Sales Invoice", reference_docname="ACC-SINV-...",
          payer_name="Test User", payer_email="test@example.com",
          description="Smoke test",
      )
      # → returns a https://<instance>.payrexx.com/...?... URL
      ```
- [ ] Completing a sandbox payment fires the webhook; the matching
      `Integration Request` flips to `Completed`.
- [ ] Forging a request with a wrong `X-Webhook-Signature` is rejected
      (check `Error Log`).
- [ ] The reference doc's `on_payment_authorized` method fires (e.g. Payment
      Request → Status = Paid for an ERPNext flow).
- [ ] A `cancel` from the Payrexx checkout returns the user to
      `/payment-failed` and the Integration Request stays `Queued` (no webhook
      until status actually changes) or transitions to `Failed`.

---

## 7. Where to find things

| Concern | File |
|---|---|
| DocType definition | `payrexx_integration/payrexx_integration/payrexx_integration/doctype/payrexx_settings/payrexx_settings.json` |
| Controller + webhook | `…/doctype/payrexx_settings/payrexx_settings.py` |
| Desk form helpers (shows webhook URL) | `…/doctype/payrexx_settings/payrexx_settings.js` |
| HTTP client | `…/payrexx_integration/payrexx/payrexx_client.py` |
| Signature verifier | `…/payrexx_integration/payrexx/webhook_validator.py` |
| App-level config | `payrexx_integration/payrexx_integration/hooks.py` |

---

## 8. References

- Payrexx Gateway API — <https://developers.payrexx.com/reference/create-a-gateway>
- Payrexx Webhook docs — <https://docs.payrexx.com/developer/guides/webhook>
- Payrexx Build Gateway guide — <https://docs.payrexx.com/developer/guides/gateway/build>
- Payrexx PHP SDK (auth pattern) — <https://github.com/payrexx/payrexx-php>
- Frappe `payments` app — <https://github.com/frappe/payments>
- In-bench analog — `apps/payments/payments/payment_gateways/doctype/paymob_settings/paymob_settings.py`
- Stripe-style `on_update` registration — `apps/payments/payments/payment_gateways/doctype/stripe_settings/stripe_settings.py`
- Gateway resolver — `apps/payments/payments/utils/utils.py::get_payment_gateway_controller`
