"""Polite HTTP client for UFC Stats, including the proof-of-work wall solver.

UFC Stats gates every page behind a one-time hashcash "checking your browser"
challenge: it embeds a nonce and a difficulty, and expects a proof of work (an n
such that sha256("nonce:n") starts with that many hex zeros) POSTed to /__c, after
which it sets a session cookie that unlocks normal browsing. `Fetcher.get` detects
the challenge on any response and re-solves it, so a mid-run cookie expiry
self-heals.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

import requests

BASE = "http://ufcstats.com"
EVENTS_LIST_URL = f"{BASE}/statistics/events/completed?page=all"

HEADERS = {"User-Agent": "Mozilla/5.0 (canon-db research scraper; ~1 request/sec)"}

CHALLENGE_MARKER = "Checking your browser"
CHALLENGE_URL = f"{BASE}/__c"
NONCE_RE = re.compile(r'nonce="([0-9a-f]+)"')
DIFFICULTY_RE = re.compile(r"Array\((\d+)\+1\)")  # new Array(N+1).join('0') -> N zeros

log = logging.getLogger("fetcher")


class Fetcher:
    """Fixed delay between requests, retries with exponential backoff."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 30):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.last_request = 0.0
        self.requests_made = 0
        self.walls_solved = 0
        self.failures: list[str] = []

    def _throttle(self) -> None:
        """Hold the fixed delay between requests, then mark this moment as the last one."""
        wait = self.delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        self.last_request = time.monotonic()

    def _solve_wall(self, html: str) -> bool:
        """Clear the hashcash challenge for this session (a few hundred hashes)."""
        m_nonce = NONCE_RE.search(html)
        m_diff = DIFFICULTY_RE.search(html)
        if not (m_nonce and m_diff):
            log.error("Hit the browser check but couldn't read its challenge; "
                      "the site may have changed it.")
            return False
        nonce = m_nonce.group(1)
        target = "0" * int(m_diff.group(1))
        n = 0
        while not hashlib.sha256(f"{nonce}:{n}".encode()).hexdigest().startswith(target):
            n += 1
        self._throttle()
        self.requests_made += 1
        try:
            r = self.session.post(CHALLENGE_URL, data={"nonce": nonce, "n": n},
                                  timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("Browser-check POST failed: %s", type(e).__name__)
            return False
        if r.status_code // 100 != 2:
            log.warning("Browser-check POST returned HTTP %s", r.status_code)
            return False
        self.walls_solved += 1
        log.info("Cleared the browser check in %d hashes (solve #%d).", n, self.walls_solved)
        return True

    def get(self, url: str) -> str | None:
        for attempt in range(1, self.retries + 1):
            self._throttle()
            self.requests_made += 1
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    if CHALLENGE_MARKER not in r.text:
                        return r.text
                    # Cookie missing or expired: solve and retry without backoff.
                    if self._solve_wall(r.text):
                        continue
                else:
                    log.warning("HTTP %s on %s (attempt %d)", r.status_code, url, attempt)
            except requests.RequestException as e:
                log.warning("%s on %s (attempt %d)", type(e).__name__, url, attempt)
            time.sleep(self.delay * 2 ** attempt)  # back off: 2s, 4s, 8s, 16s
        self.failures.append(url)
        return None


def save(path: Path, html: str) -> None:
    """Write via a temp file so a crash mid-write never leaves a half page that looks finished."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)
