"""Save every promotion's rankings page that changed, without touching the database: five requests.
Each source also does this in its own run; CI's copies are lost (it starts without data/raw/), so
until rankings are loaded their history is what's run here.

    .venv/Scripts/python scripts/save_rankings.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from canon_db.db import RAW
from canon_db.http import Fetcher, snapshot
from canon_db.sources import aca, brave, ksw
from canon_db.sources.pfl.fetch import RANKINGS as PFL
from canon_db.sources.ufc.client import RANKINGS as UFC

PAGES = {"ufc": UFC, "pfl": PFL, "brave": brave.RANKINGS, "aca": aca.RANKINGS, "ksw": ksw.RANKINGS}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    fetcher = Fetcher()
    print(", ".join(f"{source} {snapshot(fetcher, *page, RAW / source / 'rankings')}"
                    for source, page in PAGES.items()))
