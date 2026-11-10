"""UFC Stats (ufcstats.com): events, fights, per-round stats, fighter bios."""

from canon_db.sources.ufc.load import add_arguments, update

NAME = "ufc"
__all__ = ["NAME", "add_arguments", "update"]
