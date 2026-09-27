# canon-db

A SQLite database of UFC events, fights and round-by-round stats, scraped from
[UFC Stats](http://ufcstats.com) and updated automatically.

- Database: [`data/canon.db`](data/canon.db)
- Coverage and row counts: [`data/summary.json`](data/summary.json)
- Schema: [`canon_db/schema.sql`](canon_db/schema.sql)

## Use it from another repo

```bash
curl -L -o canon.db https://raw.githubusercontent.com/<owner>/<repo>/main/data/canon.db
```

## Tables

All tables link by UFC Stats ID.

| Table | One row per | Keys |
| --- | --- | --- |
| `events` | event | `event_id` |
| `fights` | fight | `fight_id` → `event_id` |
| `fight_participants` | fighter in a fight (2 per fight) | `fight_id`, `fighter_id` |
| `round_stats` | fighter per round | `fight_id`, `fighter_id`, `round` |
| `fighters` | fighter | `fighter_id` |
| `fighter_aliases` | spelling of a fighter's name | `fighter_id` |
| `judge_scores` | reserved (empty) | `fight_id`, `fighter_id` |
| `odds` | reserved (empty) | `fight_id`, `fighter_id` |

Stats that weren't recorded are `NULL`, not `0`.

## How it updates

A GitHub Action runs daily. It fetches any new events from UFC Stats, reloads the
last 21 days (UFC Stats often posts stats and corrections late), and commits
`data/canon.db` only if something changed.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m canon_db update     # update the database
.venv/bin/python -m pytest -q tests/    # run tests
```
