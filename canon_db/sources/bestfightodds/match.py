"""Match BestFightOdds fights to canon.db fights.

Event first: only fights dated within a day of the BestFightOdds date are candidates (time
zones shift dates), and the two fighter names only have to pick one bout among those.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from datetime import date, timedelta


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    return " ".join(re.sub(r"[^A-Z ]", " ", s).split())


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class Matcher:
    def __init__(self, conn: sqlite3.Connection):
        names: dict[str, list[str]] = {}
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighter_aliases"):
            names.setdefault(fid, []).append(name)
        self.by_date: dict[str, list[dict]] = {}
        for fight_id, day in conn.execute(
                "SELECT f.fight_id, e.date FROM fights f JOIN events e USING (event_id)"):
            corners = [r[0] for r in conn.execute(
                "SELECT fighter_id FROM fight_participants WHERE fight_id = ? ORDER BY corner",
                (fight_id,))]
            self.by_date.setdefault(day, []).append(
                {"fight_id": fight_id, "corners": [(c, names.get(c, [])) for c in corners]})

    @staticmethod
    def _score(name: str, names: list[str]) -> float:
        return max((sim(name, n) for n in names), default=0.0)

    def match(self, fight: dict, min_score=0.8, margin=0.1):
        """(fight_id, {bfo_slug: fighter_id}) or (None, reason)."""
        d = date.fromisoformat(fight["date"])
        cands = [c for off in (0, -1, 1) for c in self.by_date.get((d + timedelta(off)).isoformat(), [])]
        a, b = fight["fighters"]
        scored = []
        for c in cands:
            if len(c["corners"]) != 2:
                continue
            (i0, n0), (i1, n1) = c["corners"]
            straight = (self._score(a["name"], n0) + self._score(b["name"], n1)) / 2
            swapped = (self._score(a["name"], n1) + self._score(b["name"], n0)) / 2
            if straight >= swapped:
                scored.append((straight, c["fight_id"], {a["slug"]: i0, b["slug"]: i1}))
            else:
                scored.append((swapped, c["fight_id"], {a["slug"]: i1, b["slug"]: i0}))
        scored.sort(key=lambda t: -t[0])
        if not scored or scored[0][0] < min_score:
            return None, "no fight on that date with both fighters"
        if len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
            return None, "ambiguous"
        return scored[0][1], scored[0][2]
