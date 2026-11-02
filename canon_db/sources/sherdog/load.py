"""Sherdog loader: link fighters to Sherdog through shared fights, and write the fights on
their Sherdog pages.

Linking. A fighter is linked to a Sherdog id only through fights both sources list:
  - a seed (a fighter in scope with no Sherdog id, whose name is in Sherdog's fighter
    sitemap) is linked once their Sherdog page shows at least two of their fights (same date
    within a day, opponent names agree); one is enough if that's all the fights they have;
  - every fight on a linked fighter's page that matches one of their fights in the database
    links the opponent too (the other corner of that fight), and their page is read next.
Names only have to agree (see name_ok). A Sherdog id another fighter already has makes the
two one fighter (merged, the older id kept), unless both have ids from one promotion's site:
that is reported in conflicts.tsv and left alone.

Fights. Sherdog has no fight ids; a fight's alias is '<event>:<fighter>-<fighter>'. Every pro
fight on a page is found by that alias, else matched to one of the fighter's fights within a
day, else written as a new fight (`bout_type = 'pro'`), with new fighters and events for any
Sherdog doesn't link to ours. Sherdog is the fallback source: on a row a promotion's own site
has, it only fills blanks.

Cards. After the fighter pages, the same update reads whole cards (see cards.py). A fight on
such an event belongs to its card: this loader fills it in but never removes it.

Which pages:
  default     fighters on promotions' own cards (UFC Stats, PFL, Oktagon) from the last
              --recent-days days, plus anyone linked during the run (their page holds their
              earlier career)
  --all       every such fighter, whenever they fought (~4.5k pages, ~1.5 h)
  --offline   every page already on disk, no network
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from canon_db import ids
from canon_db.http import Fetcher
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.fetch import NAME, Pages
from canon_db.sources.sherdog.linking import State, crawl, follow
from canon_db.sources.sherdog.write import write

log = logging.getLogger(NAME)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="read fighters from promotions' own cards in the last N days (default: 21)")
    p.add_argument("--all", action="store_true",
                   help="every fighter on a promotion's own card (~4.5k pages, ~1.5 h)")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-read every page already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--no-cards", action="store_true",
                   help="skip reading whole cards (the events in Sherdog's sitemaps that are due)")
    p.add_argument("--workers", type=int, default=4,
                   help="pages fetched at once for cards and bout types (the delay still holds)")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    st = State(conn)
    pages = Pages(raw, None if args.offline else Fetcher(args.delay))
    if args.offline:
        on_disk = set(pages.on_disk())
        read = {}
        queue = sorted(f for f, s in st.sd.items() if s in on_disk and f in st.official)
        while queue:  # links found on one page make more pages usable
            fid = queue.pop()
            if fid in read:
                continue
            read[fid] = pages.get(st.sd[fid])
            queue += [f for f in follow(st, fid, read[fid]) if st.sd[f] in on_disk]
    else:
        since = "0000" if args.all else (date.today() - timedelta(days=args.recent_days)).isoformat()
        scope = {r[0] for r in conn.execute(
            f"SELECT DISTINCT p.fighter_id FROM fight_participants p JOIN fights f USING (fight_id) "
            f"JOIN events e USING (event_id) WHERE e.date >= ? AND f.fight_id IN (SELECT fight_id FROM "
            f"fight_aliases WHERE source IN ({', '.join('?' * len(ids.OFFICIAL))}))", (since, *sorted(ids.OFFICIAL)))}
        log.info("Sherdog: %d fighters in scope, %d already linked.",
                 len(scope), len(scope & st.sd.keys()))
        read = crawl(st, pages, scope)
    links = len(st.new_links)
    counts = write(conn, st, read, pages)
    if not (args.offline or args.no_cards):
        c = cards.crawl(conn, pages, args.recent_days, args.workers)
        log.info("Sherdog cards: %s.", ", ".join(f"{k} {v}" for k, v in c.items()))
        since = (date.today() - timedelta(days=args.recent_days)).isoformat()
        c = cards.classify(conn, pages, since, args.workers)
        log.info("Sherdog bout types: %s.", ", ".join(f"{k} {v}" for k, v in c.items()))
    (raw / "conflicts.tsv").unlink(missing_ok=True)
    if st.conflicts:
        with open(raw / "conflicts.tsv", "w", encoding="utf-8") as fh:
            fh.write("fighter_id\tsherdog_id\tfighter_has\tsherdog_has\twhy\n")
            fh.writelines("\t".join(c) + "\n" for c in st.conflicts)
    log.info("Sherdog: %d pages read; %d new links; %s; %d conflicts%s.",
             len(read), links, ", ".join(f"{k} {v}" for k, v in counts.items()),
             len(st.conflicts), " (see conflicts.tsv)" if st.conflicts else "")
