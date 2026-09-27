-- Schema of data/canon.db.
--
-- Every table is keyed by UFC Stats IDs (the 16-character hex slug in each page URL).
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
    nationality  TEXT,                      -- reserved
    source       TEXT NOT NULL DEFAULT 'ufcstats'
);

-- Every spelling of a fighter's name seen in a source.
CREATE TABLE fighter_aliases (
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    source       TEXT NOT NULL,
    source_id    TEXT,                      -- the fighter's id in that source
    name         TEXT NOT NULL,
    PRIMARY KEY (source, source_id, name)
);

CREATE TABLE events (
    event_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    date         TEXT,                      -- YYYY-MM-DD
    location     TEXT,
    promotion    TEXT NOT NULL DEFAULT 'UFC',
    source       TEXT NOT NULL DEFAULT 'ufcstats'
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
    source           TEXT NOT NULL DEFAULT 'ufcstats'
);

-- Two rows per fight.
CREATE TABLE fight_participants (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    corner       INTEGER NOT NULL,          -- 0 or 1, in the order UFC Stats lists them
    result       TEXT,                      -- W, L, D, NC
    PRIMARY KEY (fight_id, corner)
);

-- One row per fighter per round. *_land = landed, *_att = attempted.
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

-- Reserved: per-judge, per-round scores.
CREATE TABLE judge_scores (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    round        INTEGER NOT NULL,
    judge        TEXT NOT NULL,
    score        INTEGER,
    source       TEXT NOT NULL,
    PRIMARY KEY (fight_id, fighter_id, round, judge)
);

-- Reserved: closing odds (American).
CREATE TABLE odds (
    fight_id     TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id   TEXT NOT NULL REFERENCES fighters(fighter_id),
    odds         INTEGER,
    source       TEXT NOT NULL,
    PRIMARY KEY (fight_id, fighter_id, source)
);

CREATE INDEX idx_fights_event          ON fights(event_id);
CREATE INDEX idx_participants_fighter  ON fight_participants(fighter_id);
CREATE INDEX idx_round_stats_fighter   ON round_stats(fighter_id);
CREATE INDEX idx_round_stats_fight     ON round_stats(fight_id);
