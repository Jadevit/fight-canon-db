"""MMA Decisions (mmadecisions.com): judges' cards round by round and media scores of decisions, attached to fights already in the database."""

from canon_db.sources.mmadecisions.load import NAME, add_arguments, update

__all__ = ["NAME", "add_arguments", "update"]
