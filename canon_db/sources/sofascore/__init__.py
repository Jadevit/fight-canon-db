"""Sofascore (sofascore.com): Sofascore's own per-round fight stats for promotions with no official stats, attached to fights already in the database."""

from canon_db.sources.sofascore.load import NAME, add_arguments, update

__all__ = ["NAME", "add_arguments", "update"]
