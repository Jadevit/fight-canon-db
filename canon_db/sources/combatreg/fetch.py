"""Combat Registry (combatreg.com): fetch pages to disk, nothing loaded yet. Not a registered
source: run it on its own, in steps, each one safe to stop and start again.

    python -m canon_db.sources.combatreg.fetch lists      # every event list page (~660)
    python -m canon_db.sources.combatreg.fetch count      # what the lists hold, and what's left
    python -m canon_db.sources.combatreg.fetch events     # pages of the events worth reading
    python -m canon_db.sources.combatreg.fetch fighters   # pages of the fighters on those events

Only commission-verified pro MMA bouts will be kept, and those are told apart on fighter pages:
each has a table per level and sport ("Official Professional MMA Fight Record", "Official Amateur
Kickboxing Fight Record") and tags each bout OFF (an ABC-recognized commission), SB (a sanctioning
body) or NRB (not regulated). Event pages say none of that, so they only lead to fighters. The
events read are those the list marks official, of type MMA or Hybrid (Hybrid cards mix MMA with
other sports; the tables keep them apart), already held, and not named as amateur cards: a fighter
with a pro bout has it on a pro card too, so amateur cards would only cost requests.

Pages go to data/raw/combatreg/: lists/<n>.html (read again each run: new events push the
older ones down), events/<uuid>.html, fighters/<id>.html. A page on disk isn't fetched again.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from canon_db.db import RAW
from canon_db.http import Fetcher, save
from canon_db.pages import text

NAME = "combatreg"
BASE = "https://combatreg.com"
RAW_DIR = RAW / NAME

log = logging.getLogger(NAME)

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
_EVENT = re.compile(r'href="?(?:https://combatreg\.com)?/events/([^":>\s]+):([0-9A-Fa-f\-]{36})')  # older ids are upper case
_FIGHTER = re.compile(r'href="(/fighters/[^":]+:([0-9A-Fa-f]{16}))"')


def parse_list(page: str) -> tuple[list[dict], bool]:
    """An event list page: its rows {uuid, slug, date, type, official, state, name}, and whether
    there's a next page."""
    rows = []
    for row in _ROW.findall(page):
        cells = _CELL.findall(row)
        if len(cells) < 4 or not (ev := _EVENT.search(cells[2][1])):
            continue
        when = re.search(r'data-sort="(\d{4}-\d{2}-\d{2})', cells[0][0])
        rows.append(dict(uuid=ev.group(2), slug=ev.group(1), date=when.group(1) if when else "",
                         type=text(cells[1][1]), official=int("official" in cells[2][1]),
                         state=text(cells[3][1]), name=text(cells[2][1])))
    return rows, "Next page" in page


def wanted(e: dict, today: str) -> bool:
    """An event whose page is worth reading (see the module's docstring)."""
    return (e["official"] and e["type"] in ("MMA", "Hybrid") and e["date"] and e["date"] <= today
            and "amateur" not in e["name"].casefold())


def fighters_on(page: str) -> dict[str, str]:
    """The fighters an event page links to: {id: path}."""
    return {fid: path for path, fid in _FIGHTER.findall(page)}


def _index(raw: Path) -> list[dict]:
    path = raw / "events.tsv"
    if not path.exists():
        raise SystemExit("No event index yet: run `lists` first.")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def fetch_lists(fetcher: Fetcher, raw: Path) -> None:
    """Every event list page, then the index of every event on them (events.tsv)."""
    events: dict[str, dict] = {}
    n = 0
    while True:
        # Under load the site sometimes answers "No Events Found." for a page that has events, so an
        # empty page is asked for again; the list ends on a page with events and no next page.
        for attempt in range(1, 6):
            page = fetcher.get(f"{BASE}/events/results" + (f"?page={n}" if n else ""))
            rows, more = parse_list(page or "")
            if rows:
                break
            log.warning("List page %d came back with no events (attempt %d); asking again.", n, attempt)
            time.sleep(10 * attempt)
        else:
            raise SystemExit(f"List page {n} has no events after 5 tries; run `lists` again later.")
        save(raw / "lists" / f"{n}.html", page)
        for r in rows:
            events.setdefault(r["uuid"], r)
        n += 1
        if n % 50 == 0:
            log.info("  list pages: %d, events: %d", n, len(events))
        if not more:
            break
    with open(raw / "events.tsv.tmp", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ("uuid", "slug", "date", "type", "official", "state", "name"), delimiter="\t")
        w.writeheader()
        w.writerows(sorted(events.values(), key=lambda e: (e["date"], e["uuid"])))
    (raw / "events.tsv.tmp").replace(raw / "events.tsv")
    log.info("Lists: %d pages, %d events.", n, len(events))


def count(raw: Path) -> None:
    events = _index(raw)
    today = date.today().isoformat()
    log.info("Events: %d. By type and official:", len(events))
    for (t, o), n in Counter((e["type"], e["official"]) for e in events).most_common():
        log.info("  %-25s %-12s %6d", t, "official" if o == "1" else "", n)
    want = [e for e in events if wanted({**e, "official": e["official"] == "1"}, today)]
    amateur = sum(1 for e in events if e["official"] == "1" and e["type"] in ("MMA", "Hybrid")
                  and "amateur" in e["name"].casefold())
    have = sum(1 for e in want if (raw / "events" / f"{e['uuid']}.html").exists())
    log.info("Event pages to read: %d (%d on disk); official MMA or Hybrid cards named amateur, skipped: %d.",
             len(want), have, amateur)
    fighters = _fighters(raw, want)
    if fighters:
        on_disk = sum(1 for f in fighters if (raw / "fighters" / f"{f}.html").exists())
        log.info("Fighters on the event pages read so far: %d (%d on disk).", len(fighters), on_disk)


def _fighters(raw: Path, events: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in events:
        path = raw / "events" / f"{e['uuid']}.html"
        if path.exists():
            out.update(fighters_on(path.read_text(encoding="utf-8")))
    return out


def _fetch_all(fetcher: Fetcher, todo: list[tuple[str, Path]], marker: str, workers: int, what: str) -> None:
    """Fetch each (url, path) not on disk, `workers` at a time (the fetcher keeps one request a
    second); a page without `marker` isn't saved."""
    todo = [(u, p) for u, p in todo if not p.exists()]
    log.info("%s: %d to fetch.", what, len(todo))
    done = Counter()

    def one(item: tuple[str, Path]) -> None:
        url, path = item
        page = fetcher.get(url)
        if page is None:
            done["failed"] += 1
        elif marker not in page:
            done["not the page"] += 1
            log.warning("Not the page we expect: %s", url)
        else:
            save(path, page)
            done["saved"] += 1
        if sum(done.values()) % 500 == 0:
            log.info("  %s: %s", what, ", ".join(f"{k} {v}" for k, v in done.items()))

    with ThreadPoolExecutor(max(1, workers)) as pool:
        list(pool.map(one, todo))
    log.info("%s done: %s.", what, ", ".join(f"{k} {v}" for k, v in done.items()) or "nothing to do")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m canon_db.sources.combatreg.fetch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("lists", "count", "events", "fighters"))
    ap.add_argument("--raw", type=Path, default=RAW_DIR)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--workers", type=int, default=3, help="requests in flight (the delay still holds)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    raw = args.raw
    raw.mkdir(parents=True, exist_ok=True)
    if args.step == "count":
        count(raw)
        return
    fetcher = Fetcher(args.delay)
    if args.step == "lists":
        fetch_lists(fetcher, raw)
        count(raw)
        return
    today = date.today().isoformat()
    want = [e for e in _index(raw) if wanted({**e, "official": e["official"] == "1"}, today)]
    if args.step == "events":
        _fetch_all(fetcher, [(f"{BASE}/events/{e['slug']}:{e['uuid']}", raw / "events" / f"{e['uuid']}.html")
                             for e in want], "Date/Time:", args.workers, "Event pages")
    else:
        fighters = _fighters(raw, want)
        _fetch_all(fetcher, [(BASE + path, raw / "fighters" / f"{fid}.html") for fid, path in sorted(fighters.items())],
                   "Fight Record", args.workers, "Fighter pages")
    if fetcher.failures:
        log.warning("%d requests failed; run the step again to retry them.", len(fetcher.failures))


if __name__ == "__main__":
    main()
