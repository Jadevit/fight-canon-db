"""Corrections by hand, from corrections.csv: the top tier, above every source.

Each row has its own `id` (never reused), what it corrects, a citation, an explanation in words
(what was wrong, and why the new value is right), who submitted it, who accepted it, and the date.
Only accepted rows belong in the file; validate() refuses any other, and the update fails on one.

  kind       target      field                       value
  fighter    fighter id  a column of fighters        the right value
  event      event id    a column of events          the right value
  fight      fight id    a column of fights          the right value
  result     fight id    a fighter id in that fight  W, L, D or NC (the other side's follows)
  same       an id       fighter, event or fight     another id: the two are one (merged)
  different  an id       fighter, event or fight     another id: the two are never merged

apply() runs at the start of every update: it copies the file into `corrections`, merges what
`same` says, and writes values and results as claims of source 'correction', which resolve()
ranks above every source whenever it runs. Ids follow `retired_ids`: a correction to an id
since merged applies to the row it became; one to an id that's gone is skipped and logged.
Taking a row out of the file undoes it at the next update (merges aside: a merge stays).
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from canon_db import claims, ids
from canon_db.db import COLUMNS

FILE = Path(__file__).resolve().parent / "corrections.csv"
FIELDS = ("id", "kind", "target", "field", "value", "citation", "explanation", "submitted_by", "accepted_by",
          "date")
VALUES = {"fighter": "fighters", "event": "events", "fight": "fights"}  # kind -> main table
RESULTS = {"W": "L", "L": "W", "D": "D", "NC": "NC"}  # a side's result -> the other side's
SOURCE = "correction"

log = logging.getLogger("canon_db")


def read(path: Path = FILE) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise RuntimeError(f"{path.name}: the columns must be {', '.join(FIELDS)}")
        return [{k: (v or "").strip() for k, v in r.items()} for r in reader]


def _columns(kind: str) -> set[str]:
    """The columns a correction of `kind` can set: those its claims and its main table both have."""
    table = VALUES[kind]
    key = f"{kind}_id"
    return {c for c in COLUMNS[claims.TABLES[kind][0]] if c in COLUMNS[table]} - {key, *claims.KEY} \
        - {c for t, c in claims.COUNTED if t == table}


def validate(rows: list[dict[str, str]]) -> list[str]:
    """What's wrong with the file's rows, as messages (none: it's fine)."""
    errors, seen = [], set()
    for n, r in enumerate(rows, 2):
        at = f"line {n} (id {r['id'] or '?'})"
        if not r["id"].isdigit():
            errors.append(f"{at}: `id` must be a number")
        elif r["id"] in seen:
            errors.append(f"{at}: `id` is used twice")
        seen.add(r["id"])
        for c in ("citation", "explanation", "submitted_by", "accepted_by"):
            if not r[c]:
                errors.append(f"{at}: no {c}" + (" (only accepted corrections go in the file)" if c == "accepted_by" else ""))
        try:
            if date.fromisoformat(r["date"]).isoformat() != r["date"]:
                raise ValueError
        except ValueError:
            errors.append(f"{at}: `date` must be YYYY-MM-DD")
        kind, target, field, value = r["kind"], r["target"], r["field"], r["value"]
        if kind in VALUES:
            if field not in _columns(kind):
                errors.append(f"{at}: {VALUES[kind]} has no column {field!r} a correction can set")
            if not value:
                errors.append(f"{at}: no value")
        elif kind == "result":
            if not field.isdigit():
                errors.append(f"{at}: `field` must be a fighter id")
            if value not in RESULTS:
                errors.append(f"{at}: `value` must be one of {', '.join(RESULTS)}")
        elif kind in ("same", "different"):
            if field not in VALUES:
                errors.append(f"{at}: `field` must be fighter, event or fight")
            if not value.isdigit() or value == target:
                errors.append(f"{at}: `value` must be another id")
        else:
            errors.append(f"{at}: no kind {kind!r}")
            continue
        if not target.isdigit():
            errors.append(f"{at}: `target` must be an id")
    return errors


def _typed(conn: sqlite3.Connection, kind: str, field: str, value: str) -> int | str:
    """`value` as its column stores it."""
    types = {r[1]: r[2].upper() for r in conn.execute(f"PRAGMA table_info({VALUES[kind]})")}
    return int(value) if types.get(field) == "INTEGER" else value


def apply(conn: sqlite3.Connection, path: Path = FILE) -> Counter:
    """Copy the file into `corrections` and apply it (see the module's docstring). Raises
    RuntimeError if the file has a row validate() refuses. Returns counts."""
    rows = read(path)
    errors = validate(rows)
    if errors:
        raise RuntimeError(f"{path.name}: " + "; ".join(errors))
    counts: Counter = Counter()
    with conn:
        conn.execute("DELETE FROM corrections")
        conn.executemany(f"INSERT INTO corrections ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
                         [tuple(r[c] or None for c in FIELDS) for r in rows])
        for t in ("fighter_claims", "event_claims", "fight_claims", "participant_claims"):
            if conn.execute(f"DELETE FROM {t} WHERE source = ?", (SOURCE,)).rowcount:
                claims.dirty = True

        def skip(r: dict, why: str) -> None:
            log.warning("Correction %s not applied: %s.", r["id"], why)
            counts["not applied"] += 1

        for r in (r for r in rows if r["kind"] == "same"):
            kind = r["field"]
            a, b = ids.now(conn, kind, int(r["target"])), ids.now(conn, kind, int(r["value"]))
            if a is None or b is None:
                skip(r, f"{kind} {r['target'] if a is None else r['value']} is gone")
            elif a != b:
                if ids.merge(conn, kind, a, b) is None:
                    skip(r, f"a `different` correction keeps {kind}s {a} and {b} apart")
                else:
                    counts["merged"] += 1
        for r in (r for r in rows if r["kind"] in VALUES):
            kind = r["kind"]
            id_ = ids.now(conn, kind, int(r["target"]))
            if id_ is None:
                skip(r, f"{kind} {r['target']} is gone")
                continue
            claims.claim(conn, kind, id_, SOURCE, str(id_), {r["field"]: _typed(conn, kind, r["field"], r["value"])},
                         mode="fill")
            counts["values"] += 1
        results: dict[int, dict[int, str]] = {}
        for r in (r for r in rows if r["kind"] == "result"):
            fight, fighter = ids.now(conn, "fight", int(r["target"])), ids.now(conn, "fighter", int(r["field"]))
            if fight is None or fighter is None:
                skip(r, f"{'fight' if fight is None else 'fighter'} {r['target'] if fight is None else r['field']} is gone")
                continue
            results.setdefault(fight, {})[fighter] = r["value"]
        for fight, said in results.items():
            sides = conn.execute("SELECT fighter_id, corner FROM fight_participants WHERE fight_id = ? ORDER BY corner",
                                 (fight,)).fetchall()
            if len(sides) != 2 or not set(said) <= {f for f, _ in sides}:
                for r in (r for r in rows if r["kind"] == "result" and ids.now(conn, "fight", int(r["target"])) == fight):
                    skip(r, f"fighter {r['field']} isn't in fight {fight}")
                continue
            (a, ca), (b, cb) = sides
            ra = said.get(a) or RESULTS[said[b]]
            rb = said.get(b) or RESULTS[ra]
            if RESULTS[ra] != rb:
                raise RuntimeError(f"{path.name}: fight {fight}'s corrected results don't fit ({ra} and {rb})")
            claims.sides(conn, fight, SOURCE, str(fight), [dict(fighter_id=a, result=ra, corner=ca),
                                                         dict(fighter_id=b, result=rb, corner=cb)])
            counts["results"] += 1
    counts["different"] = sum(r["kind"] == "different" for r in rows)
    return counts

