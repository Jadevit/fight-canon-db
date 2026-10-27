# fight-canon-db

A SQLite database of MMA events, fights and round-by-round stats (UFC, PRIDE and more),
scraped from [UFC Stats](http://ufcstats.com), with fighters' full pro records from
[Sherdog](https://www.sherdog.com), their own stats from [PFL](https://pflmma.com) and
[Oktagon](https://oktagonmma.com), [Sofascore](https://www.sofascore.com)'s stats for promotions
that publish none, and betting lines from [BestFightOdds](https://www.bestfightodds.com),
updated automatically.

- Browse it: **https://fightcanon.com**
- JSON (a file per fighter and event, rebuilt daily): `https://db.fightcanon.com/v1/`
- Database: [latest release](https://github.com/jadevit/fight-canon-db/releases/latest) (`canon.db`)
- Coverage and row counts: [`data/summary.json`](data/summary.json)
- Schema: [`canon_db/schema.sql`](canon_db/schema.sql)

## Use it from another repo

```bash
curl -L -o canon.db https://github.com/jadevit/fight-canon-db/releases/latest/download/canon.db
```

Every update is its own release (`db-YYYY-MM-DD-HHMM`), so you can pin a version:
`.../releases/download/<tag>/canon.db`. Releases are pruned to the newest 30 plus the first of
each month; to keep another one for good, add its tag to
[`data/keep-releases.txt`](data/keep-releases.txt). To work on this repo, put the latest one at
`data/canon.db` first: `gh release download --pattern canon.db --dir data`.

## Tables

Fighters, events and fights have Fight Canon IDs: numbers given once and never changed. Every
source's own id for them is in `fighter_aliases`, `event_aliases` and `fight_aliases`
(`source`, `source_id`), so a UFC fight has a UFC Stats alias, a Sherdog alias, and so on. For
UFC Stats' fights, filter
`fight_id IN (SELECT fight_id FROM fight_aliases WHERE source = 'ufcstats')`. Where sources
disagree, the promotions' own sites (UFC Stats, PFL, Oktagon) are taken over Sherdog, which
fills in what they leave blank.

| Table                | One row per                      | Keys                              |
| -------------------- | -------------------------------- | --------------------------------- |
| `events`             | event (with its `promotion`)     | `event_id`                        |
| `promotions`         | league (parent, card coverage)   | `promotion`                       |
| `promotion_aliases`  | league's id in another source    | `promotion`                       |
| `fights`             | fight                            | `fight_id` → `event_id`           |
| `fight_participants` | fighter in a fight (2 per fight) | `fight_id`, `fighter_id`          |
| `round_stats`        | fighter per round                | `fight_id`, `fighter_id`, `round` |
| `smartcage_round_stats` | fighter per round (older PFL) | `fight_id`, `fighter_id`, `round` |
| `sofascore_round_stats` | fighter per round (Sofascore's extras) | `fight_id`, `fighter_id`, `round` |
| `fighters`           | fighter (bio + pro record)       | `fighter_id`                      |
| `fighter_aliases`    | fighter's name and id per source | `fighter_id`                      |
| `merged_ids`         | fighter, event or fight merged into another | `kind`, `old_id` → `new_id` |
| `event_aliases`      | event's id per source            | `event_id`                        |
| `fight_aliases`      | fight's id per source            | `fight_id`                        |
| `event_cards`        | event whose whole card was read  | `event_id`                        |
| `judge_scores`       | judge's score per round          | `fight_id`, `fighter_id`          |
| `odds`               | fighter's betting line per fight | `fight_id`, `fighter_id`          |

`round_stats.source` says who counted: `ufcstats`; `pfl` for PFL events from 2025-12 on (same
columns, no per-round reversals); `oktagon`, which only has what landed (strikes, significant
strikes by target, takedowns) and submission attempts, with every other column NULL; or
`sofascore`, a third party's count for promotions with no official stats (ONE, RIZIN, KSW, ACA,
BRAVE, Cage Warriors, Fight Nation, Jungle Fight) and for Oktagon fights in place of
Oktagon's: significant strikes landed and by target, total strikes thrown (not landed),
knockdowns, takedowns, submission attempts and control time. Its strikes by position and time
in each position are in `sofascore_round_stats`. A fight's stats come from one source. Different
crews score fights, so filter on it when the numbers have to be comparable. PFL's earlier events (2018 to 2025-11) have a different set of
stats (strikes by arm / leg / ground, ground and standing time), kept in `smartcage_round_stats`.

`fighters.sex` is `M` or `F` where a source says so (UFC Stats' divisions, Oktagon's bouts), and
NULL otherwise.

Stats that weren't recorded are `NULL`, not `0`. Odds are American and cover 2007 onward:
the opening line and the lowest/highest closing line across sportsbooks. Per-round judge
scores come from official UFC scorecards and cover UFC fights from 2020-08 to 2024-11.

`fight_participants.corner` is red (0) / blue (1) only from 2010-03-21 on. Before that
UFC Stats usually lists the winner first, so don't use corner as a feature for older fights.
In fights UFC Stats doesn't have, corner means nothing. Events no promotion's site has are
labelled by their Sherdog organization: the label we already use if its events overlap ours,
else its full name.

Fights get in two ways. Every event in Sherdog's event sitemaps has its card read whole, and
those events are in `event_cards` (nearly all of them). Other fights come from the pages of
fighters who fought on a promotion's own cards, or from the fighters UFC Stats tracks on other
promotions' cards, so they are there because of who fought in them: filter on `event_cards`
when that matters. Only pro MMA fights are kept. Event pages don't say whether a bout was pro,
so card fights are checked on the fighters' Sherdog pages and amateur and exhibition bouts no
promotion's site has are removed; `fights.bout_type` is what those pages list it as, and NULL
until checked.

A fighter is linked to a source's id only through a fight both list (same date, the other
fighter already linked), never by name alone. When two rows turn out to be one fighter, event
or fight, the older ID is kept and `merged_ids` records the other.

To filter by promotion, join through `events`:

```sql
SELECT f.* FROM fights f JOIN events e USING (event_id) WHERE e.promotion = 'PRIDE';
```

## How it updates

A GitHub Action runs daily. It fetches any new events from UFC Stats, reloads the
last 21 days (UFC Stats often posts stats and corrections late), and if something
changed publishes the database as a new release and commits `data/summary.json`. A second, weekly Action picks up events UFC Stats
leaves off its events list (Dana White's Contender Series, PRIDE and other promotions).
Sherdog pages of fighters from the last 21 days' cards are read each day too, and so are
Sherdog's new and recent event cards, with PFL's, Oktagon's and Sofascore's stats.

## The website

`site/` is a static page (fightcanon.com): fighters, events, round-by-round fight stats,
leaderboards and a SQL box. Its pages read JSON files written by `scripts/site_json.py`; the
SQL box and filtered leaderboards query the database in the browser with
[DuckDB-WASM](https://duckdb.org/docs/api/wasm/overview), from Parquet files written by
`scripts/export_parquet.py`, reading only the columns a query needs.
`.github/workflows/deploy.yml` publishes it after every database update. To look at it
locally (needs `pip install duckdb`):

```bash
python scripts/site_json.py build data/canon.db site/db
python scripts/export_parquet.py data/canon.db site/db
python scripts/serve.py      # then open http://localhost:8000/
```

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m canon_db update     # update the database
.venv/bin/python -m pytest -q tests/    # run tests
```
