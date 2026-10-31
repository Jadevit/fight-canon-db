"""Export canon.db as Parquet for the site's SQL page and filtered leaderboards, which query it
in the browser with DuckDB-WASM: one file per table (zstd), each sorted the way queries walk
it, so DuckDB reads only the columns and row groups a query needs, a few requests at a time.

    python scripts/export_parquet.py [canon.db] [out dir]    # defaults: data/canon.db site/db

Writes <out>/<table>.parquet and <out>/config.json ({tables, build}); `build` names the files'
folder in R2, so each build gets its own.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import duckdb

NOT_ON_SITE = {"media_scores"}  # in canon.db only, for now

ORDER = {  # each table's row order: the ranges queries on it usually read
    "events": "date, event_id",
    "fights": "event_id, fight_id",
    "fight_participants": "fight_id, corner",
    "round_stats": "fight_id, fighter_id, round, source",
    "smartcage_round_stats": "fight_id, fighter_id, round",
    "sofascore_round_stats": "fight_id, fighter_id, round",
    "judge_scores": "fight_id, judge, round",
    "odds": "fight_id, fighter_id",
    "fighters": "fighter_id",
}


def export(src: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.parquet"):
        old.unlink()
    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite")
    con.execute("ATTACH ? AS canon (TYPE sqlite, READ_ONLY)", [str(src)])
    tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables "
                                        "WHERE table_catalog = 'canon' ORDER BY 1").fetchall()
              if r[0] not in NOT_ON_SITE]
    digest = hashlib.sha256()
    for t in tables:
        path = out / f"{t}.parquet"
        con.execute(f"COPY (SELECT * FROM canon.{t} ORDER BY {ORDER.get(t, 'ALL')}) TO '{path.as_posix()}' "
                    "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
        digest.update(path.read_bytes())
    con.close()
    config = dict(tables=tables, build=digest.hexdigest()[:12])
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


if __name__ == "__main__":
    args = sys.argv[1:]
    out = Path(args[1] if len(args) > 1 else "site/db")
    c = export(Path(args[0] if args else "data/canon.db"), out)
    size = sum(p.stat().st_size for p in out.glob("*.parquet"))
    print(f"{len(c['tables'])} tables, {size / 1e6:.0f} MB, build {c['build']}")
