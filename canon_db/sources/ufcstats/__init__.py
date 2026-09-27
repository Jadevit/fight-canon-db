"""UFC Stats (ufcstats.com): events, fights, per-round stats, fighter bios."""

from canon_db.sources.ufcstats.load import add_arguments, update

NAME = "ufcstats"
__all__ = ["NAME", "add_arguments", "update"]
