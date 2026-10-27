"""UFC Stats loader: pick events -> fetch pages -> parse -> write them to the DB.

Each run:
  1. fetches the UFC Stats events list;
  2. picks the events to (re)load: completed events not yet in the database, plus
     every event from the last --recent-days days (stats and corrections are posted
     after fight night). Upcoming events are skipped;
  3. downloads those events' event, fight and fighter pages to data/raw/ufcstats/
     (kept for debugging and --offline re-runs; never committed);
  4. writes each of those events (its event row, fights, participants and round stats)
     and the fighters' bios.

An event is only written if every one of its fight pages downloaded, so a flaky run can
never delete data. UFC Stats' ids are aliases (`ufcstats`) of our rows. A bout whose fight
has no UFC Stats alias yet is placed on one of ours if both fighters are known and fought
within a day of it, or one is and the other's name agrees (that links the other); else it's
a new fight. UFC Stats is a promotion's own site: its values replace other sources'.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from canon_db import ids
from canon_db.http import save
from canon_db.matching import Bouts
from canon_db.sources.ufcstats import fields as F
from canon_db.sources.ufcstats.client import BASE, EVENTS_LIST_URL, Fetcher
from canon_db.sources.ufcstats.pages import (parse_event, parse_events_list, parse_fight,
                                             parse_fighter)

NAME = "ufcstats"
URLS = {"events": "event-details", "fights": "fight-details", "fighters": "fighter-details"}
ROUND_COLS = ("knockdowns", "sig_str_land", "sig_str_att", "total_str_land", "total_str_att", "td_land",
              "td_att", "sub_att", "reversals", "ctrl_sec", "head_land", "head_att", "body_land", "body_att",
              "leg_land", "leg_att", "dist_land", "dist_att", "clinch_land", "clinch_att", "ground_land",
              "ground_att")

log = logging.getLogger("ufcstats")


# --- page source: network (saving to raw/) or raw/ only ---------------------------

class Pages:
    def __init__(self, raw: Path, fetcher: Fetcher | None):
        self.raw, self.fetcher = raw, fetcher
        for kind in URLS:
            (raw / kind).mkdir(parents=True, exist_ok=True)

    def get(self, kind: str, id_: str, prefer_disk: bool = False) -> str | None:
        path = self.raw / kind / f"{id_}.html"
        if self.fetcher is None or (prefer_disk and path.exists()):
            return path.read_text(encoding="utf-8") if path.exists() else None
        html = self.fetcher.get(f"{BASE}/{URLS[kind]}/{id_}")
        if html is not None:
            save(path, html)
        return html


# --- turning one event's pages into rows -------------------------------------------

def _scheduled_rounds(time_format: str) -> int | None:
    return F.parse_int(time_format.split("Rnd")[0]) if "Rnd" in time_format else None


def _round_row(rnd: int, c: dict) -> dict:
    (sig, tot, td, head, body, leg, dist, cl, gr) = [F.parse_of(c.get(k)) for k in (
        "sig", "total", "td", "head", "body", "leg", "distance", "clinch", "ground")]
    vals = (F.parse_int(c.get("kd")), *sig, *tot, *td, F.parse_int(c.get("sub")), F.parse_int(c.get("rev")),
            F.parse_ctrl(c.get("ctrl")), *head, *body, *leg, *dist, *cl, *gr)
    return dict(round=rnd, **dict(zip(ROUND_COLS, vals)))


# Series labelled by how their event names start, ahead of the rules in promotion().
PROMOTION_PREFIXES = {"PRIDE": "PRIDE", "Road to UFC": "Road to UFC",
                      "K-1 Hero's": "HERO'S", "K-1 Dynamite": "Dynamite!!"}

# Promotion names the rule in promotion() gets wrong, keyed by the name it would produce.
PROMOTION_OVERRIDES: dict[str, str] = {
    "K": "K-1", "M": "M-1",                     # the number is part of the name
    "Strikeforce Challengers": "Strikeforce",   # a promotion's own sub-series
    "DWCS Brazil": "DWCS",
    "UCC Hawaii": "UCC",
    "2H2H - Pride & Honor": "2H2H",
    "Brazil Open '": "Brazil Open",
}


# Values UFC Stats has wrong, by UFC Stats fight id: {parse_fight() key: corrected value}.
FIGHT_FIXES: dict[str, dict[str, str]] = {
    "5a91827fe8c1dc26": {"time_format": "3 Rnd (5-5-5)"},  # listed as 10-5-5
}


def promotion(event_name: str) -> str:
    """Promotion of an event missing from UFC Stats' events list, from its name.

    Events on that list are always UFC and don't go through this (early UFC names like
    "Ortiz vs. Shamrock 3" don't say UFC). First PROMOTION_PREFIXES ("PRIDE 33: ..." -> PRIDE),
    then any name with UFC in it -> UFC, otherwise the name up to its number or subtitle:
    "Strikeforce: ..." -> Strikeforce, "Meca 9" -> Meca, "IFC - Global Domination" -> IFC.
    """
    name = " ".join(event_name.split())
    for prefix, label in PROMOTION_PREFIXES.items():
        if name.lower().startswith(prefix.lower()):
            return label
    if re.search(r"\bUFC\b", name, re.I):
        return "UFC"
    head = re.split(r"\s*(?::| - | vs\.? |\d)", name, maxsplit=1, flags=re.I)[0].strip(" -:")
    head = head or name
    return PROMOTION_OVERRIDES.get(head, head)


def load_event(pages: Pages, eid: str) -> dict | None:
    """One event's rows, by UFC Stats ids, or None if any of its pages is missing:
    {event: {id, name, date, location}, fights: [{id, values, sides: [{id, name, result}],
    rounds: {fighter id: [row]}}]}."""
    html = pages.get("events", eid)
    if html is None:
        return None
    ev = parse_event(html)
    out = dict(event=dict(id=eid, name=ev["name"], date=F.parse_dob(ev["date"]), location=ev["location"] or None),
               fights=[])
    for fid in ev["fight_ids"]:
        html = pages.get("fights", fid)
        if html is None:
            log.warning("Fight page %s missing; leaving event %s as it is.", fid, eid)
            return None
        f = parse_fight(html)
        statuses = [p["status"] for p in f["fighters"]]
        if len(f["fighters"]) != 2 or not (any(statuses) or f["method"]):
            continue  # no result yet (upcoming / in progress)
        f.update(FIGHT_FIXES.get(fid, {}))
        bout, method, details = f["bout"], f["method"], f["details"]
        nc = F.is_no_contest(method) or statuses == ["NC", "NC"]
        rounds: dict[str, list] = {}
        for (rnd, fighter_id), cells in sorted(f["rounds"].items()):
            rounds.setdefault(fighter_id, []).append(_round_row(rnd, cells))
        out["fights"].append(dict(
            id=fid, rounds=rounds,
            values=dict(weight_class=bout or None, title_fight=int("title" in bout.lower()),
                        scheduled_rounds=_scheduled_rounds(f["time_format"]), method=method or None,
                        end_round=F.parse_int(f["round"]), end_time=f["time"] or None,
                        time_format=f["time_format"] or None, referee=f["referee"] or None,
                        details=details or None, overturned=int(F.is_overturned(method, details, nc)),
                        no_contest=int(nc)),
            sides=[dict(id=p["fighter_id"], name=p["name"], result=p["status"] or None) for p in f["fighters"]]))
    return out


# --- writing -------------------------------------------------------------------------

class Writer:
    """Writes UFC Stats events onto our rows (see the module docstring)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.bouts = conn, Bouts(conn)
        self.fighters = ids.aliases(conn, "fighter", NAME)  # UFC Stats id -> ours
        self.counts = Counter()

    def fighter(self, side: dict) -> int:
        fid = self.fighters.get(side["id"])
        if fid is None:
            fid = self.fighters[side["id"]] = ids.add(self.conn, "fighter", {"name": side["name"]})
            self.counts["new fighters"] += 1
        ids.alias(self.conn, "fighter", fid, NAME, side["id"], side["name"])
        return fid

    def place(self, f: dict, day: str | None) -> int | None:
        """Our fight for this bout, or None (a new one). Links an unknown fighter through it."""
        fight = ids.find(self.conn, "fight", NAME, f["id"])
        if fight is not None:
            return fight
        a, b = (self.fighters.get(s["id"]) for s in f["sides"])
        if a and b:
            m = self.bouts.pair(a, b, day)
        elif a or b:
            known, other = (a, f["sides"][1]) if a else (b, f["sides"][0])
            m = self.bouts.named(known, day, other["name"])
            if m and m[3] in set(self.fighters.values()):
                m = None  # the opponent there has another UFC Stats id: not this fighter
            if m:
                self.fighters[other["id"]] = m[3]
                self.counts["fighters linked through a fight"] += 1
        else:
            m = None
        if m:
            self.counts["fights other sources had"] += 1
        return m[1] if m else None

    def event(self, ev: dict, promo: str | None) -> int:
        """Write one event; returns our id for it."""
        conn, e = self.conn, ev["event"]
        placed = [(f, self.place(f, e["date"])) for f in ev["fights"]]
        eid = ids.find(conn, "event", NAME, e["id"])
        for _, fight in placed:  # an event of ours holding one of these fights is this event
            if fight is None:
                continue
            other = conn.execute("SELECT event_id FROM fights WHERE fight_id = ?", (fight,)).fetchone()[0]
            if NAME in ids.sources(conn, "event", other):
                continue  # another UFC Stats event: the fight moves, the events stay apart
            eid = other if eid is None else ids.merge(conn, "event", eid, other)
        if eid is None:
            if not ev["fights"]:
                return None
            eid = ids.add(conn, "event", {"name": e["name"], "promotion": promo or promotion(e["name"])})
            self.counts["new events"] += 1
        elif promo:
            conn.execute("UPDATE events SET promotion = ? WHERE event_id = ?", (promo, eid))
        ids.alias(conn, "event", eid, NAME, e["id"])
        ids.write(conn, "event", eid, NAME, {k: e[k] for k in ("name", "date", "location")})
        mine = set()
        for f, fight in placed:
            sides = [self.fighter(s) for s in f["sides"]]
            if fight is None:
                fight = ids.add(conn, "fight", {"event_id": eid, **f["values"]})
                self.bouts.add(fight, eid, e["date"], *sides)
                self.counts["new fights"] += 1
            else:
                ids.write(conn, "fight", fight, NAME, {"event_id": eid, **f["values"]})
            ids.alias(conn, "fight", fight, NAME, f["id"])
            mine.add(fight)
            conn.execute("DELETE FROM fight_participants WHERE fight_id = ?", (fight,))
            conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) VALUES (?,?,?,?)",
                             [(fight, fid, corner, s["result"]) for corner, (fid, s) in enumerate(zip(sides, f["sides"]))])
            rows = [(fight, fid, r["round"], *(r.get(c) for c in ROUND_COLS))
                    for fid, s in zip(sides, f["sides"]) for r in f["rounds"].get(s["id"], [])]
            if rows:  # a fight's stats come from one source, and UFC Stats' are the fullest
                for t in ("round_stats", "smartcage_round_stats", "sofascore_round_stats"):
                    conn.execute(f"DELETE FROM {t} WHERE fight_id = ?", (fight,))
                conn.executemany(f"INSERT INTO round_stats (fight_id, fighter_id, round, {', '.join(ROUND_COLS)}) "
                                 f"VALUES ({', '.join('?' * (len(ROUND_COLS) + 3))})", rows)
        # Bouts UFC Stats no longer lists on this event.
        for (fight,) in conn.execute("SELECT fight_id FROM fights f WHERE event_id = ? AND EXISTS (SELECT 1 FROM "
                                     "fight_aliases a WHERE a.fight_id = f.fight_id AND a.source = ?)",
                                     (eid, NAME)).fetchall():
            if fight not in mine:
                ids.drop_claim(conn, fight, NAME)
                self.counts["bouts gone from the card"] += 1
        if not conn.execute("SELECT 1 FROM fights WHERE event_id = ?", (eid,)).fetchone():
            for t in ("event_cards", "event_aliases", "events"):  # nothing left on it
                conn.execute(f"DELETE FROM {t} WHERE event_id = ?", (eid,))
        return eid


def apply_fighter(conn: sqlite3.Connection, fid: int, html: str | None, ufc_id: str, fight_names: set[str]) -> None:
    """Write a fighter's bio from their page (UFC Stats' values replace other sources')."""
    names = set(fight_names)
    if html:
        b = parse_fighter(html)
        if b["name"]:
            names.add(b["name"])
        wins, losses, draws, ncs = F.parse_record(b["record"])
        ids.write(conn, "fighter", fid, NAME, dict(
            name=b["name"] or None, nickname=b["nickname"] or None, dob=F.parse_dob(b["dob"]),
            height_in=F.parse_height_in(b["height"]), reach_in=F.parse_reach_in(b["reach"]),
            weight_lbs=F.parse_weight_lbs(b["weight"]), stance=b["stance"] or None,
            wins=wins, losses=losses, draws=draws, no_contests=ncs))
    for n in sorted(names):
        ids.alias(conn, "fighter", fid, NAME, ufc_id, n)


# --- loading a batch of events ------------------------------------------------------

def load_events(conn: sqlite3.Connection, pages: Pages, targets: list[str],
                listed: set[str] = frozenset(), reuse_fighters: bool = False) -> tuple[list, list]:
    """Load, then write, a batch of events and their fighters. Returns (loaded, skipped).

    Promotion: events on UFC Stats' events list are UFC; an event already in the database
    keeps its label; anything else is inferred from its name (see promotion())."""
    loaded, skipped, fight_names = [], [], {}
    for i, eid in enumerate(targets, 1):
        ev = load_event(pages, eid)
        if ev is None:
            skipped.append(eid)
            continue
        loaded.append(ev)
        for f in ev["fights"]:
            for s in f["sides"]:
                fight_names.setdefault(s["id"], set()).add(s["name"])
        if i % 50 == 0:
            log.info("Events: %d/%d", i, len(targets))
    fighter_pages = {fid: pages.get("fighters", fid, prefer_disk=reuse_fighters) for fid in sorted(fight_names)}
    with conn:
        w = Writer(conn)
        for ev in loaded:
            w.event(ev, "UFC" if ev["event"]["id"] in listed else None)
        for ufc_id, names in fight_names.items():
            if ufc_id in w.fighters:
                apply_fighter(conn, w.fighters[ufc_id], fighter_pages[ufc_id], ufc_id, names)
        w.counts["duplicate fights merged"] = ids.merge_duplicates(conn)
        ids.drop_empty(conn)
    log.info("Loaded %d events (%d fights, %d fighters); %d skipped for missing pages. %s",
             len(loaded), sum(len(ev["fights"]) for ev in loaded), len(fight_names), len(skipped),
             ", ".join(f"{k} {v}" for k, v in w.counts.items()) if loaded else "")
    return loaded, skipped


def discover(conn: sqlite3.Connection, pages: Pages) -> None:
    """Find events UFC Stats hosts but leaves out of its events list (e.g. PRIDE).

    Fighter pages list every fight UFC Stats has for that fighter. Fights not in the
    database lead (via their fight page) to missing events, which are loaded; their new
    fighters' pages are then scanned too, until no new events turn up. Pages already on
    disk are reused.
    """
    known = set(ids.aliases(conn, "fight", NAME))
    scanned: set[str] = set()
    for sweep in range(1, 20):
        todo = sorted(set(ids.aliases(conn, "fighter", NAME)) - scanned)
        log.info("Discovery sweep %d: scanning %d fighter pages.", sweep, len(todo))
        unknown: set[str] = set()
        for i, fid in enumerate(todo, 1):
            html = pages.get("fighters", fid, prefer_disk=True)
            scanned.add(fid)
            if html:
                unknown |= set(parse_fighter(html)["fight_ids"]) - known
            if i % 500 == 0:
                log.info("  fighter pages: %d/%d", i, len(todo))
        events: set[str] = set()
        for fight_id in sorted(unknown):
            html = pages.get("fights", fight_id, prefer_disk=True)
            if html and (eid := parse_fight(html)["event_id"]):
                events.add(eid)
        known |= unknown
        log.info("  %d unknown fights -> %d events not in the database.", len(unknown), len(events))
        if not events:
            break
        # fighter pages were fetched moments ago by this sweep's scan: reuse them
        loaded, _ = load_events(conn, pages, sorted(events), reuse_fighters=True)
        for ev in sorted(loaded, key=lambda e: e["event"]["date"] or ""):
            e = ev["event"]
            log.info("  + %s  %s  (%d fights)", e["date"], e["name"], len(ev["fights"]))
    counts = conn.execute("SELECT promotion, COUNT(*), MIN(date), MAX(date) FROM events "
                          "GROUP BY promotion ORDER BY COUNT(*) DESC LIMIT 40").fetchall()
    log.info("Events by promotion (check these labels; fix any in PROMOTION_OVERRIDES):")
    for promo, n, first, last in counts:
        log.info("  %-20s %5d events  %s to %s", promo, n, first, last)


# --- entry points (see canon_db.sources) ---------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="always reload events from the last N days (default: 21)")
    p.add_argument("--events", nargs="*", default=[], metavar="EVENT_ID",
                   help="also load these events (UFC Stats ids; even ones missing from the events list)")
    p.add_argument("--all", action="store_true",
                   help="reload every completed event (~15k pages, ~4-5 h)")
    p.add_argument("--discover", action="store_true",
                   help="only find and load events missing from the events list (e.g. PRIDE), "
                        "by walking fighter pages")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-apply the event pages already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def fill_sex(conn: sqlite3.Connection) -> None:
    """Set fighters.sex where UFC Stats' bout titles say it: 'F' for anyone in a women's
    bout, 'M' for anyone else in a named division (catch weight and open weight don't say)."""
    bouts = ("SELECT p.fighter_id FROM fight_participants p JOIN fights f USING (fight_id) "
             "JOIN fight_aliases a ON a.fight_id = f.fight_id AND a.source = 'ufcstats' WHERE f.weight_class LIKE ")
    with conn:
        conn.execute(f"UPDATE fighters SET sex = 'F' WHERE sex IS NULL AND fighter_id IN ({bouts} '%Women%')")
        conn.execute(f"UPDATE fighters SET sex = 'M' WHERE sex IS NULL AND fighter_id IN ({bouts} '%weight%' "
                     "AND f.weight_class NOT LIKE '%Catch%' AND f.weight_class NOT LIKE '%Open%')")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    """Bring `conn` up to date with UFC Stats. Raises if nothing could be loaded."""
    pages = Pages(raw, None if args.offline else Fetcher(args.delay))
    if args.discover:
        discover(conn, pages)
        return
    if args.offline:
        targets = sorted(p.stem for p in (raw / "events").glob("*.html"))
    else:
        listing = pages.fetcher.get(EVENTS_LIST_URL)
        if listing is None:
            raise RuntimeError("Couldn't get the UFC Stats events list.")
        save(raw / "events_list.html", listing)
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=args.recent_days)).isoformat()
        completed = [(e["event_id"], F.parse_dob(e["date"])) for e in parse_events_list(listing)]
        completed = [(eid, d) for eid, d in completed if d and d <= today]
        have = set(ids.aliases(conn, "event", NAME))
        targets = [eid for eid, d in completed
                   if args.all or eid not in have or d >= cutoff or eid in args.events]
        # Explicitly requested events load even if the events list omits them.
        targets += [eid for eid in args.events if eid not in targets]
        log.info("Events on UFC Stats: %d completed, %d in database; loading %d.",
                 len(completed), len(have), len(targets))
    loaded, skipped = load_events(conn, pages, targets, listed=set() if args.offline else
                                  {eid for eid, _ in completed})
    if skipped and not loaded:
        raise RuntimeError(f"All {len(skipped)} target events failed to download.")
    fill_sex(conn)
