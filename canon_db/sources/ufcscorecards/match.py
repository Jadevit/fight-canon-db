"""Match a read scorecard to its fight in canon.db, then check it for consistency.

Matching is event first: the event name on the card picks one event, and the fighter
names only have to pick one of that event's bouts, which come from the database and are
known to be correct. Searching every fight is the fallback.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from collections import Counter


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    return " ".join(re.sub(r"[^A-Z ]", " ", s).split())


def sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class Matcher:
    def __init__(self, conn: sqlite3.Connection, since="2019-06-01"):
        spellings = {}
        for fid, name in conn.execute("SELECT fighter_id, name FROM fighter_aliases"):
            spellings.setdefault(fid, []).append(name)
        self.fights, self.events = [], {}
        for fid, ename, date, method, end_round, end_time, details, time_format in conn.execute("""
                SELECT f.fight_id, e.name, e.date, f.method, f.end_round, f.end_time, f.details,
                       f.time_format
                FROM fights f JOIN events e USING (event_id) WHERE e.date >= ?""", (since,)):
            corners = [(r[0], spellings.get(r[0], [])) for r in conn.execute(
                "SELECT fighter_id FROM fight_participants WHERE fight_id = ? ORDER BY corner", (fid,))]
            f = dict(fight_id=fid, event=ename, date=date, method=method or "", end_round=end_round,
                     end_time=end_time, details=details or "", time_format=time_format or "",
                     corners=corners)
            self.fights.append(f)
            self.events.setdefault((ename, date), []).append(f)

    # --- matching ---------------------------------------------------------------------

    def match_event(self, card):
        """The card's event: best event-name match. None if unsure."""
        cand = [card.get("event") or ""] + [l for l in (card.get("top_text") or "").splitlines()
                                             if len(l.strip()) > 8]
        text = " ".join(cand)
        scored = []
        for (name, date), fights in self.events.items():
            s = max(sim(c, name) for c in cand) + (0.3 if card.get("date") == date else 0)
            # tie-breakers for rematch events with near-identical names: event number and year
            s += 0.2 * any(n in text for n in re.findall(r"\b\d{3}\b", name))
            s += 0.1 * (date[:4] in text)
            scored.append((s, fights))
        scored.sort(key=lambda t: -t[0])
        if scored and scored[0][0] >= 0.75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
            return scored[0][1]
        return None

    def match(self, card):
        """(fight, red_is_corner0, error)."""
        fights = self.match_event(card)
        if fights:
            # Score each bout by how well both fighters' names appear anywhere in the card's
            # name text (header, name boxes, result line): no line parsing needed.
            tokens = norm(" ".join([card.get("names_text", ""), card.get("red", ""),
                                    card.get("blue", ""), card.get("result", "")])).split()

            def present(names):
                best = 0
                for n in names:
                    parts = [p for p in norm(n).split() if len(p) > 1]
                    if parts:
                        best = max(best, sum(max((sim(p, t) for t in tokens), default=0)
                                             for p in parts) / len(parts))
                return best

            pairs = []
            for f in fights:
                if len(f["corners"]) != 2:
                    continue
                s = (present(f["corners"][0][1]) + present(f["corners"][1][1])) / 2
                pairs.append((s, f, self._orientation(card, f)))
            pairs.sort(key=lambda t: -t[0])
            if pairs and pairs[0][0] >= 0.5 and (len(pairs) == 1 or pairs[0][0] - pairs[1][0] >= 0.15):
                return pairs[0][1], pairs[0][2], None
        return self._match_global(card)

    @staticmethod
    def _name_score(card_name, names):
        return max((sim(card_name, n) for n in names), default=0)

    def _orientation(self, card, f):
        """True if the card's red corner is the fight's corner 0."""
        a = self._name_score(card["red"], f["corners"][0][1]) + self._name_score(card["blue"], f["corners"][1][1])
        b = self._name_score(card["red"], f["corners"][1][1]) + self._name_score(card["blue"], f["corners"][0][1])
        return a >= b

    def _match_global(self, card):
        best = []
        for f in self.fights:
            if len(f["corners"]) != 2:
                continue
            c0, c1 = f["corners"][0][1], f["corners"][1][1]
            a = self._name_score(card["red"], c0) + self._name_score(card["blue"], c1)
            b = self._name_score(card["red"], c1) + self._name_score(card["blue"], c0)
            s = max(a, b) / 2
            if s < 0.75:
                # one name garbled: accept if the other is a strong match on the same date
                one = max(self._name_score(card["red"], c0), self._name_score(card["blue"], c1),
                          self._name_score(card["red"], c1), self._name_score(card["blue"], c0))
                if not (one >= 0.9 and card.get("date") == f["date"]):
                    continue
                s = one / 2
            bonus = 0.3 * (card.get("date") == f["date"])
            if card.get("event"):
                bonus += 0.2 * sim(card["event"], f["event"])
            if f["end_time"] and f["end_time"] in (card.get("result") or ""):
                bonus += 0.2
            best.append((s + bonus, f, a >= b))
        best.sort(key=lambda t: -t[0])
        if not best:
            return None, None, "no match"
        if len(best) > 1 and best[0][0] - best[1][0] < 0.05:
            return None, None, f"ambiguous: {best[0][1]['fight_id']} vs {best[1][1]['fight_id']}"
        return best[0][1], best[0][2], None


# --- checks -----------------------------------------------------------------------------

def _judge_totals(details):
    """'Sal D'amato 29 - 28. Chris Lee 28 - 29.' -> {surname: sorted (a, b)}"""
    out = {}
    for name, a, b in re.findall(r"([A-Za-z'. -]+?)\s+(\d+)\s*-\s*(\d+)\.?", details):
        out[norm(name).split()[-1] if norm(name) else name] = tuple(sorted((int(a), int(b))))
    return out


def check(card, fight):
    """(rounds scored, [problems]). A card is trusted only with no problems."""
    problems = []
    judges = card["judges"]
    if len(judges) != 3:
        problems.append("not 3 judges")
    filled = []

    def ded(v):
        return v if isinstance(v, int) else 0

    for j in judges:
        rows = [r for r in j["rounds"] if r["red"] is not None or r["blue"] is not None]
        filled.append(len(rows))
        for r in rows:
            if not (isinstance(r["red"], int) and isinstance(r["blue"], int)):
                problems.append(f"{j['judge']}: unreadable score {r['red']}/{r['blue']}")
                break
            before = (r["red"] + ded(r["red_ded"]), r["blue"] + ded(r["blue_ded"]))  # before deductions
            if max(before) != 10 or min(before) < 7:
                problems.append(f"{j['judge']}: invalid round {r['red']}-{r['blue']}")
        rt, bt = j["total"]
        if rows and all(isinstance(r["red"], int) and isinstance(r["blue"], int) for r in rows):
            sr, sb = sum(r["red"] for r in rows), sum(r["blue"] for r in rows)
            dr, db_ = sum(ded(r["red_ded"]) for r in rows), sum(ded(r["blue_ded"]) for r in rows)
            if (rt is not None or bt is not None) and (rt, bt) not in {(sr, sb), (sr - dr, sb - db_)}:
                problems.append(f"{j['judge']}: rounds sum {sr}-{sb} != total {rt}-{bt}")
    if len(set(filled)) > 1:
        problems.append(f"judges disagree on rounds scored {filled}")
    n = filled[0] if filled else 0

    decision = fight["method"].startswith("Decision")
    er, m = fight["end_round"], fight["method"]
    if er:
        if decision:
            expected = {er}
        else:
            expected = {er - 1}
            # the last round counts as completed when the fight ended at the bell or between rounds
            lens = re.search(r"\(([\d-]+)\)", fight["time_format"])
            parts = lens.group(1).split("-") if lens else []
            last_len = int(parts[er - 1]) if len(parts) >= er else 5
            if fight["end_time"] == f"{last_len}:00" or any(k in m for k in ("Doctor", "Overturned", "Could Not Continue")):
                expected.add(er)
        if n not in expected:
            problems.append(f"{n} rounds scored, expected {sorted(expected)} ({m}, R{er} {fight['end_time']})")
    if decision:
        db_vals = sorted(_judge_totals(fight["details"]).values())
        card_vals = sorted(tuple(sorted(j["total"])) for j in judges
                           if all(isinstance(v, int) for v in j["total"]))
        # UFC Stats' details sometimes lists fewer than three judges: then it only has to be a subset
        ok = card_vals == db_vals if len(db_vals) >= 3 else not (Counter(db_vals) - Counter(card_vals))
        if db_vals and not ok:
            problems.append(f"totals {card_vals} != UFC Stats {db_vals}")
    return n, problems
