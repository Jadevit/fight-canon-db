"""MMA Decisions loader: judges' cards and media scores of decisions, attached to fights the
database already has.

MMA Decisions copies each decision's official cards (every judge, round by round) and
collects journalists' scores of the fight. Cards go in judge_scores with `source =
'mmadecisions'`, unless the fight already has official card images ('ufc.com'): a fight's
cards come from one source. A card is kept only if every round has both scores and the
rounds add up to its totals. Judges' names take UFC Stats' spelling where the fight's UFC
Stats details name the judge (by surname). Media scores go in media_scores, one row per
writer per fighter; a score whose higher side isn't the writer's stated pick is left out.

A decision is matched to our fight by date (within a day) and both names, and only if our
result agrees: the page's winner won, or both drew. That links MMA Decisions' fighter ids
(fighter_aliases, if the name agrees) and the event (event_aliases).

Which events:
  default     this year's events not on disk, and those from the last --recent-days days again
  --all       every year since 1995 (pages already on disk are reused; --refresh fetches again)
  --offline   no network: load what is on disk
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from collections import Counter
from pathlib import Path

from canon_db.matching import Matcher, name_ok, norm
from canon_db.sources.mmadecisions.pages import decision

NAME = "mmadecisions"
OFFICIAL_CARDS = "ufc.com"  # card images: a fight with these keeps them
log = logging.getLogger(NAME)


def valid(card: dict) -> bool:
    """Every round has both scores, and they add up to the totals (when the page gives them)."""
    rounds = card["rounds"]
    if not rounds or any(a is None or b is None for _, a, b in rounds):
        return False
    if None in card["totals"]:
        return True
    return (sum(r[1] for r in rounds), sum(r[2] for r in rounds)) == tuple(card["totals"])


def ufcstats_judges(details: str | None) -> dict[str, str]:
    """Surname -> UFC Stats' spelling of the judges in a fight's details ("Name 29 - 28. ...")."""
    names = re.findall(r"([^.\d]+?)\s+\d+\s*-\s*\d+", details or "")
    return {norm(n).split()[-1]: n.strip() for n in names if norm(n)}


def load(conn: sqlite3.Connection, raw: Path) -> Counter:
    counts = Counter()
    matcher = Matcher(conn)
    results: dict[int, dict[int, str]] = {}
    for fight, fighter, res in conn.execute("SELECT fight_id, fighter_id, result FROM fight_participants"):
        results.setdefault(fight, {})[fighter] = res
    details = dict(conn.execute("SELECT fight_id, details FROM fights WHERE details IS NOT NULL"))
    official = {r[0] for r in conn.execute("SELECT DISTINCT fight_id FROM judge_scores WHERE source <> ?", (NAME,))}
    names: dict[int, list[str]] = {}
    for fid, name in conn.execute("SELECT fighter_id, name FROM fighters UNION SELECT fighter_id, name "
                                  "FROM fighter_aliases"):
        names.setdefault(fid, []).append(name)
    event_of = dict(conn.execute("SELECT fight_id, event_id FROM fights"))

    cards, media, aliases, event_alias, seen = [], [], set(), {}, set()
    for path in sorted((raw / "decisions").glob("*.html"), key=lambda p: int(p.stem)):
        d = decision(path.read_text(encoding="utf-8"))
        if d is None:
            counts["pages not a decision"] += 1
            continue
        counts["decisions"] += 1
        fight, ids = matcher.match({"date": d["date"], "fighters": d["fighters"]})
        if fight is None:
            counts["decisions not matched"] += 1
            continue
        a, b = (ids[f["slug"]] for f in d["fighters"])
        ours = results.get(fight, {})
        draw = "draw" in (d["method"] or "").lower()
        if (ours.get(a), ours.get(b)) != (("D", "D") if draw else ("W", "L")):
            counts["decisions whose result disagrees"] += 1
            continue
        if fight in seen:
            counts["fights listed twice"] += 1
            continue
        seen.add(fight)
        counts["decisions matched"] += 1
        for f in d["fighters"]:
            if name_ok(f["name"], names.get(ids[f["slug"]], [])):
                aliases.add((ids[f["slug"]], NAME, f["slug"], f["name"]))
        event_alias[d["event"]["id"]] = event_of[fight]

        if fight in official:
            counts["fights with official card images"] += 1
        else:
            spelled = ufcstats_judges(details.get(fight))
            kept = [c for c in d["judges"] if valid(c)]
            for c in d["judges"]:
                if c not in kept:  # many pages give only totals
                    counts["cards without round scores" if any(None in r for r in c["rounds"])
                           else "cards whose rounds don't add up"] += 1
            for c in kept:
                judge = spelled.get((norm(c["name"]).split() or [""])[-1], c["name"])
                for n, sa, sb in c["rounds"]:
                    cards += [(fight, a, n, judge, sa, NAME), (fight, b, n, judge, sb, NAME)]
            counts["fights with cards"] += bool(kept)

        for m in d["media"]:
            high = 0 if m["score"][0] > m["score"][1] else 1 if m["score"][1] > m["score"][0] else None
            if high != m["pick"]:
                counts["media scores whose pick disagrees"] += 1
                continue
            for fighter, score in zip((a, b), m["score"]):
                media.append((fight, fighter, m["writer"], m["outlet"], score, m["url"], NAME))
        counts["fights with media scores"] += bool(d["media"])

    with conn:
        conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, name) "
                         "VALUES (?,?,?,?)", sorted(aliases))
        conn.executemany("INSERT OR REPLACE INTO event_aliases (event_id, source, source_id) VALUES (?,?,?)",
                         [(eid, NAME, sid) for sid, eid in event_alias.items()])
        for fight in seen:  # each fight read here is replaced by what's on disk; the rest stay
            conn.execute("DELETE FROM judge_scores WHERE fight_id = ? AND source = ?", (fight, NAME))
            conn.execute("DELETE FROM media_scores WHERE fight_id = ? AND source = ?", (fight, NAME))
        conn.executemany("INSERT OR REPLACE INTO judge_scores (fight_id, fighter_id, round, judge, score, source) "
                         "VALUES (?,?,?,?,?,?)", cards)
        conn.executemany("INSERT OR REPLACE INTO media_scores (fight_id, fighter_id, writer, outlet, score, url, "
                         "source) VALUES (?,?,?,?,?,?,?)", media)
    counts.update({"judge_scores rows": len(cards), "media_scores rows": len(media)})
    return counts


# --- entry points (see canon_db.sources) ------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="fetch events from the last N days again (default: 21)")
    p.add_argument("--all", action="store_true", help="every year since 1995 (~15k pages, ~5 h the first time)")
    p.add_argument("--refresh", action="store_true", help="with --all: fetch pages already on disk too")
    p.add_argument("--offline", action="store_true", help="no network: load what is on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    if not args.offline:
        from canon_db.sources.mmadecisions.fetch import fetch
        fetch(raw, args.all, args.refresh, args.recent_days, args.delay)
    counts = load(conn, raw)
    log.info("MMA Decisions: %s.", ", ".join(f"{k} {v}" for k, v in counts.items()))
