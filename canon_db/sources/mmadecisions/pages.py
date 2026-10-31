"""MMA Decisions pages -> plain data. No network, no database.

A year's list of events, an event's list of decisions, and a decision: both fighters, the
event and date, each judge's card round by round, and the media scores (writers' cards)."""

from __future__ import annotations

import html
import re
from datetime import datetime

from canon_db.matching import norm


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def events(page: str) -> list[dict]:
    """A year's events: [{id, path, name, date}], date as YYYY-MM-DD."""
    out = []
    for date, path, eid, name in re.findall(
            r'<td[^>]*>\s*(\w{3} \d{1,2}, \d{4})&nbsp;\s*</td>\s*<td[^>]*><a href="(event/(\d+)/[^"]*)">(.*?)</a>',
            page, re.S):
        out.append({"id": eid, "path": path.strip(), "name": text(name),
                    "date": datetime.strptime(date, "%b %d, %Y").date().isoformat()})
    return out


def decisions(page: str) -> list[str]:
    """An event's decisions, as their paths ("decision/<id>/<names>")."""
    return list(dict.fromkeys(p.strip() for p in re.findall(r'href="(decision/\d+/[^"]*)"', page)))


def _scores(cell: str) -> int | None:
    t = text(cell)
    return int(t) if re.fullmatch(r"\d+", t) else None


def _side(label: str, fighters: list[dict]) -> int | None:
    """Which fighter (0 or 1) a surname or short name on the page means; None if neither or both."""
    hit = [i for i, f in enumerate(fighters) if norm(label) and norm(label) in norm(f["name"])]
    return hit[0] if len(hit) == 1 else None


def decision(page: str) -> dict | None:
    """{fighters: [{slug, name}] x2, method, event: {id, name}, date, judges: [{name, rounds:
    [(round, score_0, score_1)], totals: (score_0, score_1)}], media: [{writer, outlet, url,
    score: (high, low), pick: 0, 1 or None for a draw}]}; scores are by fighter, in the order
    of `fighters` (the page's: the winner first). None if the page isn't a decision."""
    fighters = [{"slug": fid, "name": text(name)}
                for fid, name in re.findall(r'<a href="fighter/(\d+)/[^"]*">(.*?)</a>', page, re.S)[:2]]
    event = re.search(r'<a href="event/(\d+)/[^"]*">(.*?)</a></b><br>\s*(\w+ \d{1,2}, \d{4})', page)
    if len(fighters) != 2 or event is None:
        return None
    method = re.search(r'class="event2"[^>]*><i>(.*?)</i>', page)
    out = {"fighters": fighters, "method": text(method.group(1)) if method else None,
           "event": {"id": event.group(1), "name": text(event.group(2))},
           "date": datetime.strptime(event.group(3), "%B %d, %Y").date().isoformat(),
           "judges": [], "media": []}

    # Judges: a table each, headed by the judge's name, then ROUND | <fighter> | <fighter>.
    end = page.find("MEDIA SCORES")
    for block in re.split(r'class="judge">', page[:end if end > 0 else len(page)])[1:]:
        name = text(re.match(r"(.*?)</td>", block, re.S).group(1))
        head = re.search(r"ROUND</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", block, re.S)
        if not head:
            continue
        cols = [_side(text(h), fighters) for h in head.groups()]
        if sorted(c for c in cols if c is not None) != [0, 1]:
            cols = [0, 1]  # the page's own order
        flip = cols == [1, 0]
        rows = []
        for n, a, b in re.findall(r'<tr class="decision">\s*<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>(.*?)</td>\s*'
                                  r'<td[^>]*>(.*?)</td>', block, re.S):
            a, b = _scores(a), _scores(b)
            rows.append((int(n), *((b, a) if flip else (a, b))))
        total = re.search(r"TOTAL</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", block, re.S)
        totals = tuple(_scores(t) for t in total.groups()) if total else (None, None)
        out["judges"].append({"name": name, "rounds": rows, "totals": totals[::-1] if flip else totals})

    # Media scores: writer and outlet, the score (winner's first) and whom it's for.
    if end > 0:
        media = page[end:page.find("YOUR SCORECARD", end) if "YOUR SCORECARD" in page[end:] else len(page)]
        for who, score, pick in re.findall(r'<tr class="decision">\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*'
                                           r'<td[^>]*>(.*?)</td>', media, re.S):
            writer, _, outlet = who.partition("<br>")
            url = re.search(r"href='([^']*)'", score) or re.search(r"href='([^']*)'", outlet)
            nums = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text(score))
            if not text(writer) or not nums:
                continue
            pick_text = text(pick)
            out["media"].append({
                "writer": text(writer), "outlet": text(outlet) or None, "url": url.group(1) if url else None,
                "score": (int(nums.group(1)), int(nums.group(2))),
                "pick": None if pick_text.lower() == "draw" else _side(pick_text, fighters),
                "pick_text": pick_text})
    return out
