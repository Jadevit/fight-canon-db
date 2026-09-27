"""Command line: bring data/canon.db up to date from its sources.

    python -m canon_db update                    # every source, default options
    python -m canon_db update ufcstats --events 7e654edcddd71550
    python -m canon_db update ufcstats --all     # reload everything (~4-5 h)
    python -m canon_db update ufcstats --offline # re-apply saved pages, no network
    python -m canon_db update ufcstats -h        # a source's own options

All sources run against one working copy of the database, which replaces
data/canon.db (and data/summary.json) only if its content changed. If any source
fails, nothing is published. Under GitHub Actions, `changed=true|false` is written
to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from canon_db import db
from canon_db.sources import SOURCES


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m canon_db", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    ap.add_argument("--raw", type=Path, default=db.RAW,
                    help="where fetched pages are saved, one folder per source")
    commands = ap.add_subparsers(dest="command", required=True)
    update = commands.add_parser("update", help="update the database from its sources")
    per_source = update.add_subparsers(dest="source")
    for name, source in SOURCES.items():
        source.add_arguments(per_source.add_parser(name, help=source.__doc__))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("canon_db")

    if args.source:
        run = {args.source: args}
    else:  # every source with its defaults
        run = {}
        for name, source in SOURCES.items():
            p = argparse.ArgumentParser()
            source.add_arguments(p)
            run[name] = p.parse_args([])

    try:
        with db.updating(args.db) as (conn, result):
            for name, source_args in run.items():
                raw = args.raw / name
                raw.mkdir(parents=True, exist_ok=True)
                SOURCES[name].update(conn, source_args, raw)
    except (FileNotFoundError, RuntimeError) as e:
        db.github_output(changed=False)
        sys.exit(f"Update failed, database left untouched: {e}")

    if result.changed:
        s = json.loads((args.db.parent / "summary.json").read_text(encoding="utf-8"))
        log.info("Database changed; now through %s (%s).",
                 s["coverage"]["last_event"], s["coverage"]["last_event_date"])
    else:
        log.info("No change.")
    db.github_output(changed=result.changed)


if __name__ == "__main__":
    main()
