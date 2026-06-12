"""Privilege-switch helpers shared by the Payrexx endpoints.

Kept locally (instead of good_connector.workflow_support.as_automation_user)
because payrexx_integration does not depend on good_connector. There is
exactly one implementation here — the api.py and payrexx_settings.py copies
were consolidated.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe


def payment_authorization_user_name() -> str:
	"""Least-privilege automation user for payment side effects.

	Uses the configured ``Non Profit Settings.creation_user`` when available,
	falling back to Administrator. Both guest payment paths (pay-by-email
	redirect and webhook authorization) resolve through this so neither runs
	with more privilege than the other.
	"""
	if frappe.db.exists("DocType", "Non Profit Settings"):
		creation_user = frappe.db.get_single_value("Non Profit Settings", "creation_user")
		if creation_user and frappe.db.exists("User", creation_user):
			return creation_user

	return "Administrator"


@contextmanager
def as_automation_user(user_name: str | None = None):
	automation_user = user_name or payment_authorization_user_name()
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
