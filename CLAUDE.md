# CLAUDE.md

This repo has one job: scrape sources, parse them, and keep `data/canon.db`
up to date for other repos to use. Keep this file current when the repo changes.

## Layout

```
canon_db/
  __main__.py          CLI: python -m canon_db update [source] [options] | migrate
  schema.sql           the database schema
  db.py                runs updates on a temp copy; publishes only if content changed;
                       migrate() rebuilds the DB from schema.sql keeping every row
  sources/
    __init__.py        registry of sources
    ufcstats/
      client.py        HTTP client (1 req/sec, retries, solves the site's proof-of-work check)
      pages.py         parses events list, event, fight and fighter pages
      fields.py        parses cells ("13 of 27", "1:30", "---" → NULL)
      load.py          decides what to fetch, writes rows to the database
data/
  canon.db             the database (committed)
  summary.json         coverage + row counts (committed)
  raw/                 fetched pages (ignored)
site/                  static website (sql.js): index.html, app.js, style.css
tests/                 same layout as canon_db/; saved pages in tests/ufcstats/fixtures/
.github/workflows/update-db.yml   daily update + commit
.github/workflows/discover.yml    weekly --discover + commit (DWCS, PRIDE, other promotions)
.github/workflows/pages.yml       publishes site/ + gzipped DB to GitHub Pages
```

## Rules

- Everything links by UFC Stats ID (16-hex slug from the page URL). No name matching.
  Fighter IDs come from the two fighter links on each fight page.
- Unrecorded stats are NULL, never 0.
- `schema.sql` must match `data/canon.db`; `tests/test_db.py` enforces it. After editing
  `schema.sql`, run `python -m canon_db migrate`.
- `events.promotion` says which promotion a fight belongs to (fights join to events).
  Events on UFC Stats' events list are `UFC`; an event already in the DB keeps its label;
  others (found by `--discover`) get it from their name via `promotion()` in
  `sources/ufcstats/load.py`, with one-line fixes in `PROMOTION_OVERRIDES`.
- `fighters.wins/losses/draws/no_contests` are the fighter's pro MMA record as shown on
  their UFC Stats page, not a count of the fights in the DB.

## How an update works

1. Fetch the UFC Stats events list. Skip events dated after today.
2. Load events that aren't in the database yet, plus every event from the last 21
   days, plus any passed with `--events` (even ones missing from the list).
3. For each, fetch its event, fight and fighter pages. If a fight page fails, skip
   that event and leave its rows alone.
4. Replace each loaded event's rows (event, fights, participants, round stats) and
   upsert its fighters.
5. If the content changed, overwrite `data/canon.db` and `data/summary.json`.
   Otherwise leave the file untouched.

UFC Stats also hosts events that aren't on its events list (PRIDE, and other promotions'
fights for fighters it tracks). `--discover` finds them: it scans fighter pages for fights
not in the DB, follows each to its event, loads it, then scans the new fighters' pages,
until nothing new turns up. It runs weekly (`discover.yml`), since the daily job only sees
the events list, which leaves out DWCS and every non-UFC promotion. Other promotions'
events hold only the fights UFC Stats tracked, so some cards are partial.

## Website

`site/` has no build step. `app.js` loads `canon.db.gz` (deployed next to it) or
falls back to `../data/canon.db` for local preview (`python3 -m http.server` from the
repo root, open `/site/`). Views are routed by URL hash: `#/fighters`, `#/fighter/<id>`,
`#/events`, `#/event/<id>`, `#/fight/<id>`, `#/leaders`, `#/sql`. `pages.yml` runs on
changes to `site/` and after each "Update database" or "Discover events" run (a bot push
can't trigger it directly).

## Adding a source

Create `canon_db/sources/<name>/` exposing `NAME`, `add_arguments(parser)` and
`update(conn, args, raw_dir)`, then register it in `canon_db/sources/__init__.py`.

## Commands

```bash
.venv/bin/python -m canon_db update                           # all sources
.venv/bin/python -m canon_db update ufcstats --events <id>    # load specific events
.venv/bin/python -m canon_db update ufcstats --all            # reload everything (~4-5 h)
.venv/bin/python -m canon_db update ufcstats --offline        # re-parse data/raw/, no network
.venv/bin/python -m canon_db update ufcstats --discover       # load events missing from the list
.venv/bin/python -m canon_db migrate                          # after editing schema.sql
.venv/bin/python -m pytest -q tests/
```
