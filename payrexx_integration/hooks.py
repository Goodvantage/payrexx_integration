app_name = "payrexx_integration"
app_title = "Payrexx Integration"
app_publisher = "Goodvantage GmbH"
app_description = "Payrexx payment gateway integration for the Frappe payments app"
app_logo_url = "/assets/payrexx_integration/images/payrexx-integration-app-logo.svg"
app_email = "info@goodvanta.ge"
app_license = "unlicense"

# Apps
# ------------------

required_apps = ["payments"]

# Extension point for apps that own a non-Sales-Invoice settlement source.
# With no provider registered, only Sales Invoice-backed Payment Requests are supported.
payrexx_settlement_source_providers = []

# Extension point for the app that owns a recurring instruction. Called as
# provider(event="charge"|"reversal"|"status", subscription=..., transaction=...,
# reference_id=..., status=..., settings_name=...); return True to claim it.
# This app has no concept of a recurring donation or membership, so with no
# provider registered a subscription event is logged and otherwise ignored.
# Calls run as the owning gateway's automation user. Installment and reversal
# identity is claimed durably before the first provider is called; providers
# must keep their effects transaction-local and return False without side
# effects when the recurring instruction is not theirs.
payrexx_subscription_event_providers = []

# Extension point for leaving a provider-refund notice on a non-Sales-Invoice
# source document. Called as provider(integration_request=..., reversal=...);
# return True once the reference type has been handled. With no provider
# registered, only the Payment Request -> Sales Invoice chain is annotated.
payrexx_refund_notice_providers = []

# Webhook delivery is not a guarantee. The worker recovers real subscription
# transactions through a bounded UTC cursor before its status-only subscription
# sweep; only the provider transaction endpoint supplies settlement evidence.
scheduler_events = {
	"daily": [
		"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
		"payrexx_settings.enqueue_subscription_reconciliation",
	],
}

jinja = {
	"methods": [
		"payrexx_integration.api.payrexx_pay_url",
	],
}
