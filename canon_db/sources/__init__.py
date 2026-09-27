"""Data sources. Each source is a subpackage exposing:

    NAME                              short id; also its data/raw/<NAME>/ folder
    add_arguments(parser)             its own CLI options
    update(conn, args, raw_dir)       bring the database up to date from that source

To add a source, create sources/<name>/ with those three and register it below.
"""

from canon_db.sources import ufcstats

SOURCES = {s.NAME: s for s in (ufcstats,)}
