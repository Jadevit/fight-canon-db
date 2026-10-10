-- Schema of data/canon.db.
--
-- Fighters, events and fights from UFC Stats keep their UFC Stats IDs (the 16-character hex
-- slug in each page URL). Anything UFC Stats doesn't have comes from Sherdog with an id like
-- `sherdog:<number>` (fights: `sherdog:<event>:<fighter>-<fighter>`) and `source = 'sherdog'`.
-- Stats that were not recorded are NULL, never 0.

PRAGMA foreign_keys = ON;

CREATE TABLE fighters (
    fighter_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    nickname     TEXT,
    dob          TEXT,                      -- YYYY-MM-DD
    height_in    INTEGER,
    reach_in     INTEGER,
    weight_lbs   INTEGER,
    stance       TEXT,
    wins         INTEGER,                   -- pro MMA record, as shown on UFC Stats
    losses       INTEGER,
    draws        INTEGER,
    no_contests  INTEGER,
    nationality  TEXT,                      -- from Sherdog (sherdog: fighters only)
    source       TEXT NOT NULL DEFAULT 'ufcstats'
);

-- Every spelling of a fighter's name seen in a source, and their id there. A fighter's
-- Sherdog id is linked only through a fight both sources list (same date, one fighter
-- already linked); names just have to agree.
CREATE TABLE fighter_aliases (
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL,
    source_id    TEXT,                      -- the fighter's id in that source
    name         TEXT NOT NULL,
    PRIMARY KEY (source, source_id, name)
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
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    name         TEXT,                      -- the organization's name there
    PRIMARY KEY (source, source_id)
);

-- Fighters merged into another id: a fighter first seen on Sherdog (`sherdog:<id>`) who
-- later turns up on UFC Stats moves to their UFC Stats id.
CREATE TABLE fighter_redirects (
    old_id       TEXT PRIMARY KEY,
    new_id       TEXT NOT NULL REFERENCES fighters(fighter_id)
);

CREATE TABLE events (
    event_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    date         TEXT,                      -- YYYY-MM-DD
    location     TEXT,
    promotion    TEXT NOT NULL DEFAULT 'UFC' REFERENCES promotions(promotion),
    source       TEXT NOT NULL DEFAULT 'ufcstats'
);

-- An event's id in another source (e.g. its Sherdog event number).
CREATE TABLE event_aliases (
    event_id     TEXT NOT NULL REFERENCES events(event_id),
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

-- Events whose whole card was read from a source's card listing. Their fights are in the
-- database because they were on the card, not because of anyone's later career; Sherdog
-- fights on other events were found through a fighter's page.
CREATE TABLE event_cards (
    event_id     TEXT PRIMARY KEY REFERENCES events(event_id),
    source       TEXT NOT NULL,             -- whose card: 'sherdog'
    via          TEXT NOT NULL,             -- 'event_page', or 'kaggle:<dataset> v<N>'
    complete     INTEGER NOT NULL,          -- 1: every bout the source lists is in the database
    fetched_at   TEXT                       -- YYYY-MM-DD the card was read
);

CREATE TABLE fights (
    fight_id         TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL REFERENCES events(event_id),
    weight_class     TEXT,                  -- bout title as listed
    title_fight      INTEGER NOT NULL DEFAULT 0,
    scheduled_rounds INTEGER,
    method           TEXT,
    end_round        INTEGER,
    end_time         TEXT,                  -- m:ss
    time_format      TEXT,
    referee          TEXT,
    details          TEXT,                  -- finish detail or judges' scores
    overturned       INTEGER NOT NULL DEFAULT 0,
    no_contest       INTEGER NOT NULL DEFAULT 0,
    bout_type        TEXT,                  -- Sherdog fights: 'pro', 'exhibition' or 'amateur',
                                            -- as a fighter's Sherdog page lists it; NULL: unchecked
    source           TEXT NOT NULL DEFAULT 'ufcstats'
);

-- Two rows per fight.
CREATE TABLE fight_participants (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    corner       INTEGER NOT NULL,          -- 0 or 1, in the order UFC Stats lists them:
                                            -- 0 = red from 2010-03-21 on; before that the
                                            -- winner is usually listed first, so corner
                                            -- isn't red/blue and gives away the result.
                                            -- In Sherdog fights it's by fighter id: no meaning
    result       TEXT,                      -- W, L, D, NC
    PRIMARY KEY (fight_id, corner)
);

-- One row per fighter per round. *_land = landed, *_att = attempted. `source` says who counted:
-- 'ufcstats', or 'pfl' (PFL's own stats from 2025-12, same columns; it has no per-round
-- reversals). Different crews score fights, so the same column isn't guaranteed the same
-- judgment across sources.
CREATE TABLE round_stats (
    fight_id       TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id     TEXT NOT NULL REFERENCES fighters(fighter_id),
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
    source         TEXT NOT NULL DEFAULT 'ufcstats',
    PRIMARY KEY (fight_id, fighter_id, round)
);

-- PFL's older SmartCage stats (2018 to 2025-11), one row per fighter per round. A different
-- set from round_stats: strikes are split by arm / leg / ground (all strikes, not only
-- significant ones), and there is ground and standing time instead of control time.
CREATE TABLE smartcage_round_stats (
    fight_id           TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id         TEXT NOT NULL REFERENCES fighters(fighter_id),
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
    source             TEXT NOT NULL DEFAULT 'pfl',
    PRIMARY KEY (fight_id, fighter_id, round)
);

-- Per-judge, per-round scores from official UFC scorecards (2020-08 to 2024-11 so far).
-- Rounds after a finish aren't scored, so a finish has rows only for completed rounds.
CREATE TABLE judge_scores (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    round        INTEGER NOT NULL,
    judge        TEXT NOT NULL,
    score        INTEGER,                   -- after point deductions, as on the card
    deduction    INTEGER NOT NULL DEFAULT 0,  -- points taken off this fighter that round
    source       TEXT NOT NULL,
    PRIMARY KEY (fight_id, fighter_id, round, judge)
);

-- Betting lines, American odds. One row per fighter per fight per source per book.
-- `source` is where the data came from; `book` is who set the line, or 'all' for a
-- summary across books. Both fighters of a fight always get a row.
CREATE TABLE odds (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL,
    book         TEXT NOT NULL,
    open         INTEGER,
    close        INTEGER,                   -- one book's closing line
    close_low    INTEGER,                   -- lowest close across books ('all' rows)
    close_high   INTEGER,                   -- highest close across books ('all' rows)
    PRIMARY KEY (fight_id, fighter_id, source, book)
);

CREATE INDEX idx_fights_event          ON fights(event_id);
CREATE INDEX idx_participants_fighter  ON fight_participants(fighter_id);
CREATE INDEX idx_round_stats_fighter   ON round_stats(fighter_id);
CREATE INDEX idx_round_stats_fight     ON round_stats(fight_id);
