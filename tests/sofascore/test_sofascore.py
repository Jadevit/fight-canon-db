import json
import sqlite3
from pathlib import Path

from canon_db import db
from canon_db.sources.sofascore.load import load
from canon_db.sources.sofascore.pages import bouts, rounds

FIXTURES = Path(__file__).parent / "fixtures"


def test_rounds_from_a_saved_bout():
    rows = {(r["side"], r["round"]): r for r in rounds(json.loads((FIXTURES / "stats.json").read_text(encoding="utf-8")))}
    assert sorted(rows) == [(0, 1), (0, 2), (1, 1), (1, 2)]  # a second-round finish
    r = rows[0, 2]
    assert (r["sig_str_land"], r["total_str_att"], r["head_land"], r["body_land"], r["leg_land"]) == (7, 23, 6, 0, 1)
    assert (r["td_land"], r["td_att"], r["knockdowns"]) == (1, 1, 0)
    assert (r["sig_distance_land"], r["sig_clinch_land"], r["sig_ground_land"]) == (2, 5, 5)
    # Control is clinch plus ground control; time in position is the fight's, the same for both.
    assert (r["clinch_ctrl_sec"], r["ground_ctrl_sec"], r["ctrl_sec"]) == (0, 104, 104)
    assert rows[1, 2]["ground_sec"] == r["ground_sec"] == 178
    assert "total_str_land" not in r and "dist_land" not in r  # not recorded: stays NULL


def _stats(sig, thrown):
    """A bout's stats: totals and one round, home's numbers first."""
    def period(name):
        items = [dict(name=n, homeValue=h, awayValue=a) for n, (h, a) in
                 (("Significant Strikes", sig), ("Strikes", thrown), ("Clinch Control Seconds", (10, 0)),
                  ("Ground Clinch Control Seconds", (5, 0)))]
        return dict(period=name, groups=[dict(groupName="Strikes", statisticsItems=items)])
    return dict(statistics=[period("ALL"), period("1ST")])


def test_all_zero_stats_are_not_recorded():
    assert rounds(_stats((0, 0), (0, 0))) != []  # control alone still counts
    zero = _stats((0, 0), (0, 0))
    for p in zero["statistics"]:
        for it in p["groups"][0]["statisticsItems"]:
            it["homeValue"] = it["awayValue"] = 0
    assert rounds(zero) == []
    # Totals with something in them don't help when no round has anything counted.
    zero["statistics"][0]["groups"][0]["statisticsItems"].append(dict(name="Distance Seconds", homeValue=40, awayValue=40))
    zero["statistics"][1]["groups"][0]["statisticsItems"].append(dict(name="Distance Seconds", homeValue=40, awayValue=40))
    assert rounds(zero) == []


def test_rounds_after_the_last_counted_one_are_dropped():
    s = _stats((5, 2), (9, 4))
    for name in ("2ND", "3RD"):
        empty = [dict(name=n, homeValue=0, awayValue=0) for n in
                 ("Significant Strikes", "Strikes", "Clinch Control Seconds", "Ground Clinch Control Seconds")]
        s["statistics"].append(dict(period=name, groups=[dict(groupName="Strikes", statisticsItems=empty)]))
    s["statistics"][2]["groups"][0]["statisticsItems"][0]["homeValue"] = 1  # round 2 has a strike, round 3 nothing
    assert sorted({r["round"] for r in rounds(s)}) == [1, 2]


def test_bouts_keep_finished_mma():
    def event(i, discipline, status="finished"):
        return dict(id=i, fightDiscipline=discipline, status=dict(type=status), startTimestamp=1577923200,
                    homeTeam=dict(id=1, name="Alan Able"), awayTeam=dict(id=2, name="Ben Baker"))
    season = dict(events=[event(1, "mma"), event(2, None), event(3, "muay-thai"), event(4, "mma", "canceled")])
    assert [b["id"] for b in bouts(season)] == [1, 2]
    assert bouts(season)[0]["date"] == "2020-01-02"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?,?)",
                     [(1, "Alan Able"), (2, "Ben Baker"),
                      (3, "Carl Cole"), (4, "Dan Dale")])
    conn.executemany("INSERT INTO fighter_aliases VALUES (?, 'sherdog', ?, ?)",
                     [(1, "100", "Alan Able"), (2, "200", "Ben Baker"),
                      (3, "300", "Carl Cole"), (4, "400", "Dan Dale")])
    conn.execute("INSERT INTO events (event_id, name, date) VALUES (9, 'Oktagon 1', '2020-01-02')")
    for fight, pair in ((1, (2, 1)), (2, (3, 4))):
        conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (?, 9)", (fight,))
        conn.executemany("INSERT INTO fight_participants VALUES (?, ?, ?, ?)",
                         [(fight, pair[0], 0, "W"), (fight, pair[1], 1, "L")])
    # Oktagon's own stats on the first fight; official stats on the second.
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, source) "
                 "VALUES (1, 1, 1, 4, 'oktagon')")
    conn.execute("INSERT INTO round_stats (fight_id, fighter_id, round, sig_str_land, source) "
                 "VALUES (2, 3, 1, 9, 'ufcstats')")
    return conn


def _raw(tmp_path):
    for folder in ("events", "stats"):
        (tmp_path / folder).mkdir()
    season = dict(promotion=20258, events=[
        dict(id=11, fightDiscipline=None, status=dict(type="finished"), startTimestamp=1577923200,
             homeTeam=dict(id=1, name="Alan Able"), awayTeam=dict(id=2, name="Ben Baker")),
        dict(id=12, fightDiscipline="mma", status=dict(type="finished"), startTimestamp=1577923200,
             homeTeam=dict(id=3, name="Carl Cole"), awayTeam=dict(id=4, name="Dan Dale"))])
    (tmp_path / "events" / "500.json").write_text(json.dumps(season), encoding="utf-8")
    for bout in (11, 12):
        (tmp_path / "stats" / f"{bout}.json").write_text(json.dumps(_stats((7, 3), (20, 15))), encoding="utf-8")


def test_load(tmp_path):
    conn = _db()
    _raw(tmp_path)
    counts = load(conn, tmp_path)
    assert counts["fights with stats"] == 1 and counts["bouts with official stats"] == 1
    assert counts["oktagon rows replaced"] == 1
    # Home is Alan Able whatever the corner order; Oktagon's row is gone, the official one stays.
    assert conn.execute("SELECT fight_id, fighter_id, sig_str_land, total_str_att, total_str_land, ctrl_sec, source "
                        "FROM round_stats ORDER BY fight_id, fighter_id").fetchall() == [
        (1, 1, 7, 20, None, 15, "sofascore"),
        (1, 2, 3, 15, None, 0, "sofascore"),
        (2, 3, 9, None, None, None, "ufcstats")]
    assert conn.execute("SELECT count(*) FROM sofascore_round_stats").fetchone() == (2,)
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source = 'sofascore' AND source_id = '500'").fetchone() == (
        9,)
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source = 'sofascore' AND source_id = '1'").fetchone() == (
        1,)
    load(conn, tmp_path)  # loading again replaces, doesn't duplicate
    assert conn.execute("SELECT count(*) FROM round_stats WHERE source = 'sofascore'").fetchone() == (2,)


def test_disagreeing_winner_and_differing_copies_get_no_stats(tmp_path):
    conn = _db()
    _raw(tmp_path)
    load(conn, tmp_path)
    season = json.loads((tmp_path / "events" / "500.json").read_text(encoding="utf-8"))
    season["events"][0]["winnerCode"] = 1  # Sofascore says Alan Able won; we have Ben Baker
    (tmp_path / "events" / "500.json").write_text(json.dumps(season), encoding="utf-8")
    counts = load(conn, tmp_path)
    assert counts["bouts whose winner disagrees"] == 1 and counts["fights with stats"] == 0
    assert conn.execute("SELECT count(*) FROM round_stats WHERE source = 'sofascore'").fetchone() == (0,)

    season["events"][0]["winnerCode"] = 2
    season["events"].append(dict(season["events"][0], id=13))  # the same bout listed again...
    (tmp_path / "events" / "500.json").write_text(json.dumps(season), encoding="utf-8")
    (tmp_path / "stats" / "13.json").write_text(json.dumps(_stats((8, 3), (20, 15))), encoding="utf-8")  # ...counted differently
    assert load(conn, tmp_path)["fights with differing copies"] == 1
    (tmp_path / "stats" / "13.json").write_text(json.dumps(_stats((7, 3), (20, 15))), encoding="utf-8")  # the same numbers
    assert load(conn, tmp_path)["fights with stats"] == 1


def test_a_bout_that_stops_matching_loses_its_stats(tmp_path):
    conn = _db()
    _raw(tmp_path)
    load(conn, tmp_path)
    assert conn.execute("SELECT count(*) FROM round_stats WHERE source = 'sofascore'").fetchone() == (2,)
    # The season now names someone else for the first bout: no match, so its stats go.
    season = json.loads((tmp_path / "events" / "500.json").read_text(encoding="utf-8"))
    season["events"][0]["homeTeam"] = dict(id=9, name="Zed Zane")
    season["events"][0]["awayTeam"] = dict(id=8, name="Yan Yates")
    (tmp_path / "events" / "500.json").write_text(json.dumps(season), encoding="utf-8")
    counts = load(conn, tmp_path)
    assert counts["fights whose stats were cleared"] == 1
    assert conn.execute("SELECT count(*) FROM round_stats WHERE source = 'sofascore'").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM sofascore_round_stats").fetchone() == (0,)


def test_a_name_that_disagrees_isnt_saved_as_an_alias(tmp_path):
    conn = _db()
    _raw(tmp_path)
    season = json.loads((tmp_path / "events" / "500.json").read_text(encoding="utf-8"))
    season["events"][0]["awayTeam"] = dict(id=2, name="Somebody Else")  # matched by Alan Able, the other by elimination
    (tmp_path / "events" / "500.json").write_text(json.dumps(season), encoding="utf-8")
    counts = load(conn, tmp_path)
    assert counts["fights with stats"] == 1 and counts["names not saved as aliases"] == 1
    assert conn.execute("SELECT count(*) FROM fighter_aliases WHERE source = 'sofascore' AND source_id = '2'").fetchone() == (0,)
