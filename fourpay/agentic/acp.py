"""ACP — Agentic Commerce Protocol (merchant side).

ACP is the shape of a checkout that happens inside someone else's chat. The
buyer never reaches your site: an agent reads your product feed, builds a cart,
and drives a checkout session against **your** endpoints::

    POST /checkout_sessions               create — price it, quote tax and shipping
    POST /checkout_sessions/:id           update — address or quantity changed
    POST /checkout_sessions/:id/complete  pay
    POST /checkout_sessions/:id/cancel    cancel

:class:`AcpCheckoutBridge` implements those four against FourPay.

**Where the delegated token stands.** ACP's delegated-payments flow has the
agent hand you a single-use token minted by the buyer's payment provider.
FourPay does not accept third-party vault tokens today, so this bridge offers
the two honest options: ``hosted`` returns a payment URL for the agent to pass
back, and ``saved_card`` charges a card already stored in the platform.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..errors import TransactionRejectedError

__all__ = ["AcpError", "AcpCheckoutBridge", "InMemorySessionStore"]


class AcpError(Exception):
    """An error in the shape ACP clients expect, with the status to answer with."""

    def __init__(self, message: str, code: str, http_status: int = 400, type_: str = "invalid_request"):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.type = type_

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "code": self.code, "message": str(self)}


class InMemorySessionStore:
    """Sessions for one process. Swap for your database in production."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)

    def set(self, session_id: str, session: Dict[str, Any]) -> None:
        self._sessions[session_id] = session


class AcpCheckoutBridge:
    """The merchant half of an ACP checkout, backed by FourPay.

    Wire the four methods to the four routes. Prices come from your
    ``price_item`` callback and never from the agent's request — an agent that
    could name its own price would.
    """

    def __init__(
        self,
        client,
        env: str,
        currency: str,
        price_item: Callable[[str, int], str],
        calculate_tax: Optional[Callable[[Dict[str, Any]], str]] = None,
        calculate_fulfillment: Optional[Callable[[Dict[str, Any]], str]] = None,
        mode: str = "hosted",
        return_url: Optional[str] = None,
        fail_url: Optional[str] = None,
        store: Optional[InMemorySessionStore] = None,
        generate_id: Optional[Callable[[], str]] = None,
    ) -> None:
        self.client = client
        self.env = env
        self.currency = currency
        self.price_item = price_item
        self.calculate_tax = calculate_tax
        self.calculate_fulfillment = calculate_fulfillment
        self.mode = mode
        self.return_url = return_url
        self.fail_url = fail_url
        self.store = store or InMemorySessionStore()
        self.generate_id = generate_id or (lambda: str(uuid.uuid4()))

    # ------------------------------------------------------------ the four calls

    def create(self, items: List[Mapping[str, Any]], buyer=None, fulfillment_address=None) -> Dict[str, Any]:
        """``POST /checkout_sessions``"""
        if not items:
            raise AcpError("A checkout session needs at least one line item.", "missing_items", 422)

        session = {
            "id": self.generate_id(),
            "status": "not_ready_for_payment",
            "currency": self.currency,
            "line_items": [],
            "totals": [],
            "messages": [],
            "links": [],
        }
        if buyer:
            session["buyer"] = dict(buyer)
        if fulfillment_address:
            session["fulfillment_address"] = dict(fulfillment_address)

        self._price(session, items)
        self.store.set(session["id"], session)
        return session

    def update(self, session_id: str, items=None, buyer=None, fulfillment_address=None) -> Dict[str, Any]:
        """``POST /checkout_sessions/:id`` — repricing after the agent changes something."""
        session = self._require(session_id)
        if session["status"] in ("completed", "canceled"):
            raise AcpError(f"Session {session_id} is {session['status']}.", "session_not_updatable", 409)

        if buyer:
            session["buyer"] = {**session.get("buyer", {}), **dict(buyer)}
        if fulfillment_address:
            session["fulfillment_address"] = dict(fulfillment_address)

        if items is not None:
            self._price(session, items)
        else:
            self._recompute_totals(session)

        self.store.set(session_id, session)
        return session

    def complete(self, session_id: str, payment_data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """``POST /checkout_sessions/:id/complete`` — turn the session into a payment.

        The session id becomes the transaction's ``txid``, so an agent that
        retries a timed-out complete gets the same payment back.
        """
        session = self._require(session_id)

        if session["status"] == "completed":
            return session  # idempotent by design
        if session["status"] == "canceled":
            raise AcpError(f"Session {session_id} was canceled.", "session_canceled", 409)
        if session["status"] != "ready_for_payment":
            raise AcpError(
                f"Session {session_id} is {session['status']} — price and address must settle first.",
                "session_not_ready",
                409,
            )

        total = _total_of(session)
        session["status"] = "in_progress"
        self.store.set(session_id, session)

        try:
            transaction = self._charge(session, total, payment_data or {})
        except Exception as error:
            session["status"] = "ready_for_payment"
            session["messages"].append({
                "type": "error",
                "content_type": "plain",
                "content": (
                    f"Payment refused: {error.reason}"
                    if isinstance(error, TransactionRejectedError)
                    else f"Payment could not be created: {error}"
                ),
            })
            self.store.set(session_id, session)
            raise

        session["order"] = {"id": transaction["id"], "checkout_session_id": session_id}
        url = transaction.get("widget_url")
        if url:
            session["order"]["permalink_url"] = url
            session["links"] = [{"type": "payment", "url": url}]
            session["messages"].append(
                {"type": "info", "content_type": "plain", "content": "Open the payment link to finish paying."}
            )
            # Not `completed`: the buyer has not paid yet, and saying otherwise
            # is how an agent tells someone their order is placed when it isn't.
            session["status"] = "in_progress"
        else:
            session["status"] = "completed" if transaction.get("status") == "charged" else "in_progress"

        self.store.set(session_id, session)
        return session

    def cancel(self, session_id: str) -> Dict[str, Any]:
        """``POST /checkout_sessions/:id/cancel``"""
        session = self._require(session_id)
        if session["status"] == "completed":
            raise AcpError(f"Session {session_id} is already completed.", "session_completed", 409)
        session["status"] = "canceled"
        self.store.set(session_id, session)
        return session

    def get(self, session_id: str) -> Dict[str, Any]:
        """``GET /checkout_sessions/:id``"""
        return self._require(session_id)

    # ---------------------------------------------------------------- internals

    def _charge(self, session: Dict[str, Any], total: str, payment_data: Mapping[str, Any]):
        base = {
            "amount": total,
            "currency": session["currency"],
            "txid": session["id"],
            "env": self.env,
            "description": _describe(session),
            "merchant_meta": {
                "acp": {
                    "checkout_session_id": session["id"],
                    "line_items": [
                        {"item_id": line["item_id"], "quantity": line["quantity"]}
                        for line in session["line_items"]
                    ],
                    "payment_provider": payment_data.get("provider"),
                }
            },
        }
        buyer_email = (session.get("buyer") or {}).get("email")
        if buyer_email:
            base["customer"] = {"email": buyer_email}

        if self.mode == "saved_card":
            token = payment_data.get("token")
            if not token:
                raise AcpError(
                    "saved_card mode needs the token of a card already stored in the platform.",
                    "missing_payment_token",
                    422,
                )
            return self.client.create_payment(**base, money_storage={"token": token})

        # create_payment rather than create_hosted_payment: a terminal set to
        # charge instantly settles without ever issuing a page.
        return self.client.create_payment(
            **base, hosted=True, returnUrl=self.return_url, failUrl=self.fail_url
        )

    def _price(self, session: Dict[str, Any], items) -> None:
        lines = []
        for item in items:
            quantity = item["quantity"]
            if not isinstance(quantity, int) or quantity < 1:
                raise AcpError(
                    f"Quantity for {item['id']} must be a positive integer.", "invalid_quantity", 422
                )
            lines.append({
                "id": f"li_{item['id']}",
                "item_id": item["id"],
                "quantity": quantity,
                "base_amount": str(self.price_item(item["id"], quantity)),
            })

        session["line_items"] = lines
        self._recompute_totals(session)

    def _recompute_totals(self, session: Dict[str, Any]) -> None:
        items_total = sum(int(line["base_amount"]) for line in session["line_items"])
        tax = int(self.calculate_tax(session)) if self.calculate_tax else 0
        fulfillment = int(self.calculate_fulfillment(session)) if self.calculate_fulfillment else 0

        totals = [{"type": "items_base_amount", "display_text": "Items", "amount": str(items_total)}]
        if tax:
            totals.append({"type": "tax", "display_text": "Tax", "amount": str(tax)})
        if fulfillment:
            totals.append({"type": "fulfillment", "display_text": "Shipping", "amount": str(fulfillment)})
        totals.append({"type": "total", "display_text": "Total", "amount": str(items_total + tax + fulfillment)})

        session["totals"] = totals
        if session["status"] in ("not_ready_for_payment", "ready_for_payment"):
            session["status"] = "ready_for_payment" if self._is_ready(session) else "not_ready_for_payment"

    def _is_ready(self, session: Dict[str, Any]) -> bool:
        """Ready means priced and, when shipping is charged, addressed."""
        if not session["line_items"]:
            return False
        if self.calculate_fulfillment and not session.get("fulfillment_address"):
            return False
        return int(_total_of(session)) > 0

    def _require(self, session_id: str) -> Dict[str, Any]:
        session = self.store.get(session_id)
        if session is None:
            raise AcpError(f"No checkout session {session_id}.", "session_not_found", 404)
        return session


def _total_of(session: Mapping[str, Any]) -> str:
    for total in session.get("totals", []):
        if total["type"] == "total":
            return total["amount"]
    return "0"


def _describe(session: Mapping[str, Any]) -> str:
    count = sum(line["quantity"] for line in session.get("line_items", []))
    return f"ACP checkout {session['id']} ({count} item{'' if count == 1 else 's'})"
