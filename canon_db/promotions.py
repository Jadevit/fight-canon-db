"""A promotion's label from an event's name, for events whose source doesn't give one (UFC
Stats' events missing from its events list; new organizations' events on Sherdog)."""

from __future__ import annotations

import re

# Series labelled by how their event names start, ahead of the rules in promotion().
PROMOTION_PREFIXES = {"PRIDE": "PRIDE", "Road to UFC": "Road to UFC",
                      "K-1 Hero's": "HERO'S", "K-1 Dynamite": "Dynamite!!"}

# Promotion names the rule in promotion() gets wrong, keyed by the name it would produce.
PROMOTION_OVERRIDES: dict[str, str] = {
    "K": "K-1", "M": "M-1",                     # the number is part of the name
    "Strikeforce Challengers": "Strikeforce",   # a promotion's own sub-series
    "DWCS Brazil": "DWCS",
    "UCC Hawaii": "UCC",
    "2H2H - Pride & Honor": "2H2H",
    "Brazil Open '": "Brazil Open",
}


def promotion(event_name: str) -> str:
    """Promotion of an event from its name.

    UFC Stats' events list is always UFC and doesn't go through this (early UFC names like
    "Ortiz vs. Shamrock 3" don't say UFC). First PROMOTION_PREFIXES ("PRIDE 33: ..." -> PRIDE),
    then any name with UFC in it -> UFC, otherwise the name up to its number or subtitle:
    "Strikeforce: ..." -> Strikeforce, "Meca 9" -> Meca, "IFC - Global Domination" -> IFC.
    """
    name = " ".join(event_name.split())
    for prefix, label in PROMOTION_PREFIXES.items():
        if name.lower().startswith(prefix.lower()):
            return label
    if re.search(r"\bUFC\b", name, re.I):
        return "UFC"
    head = re.split(r"\s*(?::| - | vs\.? |\d)", name, maxsplit=1, flags=re.I)[0].strip(" -:")
    head = head or name
    return PROMOTION_OVERRIDES.get(head, head)
