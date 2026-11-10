-- Schema of data/canon.db.
--
-- Fighters, events and fights have Fight Canon ids: numbers given once, when a row is first
-- seen, never changed or reused. Every source (UFC Stats, Sherdog, PFL, Oktagon, Sofascore,
-- BestFightOdds, ...) is only aliases, its ids for our rows in fighter_aliases, event_aliases
-- and fight_aliases, plus the data it adds. Which source's value a table shows is by rank:
-- source_tiers, per source and kind of data. Stats are kept by who counted them (`source`).
-- Stats that were not recorded are NULL, never 0.

PRAGMA foreign_keys = ON;

-- Every source of data, from canon_db/sources.csv (rebuilt on every update). A source is an
-- organization, not a site: one can have several sites, each with its own ids. 'correction'
-- (values set by hand, with a citation) and 'legacy' (values no saved page holds) are sources too.
CREATE TABLE sources (
    source       TEXT PRIMARY KEY,          -- the short key every `source` column uses
    name         TEXT NOT NULL,
    url          TEXT,
    sites        TEXT,                      -- its sites, space-separated, the default first; the
                                            -- `site` of its aliases
    full_results INTEGER NOT NULL CHECK (full_results IN (0, 1)),  -- 1: lists whole results, so it
                                            -- makes rows for fights no one else has
    attribution  TEXT                       -- what reuse of its data must credit, if anything
);

-- How a source ranks, per kind of data, from canon_db/source_tiers.csv. Tiers, best first:
-- correction, official (athletic commissions), primary (a promotion's own site), secondary
-- (aggregators of those), legacy. third_party is kept beside the others and never replaces them.
-- `priority` orders sources within a tier (1 first) where the kind has no rule of its own.
-- Kinds: fighter, event, fight, result, round_stats, judge_scores, media_scores, odds, rankings.
CREATE TABLE source_tiers (
    source       TEXT NOT NULL REFERENCES sources(source),
    kind         TEXT NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('correction', 'official', 'primary', 'secondary',
                                               'third_party', 'legacy')),
    priority     INTEGER NOT NULL,
    PRIMARY KEY (source, kind)
);

CREATE TABLE fighters (
    fighter_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    nickname     TEXT,
    dob          TEXT CHECK (dob = date(dob)),  -- YYYY-MM-DD
    height_in    INTEGER,
    reach_in     INTEGER,
    weight_lbs   INTEGER,
    stance       TEXT,
    nationality  TEXT,
    wins         INTEGER NOT NULL DEFAULT 0,  -- pro record, counted from this database's fights
    losses       INTEGER NOT NULL DEFAULT 0,  -- (fight_participants), not any source's printed one:
    draws        INTEGER NOT NULL DEFAULT 0,  -- that's in fighter_claims, to check against
    no_contests  INTEGER NOT NULL DEFAULT 0,
    sex          TEXT CHECK (sex IN ('M', 'F'))  -- where a source says so (UFC Stats' divisions,
                                            -- Oktagon's bouts); NULL: not known
);

-- A fighter's id in a source, with every spelling of their name seen there. A fighter is
-- linked to a source's id only through a fight both list (date within a day, a fighter
-- already linked); names just have to agree. A fighter can have several ids in one source
-- (Sherdog's duplicate profiles). `site` is which of the source's sites the id is from.
CREATE TABLE fighter_aliases (
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,             -- the fighter's id on that site
    name         TEXT NOT NULL,             -- the name as printed there
    PRIMARY KEY (source, site, source_id, name)
);

-- One row per league, from canon_db/promotions.csv (unknown labels are added as partial).
CREATE TABLE promotions (
    promotion    TEXT PRIMARY KEY,          -- the label in events.promotion
    parent       TEXT REFERENCES promotions(promotion),  -- for a sub-series, its promotion
    coverage     TEXT NOT NULL              -- full: UFC Stats has whole cards; partial: only
                                            -- the fights of fighters it tracks
);

-- A promotion's id in another source (e.g. its Sherdog organization number). Several
-- organizations can share one label when they are one league (e.g. PRIDE and its series).
CREATE TABLE promotion_aliases (
    promotion    TEXT NOT NULL REFERENCES promotions(promotion),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT,                      -- the organization's name there
    PRIMARY KEY (source, site, source_id)
);

-- Every id that no longer names a row, and why: `new_id` is the row it became (a merge keeps
-- the older id), or NULL if it's gone (deleted, or not pro MMA). Ids are never reused. Kept
-- from 2026-11 on; older gaps can only be explained from old releases.
CREATE TABLE retired_ids (
    kind         TEXT NOT NULL CHECK (kind IN ('fighter', 'event', 'fight', 'promotion', 'official')),
    old_id       INTEGER NOT NULL,
    new_id       INTEGER,
    reason       TEXT NOT NULL,             -- 'merged', 'deleted', ...
    retired_on   TEXT CHECK (retired_on = date(retired_on)),  -- YYYY-MM-DD; NULL: before this was kept
    PRIMARY KEY (kind, old_id)
);

CREATE TABLE events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    date         TEXT CHECK (date = date(date)),  -- YYYY-MM-DD
    location     TEXT,
    promotion    TEXT NOT NULL REFERENCES promotions(promotion)
);

-- An event's id in a source (its UFC Stats id, Sherdog event number, PFL page, ...). An
-- event can have several in one source (Sherdog lists some tournaments as two events).
CREATE TABLE event_aliases (
    event_id     INTEGER NOT NULL REFERENCES events(event_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT,                      -- the event's name as printed there
    PRIMARY KEY (source, site, source_id)
);

-- Events whose whole card was read from a source's card listing. Their fights are in the
-- database because they were on the card, not because of anyone's later career; other
-- events hold the fights reached through fighters' pages (Sherdog) or a source that tracks
-- only some fighters (UFC Stats' other promotions).
CREATE TABLE event_cards (
    event_id     INTEGER PRIMARY KEY REFERENCES events(event_id),
    source       TEXT NOT NULL REFERENCES sources(source),  -- whose card: 'sherdog'
    via          TEXT NOT NULL,             -- 'event_page', or 'kaggle:<dataset> v<N>'
    complete     INTEGER NOT NULL CHECK (complete IN (0, 1)),  -- 1: every bout the source lists
                                            -- was written (amateur and exhibition bouts are removed after)
    fetched_at   TEXT CHECK (fetched_at = date(fetched_at))  -- YYYY-MM-DD the card was read
);

-- Bouts a source lists that were deleted as amateur or exhibition, so reading their card
-- again doesn't bring them back.
CREATE TABLE non_pro_bouts (
    source       TEXT NOT NULL REFERENCES sources(source),  -- 'sherdog'
    source_id    TEXT NOT NULL,             -- the bout's id there (as in fight_aliases)
    bout_type    TEXT NOT NULL CHECK (bout_type IN ('amateur', 'exhibition')),
    PRIMARY KEY (source, source_id)
);

-- Event pages a source's card reader has read, whatever came of them: an event that left
-- nothing in the database (not held yet, or only amateur and exhibition bouts) is still here,
-- so it isn't read again.
CREATE TABLE event_pages (
    source       TEXT NOT NULL REFERENCES sources(source),  -- 'sherdog'
    source_id    TEXT NOT NULL,             -- the source's event id
    date         TEXT CHECK (date = date(date)),  -- YYYY-MM-DD, as the page gives it
    fetched_at   TEXT NOT NULL CHECK (fetched_at = date(fetched_at)),  -- YYYY-MM-DD the page was fetched
    PRIMARY KEY (source, source_id)
);

CREATE TABLE fights (
    fight_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         INTEGER NOT NULL REFERENCES events(event_id),
    weight_class     TEXT,                  -- bout title as listed
    title_fight      INTEGER NOT NULL DEFAULT 0 CHECK (title_fight IN (0, 1)),
    scheduled_rounds INTEGER,
    method           TEXT,                  -- as the source writes it: UFC Stats 'KO/TKO' (the
                                            -- detail in `details`), Sherdog 'TKO (Punches)'
    end_round        INTEGER,
    end_time         TEXT,                  -- m:ss
    time_format      TEXT,
    referee          TEXT,
    details          TEXT,                  -- finish detail or judges' scores
    overturned       INTEGER NOT NULL DEFAULT 0 CHECK (overturned IN (0, 1)),
    no_contest       INTEGER NOT NULL DEFAULT 0 CHECK (no_contest IN (0, 1)),
    bout_type        TEXT                   -- what the fighters' Sherdog pages list it as: 'pro',
                                            -- 'exhibition' or 'amateur' (non-pro fights no
                                            -- promotion's site has are deleted); NULL: not checked
);

-- A fight's id in a source: UFC Stats' fight id; Sherdog has none, so its alias is
-- '<event>:<fighter>-<fighter>' (Sherdog ids, lower first).
CREATE TABLE fight_aliases (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT,                      -- the bout's name as printed there, if it has one
    PRIMARY KEY (source, site, source_id)
);

-- Two rows per fight.
CREATE TABLE fight_participants (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    corner       INTEGER NOT NULL,          -- 0 or 1, in the order UFC Stats lists them:
                                            -- 0 = red from 2010-03-21 on; before that the
                                            -- winner is usually listed first, so corner
                                            -- isn't red/blue and gives away the result.
                                            -- In fights UFC Stats doesn't have it's by fighter
                                            -- id: no meaning
    result       TEXT CHECK (result IN ('W', 'L', 'D', 'NC')),
    PRIMARY KEY (fight_id, corner)
);

-- Claims: what each source's page says about a fighter, event or fight, one row per page (its
-- alias: source, site, source_id), keyed to our row. `fighters`, `events`, `fights` and
-- `fight_participants` are worked out from them at the end of every update (canon_db/claims.py):
-- per column, the best-ranked claim with a value (source_tiers), so a blank falls through and
-- nothing depends on the order sources ran in. Values are as the page gives them, parsed but not
-- cleaned: cleaning rules run when the tables are worked out, so a rule can change without
-- reading pages again. Not in the Parquet.
CREATE TABLE fighter_claims (
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,             -- the fighter's id on that site (as in fighter_aliases)
    name         TEXT,
    nickname     TEXT,
    dob          TEXT,
    height_in    INTEGER,
    reach_in     INTEGER,
    weight_lbs   INTEGER,                   -- the listed weight, not what they fought at
    stance       TEXT,
    nationality  TEXT,
    sex          TEXT,                      -- only where the source says it
    association  TEXT,                      -- team or gym
    wins         INTEGER,                   -- the pro record the page prints (a fighter's record is
    losses       INTEGER,                   -- counted from fight_participants; this checks it)
    draws        INTEGER,
    no_contests  INTEGER,
    PRIMARY KEY (source, site, source_id)
);

CREATE TABLE event_claims (
    event_id     INTEGER NOT NULL REFERENCES events(event_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,             -- as in event_aliases
    name         TEXT,
    date         TEXT,
    location     TEXT,
    promotion    TEXT,                      -- the promotion the source puts it under, if it says
    PRIMARY KEY (source, site, source_id)
);

-- A source's bout. `event_id` is the event it puts the bout on.
CREATE TABLE fight_claims (
    fight_id         INTEGER NOT NULL REFERENCES fights(fight_id),
    source           TEXT NOT NULL REFERENCES sources(source),
    site             TEXT NOT NULL,
    source_id        TEXT NOT NULL,         -- as in fight_aliases
    event_id         INTEGER REFERENCES events(event_id),
    weight_class     TEXT,
    title_fight      INTEGER,               -- NULL: the page doesn't say
    scheduled_rounds INTEGER,
    method           TEXT,
    end_round        INTEGER,
    end_time         TEXT,
    time_format      TEXT,
    referee          TEXT,
    details          TEXT,
    overturned       INTEGER,
    no_contest       INTEGER,
    bout_type        TEXT,                  -- 'pro', 'amateur' or 'exhibition', where the source says
    PRIMARY KEY (source, site, source_id)
);

-- A source's two sides of a bout (`side` 0 and 1, in the source's order), with the fighter as
-- printed there; `fighter_id` is NULL if the side isn't linked to a fighter of ours.
CREATE TABLE participant_claims (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    site         TEXT NOT NULL,
    source_id    TEXT NOT NULL,             -- the bout's id, as in fight_claims
    side         INTEGER NOT NULL CHECK (side IN (0, 1)),
    fighter_id   INTEGER REFERENCES fighters(fighter_id),
    fighter_source_id TEXT,                 -- the fighter's id on that site, if it gives one
    name         TEXT,                      -- the fighter's name as printed
    result       TEXT,
    corner       INTEGER,                   -- the side's corner, where the source's order means one
                                            -- (UFC Stats: 0 red, 1 blue); NULL otherwise
    PRIMARY KEY (source, site, source_id, side)
);

-- One row per fighter per round. *_land = landed, *_att = attempted. `source` says who counted:
-- 'ufc' (UFC Stats); 'pfl' (PFL's own stats from 2025-12, same columns; it has no per-round
-- reversals); 'oktagon' (Oktagon's own stats: only strikes landed, by significance and
-- target, takedowns landed and submission attempts; every other column is NULL); or
-- 'sofascore' (a third party's count, for promotions with no official stats: significant
-- strikes landed and by target, total strikes thrown but not landed, knockdowns, takedowns,
-- submission attempts and control; the rest is NULL, the split by position included).
-- Different crews score fights, so the same column isn't guaranteed the same judgment
-- across sources. A fight with UFC Stats' or PFL's has only those; one of Oktagon's can
-- also have Sofascore's (two sets of rows, told apart by source: filter on it).
CREATE TABLE round_stats (
    fight_id       INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id     INTEGER NOT NULL REFERENCES fighters(fighter_id),
    round          INTEGER NOT NULL,
    knockdowns     INTEGER,
    sig_str_land   INTEGER,
    sig_str_att    INTEGER,
    total_str_land INTEGER,
    total_str_att  INTEGER,
    td_land        INTEGER,
    td_att         INTEGER,
    sub_att        INTEGER,
    reversals      INTEGER,
    ctrl_sec       INTEGER,                 -- control time, seconds
    head_land      INTEGER,
    head_att       INTEGER,
    body_land      INTEGER,
    body_att       INTEGER,
    leg_land       INTEGER,
    leg_att        INTEGER,
    dist_land      INTEGER,
    dist_att       INTEGER,
    clinch_land    INTEGER,
    clinch_att     INTEGER,
    ground_land    INTEGER,
    ground_att     INTEGER,
    source         TEXT NOT NULL REFERENCES sources(source),
    PRIMARY KEY (fight_id, fighter_id, round, source)
);

-- PFL's older SmartCage stats (2018 to 2025-11), one row per fighter per round. A different
-- set from round_stats: strikes are split by arm / leg / ground (all strikes, not only
-- significant ones), and there is ground and standing time instead of control time.
CREATE TABLE smartcage_round_stats (
    fight_id           INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id         INTEGER NOT NULL REFERENCES fighters(fighter_id),
    round              INTEGER NOT NULL,
    knockdowns         INTEGER,
    total_str_land     INTEGER,
    total_str_att      INTEGER,
    arm_land           INTEGER,
    arm_att            INTEGER,
    leg_land           INTEGER,
    leg_att            INTEGER,
    ground_land        INTEGER,
    ground_att         INTEGER,
    power_land         INTEGER,             -- NULL on events that recorded none
    td_land            INTEGER,
    td_att             INTEGER,
    sub_att            INTEGER,
    dominant_positions INTEGER,             -- NULL on events that recorded none
    ground_sec         INTEGER,             -- time the fight spent on the ground
    standing_sec       INTEGER,
    source             TEXT NOT NULL REFERENCES sources(source),
    PRIMARY KEY (fight_id, fighter_id, round)
);

-- What Sofascore counts beyond round_stats' columns, for the fights whose round_stats are
-- Sofascore's: significant strikes landed by position (the parts overlap and don't add up
-- to sig_str_land, unlike round_stats' dist/clinch/ground), time spent in each position
-- (the same for both fighters), and control split into clinch and ground
-- (round_stats.ctrl_sec is their sum). Time in a position seems to run on the real clock,
-- stoppages included: in about half of 5-minute rounds the three add up to more than 300.
CREATE TABLE sofascore_round_stats (
    fight_id          INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id        INTEGER NOT NULL REFERENCES fighters(fighter_id),
    round             INTEGER NOT NULL,
    sig_distance_land INTEGER,
    sig_clinch_land   INTEGER,
    sig_ground_land   INTEGER,
    distance_sec      INTEGER,
    clinch_sec        INTEGER,
    ground_sec        INTEGER,
    clinch_ctrl_sec   INTEGER,
    ground_ctrl_sec   INTEGER,
    transitions       INTEGER,              -- position changes the fighter made
    source            TEXT NOT NULL REFERENCES sources(source),
    PRIMARY KEY (fight_id, fighter_id, round)
);

-- Per-judge, per-round scores. `source` is 'ufc' (the official UFC scorecards on ufc.com, 2020-08 to
-- 2024-11) or 'mmadecisions' (MMA Decisions' copies of the official cards, decisions only,
-- many promotions); a fight's rows come from one source, MMA Decisions' where it has the
-- fight's cards. Rounds after a
-- finish aren't scored, so a finish has rows only for completed rounds.
CREATE TABLE judge_scores (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    round        INTEGER NOT NULL,
    judge        TEXT NOT NULL,
    score        INTEGER,                   -- after point deductions, as on the card
    deduction    INTEGER NOT NULL DEFAULT 0,  -- points taken off this fighter that round
    source       TEXT NOT NULL REFERENCES sources(source),
    PRIMARY KEY (fight_id, fighter_id, round, judge)
);

-- Journalists' scores of decisions (MMA Decisions' media scores): one row per writer per
-- fighter, with the writer's total for that fighter (rounds aren't given). Opinions, not
-- results: they show how a decision was seen. The writer's pick is the higher score.
CREATE TABLE media_scores (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    writer       TEXT NOT NULL,
    outlet       TEXT,                      -- where the writer published it
    score        INTEGER NOT NULL,          -- the writer's total for this fighter
    url          TEXT,                      -- the published score, when given
    source       TEXT NOT NULL REFERENCES sources(source),
    PRIMARY KEY (fight_id, fighter_id, writer)
);

-- Betting lines, American odds. One row per fighter per fight per source per book.
-- `source` is where the data came from; `book` is who set the line, or 'all' for a
-- summary across books. Both fighters of a fight always get a row.
CREATE TABLE odds (
    fight_id     INTEGER NOT NULL REFERENCES fights(fight_id),
    fighter_id   INTEGER NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL REFERENCES sources(source),
    book         TEXT NOT NULL,
    open         INTEGER,
    close        INTEGER,                   -- one book's closing line
    close_low    INTEGER,                   -- lowest close across books ('all' rows)
    close_high   INTEGER,                   -- highest close across books ('all' rows)
    PRIMARY KEY (fight_id, fighter_id, source, book)
);

-- Values a check found unlikely, for review by hand: rebuilt on every update from the checks.
-- A flagged value is blanked in its table and kept here as printed, so nothing is lost; a
-- correction can restore it ("this flagged value is right"), which clears the flag. One table for
-- every check (range checks, conflicts between sources, links that look wrong). `fighter_id`,
-- `event_id` and `fight_id` say what the row is about, to find flags by fighter or fight.
CREATE TABLE review_flags (
    flag_id      INTEGER PRIMARY KEY,
    check_name   TEXT NOT NULL,             -- which check
    table_name   TEXT NOT NULL,             -- the table of the row
    row_key      TEXT NOT NULL,             -- the row's key, as JSON ({"fight_id": 1, "round": 2, ...})
    column_name  TEXT,                      -- NULL: the row as a whole
    value        TEXT,                      -- as the source printed it
    reason       TEXT NOT NULL,             -- why it's unlikely, in words
    fighter_id   INTEGER REFERENCES fighters(fighter_id),
    event_id     INTEGER REFERENCES events(event_id),
    fight_id     INTEGER REFERENCES fights(fight_id)
);
CREATE UNIQUE INDEX idx_review_flags ON review_flags(check_name, table_name, row_key, ifnull(column_name, ''));

CREATE INDEX idx_fights_event          ON fights(event_id);
CREATE INDEX idx_participants_fighter  ON fight_participants(fighter_id);
CREATE INDEX idx_round_stats_fighter   ON round_stats(fighter_id);
CREATE INDEX idx_round_stats_fight     ON round_stats(fight_id);
CREATE INDEX idx_fighter_aliases       ON fighter_aliases(fighter_id);
CREATE INDEX idx_event_aliases         ON event_aliases(event_id);
CREATE INDEX idx_fight_aliases         ON fight_aliases(fight_id);
CREATE INDEX idx_fighter_claims        ON fighter_claims(fighter_id);
CREATE INDEX idx_event_claims          ON event_claims(event_id);
CREATE INDEX idx_fight_claims          ON fight_claims(fight_id);
CREATE INDEX idx_fight_claims_event    ON fight_claims(event_id);
CREATE INDEX idx_participant_claims    ON participant_claims(fight_id);
CREATE INDEX idx_participant_claims_fighter ON participant_claims(fighter_id);
