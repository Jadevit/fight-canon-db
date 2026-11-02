"""BestFightOdds loader: fighter pages -> opening line and closing range per fight.

Each run picks fighters, downloads their BestFightOdds pages to data/raw/bestfightodds/,
parses every fight listed there, matches each to a fight in the database (event date first,
then the two names), and replaces that fight's BestFightOdds rows in `odds` (book 'all').
Each fight appears on both fighters' pages; the first one read wins.

Which fighters:
  default     fighters on cards from the last --recent-days days (closing lines settle after)
  --all       every fighter in the site's sitemap (~22k pages, ~7 h the first time): starting from
              its side finds fighters whose names we spell differently, and every promotion it
              covers. Pages already on disk are kept (--refresh fetches them again).
  --offline   every page already on disk, no network
A fighter's BestFightOdds page is found through fighter_aliases (source 'bestfightodds',
recorded on first match) or else by name from the site's fighter sitemap. Pages that fail
to download are skipped, so an outage there never blocks the UFC Stats update.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import Counter
from pathlib import Path

from canon_db.ids import alias, clear
from canon_db.matching import Matcher
from canon_db.sources.bestfightodds.fetch import fetch
from canon_db.sources.bestfightodds.pages import parse_fighter_page

NAME = "bestfightodds"
log = logging.getLogger(NAME)


def load(conn: sqlite3.Connection, pages: Path, slugs: list[str]) -> Counter:
    """Replace the odds of every fight the pages of these fighters list."""
    counts = Counter()
    matcher = Matcher(conn)
    placed: dict[tuple, tuple] = {}  # a fight is on both fighters' pages: matched once
    rows, aliases, seen = [], set(), set()
    for slug in slugs:
        path = pages / f"{slug}.html"
        if not path.exists():
            continue
        counts["pages"] += 1
        for fight in parse_fighter_page(path.read_text(encoding="utf-8")):
            counts["fight listings"] += 1
            key = (fight["date"], frozenset((f["slug"], f["name"]) for f in fight["fighters"]))
            if key not in placed:
                placed[key] = matcher.match(fight)
            fight_id, ids = placed[key]
            if fight_id is None:
                continue
            for f in fight["fighters"]:
                aliases.add((ids[f["slug"]], f["slug"], f["name"]))
            if fight_id in seen:
                continue
            seen.add(fight_id)
            if all(f["open"] is None and f["close_low"] is None for f in fight["fighters"]):
                counts["fights without a posted line"] += 1
                continue
            counts["fights with odds"] += 1
            for f in fight["fighters"]:
                rows.append((fight_id, ids[f["slug"]], NAME, "all", f["open"], None,
                             f["close_low"], f["close_high"]))
    with conn:
        clear(conn, NAME, seen, ("odds",))
        conn.executemany("INSERT OR REPLACE INTO odds (fight_id, fighter_id, source, book, open, "
                         "close, close_low, close_high) VALUES (?,?,?,?,?,?,?,?)", rows)
        for fid, sid, name in sorted(aliases):
            alias(conn, "fighter", fid, NAME, sid, name)
    counts["fights matched"] = len(seen)
    return counts


# --- entry points (see canon_db.sources) ---------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="refresh fighters from cards in the last N days (default: 21)")
    p.add_argument("--all", action="store_true",
                   help="every fighter in the sitemap (~22k pages, ~7 h the first time)")
    p.add_argument("--refresh", action="store_true", help="with --all: fetch pages already on disk too")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-read every page already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    pages = raw / "fighters"
    if args.offline:
        slugs = sorted(p.stem for p in pages.glob("*.html"))
    else:
        slugs = fetch(conn, pages, args.all, args.refresh, args.recent_days, args.delay, NAME)
    counts = load(conn, pages, slugs)
    log.info("BestFightOdds: %s.", ", ".join(f"{k} {v}" for k, v in counts.items()))
