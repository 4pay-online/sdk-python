"""The platform stamps times as ``"2026-09-03 11:18:30.101839 Etc/UTC"``.

That is not ISO 8601: a space where ``T`` belongs, six fractional digits, and a
named zone. ``datetime.fromisoformat`` rejects it on every Python before 3.11
and still rejects the zone name after. Parse it here, once.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

_PLATFORM = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:\s+(\S+))?$"
)
_UTC_NAMES = {"Etc/UTC", "UTC", "Z", "GMT", None, ""}


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse a platform timestamp into a timezone-aware :class:`datetime`.

    Returns ``None`` for empty or unparseable input — a wrong datetime is worse
    than a missing one. A zone the platform does not use is not silently read
    as UTC; it falls through to :func:`datetime.fromisoformat`.
    """
    if not value:
        return None

    match = _PLATFORM.match(value.strip())
    if match:
        year, month, day, hour, minute, second, frac, zone = match.groups()
        if zone in _UTC_NAMES:
            micro = int((frac or "0")[:6].ljust(6, "0"))
            return datetime(
                int(year), int(month), int(day), int(hour), int(minute), int(second),
                micro, tzinfo=timezone.utc,
            )

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def from_unix_seconds(seconds) -> Optional[datetime]:
    """Unix seconds — as webhook envelopes and sessions report time — to a datetime."""
    if seconds is None or seconds == "":
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
