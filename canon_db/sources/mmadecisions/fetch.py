"""Fetch MMA Decisions' pages into data/raw/mmadecisions/:
    years/<year>.html        a year's events (with dates)
    events/<id>.html         an event's decisions
    decisions/<id>.html      a decision: judges' cards and media scores
Boxing events are skipped. Media scores keep coming in for a few days after an event, so
the default run fetches recent events' pages again.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from canon_db.http import Fetcher, save
from canon_db.sources.mmadecisions.pages import decisions, events

BASE = "https://mmadecisions.com"
FIRST_YEAR = 1995
THREADS = 3
log = logging.getLogger("mmadecisions")


def fetch(raw: Path, all_: bool, refresh: bool, recent_days: int, delay: float) -> None:
    for folder in ("years", "events", "decisions"):
        (raw / folder).mkdir(parents=True, exist_ok=True)
    site = Fetcher(delay)
    today = date.today()
    since = (today - timedelta(days=recent_days)).isoformat()
    years = range(FIRST_YEAR, today.year + 1) if all_ else sorted({int(since[:4]), today.year})
    listed = []
    for y in years:
        page = site.get(f"{BASE}/decisions-by-event/{y}/")
        if page is None:
            log.warning("Couldn't get MMA Decisions' %d events; skipped.", y)
            continue
        save(raw / "years" / f"{y}.html", page)
        listed += [e for e in events(page) if not e["name"].lower().startswith("boxing")]
    # Each event's page and its decisions: what isn't on disk, and recent ones again (media
    # scores come in late). An event on disk still gets the decisions missing from disk.
    todo = [e for e in listed if e["date"] <= today.isoformat()]
    # The site takes ~2 s a page, so a few requests are in flight at once; the Fetcher still
    # keeps them a second apart.
    pool = ThreadPoolExecutor(THREADS)

    def get(path: str, out: Path) -> None:
        d = site.get(f"{BASE}/{path}")
        if d is not None:
            save(out, d)

    log.info("%d MMA Decisions events listed.", len(todo))
    for i, e in enumerate(todo, 1):
        path = raw / "events" / f"{e['id']}.html"
        again = refresh or e["date"] >= since
        if again or not path.exists():
            page = site.get(f"{BASE}/{e['path']}")
            if page is None:
                continue
            save(path, page)
        else:
            page = path.read_text(encoding="utf-8")
        wanted = [(p, raw / "decisions" / f"{p.split('/')[1]}.html") for p in decisions(page)]
        list(pool.map(lambda w: get(*w), [w for w in wanted if again or not w[1].exists()]))
        if i % 100 == 0:
            log.info("  %d/%d events", i, len(todo))
