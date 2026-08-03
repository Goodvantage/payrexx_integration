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

# With no extension provider, only Sales Invoice-backed Payment Requests are supported.
payrexx_settlement_source_providers = []

jinja = {
	"methods": [
		"payrexx_integration.api.payrexx_pay_url",
	],
}
