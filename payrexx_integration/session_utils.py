"""Privilege-switch helpers shared by the Payrexx endpoints.

Kept locally (instead of good_connector.workflow_support.as_automation_user)
because payrexx_integration does not depend on good_connector. There is
exactly one implementation here — the api.py and payrexx_settings.py copies
were consolidated.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


def is_valid_automation_user(user_name: str | None) -> bool:
	user_name = cstr(user_name).strip()
	if not user_name:
		return False
	user = frappe.db.get_value("User", user_name, ["enabled", "user_type"], as_dict=True)
	return bool(user and cint(user.enabled) and user.user_type == "System User")


def payment_authorization_user_name(settings: str | Document) -> str:
	"""Return the owning gateway's configured, enabled System User."""
	settings = frappe.get_cached_doc("Payrexx Settings", settings) if isinstance(settings, str) else settings
	user_name = cstr(settings.get("automation_user")).strip()
	if not user_name:
		frappe.throw(
			_("Payrexx Settings {0} requires an Automation User.").format(settings.name),
			frappe.ValidationError,
		)
	if not is_valid_automation_user(user_name):
		frappe.throw(
			_("Automation User {0} must be an enabled System User.").format(user_name),
			frappe.ValidationError,
		)
	return user_name


@contextmanager
def as_automation_user(settings: str | Document):
	automation_user = payment_authorization_user_name(settings)
	previous_user = frappe.session.user
	previous_sid = getattr(frappe.session, "sid", None)
	previous_data = getattr(frappe.session, "data", None)

	if automation_user and automation_user != previous_user:
		frappe.set_user(automation_user)  # nosemgrep: frappe-setuser

	try:
		yield
	finally:
		if automation_user and automation_user != previous_user:
			frappe.set_user(previous_user)  # nosemgrep: frappe-setuser
			frappe.session.sid = previous_sid
			frappe.session.data = previous_data
