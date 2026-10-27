"""One-off: convert a canon.db with source ids (UFC Stats hex, `sherdog:...`) to Fight Canon ids.

    python scripts/migrate_ids.py data/canon.db out.db

Events are numbered by date, fights by their event's date, then event, then card order (the
order they were written in, which the site shows), fighters by the date of their first fight. The old ids become aliases: UFC Stats ids under `ufcstats`, `sherdog:`
ids under `sherdog` (fights: '<event>:<fighter>-<fighter>'). A UFC Stats fight whose event
and both fighters are linked to Sherdog also gets the Sherdog alias it would have.
fighter_redirects (old ids) is dropped.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from canon_db import db  # noqa: E402

FIGHT_TABLES = ("round_stats", "smartcage_round_stats", "sofascore_round_stats", "judge_scores", "odds")


def cols(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def migrate(src: Path, out: Path) -> dict[str, int]:
    out.unlink(missing_ok=True)
    conn = sqlite3.connect(out)
    conn.executescript(db.SCHEMA.read_text())
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ATTACH ? AS old", (str(src),))
    x = conn.execute
    with conn:
        x("""CREATE TEMP TABLE ev AS SELECT event_id AS old,
                 ROW_NUMBER() OVER (ORDER BY date IS NULL, date, name, event_id) AS new FROM old.events""")
        x("""CREATE TEMP TABLE fi AS SELECT f.fight_id AS old,
                 ROW_NUMBER() OVER (ORDER BY e.date IS NULL, e.date, m.new, f.rowid) AS new
             FROM old.fights f JOIN old.events e USING (event_id) JOIN ev m ON m.old = f.event_id""")
        x("""CREATE TEMP TABLE fr AS SELECT f.fighter_id AS old,
                 ROW_NUMBER() OVER (ORDER BY d.first IS NULL, d.first, f.name, f.fighter_id) AS new
             FROM old.fighters f LEFT JOIN (
                 SELECT p.fighter_id, MIN(e.date) AS first FROM old.fight_participants p
                 JOIN old.fights USING (fight_id) JOIN old.events e USING (event_id) GROUP BY 1) d
             USING (fighter_id)""")
        for t in ("ev", "fi", "fr"):
            x(f"CREATE UNIQUE INDEX temp.{t}_old ON {t}(old)")

        def copy(table: str, maps: dict[str, str]) -> None:
            """Copy `table` from old, with each column in `maps` turned into its new id."""
            names = cols(conn, table)
            have = set(cols(conn, table, "old"))
            sel = [f"{maps[c]}.new" if c in maps else f"t.{c}" for c in names if c in have]
            joins = " ".join(f"JOIN {m} ON {m}.old = t.{c}" for c, m in maps.items())
            x(f"INSERT INTO main.{table} ({', '.join(c for c in names if c in have)}) "
              f"SELECT {', '.join(sel)} FROM old.{table} t {joins}")

        copy("events", {"event_id": "ev"})
        copy("fighters", {"fighter_id": "fr"})
        copy("fights", {"fight_id": "fi", "event_id": "ev"})
        copy("fight_participants", {"fight_id": "fi", "fighter_id": "fr"})
        for t in FIGHT_TABLES:
            copy(t, {"fight_id": "fi", "fighter_id": "fr"})
        copy("fighter_aliases", {"fighter_id": "fr"})
        copy("event_aliases", {"event_id": "ev"})
        copy("event_cards", {"event_id": "ev"})
        for t in ("promotions", "promotion_aliases", "event_pages"):
            copy(t, {})

        # The old ids as aliases.
        x("""INSERT INTO event_aliases (event_id, source, source_id)
             SELECT m.new, CASE WHEN e.source = 'sherdog' THEN 'sherdog' ELSE 'ufcstats' END,
                    CASE WHEN e.source = 'sherdog' THEN substr(e.event_id, 9) ELSE e.event_id END
             FROM old.events e JOIN ev m ON m.old = e.event_id""")
        x("""INSERT INTO fight_aliases (fight_id, source, source_id)
             SELECT m.new, CASE WHEN f.source = 'sherdog' THEN 'sherdog' ELSE 'ufcstats' END,
                    CASE WHEN f.source = 'sherdog' THEN substr(f.fight_id, 9) ELSE f.fight_id END
             FROM old.fights f JOIN fi m ON m.old = f.fight_id""")
        # Sherdog's key for a UFC Stats fight, when its event and both fighters have one Sherdog id.
        x("""INSERT OR IGNORE INTO fight_aliases (fight_id, source, source_id)
             SELECT m.new, 'sherdog', se.sid || ':' || min(CAST(a.sid AS INTEGER), CAST(b.sid AS INTEGER))
                    || '-' || max(CAST(a.sid AS INTEGER), CAST(b.sid AS INTEGER))
             FROM old.fights f JOIN fi m ON m.old = f.fight_id
             JOIN (SELECT event_id, min(source_id) AS sid FROM old.event_aliases WHERE source = 'sherdog'
                   GROUP BY 1 HAVING count(*) = 1) se USING (event_id)
             JOIN old.fight_participants pa ON pa.fight_id = f.fight_id AND pa.corner = 0
             JOIN old.fight_participants pb ON pb.fight_id = f.fight_id AND pb.corner = 1
             JOIN (SELECT fighter_id, min(source_id) AS sid FROM old.fighter_aliases WHERE source = 'sherdog'
                   GROUP BY 1 HAVING count(DISTINCT source_id) = 1) a ON a.fighter_id = pa.fighter_id
             JOIN (SELECT fighter_id, min(source_id) AS sid FROM old.fighter_aliases WHERE source = 'sherdog'
                   GROUP BY 1 HAVING count(DISTINCT source_id) = 1) b ON b.fighter_id = pb.fighter_id
             WHERE f.source = 'ufcstats'""")
        # Corners mean nothing in fights UFC Stats doesn't have: ordered by fighter id, as the
        # loaders write them (two steps, so the swap never collides).
        not_ufc = "fight_id NOT IN (SELECT fight_id FROM fight_aliases WHERE source = 'ufcstats')"
        x(f"UPDATE fight_participants SET corner = corner + 10 WHERE {not_ufc}")
        x(f"""UPDATE fight_participants SET corner = (SELECT count(*) FROM fight_participants o
                  WHERE o.fight_id = fight_participants.fight_id AND o.fighter_id < fight_participants.fighter_id)
              WHERE {not_ufc}""")

    counts = {}
    for t in db.TABLES:
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        old_t = {"merged_ids": None, "fight_aliases": None}.get(t, t)
        if old_t and t not in ("event_aliases",):
            n_old = conn.execute(f"SELECT COUNT(*) FROM old.{old_t}").fetchone()[0]
            if n_old != counts[t]:
                raise RuntimeError(f"{t}: {n_old} rows before, {counts[t]} after")
    problems = conn.execute("PRAGMA main.foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"foreign key problems: {problems[:5]}")
    conn.commit()
    conn.execute("DETACH old")
    conn.execute("VACUUM")
    conn.close()
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    for t, n in migrate(Path(sys.argv[1]), Path(sys.argv[2])).items():
        print(f"{t:24} {n:>9,}")
