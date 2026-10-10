"""Fetch what the PFL site (pflmma.com) serves for an event into data/raw/pfl/<tag>/:
    event.html   the event page (name, date, and the stats URL)
    card.html    the fight card (bouts, results, tale of the tape)
    stats.json   per-fight and per-round stats, as the page's own script requests them
The stats URL is /smt/v2/<tag> for newer events and /smt/<tag> for older ones; event.html
says which (`is_v2`).

The site loads an older event's stats only after the first request for them, which gets an
empty reply; so events are asked in rounds until each one answers.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

from canon_db.sources.ufcstats.client import HEADERS

BASE = "https://pflmma.com"
EXPIRED = 419  # the session's token timed out
TOKEN_RE = re.compile(r"'X-CSRF-TOKEN': '([^']+)'")
URL_RE = re.compile(r"var fight_url = '([^']+)'")
log = logging.getLogger("pfl")


class Site:
    def __init__(self, delay: float):
        self.delay, self.last, self.token = delay, 0.0, None
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def request(self, method: str, url: str, **kw) -> requests.Response | None:
        for attempt in range(1, 5):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.session.request(method, url, timeout=30, **kw)
                if r.status_code in (200, EXPIRED):
                    return r
                log.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt)
            except requests.RequestException as e:
                log.warning("%s on %s (attempt %d)", type(e).__name__, url, attempt)
            time.sleep(self.delay * 2 ** attempt)
        return None

    def post(self, url: str, page_tag: str, **kw) -> requests.Response | None:
        """POST with the session's token, getting a new one (from an event page) if it expired."""
        for _ in range(2):
            if self.token is None:
                page = self.request("GET", f"{BASE}/event/{page_tag}")
                found = page is not None and TOKEN_RE.search(page.text)
                if not found:
                    return None
                self.token = found.group(1)
            r = self.request("POST", url, headers={"X-CSRF-TOKEN": self.token,
                                                   "X-Requested-With": "XMLHttpRequest"}, **kw)
            if r is None or r.status_code != EXPIRED:
                return r
            self.token = None
        return None

    def events(self) -> list[str]:
        """The tags of every event on the site's events page."""
        r = self.request("GET", f"{BASE}/events")
        return sorted(set(re.findall(r"/event/([a-z0-9-]*[a-z][a-z0-9-]*)", r.text))) if r else []

    def open(self, tag: str, out: Path, fresh: bool = False) -> str | None:
        """Save an event's page and fight card (once, unless `fresh`); returns its stats URL."""
        if fresh or not (out / "card.html").exists():
            page = self.request("GET", f"{BASE}/event/{tag}")
            if page is None or page.status_code != 200 or not URL_RE.search(page.text):
                return None
            found = TOKEN_RE.search(page.text)
            self.token = found.group(1) if found else self.token
            card = self.post(f"{BASE}/ajax/get_fight_card_component", tag,
                             data={"event_tag": tag, "is_mobile": 0})
            out.mkdir(parents=True, exist_ok=True)
            (out / "event.html").write_text(page.text, encoding="utf-8")
            if card is not None and card.status_code == 200:
                (out / "card.html").write_text(card.text, encoding="utf-8")
        url = URL_RE.search((out / "event.html").read_text(encoding="utf-8"))
        return url.group(1) if url else None

    def stats(self, tag: str, url: str, out: Path) -> bool:
        """Ask for an event's stats once and save them; False if they aren't ready."""
        r = self.post(url, tag)
        try:
            r.json()
        except (AttributeError, ValueError):
            return False
        (out / "stats.json").write_text(r.text, encoding="utf-8")
        return True


def fetch(site: Site, raw: Path, tags: list[str], rounds: int, fresh: bool) -> None:
    """Save these events' files, asking up to `rounds` times for stats that aren't ready."""
    pending = {}
    for tag in tags:
        url = site.open(tag, raw / tag, fresh)
        if url is None:
            log.warning("PFL: no event page for %s.", tag)
        else:
            pending[tag] = url
    for n in range(rounds):
        if not pending:
            return
        started = time.monotonic()
        for tag in list(pending):
            if site.stats(tag, pending[tag], raw / tag):
                del pending[tag]
        if pending and n + 1 < rounds:
            log.info("PFL: %d events' stats not ready; asking again (%d/%d).", len(pending), n + 2, rounds)
            time.sleep(max(0.0, 20 - (time.monotonic() - started)))  # a short list would hammer one event
    if pending:
        log.info("PFL: no stats reply for %s.", ", ".join(sorted(pending)))
