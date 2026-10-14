"""Sofascore loader: the stats Sofascore counts for promotions that publish none of their own,
attached to fights the database already has (their cards come from Sherdog).

Sofascore's counts are its own, so they never mix with another source's within a fight. A
fight with official stats (UFC Stats, PFL) is left alone. Oktagon's own stats count only
what landed, so on Oktagon fights Sofascore's replace them; Oktagon's stay on fights
Sofascore doesn't have.

round_stats rows get `source = 'sofascore'` and the columns in pages.ROUND plus ctrl_sec
(clinch and ground control); the rest are NULL (total_str_land among them: Sofascore gives
strikes thrown, not landed). Significant strikes by position (its parts overlap, so they
aren't round_stats' dist/clinch/ground), time at distance, in the clinch and on the ground,
and the two kinds of control, go in sofascore_round_stats. A bout Sofascore lists with all-zero stats
wasn't counted and gets no rows. Nor does a bout whose winner is our loser (its sides may be
swapped), or a fight Sofascore lists twice with different numbers.

A bout is matched to our fight like a PFL bout (see pfl/load.py): date within a day and
both names, then the one unclaimed fight on the linked event that has one of its fighters.
That links Sofascore's fighter ids (fighter_aliases) and the event (event_aliases, the
season id).

Which events:
  default     this year's seasons not linked yet, and those from the last --recent-days days again
  --all       every season (files already on disk are reused; --refresh fetches them again)
  --offline   no network: load what is on disk
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from canon_db.sources.bestfightodds.match import Matcher
from canon_db.sources.pfl.load import leftover
from canon_db.sources.sofascore.pages import EXTRA_COLS, ROUND_COLS, bouts, rounds

NAME = "sofascore"
# Sofascore's promotions (unique tournaments) with no official stats we can get. UFC, DWCS
# and PFL are left out: they have their own.
PROMOTIONS = {
    20254: "Absolute Championship Akhmat",
    20267: "Brave Combat Federation",
    19909: "Cage Warriors",
    20463: "Fight Nation Championship",
    20253: "Jungle Fight Championship",
    19946: "KSW",
    20258: "OKTAGON MMA",
    20269: "ONE Fighting Championship",
    19905: "Rizin",
}
REPLACES = "oktagon"  # the one source whose stats Sofascore's replace

log = logging.getLogger(NAME)


def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(conn: sqlite3.Connection, raw: Path) -> Counter:
    """Load every season on disk."""
    counts = Counter()
    matcher = Matcher(conn)
    event_of = dict(conn.execute("SELECT fight_id, event_id FROM fights"))
    official = {r[0] for r in conn.execute("SELECT DISTINCT fight_id FROM round_stats WHERE source NOT IN (?, ?)",
                                           (NAME, REPLACES))}
    names: dict[str, list[str]] = {}
    for fid, name in conn.execute("SELECT fighter_id, name FROM fighters UNION SELECT fighter_id, name "
                                  "FROM fighter_aliases"):
        names.setdefault(fid, []).append(name)
    card: dict[str, dict[str, list]] = {}  # event_id -> fight_id -> [(fighter_id, names)]
    for eid, fight_id, fid in conn.execute("SELECT f.event_id, f.fight_id, p.fighter_id FROM fights f "
                                           "JOIN fight_participants p USING (fight_id) ORDER BY p.corner"):
        card.setdefault(eid, {}).setdefault(fight_id, []).append((fid, names.get(fid, [])))

    result = {(f, p): r for f, p, r in conn.execute("SELECT fight_id, fighter_id, result FROM fight_participants")}
    aliases, event_alias, copies, touched = set(), {}, {}, set()  # copies: fight -> [(sides, rows)]
    for path in sorted((raw / "events").glob("*.json"), key=lambda p: int(p.stem)):
        season = read(path)
        if not season or season.get("promotion") not in PROMOTIONS:
            continue
        counts["events read"] += 1
        matched, hits, rest = {}, Counter(), []
        for b in bouts(season):
            fight_id, ids = matcher.match(b)
            if fight_id is None:
                rest.append(b)
                continue
            matched[b["id"]] = (fight_id, ids)
            hits[event_of[fight_id]] += 1
        if hits:
            event_alias[path.stem] = eid = hits.most_common(1)[0][0]
            for b in rest:
                taken = {f for f, _ in matched.values()}
                found = leftover(b["fighters"], [(k, v) for k, v in card.get(eid, {}).items()
                                                 if k not in taken and len(v) == 2])
                if found:
                    matched[b["id"]] = found
                    counts["bouts matched by one fighter"] += 1
        for b in bouts(season):
            counts["bouts"] += 1
            if b["id"] not in matched:
                counts["bouts not matched"] += 1
                continue
            fight_id, ids = matched[b["id"]]
            aliases.update((ids[f["slug"]], NAME, f["slug"], f["name"]) for f in b["fighters"])
            stats = read(raw / "stats" / f"{b['id']}.json")
            if stats is None:
                continue
            per_round = rounds(stats)
            touched.add(fight_id)
            if not per_round:
                counts["bouts with all-zero stats"] += 1
                continue
            if fight_id in official:
                counts["bouts with official stats"] += 1
                continue
            side = [ids[f["slug"]] for f in b["fighters"]]
            if b["winner"] is not None and result.get((fight_id, side[1 - b["winner"]])) == "W":
                # Sofascore's winner is our loser: its sides may be the other way round.
                counts["bouts whose winner disagrees"] += 1
                continue
            counts["bouts with stats"] += 1
            copies.setdefault(fight_id, []).append((side, per_round))

    rows, extra, fights = [], [], set()
    for fight_id, found in copies.items():
        if len({json.dumps(c, sort_keys=True) for c in found}) > 1:  # the bout listed twice, counted differently
            counts["fights with differing copies"] += 1
            continue
        fights.add(fight_id)
        side, per_round = found[0]
        for r in per_round:
            key = (fight_id, side[r["side"]], r["round"])
            rows.append((*key, *(r[c] for c in ROUND_COLS), NAME))
            extra.append((*key, *(r[c] for c in EXTRA_COLS), NAME))

    with conn:
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, sid) for sid, eid in event_alias.items()])
        for fid in touched:  # each fight's rows are replaced by what's on disk
            conn.execute("DELETE FROM round_stats WHERE fight_id = ? AND source = ?", (fid, NAME))
            conn.execute("DELETE FROM sofascore_round_stats WHERE fight_id = ?", (fid,))
        replaced = sum(conn.execute("DELETE FROM round_stats WHERE fight_id = ? AND source = ?",
                                    (fid, REPLACES)).rowcount for fid in fights)
        conn.executemany(
            f"INSERT OR REPLACE INTO round_stats (fight_id, fighter_id, round, {', '.join(ROUND_COLS)}, source) "
            f"VALUES ({', '.join('?' * (len(ROUND_COLS) + 4))})", rows)
        conn.executemany(
            f"INSERT OR REPLACE INTO sofascore_round_stats (fight_id, fighter_id, round, {', '.join(EXTRA_COLS)}, "
            f"source) VALUES ({', '.join('?' * (len(EXTRA_COLS) + 4))})", extra)
    counts.update({"events linked": len(event_alias), "fights with stats": len(fights),
                   "round_stats rows": len(rows), f"{REPLACES} rows replaced": replaced})
    return counts


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="fetch events from the last N days again (default: 21)")
    p.add_argument("--all", action="store_true", help="every season (~650 events, ~2 h the first time)")
    p.add_argument("--refresh", action="store_true", help="with --all: fetch files already on disk too")
    p.add_argument("--offline", action="store_true", help="no network: load what is on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    if not args.offline:
        fetch(conn, args, raw)
    counts = load(conn, raw)
    log.info("Sofascore: %s.", ", ".join(f"{k} {v}" for k, v in counts.items()))


def fetch(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    from canon_db.sources.sofascore.fetch import Site, fetch_events, fetch_seasons, fetch_stats

    site = Site(args.delay)
    today = date.today()
    since = (today - timedelta(days=args.recent_days)).isoformat()
    years = {str(today.year), since[:4]}
    linked = dict(conn.execute("SELECT a.source_id, e.date FROM event_aliases a JOIN events e USING (event_id) "
                               "WHERE a.source = ?", (NAME,)))
    with_stats = {r[0] for r in conn.execute("SELECT DISTINCT fight_id FROM round_stats WHERE source = ?", (NAME,))}
    matcher = Matcher(conn)
    for promotion, label in PROMOTIONS.items():
        seasons = fetch_seasons(site, raw, promotion)
        if args.all:
            todo = [s for s in seasons if args.refresh or not (raw / "events" / f"{s['id']}.json").exists()]
        else:
            todo = [s for s in seasons if s.get("year") in years
                    and (str(s["id"]) not in linked or linked[str(s["id"])] >= since)]
        log.info("Sofascore: %s: %d events listed, %d to fetch.", label, len(seasons), len(todo))
        for s in todo:
            fetch_events(site, raw, promotion, s["id"])
        for s in seasons:  # stats of bouts on events we have: those not loaded yet, and recent ones
            season = read(raw / "events" / f"{s['id']}.json")
            listed = bouts(season) if season else []
            found = {b["id"]: matcher.match(b)[0] for b in listed}
            if not any(found.values()):
                continue
            for b in listed:
                path = raw / "stats" / f"{b['id']}.json"
                if args.refresh or b["date"] >= since or not (path.exists() or found[b["id"]] in with_stats):
                    fetch_stats(site, raw, b["id"])
