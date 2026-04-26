# Copyright (c) 2026, Goodvantage GmbH and contributors

from urllib.parse import urlencode

from frappe.integrations.utils import make_get_request, make_post_request

BASE_URL = "https://api.payrexx.com"


class PayrexxAPIError(RuntimeError):
	pass


class PayrexxClient:
	"""Thin wrapper over the Payrexx v1.x REST API.

	Auth: API secret is sent in the 'x-api-key' header (current Payrexx scheme,
	per the official PHP SDK in payrexx/payrexx-php). The legacy ApiSignature
	body field is no longer required.
	"""

	def __init__(self, instance: str, api_secret: str, api_version: str = "v1.14"):
		if not instance:
			raise ValueError("Payrexx instance name is required")
		if not api_secret:
			raise ValueError("Payrexx API secret is required")
		self.instance = instance
		self.api_secret = api_secret
		self.version = api_version

	# ----------------------------------------------------------------- Gateway

	def create_gateway(self, params: dict) -> dict:
		"""POST /Gateway/  ->  dict with id, link, hash, status, ..."""
		return _unwrap(
			make_post_request(
				url=self._url("Gateway/"),
				data=params,
				headers={
					**self._headers(),
					"Content-Type": "application/x-www-form-urlencoded",
				},
			)
		)

	def retrieve_gateway(self, gateway_id: int) -> dict:
		"""GET /Gateway/{id}/"""
		return _unwrap(
			make_get_request(
				url=self._url(f"Gateway/{gateway_id}/"),
				headers=self._headers(),
			)
		)

	def delete_gateway(self, gateway_id: int) -> dict:
		# DELETE not exposed by frappe.integrations.utils; use a plain request
		# only when needed. Stub here for completeness.
		raise NotImplementedError("Delete via Payrexx dashboard or extend with a DELETE call.")

	# ------------------------------------------------------------- Transaction

	def retrieve_transaction(self, transaction_id: int) -> dict:
		"""GET /Transaction/{id}/"""
		return _unwrap(
			make_get_request(
				url=self._url(f"Transaction/{transaction_id}/"),
				headers=self._headers(),
			)
		)

	# ----------------------------------------------------------------- internal

	def _url(self, path: str, query: dict | None = None) -> str:
		q = {"instance": self.instance}
		if query:
			q.update(query)
		return f"{BASE_URL}/{self.version}/{path.lstrip('/')}?{urlencode(q)}"

	def _headers(self) -> dict:
		return {"x-api-key": self.api_secret, "Accept": "application/json"}


def _unwrap(resp: dict) -> dict:
	"""Payrexx wraps ok responses as {status: 'success', data: [obj]}."""
	if not resp or resp.get("status") != "success":
		raise PayrexxAPIError((resp or {}).get("message", "Unknown Payrexx error"))
	data = resp.get("data") or []
	return data[0] if data else {}
