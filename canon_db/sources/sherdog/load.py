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
  --relabel   no fighter pages: label every Sherdog event from its organization
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from canon_db import ids
from canon_db.http import Fetcher, save
from canon_db.matching import Bouts, days_apart, name_ok, norm
from canon_db.sources.sherdog.pages import parse_event, parse_fighter, parse_sitemap
from canon_db.sources.ufcstats.load import promotion

NAME = "sherdog"
BASE = "https://www.sherdog.com"
SITEMAPS = 18  # sitemap-fighters.xml, sitemap-fighters2.xml, ... sitemap-fighters18.xml

# Fallback when an event's page can't be read: its name starts with the promotion
# ("CWFC 88 - ..."); labels that differ from ours.
SHERDOG_PROMOTIONS: dict[str, str] = {
    "Professional Fighters League": "PFL",
    "CW": "CWFC",
    "Bellator MMA": "Bellator",
    "ONE Championship": "ONE FC",
}

log = logging.getLogger(NAME)


def fight_key(event: str, a: str, b: str) -> str:
    """A fight's Sherdog alias: '<event>:<fighter>-<fighter>', lower Sherdog id first."""
    lo, hi = sorted((a, b), key=int)
    return f"{event}:{lo}-{hi}"


def sherdog_promotion(event_name: str, ours: dict[str, str]) -> str:
    """'CWFC 88 - Cage Warriors Fighting Championship 88' -> 'CWFC' (see promotion()); a
    label we already use in another case ('Deep' for 'DEEP') becomes ours."""
    label = promotion(event_name.split(" - ")[0])
    label = SHERDOG_PROMOTIONS.get(label, label)
    return ours.get(label.casefold(), label)


# --- what the database already has --------------------------------------------------------

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


# --- reading pages ------------------------------------------------------------------------

class Pages:
    def __init__(self, raw: Path, fetcher: Fetcher | None, cached: bool = False):
        self.dir, self.events, self.fetcher, self.cached = raw / "fighters", raw / "events", fetcher, cached
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events.mkdir(parents=True, exist_ok=True)

    def event(self, sid: str) -> dict | None:
        """An event's page, parsed (read from disk if it's there)."""
        page = self.event_page(sid)
        return parse_event(page) if page is not None else None

    def event_page(self, sid: str, fresh: bool = False) -> str | None:
        """An event's page as HTML: from disk unless `fresh` (or not there yet)."""
        path = self.events / f"{sid}.html"
        if self.fetcher is not None and (fresh or not path.exists()):
            page = self.fetcher.get(f"{BASE}/events/x-{sid}")
            if page is not None:
                save(path, page)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def get(self, sid: str, cached: bool | None = None) -> dict | None:
        """A fighter's page, fetched fresh unless `cached` (default: self.cached) and it's on
        disk already."""
        path = self.dir / f"{sid}.html"
        if self.fetcher is not None and not ((self.cached if cached is None else cached) and path.exists()):
            page = self.fetcher.get(f"{BASE}/fighter/{sid}")
            if page is not None:
                save(path, page)
        return parse_fighter(path.read_text(encoding="utf-8")) if path.exists() else None

    def on_disk(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.html"))


def sitemap(fetcher: Fetcher) -> dict[str, list[str]]:
    """Normalized name -> Sherdog ids, from the fighter sitemaps."""
    out = defaultdict(list)
    for i in range(1, SITEMAPS + 1):
        xml = fetcher.get(f"{BASE}/sitemap-fighters{'' if i == 1 else i}.xml")
        for sid, name in parse_sitemap(xml or ""):
            out[norm(name)].append(sid)
    return out


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


def crawl(st: State, pages: Pages, scope: set[int], limit: int | None = None) -> dict[int, dict]:
    """Read the pages of every linked fighter in `scope` (and of anyone linked on the way),
    seeding unlinked ones from the sitemap. Returns {fighter_id: parsed page}. `limit` caps
    the pages read (for trying it out)."""
    read: dict[int, dict] = {}
    names = None
    tried: set[int] = set()

    def walk(queue: list[int]) -> None:
        while queue and (limit is None or len(read) < limit):
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
        if limit is not None and len(read) >= limit:
            break
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


# --- promotion labels -----------------------------------------------------------------------

class Labels:
    """Sherdog organization -> our promotion label. Known ones come from promotion_aliases;
    a new one takes the label of the database events it shares (its PRIDE events are our
    PRIDE), else its Sherdog name ("Absolute Fighting Championship"), with its id added if
    another organization already has that name."""

    def __init__(self, conn: sqlite3.Connection, hints: dict[str, str] | None = None):
        self.by_org = dict(conn.execute(
            "SELECT source_id, promotion FROM promotion_aliases WHERE source = ?", (NAME,)))
        self.named = {p: o for o, p in self.by_org.items()}
        self.hints = hints or {}
        self.new: dict[str, tuple[str, str]] = {}  # org id -> (label, name)

    def label(self, org_id: str, org: str) -> str:
        if org_id not in self.by_org:
            label = self.hints.get(org_id)
            if label is None:
                label = org if self.named.get(org, org_id) == org_id else f"{org} ({org_id})"
                self.named[label] = org_id
            self.by_org[org_id] = label
            self.new[org_id] = (label, org)
        return self.by_org[org_id]


def org_hints(conn: sqlite3.Connection, pages: Pages) -> dict[str, str]:
    """Sherdog organization -> the label most of its events in the database have, from the
    events a promotion's own site also has."""
    votes = defaultdict(lambda: defaultdict(int))
    for sid, label in conn.execute(
            f"SELECT a.source_id, e.promotion FROM event_aliases a JOIN events e USING (event_id) "
            f"WHERE a.source = ? AND a.event_id IN (SELECT event_id FROM event_aliases WHERE source IN "
            f"({', '.join('?' * len(ids.OFFICIAL))}))", (NAME, *sorted(ids.OFFICIAL))):
        info = pages.event(sid)
        if info and info["org_id"]:
            votes[info["org_id"]][label] += 1
    return {org: max(v, key=v.get) for org, v in votes.items()}


def label_events(pages: Pages, labels: Labels, events: dict[int, str]) -> list[tuple]:
    """(promotion, location, event_id) for these events ({our id: Sherdog id}), from their
    pages; an event whose page can't be read keeps its label."""
    out = []
    for i, (eid, sid) in enumerate(sorted(events.items()), 1):
        info = pages.event(sid)
        if info and info["org_id"]:
            out.append((labels.label(info["org_id"], info["org"]), info["location"] or None, eid))
        if i % 1000 == 0:
            log.info("  event pages: %d/%d", i, len(events))
    return out


# --- writing ------------------------------------------------------------------------------

class Writer:
    """Sherdog's fighters, events and fights onto our rows; shared by fighter pages and cards."""

    def __init__(self, conn: sqlite3.Connection, st: State):
        self.conn, self.st = conn, st
        self.ranked = ids.official(conn, "fight")  # fights a promotion's site has
        self.ranked_events = ids.official(conn, "event")
        self.moved: dict[int, int] = {}            # merged fighter -> the fighter kept
        self.moved_events: dict[int, int] = {}
        self.new_events: dict[int, str] = {}       # our id -> Sherdog id, made this run
        self.counts = Counter()

    def fighter_now(self, fid: int | None) -> int | None:
        while fid in self.moved:
            fid = self.moved[fid]
        return fid

    def event_now(self, eid: int | None) -> int | None:
        while eid in self.moved_events:
            eid = self.moved_events[eid]
        return eid

    def merges(self) -> None:
        """Fighters found to be one through a Sherdog id, events through a shared fight."""
        conn, st = self.conn, self.st
        exists = lambda f: conn.execute("SELECT 1 FROM fighters WHERE fighter_id = ?", (f,)).fetchone()
        for fid, sid, prior in st.new_links:
            a, b = self.fighter_now(fid), self.fighter_now(prior)
            if b is not None and a != b and exists(a) and exists(b):
                keep = ids.merge(conn, "fighter", a, b)
                self.moved[a + b - keep] = keep
                self.counts["fighters merged"] += 1
        st.new_links = []
        for a, b in st.same_events:
            a, b = self.event_now(a), self.event_now(b)
            if a != b and not ids.sources(conn, "event", a) & ids.sources(conn, "event", b) & ids.OFFICIAL:
                keep = ids.merge(conn, "event", a, b)
                self.moved_events[a + b - keep] = keep
                if keep in self.ranked_events or a + b - keep in self.ranked_events:
                    self.ranked_events.add(keep)
                self.counts["events merged"] += 1
        st.same_events = []
        known = ids.aliases(conn, "event", NAME)
        for sid, eid in st.events.items():
            st.events[sid] = self.event_now(eid)
            if known.get(sid) != st.events[sid]:  # linked through a shared fight
                ids.alias(conn, "event", st.events[sid], NAME, sid)

    def fighter(self, sid: str, name: str) -> int:
        """Our fighter for a Sherdog id (a new one if it's not linked)."""
        fid = self.fighter_now(self.st.fid.get(sid))
        if fid is None:
            fid = ids.add(self.conn, "fighter", {"name": name})
            self.st.names[fid] = [name]
            self.counts["new fighters"] += 1
        self.st.fid[sid] = fid
        self.st.sd.setdefault(fid, sid)
        ids.alias(self.conn, "fighter", fid, NAME, sid, name)
        return fid

    def event(self, sid: str, name: str, day: str | None, location: str | None, label: str) -> int:
        """Our event for a Sherdog event id (a new one if it's not linked). An event's name and
        date are set once: fighter pages and event pages name some events differently."""
        eid = self.event_now(self.st.events.get(sid))
        if eid is None:
            eid = ids.add(self.conn, "event", {"name": name, "date": day, "location": location, "promotion": label})
            self.st.events[sid] = eid
            self.new_events[eid] = sid
            self.counts["new events"] += 1
        else:
            ids.write(self.conn, "event", eid, NAME, {"name": name, "date": day, "location": location}, True)
        ids.alias(self.conn, "event", eid, NAME, sid)
        return eid

    def fight(self, key: str, found: int | None, event: tuple, values: dict, sides: list[tuple],
              bout_type: str | None = None) -> int:
        """Write one Sherdog fight (found by its alias, else `found`, else new) and return it.
        `event`: Writer.event's arguments; `sides`: [(Sherdog id, name, result)] x2. On a fight
        a promotion's site has, Sherdog only fills blanks: its results, corners and event stay,
        and a side Sherdog doesn't link to ours isn't added. A new fight gets `bout_type`."""
        conn = self.conn
        fight = ids.find(conn, "fight", NAME, key) or found
        if fight is not None and fight in self.ranked:
            ids.write(conn, "fight", fight, NAME, values, True)
            for sid, name, _ in sides:
                if sid in self.st.fid:
                    self.fighter(sid, name)
            self.counts["fights a promotion's site has"] += 1
        else:
            people = [(self.fighter(sid, name), result) for sid, name, result in sides]
            eid = self.event(*event)
            if fight is None:
                fight = ids.add(conn, "fight", {"event_id": eid, "bout_type": bout_type, **values,
                                                "title_fight": values.get("title_fight") or 0})
                self.st.bouts.add(fight, eid, event[2], people[0][0], people[1][0])
                self.counts["new fights"] += 1
            else:
                ids.write(conn, "fight", fight, NAME, {"event_id": eid, **values}, False)
                self.counts["fights updated"] += 1
            conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (fight,))
            conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) VALUES (?,?,?,?)",
                             [(fight, f, corner, r) for corner, (f, r) in enumerate(sorted(people))])
        ids.alias(conn, "fight", fight, NAME, key)
        return fight


def write(conn: sqlite3.Connection, st: State, read: dict[int, dict], pages: Pages | None = None,
          relabel: bool = False) -> Counter:
    """Write the fights on these fighters' pages ({fighter: parsed page})."""
    ours = {p.casefold(): p for (p,) in conn.execute("SELECT promotion FROM promotions")}
    found = {}  # fight key -> (the page's fighter, their Sherdog id, the fight, a match in the database)
    for fid, page in read.items():
        sid = st.sd[fid]
        for f in page["fights"]:
            if f["date"] and f["opponent_id"] and f["event_id"]:
                found.setdefault(fight_key(f["event_id"], sid, f["opponent_id"]),
                                 (fid, sid, f, st.match(fid, f, crowded(page, f))))
    with conn:
        w = Writer(conn, st)
        w.merges()
        touched = set()
        for key, (fid, sid, f, m) in found.items():
            other = {"W": "L", "L": "W"}.get(f["result"], f["result"])
            event = (f["event_id"], f["event"], f["date"], None, sherdog_promotion(f["event"], ours))
            values = dict(title_fight=None if f["title"] is None else int(f["title"]), method=f["method"] or None,
                          end_round=int(f["round"]) if f["round"].isdigit() else None, end_time=f["time"] or None,
                          referee=f["referee"] or None, no_contest=int(f["result"] == "NC"))
            touched.add(w.fight(key, m[1] if m else None, event, values,
                                [(sid, read[fid]["name"], f["result"]), (f["opponent_id"], f["opponent"], other)], "pro"))
        # A read fighter's fights that their page no longer lists lose Sherdog's claim, except on
        # events whose whole card was read (the card says what was on it).
        for fid in read:
            for (old,) in conn.execute(
                    "SELECT p.fight_id FROM fight_participants p JOIN fights f USING (fight_id) "
                    "WHERE p.fighter_id = ? AND f.event_id NOT IN (SELECT event_id FROM event_cards) AND EXISTS "
                    "(SELECT 1 FROM fight_aliases a WHERE a.fight_id = p.fight_id AND a.source = ?)",
                    (w.fighter_now(fid), NAME)).fetchall():
                if old not in touched:
                    ids.drop_claim(conn, old, NAME)
                    w.counts["fights no longer listed"] += 1
        # New events (all of them with `relabel`) get their promotion from their page.
        todo = dict(w.new_events)
        if relabel:
            todo.update({e: s for s, e in st.events.items() if e not in w.ranked_events})
        if pages is not None and todo:
            labels = Labels(conn, org_hints(conn, pages) if relabel else None)
            conn.executemany("UPDATE events SET promotion = ?, location = coalesce(?, location) WHERE event_id = ?",
                             label_events(pages, labels, todo))
            conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                             sorted({(label,) for label, _ in labels.new.values()}))
            conn.executemany("INSERT OR REPLACE INTO promotion_aliases (promotion, source, source_id, name) "
                             "VALUES (?,?,?,?)", [(label, NAME, org, name) for org, (label, name) in labels.new.items()])
            w.counts["new promotions"] = len(labels.new)
        conn.execute("INSERT OR IGNORE INTO promotions (promotion, coverage) SELECT DISTINCT promotion, 'partial' "
                     "FROM events WHERE promotion NOT IN (SELECT promotion FROM promotions)")
        mark_bout_types(conn, {st.sd[fid]: page for fid, page in read.items()})
        default_pro(conn)
        w.counts["duplicate fights merged"] = ids.merge_duplicates(conn)
        w.counts["non-pro dropped"] = drop_non_pro(conn)
    w.counts.update(fights=len(found))
    return w.counts


def mark_bout_types(conn: sqlite3.Connection, pages: dict[str, dict]) -> set[str]:
    """Set bout_type on the fights these pages ({sherdog id: parsed page}) list, by the section
    they're in. Returns the fight keys listed."""
    rows = [(kind, fight_key(f["event_id"], sid, f["opponent_id"])) for sid, page in pages.items()
            for kind, key in (("pro", "fights"), ("exhibition", "exhibition"), ("amateur", "amateur"))
            for f in page.get(key, []) if f["event_id"] and f["opponent_id"]]
    conn.executemany("UPDATE fights SET bout_type = ? WHERE fight_id = "
                     "(SELECT fight_id FROM fight_aliases WHERE source = 'sherdog' AND source_id = ?)", rows)
    return {r[1] for r in rows}


def default_pro(conn: sqlite3.Connection) -> None:
    """Sherdog fights not from a card came from a fighter page's pro section."""
    conn.execute("UPDATE fights SET bout_type = 'pro' WHERE bout_type IS NULL AND event_id NOT IN "
                 "(SELECT event_id FROM event_cards) AND fight_id IN "
                 "(SELECT fight_id FROM fight_aliases WHERE source = ?)", (NAME,))


def drop_non_pro(conn: sqlite3.Connection) -> int:
    """Delete amateur and exhibition fights that no promotion's site has (pro MMA only), then
    fighters and events left with no fights. Returns the number of fights deleted."""
    gone = [r[0] for r in conn.execute(
        f"SELECT fight_id FROM fights WHERE bout_type IN ('amateur', 'exhibition') AND fight_id NOT IN "
        f"(SELECT fight_id FROM fight_aliases WHERE source IN ({', '.join('?' * len(ids.OFFICIAL))}))",
        sorted(ids.OFFICIAL))]
    for fight in gone:
        ids.delete_fight(conn, fight)
    ids.drop_empty(conn)
    return len(gone)


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="read fighters from promotions' own cards in the last N days (default: 21)")
    p.add_argument("--all", action="store_true",
                   help="every fighter on a promotion's own card (~4.5k pages, ~1.5 h)")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-read every page already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--limit", type=int, help="read at most N pages (for trying it out)")
    p.add_argument("--cached", action="store_true",
                   help="reuse fighter pages already on disk; fetch only missing ones")
    p.add_argument("--no-cards", action="store_true",
                   help="skip reading whole cards (the events in Sherdog's sitemaps that are due)")
    p.add_argument("--classify-all", action="store_true",
                   help="check the bout type of every card fight not checked yet, not just recent cards")
    p.add_argument("--workers", type=int, default=4,
                   help="pages fetched at once for cards and bout types (the delay still holds)")
    p.add_argument("--relabel", action="store_true",
                   help="label every Sherdog event from its organization (~25k event pages "
                        "the first time, ~7 h; read from disk after)")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    st = State(conn)
    pages = Pages(raw, None if args.offline else Fetcher(args.delay), args.cached)
    if args.relabel:  # event pages only
        read = {}
    elif args.offline:
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
        read = crawl(st, pages, scope, args.limit)
    links = len(st.new_links)
    counts = write(conn, st, read, pages, args.relabel)
    if not (args.offline or args.relabel or args.no_cards):
        from canon_db.sources.sherdog import cards  # it builds on this module
        c = cards.crawl(conn, pages, args.recent_days, args.workers)
        log.info("Sherdog cards: %s.", ", ".join(f"{k} {v}" for k, v in c.items()))
        since = None if args.classify_all else (date.today() - timedelta(days=args.recent_days)).isoformat()
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
