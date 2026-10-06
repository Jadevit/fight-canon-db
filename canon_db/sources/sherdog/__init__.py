"""Sherdog (sherdog.com): fighters' full pro records, linked to UFC Stats through shared fights."""

from canon_db.sources.sherdog.load import NAME, add_arguments, update

__all__ = ["NAME", "add_arguments", "update"]
