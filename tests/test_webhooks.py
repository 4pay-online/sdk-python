import hashlib
import hmac
import json

import pytest

from fourpay import EventDeduplicator, WebhookVerificationError, verify_webhook

SECRET = "whsec_test"


def delivery(secret=SECRET, timestamp=1788434490):
    envelope = {
        "event_id": "4d9d0e6c-1d0a-4a2e-9a2f-0c9a1b0e8f21",
        "timestamp": timestamp,
        "type": "transaction.charged",
        "data": {"id": "tx-1", "txid": "order-1", "type": "payment", "status": "charged", "amount": "1000"},
    }
    body = json.dumps(envelope)
    signature = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return body, {
        "x-webhook-event-id": envelope["event_id"],
        "x-webhook-timestamp": str(timestamp),
        "x-webhook-signature": signature,
    }


def test_a_genuine_delivery_verifies():
    body, headers = delivery()
    event = verify_webhook(body, headers, SECRET, now=1788434500)
    assert event["data"]["status"] == "charged"


def test_re_serialising_the_body_breaks_the_signature():
    body, headers = delivery()
    reserialised = json.dumps(json.loads(body), indent=2)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(reserialised, headers, SECRET, now=1788434500)


def test_a_forged_signature_is_refused():
    body, headers = delivery()
    headers["x-webhook-signature"] = "0" * 64
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify_webhook(body, headers, SECRET, now=1788434500)


def test_a_delivery_signed_with_another_secret_is_refused():
    body, headers = delivery(secret="whsec_other")
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify_webhook(body, headers, SECRET, now=1788434500)


def test_an_old_delivery_is_refused_as_a_replay():
    body, headers = delivery()
    with pytest.raises(WebhookVerificationError, match="replay refused"):
        verify_webhook(body, headers, SECRET, now=1788434490 + 3600)


def test_an_unsigned_delivery_is_refused_not_trusted():
    body, _ = delivery()
    with pytest.raises(WebhookVerificationError, match="no signature headers"):
        verify_webhook(body, {}, SECRET)


def test_header_case_does_not_matter():
    body, headers = delivery()
    upper = {k.upper(): v for k, v in headers.items()}
    assert verify_webhook(body, upper, SECRET, now=1788434500)["type"] == "transaction.charged"


def test_bytes_bodies_work_too():
    body, headers = delivery()
    assert verify_webhook(body.encode(), headers, SECRET, now=1788434500)


def test_the_same_event_id_is_accepted_once():
    dedupe = EventDeduplicator()
    assert dedupe.accept("evt-1") is True
    assert dedupe.accept("evt-1") is False
    assert dedupe.accept("evt-2") is True
