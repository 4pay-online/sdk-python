"""Verifying a webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from typing import Any, Dict, Mapping, Optional, Union

from .errors import FourPayError


class WebhookVerificationError(FourPayError):
    """The delivery did not prove it came from the platform. Answer 401 and stop."""


def verify_webhook(
    raw_body: Union[str, bytes],
    headers: Mapping[str, str],
    secret: str,
    tolerance_seconds: int = 300,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Verify a webhook and return its envelope.

    The signature is HMAC-SHA256 over ``"<timestamp>.<raw body>"``, hex-encoded,
    keyed with the webhook secret your operator set on the terminal.

    Two things decide whether this works:

    * pass the **raw bytes exactly as received**. ``json.loads`` followed by
      ``json.dumps`` reorders and reformats, and the signature covers bytes.
      In Flask that is ``request.get_data()``, not ``request.json``;
    * the signature covers the **whole envelope**, not ``data``.

    :raises WebhookVerificationError: on any failure. Never fall through to
        processing — an unverified delivery is a stranger's instruction.
    """
    if not secret:
        raise WebhookVerificationError(
            "No webhook secret. Without one the platform sends no signature at all, and every "
            "delivery is unauthenticated — ask your operator to set one on the terminal."
        )

    lower = {k.lower(): v for k, v in headers.items()}
    signature = lower.get("x-webhook-signature")
    timestamp = lower.get("x-webhook-timestamp")

    if not signature or not timestamp:
        raise WebhookVerificationError(
            "Delivery carries no signature headers. Either the terminal has no webhook secret, "
            "or this request did not come from the platform."
        )

    body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
    expected = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise WebhookVerificationError("Webhook signature does not match.")

    current = time.time() if now is None else now
    try:
        sent = int(timestamp)
    except ValueError:
        raise WebhookVerificationError(f"Webhook timestamp {timestamp!r} is not a number.") from None

    if abs(current - sent) > tolerance_seconds:
        raise WebhookVerificationError(
            f"Webhook timestamp {timestamp} is outside the {tolerance_seconds}s window — replay refused."
        )

    envelope = json.loads(body)
    if not isinstance(envelope, dict) or "data" not in envelope:
        raise WebhookVerificationError("Verified body is not a webhook envelope.")
    return envelope


class EventDeduplicator:
    """Deduplication by ``event_id``, kept in memory.

    Deliveries repeat: a handler that timed out after doing the work still gets
    the event again. Deduplicate on ``event_id`` — never on the transaction id,
    since one transaction legitimately produces several events.

    In-memory is enough for one process. More than one instance, or anything
    that must survive a restart, needs the same check against shared storage.
    """

    def __init__(self, ttl_seconds: int = 86_400, max_entries: int = 10_000) -> None:
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_entries

    def accept(self, event_id: str) -> bool:
        """``True`` the first time this event id is seen, ``False`` on every repeat."""
        now = time.time()
        while self._seen:
            oldest_id, seen_at = next(iter(self._seen.items()))
            if now - seen_at <= self._ttl:
                break
            self._seen.pop(oldest_id)

        if event_id in self._seen:
            return False

        self._seen[event_id] = now
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True
