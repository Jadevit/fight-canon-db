"""UFC Stats loader: pick events -> fetch pages -> parse -> replace them in the DB.

Each run:
  1. fetches the UFC Stats events list;
  2. picks the events to (re)load: completed events not yet in the database, plus
     every event from the last --recent-days days (stats and corrections are posted
     after fight night). Upcoming events are skipped;
  3. downloads those events' event, fight and fighter pages to data/raw/ufcstats/
     (kept for debugging and --offline re-runs; never committed);
  4. replaces each of those events wholesale in the database (its event row,
     fights, participants and round stats) and upserts the fighters' bios.

An event is only replaced if every one of its fight pages downloaded, so a flaky
run can never delete data. Fighter IDs come straight from each fight page's two
fighter links; no name matching is involved anywhere.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from canon_db.sources.ufcstats import fields as F
from canon_db.sources.ufcstats.client import BASE, EVENTS_LIST_URL, Fetcher, save
from canon_db.sources.ufcstats.pages import (parse_event, parse_events_list, parse_fight,
                                             parse_fighter)

URLS = {"events": "event-details", "fights": "fight-details", "fighters": "fighter-details"}

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


def _round_row(fight_id: str, rnd: int, fighter_id: str, c: dict) -> tuple:
    (sig, tot, td, head, body, leg, dist, cl, gr) = [F.parse_of(c.get(k)) for k in (
        "sig", "total", "td", "head", "body", "leg", "distance", "clinch", "ground")]
    return (fight_id, fighter_id, rnd, F.parse_int(c.get("kd")), *sig, *tot, *td,
            F.parse_int(c.get("sub")), F.parse_int(c.get("rev")), F.parse_ctrl(c.get("ctrl")),
            *head, *body, *leg, *dist, *cl, *gr)


# Promotion names the rule in promotion() gets wrong, keyed by the name it would produce.
PROMOTION_OVERRIDES: dict[str, str] = {
    "K": "K-1", "M": "M-1",                     # the number is part of the name
    "Strikeforce Challengers": "Strikeforce",   # a promotion's own sub-series
    "DWCS Brazil": "DWCS",
    "UCC Hawaii": "UCC",
    "2H2H - Pride & Honor": "2H2H",
    "Brazil Open '": "Brazil Open",
}


def promotion(event_name: str) -> str:
    """Promotion of an event missing from UFC Stats' events list, from its name.

    Events on that list are always UFC and don't go through this (early UFC names like
    "Ortiz vs. Shamrock 3" don't say UFC). "PRIDE 33: ..." -> PRIDE, "Road to UFC ..." -> UFC,
    otherwise the name up to its number or subtitle: "Strikeforce: ..." -> Strikeforce,
    "Meca 9" -> Meca, "IFC - Global Domination" -> IFC.
    """
    name = " ".join(event_name.split())
    if name.upper().startswith("PRIDE"):
        return "PRIDE"
    if re.search(r"\bUFC\b", name, re.I):
        return "UFC"
    head = re.split(r"\s*(?::| - | vs\.? |\d)", name, maxsplit=1, flags=re.I)[0].strip(" -:")
    head = head or name
    return PROMOTION_OVERRIDES.get(head, head)


def load_event(pages: Pages, eid: str, promo: str | None = None) -> dict | None:
    """All rows for one event, or None if any of its pages is missing. `promo` defaults
    to the promotion inferred from the event name."""
    html = pages.get("events", eid)
    if html is None:
        return None
    ev = parse_event(html)
    out = dict(event=(eid, ev["name"], F.parse_dob(ev["date"]), ev["location"] or None,
                      promo or promotion(ev["name"])),
               fights=[], participants=[], rounds=[], fight_names={})
    for fid in ev["fight_ids"]:
        html = pages.get("fights", fid)
        if html is None:
            log.warning("Fight page %s missing; leaving event %s as it is.", fid, eid)
            return None
        f = parse_fight(html)
        statuses = [p["status"] for p in f["fighters"]]
        if len(f["fighters"]) != 2 or not (any(statuses) or f["method"]):
            continue  # no result yet (upcoming / in progress)
        bout, method, details = f["bout"], f["method"], f["details"]
        out["fights"].append((
            fid, eid, bout or None, int("title" in bout.lower()),
            _scheduled_rounds(f["time_format"]), method or None, F.parse_int(f["round"]),
            f["time"] or None, f["time_format"] or None, f["referee"] or None,
            details or None, int(F.is_overturned(details) or method.lower() == "overturned"),
            int(F.is_no_contest(method) or statuses == ["NC", "NC"])))
        for corner, p in enumerate(f["fighters"]):
            out["participants"].append((fid, p["fighter_id"], corner, p["status"] or None))
            out["fight_names"].setdefault(p["fighter_id"], set()).add(p["name"])
        for (rnd, fighter_id), cells in sorted(f["rounds"].items()):
            out["rounds"].append(_round_row(fid, rnd, fighter_id, cells))
    return out


# --- writing -------------------------------------------------------------------------

def apply_event(conn: sqlite3.Connection, ev: dict) -> None:
    """Replace one event's rows wholesale (also drops bouts since removed from the card)."""
    eid = ev["event"][0]
    fight_ids = [f[0] for f in ev["fights"]]
    stale = [r[0] for r in conn.execute("SELECT fight_id FROM fights WHERE event_id = ?", (eid,))]
    for fid in set(stale) | set(fight_ids):
        for table in ("round_stats", "fight_participants", "fights"):
            conn.execute(f"DELETE FROM {table} WHERE fight_id = ?", (fid,))
    conn.execute("DELETE FROM events WHERE event_id = ?", (eid,))
    if not ev["fights"]:
        return
    conn.execute("INSERT INTO events (event_id, name, date, location, promotion) VALUES (?,?,?,?,?)",
                 ev["event"])
    conn.executemany(
        "INSERT INTO fights (fight_id, event_id, weight_class, title_fight, scheduled_rounds, "
        "method, end_round, end_time, time_format, referee, details, overturned, no_contest) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ev["fights"])
    conn.executemany("INSERT INTO fight_participants (fight_id, fighter_id, corner, result) "
                     "VALUES (?,?,?,?)", ev["participants"])
    conn.executemany(
        "INSERT INTO round_stats (fight_id, fighter_id, round, knockdowns, sig_str_land, "
        "sig_str_att, total_str_land, total_str_att, td_land, td_att, sub_att, reversals, "
        "ctrl_sec, head_land, head_att, body_land, body_att, leg_land, leg_att, dist_land, "
        "dist_att, clinch_land, clinch_att, ground_land, ground_att) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ev["rounds"])


def apply_fighter(conn: sqlite3.Connection, fighter_id: str, html: str | None,
                  fight_names: set[str]) -> None:
    """Upsert a fighter's bio from their page; without a page, only ensure the row exists."""
    if html:
        b = parse_fighter(html)
        name = b["name"] or sorted(fight_names)[0]
        conn.execute(
            "INSERT INTO fighters (fighter_id, name, nickname, dob, height_in, reach_in, "
            "weight_lbs, stance, wins, losses, draws, no_contests) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fighter_id) DO UPDATE "
            "SET name=excluded.name, nickname=excluded.nickname, dob=excluded.dob, "
            "height_in=excluded.height_in, reach_in=excluded.reach_in, "
            "weight_lbs=excluded.weight_lbs, stance=excluded.stance, wins=excluded.wins, "
            "losses=excluded.losses, draws=excluded.draws, no_contests=excluded.no_contests",
            (fighter_id, name, b["nickname"] or None, F.parse_dob(b["dob"]),
             F.parse_height_in(b["height"]), F.parse_reach_in(b["reach"]),
             F.parse_weight_lbs(b["weight"]), b["stance"] or None, *F.parse_record(b["record"])))
        fight_names = fight_names | {name}
    else:
        conn.execute("INSERT OR IGNORE INTO fighters (fighter_id, name) VALUES (?,?)",
                     (fighter_id, sorted(fight_names)[0]))
    conn.executemany("INSERT OR IGNORE INTO fighter_aliases (fighter_id, source, source_id, "
                     "name) VALUES (?,?,?,?)",
                     [(fighter_id, "ufcstats", fighter_id, n) for n in sorted(fight_names)])


# --- loading a batch of events ------------------------------------------------------

def load_events(conn: sqlite3.Connection, pages: Pages, targets: list[str],
                listed: set[str] = frozenset(), reuse_fighters: bool = False) -> tuple[list, list]:
    """Load, then write, a batch of events and their fighters. Returns (loaded, skipped).

    Promotion: events on UFC Stats' events list are UFC; an event already in the database
    keeps its label; anything else is inferred from its name (see promotion())."""
    existing = dict(conn.execute("SELECT event_id, promotion FROM events"))
    loaded, skipped, fight_names = [], [], {}
    for i, eid in enumerate(targets, 1):
        ev = load_event(pages, eid, "UFC" if eid in listed else existing.get(eid))
        if ev is None:
            skipped.append(eid)
            continue
        loaded.append(ev)
        for fid, names in ev["fight_names"].items():
            fight_names.setdefault(fid, set()).update(names)
        if i % 50 == 0:
            log.info("Events: %d/%d", i, len(targets))
    fighter_pages = {fid: pages.get("fighters", fid, prefer_disk=reuse_fighters)
                     for fid in sorted(fight_names)}
    with conn:
        for ev in loaded:
            apply_event(conn, ev)
        for fid, names in fight_names.items():
            apply_fighter(conn, fid, fighter_pages[fid], names)
    log.info("Loaded %d events (%d fights, %d fighters); %d skipped for missing pages.",
             len(loaded), sum(len(ev["fights"]) for ev in loaded), len(fight_names), len(skipped))
    return loaded, skipped


def discover(conn: sqlite3.Connection, pages: Pages) -> None:
    """Find events UFC Stats hosts but leaves out of its events list (e.g. PRIDE).

    Fighter pages list every fight UFC Stats has for that fighter. Fights not in the
    database lead (via their fight page) to missing events, which are loaded; their new
    fighters' pages are then scanned too, until no new events turn up. Pages already on
    disk are reused.
    """
    known = {r[0] for r in conn.execute("SELECT fight_id FROM fights")}
    scanned: set[str] = set()
    for sweep in range(1, 20):
        todo = sorted({r[0] for r in conn.execute("SELECT fighter_id FROM fighters")} - scanned)
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
        for ev in sorted(loaded, key=lambda e: e["event"][2] or ""):
            eid, name, day, _, promo = ev["event"]
            log.info("  + %s  %-12s %s  (%d fights)", day, promo, name, len(ev["fights"]))
    counts = conn.execute("SELECT promotion, COUNT(*), MIN(date), MAX(date) FROM events "
                          "GROUP BY promotion ORDER BY COUNT(*) DESC").fetchall()
    log.info("Events by promotion (check these labels; fix any in PROMOTION_OVERRIDES):")
    for promo, n, first, last in counts:
        log.info("  %-20s %5d events  %s to %s", promo, n, first, last)


# --- entry points (see canon_db.sources) ---------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--recent-days", type=int, default=21,
                   help="always reload events from the last N days (default: 21)")
    p.add_argument("--events", nargs="*", default=[], metavar="EVENT_ID",
                   help="also load these events (even ones missing from the events list)")
    p.add_argument("--all", action="store_true",
                   help="reload every completed event (~15k pages, ~4-5 h)")
    p.add_argument("--discover", action="store_true",
                   help="only find and load events missing from the events list (e.g. PRIDE), "
                        "by walking fighter pages")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-apply the event pages already on disk")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


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
        have = {r[0] for r in conn.execute("SELECT event_id FROM events")}
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
