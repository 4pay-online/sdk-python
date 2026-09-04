import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives import hashes

from fourpay.agentic.a2a import A2APaymentAgent
from fourpay.agentic.acp import AcpCheckoutBridge
from fourpay.agentic.ap2 import (
    MandateVerificationError,
    assert_within_intent,
    cart_mandate_to_payment,
    verify_cart_mandate,
)
from fourpay.agentic.tools import execute_payment_tool, payment_tools

from conftest import CREATED

PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()
JWK = {
    "kty": "EC",
    "crv": "P-256",
    "x": base64.urlsafe_b64encode(PUBLIC_NUMBERS.x.to_bytes(32, "big")).rstrip(b"=").decode(),
    "y": base64.urlsafe_b64encode(PUBLIC_NUMBERS.y.to_bytes(32, "big")).rstrip(b"=").decode(),
}
KEYS = {"issuer-1": JWK}


def sign_mandate(payload, header=None):
    header = header or {"alg": "ES256", "kid": "issuer-1"}
    encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    signing_input = f"{encode(header)}.{encode(payload)}"
    der = PRIVATE_KEY.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}"


def cart(**overrides):
    base = {
        "id": "cart-1",
        "merchant_id": "merchant-1",
        "items": [{"name": "Runner", "quantity": 2, "price": "6000"}],
        "total": "12000",
        "currency": "EUR",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "user_id": "user-1",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------- AP2


def test_a_properly_signed_cart_mandate_verifies():
    assert verify_cart_mandate(sign_mandate(cart()), KEYS, "merchant-1")["id"] == "cart-1"


def test_a_tampered_payload_no_longer_verifies():
    header, _, signature = sign_mandate(cart()).split(".")
    forged = base64.urlsafe_b64encode(json.dumps(cart(total="1")).encode()).rstrip(b"=").decode()
    with pytest.raises(MandateVerificationError, match="does not verify"):
        verify_cart_mandate(f"{header}.{forged}.{signature}", KEYS, "merchant-1")


def test_a_mandate_from_an_unknown_issuer_is_refused():
    jws = sign_mandate(cart(), {"alg": "ES256", "kid": "issuer-unknown"})
    with pytest.raises(MandateVerificationError, match="No public key for kid"):
        verify_cart_mandate(jws, KEYS, "merchant-1")


def test_an_hmac_signed_mandate_is_refused():
    jws = sign_mandate(cart(), {"alg": "HS256", "kid": "issuer-1"})
    with pytest.raises(MandateVerificationError, match="Refusing algorithm HS256"):
        verify_cart_mandate(jws, KEYS, "merchant-1")


def test_a_cart_addressed_to_another_merchant_is_refused():
    with pytest.raises(MandateVerificationError, match="addressed to merchant"):
        verify_cart_mandate(sign_mandate(cart(merchant_id="someone-else")), KEYS, "merchant-1")


def test_an_expired_mandate_is_refused():
    expired = cart(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    with pytest.raises(MandateVerificationError, match="expired"):
        verify_cart_mandate(sign_mandate(expired), KEYS, "merchant-1")


def test_a_signed_total_that_disagrees_with_its_lines_is_refused():
    with pytest.raises(MandateVerificationError, match="does not match its lines"):
        verify_cart_mandate(sign_mandate(cart(total="3")), KEYS, "merchant-1")


def test_an_intent_mandate_caps_what_the_agent_may_spend():
    intent = {
        "id": "intent-1",
        "constraints": {"max_amount": "10000", "currency": "EUR"},
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    with pytest.raises(MandateVerificationError, match="exceeds"):
        assert_within_intent(cart(), intent)

    smaller = cart(total="6000", items=[{"name": "Runner", "quantity": 1, "price": "6000"}])
    assert_within_intent(smaller, intent)


def test_a_verified_cart_becomes_an_idempotent_payment_carrying_its_authority():
    params = cart_mandate_to_payment(cart(), env="test")
    assert params["txid"] == "cart-1"
    assert params["hosted"] is True
    assert params["merchant_meta"]["ap2"]["payment_mandate"]["cart_mandate_id"] == "cart-1"


# ------------------------------------------------------------------------- ACP


def test_an_acp_session_prices_becomes_payable_and_produces_a_payment(stub):
    client, transport = stub(lambda n: {"status": 201, "body": {**CREATED, "txid": "s-1"}})
    bridge = AcpCheckoutBridge(
        client=client, env="test", currency="EUR",
        price_item=lambda item_id, qty: str(2000 * qty), generate_id=lambda: "s-1",
    )

    session = bridge.create(items=[{"id": "sku-1", "quantity": 2}])
    assert session["status"] == "ready_for_payment"
    assert session["totals"][-1]["amount"] == "4000"

    session = bridge.complete("s-1")
    assert session["order"]["id"] == "tx-1"
    assert transport.calls[0]["body"]["params"]["txid"] == "s-1"
    assert session["status"] != "completed"


def test_prices_come_from_the_catalogue_never_from_the_agents_request(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    bridge = AcpCheckoutBridge(
        client=client, env="test", currency="EUR",
        price_item=lambda item_id, qty: "2000", generate_id=lambda: "s-1",
    )
    bridge.create(items=[{"id": "sku-1", "quantity": 1, "base_amount": "1"}])
    bridge.complete("s-1")

    assert transport.calls[0]["body"]["params"]["amount"] == "2000"


def test_completing_twice_does_not_pay_twice(stub):
    client, transport = stub(
        lambda n: {"status": 201, "body": {**CREATED, "status": "charged", "widget_url": None}}
    )
    bridge = AcpCheckoutBridge(
        client=client, env="test", currency="EUR",
        price_item=lambda item_id, qty: "2000", generate_id=lambda: "s-1",
    )
    bridge.create(items=[{"id": "sku-1", "quantity": 1}])
    bridge.complete("s-1")
    bridge.complete("s-1")

    assert len(transport.calls) == 1


# ------------------------------------------------------------------------- A2A


def test_the_agent_card_advertises_the_payment_skills(stub):
    client, _ = stub(lambda n: {"body": CREATED})
    agent = A2APaymentAgent(client=client, env="test", url="https://agent.example/a2a")
    assert sorted(s["id"] for s in agent.agent_card["skills"]) == [
        "create_payment", "payment_status", "refund_payment",
    ]


def test_message_send_produces_a_task_carrying_a_payment_link(stub):
    client, _ = stub(lambda n: {"status": 201, "body": CREATED})
    agent = A2APaymentAgent(client=client, env="test", url="https://agent.example/a2a")

    response = agent.handle({
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m-1", "parts": [
            {"kind": "data", "data": {"skill": "create_payment", "amount": "4000",
                                      "currency": "EUR", "order_id": "s-1"}},
        ]}},
    })

    assert response["result"]["status"]["state"] == "completed"
    assert response["result"]["artifacts"][0]["parts"][1]["data"]["payment_url"] == CREATED["widget_url"]


def test_a_payment_above_the_ceiling_fails_the_task_instead_of_charging(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    agent = A2APaymentAgent(client=client, env="test", url="https://a.example/a2a", max_amount="1000")

    response = agent.handle({
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m-1", "parts": [
            {"kind": "data", "data": {"skill": "create_payment", "amount": "500000", "currency": "EUR"}},
        ]}},
    })

    assert response["result"]["status"]["state"] == "failed"
    assert transport.calls == []


# ------------------------------------------------------------------ LLM tools


def test_refunds_are_absent_unless_explicitly_enabled():
    assert not any(t["name"] == "refund_payment" for t in payment_tools())
    assert any(t["name"] == "refund_payment" for t in payment_tools(allow_refunds=True))


def test_a_decimal_amount_from_a_model_is_rejected_before_it_reaches_the_platform(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    result = execute_payment_tool(
        client, "create_payment", {"amount": "40.00", "order_id": "s-1"}, env="test", currency="EUR"
    )

    assert result["ok"] is False
    assert "minor units" in result["content"]
    assert transport.calls == []


def test_the_ceiling_is_enforced_where_a_prompt_cannot_argue_with_it(stub):
    client, transport = stub(lambda n: {"status": 201, "body": CREATED})
    result = execute_payment_tool(
        client, "create_payment", {"amount": "999999", "order_id": "s-1"},
        env="test", currency="EUR", max_amount="10000",
    )

    assert result["ok"] is False
    assert "ceiling" in result["content"]
    assert transport.calls == []


def test_a_refused_payment_comes_back_as_a_sentence_the_model_can_act_on(stub):
    client, _ = stub(lambda n: {
        "status": 201,
        "body": {**CREATED, "status": "rejected", "error_description": "terminal not found"},
    })
    result = execute_payment_tool(
        client, "create_payment", {"amount": "4000", "order_id": "s-1"}, env="test", currency="EUR"
    )

    assert result["ok"] is False
    assert "No terminal is configured" in result["content"]
