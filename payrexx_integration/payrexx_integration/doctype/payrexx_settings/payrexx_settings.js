// Copyright (c) 2026, Goodvantage GmbH and contributors
// For license information, please see license.txt

(() => {
	const callbackPath =
		"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback";
	const webhookUrlMethod =
		"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.get_webhook_url";
	const webhookUrlLabel = __("Webhook URL for Payrexx:");
	const webhookUrlSelector = "[data-payrexx-webhook-url]";
	let webhookUrlRequestId = 0;

	function getGatewayName(frm) {
		return (frm.doc.gateway_name || "").trim();
	}

	function getBrowserWebhookUrl(frm) {
		const gatewayName = getGatewayName(frm);

		if (!gatewayName) {
			return "";
		}

		return `${
			window.location.origin
		}/api/method/${callbackPath}?gateway_name=${encodeURIComponent(gatewayName)}`;
	}

	async function showWebhookUrl(frm) {
		const gatewayName = getGatewayName(frm);
		const requestId = ++webhookUrlRequestId;
		clearWebhookUrl(frm);

		if (!gatewayName) {
			return;
		}

		let url = "";
		try {
			const response = await frappe.call({
				method: webhookUrlMethod,
				args: { gateway_name: gatewayName },
			});
			url = response.message;
		} catch {
			url = getBrowserWebhookUrl(frm);
		}

		if (requestId !== webhookUrlRequestId || gatewayName !== getGatewayName(frm)) {
			return;
		}

		renderWebhookUrl(frm, url);
	}

	function renderWebhookUrl(frm, url) {
		if (!url) {
			return;
		}

		frm.dashboard.add_comment(
			`<span data-payrexx-webhook-url>${webhookUrlLabel} <code>${frappe.utils.escape_html(
				url
			)}</code></span>`,
			"blue",
			true
		);
	}

	function clearWebhookUrl(frm) {
		const message = frm.layout?.message;

		if (!message) {
			return;
		}

		message.find(webhookUrlSelector).closest(".form-message").remove();
		message
			.children(".form-message")
			.filter((_, element) => $(element).text().trim().startsWith(webhookUrlLabel))
			.remove();

		if (!message.children().length) {
			message.addClass("hidden");
		}
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
