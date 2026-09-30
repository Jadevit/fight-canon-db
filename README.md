# canon-db

A SQLite database of MMA events, fights and round-by-round stats (UFC, PRIDE and more),
scraped from [UFC Stats](http://ufcstats.com) and updated automatically.

- Browse it: **https://jadevit.github.io/fight-canon-db/** (works on phones)
- Database: [`data/canon.db`](data/canon.db)
- Coverage and row counts: [`data/summary.json`](data/summary.json)
- Schema: [`canon_db/schema.sql`](canon_db/schema.sql)

## Use it from another repo

```bash
curl -L -o canon.db https://raw.githubusercontent.com/jadevit/fight-canon-db/main/data/canon.db
```

## Tables

All tables link by UFC Stats ID.

| Table                | One row per                      | Keys                              |
| -------------------- | -------------------------------- | --------------------------------- |
| `events`             | event (with its `promotion`)     | `event_id`                        |
| `fights`             | fight                            | `fight_id` → `event_id`           |
| `fight_participants` | fighter in a fight (2 per fight) | `fight_id`, `fighter_id`          |
| `round_stats`        | fighter per round                | `fight_id`, `fighter_id`, `round` |
| `fighters`           | fighter (bio + pro record)       | `fighter_id`                      |
| `fighter_aliases`    | spelling of a fighter's name     | `fighter_id`                      |
| `judge_scores`       | reserved (empty)                 | `fight_id`, `fighter_id`          |
| `odds`               | reserved (empty)                 | `fight_id`, `fighter_id`          |

Stats that weren't recorded are `NULL`, not `0`.

To filter by promotion, join through `events`:

```sql
SELECT f.* FROM fights f JOIN events e USING (event_id) WHERE e.promotion = 'PRIDE';
```

## How it updates

A GitHub Action runs daily. It fetches any new events from UFC Stats, reloads the
last 21 days (UFC Stats often posts stats and corrections late), and commits
`data/canon.db` if something changes. A second, weekly Action picks up events UFC Stats
leaves off its events list (Dana White's Contender Series, PRIDE and other promotions).

## The website

`site/` is a static page that loads the whole database into the browser with
[sql.js](https://sql.js.org): fighter search, events, round-by-round fight stats,
leaderboards and a SQL box. `.github/workflows/pages.yml` publishes it to GitHub
Pages (with a gzipped copy of the DB) after every database update.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m canon_db update     # update the database
.venv/bin/python -m pytest -q tests/    # run tests
```
