"""PFL (pflmma.com): PFL's own per-round fight stats, attached to fights already in the database."""

from canon_db.sources.pfl.load import NAME, add_arguments, update

__all__ = ["NAME", "add_arguments", "update"]
