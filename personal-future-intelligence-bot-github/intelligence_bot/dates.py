from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# RFC 822 feeds sometimes use common non-GMT abbreviations. Resolve only
# unambiguous abbreviations; e.g. "IST" is intentionally omitted because it
# can mean India, Ireland, or Israel time.
_RFC_ZONE_OFFSETS = {
    "CEST": "+0200",
    "CET": "+0100",
    "EDT": "-0400",
    "EST": "-0500",
    "EEST": "+0300",
    "EET": "+0200",
    "JST": "+0900",
    "KST": "+0900",
    "SGT": "+0800",
    "AEST": "+1000",
    "AEDT": "+1100",
    "NZST": "+1200",
    "NZDT": "+1300",
    "PDT": "-0700",
    "PST": "-0800",
}
_ZONE_SUFFIX = re.compile(r"\s+([A-Za-z]{2,5})\s*$")


def parse_publication_datetime(value: str) -> datetime | None:
    """Parse ISO-8601 and RSS/RFC dates into an aware UTC datetime.

    Naive timestamps are treated as UTC for compatibility with feeds that omit
    a zone. Known, unambiguous RFC timezone abbreviations are converted to an
    explicit numeric offset. Ambiguous named zones are rejected instead of
    being silently interpreted as UTC.
    """
    raw = value.strip()
    if not raw:
        return None

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        rfc_value = raw
        suffix = _ZONE_SUFFIX.search(raw)
        if suffix:
            zone = suffix.group(1).upper()
            if zone in _RFC_ZONE_OFFSETS:
                rfc_value = raw[:suffix.start(1)] + _RFC_ZONE_OFFSETS[zone]
            elif zone not in {"UTC", "GMT", "UT", "Z"}:
                # email.utils accepts some unknown abbreviations as naive time.
                # Refuse those so a local clock is never mistaken for UTC.
                return None
        try:
            parsed = parsedate_to_datetime(rfc_value)
        except (TypeError, ValueError, OverflowError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def publication_date_status(
    value: str,
    max_age_seconds: int,
    now: datetime | None = None,
) -> tuple[str, datetime | None]:
    """Return missing_date, invalid_date, future_date, too_old, or fresh."""
    if not value.strip():
        return "missing_date", None
    parsed = parse_publication_datetime(value)
    if parsed is None:
        return "invalid_date", None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current.astimezone(timezone.utc) - parsed).total_seconds()
    if age_seconds < -300:
        return "future_date", parsed
    if age_seconds > max_age_seconds:
        return "too_old", parsed
    return "fresh", parsed


def publication_date_is_fresh(value: str, max_age_seconds: int) -> bool:
    status, _ = publication_date_status(value, max_age_seconds)
    # Missing dates remain eligible for consideration, but are counted in
    # diagnostics. The source must still be new and pass relevance/verification.
    return status in ("fresh", "missing_date")
