# canon-db

A SQLite database of MMA events, fights and round-by-round stats (UFC, PRIDE and more),
scraped from [UFC Stats](http://ufcstats.com), with fighters' full pro records from
[Sherdog](https://www.sherdog.com), PFL's own stats from [PFL](https://pflmma.com) and betting
lines from [BestFightOdds](https://www.bestfightodds.com), updated automatically.

- Browse it: **https://jadevit.github.io/fight-canon-db/** (works on phones)
- Database: [latest release](https://github.com/jadevit/fight-canon-db/releases/latest) (`canon.db`)
- Coverage and row counts: [`data/summary.json`](data/summary.json)
- Schema: [`canon_db/schema.sql`](canon_db/schema.sql)

## Use it from another repo

```bash
curl -L -o canon.db https://github.com/jadevit/fight-canon-db/releases/latest/download/canon.db
```

Every update is its own release (`db-YYYY-MM-DD-HHMM`), so you can pin a version:
`.../releases/download/<tag>/canon.db`. To work on this repo, put the latest one at
`data/canon.db` first: `gh release download --pattern canon.db --dir data`.

## Tables

Everything UFC Stats has keeps its UFC Stats ID. Other fights come from Sherdog
(`source = 'sherdog'`: results, weight class, no stats); fighters and events UFC Stats doesn't
know get IDs like `sherdog:12345`. Filter `fights.source = 'ufcstats'` for UFC Stats fights only.

| Table                | One row per                      | Keys                              |
| -------------------- | -------------------------------- | --------------------------------- |
| `events`             | event (with its `promotion`)     | `event_id`                        |
| `promotions`         | league (parent, card coverage)   | `promotion`                       |
| `promotion_aliases`  | league's id in another source    | `promotion`                       |
| `fights`             | fight                            | `fight_id` → `event_id`           |
| `fight_participants` | fighter in a fight (2 per fight) | `fight_id`, `fighter_id`          |
| `round_stats`        | fighter per round                | `fight_id`, `fighter_id`, `round` |
| `smartcage_round_stats` | fighter per round (older PFL) | `fight_id`, `fighter_id`, `round` |
| `fighters`           | fighter (bio + pro record)       | `fighter_id`                      |
| `fighter_aliases`    | fighter's name and id per source | `fighter_id`                      |
| `fighter_redirects`  | old id of a merged fighter       | `old_id` → `new_id`               |
| `event_aliases`      | event's id in another source     | `event_id`                        |
| `event_cards`        | event whose whole card was read  | `event_id`                        |
| `judge_scores`       | judge's score per round          | `fight_id`, `fighter_id`          |
| `odds`               | fighter's betting line per fight | `fight_id`, `fighter_id`          |

`round_stats.source` says who counted: `ufcstats`, or `pfl` for PFL events from 2025-12 on (same
columns, no per-round reversals). Different crews score fights, so filter on it when the
numbers have to be comparable. PFL's earlier events (2018 to 2025-11) have a different set of
stats (strikes by arm / leg / ground, ground and standing time), kept in `smartcage_round_stats`.
PFL stats attach to the Sherdog fight, so those fights have `fights.source = 'sherdog'`.

Stats that weren't recorded are `NULL`, not `0`. Odds are American and cover 2007 onward:
the opening line and the lowest/highest closing line across sportsbooks. Per-round judge
scores come from official UFC scorecards and cover UFC fights from 2020-08 to 2024-11.

`fight_participants.corner` is red (0) / blue (1) only from 2010-03-21 on. Before that
UFC Stats usually lists the winner first, so don't use corner as a feature for older fights.
In Sherdog fights corner means nothing. Sherdog events are labelled by their Sherdog
organization: the label we already use if its events overlap ours, else its full name.

Sherdog fights get in two ways. For some promotions (ACA, Bellator, Cage Warriors, Jungle Fight,
KSW, LFA, Oktagon, PFL, RIZIN) every card is read whole, and those events are in `event_cards`.
Other Sherdog fights come from the pages of fighters linked to UFC Stats, so they are there
because of who fought in them: filter on `event_cards` when that matters. Event pages don't say
whether a bout was pro, so `fights.bout_type` (`pro`, `exhibition`, `amateur`) comes from the
fighters' Sherdog pages; it is NULL for UFC Stats fights and for card fights not yet checked.

A fighter's Sherdog page is linked only through a fight both sites list (same date, the other
fighter already linked), never by name alone. If a `sherdog:` fighter later shows up on UFC
Stats, their rows move to the UFC Stats ID and `fighter_redirects` records the old one.
`sherdog:` fighters' bios (DOB, height, weight, nationality) come from Sherdog.

To filter by promotion, join through `events`:

```sql
SELECT f.* FROM fights f JOIN events e USING (event_id) WHERE e.promotion = 'PRIDE';
```

## How it updates

A GitHub Action runs daily. It fetches any new events from UFC Stats, reloads the
last 21 days (UFC Stats often posts stats and corrections late), and if something
changed publishes the database as a new release and commits `data/summary.json`. A second, weekly Action picks up events UFC Stats
leaves off its events list (Dana White's Contender Series, PRIDE and other promotions).
Sherdog pages of fighters from the last 21 days' cards are read each day too, and so are the
new and recent cards of the promotions above.

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
