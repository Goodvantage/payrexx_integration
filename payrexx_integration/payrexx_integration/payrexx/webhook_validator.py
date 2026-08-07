# Copyright (c) 2026, Goodvantage GmbH and contributors

import base64
import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, received_signature: str, signing_key: str) -> bool:
	"""Verify the X-Webhook-Signature header on a Payrexx webhook.

	Payrexx signs the raw request body with HMAC-SHA256 using the per-webhook
	signing key configured in the Payrexx dashboard (separate from the API
	secret), and delivers the digest in the ``X-Webhook-Signature`` header.

	The payload for the digest is the raw request body bytes — do NOT
	re-serialise the JSON before hashing.

	Payrexx's documented encoding is lowercase hexadecimal. Base64 remains an
	accepted compatibility form for previously observed/account-configured
	deliveries; both compare in constant time.
	"""
	if not received_signature or not signing_key:
		return False

	# Surrounding whitespace is a transport artefact, not part of the digest.
	candidate = received_signature.strip()
	if not candidate:
		return False

	digest = hmac.new(
		key=signing_key.encode("utf-8"),
		msg=raw_body or b"",
		digestmod=hashlib.sha256,
	).digest()

	# Decode the documented hex representation to bytes, so an uppercase
	# hex is not mistaken for a forgery. Decoding only ever touches the
	# caller-supplied value, so it reveals nothing about the expected digest;
	# the comparison itself stays constant-time.
	try:
		received_digest = bytes.fromhex(candidate)
	except ValueError:
		received_digest = b""
	if hmac.compare_digest(digest, received_digest):
		return True

	expected_b64 = base64.b64encode(digest).decode("ascii")
	return hmac.compare_digest(expected_b64, candidate)
