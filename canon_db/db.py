"""Source-agnostic database plumbing: the working copy, change detection, summary.

Every update runs against a temporary copy of data/canon.db. The real file is
replaced only if the content changed, so an idle run leaves it byte-identical and
git sees nothing. Rewriting identical rows can still change the file's bytes, which
is why the comparison is on content, not bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "canon.db"
RAW = ROOT / "data" / "raw"

TABLES = ("fighters", "fighter_aliases", "events", "fights", "fight_participants",
          "round_stats", "judge_scores", "odds")


def digest(conn: sqlite3.Connection) -> str:
    """Hash of the database's content: its SQL dump, sorted so it is independent of
    file layout and of physical row order (re-inserted rows land in a new order)."""
    h = hashlib.sha256()
    for line in sorted(conn.iterdump()):
        h.update(line.encode())
    return h.hexdigest()


def summary(conn: sqlite3.Connection) -> dict:
    """Coverage + row counts, published as data/summary.json next to the database."""
    eid, name, day = conn.execute(
        "SELECT event_id, name, date FROM events ORDER BY date DESC, event_id LIMIT 1").fetchone()
    first = conn.execute("SELECT MIN(date) FROM events").fetchone()[0]
    return dict(updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                coverage=dict(first_event_date=first, last_event_date=day,
                              last_event=name, last_event_id=eid),
                rows={t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in TABLES})


class Update:
    """Result of an `updating()` block; `changed` is set when the block exits."""
    changed: bool = False


@contextmanager
def updating(db: Path):
    """Yield (connection to a working copy, Update). On a clean exit, publish the copy
    over `db` (plus summary.json) only if its content changed; otherwise discard it.
    On an exception, discard it and leave `db` untouched."""
    if not db.exists():
        raise FileNotFoundError(f"No database at {db}.")
    work = db.with_suffix(".db.tmp")
    shutil.copyfile(db, work)
    conn = sqlite3.connect(work)
    result = Update()
    try:
        before = digest(conn)
        yield conn, result
        result.changed = digest(conn) != before
        if result.changed:
            conn.execute("VACUUM")
            s = summary(conn)
            conn.close()
            work.replace(db)
            (db.parent / "summary.json").write_text(json.dumps(s, indent=2) + "\n",
                                                    encoding="utf-8")
    finally:
        conn.close()
        work.unlink(missing_ok=True)


def github_output(**values) -> None:
    """Expose values to later GitHub Actions steps (no-op outside Actions)."""
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n")
