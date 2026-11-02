"""Linking fighters to Sherdog through fights both list (see load.py): what the database has
now, following a page's opponents, and the crawl from page to page.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

from canon_db import ids
from canon_db.matching import Bouts, days_apart, name_ok, norm
from canon_db.sources.sherdog.fetch import NAME, Pages, sitemap

log = logging.getLogger(NAME)


class State:
    """Links (fighter <-> Sherdog id), event links, and every fight by fighter."""

    def __init__(self, conn: sqlite3.Connection):
        self.bouts = Bouts(conn)
        self.names, self.fights = self.bouts.names, self.bouts.fights
        self.sd, self.fid = {}, {}  # fighter_id -> a Sherdog id, Sherdog id -> fighter_id
        for fid, sid in conn.execute(
                "SELECT DISTINCT fighter_id, source_id FROM fighter_aliases WHERE source = ? "
                "ORDER BY fighter_id, CAST(source_id AS INTEGER)", (NAME,)):
            self.sd.setdefault(fid, sid)
            self.fid[sid] = fid
        self.official: dict[int, set] = defaultdict(set)  # fighter -> (source, id) on promotions' sites
        for fid, src, sid in conn.execute(
                f"SELECT fighter_id, source, source_id FROM fighter_aliases WHERE source IN "
                f"({', '.join('?' * len(ids.OFFICIAL))})", sorted(ids.OFFICIAL)):
            self.official[fid].add((src, sid))
        self.events = ids.aliases(conn, "event", NAME)  # Sherdog event id -> our event
        self.same_events: list[tuple[int, int]] = []    # pairs of our events found to be one
        self.new_links: list[tuple[int, str, int | None]] = []  # (fighter, Sherdog id, its owner before)
        self.conflicts: list[tuple] = []
        self.witnesses = defaultdict(set)  # (fighter_id, sherdog id) -> pages that pair them

    def clash(self, a: int, b: int) -> bool:
        """Do these two fighters have different ids on one promotion's site?"""
        sa, sb = {s for s, _ in self.official.get(a, ())}, {s for s, _ in self.official.get(b, ())}
        return bool(sa & sb) and self.official[a] != self.official[b]

    def link(self, fid: int, sid: str, why: str) -> bool:
        """Link a fighter to a Sherdog id. If another fighter has it, the two are one (merged
        on write), unless they clash(). A fighter already linked can take a second id: Sherdog
        sometimes has a duplicate profile. Returns True if `fid` is newly linked (their page
        is worth reading)."""
        have_sid, have_fid = self.sd.get(fid), self.fid.get(sid)
        if have_fid == fid:
            return False
        if have_fid is not None and self.clash(fid, have_fid):
            self.conflicts.append((str(fid), sid, have_sid or "", str(have_fid), why))
            return False
        self.fid[sid] = fid
        self.new_links.append((fid, sid, have_fid))
        if have_sid is None:
            self.sd[fid] = sid
            return True
        return False

    def event_is(self, sid: str, eid: int) -> None:
        """Sherdog event `sid` is our event `eid` (one of its fights is there)."""
        have = self.events.setdefault(sid, eid)
        if have != eid:
            self.same_events.append((have, eid))

    def match(self, fid: int, fight: dict, crowded: bool = False) -> tuple | None:
        """The database fight this Sherdog fight of `fid` is: (date, fight_id, event_id, opponent)."""
        return self.bouts.on(fid, fight["date"], fight["opponent"], crowded)

    def named_match(self, fid: int, fight: dict) -> tuple | None:
        return self.bouts.named(fid, fight["date"], fight["opponent"])


def crowded(page: dict, fight: dict) -> bool:
    """Does the page list another fight within a day of this one (a tournament)?"""
    return sum(1 for f in page["fights"] if f["date"] and fight["date"]
               and days_apart(f["date"], fight["date"]) <= 1) > 1


def follow(st: State, fid: int, page: dict) -> list[int]:
    """Link the opponents in `page` (fighter `fid`'s) through the fights we share; record
    their events. Returns the fighters newly linked."""
    out = []
    for f in page["fights"]:
        m = st.match(fid, f, crowded(page, f))
        if m is None or not f["opponent_id"]:
            continue
        if f["event_id"]:
            st.event_is(f["event_id"], m[2])
        # Names that don't agree (a ring name, a mononym) need a second page to say the same.
        seen = st.witnesses[(m[3], f["opponent_id"])]
        seen.add(fid)
        if not name_ok(f["opponent"], st.names.get(m[3], [])) and len(seen) < 2:
            continue
        if st.link(m[3], f["opponent_id"], f"opponent of {fid} on {f['date']}"):
            out.append(m[3])
    return out


def verify_seed(st: State, fid: int, page: dict) -> bool:
    """Is this Sherdog page fighter `fid`'s? Two shared fights, or one if that's all they have."""
    shared = sum(st.named_match(fid, f) is not None for f in page["fights"])
    return shared >= 2 or (shared == 1 and len(st.fights.get(fid, [])) == 1
                           and norm(page["name"]) in {norm(n) for n in st.names.get(fid, [])})


def crawl(st: State, pages: Pages, scope: set[int]) -> dict[int, dict]:
    """Read the pages of every linked fighter in `scope` (and of anyone linked on the way),
    seeding unlinked ones from the sitemap. Returns {fighter_id: parsed page}."""
    read: dict[int, dict] = {}
    names = None
    tried: set[int] = set()

    def walk(queue: list[int]) -> None:
        while queue:
            fid = queue.pop()
            if fid in read or fid not in st.sd:
                continue
            page = pages.get(st.sd[fid])
            if page is None:
                continue
            read[fid] = page
            queue.extend(follow(st, fid, page))
            if len(read) % 250 == 0:
                log.info("  pages read: %d (linked so far: %d)", len(read), len(st.sd))

    walk(sorted(f for f in scope if f in st.sd))
    # Seed the fighters nobody linked yet, most fights first; each seed's walk links many more.
    for fid in sorted(scope, key=lambda f: -len(st.fights.get(f, []))):
        if fid in st.sd or fid in tried or not st.fights.get(fid):
            continue
        tried.add(fid)
        if names is None:
            log.info("Reading Sherdog's fighter sitemap for seeds.")
            names = sitemap(pages.fetcher)
        for sid in {s for n in st.names.get(fid, []) for s in names.get(norm(n), [])}:
            if sid in st.fid and st.clash(fid, st.fid[sid]):
                continue
            page = pages.get(sid)
            if page and verify_seed(st, fid, page) and st.link(fid, sid, "seed"):
                walk([fid])
                break
    return read
