"""Polite HTTP for every source: a fixed delay between requests (about one a second), retries
with backoff, and pages written so a crash never leaves half of one on disk."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (canon-db research scraper; ~1 request/sec)"}

log = logging.getLogger("fetcher")


class Fetcher:
    """Fixed delay between requests, retries with exponential backoff. Threads may share one:
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
        self.requests_made = 0
        self.failures: list[str] = []

    def throttle(self) -> None:
        """Hold the fixed delay between requests, then mark this moment as the last one."""
        with self.lock:
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests_made += 1

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


def save(path: Path, html: str) -> None:
    """Write via a temp file so a crash mid-write never leaves a half page that looks finished."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)
