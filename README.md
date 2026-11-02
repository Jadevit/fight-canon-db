# fight-canon-db

[![Update database](https://github.com/jadevit/fight-canon-db/actions/workflows/update-db.yml/badge.svg)](https://github.com/jadevit/fight-canon-db/actions/workflows/update-db.yml)
[![Data through](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fjadevit%2Ffight-canon-db%2Fmain%2Fdata%2Fsummary.json&query=%24.coverage.last_event_date&label=data%20through&color=blue)](https://github.com/jadevit/fight-canon-db/releases/latest)
[![License: MIT](https://img.shields.io/github/license/jadevit/fight-canon-db)](LICENSE)
[![Website](https://img.shields.io/badge/browse-fightcanon.com-E3A63B)](https://fightcanon.com)

An open SQLite database of pro MMA: events, fights, fighters, round-by-round stats, judges'
scores and betting lines, updated automatically.

- Browse it: **https://fightcanon.com**
- JSON (a file per fighter and event, rebuilt daily): `https://db.fightcanon.com/v1/`
- Database: [latest release](https://github.com/jadevit/fight-canon-db/releases/latest) (`canon.db`)
- Coverage and row counts: [`data/summary.json`](data/summary.json)
- Schema, with a comment on every table: [`canon_db/schema.sql`](canon_db/schema.sql)

## Where the data comes from

| Source | What it adds |
| ------ | ------------ |
| [UFC Stats](http://ufcstats.com) | Fights and full round-by-round stats, for the UFC and other promotions it hosts |
| [Sherdog](https://www.sherdog.com) | Results: whole cards from promotions worldwide, and fighters' pro records |
| [PFL](https://pflmma.com), [Oktagon](https://oktagonmma.com) | Their own stats |
| [Sofascore](https://www.sofascore.com) | A third party's stats, for promotions that publish none, and for Oktagon |
| [MMA Decisions](https://mmadecisions.com) | Judges' cards and media scores of decisions |
| [BestFightOdds](https://www.bestfightodds.com) | Betting lines |

## Use it from another repo

```bash
curl -L -o canon.db https://github.com/jadevit/fight-canon-db/releases/latest/download/canon.db
```

Every update is its own release (`db-YYYY-MM-DD-HHMM`), so you can pin a version:
`.../releases/download/<tag>/canon.db`. Releases are pruned to the newest 30 plus the first of
each month; to keep another one for good, add its tag to
[`data/keep-releases.txt`](data/keep-releases.txt). To work on this repo, put the latest one at
`data/canon.db` first: `scripts/get-db.sh`.

## Tables

Fighters, events and fights have Fight Canon IDs: numbers given once and never changed. Every
source's own id for them is in `fighter_aliases`, `event_aliases` and `fight_aliases`
(`source`, `source_id`), so a UFC fight has a UFC Stats alias, a Sherdog alias, and so on. For
UFC Stats' fights, filter
`fight_id IN (SELECT fight_id FROM fight_aliases WHERE source = 'ufcstats')`. Where sources
disagree, the promotions' own sites (UFC Stats, PFL, Oktagon) are taken over Sherdog, which
fills in what they leave blank.

| Table                   | One row per                                 | Key                                         |
| ----------------------- | ------------------------------------------- | ------------------------------------------- |
| `events`                | event (with its `promotion`)                | `event_id`                                  |
| `promotions`            | league (parent, card coverage)              | `promotion`                                 |
| `promotion_aliases`     | league's id in another source               | `source`, `source_id`                       |
| `fights`                | fight (→ `event_id`)                        | `fight_id`                                  |
| `fight_participants`    | fighter in a fight (2 per fight)            | `fight_id`, `corner`                        |
| `round_stats`           | fighter per round, per source               | `fight_id`, `fighter_id`, `round`, `source` |
| `smartcage_round_stats` | fighter per round (older PFL)               | `fight_id`, `fighter_id`, `round`           |
| `sofascore_round_stats` | fighter per round (Sofascore's extras)      | `fight_id`, `fighter_id`, `round`           |
| `fighters`              | fighter (bio + pro record)                  | `fighter_id`                                |
| `fighter_aliases`       | fighter's name and id per source            | `source`, `source_id`, `name`               |
| `event_aliases`         | event's id per source                       | `source`, `source_id`                       |
| `fight_aliases`         | fight's id per source                       | `source`, `source_id`                       |
| `merged_ids`            | fighter, event or fight merged into another | `kind`, `old_id`                            |
| `event_cards`           | event whose whole card was read             | `event_id`                                  |
| `event_pages`           | event page a card reader has read           | `source`, `source_id`                       |
| `non_pro_bouts`         | bout deleted as amateur or exhibition       | `source`, `source_id`                       |
| `judge_scores`          | judge's score per fighter per round         | `fight_id`, `fighter_id`, `round`, `judge`  |
| `media_scores`          | journalist's total per fighter              | `fight_id`, `fighter_id`, `writer`          |
| `odds`                  | fighter's betting line per source and book  | `fight_id`, `fighter_id`, `source`, `book`  |

### Round stats

`round_stats.source` says who counted. Different crews count differently, so filter on it when
the numbers have to be comparable:

- `ufcstats`: the full set of columns.
- `pfl`: PFL events from 2025-12 on. The same columns, with no per-round reversals.
- `oktagon`: only what landed (strikes, significant strikes by target, takedowns) and submission
  attempts. Every other column is NULL.
- `sofascore`: a third party's count, for promotions with no official stats (ONE, RIZIN, KSW,
  ACA, BRAVE, Cage Warriors, Fight Nation, Jungle Fight) and for Oktagon. Significant strikes
  landed and by target, total strikes thrown (not landed), knockdowns, takedowns, submission
  attempts and control time. Its strikes by position and time in each position are in
  `sofascore_round_stats`.

A fight with UFC Stats' or PFL's stats has only those. A fight with Oktagon's can also have
Sofascore's, as a second set of rows, so **always filter on `source`** when summing them. PFL's
earlier events (2018 to 2025-08) have a different set of stats (strikes by arm / leg / ground,
ground and standing time), kept in `smartcage_round_stats`.

Stats that weren't recorded are `NULL`, not `0`.

### Judges' scores and odds

`judge_scores.source` is `mmadecisions` (its copies of the official cards: decisions only, many
promotions, from 1997) or `ufc.com` (official UFC scorecards, 2020-08 to 2024-11). A fight's rows
come from one source, MMA Decisions' where it has that fight's cards.

`media_scores` are journalists' scores of decisions, from MMA Decisions: each writer's total for
each fighter. They're opinions, not results.

Odds are American and cover 2007 onward: the opening line and the lowest/highest closing line
across sportsbooks.

### Fighters and corners

`fighters.sex` is `M` or `F` where a source says so (UFC Stats' divisions, Oktagon's bouts), and
NULL otherwise.

`fight_participants.corner` is red (0) / blue (1) only from 2010-03-21 on. Before that UFC Stats
usually lists the winner first, so read nothing into corner on older fights. In fights UFC Stats
doesn't have, corner means nothing.

A fighter is linked to a source's id only through a fight both list (within a day, the other
fighter already linked), never by name alone. When two rows turn out to be one fighter, event or
fight, the older ID is kept and `merged_ids` records the other.

### Which fights are here

Fights get in two ways. Every event in Sherdog's event sitemaps has its card read whole, and
those events are in `event_cards` (nearly all of them). Other fights come from the pages of
fighters who fought on a promotion's own cards, or from the fighters UFC Stats tracks on other
promotions' cards, so they are there because of who fought in them: filter on `event_cards`
when that matters.

Only pro MMA fights are kept. Event pages don't say whether a bout was pro, so card fights are
checked on the fighters' Sherdog pages and amateur and exhibition bouts no promotion's site has
are removed. `fights.bout_type` is what those pages list it as, and NULL until checked.

Events no promotion's site has are labelled by their Sherdog organization. To filter by
promotion, join through `events`:

```sql
SELECT f.* FROM fights f JOIN events e USING (event_id) WHERE e.promotion = 'PRIDE';
```

## How it updates

A GitHub Action runs daily and reads, in order:

- **UFC Stats:** new events, and the last 21 days again (it often posts stats and corrections late).
- **Sherdog:** the pages of fighters from the last 21 days' cards, and new and recent event cards.
- **PFL, Oktagon, MMA Decisions and BestFightOdds:** what's new or recent.

If something changed, and no table fell by more than an update should, it publishes the database
as a new release and commits `data/summary.json`. A second, weekly Action finds events UFC Stats
leaves off its events list (Dana White's Contender Series, PRIDE and other promotions), which
can only be reached through fighter pages.

Sofascore refuses GitHub's runners, so its update runs separately from a home machine, on top of
the latest release, with `scripts/sofascore-update.sh`.

## The website

`site/` is a static page (fightcanon.com): fighters, events, round-by-round fight stats,
leaderboards and a Query page. Its pages read JSON files written by `scripts/site_json.py`; the
Query page and filtered leaderboards query the database in the browser with
[DuckDB-WASM](https://duckdb.org/docs/api/wasm/overview), from Parquet files written by
`scripts/export_parquet.py`, reading only the columns a query needs.
`.github/workflows/deploy.yml` publishes it after every database update. To look at it locally:

```bash
python scripts/site_json.py build data/canon.db site/db
python scripts/export_parquet.py data/canon.db site/db
python scripts/serve.py      # then open http://localhost:8000/
```

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m canon_db update          # update the database
.venv/bin/python -m pytest -q tests/         # run tests
node --test tests/site/modules.test.mjs      # the site's tests
```
