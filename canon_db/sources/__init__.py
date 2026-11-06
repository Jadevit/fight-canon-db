"""Data sources. Each source is a subpackage exposing:

    NAME                              short id; also its data/raw/<NAME>/ folder
    add_arguments(parser)             its own CLI options
    update(conn, args, raw_dir)       bring the database up to date from that source

To add a source, create sources/<name>/ with those three and register it below.
"""

from canon_db.sources import (aca, bestfightodds, brave, ksw, mmadecisions, oktagon, pfl, sherdog, sofascore,
                              ufcstats)

# ufcstats first: Sherdog links through its fights, and odds match against both. pfl and
# oktagon after sherdog: their stats attach to the fights Sherdog's cards give. sofascore
# after oktagon: its stats sit beside Oktagon's. mmadecisions attaches judges' cards to any of them.
# brave, aca and ksw (rankings pages only, for now) last: their lists will link to fighters the others have.
SOURCES = {s.NAME: s for s in (ufcstats, sherdog, pfl, oktagon, sofascore, mmadecisions, bestfightodds,
                               brave, aca, ksw)}
