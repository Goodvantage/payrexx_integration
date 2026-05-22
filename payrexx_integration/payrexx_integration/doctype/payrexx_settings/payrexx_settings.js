// Copyright (c) 2026, Goodvantage GmbH and contributors
// For license information, please see license.txt

(() => {
	const callbackPath =
		"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback";

	function getGatewayName(frm) {
		return (frm.doc.gateway_name || "").trim();
	}

	function getWebhookUrl(frm) {
		const gatewayName = getGatewayName(frm);

		if (!gatewayName) {
			return "";
		}

		return `${
			window.location.origin
		}/api/method/${callbackPath}?gateway_name=${encodeURIComponent(gatewayName)}`;
	}

	function showWebhookUrl(frm) {
		const url = getWebhookUrl(frm);

		if (!url) {
			frm.dashboard.clear_comment();
			return;
		}

		frm.dashboard.add_comment(
			__("Webhook URL for Payrexx: <code>{0}</code>", [frappe.utils.escape_html(url)]),
			"blue",
			true
		);
	}

	frappe.ui.form.on("Payrexx Settings", {
		refresh(frm) {
			const gatewayName = getGatewayName(frm);

			if (gatewayName) {
				frm.dashboard.add_indicator(
					__("Payment Gateway: Payrexx-{0}", [gatewayName]),
					"blue"
				);
			}

			showWebhookUrl(frm);
		},

		gateway_name(frm) {
			showWebhookUrl(frm);
		},
	});
})();
