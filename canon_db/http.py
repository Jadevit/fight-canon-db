"""Polite HTTP for every source: a delay between requests (about one a second, varied so it isn't a
fixed beat), retries with backoff, and pages written so a crash never leaves half of one on disk
(and, for a page that changes in place, a history of it)."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from canon_db.pages import text

HEADERS = {"User-Agent": "Mozilla/5.0 (canon-db research scraper; ~1 request/sec)"}

log = logging.getLogger("fetcher")


class Fetcher:
    """A delay between requests, retries with exponential backoff. Threads may share one:
    the delay is between any two requests, whichever thread makes them. A site with a wall
    in front of its pages overrides wall()."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 30):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.last_request = 0.0
        self.lock = threading.Lock()
        self.failures: list[str] = []

    def throttle(self) -> None:
        """Hold the delay between requests, 0.7 to 1.5 times `delay` at random, then mark this
        moment as the last one."""
        with self.lock:
            wait = self.delay * random.uniform(0.7, 1.5) - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()

    def wall(self, page: str) -> bool | None:
        """None if `page` is the page; True if it was a wall now cleared (ask again at once);
        False if it's a wall that couldn't be cleared."""
        return None

    def request(self, method: str, url: str, ok: tuple[int, ...] = (200,), **kw) -> requests.Response | None:
        """The response, once its status is in `ok`; None after the retries (or a 404, which
        asking again won't change)."""
        for attempt in range(1, self.retries + 1):
            self.throttle()
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                if r.status_code in ok:
                    cleared = self.wall(r.text) if r.status_code == 200 else None
                    if cleared is None:
                        return r
                    if cleared:
                        continue
                elif r.status_code == 404:
                    log.warning("HTTP 404 on %s", url)
                    break
                else:
                    log.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt)
            except requests.RequestException as e:
                log.warning("%s on %s (attempt %d)", type(e).__name__, url, attempt)
            time.sleep(self.delay * 2 ** attempt)  # back off: 2s, 4s, 8s, 16s
        self.failures.append(url)
        return None

    def get(self, url: str) -> str | None:
        r = self.request("GET", url)
        return None if r is None else r.text


def save(path: Path, text: str) -> None:
    """Write via a temp file so a crash mid-write never leaves a half page that looks finished.
    Makes the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


_HIDDEN = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def visible(page: str) -> str:
    """A page's visible text: scripts and styles left out, since they carry tokens that differ
    on every request."""
    return text(_HIDDEN.sub(" ", page))


def save_changed(folder: Path, page: str, marker: str, now: datetime | None = None) -> str:
    """Keep a history of a page that changes in place (a rankings list): save it as
    <folder>/<YYYY-MM-DD-HHMM>.html (UTC) if its visible text differs from the last one saved.
    A page without `marker` (an error page, a wall) isn't saved. Says which it was."""
    if marker not in visible(page):
        return "not the page"
    saved = sorted(folder.glob("*.html"))
    if saved and visible(saved[-1].read_text(encoding="utf-8")) == visible(page):
        return "unchanged"
    save(folder / f"{now or datetime.now(UTC):%Y-%m-%d-%H%M}.html", page)
    return "saved"


def snapshot(fetcher: Fetcher, url: str, marker: str, folder: Path) -> str:
    """Fetch a page and save_changed() it; logs a warning (never raises) if it couldn't."""
    page = fetcher.get(url)
    done = "failed" if page is None else save_changed(folder, page, marker)
    if done in ("failed", "not the page"):
        log.warning("%s: %s.", url, done)
    return done


def save_json(path: Path, data) -> None:
    save(path, json.dumps(data, ensure_ascii=False))


def read_json(path: Path):
    """A saved JSON file's data, or None if it's missing or isn't JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
