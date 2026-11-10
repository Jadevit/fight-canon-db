"""Cell-level parsers for UFC Stats data.

UFC Stats encodes a handful of quirky formats in its tables: "13 of 27" for
landed-of-attempted, "0:45" for control time, and "---"
(or "--", or blank) wherever a value is unknown. These functions turn each of
those into clean Python values, mapping every flavour of "unknown" to None so
the ground rule *blank means unknown, never zero* holds all the way to the DB.

Kept deliberately free of any DB or pandas dependency so they are trivial to
unit-test (see tests/test_fields.py).
"""

from __future__ import annotations

import re
from datetime import datetime

# Anything in this set means "no data", and must become NULL rather than 0.
_BLANKS = {"", "---", "--", "-", "n/a", "na"}


def _blank(s: str | None) -> bool:
    return s is None or s.strip().lower() in _BLANKS


def id_from_url(url: str | None) -> str | None:
    """Pull the 16-hex UFC Stats id out of a detail-page URL.

    >>> id_from_url("http://ufcstats.com/fight-details/568ec6af4008355a")
    '568ec6af4008355a'
    """
    if _blank(url):
        return None
    m = re.search(r"/(?:event|fight|fighter)-details/([0-9a-f]{16})", url)
    return m.group(1) if m else None


def parse_int(s: str | None) -> int | None:
    """Plain integer cell. Blank/sentinel -> None."""
    if _blank(s):
        return None
    try:
        return int(s.strip())
    except ValueError:
        return None


def parse_of(s: str | None) -> tuple[int | None, int | None]:
    """"13 of 27" -> (13, 27). Blank/sentinel -> (None, None).

    >>> parse_of("13 of 27")
    (13, 27)
    >>> parse_of("0 of 0")
    (0, 0)
    >>> parse_of("---")
    (None, None)
    """
    if _blank(s):
        return (None, None)
    m = re.match(r"\s*(\d+)\s+of\s+(\d+)\s*", s)
    if not m:
        return (None, None)
    return (int(m.group(1)), int(m.group(2)))


def parse_ctrl(s: str | None) -> int | None:
    """Control time "m:ss" -> seconds. Blank/sentinel -> None.

    >>> parse_ctrl("1:30")
    90
    >>> parse_ctrl("0:45")
    45
    >>> parse_ctrl("--")
    """
    if _blank(s):
        return None
    m = re.match(r"\s*(\d+):(\d{1,2})\s*", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_weight_lbs(s: str | None) -> int | None:
    """"155 lbs." -> 155."""
    if _blank(s):
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def parse_height_in(s: str | None) -> int | None:
    """Height like `5' 11"` -> 71 (inches). Blank/sentinel -> None."""
    if _blank(s):
        return None
    m = re.match(r"\s*(\d+)'\s*(\d+)\"?", s)
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def parse_reach_in(s: str | None) -> int | None:
    """Reach like `76"` -> 76. Blank/sentinel -> None."""
    if _blank(s):
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def parse_record(s: str | None) -> tuple[int | None, int | None, int | None, int]:
    """Pro record "Record: 20-5-1 (1 NC)" -> (20, 5, 1, 1). Unreadable -> Nones, 0 NC.

    >>> parse_record("Record: 4-6-0")
    (4, 6, 0, 0)
    """
    m = re.search(r"(\d+)-(\d+)-(\d+)(?:\s*\((\d+)\s*NC\))?", s or "")
    if not m:
        return (None, None, None, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))


def parse_dob(s: str | None) -> str | None:
    """UFC Stats date "Jul 13, 1978" -> ISO "1978-07-13". Blank -> None."""
    if _blank(s):
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Method strings signalling a fight's result was voided rather than a normal W/L.
def is_no_contest(method: str | None) -> bool:
    return bool(method) and "no contest" in method.lower()


def is_overturned(method: str | None, details: str | None, no_contest: bool) -> bool:
    """The result was changed after the fight. Besides method "Overturned" or "overturned"
    in the details, a no contest that still shows a finish (e.g. KO/TKO voided for a failed
    drug test) counts; one from an accidental foul ("Could Not Continue") doesn't."""
    method = (method or "").lower()
    if method == "overturned" or "overturned" in (details or "").lower():
        return True
    return no_contest and method not in ("", "could not continue", "other") \
        and not is_no_contest(method)
