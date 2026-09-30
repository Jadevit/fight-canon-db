"""BestFightOdds loader: fighter pages -> opening line and closing range per fight.

Each run picks fighters, downloads their BestFightOdds pages to data/raw/bestfightodds/,
parses every fight listed there, matches each to a fight in the database (event date first,
then the two names), and replaces that fight's BestFightOdds rows in `odds` (book 'all').
Each fight appears on both fighters' pages; the first one read wins.

Which fighters:
  default     fighters on cards from the last --recent-days days (closing lines settle after)
  --all       every fighter who fought since 2007 (BestFightOdds starts in 2007)
  --offline   every page already on disk, no network
A fighter's BestFightOdds page is found through fighter_aliases (source 'bestfightodds',
recorded on first match) or else by name from the site's fighter sitemap. Pages that fail
to download are skipped, so an outage there never blocks the UFC Stats update.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from canon_db.sources.bestfightodds.match import Matcher, norm
from canon_db.sources.bestfightodds.pages import parse_fighter_page
from canon_db.sources.ufcstats.client import Fetcher, save

NAME = "bestfightodds"
BASE = "https://www.bestfightodds.com"
log = logging.getLogger(NAME)


# --- choosing fighter pages ----------------------------------------------------------

def sitemap_slugs(fetcher: Fetcher) -> list[str]:
    xml = fetcher.get(f"{BASE}/sitemap-teams.xml")
    if xml is None:  # odds are a bonus: never let this source block the rest of the update
        log.warning("Couldn't get the BestFightOdds fighter sitemap; new fighters skipped.")
        return []
    return re.findall(r"/fighters/([^<]+)</loc>", xml)


def slugs_for(fighter_ids: set[str], conn: sqlite3.Connection, fetcher: Fetcher) -> set[str]:
    """BestFightOdds pages for these fighters: known ones from fighter_aliases, the rest by
    name from the sitemap (a shared name can give several pages; matching sorts them out)."""
    known = dict(conn.execute(
        "SELECT fighter_id, source_id FROM fighter_aliases WHERE source = ?", (NAME,)))
    out = {known[f] for f in fighter_ids if f in known}
    missing = fighter_ids - known.keys()
    if missing:
        by_name = defaultdict(list)
        for s in sitemap_slugs(fetcher):
            by_name[norm(s.rsplit("-", 1)[0].replace("-", " "))].append(s)
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighter_aliases"):
            if fid in missing:
                out.update(by_name.get(norm(name), []))
    return out


def fighters_since(conn: sqlite3.Connection, day: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT p.fighter_id FROM fight_participants p JOIN fights USING (fight_id) "
        "JOIN events e USING (event_id) WHERE e.date >= ?", (day,))}


# --- entry points (see canon_db.sources) ---------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="refresh fighters from cards in the last N days (default: 21)")
    p.add_argument("--all", action="store_true",
                   help="every fighter who fought since 2007 (~3.5k pages, ~1 h)")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-read every page already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    pages = raw / "fighters"
    pages.mkdir(parents=True, exist_ok=True)
    if args.offline:
        slugs = sorted(p.stem for p in pages.glob("*.html"))
    else:
        fetcher = Fetcher(args.delay)
        since = "2007-01-01" if args.all else (date.today() - timedelta(days=args.recent_days)).isoformat()
        slugs = sorted(slugs_for(fighters_since(conn, since), conn, fetcher))
        log.info("Fetching %d BestFightOdds fighter pages.", len(slugs))
        for i, slug in enumerate(slugs, 1):
            page = fetcher.get(f"{BASE}/fighters/{slug}")
            if page is not None:
                save(pages / f"{slug}.html", page)
            if i % 250 == 0:
                log.info("  %d/%d", i, len(slugs))

    matcher = Matcher(conn)
    rows, aliases, seen = [], set(), set()
    stats = defaultdict(int)
    for slug in slugs:
        path = pages / f"{slug}.html"
        if not path.exists():
            continue
        for fight in parse_fighter_page(path.read_text(encoding="utf-8")):
            stats["listed"] += 1
            fight_id, ids = matcher.match(fight)
            if fight_id is None:
                continue
            for f in fight["fighters"]:
                aliases.add((ids[f["slug"]], NAME, f["slug"], f["name"]))
            if fight_id in seen:
                continue
            seen.add(fight_id)
            if all(f["open"] is None and f["close_low"] is None for f in fight["fighters"]):
                stats["no line"] += 1
                continue
            for f in fight["fighters"]:
                rows.append((fight_id, ids[f["slug"]], NAME, "all", f["open"], None,
                             f["close_low"], f["close_high"]))
    with conn:
        conn.executemany("DELETE FROM odds WHERE fight_id = ? AND source = ?",
                         [(fid, NAME) for fid in seen])
        conn.executemany("INSERT OR REPLACE INTO odds (fight_id, fighter_id, source, book, open, "
                         "close, close_low, close_high) VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
    log.info("BestFightOdds: %d pages, %d fight listings -> %d fights matched, %d with odds "
             "(%d without a posted line).", len(slugs), stats["listed"], len(seen),
             len(rows) // 2, stats["no line"])
