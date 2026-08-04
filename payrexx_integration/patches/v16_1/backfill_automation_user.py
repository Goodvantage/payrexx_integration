import frappe
from frappe.utils import cstr

from payrexx_integration.session_utils import is_valid_automation_user


def execute():
	if not frappe.db.exists("DocType", "Non Profit Settings") or not frappe.get_meta(
		"Non Profit Settings"
	).has_field("creation_user"):
		return
	legacy_user = cstr(frappe.db.get_single_value("Non Profit Settings", "creation_user")).strip()
	if not is_valid_automation_user(legacy_user):
		return
	for settings_name in frappe.get_all(
		"Payrexx Settings",
		filters={"automation_user": ["is", "not set"]},
		pluck="name",
	):
		frappe.db.set_value(
			"Payrexx Settings",
			settings_name,
			"automation_user",
			legacy_user,
			update_modified=False,
		)
