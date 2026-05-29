from __future__ import annotations

from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import cstr, get_url


def get_public_url(path: str = "") -> str:
	"""Build an externally reachable URL from the configured host_name.

	``frappe.utils.get_url`` appends ``webserver_port`` when a bench runs on a
	non-standard local port. That is fine for direct local access, but hosted
	checkout providers need the public reverse-proxy/ngrok host exactly as set in
	``host_name``.
	"""
	host_name = (frappe.conf.get("host_name") or "").strip().rstrip("/")
	if _has_absolute_host(host_name):
		return f"{host_name}/{path.lstrip('/')}" if path else host_name
	return get_url(path)


def safe_return_url(redirect_to: str, error_label: str = "Unsafe payment redirect URL") -> str:
	target = cstr(redirect_to).strip()
	parts = urlsplit(target)
	if parts.scheme or parts.netloc:
		public_parts = urlsplit(get_public_url(""))
		if parts.scheme in {"http", "https"} and parts.netloc == public_parts.netloc:
			return target
		frappe.throw(_(error_label), frappe.PermissionError)
	return get_public_url(target)


def _has_absolute_host(host_name: str) -> bool:
	parts = urlsplit(host_name)
	return bool(parts.scheme and parts.netloc)
