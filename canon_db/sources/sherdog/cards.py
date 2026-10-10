"""Whole Sherdog cards: every bout of an event, whoever fought in it.

Fighter pages (load.py) only reach fighters linked to UFC Stats and their opponents, so which
regional fights the database has would depend on careers. Cards don't: for the organizations
in ORGS every event is read, and each one read is recorded in event_cards.

A card's bouts are written like the fighter-page loader writes fights (same ids, same
linking):
  - a bout the database has as a UFC Stats fight (same date, a fighter already linked; names
    decide on a crowded card) isn't added; it links the other fighter if the names agree, and
    the event becomes an event_aliases row;
  - a bout the database has as a Sherdog fight is updated from a fresh event page, or only
    has its empty columns filled from an older copy (the Kaggle datasets);
  - any other bout becomes a `source = 'sherdog'` fight.
Bios (when the card has them) fill empty columns of `sherdog:` fighters; of other fighters
only `dob`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import date, timedelta

from canon_db.sources.sherdog.load import (BASE, NAME, PREFIX, Labels, Pages, State, drop_duplicates,
                                           drop_empty_events, mark_bout_types, merge, minted, move_event,
                                           name_ok)
from canon_db.sources.sherdog.pages import bio, parse_card, parse_event, parse_org_events

# Sherdog organizations whose every event is read (id: name on Sherdog).
ORGS = {
    "8185": "Absolute Championship Akhmat",
    "1960": "Bellator MMA",
    "186": "Cage Warriors",
    "351": "Jungle Fight",
    "668": "Konfrontacja Sztuk Walki",
    "11339": "Legacy Fighting Alliance (LFA)",
    "5383": "Oktagon MMA",
    "12241": "Professional Fighters League",
    "10333": "Rizin Fighting Federation",
}
BIO = ("nickname", "dob", "height_in", "weight_lbs", "nationality")

log = logging.getLogger(NAME)


def match_ufcstats(st: State, b: dict) -> tuple | None:
    """The UFC Stats fight this bout is, from whichever side is linked; links the other side
    if the names agree."""
    for me, opp in (b["sides"], b["sides"][::-1]):
        fid = st.fid.get(me["sid"])
        if fid is None or fid.startswith(PREFIX):
            continue
        m = st.match(fid, dict(date=b["date"], opponent=opp["name"]), b["crowded"])
        if m is None:
            continue
        st.events.setdefault(b["event"], m[2])
        if opp["sid"] and name_ok(opp["name"], st.names[m[3]]):
            st.link(m[3], opp["sid"], f"card bout with {fid} on {b['date']}")
        return m
    return None


def write(conn: sqlite3.Connection, st: State, bouts: list[dict], fresh: bool) -> Counter:
    """Write cards. Each bout: {event, event_name, date, location, label, via, fetched_at,
    weight_class, method, referee, round, time, title (None: unknown), placed (False: the card
    lists it but it can't be written), sides: [{sid, name, result, *BIO}] x2}. `fresh`: the
    cards are newer than the database's own Sherdog fights, so they replace them."""
    counts = Counter(bouts=len(bouts))
    redirects = dict(conn.execute("SELECT old_id, new_id FROM fighter_redirects"))
    per_card = Counter((b["event"], s["sid"]) for b in bouts for s in b["sides"])
    for b in bouts:
        b["crowded"] = any(per_card[(b["event"], s["sid"])] > 1 for s in b["sides"])
    ok = [b for b in bouts if b.get("placed", True) and b["event"] and all(s["sid"] for s in b["sides"])]
    written = {id(b) for b in ok}
    while True:  # a new link can make another bout matchable
        before = len(st.new_links)
        for b in ok:
            match_ufcstats(st, b)
        if len(st.new_links) == before:
            break

    def fighter(s: dict) -> str:
        fid = st.fid.get(s["sid"]) or minted(s["sid"])
        return redirects.get(fid, fid)

    fighters, aliases, events, fights, parts = {}, set(), {}, {}, []
    cards: dict[str, list] = {}  # Sherdog event id -> [bout, every bout placed]
    for b in bouts:
        cards.setdefault(b["event"], [b, True])
        if id(b) not in written:
            cards[b["event"]][1] = False
            counts["bouts not written"] += 1
    for b in ok:
        eid = st.events.get(b["event"], minted(b["event"]))
        ids = [fighter(s) for s in b["sides"]]
        matched = match_ufcstats(st, b) is not None
        for s, fid in zip(b["sides"], ids):
            if matched and fid.startswith(PREFIX):
                continue  # not linked: no fight of theirs is written, so no fighter either
            aliases.add((fid, NAME, s["sid"], s["name"]))
            if fid not in fighters or not fighters[fid].get("dob"):
                fighters[fid] = s
        if matched:
            counts["bouts UFC Stats has"] += 1
            continue
        if eid.startswith(PREFIX):
            events.setdefault(eid, (eid, b["event_name"], b["date"], b["location"], b["label"], NAME))
        lo, hi = sorted((s["sid"] for s in b["sides"]), key=int)
        fight_id = f"{PREFIX}{b['event']}:{lo}-{hi}"
        fights[fight_id] = (fight_id, eid, b["weight_class"], int(bool(b["title"])), b["method"], b["round"],
                            b["time"], b["referee"], int(b["sides"][0]["result"] == "NC"), NAME)
        ranked = sorted(zip(ids, (s["result"] for s in b["sides"])))  # corner means nothing
        parts += [(fight_id, fid, corner, r) for corner, (fid, r) in enumerate(ranked)]

    keep = "excluded.{0}" if fresh else "coalesce({0}, excluded.{0})"
    cols = ["weight_class", "method", "end_round", "end_time", "referee"] + (
        ["title_fight", "no_contest"] if fresh else [])
    with conn:
        for fid, s in st.new_links:  # a `sherdog:` fighter found on UFC Stats
            if conn.execute("SELECT 1 FROM fighters WHERE fighter_id = ?", (minted(s),)).fetchone():
                merge(conn, minted(s), fid)
                counts["merged"] += 1
        known = {r[0] for r in conn.execute("SELECT fighter_id FROM fighters")}
        new = [(fid, s["name"], NAME) for fid, s in fighters.items() if fid not in known]
        conn.executemany("INSERT INTO fighters (fighter_id, name, source) VALUES (?,?,?)", new)
        counts["new fighters"] = len(new)
        conn.executemany(
            "UPDATE fighters SET " + ", ".join(f"{c} = coalesce({c}, ?)" for c in BIO)
            + " WHERE fighter_id = ? AND source = ?",
            [(*(s.get(c) for c in BIO), fid, NAME) for fid, s in fighters.items()])
        conn.executemany("UPDATE fighters SET dob = ? WHERE fighter_id = ? AND dob IS NULL AND source <> ?",
                         [(s["dob"], fid, NAME) for fid, s in fighters.items() if s.get("dob")])
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, s) for s, eid in st.events.items() if not eid.startswith(PREFIX)])
        for s, eid in st.events.items():  # events now known to be the database's own
            move_event(conn, minted(s), eid)
        conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                         sorted({(e[4],) for e in events.values()}))
        conn.executemany("INSERT OR IGNORE INTO events (event_id, name, date, location, promotion, source) "
                         "VALUES (?,?,?,?,?,?)", events.values())
        conn.executemany("UPDATE events SET location = ? WHERE event_id = ? AND location IS NULL",
                         [(e[3], eid) for eid, e in events.items() if e[3]])
        have = {r[0] for r in conn.execute("SELECT fight_id FROM fights WHERE source = ?", (NAME,))}
        counts["new fights"] = len(fights.keys() - have)
        counts["fights updated"] = len(fights.keys() & have)
        conn.executemany(
            "INSERT INTO fights (fight_id, event_id, weight_class, title_fight, method, end_round, end_time, "
            "referee, no_contest, source) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (fight_id) DO UPDATE SET "
            + ", ".join(f"{c} = {keep.format(c)}" for c in cols), fights.values())
        if fresh:
            conn.executemany("DELETE FROM fight_participants WHERE fight_id = ?", [(f,) for f in fights])
        conn.executemany("INSERT OR IGNORE INTO fight_participants (fight_id, fighter_id, corner, result) "
                         "VALUES (?,?,?,?)", parts)
        counts["duplicates dropped"] = drop_duplicates(conn)
        drop_empty_events(conn)
        exist = {r[0] for r in conn.execute("SELECT event_id FROM events")}
        rows = [(st.events.get(e, minted(e)), NAME, b["via"], int(placed), b["fetched_at"])
                for e, (b, placed) in cards.items() if e]
        rows = [r for r in rows if r[0] in exist]
        conn.executemany("INSERT OR REPLACE INTO event_cards (event_id, source, via, complete, fetched_at) "
                         "VALUES (?,?,?,?,?)", rows)
        counts["cards"] = len(rows)
        counts["new links"] = len(st.new_links)
    return counts


# --- reading event pages ------------------------------------------------------------------

def org_events(pages: Pages, org: str, every_page: bool) -> list[tuple[str, str]]:
    """[(event id, date)] listed on an organization's page; with `every_page`, its older
    pages too (100 events each)."""
    out, n = [], 1
    while True:
        url = f"{BASE}/organizations/x-{org}" + (f"/recent-events/{n}" if n > 1 else "")
        page = pages.fetcher.get(url)
        if page is None:
            log.warning("Couldn't read %s; its events are skipped this run.", url)
            return out
        events, older = parse_org_events(page)
        out += events
        if not (every_page and older):
            return out
        n += 1


def card_bouts(page: str, sid: str, label: str, today: str) -> list[dict]:
    """The bouts of an event page in write()'s shape; a bout with no result yet isn't placed."""
    info = parse_event(page)
    out = []
    for c in parse_card(page):
        out.append(dict(
            event=sid, event_name=info["name"], date=info["date"], location=info["location"] or None,
            label=label, via="event_page", fetched_at=today, weight_class=c["weight_class"] or None,
            method=c["method"] or None, referee=c["referee"] or None,
            round=int(c["round"]) if c["round"].isdigit() else None, time=c["time"] or None,
            title=c["title"], placed=all(s["result"] for s in c["sides"]), sides=c["sides"]))
    return out


def crawl(conn: sqlite3.Connection, pages: Pages, recent_days: int, every_page: bool) -> Counter:
    """Read the cards of ORGS: events with no card yet, and every event from the last
    `recent_days` days again (results get corrected)."""
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=recent_days)).isoformat()
    st = State(conn)
    carded = {r[0] for r in conn.execute("SELECT event_id FROM event_cards WHERE source = ? AND complete = 1",
                                         (NAME,))}
    labels = Labels(conn)
    bouts = []
    for org, org_name in ORGS.items():
        listed = [(e, d) for e, d in org_events(pages, org, every_page) if d <= today]
        todo = [e for e, d in listed if d >= since or st.events.get(e, minted(e)) not in carded]
        log.info("Sherdog cards: %s: %d events listed, %d to read.", org_name, len(listed), len(todo))
        for e in todo:
            page = pages.event_page(e, fresh=True)
            if page is None:
                continue
            info = parse_event(page)
            label = labels.label(info["org_id"] or org, info["org"] or org_name)
            bouts += card_bouts(page, e, label, today)
    with conn:
        conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                         sorted({(label,) for label, _ in labels.new.values()}))
        conn.executemany("INSERT OR REPLACE INTO promotion_aliases (promotion, source, source_id, name) "
                         "VALUES (?,?,?,?)", [(label, NAME, o, n) for o, (label, n) in labels.new.items()])
    return write(conn, st, bouts, fresh=True)


# --- pro, exhibition or amateur -----------------------------------------------------------

def classify(conn: sqlite3.Connection, pages: Pages, since: str | None) -> Counter:
    """Event pages don't say whether a bout was pro: read fighter pages to set bout_type on
    card fights that don't have it (cards read since `since`, or all of them if None), and
    fill those fighters' bios. One fighter's page covers both sides of a bout, so fighters
    with the most such bouts go first and anyone whose bouts are all covered is skipped."""
    counts = Counter()
    todo: dict[str, set[str]] = {}
    for (fight,) in conn.execute(
            "SELECT f.fight_id FROM fights f JOIN event_cards k USING (event_id) "
            "WHERE f.source = ? AND f.bout_type IS NULL AND k.fetched_at >= ?", (NAME, since or "")):
        for sid in fight.rsplit(":", 1)[1].split("-"):
            todo.setdefault(sid, set()).add(fight)
    owner = dict(conn.execute("SELECT source_id, fighter_id FROM fighter_aliases WHERE source = ?", (NAME,)))
    done: set[str] = set()
    log.info("Sherdog bout types: %d card fights to check.", len(set().union(*todo.values())) if todo else 0)
    for sid in sorted(todo, key=lambda s: -len(todo[s])):
        if not todo[sid] - done:
            continue
        page = pages.get(sid)
        if page is None:
            continue
        counts["pages read"] += 1
        with conn:
            done |= mark_bout_types(conn, {sid: page}) & todo[sid]
            fid, b = owner.get(sid), bio(page)
            if fid is not None:
                conn.execute("UPDATE fighters SET " + ", ".join(f"{c} = coalesce({c}, ?)" for c in BIO)
                             + " WHERE fighter_id = ? AND source = ?", (*(b[c] for c in BIO), fid, NAME))
                if b["dob"]:
                    conn.execute("UPDATE fighters SET dob = ? WHERE fighter_id = ? AND dob IS NULL",
                                 (b["dob"], fid))
        if counts["pages read"] % 250 == 0:
            log.info("  pages read: %d, fights checked: %d", counts["pages read"], len(done))
    counts["fights checked"] = len(done)
    counts.update(dict(conn.execute(
        "SELECT 'card fights ' || coalesce(bout_type, 'unchecked'), count(*) FROM fights "
        "WHERE event_id IN (SELECT event_id FROM event_cards) AND source = ? GROUP BY 1", (NAME,))))
    return counts
