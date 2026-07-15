# Copyright (c) 2026, Goodvantage GmbH and contributors

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr


def resolve_payrexx_settings(
	gateway_name: str | None = None,
	*,
	site_config_key: str | None = None,
) -> Document:
	"""Return one unambiguous Payrexx Settings document.

	An explicit gateway wins, followed by the optional caller-owned site config
	key, then a single configured row. Multiple rows are never resolved by order
	or conventional names because that can route production payments to Sandbox.
	"""
	selected_gateway = cstr(gateway_name).strip()
	selection_source = ""
	if not selected_gateway and site_config_key:
		selected_gateway = cstr(frappe.conf.get(site_config_key)).strip()
		selection_source = site_config_key if selected_gateway else ""

	if selected_gateway:
		if frappe.db.exists("Payrexx Settings", selected_gateway):
			return frappe.get_cached_doc("Payrexx Settings", selected_gateway)
		if selection_source:
			frappe.throw(
				_("The Payrexx gateway {0} from site config ({1}) is not configured.").format(
					selected_gateway, selection_source
				)
			)
		frappe.throw(_("Payrexx gateway {0} is not configured.").format(selected_gateway))

	rows = frappe.get_all("Payrexx Settings", pluck="name", limit=2, order_by="creation asc")
	if len(rows) == 1:
		return frappe.get_cached_doc("Payrexx Settings", rows[0])
	if not rows:
		frappe.throw(_("No Payrexx Settings are configured."))
	if site_config_key:
		frappe.throw(
			_(
				"Multiple Payrexx gateways are configured. Set {0} in site config or choose one explicitly."
			).format(site_config_key)
		)
	frappe.throw(_("Multiple Payrexx gateways are configured. Choose one explicitly."))
