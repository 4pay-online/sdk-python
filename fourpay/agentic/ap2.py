"""AP2 — Agent Payments Protocol (merchant side).

AP2 answers a question a normal checkout never has to ask: *who authorised
this, and to what extent?* When a human clicks Pay, the click is the proof.
When an agent pays for them, the proof has to travel with the request — signed,
bounded and verifiable afterwards.

It travels as **mandates**: an *Intent Mandate* is standing permission ("buy
these shoes below 120 EUR"), a *Cart Mandate* is a signed itemised cart, and a
*Payment Mandate* is what the merchant forwards to the rail.

This module verifies what a shopping agent presents and turns it into a FourPay
payment. Verification is local — signature, audience, expiry, totals. The
mandate then rides in ``merchant_meta.ap2`` so the authority stays attached to
the transaction. FourPay does not forward a Payment Mandate to card networks as
an AP2 credential; no rail we route to accepts one today.

Signature checking needs ``cryptography`` (``pip install 4pay-sdk[crypto]``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "MandateVerificationError",
    "verify_cart_mandate",
    "assert_total_matches_items",
    "assert_within_intent",
    "cart_mandate_to_payment",
]


class MandateVerificationError(Exception):
    """A mandate that fails any check is not a mandate — it is a claim."""


def verify_cart_mandate(
    jws: str,
    keys: Mapping[str, Any],
    merchant_id: str,
    tolerance_seconds: int = 0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Verify a Cart Mandate presented as a compact JWS.

    Checks, in order: the header names a key we hold; the signature matches;
    the cart is addressed to us; it has not expired; the total equals the sum of
    its lines.

    The line-sum check is the one people leave out. Without it an agent can
    present three items worth 300 and a total of 3, and every signature in the
    chain still verifies — the issuer signed exactly what it was asked to.

    :param keys: public keys by ``kid``; each a JWK dict or a PEM string.
    """
    header, payload, signing_input, signature = _decode_jws(jws)

    kid = header.get("kid")
    if not kid:
        raise MandateVerificationError("JWS header carries no kid.")
    if kid not in keys:
        raise MandateVerificationError(
            f"No public key for kid {kid}. Refusing a mandate signed by an unknown issuer."
        )

    if not _verify_signature(header.get("alg", ""), keys[kid], signing_input, signature):
        raise MandateVerificationError("Mandate signature does not verify.")

    if payload.get("merchant_id") != merchant_id:
        raise MandateVerificationError(
            f"Mandate is addressed to merchant {payload.get('merchant_id')}, not {merchant_id}."
        )

    expires_at = _parse_iso(payload.get("expires_at"))
    if expires_at is None:
        raise MandateVerificationError(f"Mandate expires_at is not a date: {payload.get('expires_at')!r}")

    current = now or datetime.now(timezone.utc)
    if (expires_at - current).total_seconds() + tolerance_seconds < 0:
        raise MandateVerificationError(f"Mandate expired at {payload['expires_at']}.")

    assert_total_matches_items(payload)
    return payload


def assert_total_matches_items(mandate: Mapping[str, Any]) -> None:
    """Does the signed total agree with the signed lines?"""
    total = sum(int(item["price"]) * int(item["quantity"]) for item in mandate.get("items", []))
    if total != int(mandate["total"]):
        raise MandateVerificationError(
            f"Cart total {mandate['total']} does not match its lines ({total}). Refusing the cart."
        )


def assert_within_intent(
    cart: Mapping[str, Any], intent: Mapping[str, Any], now: Optional[datetime] = None
) -> None:
    """Is this cart within the standing permission the user gave?

    A Cart Mandate proves the *agent* signed a cart. Only the Intent Mandate
    proves the *user* allowed that kind of purchase at all.
    """
    current = now or datetime.now(timezone.utc)
    expires_at = _parse_iso(intent.get("expires_at"))
    if expires_at is None or expires_at < current:
        raise MandateVerificationError(f"Intent mandate {intent.get('id')} expired at {intent.get('expires_at')}.")

    constraints = intent.get("constraints") or {}
    currency = constraints.get("currency")
    if currency and currency != cart.get("currency"):
        raise MandateVerificationError(f"Intent covers {currency}; the cart is in {cart.get('currency')}.")

    max_amount = constraints.get("max_amount")
    if max_amount and int(cart["total"]) > int(max_amount):
        raise MandateVerificationError(
            f"Cart total {cart['total']} exceeds the {max_amount} the user authorised."
        )

    merchants = constraints.get("merchants")
    if merchants and cart.get("merchant_id") not in merchants:
        raise MandateVerificationError(
            f"Merchant {cart.get('merchant_id')} is not among those the user authorised."
        )


def cart_mandate_to_payment(
    cart: Mapping[str, Any],
    env: str,
    return_url: Optional[str] = None,
    fail_url: Optional[str] = None,
    modality: str = "human_present",
    jws: Optional[str] = None,
) -> Dict[str, Any]:
    """Turn a verified cart into keyword arguments for :meth:`FourPay.create_payment`.

    The cart id becomes ``txid``, which is what makes the payment idempotent: an
    agent that retries after a timeout gets the original transaction back
    instead of a second charge.
    """
    payment_mandate = {
        "id": f"pm_{cart['id']}",
        "cart_mandate_id": cart["id"],
        "amount": cart["total"],
        "currency": cart["currency"],
        "merchant_id": cart["merchant_id"],
        "modality": modality,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    params: Dict[str, Any] = {
        "type": "payment",
        "amount": cart["total"],
        "currency": cart["currency"],
        "txid": cart["id"],
        "env": env,
        "hosted": True,
        "description": _describe(cart),
        "merchant_meta": {
            "ap2": {
                "cart_mandate_id": cart["id"],
                "intent_mandate_id": cart.get("intent_mandate_id"),
                "user_id": cart.get("user_id"),
                "agent_id": cart.get("agent_id"),
                "payment_mandate": payment_mandate,
                # The hash, not the mandate: a JWS runs to kilobytes, and
                # merchant_meta rides along on every read and every webhook.
                "cart_mandate_sha256": hashlib.sha256(jws.encode()).hexdigest() if jws else None,
            }
        },
    }
    if return_url:
        params["returnUrl"] = return_url
    if fail_url:
        params["failUrl"] = fail_url
    return params


# ------------------------------------------------------------------- internals


def _describe(cart: Mapping[str, Any]) -> str:
    items = cart.get("items") or []
    if not items:
        return f"AP2 cart {cart['id']}"
    if len(items) == 1:
        return f"{items[0]['name']} x{items[0]['quantity']}"
    return f"{items[0]['name']} and {len(items) - 1} more"


def _decode_jws(jws: str):
    parts = jws.split(".")
    if len(parts) != 3:
        raise MandateVerificationError("Not a compact JWS: expected three dot-separated parts.")

    header = json.loads(_b64url(parts[0]))
    payload = json.loads(_b64url(parts[1]))
    return header, payload, f"{parts[0]}.{parts[1]}".encode(), _b64url(parts[2])


def _b64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _verify_signature(alg: str, key: Any, signing_input: bytes, signature: bytes) -> bool:
    """Verify one of the algorithms AP2 issuers actually use.

    ``none`` and the HMAC family are refused outright: a mandate is a bearer
    authorisation from a third party, and a symmetric key would mean the
    merchant could mint the user's permissions itself.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, utils
        from cryptography.exceptions import InvalidSignature
    except ImportError:  # pragma: no cover - depends on the install extras
        raise MandateVerificationError(
            "Verifying mandate signatures needs the cryptography package: "
            "pip install '4pay-sdk[crypto]'"
        ) from None

    if alg not in ("EdDSA", "ES256", "ES384", "RS256", "PS256"):
        raise MandateVerificationError(
            f"Refusing algorithm {alg}. Mandates must be signed with an asymmetric key "
            f"(EdDSA, ES256, ES384, RS256, PS256)."
        )

    public_key = (
        serialization.load_pem_public_key(key.encode() if isinstance(key, str) else key)
        if isinstance(key, (str, bytes))
        else _jwk_to_key(key)
    )

    try:
        if alg == "EdDSA":
            public_key.verify(signature, signing_input)
        elif alg in ("ES256", "ES384"):
            digest = hashes.SHA256() if alg == "ES256" else hashes.SHA384()
            size = len(signature) // 2
            r = int.from_bytes(signature[:size], "big")
            s = int.from_bytes(signature[size:], "big")
            public_key.verify(utils.encode_dss_signature(r, s), signing_input, ec.ECDSA(digest))
        elif alg == "RS256":
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:
            public_key.verify(
                signature,
                signing_input,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
    except InvalidSignature:
        return False
    return True


def _jwk_to_key(jwk: Mapping[str, Any]):
    """Build a public key from a JWK dict, for the curve families AP2 uses."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    kty = jwk.get("kty")
    if kty == "EC":
        curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1()}
        curve = curves.get(jwk.get("crv", ""))
        if curve is None:
            raise MandateVerificationError(f"Unsupported EC curve {jwk.get('crv')}.")
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64url(jwk["x"]), "big"),
            int.from_bytes(_b64url(jwk["y"]), "big"),
            curve,
        ).public_key()
    if kty == "OKP":
        return ed25519.Ed25519PublicKey.from_public_bytes(_b64url(jwk["x"]))
    if kty == "RSA":
        return rsa.RSAPublicNumbers(
            int.from_bytes(_b64url(jwk["e"]), "big"),
            int.from_bytes(_b64url(jwk["n"]), "big"),
        ).public_key()
    raise MandateVerificationError(f"Unsupported key type {kty}.")
