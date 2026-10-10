"""Sherdog loader: link fighters to Sherdog through shared fights, then add the fights
UFC Stats doesn't have.

Linking. A fighter is linked to a Sherdog id only through fights both sources list:
  - a seed (an unlinked UFC Stats fighter whose name is in Sherdog's fighter sitemap) is
    linked once their Sherdog page shows at least two of their fights (same date within a
    day, opponent names agree); one is enough if that's all the fights they have;
  - every fight on a linked fighter's page that matches one of their fights in the database
    links the opponent too (the other corner of that fight), and their page is read next.
Names only have to agree (see name_ok); a Sherdog id already linked to someone else is
reported in conflicts.tsv and left alone.

Fights. Every pro fight on a linked fighter's page that the database doesn't have becomes a
`source = 'sherdog'` fight. Opponents UFC Stats doesn't know become `sherdog:<id>` fighters;
events are the database's own when one of their fights matched (event_aliases), else
`sherdog:<id>`. When a `sherdog:` fighter later turns up on UFC Stats, their rows move to the
UFC Stats id (fighter_redirects), and a Sherdog fight that UFC Stats also has is dropped.

Which pages:
  default     fighters on UFC Stats cards from the last --recent-days days, plus anyone
              linked during the run (their page holds their earlier career)
  --all       every UFC Stats fighter with a fight (~4.5k pages, ~1.5 h)
  --offline   every page already on disk, no network
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from canon_db.sources.bestfightodds.match import norm, sim
from canon_db.sources.sherdog.pages import parse_event, parse_fighter, parse_sitemap
from canon_db.sources.ufcstats.client import Fetcher, save
from canon_db.sources.ufcstats.load import promotion

NAME = "sherdog"
BASE = "https://www.sherdog.com"
PREFIX = "sherdog:"
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


def minted(sherdog_id: str) -> str:
    return PREFIX + sherdog_id


def name_ok(name: str, names: list[str]) -> bool:
    """Two spellings plausibly of one person: close overall, or sharing a surname-length word."""
    words = {w for w in norm(name).split() if len(w) >= 3}
    return any(sim(name, n) >= 0.6 or words & {w for w in norm(n).split() if len(w) >= 3}
               for n in names)


def days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def sherdog_promotion(event_name: str, ours: dict[str, str]) -> str:
    """'CWFC 88 - Cage Warriors Fighting Championship 88' -> 'CWFC' (see promotion()); a
    label we already use in another case ('Deep' for 'DEEP') becomes ours."""
    label = promotion(event_name.split(" - ")[0])
    label = SHERDOG_PROMOTIONS.get(label, label)
    return ours.get(label.casefold(), label)


# --- what the database already has --------------------------------------------------------

class State:
    """Links (fighter <-> Sherdog id), event links, names, and every non-Sherdog fight."""

    def __init__(self, conn: sqlite3.Connection):
        self.sd, self.fid = {}, {}  # fighter_id -> sherdog id, sherdog id -> fighter_id
        for fid, sid in conn.execute(
                "SELECT DISTINCT fighter_id, source_id FROM fighter_aliases WHERE source = ?", (NAME,)):
            self.sd[fid], self.fid[sid] = sid, fid
        self.events = dict(conn.execute(
            "SELECT source_id, event_id FROM event_aliases WHERE source = ?", (NAME,)))
        self.names = defaultdict(list)
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighter_aliases"):
            self.names[fid].append(name)
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighters"):
            self.names[fid].append(name)
        self.fights = defaultdict(list)  # fighter_id -> [(date, fight_id, event_id, opponent)]
        for fid, eid, day, a, b in conn.execute("""
                SELECT f.fight_id, f.event_id, e.date, p.fighter_id, o.fighter_id
                FROM fights f JOIN events e USING (event_id)
                JOIN fight_participants p ON p.fight_id = f.fight_id
                JOIN fight_participants o ON o.fight_id = f.fight_id AND o.corner <> p.corner
                WHERE f.source <> ? AND e.date IS NOT NULL""", (NAME,)):
            self.fights[a].append((day, fid, eid, b))
        self.new_links: list[tuple[str, str]] = []  # (fighter_id, sherdog id) linked this run
        self.conflicts: list[tuple] = []
        self.witnesses = defaultdict(set)  # (fighter_id, sherdog id) -> pages that pair them

    def link(self, fid: str, sid: str, why: str) -> bool:
        """Link a fighter to a Sherdog id unless the id is someone else's. A fighter already
        linked can take a second id: Sherdog sometimes has a duplicate profile for one fight.
        Returns True if `fid` is newly linked (their page is worth reading)."""
        have_sid, have_fid = self.sd.get(fid), self.fid.get(sid)
        if have_fid == fid:
            return False
        if have_fid is not None and not have_fid.startswith(PREFIX):
            self.conflicts.append((fid, sid, have_sid or "", have_fid, why))
            return False
        self.fid[sid] = fid
        self.new_links.append((fid, sid))
        if have_sid is None:
            self.sd[fid] = sid
            return True
        return False

    def match(self, fid: str, fight: dict, crowded: bool = False) -> tuple | None:
        """The database fight this Sherdog fight of `fid` is: (date, fight_id, event_id,
        opponent), or None. Their only fight within a day of it is it, unless either side
        has several that day (`crowded`: a tournament on Sherdog, maybe only partly on UFC
        Stats); then the opponent's name decides."""
        if not fight["date"]:
            return None
        cands = [c for c in self.fights.get(fid, []) if days_apart(c[0], fight["date"]) <= 1]
        if len(cands) > 1 or crowded:
            cands = [c for c in cands if name_ok(fight["opponent"], self.names[c[3]])]
        return cands[0] if len(cands) == 1 else None

    def named_match(self, fid: str, fight: dict) -> tuple | None:
        """match(), when the opponent's names also agree."""
        m = self.match(fid, fight)
        return m if m and name_ok(fight["opponent"], self.names[m[3]]) else None


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

    def get(self, sid: str) -> dict | None:
        """A fighter's page, fetched fresh unless `cached` and it's on disk already."""
        path = self.dir / f"{sid}.html"
        if self.fetcher is not None and not (self.cached and path.exists()):
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


def follow(st: State, fid: str, page: dict) -> list[str]:
    """Link the opponents in `page` (fighter `fid`'s) through the fights we share; record
    their events. Returns the fighters newly linked."""
    out = []
    for f in page["fights"]:
        m = st.match(fid, f, crowded(page, f))
        if m is None or not f["opponent_id"]:
            continue
        if f["event_id"]:
            st.events.setdefault(f["event_id"], m[2])
        # Names that don't agree (a ring name, a mononym) need a second page to say the same.
        seen = st.witnesses[(m[3], f["opponent_id"])]
        seen.add(fid)
        if not name_ok(f["opponent"], st.names[m[3]]) and len(seen) < 2:
            continue
        if st.link(m[3], f["opponent_id"], f"opponent of {fid} on {f['date']}"):
            out.append(m[3])
    return out


def verify_seed(st: State, fid: str, page: dict) -> bool:
    """Is this Sherdog page fighter `fid`'s? Two shared fights, or one if that's all they have."""
    shared = sum(st.named_match(fid, f) is not None for f in page["fights"])
    return shared >= 2 or (shared == 1 and len(st.fights[fid]) == 1
                           and norm(page["name"]) in {norm(n) for n in st.names[fid]})


def crawl(st: State, pages: Pages, scope: set[str], limit: int | None = None) -> dict[str, dict]:
    """Read the pages of every linked fighter in `scope` (and of anyone linked on the way),
    seeding unlinked ones from the sitemap. Returns {fighter_id: parsed page}. `limit` caps
    the pages read (for trying it out)."""
    read: dict[str, dict] = {}
    names = None
    tried: set[str] = set()

    def walk(queue: list[str]) -> None:
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
    for fid in sorted(scope, key=lambda f: -len(st.fights[f])):
        if limit is not None and len(read) >= limit:
            break
        if fid in st.sd or fid in tried or not st.fights[fid]:
            continue
        tried.add(fid)
        if names is None:
            log.info("Reading Sherdog's fighter sitemap for seeds.")
            names = sitemap(pages.fetcher)
        for sid in {s for n in st.names[fid] for s in names.get(norm(n), [])}:
            if sid in st.fid and not st.fid[sid].startswith(PREFIX):
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
    events both sources share (event_aliases)."""
    votes = defaultdict(lambda: defaultdict(int))
    for sid, label in conn.execute(
            "SELECT a.source_id, e.promotion FROM event_aliases a JOIN events e USING (event_id) "
            "WHERE a.source = ? AND e.source <> ?", (NAME, NAME)):
        info = pages.event(sid)
        if info and info["org_id"]:
            votes[info["org_id"]][label] += 1
    return {org: max(v, key=v.get) for org, v in votes.items()}


def label_events(pages: Pages, labels: Labels, event_ids: list[str]) -> list[tuple]:
    """(promotion, location, event_id) for these `sherdog:` events, from their pages; an
    event whose page can't be read keeps the label from its name."""
    out = []
    for i, eid in enumerate(event_ids, 1):
        info = pages.event(eid[len(PREFIX):])
        if info and info["org_id"]:
            out.append((labels.label(info["org_id"], info["org"]), info["location"] or None, eid))
        if i % 1000 == 0:
            log.info("  event pages: %d/%d", i, len(event_ids))
    return out


# --- writing ------------------------------------------------------------------------------

def merge(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Move every row of fighter `old` to `new`, and leave a redirect."""
    for table in ("fight_participants", "round_stats", "judge_scores", "odds", "fighter_aliases"):
        conn.execute(f"UPDATE OR IGNORE {table} SET fighter_id = ? WHERE fighter_id = ?", (new, old))
        conn.execute(f"DELETE FROM {table} WHERE fighter_id = ?", (old,))
    conn.execute("UPDATE fighter_redirects SET new_id = ? WHERE new_id = ?", (new, old))
    conn.execute("INSERT OR REPLACE INTO fighter_redirects (old_id, new_id) VALUES (?,?)", (old, new))
    conn.execute("DELETE FROM fighters WHERE fighter_id = ?", (old,))


def write(conn: sqlite3.Connection, st: State, read: dict[str, dict], pages: Pages | None = None,
          relabel: bool = False) -> dict:
    counts = defaultdict(int)
    fighters, events, fights, parts, aliases = {}, {}, {}, [], set()
    ours = {p.casefold(): p for (p,) in conn.execute("SELECT promotion FROM promotions")}
    for fid, page in read.items():
        sid = st.sd[fid]
        aliases.add((fid, NAME, sid, page["name"]))
        for f in page["fights"]:
            if not (f["date"] and f["opponent_id"] and f["event_id"]):
                continue
            opp_sid = f["opponent_id"]
            opp = st.fid.get(opp_sid, minted(opp_sid))
            aliases.add((opp, NAME, opp_sid, f["opponent"]))
            if st.match(fid, f, crowded(page, f)) is not None:
                continue  # the database has it
            if opp.startswith(PREFIX):
                fighters.setdefault(opp, f["opponent"])
            eid = st.events.get(f["event_id"], minted(f["event_id"]))
            if eid.startswith(PREFIX):
                events[eid] = (eid, f["event"], f["date"], None, sherdog_promotion(f["event"], ours), NAME)
            a, b = sorted((sid, opp_sid), key=int)
            fight_id = f"{PREFIX}{f['event_id']}:{a}-{b}"
            if fight_id in fights:
                continue
            nc = f["result"] == "NC"
            fights[fight_id] = (fight_id, eid, None, int(f["title"]), None, f["method"] or None,
                                int(f["round"]) if f["round"].isdigit() else None,
                                f["time"] or None, None, f["referee"] or None, None, 0, int(nc), NAME)
            other = {"W": "L", "L": "W"}.get(f["result"], f["result"])
            ids = sorted([(fid, f["result"]), (opp, other)])  # corner order carries no meaning
            parts += [(fight_id, x, corner, r) for corner, (x, r) in enumerate(ids)]
    with conn:
        for fid, sid in st.new_links:  # a `sherdog:` fighter found on UFC Stats
            if conn.execute("SELECT 1 FROM fighters WHERE fighter_id = ? UNION ALL SELECT 1 FROM "
                            "fighter_aliases WHERE fighter_id = ?", (minted(sid),) * 2).fetchone():
                merge(conn, minted(sid), fid)
                counts["merged"] += 1
        conn.executemany("INSERT OR IGNORE INTO fighters (fighter_id, name, source) VALUES (?,?,?)",
                         [(fid, name, NAME) for fid, name in fighters.items()])
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, sid) for sid, eid in st.events.items() if not eid.startswith(PREFIX)])
        # Events now known to be the database's own: move their Sherdog fights over.
        for sid, eid in st.events.items():
            move_event(conn, minted(sid), eid)
        # New events (all of them with `relabel`) get their promotion from their page.
        have = {r[0] for r in conn.execute("SELECT event_id FROM events WHERE source = ?", (NAME,))}
        todo = sorted(have | events.keys() if relabel else events.keys() - have)
        if pages is not None and todo:
            labels = Labels(conn, org_hints(conn, pages) if relabel else None)
            found = {eid: (label, loc) for label, loc, eid in label_events(pages, labels, todo)}
            events.update({eid: (*e[:3], found[eid][1], found[eid][0], NAME)
                           for eid, e in events.items() if eid in found})
            conn.executemany("UPDATE events SET promotion = ?, location = ? WHERE event_id = ?",
                             [(label, loc, eid) for eid, (label, loc) in found.items()])
            conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                             sorted({(label,) for label, _ in labels.new.values()}))
            conn.executemany("INSERT OR REPLACE INTO promotion_aliases (promotion, source, source_id, name) "
                             "VALUES (?,?,?,?)", [(label, NAME, org, name)
                                                  for org, (label, name) in labels.new.items()])
            counts["new promotions"] = len(labels.new)
        conn.executemany("INSERT OR IGNORE INTO promotions (promotion, coverage) VALUES (?, 'partial')",
                         sorted({(e[4],) for e in events.values()}))
        conn.executemany("INSERT OR IGNORE INTO events (event_id, name, date, location, promotion, source) "
                         "VALUES (?,?,?,?,?,?)", events.values())
        # Each read fighter's Sherdog fights are replaced by what their page lists now, except
        # on events whose whole card was read (the card says what was on it).
        for fid in read:
            for (old,) in conn.execute(
                    "SELECT f.fight_id FROM fights f JOIN fight_participants p USING (fight_id) "
                    "WHERE p.fighter_id = ? AND f.source = ? AND f.event_id NOT IN "
                    "(SELECT event_id FROM event_cards)", (fid, NAME)).fetchall():
                if old not in fights:
                    conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (old,))
                    conn.execute("DELETE FROM fights WHERE fight_id = ?", (old,))
        # A fight a card already gave keeps what only the card has (weight class).
        conn.executemany("""
            INSERT INTO fights (fight_id, event_id, weight_class, title_fight, scheduled_rounds, method,
                end_round, end_time, time_format, referee, details, overturned, no_contest, source,
                bout_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pro')
            ON CONFLICT (fight_id) DO UPDATE SET event_id = excluded.event_id,
                title_fight = excluded.title_fight, method = coalesce(excluded.method, method),
                end_round = coalesce(excluded.end_round, end_round),
                end_time = coalesce(excluded.end_time, end_time),
                referee = coalesce(excluded.referee, referee), overturned = excluded.overturned,
                no_contest = excluded.no_contest, bout_type = 'pro'""", fights.values())
        mark_bout_types(conn, {st.sd[fid]: page for fid, page in read.items()})
        default_pro(conn)
        conn.executemany("DELETE FROM fight_participants WHERE fight_id = ?", [(f,) for f in fights])
        conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) "
                         "VALUES (?,?,?,?)", parts)
        counts["duplicates dropped"] = drop_duplicates(conn)
        conn.execute("DELETE FROM fighter_aliases WHERE fighter_id NOT IN (SELECT fighter_id FROM fighters)")
        drop_empty_events(conn)
    counts.update(fights=len(fights), new_fighters=len(fighters), new_links=len(st.new_links))
    return counts


def fight_id(sid: str, f: dict) -> str | None:
    """The id of fight `f` on Sherdog fighter `sid`'s page."""
    if not (f["event_id"] and f["opponent_id"]):
        return None
    a, b = sorted((sid, f["opponent_id"]), key=int)
    return f"{PREFIX}{f['event_id']}:{a}-{b}"


def mark_bout_types(conn: sqlite3.Connection, pages: dict[str, dict]) -> set[str]:
    """Set bout_type on the Sherdog fights these pages ({sherdog id: parsed page}) list, by the
    section they're in. Returns the fight ids listed."""
    rows = [(kind, fight_id(sid, f), NAME) for sid, page in pages.items()
            for kind, key in (("pro", "fights"), ("exhibition", "exhibition"), ("amateur", "amateur"))
            for f in page.get(key, []) if fight_id(sid, f)]
    conn.executemany("UPDATE fights SET bout_type = ? WHERE fight_id = ? AND source = ?", rows)
    return {r[1] for r in rows}


def default_pro(conn: sqlite3.Connection) -> None:
    """Sherdog fights not from a card came from a fighter page's pro section."""
    conn.execute("UPDATE fights SET bout_type = 'pro' WHERE source = ? AND bout_type IS NULL AND "
                 "event_id NOT IN (SELECT event_id FROM event_cards)", (NAME,))


def move_event(conn: sqlite3.Connection, old: str, new: str) -> None:
    """Move a `sherdog:` event's fights (and its card) to the database event it turned out to be."""
    conn.execute("UPDATE fights SET event_id = ? WHERE event_id = ?", (new, old))
    conn.execute("UPDATE OR REPLACE event_cards SET event_id = ? WHERE event_id = ?", (new, old))


def drop_empty_events(conn: sqlite3.Connection) -> None:
    """Delete `sherdog:` events left with no fights, and their cards."""
    empty = "SELECT event_id FROM events WHERE source = ? AND event_id NOT IN (SELECT event_id FROM fights)"
    conn.execute(f"DELETE FROM event_cards WHERE event_id IN ({empty})", (NAME,))
    conn.execute(f"DELETE FROM events WHERE event_id IN ({empty})", (NAME,))


def drop_duplicates(conn: sqlite3.Connection) -> int:
    """Sherdog fights between two fighters who have a UFC Stats fight within a day of it."""
    dups = [r[0] for r in conn.execute("""
        SELECT DISTINCT s.fight_id FROM fights s JOIN events se ON se.event_id = s.event_id
        JOIN fight_participants sa ON sa.fight_id = s.fight_id AND sa.corner = 0
        JOIN fight_participants sb ON sb.fight_id = s.fight_id AND sb.corner = 1
        JOIN fight_participants ua ON ua.fighter_id = sa.fighter_id
        JOIN fight_participants ub ON ub.fight_id = ua.fight_id AND ub.fighter_id = sb.fighter_id
        JOIN fights u ON u.fight_id = ua.fight_id AND u.source <> ?
        JOIN events ue ON ue.event_id = u.event_id
        WHERE s.source = ? AND abs(julianday(se.date) - julianday(ue.date)) <= 1""", (NAME, NAME))]
    for fid in dups:
        conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (fid,))
        conn.execute("DELETE FROM fights WHERE fight_id = ?", (fid,))
    return len(dups)


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="read fighters from UFC Stats cards in the last N days (default: 21)")
    p.add_argument("--all", action="store_true",
                   help="every UFC Stats fighter with a fight (~4.5k pages, ~1.5 h)")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-read every page already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--limit", type=int, help="read at most N pages (for trying it out)")
    p.add_argument("--cached", action="store_true",
                   help="reuse fighter pages already on disk; fetch only missing ones")
    p.add_argument("--no-cards", action="store_true",
                   help="skip reading whole cards of the organizations in cards.ORGS")
    p.add_argument("--all-cards", action="store_true",
                   help="list every event of those organizations, not just their latest 100")
    p.add_argument("--classify-all", action="store_true",
                   help="check every card fight's bout type on fighter pages, not just recent cards'")
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
        queue = sorted(f for f, s in st.sd.items() if s in on_disk and not f.startswith(PREFIX))
        while queue:  # links found on one page make more pages usable
            fid = queue.pop()
            if fid in read:
                continue
            read[fid] = pages.get(st.sd[fid])
            queue += [f for f in follow(st, fid, read[fid]) if st.sd[f] in on_disk]
    else:
        since = "0000" if args.all else (date.today() - timedelta(days=args.recent_days)).isoformat()
        scope = {r[0] for r in conn.execute(
            "SELECT DISTINCT p.fighter_id FROM fight_participants p JOIN fights f USING (fight_id) "
            "JOIN events e USING (event_id) WHERE f.source = 'ufcstats' AND e.date >= ?", (since,))}
        log.info("Sherdog: %d fighters in scope, %d already linked.",
                 len(scope), len(scope & st.sd.keys()))
        read = crawl(st, pages, scope, args.limit)
    counts = write(conn, st, read, pages, args.relabel)
    if not (args.offline or args.relabel or args.no_cards):
        from canon_db.sources.sherdog import cards  # it builds on this module
        c = cards.crawl(conn, pages, args.recent_days, args.all_cards)
        log.info("Sherdog cards: %s.", ", ".join(f"{k} {v}" for k, v in c.items()))
        since = None if args.classify_all else (date.today() - timedelta(days=args.recent_days)).isoformat()
        c = cards.classify(conn, pages, since)
        log.info("Sherdog bout types: %s.", ", ".join(f"{k} {v}" for k, v in c.items()))
    (raw / "conflicts.tsv").unlink(missing_ok=True)
    if st.conflicts:
        with open(raw / "conflicts.tsv", "w", encoding="utf-8") as fh:
            fh.write("fighter_id\tsherdog_id\tfighter_has\tsherdog_has\twhy\n")
            fh.writelines("\t".join(c) + "\n" for c in st.conflicts)
    log.info("Sherdog: %d pages read; %d new links, %d merged; %d Sherdog fights written, "
             "%d new fighters, %d new promotions, %d duplicates of UFC Stats fights dropped; "
             "%d conflicts%s.",
             len(read), counts["new_links"], counts["merged"], counts["fights"],
             counts["new_fighters"], counts["new promotions"], counts["duplicates dropped"],
             len(st.conflicts),
             " (see conflicts.tsv)" if st.conflicts else "")
