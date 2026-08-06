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
	try:
		parts = urlsplit(target)
	except ValueError:
		frappe.throw(_(error_label), frappe.PermissionError)
	if len(target) >= 2 and target[0] in "/\\" and target[1] in "/\\":
		frappe.throw(_(error_label), frappe.PermissionError)
	if parts.scheme or parts.netloc:
		origin = _normalized_http_origin(target)
		if origin and origin in _allowed_public_origins():
			return target
		frappe.throw(_(error_label), frappe.PermissionError)
	expanded = get_public_url(target)
	if _normalized_http_origin(expanded) not in _allowed_public_origins():
		frappe.throw(_(error_label), frappe.PermissionError)
	return expanded


def is_allowed_public_origin(url: str) -> bool:
	"""True when ``url`` is an absolute HTTP(S) URL on an origin this site publishes.

	Public wrapper around the allowlist ``safe_return_url`` enforces, so every
	caller that has to bind a different kind of externally supplied absolute URL
	to this site — the permanent static QR target, for example — shares one
	definition of "an origin an operator published" instead of re-implementing
	origin normalization.
	"""
	origin = _normalized_http_origin(cstr(url).strip())
	return origin is not None and origin in _allowed_public_origins()


def _allowed_public_origins() -> set[tuple[str, str, int]]:
	"""Normalized origins an operator explicitly published through site config.

	The canonical public origin comes from ``host_name`` (or the request URL
	fallback). Apps that expose the same site under an additional public URL —
	a tunnel or a second domain — advertise it through ``*_public_base_url``
	keys. Site config is operator-owned, never guest input, so trusting every
	configured public base keeps the guest-input protection intact while
	letting upstream apps hand over URLs built from their own public base.
	"""
	origins = set()
	canonical = _normalized_http_origin(get_public_url(""))
	if canonical:
		origins.add(canonical)
	for key, value in frappe.conf.items():
		if not cstr(key).endswith("_public_base_url"):
			continue
		origin = _normalized_http_origin(cstr(value).strip().rstrip("/"))
		if origin:
			origins.add(origin)
	return origins


def _normalized_http_origin(url: str) -> tuple[str, str, int] | None:
	if not url or any(
		character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
	):
		return None
	try:
		parts = urlsplit(url)
	except ValueError:
		return None
	if parts.scheme not in {"http", "https"} or not parts.netloc:
		return None
	if parts.username is not None or parts.password is not None:
		return None
	if "\\" in parts.netloc or parts.netloc.endswith(":"):
		return None
	try:
		hostname = parts.hostname
		port = parts.port
	except ValueError:
		return None
	if not hostname or "%" in hostname or port == 0:
		return None
	try:
		hostname = hostname.encode("idna").decode("ascii").lower()
	except UnicodeError:
		return None
	return parts.scheme, hostname, port if port is not None else (443 if parts.scheme == "https" else 80)


def _has_absolute_host(host_name: str) -> bool:
	return _normalized_http_origin(host_name) is not None
