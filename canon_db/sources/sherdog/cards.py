"""Whole Sherdog cards: every bout of an event, whoever fought in it.

Fighter pages (load.py) only reach fighters linked through promotions' own cards and their
opponents, so which regional fights the database has would depend on careers. Cards don't:
every event in Sherdog's event sitemaps is read, and each one read is recorded in event_cards.

A card's bouts are written like the fighter-page loader writes fights (same aliases, same
linking): a bout is found by its alias, else matched to a fight of one of its linked fighters
(same date, names deciding on a crowded card; that links the other side if the names agree,
and the event), else written as a new fight. Bios (when the card has them) fill the fighters'
blank columns.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta

from canon_db import ids
from canon_db.http import save
from canon_db.matching import name_ok
from canon_db.sources.sherdog.load import (BASE, NAME, Labels, Pages, State, Writer, drop_non_pro,
                                           fight_key, mark_bout_types, sherdog_promotion)
from canon_db.sources.sherdog.pages import bio, parse_card, parse_event, parse_event_sitemap

BIO = ("nickname", "dob", "height_in", "weight_lbs", "nationality")

log = logging.getLogger(NAME)


def match(st: State, b: dict) -> tuple | None:
    """Our fight this bout is, through whichever side is linked; links the other side if the
    names agree, and the event."""
    for me, opp in (b["sides"], b["sides"][::-1]):
        fid = st.fid.get(me["sid"])
        if fid is None:
            continue
        m = st.match(fid, dict(date=b["date"], opponent=opp["name"]), b["crowded"])
        if m is None:
            continue
        st.event_is(b["event"], m[2])
        if opp["sid"] and name_ok(opp["name"], st.names.get(m[3], [])):
            st.link(m[3], opp["sid"], f"card bout with {fid} on {b['date']}")
        return m
    return None


def write(conn: sqlite3.Connection, st: State, bouts: list[dict]) -> Counter:
    """Write cards. Each bout: {event, event_name, date, location, label, via, fetched_at,
    weight_class, method, referee, round, time, title (None: unknown), placed (False: the card
    lists it but it can't be written), sides: [{sid, name, result, *BIO}] x2}. A bout the
    database has replaces its Sherdog values (event pages are read after its results settle)."""
    counts = Counter(bouts=len(bouts))
    per_card = Counter((b["event"], s["sid"]) for b in bouts for s in b["sides"])
    for b in bouts:
        b["crowded"] = any(per_card[(b["event"], s["sid"])] > 1 for s in b["sides"])
    ok = [b for b in bouts if b.get("placed", True) and b["event"] and all(s["sid"] for s in b["sides"])
          and b["sides"][0]["sid"] != b["sides"][1]["sid"]]  # a few cards list one fighter on both sides
    written = {id(b) for b in ok}
    non_pro = {k for (k,) in conn.execute("SELECT source_id FROM non_pro_bouts WHERE source = ?", (NAME,))}
    ok = [b for b in ok if fight_key(b["event"], *(s["sid"] for s in b["sides"])) not in non_pro]
    counts["non-pro skipped"] = len(written) - len(ok)
    while True:  # a new link can make another bout matchable
        before = len(st.new_links)
        for b in ok:
            match(st, b)
        if len(st.new_links) == before:
            break
    found = [(b, match(st, b)) for b in ok]

    cards: dict[str, list] = {}  # Sherdog event id -> [bout, every bout placed]
    for b in bouts:
        cards.setdefault(b["event"], [b, True])
        if id(b) not in written:
            cards[b["event"]][1] = False
            counts["bouts not written"] += 1
    with conn:
        w = Writer(conn, st)
        w.merges()
        official = ids.official(conn, "fighter")
        for b, m in found:
            event = (b["event"], b["event_name"], b["date"], b["location"], b["label"])
            values = dict(weight_class=b["weight_class"], title_fight=int(bool(b["title"])), method=b["method"],
                          end_round=b["round"], end_time=b["time"], referee=b["referee"],
                          no_contest=int(b["sides"][0]["result"] == "NC"))
            w.fight(fight_key(b["event"], *(s["sid"] for s in b["sides"])), m[1] if m else None, event, values,
                    [(s["sid"], s["name"], s["result"]) for s in b["sides"]])
            for s in b["sides"]:
                fid = w.fighter_now(st.fid.get(s["sid"]))
                if fid is not None:
                    ids.write(conn, "fighter", fid, NAME, {c: s.get(c) for c in BIO}, fid in official)
        conn.execute("INSERT OR IGNORE INTO promotions (promotion, coverage) SELECT DISTINCT promotion, 'partial' "
                     "FROM events WHERE promotion NOT IN (SELECT promotion FROM promotions)")
        rows = [(w.event_now(st.events.get(e)), NAME, b["via"], int(placed), b["fetched_at"])
                for e, (b, placed) in cards.items() if e]
        exist = {r[0] for r in conn.execute("SELECT event_id FROM events")}
        rows = [r for r in rows if r[0] in exist]
        conn.executemany("INSERT OR REPLACE INTO event_cards (event_id, source, via, complete, fetched_at) "
                         "VALUES (?,?,?,?,?)", rows)
        counts["duplicate fights merged"] = ids.merge_duplicates(conn)
        ids.drop_empty(conn)
        counts["cards"] = len(rows)
    counts.update(w.counts)
    return counts


# --- reading event pages ------------------------------------------------------------------

@contextmanager
def threads(workers: int) -> Iterator[ThreadPoolExecutor]:
    """A thread pool that drops its queued work if the caller fails (a plain `with` would
    first finish every queued fetch)."""
    pool = ThreadPoolExecutor(max(1, workers))
    try:
        yield pool
    finally:
        pool.shutdown(cancel_futures=True)


def sitemap_events(pages: Pages, tries: int = 6, wait: float = 10) -> list[str]:
    """Every event id in Sherdog's event sitemaps (those its sitemap index lists). Sherdog's
    cache sometimes holds an empty copy of one, so each request asks with a query string of
    its own (which gets past it), each sitemap is retried, and the last good copy on disk is
    used if it never comes."""
    index = pages.fetcher.get(f"{BASE}/sitemap.xml") or ""
    out = set()
    saved = pages.events.parent / "sitemaps"
    saved.mkdir(exist_ok=True)
    for url in re.findall(r"<loc>([^<]*/sitemap-events\d*\.xml)</loc>", index):
        path = saved / url.rsplit("/", 1)[1]
        for n in range(tries):
            xml = pages.fetcher.get(f"{url}?r={time.time_ns()}") or ""
            ids = parse_event_sitemap(xml)
            if ids:
                save(path, xml)
                break
            time.sleep(wait * (n + 1))
        else:
            ids = parse_event_sitemap(path.read_text(encoding="utf-8")) if path.exists() else []
            log.warning("Couldn't read %s; %s.", url,
                        "using the copy on disk" if ids else "its events are left for the next run")
        out.update(ids)
    if not out:
        log.warning("Couldn't read Sherdog's event sitemaps; no cards are read this run.")
    return sorted(out, key=int)


def read_event(pages: Pages, sid: str, settle: int) -> tuple[dict, list[dict], str] | None:
    """(page info, card, day fetched) of an event page. A copy on disk is used if it was saved
    more than `settle` days after the event (its results had settled); else it's fetched."""
    path = pages.events / f"{sid}.html"
    try:
        if path.exists():
            page = path.read_text(encoding="utf-8")
            info, saved = parse_event(page), date.fromtimestamp(path.stat().st_mtime)
            if info["date"] and saved > date.fromisoformat(info["date"]) + timedelta(days=settle):
                return info, parse_card(page), saved.isoformat()
        page = pages.event_page(sid, fresh=True)
        if page is None:
            return None
        return parse_event(page), parse_card(page), date.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception as e:  # one bad page shouldn't end a days-long run
        log.warning("Couldn't read event %s: %s: %s", sid, type(e).__name__, e)
        return None


def card_bouts(info: dict, card: list[dict], sid: str, label: str, fetched: str) -> list[dict]:
    """The bouts of an event page in write()'s shape; a bout with no result yet isn't placed."""
    out = []
    for c in card:
        out.append(dict(
            event=sid, event_name=info["name"], date=info["date"], location=info["location"] or None,
            label=label, via="event_page", fetched_at=fetched, weight_class=c["weight_class"] or None,
            method=c["method"] or None, referee=c["referee"] or None,
            round=int(c["round"]) if c["round"].isdigit() else None, time=c["time"] or None,
            title=c["title"], placed=all(s["result"] for s in c["sides"]), sides=c["sides"]))
    return out


def pages_read(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str]]:
    """Sherdog event id -> (date, day fetched) of every event page read before, including
    the cards read before event_pages existed."""
    seen = {s: (d, f) for s, d, f in conn.execute(
        "SELECT source_id, date, fetched_at FROM event_pages WHERE source = ?", (NAME,))}
    for s, d, f in conn.execute("""
            SELECT a.source_id, e.date, coalesce(k.fetched_at, '')
            FROM event_cards k JOIN events e USING (event_id)
            JOIN event_aliases a ON a.event_id = k.event_id AND a.source = ?
            WHERE k.source = ? AND k.complete = 1""", (NAME, NAME)):
        seen.setdefault(s, (d, f))
    return seen


def due(seen: dict, sid: str, day: str | None, today: str, settle: int) -> bool:
    """Whether to read an event: never read, or last read less than `settle` days after it
    (results get corrected) and it has happened. `day`: its date if known without reading."""
    if sid not in seen:
        return True
    day = day or seen[sid][0]
    if day is None:
        return True
    return day <= today and seen[sid][1] <= (date.fromisoformat(day) + timedelta(days=settle)).isoformat()


def crawl(conn: sqlite3.Connection, pages: Pages, recent_days: int, workers: int = 1,
          chunk: int = 1000) -> Counter:
    """Read the cards of the events in Sherdog's sitemaps that are due(): new ones and recent
    ones (all ~78k the first time, which takes days; a daily run reads a few hundred). Pages
    are fetched `workers` at a time and written `chunk` events at a time."""
    today = date.today().isoformat()
    seen = pages_read(conn)
    listed = sitemap_events(pages)
    # Events read before stay due without a sitemap (one that didn't come leaves only new ones).
    todo = [e for e in sorted(set(listed) | seen.keys(), key=int) if due(seen, e, None, today, recent_days)]
    log.info("Sherdog cards: %d events in the sitemaps, %d to read.", len(listed), len(todo))
    st, labels, counts = State(conn), Labels(conn), Counter()
    ours = {p.casefold(): p for (p,) in conn.execute("SELECT promotion FROM promotions")}
    bouts, read = [], []

    def flush() -> None:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                             sorted({(label,) for label, _ in labels.new.values()}))
            conn.executemany("INSERT OR REPLACE INTO promotion_aliases (promotion, source, source_id, name) "
                             "VALUES (?,?,?,?)", [(label, NAME, o, n) for o, (label, n) in labels.new.items()])
            conn.executemany("INSERT OR REPLACE INTO event_pages (source, source_id, date, fetched_at) "
                             "VALUES (?,?,?,?)", read)
        counts.update(write(conn, st, bouts))
        bouts.clear()
        read.clear()

    start = time.monotonic()
    with threads(workers) as pool:
        got = pool.map(lambda e: read_event(pages, e, recent_days), todo)
        for i, (e, page) in enumerate(zip(todo, got), 1):
            if page is not None:
                info, card, fetched = page
                if info["org_id"]:
                    label = labels.label(info["org_id"], info["org"])
                else:
                    label = sherdog_promotion(info["name"], ours)
                bouts += card_bouts(info, card, e, label, fetched)
                read.append((NAME, e, info["date"], fetched))
                counts["pages read"] += 1
            if i % chunk == 0 or i == len(todo):
                flush()
            if i % 500 == 0:
                left = (time.monotonic() - start) / i * (len(todo) - i)
                log.info("  event pages: %d/%d (%.1f h left)", i, len(todo), left / 3600)
    counts["new links"] = len(st.new_links)
    return counts


# --- pro, exhibition or amateur -----------------------------------------------------------

def classify(conn: sqlite3.Connection, pages: Pages, since: str | None, workers: int = 1) -> Counter:
    """Event pages don't say whether a bout was pro: read fighter pages to set bout_type on
    card fights that don't have it and no promotion's site has (those stay whatever Sherdog
    says) (cards read since `since`, or all of them if None), fill
    those fighters' bios, then delete the amateur and exhibition fights. One fighter's page
    covers both sides of a bout, so fighters with the most such bouts go first and anyone
    whose bouts are all covered is skipped. A page on disk is used if it lists one of the
    bouts; else it's fetched (`workers` at a time)."""
    counts = Counter()
    todo: dict[str, set[str]] = {}
    for (fight,) in conn.execute(
            f"SELECT a.source_id FROM fights f JOIN event_cards k USING (event_id) "
            f"JOIN fight_aliases a ON a.fight_id = f.fight_id AND a.source = ? "
            f"WHERE f.bout_type IS NULL AND k.fetched_at >= ? AND f.fight_id NOT IN (SELECT fight_id "
            f"FROM fight_aliases WHERE source IN ({', '.join('?' * len(ids.OFFICIAL))}))",
            (NAME, since or "", *sorted(ids.OFFICIAL))):
        for sid in fight.rsplit(":", 1)[1].split("-"):
            todo.setdefault(sid, set()).add(fight)
    owner = dict(conn.execute("SELECT source_id, fighter_id FROM fighter_aliases WHERE source = ?", (NAME,)))
    official = ids.official(conn, "fighter")
    done: set[str] = set()
    total = len(set().union(*todo.values())) if todo else 0
    log.info("Sherdog bout types: %d card fights to check.", total)

    def listed(sid: str, page: dict) -> set[str]:
        return {fight_key(f["event_id"], sid, f["opponent_id"]) for key in ("fights", "exhibition", "amateur")
                for f in page[key] if f["event_id"] and f["opponent_id"]}

    def fetch(sid: str) -> dict | None:
        try:
            on_disk = (pages.dir / f"{sid}.html").exists()
            page = pages.get(sid, cached=True)
            if on_disk and page is not None and not listed(sid, page) & todo[sid]:
                page = pages.get(sid, cached=False)  # saved before these bouts
            return page
        except Exception as e:  # one bad page shouldn't end a days-long run
            log.warning("Couldn't read fighter %s: %s: %s", sid, type(e).__name__, e)
            return None

    order = sorted(todo, key=lambda s: -len(todo[s]))
    start, i = time.monotonic(), 0
    with threads(workers) as pool:
        while i < len(order):
            batch = []
            while i < len(order) and len(batch) < max(1, workers) * 8:
                if todo[order[i]] - done:
                    batch.append(order[i])
                i += 1
            for sid, page in zip(batch, pool.map(fetch, batch)):
                if page is None or not todo[sid] - done:
                    continue
                counts["pages read"] += 1
                with conn:
                    done |= mark_bout_types(conn, {sid: page}) & todo[sid]
                    fid, b = owner.get(sid), bio(page)
                    if fid is not None:
                        ids.write(conn, "fighter", fid, NAME, {c: b[c] for c in BIO}, fid in official)
                if counts["pages read"] % 250 == 0:
                    left = (time.monotonic() - start) / i * (len(order) - i)
                    log.info("  pages read: %d, fights checked: %d/%d (at most %.1f h left)",
                             counts["pages read"], len(done), total, left / 3600)
    counts["fights checked"] = len(done)
    with conn:
        counts["non-pro dropped"] = drop_non_pro(conn)
    counts.update(dict(conn.execute(
        "SELECT 'card fights ' || coalesce(bout_type, 'unchecked'), count(*) FROM fights "
        "WHERE event_id IN (SELECT event_id FROM event_cards) AND fight_id NOT IN (SELECT fight_id FROM "
        f"fight_aliases WHERE source IN ({', '.join('?' * len(ids.OFFICIAL))})) GROUP BY 1", sorted(ids.OFFICIAL))))
    return counts
