# Copyright (c) 2026, Goodvantage GmbH and contributors

import base64
import hashlib
import hmac


def verify_webhook_signature(
	raw_body: bytes, received_signature: str, signing_key: str
) -> bool:
	"""Verify the X-Webhook-Signature header on a Payrexx webhook.

	Payrexx signs the raw request body with HMAC-SHA256 using the per-webhook
	signing key configured in the Payrexx dashboard (separate from the API
	secret), and delivers the digest in the ``X-Webhook-Signature`` header.

	The payload for the digest is the raw request body bytes — do NOT
	re-serialise the JSON before hashing.

	The default expected encoding is base64. Some Payrexx accounts deliver hex
	instead; if base64 verification fails consistently in production, swap to
	the hex branch (see commented code below).
	"""
	if not received_signature or not signing_key:
		return False

	digest = hmac.new(
		key=signing_key.encode("utf-8"),
		msg=raw_body or b"",
		digestmod=hashlib.sha256,
	).digest()

	expected_b64 = base64.b64encode(digest).decode("ascii")
	if hmac.compare_digest(expected_b64, received_signature):
		return True

	# Fallback: some accounts deliver the signature as a lowercase hex string.
	expected_hex = digest.hex()
	return hmac.compare_digest(expected_hex, received_signature)
