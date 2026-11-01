"""What every source's page parser needs."""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")


def text(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()
