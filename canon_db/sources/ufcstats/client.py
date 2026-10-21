"""UFC Stats' HTTP client: the shared Fetcher (canon_db.http) plus its proof-of-work wall solver.

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

import requests

from canon_db import http

BASE = "http://ufcstats.com"
EVENTS_LIST_URL = f"{BASE}/statistics/events/completed?page=all"

CHALLENGE_MARKER = "Checking your browser"
CHALLENGE_URL = f"{BASE}/__c"
NONCE_RE = re.compile(r'nonce="([0-9a-f]+)"')
DIFFICULTY_RE = re.compile(r"Array\((\d+)\+1\)")  # new Array(N+1).join('0') -> N zeros

log = logging.getLogger("fetcher")


class Fetcher(http.Fetcher):
    """canon_db.http.Fetcher that clears UFC Stats' browser check whenever a page is one."""

    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: float = 30):
        super().__init__(delay, retries, timeout)
        self.walls_solved = 0

    def wall(self, page: str) -> bool | None:
        if CHALLENGE_MARKER not in page:
            return None
        return self._solve_wall(page)  # cookie missing or expired

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
        self.throttle()
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
