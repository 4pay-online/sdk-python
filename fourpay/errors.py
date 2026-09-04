"""Errors the SDK raises, and the two error body shapes the platform sends."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class FourPayError(Exception):
    """Base class for everything this SDK raises."""


class FourPayApiError(FourPayError):
    """The platform answered, and the answer was an error.

    Two body shapes exist in the wild: the current
    ``{"errors": [{"code": ..., "detail": ...}]}`` and, on a few older
    endpoints, ``{"error": ..., "message": ...}``. Both are folded into
    :attr:`details`.
    """

    def __init__(
        self,
        status: int,
        details: List[Dict[str, str]],
        body: Any = None,
        request_id: Optional[str] = None,
    ) -> None:
        summary = "; ".join(d.get("detail", "") for d in details if d.get("detail")) or f"HTTP {status}"
        super().__init__(summary)
        self.status = status
        self.details = details
        self.body = body
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """Rate limits and platform faults are worth another try; your own bad request is not."""
        return self.status == 429 or self.status >= 500


class FourPayValidationError(FourPayApiError):
    """``422`` — one detail per rejected field."""


class FourPayAuthError(FourPayApiError):
    """``401``, or the bare ``400`` returned when ``x-organization-id`` is missing.

    The platform resolves an API key *inside* the organization the header
    names, so a key on its own is not a credential.
    """


class FourPayRateLimitError(FourPayApiError):
    """``429``. :attr:`retry_after` is seconds when the platform names one."""

    def __init__(self, status, details, body=None, retry_after=None, request_id=None):
        super().__init__(status, details, body, request_id)
        self.retry_after = retry_after


class TransactionRejectedError(FourPayError):
    """The request succeeded and the payment did not.

    ``POST /transactions`` answers ``201 Created`` even when routing, limits or
    anti-fraud refused the payment — the resource exists, it is simply
    ``rejected``. Code that checks only the HTTP status books unpaid orders as
    paid, so the client raises this instead.
    """

    def __init__(self, transaction: Dict[str, Any]) -> None:
        reason = transaction.get("error_description") or "no reason given"
        super().__init__(f"Transaction {transaction.get('id')} was {transaction.get('status')}: {reason}")
        self.transaction = transaction

    @property
    def reason(self) -> str:
        return self.transaction.get("error_description") or ""

    @property
    def is_routing_failure(self) -> bool:
        """No terminal matched. Nothing the payer does will fix it."""
        reason = self.reason.lower()
        return "terminal not found" in reason or "routing" in reason


class PaymentUrlUnavailableError(FourPayError):
    """Created, not refused, but with no hosted-page URL — read the status."""

    def __init__(self, transaction: Dict[str, Any]) -> None:
        super().__init__(
            f"Transaction {transaction.get('id')} is {transaction.get('status')} but carries no "
            f"widget_url. Use create_payment() when a payment can complete without a hosted page."
        )
        self.transaction = transaction


class FourPayConnectionError(FourPayError):
    """Network failure, DNS, TLS, or the request outlived its timeout."""


class FourPayTimeoutError(FourPayError):
    """A wait helper gave up before the transaction reached a final status."""

    def __init__(self, message: str, last_transaction: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.last_transaction = last_transaction


def parse_error_body(body: Any) -> List[Dict[str, str]]:
    """Fold either error body shape into a flat list of details."""
    if not isinstance(body, dict):
        return []

    errors = body.get("errors")
    if isinstance(errors, list):
        out = []
        for item in errors:
            if isinstance(item, str):
                out.append({"detail": item})
                continue
            if not isinstance(item, dict):
                continue
            detail = {"detail": str(item.get("detail") or item.get("title") or item.get("message") or "unknown error")}
            if item.get("code") is not None:
                detail["code"] = str(item["code"])
            source = item.get("source")
            if isinstance(source, dict) and source.get("pointer"):
                detail["pointer"] = str(source["pointer"])
            out.append(detail)
        return out

    if isinstance(body.get("error"), str) or isinstance(body.get("message"), str):
        detail = {"detail": str(body.get("message") or body.get("error"))}
        if isinstance(body.get("error"), str):
            detail["code"] = body["error"]
        return [detail]

    return []


def error_from_response(status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> FourPayApiError:
    """Pick the class that matches the status, so ``except`` clauses can discriminate."""
    details = parse_error_body(body)
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    request_id = headers.get("x-request-id")

    if status == 429:
        retry_after = headers.get("retry-after")
        return FourPayRateLimitError(
            status, details, body, int(retry_after) if retry_after and retry_after.isdigit() else None, request_id
        )
    if status in (401, 403):
        return FourPayAuthError(status, details, body, request_id)
    if status == 422:
        return FourPayValidationError(status, details, body, request_id)
    # A bare 400 with no field pointers is what a missing x-organization-id
    # looks like. Treat it as the credential problem it is.
    if status == 400 and all(not d.get("pointer") for d in details):
        return FourPayAuthError(status, details, body, request_id)
    return FourPayApiError(status, details, body, request_id)
