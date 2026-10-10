"""PFL loader: the stats PFL publishes for its own events, attached to fights the database
already has (PFL's cards come from Sherdog).

A PFL bout is one of our fights when it is the only one within a day of the event's date
whose two fighters' names both match (as odds are matched). That links the PFL fighter ids
(fighter_aliases) and the event (event_aliases). A bout left over (a ring name, a married
name) is the one fight on that event no other bout took that has one of its fighters; the
other fighter is then whoever is left. A bout that still doesn't match isn't loaded.

Events from 2025-12 have stats in round_stats' columns and go there with `source = 'pfl'`
(a fight that already has round stats from another source keeps them). Earlier events have
PFL's SmartCage stats, a different set, in smartcage_round_stats.

Which events:
  default     events not loaded yet, and those from the last --recent-days days again
  --all       every event (pages already on disk are reused; --refresh fetches them again)
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
from canon_db.sources.pfl.fetch import Site, fetch
from canon_db.sources.pfl.pages import V1, V2, parse_event, parse_stats
from canon_db.sources.sherdog.load import name_ok

NAME = "pfl"
ROUND_COLS = ["ctrl_sec", *V2]
SMARTCAGE_COLS = list(V1)

log = logging.getLogger(NAME)


def leftover(who: list[dict], free: list[tuple]) -> tuple | None:
    """(fight_id, {PFL id: fighter_id}) for a bout whose names didn't both match: the one
    fight in `free` ([(fight_id, [(fighter_id, names)] x2)], the event's fights no bout took)
    that has more of its fighters than any other."""
    best = []
    for fight_id, sides in free:
        for (a, b) in (sides, sides[::-1]):
            score = name_ok(who[0]["name"], a[1]) + name_ok(who[1]["name"], b[1])
            if score:
                best.append((score, fight_id, {who[0]["slug"]: a[0], who[1]["slug"]: b[0]}))
    best.sort(key=lambda t: -t[0])
    top = [t for t in best if t[0] == best[0][0]] if best else []
    return (top[0][1], top[0][2]) if len({t[1] for t in top}) == 1 and len(top) == 1 else None


def load(conn: sqlite3.Connection, raw: Path) -> Counter:
    """Load every event on disk that has stats."""
    counts = Counter()
    matcher = Matcher(conn)
    event_of = dict(conn.execute("SELECT fight_id, event_id FROM fights"))
    has_stats = {r[0] for r in conn.execute("SELECT DISTINCT fight_id FROM round_stats WHERE source <> ?", (NAME,))}
    names: dict[str, list[str]] = {}
    for fid, name in conn.execute("SELECT fighter_id, name FROM fighters UNION SELECT fighter_id, name "
                                  "FROM fighter_aliases"):
        names.setdefault(fid, []).append(name)
    card: dict[str, dict[str, list]] = {}  # event_id -> fight_id -> [(fighter_id, names)]
    for eid, fight_id, fid in conn.execute("SELECT f.event_id, f.fight_id, p.fighter_id FROM fights f "
                                           "JOIN fight_participants p USING (fight_id) ORDER BY p.corner"):
        card.setdefault(eid, {}).setdefault(fight_id, []).append((fid, names.get(fid, [])))
    aliases, event_alias, rounds, smartcage, fights = set(), {}, [], [], set()
    folders = sorted(p for p in raw.iterdir() if (p / "stats.json").exists() and (p / "event.html").exists())
    for folder in folders:
        event = parse_event((folder / "event.html").read_text(encoding="utf-8"))
        try:
            listed = json.loads((folder / "stats.json").read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not event["date"] or not isinstance(listed, list):
            continue
        counts["events read"] += 1
        # Every bout on the card links the event and its fighters, with stats or not.
        matched, hits, rest = {}, Counter(), []  # the pair of PFL fighter ids -> (our fight, {PFL id: ours})
        for f in listed:
            people = f.get("Fighters") or []
            if len(people) != 2:
                continue
            who = [dict(slug=str(p["Id"]), name=f"{p.get('FirstName') or ''} {p.get('LastName') or ''}".strip())
                   for p in people]
            fight_id, ids = matcher.match(dict(date=event["date"], fighters=who))
            if fight_id is None:
                rest.append(who)
                continue
            matched[frozenset(w["slug"] for w in who)] = (fight_id, ids)
            hits[event_of[fight_id]] += 1
        if hits:
            event_alias[folder.name] = eid = hits.most_common(1)[0][0]
            for who in rest:
                taken = {m[0] for m in matched.values()}
                found = leftover(who, [(k, v) for k, v in card.get(eid, {}).items()
                                       if k not in taken and len(v) == 2])
                if found:
                    matched[frozenset(w["slug"] for w in who)] = found
                    counts["bouts matched by one fighter"] += 1
        label = {str(p["Id"]): f"{p.get('FirstName') or ''} {p.get('LastName') or ''}".strip()
                 for f in listed for p in f.get("Fighters") or []}
        for _, ids in matched.values():
            aliases.update((ours, NAME, pfl_id, label[pfl_id]) for pfl_id, ours in ids.items())
        for bout in parse_stats(listed, event["v2"]):
            counts["bouts with stats"] += 1
            found = matched.get(frozenset(x["id"] for x in bout["fighters"]))
            if found is None:
                counts["bouts not matched"] += 1
                log.info("  %s (%s): no fight of ours for %s vs %s", folder.name, event["date"],
                         *(x["name"] for x in bout["fighters"]))
                continue
            fight_id, ids = found
            fights.add(fight_id)
            for r in bout["rounds"]:
                key = (fight_id, ids[r["fighter"]], r["round"])
                if not event["v2"]:
                    smartcage.append((*key, *(r[c] for c in SMARTCAGE_COLS), NAME))
                elif fight_id in has_stats:
                    counts["rounds another source already has"] += 1
                else:
                    rounds.append((*key, *(r[c] for c in ROUND_COLS), NAME))
    with conn:
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, tag) for tag, eid in event_alias.items()])
        for fid in fights:  # each fight's rows are replaced by what the site has now
            conn.execute("DELETE FROM round_stats WHERE fight_id = ? AND source = ?", (fid, NAME))
            conn.execute("DELETE FROM smartcage_round_stats WHERE fight_id = ?", (fid,))
        conn.executemany(
            f"INSERT OR REPLACE INTO round_stats (fight_id, fighter_id, round, {', '.join(ROUND_COLS)}, source) "
            f"VALUES ({', '.join('?' * (len(ROUND_COLS) + 4))})", rounds)
        conn.executemany(
            f"INSERT OR REPLACE INTO smartcage_round_stats (fight_id, fighter_id, round, "
            f"{', '.join(SMARTCAGE_COLS)}, source) VALUES ({', '.join('?' * (len(SMARTCAGE_COLS) + 4))})",
            smartcage)
    counts.update({"events linked": len(event_alias), "fights with stats": len(fights),
                   "round_stats rows": len(rounds), "smartcage rows": len(smartcage)})
    return counts


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="fetch events from the last N days again (default: 21)")
    p.add_argument("--all", action="store_true", help="every event on the site (~150 events, ~15 min)")
    p.add_argument("--refresh", action="store_true", help="with --all: fetch events already on disk too")
    p.add_argument("--offline", action="store_true", help="no network: load what is on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--rounds", type=int, help="times to ask for stats that aren't ready "
                                              "(default: 3, or 20 with --all)")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    if not args.offline:
        site = Site(args.delay)
        tags = site.events()
        if not tags:
            log.warning("PFL: couldn't read the events page; nothing fetched.")
        since = (date.today() - timedelta(days=args.recent_days)).isoformat()
        known = dict(conn.execute("SELECT a.source_id, e.date FROM event_aliases a JOIN events e USING (event_id) "
                                  "WHERE a.source = ?", (NAME,)))
        recent = [t for t in tags if known.get(t, "") >= since]
        if args.all:
            todo = [t for t in tags if args.refresh or not (raw / t / "stats.json").exists()]
        else:
            todo = [t for t in tags if t not in known]
        log.info("PFL: %d events listed, %d to fetch, %d recent to fetch again.", len(tags), len(todo), len(recent))
        fetch(site, raw, [t for t in todo if t not in recent], args.rounds or (20 if args.all else 3), args.refresh)
        fetch(site, raw, recent, args.rounds or 3, fresh=True)
    counts = load(conn, raw)
    log.info("PFL: %s.", ", ".join(f"{k} {v}" for k, v in counts.items()))
