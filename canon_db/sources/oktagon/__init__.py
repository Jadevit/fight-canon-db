"""Oktagon (oktagonmma.com): Oktagon's own per-round fight stats, attached to fights already in the database."""

from canon_db.sources.oktagon.load import NAME, add_arguments, update

__all__ = ["NAME", "add_arguments", "update"]
