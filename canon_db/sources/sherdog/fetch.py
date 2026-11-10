"""Sherdog's pages: fetched and saved to data/raw/sherdog/ (fighters/, events/), or read from
there with no fetcher; the fighter sitemaps.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from canon_db.http import Fetcher, save
from canon_db.matching import norm
from canon_db.sources.sherdog.pages import parse_event, parse_fighter, parse_sitemap

NAME = "sherdog"
BASE = "https://www.sherdog.com"
SITEMAPS = 18  # sitemap-fighters.xml, sitemap-fighters2.xml, ... sitemap-fighters18.xml


class Pages:
    def __init__(self, raw: Path, fetcher: Fetcher | None):
        self.dir, self.events, self.fetcher = raw / "fighters", raw / "events", fetcher
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events.mkdir(parents=True, exist_ok=True)

    def event(self, sid: str) -> dict | None:
        """An event's page, parsed (read from disk if it's there)."""
        page = self.event_page(sid)
        return parse_event(page) if page is not None else None

    def event_page(self, sid: str, fresh: bool = False) -> str | None:
        """An event's page as HTML: from disk unless `fresh` (or not there yet)."""
        path = self.events / f"{sid}.html"
        if self.fetcher is not None and (fresh or not path.exists()):
            page = self.fetcher.get(f"{BASE}/events/x-{sid}")
            if page is not None:
                save(path, page)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def get(self, sid: str, cached: bool = False) -> dict | None:
        """A fighter's page, fetched fresh unless `cached` and it's on disk already."""
        path = self.dir / f"{sid}.html"
        if self.fetcher is not None and not (cached and path.exists()):
            page = self.fetcher.get(f"{BASE}/fighter/{sid}")
            if page is not None:
                save(path, page)
        return parse_fighter(path.read_text(encoding="utf-8")) if path.exists() else None

    def on_disk(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.html") if p.stem.isdigit())  # a page is named by its id


def sitemap(fetcher: Fetcher) -> dict[str, list[str]]:
    """Normalized name -> Sherdog ids, from the fighter sitemaps."""
    out = defaultdict(list)
    for i in range(1, SITEMAPS + 1):
        xml = fetcher.get(f"{BASE}/sitemap-fighters{'' if i == 1 else i}.xml")
        for sid, name in parse_sitemap(xml or ""):
            out[norm(name)].append(sid)
    return out
