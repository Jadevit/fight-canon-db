"""Fetch Oktagon's files into data/raw/oktagon/:
    tournaments/<id>.json   an event on the stats host, with its matches
    matches/<id>.json       a match's whole log (every strike, takedown, submission attempt)
    fightcards/<slug>.json  the event's card on Oktagon's own API (bout gender, weigh-ins)
The match logs are kept whole on disk; the database only takes their per-round totals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from canon_db.http import Fetcher

STATS = "https://oktagon.sh12w3.esports.cz/api/export"
API = "https://api.oktagonmma.com/v1"
log = logging.getLogger("oktagon")


def get_json(site: Fetcher, url: str):
    r = site.request("GET", url)
    try:
        return r.json() if r is not None and r.status_code == 200 else None
    except ValueError:
        return None


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def tournaments(site: Fetcher, years: list[int]) -> list[dict]:
    """[{id, name, date}] of the stats host's events in these years."""
    out = {}
    for year in years:
        for t in get_json(site, f"{STATS}/tournaments?year={year}") or []:
            out[t["id"]] = t
    return sorted(out.values(), key=lambda t: t["date"])


def fetch_tournament(site: Fetcher, raw: Path, tid: int, fresh: bool) -> None:
    """Save a tournament and its matches (those not on disk yet, or all with `fresh`)."""
    path = raw / "tournaments" / f"{tid}.json"
    if fresh or not path.exists():
        data = get_json(site, f"{STATS}/tournaments/{tid}")
        if data is None:
            log.warning("Oktagon: couldn't read tournament %s.", tid)
            return
        save(path, data)
    for m in json.loads(path.read_text(encoding="utf-8")).get("matches") or []:
        mpath = raw / "matches" / f"{m['id']}.json"
        if fresh or not mpath.exists():
            data = get_json(site, f"{STATS}/matches/{m['id']}")
            if data is not None:
                save(mpath, data)


def fetch_cards(site: Fetcher, raw: Path, since: str, today: str, every: bool) -> None:
    """Save the fight cards of past events: those from `since` on again, and with `every`
    all the ones not on disk."""
    events = get_json(site, f"{API}/events?limit=200")
    if events is None:
        log.warning("Oktagon: couldn't read the events list; no fight cards fetched.")
        return
    for e in events:
        day, path = (e.get("startDate") or "")[:10], raw / "fightcards" / f"{e.get('slug')}.json"
        if e.get("type") != "TOURNAMENT" or not day or day > today:
            continue
        if day >= since or (every and not path.exists()):
            card = get_json(site, f"{API}/events/{e['slug']}/fightcard")
            if card is not None:
                save(path, card)
