"""Fetch Sofascore's API into data/raw/sofascore/:
    seasons/<promotion>.json   a promotion's seasons (on Sofascore each season is one event)
    events/<season>.json       a season's bouts
    stats/<bout>.json          a bout's stats, per round and in total
The API only answers clients that look like a browser, so requests go through curl_cffi
with Chrome's fingerprint.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://www.sofascore.com/api/v1"
log = logging.getLogger("sofascore")


class Site:
    def __init__(self, delay: float):
        self.delay, self.last = delay, 0.0
        self.session = requests.Session(impersonate="chrome")

    def get(self, path: str):
        """The JSON at BASE + path, or None (404, or still failing after retries)."""
        for attempt in range(1, 5):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.session.get(BASE + path, timeout=30)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                log.warning("HTTP %s on %s (attempt %d)", r.status_code, path, attempt)
            except (requests.RequestsError, ValueError) as e:
                log.warning("%s on %s (attempt %d)", type(e).__name__, path, attempt)
            time.sleep(self.delay * 2 ** attempt)
        return None


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def fetch_seasons(site: Site, raw: Path, promotion: int) -> list[dict]:
    """[{id, name, year}] of a promotion's seasons, newest first, saved to disk."""
    data = site.get(f"/unique-tournament/{promotion}/seasons")
    if data is None:
        log.warning("Sofascore: couldn't read the seasons of promotion %s.", promotion)
        return []
    save(raw / "seasons" / f"{promotion}.json", data)
    return data.get("seasons") or []


def fetch_events(site: Site, raw: Path, promotion: int, season: int) -> bool:
    """Save a season's bouts (every page of them)."""
    events, page = [], 0
    while True:
        data = site.get(f"/unique-tournament/{promotion}/season/{season}/events/last/{page}")
        if data is None:
            break
        events += data.get("events") or []
        if not data.get("hasNextPage"):
            break
        page += 1
    if not events:
        return False
    save(raw / "events" / f"{season}.json", {"promotion": promotion, "events": events})
    return True


def fetch_stats(site: Site, raw: Path, bout: int) -> None:
    """Save a bout's stats; a bout with none (404) saves nothing."""
    data = site.get(f"/event/{bout}/statistics")
    if data is not None:
        save(raw / "stats" / f"{bout}.json", data)
