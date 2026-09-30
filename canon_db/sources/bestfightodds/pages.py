"""Parse BestFightOdds fighter pages into per-fight odds records.

A fighter page lists every fight BestFightOdds tracked for that fighter: an event row
("UFC 92: The Ultimate 2008  Dec 27th 2008"), then one row per fighter with the opening
line and the closing range (lowest ... highest close across sportsbooks).
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime

_TAG = re.compile(r"<[^>]+>")
_FIGHTER_LINK = re.compile(r'href="/fighters/([^"]+)"')
_ODDS = re.compile(r"^[+-]\d+$")


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", fragment))).strip()


def american(s: str) -> int | None:
    s = s.strip()
    return int(s) if _ODDS.match(s) else None


def parse_date(s: str) -> str | None:
    """'Dec 27th 2008' -> '2008-12-27'."""
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{1,2})(?:st|nd|rd|th)? (\d{4})$",
                  s.strip())
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def parse_fighter_page(page: str) -> list[dict]:
    """[{event, date, fighters: [{slug, name, open, close_low, close_high}, ...]}], one per fight.

    Odds are American. Values that aren't posted (a fight with no line) are None."""
    tables = re.findall(r"<table.*?</table>", page, re.S)
    if not tables:
        return []
    table = max(tables, key=len)
    fights, current = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        slug = _FIGHTER_LINK.search(cells[0]) if cells else None
        if slug is None:
            # an event row: "<event name> <date>" in one cell
            t = text(row)
            if t and parse_date(t):
                current = {"event": re.sub(r"\s*\w{3} \d{1,2}\w* \d{4}$", "", t).strip(),
                           "date": parse_date(t), "fighters": []}
                fights.append(current)
            continue
        if current is None:
            continue
        vals = [text(c) for c in cells]
        # Matchup | Open | close low | ... | close high | (movement) | (event/date)
        odds = [american(v) for v in vals[1:5]]
        current["fighters"].append({
            "slug": slug.group(1), "name": vals[0],
            "open": odds[0], "close_low": odds[1], "close_high": odds[3]})
    return [f for f in fights if len(f["fighters"]) == 2]
