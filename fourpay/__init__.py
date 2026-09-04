"""FourPay / 4pay.online SDK.

::

    from fourpay import FourPay

    client = FourPay(
        api_key=os.environ["FOURPAY_API_KEY"],
        organization_id=os.environ["FOURPAY_ORGANIZATION_ID"],
        base_url="https://sandbox.4pay.online",
    )
"""

from . import agentic, sandbox  # noqa: F401
from .client import FourPay, FINAL_STATUSES, is_final_status, is_settled
from .dates import from_unix_seconds, parse_timestamp
from .errors import (
    PaymentUrlUnavailableError,
    FourPayApiError,
    FourPayAuthError,
    FourPayConnectionError,
    FourPayError,
    FourPayRateLimitError,
    FourPayTimeoutError,
    FourPayValidationError,
    TransactionRejectedError,
)
from .money import decimals_for, format_amount, from_minor_units, to_minor_units
from .webhooks import EventDeduplicator, WebhookVerificationError, verify_webhook

__version__ = "0.1.0"

__all__ = [
    "FourPay",
    "FINAL_STATUSES",
    "is_final_status",
    "is_settled",
    "parse_timestamp",
    "from_unix_seconds",
    "to_minor_units",
    "from_minor_units",
    "format_amount",
    "decimals_for",
    "verify_webhook",
    "WebhookVerificationError",
    "EventDeduplicator",
    "FourPayError",
    "FourPayApiError",
    "FourPayAuthError",
    "FourPayValidationError",
    "FourPayRateLimitError",
    "FourPayConnectionError",
    "FourPayTimeoutError",
    "TransactionRejectedError",
    "PaymentUrlUnavailableError",
    "sandbox",
    "agentic",
]
