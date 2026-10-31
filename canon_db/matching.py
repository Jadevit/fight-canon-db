"""Match another source's bouts to canon.db fights, and tell whether two names are one person.

Matcher: event first: only fights dated within a day of the source's date are candidates
(time zones shift dates), and the two fighter names only have to pick one bout among those.
leftover() places a bout whose names didn't both match (ring names, married names) on the one
unclaimed fight of its event that fits it best. Every source that attaches to our fights
(PFL, Oktagon, Sofascore, BestFightOdds) and Sherdog's linking use these.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from functools import lru_cache


@lru_cache(maxsize=None)
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

    @staticmethod
    def _bound(name: str, names: list[str]) -> float:
        """At least _score(): SequenceMatcher's quick_ratio is cheap and never below ratio."""
        return max((difflib.SequenceMatcher(None, norm(name), norm(n)).quick_ratio() for n in names),
                   default=0.0)

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
            # A fight below min_score - margin can neither win nor make the winner ambiguous;
            # most of a day's fights are that far off, so skip them before scoring exactly.
            ua, ub = (self._bound(a["name"], n0), self._bound(a["name"], n1)),                 (self._bound(b["name"], n0), self._bound(b["name"], n1))
            if max(ua[0] + ub[1], ua[1] + ub[0]) / 2 < min_score - margin - 1e-9:
                continue
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


def days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


class Bouts:
    """Every fight by fighter, to place a source's bout on our fight: within a day of it, with
    one fighter known and the opponent's name agreeing, or both fighters known. A source that
    links fighters through these (Sherdog, UFC Stats) never links by name alone."""

    def __init__(self, conn: sqlite3.Connection):
        self.names: dict[int, list[str]] = {}
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighters UNION ALL "
                                      "SELECT fighter_id, name FROM fighter_aliases"):
            self.names.setdefault(fid, []).append(name)
        self.fights: dict[int, list[tuple]] = {}  # fighter -> [(date, fight_id, event_id, opponent)]
        for fight, eid, day, a, b in conn.execute("""
                SELECT f.fight_id, f.event_id, e.date, p.fighter_id, o.fighter_id
                FROM fights f JOIN events e USING (event_id)
                JOIN fight_participants p ON p.fight_id = f.fight_id
                JOIN fight_participants o ON o.fight_id = f.fight_id AND o.corner <> p.corner
                WHERE e.date IS NOT NULL"""):
            self.fights.setdefault(a, []).append((day, fight, eid, b))

    def add(self, fight: int, event: int, day: str | None, a: int, b: int) -> None:
        """A fight written since: findable from now on."""
        if day:
            self.fights.setdefault(a, []).append((day, fight, event, b))
            self.fights.setdefault(b, []).append((day, fight, event, a))

    def on(self, fid: int, day: str | None, opponent: str, crowded: bool = False) -> tuple | None:
        """Fighter `fid`'s fight within a day of `day`: (date, fight_id, event_id, opponent),
        or None. Their only one is it, unless they have several (or `crowded`: the source lists
        several for them that day, a tournament); then the opponent's name decides."""
        if not day:
            return None
        cands = [c for c in self.fights.get(fid, []) if days_apart(c[0], day) <= 1]
        if len(cands) > 1 or crowded:
            cands = [c for c in cands if name_ok(opponent, self.names.get(c[3], []))]
        return cands[0] if len(cands) == 1 else None

    def named(self, fid: int, day: str | None, opponent: str, crowded: bool = False) -> tuple | None:
        """on(), when the opponent's names also agree."""
        m = self.on(fid, day, opponent, crowded)
        return m if m and name_ok(opponent, self.names.get(m[3], [])) else None

    def pair(self, a: int, b: int, day: str | None) -> tuple | None:
        """The one fight between these two within a day of `day`."""
        if not day:
            return None
        cands = [c for c in self.fights.get(a, []) if c[3] == b and days_apart(c[0], day) <= 1]
        return cands[0] if len(cands) == 1 else None


def name_ok(name: str, names: list[str]) -> bool:
    """Two spellings plausibly of one person: close overall, or sharing a surname-length word."""
    words = {w for w in norm(name).split() if len(w) >= 3}
    return any(sim(name, n) >= 0.6 or words & {w for w in norm(n).split() if len(w) >= 3}
               for n in names)


def leftover(who: list[dict], free: list[tuple]) -> tuple | None:
    """(fight_id, {source's fighter id: fighter_id}) for a bout whose names didn't both match: the one
    fight in `free` ([(fight_id, [(fighter_id, names)] x2)], the event's fights no bout took)
    that has more of its fighters than any other."""
    best = []
    for fight_id, sides in free:
        for (a, b) in (sides, sides[::-1]):
            score = name_ok(who[0]["name"], a[1]) + name_ok(who[1]["name"], b[1])
            if score:
                best.append((score, fight_id, {who[0]["slug"]: a[0], who[1]["slug"]: b[0]}))
    best.sort(key=lambda t: -t[0])
    top = [t for t in best if t[0] == best[0][0]] if best else []
    return (top[0][1], top[0][2]) if len({t[1] for t in top}) == 1 and len(top) == 1 else None
