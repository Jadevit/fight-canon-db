"""Fetch BestFightOdds' fighter pages into data/raw/bestfightodds/fighters/<slug>.html, and
choose which to read. Pages that fail to download are skipped, so an outage there never blocks
the rest of the update.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from canon_db.http import Fetcher, save
from canon_db.matching import norm

BASE = "https://www.bestfightodds.com"
log = logging.getLogger("bestfightodds")


def sitemap_slugs(fetcher: Fetcher) -> list[str]:
    xml = fetcher.get(f"{BASE}/sitemap-teams.xml")
    if xml is None:  # odds are a bonus: never let this source block the rest of the update
        log.warning("Couldn't get the BestFightOdds fighter sitemap; new fighters skipped.")
        return []
    return re.findall(r"/fighters/([^<]+)</loc>", xml)


def slugs_for(fighter_ids: set[int], conn: sqlite3.Connection, fetcher: Fetcher, source: str) -> set[str]:
    """BestFightOdds pages for these fighters: known ones from fighter_aliases, the rest by
    name from the sitemap (a shared name can give several pages; matching sorts them out)."""
    known = conn.execute("SELECT fighter_id, source_id FROM fighter_aliases WHERE source = ?", (source,)).fetchall()
    out = {slug for fid, slug in known if fid in fighter_ids}  # a fighter can have several pages
    missing = fighter_ids - {fid for fid, _ in known}
    if missing:
        by_name = defaultdict(list)
        for s in sitemap_slugs(fetcher):
            by_name[norm(s.rsplit("-", 1)[0].replace("-", " "))].append(s)
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighter_aliases"):
            if fid in missing:
                out.update(by_name.get(norm(name), []))
    return out


def fighters_since(conn: sqlite3.Connection, day: str) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT p.fighter_id FROM fight_participants p JOIN fights USING (fight_id) "
        "JOIN events e USING (event_id) WHERE e.date >= ?", (day,))}


def fetch(conn: sqlite3.Connection, pages: Path, all_: bool, refresh: bool, recent_days: int, delay: float,
          source: str) -> list[str]:
    """Fetch what this run wants; returns the slugs to read."""
    fetcher = Fetcher(delay)
    if all_:
        slugs = sorted(set(sitemap_slugs(fetcher)) | {r[0] for r in conn.execute(
            "SELECT source_id FROM fighter_aliases WHERE source = ?", (source,))})
        todo = slugs if refresh else [s for s in slugs if not (pages / f"{s}.html").exists()]
    else:
        since = (date.today() - timedelta(days=recent_days)).isoformat()
        slugs = todo = sorted(slugs_for(fighters_since(conn, since), conn, fetcher, source))
    log.info("Fetching %d BestFightOdds fighter pages.", len(todo))
    for i, slug in enumerate(todo, 1):
        page = fetcher.get(f"{BASE}/fighters/{slug}")
        if page is not None:
            save(pages / f"{slug}.html", page)
        if i % 250 == 0:
            log.info("  %d/%d", i, len(todo))
    return slugs
