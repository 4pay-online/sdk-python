"""Money is integer minor units, always.

The API speaks decimal strings — ``"1000"`` is 10.00 in a two-decimal currency
— because an 18-decimal asset does not fit a JSON number. Nothing here goes
through ``float``: money and binary fractions do not mix.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Union

Amount = Union[str, int]

_DECIMALS = {
    "USD": 2, "EUR": 2, "GBP": 2, "RUB": 2, "KZT": 2, "AED": 2, "TRY": 2,
    "CNY": 2, "INR": 2, "BRL": 2,
    "JPY": 0, "KRW": 0, "VND": 0, "IDR": 0,
    "KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "TND": 3,
    "USDT": 6, "USDC": 6, "TRX": 6, "ZOLT": 6,
    "BTC": 8,
    "ETH": 18,
}


def decimals_for(currency: str) -> int:
    """How many minor units make one major unit.

    Raises for an unknown currency rather than assuming two: a guessed scale
    overstates JPY a hundredfold and ETH by sixteen orders of magnitude.
    """
    try:
        return _DECIMALS[currency.upper()]
    except KeyError:
        raise ValueError(
            f"Unknown minor-unit scale for currency {currency}. "
            f"Pass decimals explicitly instead of letting the SDK guess."
        ) from None


def to_minor_units(value: Amount, currency: str, decimals: int = None) -> str:
    """``"10.00"`` or ``1000`` to minor units as the API wants them.

    A ``float`` is refused outright, and a decimal string with more places than
    the currency has is refused too — rounding money quietly is how ledgers
    drift.
    """
    if isinstance(value, float):
        raise TypeError(
            "Refusing a float amount. Pass a decimal string like '10.50', or an integer "
            "count of minor units."
        )
    if decimals is None:
        decimals = decimals_for(currency)
    if isinstance(value, int):
        return str(value)

    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return str(int(text))

    amount = Decimal(text)
    _, digits, exponent = amount.as_tuple()
    if -exponent > decimals:
        raise ValueError(
            f"{value} has {-exponent} decimal places but {currency} has {decimals}. "
            f"Rounding money silently is not this SDK's job."
        )
    return str(int(amount.scaleb(decimals)))


def from_minor_units(minor: Amount, currency: str, decimals: int = None) -> str:
    """Minor units to a display string with the currency's own number of decimals."""
    if decimals is None:
        decimals = decimals_for(currency)
    value = Decimal(int(str(minor).strip())).scaleb(-decimals)
    return f"{value:.{decimals}f}"


def format_amount(minor: Amount, currency: str, decimals: int = None) -> str:
    """``"10.00 USD"``."""
    return f"{from_minor_units(minor, currency, decimals)} {currency.upper()}"
