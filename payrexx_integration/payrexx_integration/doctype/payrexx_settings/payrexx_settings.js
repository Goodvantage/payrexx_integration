// Copyright (c) 2026, Goodvantage GmbH and contributors
// For license information, please see license.txt

frappe.ui.form.on("Payrexx Settings", {
	refresh(frm) {
		if (frm.doc.gateway_name) {
			frm.dashboard.add_indicator(
				__("Payment Gateway: Payrexx-{0}", [frm.doc.gateway_name]),
				"blue"
			);
		}
		if (frm.doc.gateway_name && !frm.is_new()) {
			const path =
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback";
			const url = `${
				window.location.origin
			}/api/method/${path}?gateway_name=${encodeURIComponent(frm.doc.gateway_name)}`;
			frm.dashboard.add_comment(
				__("Webhook URL for Payrexx: <code>{0}</code>", [frappe.utils.escape_html(url)]),
				"blue",
				true
			);
		}
	},
});
