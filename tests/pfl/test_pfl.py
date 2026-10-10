import json
import sqlite3

from canon_db import db
from canon_db.sources.pfl.load import leftover, load
from canon_db.sources.pfl.pages import parse_event, parse_stats

PAGE = '<title>PFL 1 | Professional Fighters League</title> "startDate": "2020-01-02T00:00:00-04:00" var is_v2 = %s;'


def _person(pid, first, last, corner, rounds=None):
    return dict(Id=pid, FirstName=first, LastName=last, Corner=corner, RoundStats=rounds or [])


def _v2_side(sig):
    return dict(Knockdowns=1, SignificantStrikesLanded=sig, SignificantStrikesThrown=sig + 5, StrikesLanded=sig + 2,
                StrikesThrown=sig + 9, Takedowns=0, TakedownAttempts=1, SubmissionAttempts=0, ControlTime="01:05",
                HeadSignificantStrikesLanded=sig, HeadSignificantStrikesAttempted=sig + 5,
                DistanceStrikesLanded=sig, DistanceStrikesThrown=sig + 5)


def _v1_round(n, thrown, clock=300):
    return dict(Round=str(n), StrikesLanded=thrown // 2, StrikesThrown=str(thrown), ArmStrikesLanded=1,
                ArmStrikesThrown="2", LegStrikesLanded=0, LegStrikesThrown="0", GroundStrikesLanded=0,
                GroundStrikesThrown="0", PowerStrikesLanded=0, Knockdowns="0", Takedowns="1", TakedownAttempts="2",
                SubmissionAttempts="0", DominantPositions="0", GroundClock=0, StandingClock=clock)


V2 = [dict(HasFightStats=True, Fighters=[_person("1", "Alan", "Able", "Red"), _person("2", "Ben", "Baker", "Blue")],
           RoundStats={"1": {"red": _v2_side(10), "blue": _v2_side(3)}}),
      dict(HasFightStats=False, Fighters=[_person("3", "Carl", "Cole", "Red"), _person("4", "Dan", "Dent", "Blue")])]
V1 = [dict(HasFightStats=True, Fighters=[
    _person(1, "Alan", "Able", "Red", [_v1_round(1, 40), _v1_round(2, 0, clock=0)]),  # round 2 wasn't recorded
    _person(2, "Ben", "Baker", "Blue", [_v1_round(1, 20)])])]


def test_parse_event():
    assert parse_event(PAGE % "true") == dict(name="PFL 1", date="2020-01-02", v2=True)
    assert parse_event(PAGE % "false")["v2"] is False


def test_parse_stats_v2():
    (bout,) = parse_stats(V2, v2=True)
    assert [f["name"] for f in bout["fighters"]] == ["Alan Able", "Ben Baker"]
    red = next(r for r in bout["rounds"] if r["fighter"] == "1")
    assert (red["round"], red["sig_str_land"], red["sig_str_att"], red["total_str_land"], red["ctrl_sec"],
            red["head_land"], red["dist_att"], red["td_att"], red["knockdowns"]) == (1, 10, 15, 12, 65, 10, 15, 1, 1)
    assert red["clinch_land"] is None  # not in the reply: not recorded, not 0


def test_parse_stats_v1():
    (bout,) = parse_stats(V1, v2=False)
    assert [(r["fighter"], r["round"]) for r in bout["rounds"]] == [("1", 1), ("2", 1)]
    r = bout["rounds"][0]
    assert (r["total_str_land"], r["total_str_att"], r["arm_att"], r["td_land"], r["standing_sec"]) == (20, 40, 2, 1, 300)
    assert r["power_land"] is None and r["dominant_positions"] is None  # the event recorded none


def test_leftover_takes_the_one_fight_with_a_fighter():
    who = [dict(slug="9", name="Cris Cyborg"), dict(slug="8", name="Sara Collins")]
    free = [("f1", [("x", ["Cristiane Justino"]), ("y", ["Sara Collins"])]),
            ("f2", [("z", ["Kayla Harrison"]), ("w", ["Aspen Ladd"])])]
    assert leftover(who, free) == ("f1", {"9": "x", "8": "y"})
    assert leftover(who, free + [("f3", [("v", ["Sara Collins"]), ("u", ["Someone Else"])])]) is None


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name, source) VALUES (?,?,'sherdog')",
                     [("sherdog:100", "Alan Able"), ("sherdog:200", "Ben Baker")])
    conn.executemany("INSERT INTO fighter_aliases VALUES (?, 'sherdog', ?, ?)",
                     [("sherdog:100", "100", "Alan Able"), ("sherdog:200", "200", "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date, source) VALUES ('sherdog:9', 'PFL 1', '2020-01-03', 'sherdog')")
    conn.execute("INSERT INTO fights (fight_id, event_id, source) VALUES ('sherdog:9:100-200', 'sherdog:9', 'sherdog')")
    conn.executemany("INSERT INTO fight_participants VALUES ('sherdog:9:100-200', ?, ?, ?)",
                     [("sherdog:100", 0, "W"), ("sherdog:200", 1, "L")])
    return conn


def _raw(tmp_path, tag, fights, v2):
    (tmp_path / tag).mkdir()
    (tmp_path / tag / "event.html").write_text(PAGE % ("true" if v2 else "false"), encoding="utf-8")
    (tmp_path / tag / "stats.json").write_text(json.dumps(fights), encoding="utf-8")


def test_load_v2_into_round_stats(tmp_path):
    conn = _db()
    _raw(tmp_path, "pfl-x", V2, True)
    counts = load(conn, tmp_path)
    assert counts["fights with stats"] == 1 and not counts["bouts not matched"]
    assert conn.execute("SELECT fight_id, fighter_id, round, sig_str_land, ctrl_sec, reversals, source "
                        "FROM round_stats ORDER BY fighter_id").fetchall() == [
        ("sherdog:9:100-200", "sherdog:100", 1, 10, 65, None, "pfl"),
        ("sherdog:9:100-200", "sherdog:200", 1, 3, 65, None, "pfl")]
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source = 'pfl' AND source_id = 'pfl-x'"
                        ).fetchone() == ("sherdog:9",)
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source = 'pfl' AND source_id = '1'"
                        ).fetchone() == ("sherdog:100",)
    load(conn, tmp_path)  # loading again replaces, doesn't duplicate
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (2,)


def test_load_v1_into_smartcage(tmp_path):
    conn = _db()
    _raw(tmp_path, "2020-pfl-1", V1, False)
    load(conn, tmp_path)
    assert conn.execute("SELECT count(*) FROM round_stats").fetchone() == (0,)
    assert conn.execute("SELECT fighter_id, round, total_str_att, power_land FROM smartcage_round_stats "
                        "ORDER BY fighter_id").fetchall() == [("sherdog:100", 1, 40, None), ("sherdog:200", 1, 20, None)]
