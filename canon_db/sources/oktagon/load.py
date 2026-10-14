"""Oktagon loader: the stats Oktagon logs for its own events, attached to fights the database
already has (Oktagon's cards come from Sherdog).

Oktagon logs every strike, takedown and submission attempt of a fight. Those logs stay on
disk (data/raw/oktagon/matches/); the database takes their per-round totals, in round_stats
with `source = 'oktagon'`. Only what the log reliably has is filled: strikes landed (all,
significant, and significant by target), takedowns landed, submission attempts. Attempts,
knockdowns, control time and position are NULL. Sofascore counts more (attempts, control),
so a fight it has gets its stats instead (sofascore/load.py), and this loader skips it.

A bout is matched to our fight like a PFL bout (see pfl/load.py): date within a day and
both names, then the one unclaimed fight on the linked event that has one of its fighters.
That links Oktagon's fighter ids (fighter_aliases) and the event (event_aliases, the
tournament id). The stats host also holds test tournaments (made-up logs of real bouts);
anything named "test" is left out. Matched fighters also get a missing `dob`, and their `sex` from the bout's
gender on Oktagon's fight card.

Which events:
  default     this year's events not loaded yet, and those from the last --recent-days days again
  --all       every event since 2005 (files already on disk are reused; --refresh fetches again)
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
from canon_db.sources.oktagon.fetch import fetch_cards, fetch_tournament, tournaments
from canon_db.sources.oktagon.pages import COLS, card_fights, fighters, rounds
from canon_db.sources.pfl.fetch import Site
from canon_db.sources.pfl.load import leftover

NAME = "oktagon"
FIRST_YEAR = 2005

log = logging.getLogger(NAME)


def is_test(name: str | None) -> bool:
    """Is this one of the stats host's test tournaments ("OKTAGON 9999 test", "Testovací věci")?"""
    return "test" in (name or "").lower()


def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(conn: sqlite3.Connection, raw: Path) -> Counter:
    """Load every tournament on disk."""
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

    aliases, event_alias, rows, fights, dobs = set(), {}, [], set(), {}
    by_match: dict[int, tuple] = {}   # stats-host match id -> (our fight, {Oktagon fighter id: ours})
    legacy: dict[int, int] = {}       # a match's older id -> its match id
    for path in sorted((raw / "tournaments").glob("*.json"), key=lambda p: int(p.stem)):
        t = read(path)
        if not t or not t.get("date"):
            continue
        first = next(iter(t.get("matches") or []), None)
        if first and is_test((read(raw / "matches" / f"{first['id']}.json") or {}).get("tournamentName")):
            continue
        counts["events read"] += 1
        day, matched, hits, rest = t["date"][:10], {}, Counter(), []
        for m in t.get("matches") or []:
            who = [dict(slug=f["id"], name=f["name"]) for f in fighters(m)]
            fight_id, ids = matcher.match(dict(date=day, fighters=who))
            if fight_id is None:
                rest.append((m, who))
                continue
            matched[m["id"]] = (fight_id, ids)
            hits[event_of[fight_id]] += 1
        if hits:
            event_alias[path.stem] = eid = hits.most_common(1)[0][0]
            for m, who in rest:
                taken = {f for f, _ in matched.values()}
                found = leftover(who, [(k, v) for k, v in card.get(eid, {}).items()
                                       if k not in taken and len(v) == 2])
                if found:
                    matched[m["id"]] = found
        for m in t.get("matches") or []:
            counts["bouts"] += 1
            if m["id"] not in matched:
                counts["bouts not matched"] += 1
                continue
            fight_id, ids = by_match[m["id"]] = matched[m["id"]]
            log_ = read(raw / "matches" / f"{m['id']}.json") or m
            if log_.get("externalId") is not None:
                legacy[log_["externalId"]] = m["id"]
            for f in fighters(log_):
                if f["id"] in ids:
                    aliases.add((ids[f["id"]], NAME, f["id"], f["name"]))
                    if f["dob"]:
                        dobs[ids[f["id"]]] = f["dob"]
            totals = rounds(log_)
            if not totals:
                continue
            counts["bouts with stats"] += 1
            if fight_id in has_stats:
                counts["bouts another source already has"] += 1
                continue
            fights.add(fight_id)
            rows += [(fight_id, ids[r["fighter"]], r["round"], *(r[c] for c in COLS), NAME)
                     for r in totals if r["fighter"] in ids]

    sexes: dict[str, str] = {}
    for path in sorted((raw / "fightcards").glob("*.json")) if (raw / "fightcards").exists() else []:
        for f in card_fights(read(path) or []):
            match_id = f["stats_id"] if f["stats_id"] in by_match else legacy.get(f["legacy_id"])
            if f["sex"] and match_id in by_match:
                for fid in by_match[match_id][1].values():
                    sexes[fid] = f["sex"]

    with conn:
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, tid) for tid, eid in event_alias.items()])
        for fid in {f for f, _ in by_match.values()}:  # each fight's rows are replaced by what's on disk
            conn.execute("DELETE FROM round_stats WHERE fight_id = ? AND source = ?", (fid, NAME))
        conn.executemany(
            f"INSERT OR REPLACE INTO round_stats (fight_id, fighter_id, round, {', '.join(COLS)}, source) "
            f"VALUES ({', '.join('?' * (len(COLS) + 4))})", rows)
        before = conn.total_changes
        conn.executemany("UPDATE fighters SET dob = ? WHERE fighter_id = ? AND dob IS NULL",
                         [(d, fid) for fid, d in dobs.items()])
        counts["birth dates filled"] = conn.total_changes - before
        before = conn.total_changes
        conn.executemany("UPDATE fighters SET sex = ? WHERE fighter_id = ? AND sex IS NULL",
                         [(s, fid) for fid, s in sexes.items()])
        counts["sex filled"] = conn.total_changes - before
    counts.update({"events linked": len(event_alias), "fights with stats": len(fights), "round_stats rows": len(rows)})
    return counts


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="fetch events from the last N days again (default: 21)")
    p.add_argument("--all", action="store_true", help="every event (~1,100 matches, ~25 min the first time)")
    p.add_argument("--refresh", action="store_true", help="with --all: fetch files already on disk too")
    p.add_argument("--offline", action="store_true", help="no network: load what is on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    if not args.offline:
        site = Site(args.delay)
        today = date.today()
        since = (today - timedelta(days=args.recent_days)).isoformat()
        years = list(range(FIRST_YEAR, today.year + 1)) if args.all else sorted({int(since[:4]), today.year})
        listed = [t for t in tournaments(site, years)
                  if t["date"][:10] <= today.isoformat() and not is_test(t.get("name"))]
        known = {r[0] for r in conn.execute("SELECT source_id FROM event_aliases WHERE source = ?", (NAME,))}
        todo = [t for t in listed if args.all or str(t["id"]) not in known or t["date"][:10] >= since]
        log.info("Oktagon: %d events listed, %d to fetch.", len(listed), len(todo))
        for t in todo:
            fetch_tournament(site, raw, t["id"], fresh=args.refresh or t["date"][:10] >= since)
        fetch_cards(site, raw, since, today.isoformat(), every=args.all)
    counts = load(conn, raw)
    log.info("Oktagon: %s.", ", ".join(f"{k} {v}" for k, v in counts.items()))
