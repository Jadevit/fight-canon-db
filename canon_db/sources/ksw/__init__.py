"""KSW (kswmma.com): for now only its rankings page (the English one), saved to raw/rankings/ as it
changes."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from canon_db.http import Fetcher, snapshot

NAME = "ksw"
RANKINGS = ("https://www.kswmma.com/en/rankings", "HEAVYWEIGHT")  # url, a string every real copy has

__all__ = ["NAME", "add_arguments", "update"]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--offline", action="store_true", help="no network (nothing to load yet)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")


def update(conn: sqlite3.Connection, args: argparse.Namespace, raw: Path) -> None:
    if not args.offline:
        snapshot(Fetcher(args.delay), *RANKINGS, raw / "rankings")
