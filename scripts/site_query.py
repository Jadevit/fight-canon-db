"""The Query page's help: the tables an AI assistant (or a person) needs, from schema.sql's own
comments, and what to tell an assistant so its query is right. The page puts the question at
the end. Bookkeeping tables (aliases, redirects, pages read) are left out.
"""

from __future__ import annotations

import re
from pathlib import Path

from site_boards import DIVISIONS

SCHEMA = Path(__file__).resolve().parent.parent / "canon_db" / "schema.sql"

QUERY_TABLES = ["fighters", "events", "fights", "fight_participants", "round_stats", "smartcage_round_stats",
                "sofascore_round_stats", "judge_scores", "odds", "promotions", "event_cards"]
BOUTS_COLUMNS = [  # the page's bouts view: each fight with its event and both fighters
    ("fight_id", "INTEGER", ""), ("event_id", "INTEGER", ""), ("date", "TEXT", "YYYY-MM-DD"),
    ("event", "TEXT", "the event's name"), ("promotion", "TEXT", ""),
    ("division", "TEXT", "division(weight_class)"),
    ("a_id", "INTEGER", "one fighter (a/b order means nothing)"), ("a_name", "TEXT", ""),
    ("a_res", "TEXT", "W, L, D or NC"), ("b_id", "INTEGER", "the other fighter"), ("b_name", "TEXT", ""),
    ("b_res", "TEXT", ""), ("...", "", "and every column of fights"),
]
QUERY_RULES = f"""You are writing one SQL query for Fight Canon, an open database of pro MMA fights (amateur
and exhibition bouts aren't in it). It runs in DuckDB, so write DuckDB SQL. Reply with only the
query, in one code block.

How the data works:
- NULL means not recorded, never 0. Don't treat missing stats as zero.
- Dates are text, 'YYYY-MM-DD': compare them as text; the year is substr(date, 1, 4).
- For anything about fights, use the view bouts (each fight with its event and both fighters).
  A fighter's fights: WHERE <fighter_id> IN (a_id, b_id). Their result: CASE WHEN a_id = ... THEN
  a_res ELSE b_res END. Or join fight_participants, which has a row per fighter per fight.
- Results are W (win), L (loss), D (draw), NC (no contest).
- method is free text in two styles ('KO/TKO', 'Submission', 'Decision - Unanimous' from UFC Stats;
  'TKO (Punches)', 'Submission (Armbar)', 'Decision (Split)' from Sherdog). Match with ILIKE:
  '%KO%' for knockouts, '%Submission%', '%Decision%'.
- division(weight_class) gives the division: one of {", ".join(DIVISIONS)}, or Other.
- Find fighters by name with ILIKE (names are as the sources spell them); a fighter's pro record
  as UFC Stats shows it is in fighters.wins/losses/draws/no_contests (NULL for fighters it doesn't have).
- Ids are Fight Canon's own numbers. For UFC Stats' fights, use promotion = 'UFC' (events), or
  fight_id IN (SELECT fight_id FROM fight_aliases WHERE source = 'ufcstats') for every fight UFC Stats has.
- Stats per round are in round_stats, one row per fighter per round. Its source column says who
  counted ('ufcstats' is the most complete); sources count differently, so rank or compare within
  one source. A fight can have rows from two sources (Oktagon's and Sofascore's): filter on
  source. Accuracy is landed / attempted over rows where attempted IS NOT NULL.
- Promotion names are as stored ('UFC', 'Bellator MMA', 'PFL', 'ONE FC', ...); events.promotion.

How to answer:
- Name every output column in plain English with AS "Title Case Name".
- When rows are fighters, events or fights, also select fighter_id, event_id or fight_id: the page
  turns them into links.
- Order the rows meaningfully, and LIMIT 100 unless the question asks for everything.
"""


def schema_tables() -> list[dict]:
    """The tables in QUERY_TABLES as schema.sql describes them: [{name, about, columns:
    [[name, type, about]]}], with the comment above a table and each column's own."""
    tables, about, current = {}, [], None
    for line in SCHEMA.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if current is None:
            if text.startswith("--"):
                about.append(text[2:].strip())
            elif m := re.match(r"CREATE TABLE (\w+)", text):
                current = tables[m.group(1)] = {"name": m.group(1), "about": " ".join(about), "columns": []}
            else:
                about = []
            continue
        if text.startswith(")"):
            current, about = None, []
        elif text.startswith("--"):  # a column's comment, carried on
            if current["columns"]:
                col = current["columns"][-1]
                col[2] = f"{col[2]} {text[2:].strip()}".strip()
        elif m := re.match(r"(\w+)\s+(TEXT|INTEGER|REAL)\b[^-]*(?:--\s*(.*))?", text):
            current["columns"].append([m.group(1), m.group(2), (m.group(3) or "").strip()])
    out = [tables[t] for t in QUERY_TABLES]
    out.insert(3, {"name": "bouts", "about": "A view the page adds: each fight with its event and both fighters. "
                   "Start here for questions about fights.", "columns": [list(c) for c in BOUTS_COLUMNS]})
    return out


def query_help() -> dict:
    """query.json: what the Query page needs to help: the rules for an assistant, and the tables."""
    return {"rules": QUERY_RULES, "tables": schema_tables()}
