"""Sherdog's fighters, events and fights onto our rows; promotion labels; bout types and what
isn't pro MMA.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter

from canon_db import ids
from canon_db.promotions import promotion
from canon_db.sources.sherdog.fetch import NAME, Pages
from canon_db.sources.sherdog.linking import State, crowded

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


class Labels:
    """Sherdog organization -> our promotion label. Known ones come from promotion_aliases;
    a new one takes its Sherdog name ("Absolute Fighting Championship"), with its id added if
    another organization already has that name."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_org = dict(conn.execute(
            "SELECT source_id, promotion FROM promotion_aliases WHERE source = ?", (NAME,)))
        self.named = {p: o for o, p in self.by_org.items()}
        self.new: dict[str, tuple[str, str]] = {}  # org id -> (label, name)

    def label(self, org_id: str, org: str) -> str:
        if org_id not in self.by_org:
            label = org if self.named.get(org, org_id) == org_id else f"{org} ({org_id})"
            self.named[label] = org_id
            self.by_org[org_id] = label
            self.new[org_id] = (label, org)
        return self.by_org[org_id]


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
        def exists(f: int) -> bool:
            return conn.execute("SELECT 1 FROM fighters WHERE fighter_id = ?", (f,)).fetchone() is not None

        for fid, _sid, prior in st.new_links:
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


def write(conn: sqlite3.Connection, st: State, read: dict[int, dict], pages: Pages | None = None) -> Counter:
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
        # New events get their promotion from their page.
        todo = dict(w.new_events)
        if pages is not None and todo:
            labels = Labels(conn)
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
    # A bout Sherdog now lists as pro comes back with its card.
    conn.executemany("DELETE FROM non_pro_bouts WHERE source = 'sherdog' AND source_id = ?",
                     [(key,) for kind, key in rows if kind == "pro"])
    return {r[1] for r in rows}


def default_pro(conn: sqlite3.Connection) -> None:
    """Sherdog fights not from a card came from a fighter page's pro section."""
    conn.execute("UPDATE fights SET bout_type = 'pro' WHERE bout_type IS NULL AND event_id NOT IN "
                 "(SELECT event_id FROM event_cards) AND fight_id IN "
                 "(SELECT fight_id FROM fight_aliases WHERE source = ?)", (NAME,))


def drop_non_pro(conn: sqlite3.Connection) -> int:
    """Delete amateur and exhibition fights that no promotion's site has (pro MMA only), then
    fighters and events left with no fights. Their Sherdog ids go in non_pro_bouts, so cards
    skip them. Returns the number of fights deleted."""
    gone = [r[0] for r in conn.execute(
        f"SELECT fight_id FROM fights WHERE bout_type IN ('amateur', 'exhibition') AND fight_id NOT IN "
        f"(SELECT fight_id FROM fight_aliases WHERE source IN ({', '.join('?' * len(ids.OFFICIAL))}))",
        sorted(ids.OFFICIAL))]
    conn.executemany(
        "INSERT OR REPLACE INTO non_pro_bouts (source, source_id, bout_type) SELECT a.source, a.source_id, "
        "f.bout_type FROM fight_aliases a JOIN fights f USING (fight_id) WHERE a.fight_id = ? AND a.source = ?",
        [(fight, NAME) for fight in gone])
    for fight in gone:
        ids.delete_fight(conn, fight)
    ids.drop_empty(conn)
    return len(gone)
