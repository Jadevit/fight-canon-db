import sqlite3
from pathlib import Path

from canon_db import db
from canon_db.sources.sherdog.load import Labels, State, follow, merge, write
from canon_db.sources.sherdog.pages import parse_event, parse_fighter, parse_sitemap

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_fighter():
    p = parse_fighter((FIXTURES / "fighter.html").read_text(encoding="utf-8"))
    assert p["name"] == "Jack Shore" and p["nickname"] == "Tank" and p["dob"] == "Feb 6, 1995"
    assert len(p["fights"]) == 20  # pro only: the amateur table is left out
    f = p["fights"][0]
    assert (f["date"], f["result"], f["opponent"], f["opponent_id"]) == (
        "2024-11-02", "L", "Youssef Zalal", "229985")
    assert f["event_id"] and f["method"] and f["round"].isdigit()


def test_parse_event():
    e = parse_event((FIXTURES / "event.html").read_text(encoding="utf-8"))
    assert (e["name"], e["org_id"], e["org"]) == ("AFC - Brazil 1", "331", "Absolute Fighting Championship")


def test_labels():
    conn = _db()
    labels = Labels(conn, hints={"1": "PRIDE"})
    assert labels.label("1", "Pride Fighting Championships") == "PRIDE"  # shares our events
    assert labels.label("2", "Absolute Fighting Championship") == "Absolute Fighting Championship"
    assert labels.label("3", "Absolute Fighting Championship") == "Absolute Fighting Championship (3)"
    assert labels.label("2", "Absolute Fighting Championship") == "Absolute Fighting Championship"


def test_parse_sitemap():
    xml = "<url><loc>https://www.sherdog.com/fighter/Jack-Shore-110177</loc></url>"
    assert parse_sitemap(xml) == [("110177", "Jack Shore")]


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA.read_text())
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?,?)",
                     [("a" * 16, "Alan Able"), ("b" * 16, "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date) VALUES ('e1', 'UFC 1', '2020-01-02')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES ('f1', 'e1')")
    conn.executemany("INSERT INTO fight_participants VALUES ('f1', ?, ?, ?)",
                     [("a" * 16, 0, "W"), ("b" * 16, 1, "L")])
    return conn


def _fight(opp, opp_id, ev_id, day, result="W"):
    return dict(result=result, opponent=opp, opponent_id=opp_id, event_id=ev_id, event="X 1 - X",
                date=day, title=False, method="TKO (Punches)", referee="", round="1", time="1:00")


def test_link_through_shared_fight_then_merge():
    conn = _db()
    st = State(conn)
    st.link("a" * 16, "100", "seed")
    page = dict(name="Alan Able", fights=[
        _fight("Ben Baker", "200", "900", "2020-01-01"),       # the UFC fight, a day off
        _fight("Carl Cole", "300", "901", "2019-05-05")])      # regional, unknown to UFC Stats
    assert follow(st, "a" * 16, page) == ["b" * 16]
    write(conn, st, {"a" * 16: page})
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source_id = '200'").fetchone() == ("b" * 16,)
    assert conn.execute("SELECT event_id FROM event_aliases WHERE source_id = '900'").fetchone() == ("e1",)
    assert conn.execute("SELECT fight_id, source FROM fights WHERE source = 'sherdog'").fetchall() == [
        ("sherdog:901:100-300", "sherdog")]
    assert conn.execute("SELECT name FROM fighters WHERE fighter_id = 'sherdog:300'").fetchone() == ("Carl Cole",)

    # Carl Cole turns up on UFC Stats, fighting Ben Baker: his rows move to the new id.
    conn.execute("INSERT INTO fighters (fighter_id, name) VALUES (?, 'Carl Cole')", ("c" * 16,))
    conn.execute("INSERT INTO events (event_id, name, date) VALUES ('e2', 'UFC 2', '2021-01-01')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES ('f2', 'e2')")
    conn.executemany("INSERT INTO fight_participants VALUES ('f2', ?, ?, 'W')", [("b" * 16, 0), ("c" * 16, 1)])
    st = State(conn)
    follow(st, "b" * 16, dict(name="Ben Baker", fights=[_fight("Carl Cole", "300", "902", "2021-01-01")]))
    write(conn, st, {})
    assert conn.execute("SELECT new_id FROM fighter_redirects WHERE old_id = 'sherdog:300'").fetchone() == ("c" * 16,)
    assert conn.execute("SELECT fighter_id FROM fight_participants WHERE fight_id = 'sherdog:901:100-300' "
                        "AND fighter_id <> ?", ("a" * 16,)).fetchone() == ("c" * 16,)
    assert conn.execute("SELECT 1 FROM fighters WHERE fighter_id = 'sherdog:300'").fetchone() is None


def test_name_mismatch_needs_two_pages():
    conn = _db()
    conn.execute("INSERT INTO fighters (fighter_id, name) VALUES (?, 'Dan Dent')", ("d" * 16,))
    conn.execute("INSERT INTO events (event_id, name, date) VALUES ('e3', 'UFC 3', '2022-01-01')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES ('f3', 'e3')")
    conn.executemany("INSERT INTO fight_participants VALUES ('f3', ?, ?, 'W')", [("d" * 16, 0), ("b" * 16, 1)])
    st = State(conn)
    st.link("a" * 16, "100", "seed")
    st.link("d" * 16, "400", "seed")
    # Sherdog knows Ben Baker by his real name: one page isn't enough, a second one is.
    assert follow(st, "a" * 16, dict(name="", fights=[_fight("Robert Smith", "200", "900", "2020-01-02")])) == []
    assert st.match("a" * 16, _fight("Robert Smith", "200", "900", "2020-01-02"))  # still the same fight
    assert follow(st, "d" * 16, dict(name="", fights=[_fight("Robert Smith", "200", "903", "2022-01-01")])) == ["b" * 16]


def test_tournament_night_needs_names():
    conn = _db()
    st = State(conn)
    st.link("a" * 16, "100", "seed")
    page = dict(name="", fights=[_fight("Ben Baker", "200", "900", "2020-01-02"),
                                 _fight("Zed Zane", "500", "900", "2020-01-02")])
    assert follow(st, "a" * 16, page) == ["b" * 16]          # the fight UFC Stats has
    assert "500" not in st.fid                                # the one it doesn't isn't forced onto it
    write(conn, st, {"a" * 16: page})
    assert conn.execute("SELECT count(*) FROM fights WHERE source = 'sherdog'").fetchone() == (1,)


def test_duplicate_sherdog_profile():
    conn = _db()
    st = State(conn)
    st.link("b" * 16, "200", "seed")
    assert st.link("b" * 16, "201", "second profile") is False  # extra id, nothing new to read
    assert st.fid["201"] == "b" * 16 and st.sd["b" * 16] == "200"
    st.link("a" * 16, "100", "seed")
    assert st.link("a" * 16, "201", "clash") is False and st.conflicts


def test_merge_keeps_one_row_per_fight():
    conn = _db()
    conn.execute("INSERT INTO fighters (fighter_id, name, source) VALUES ('sherdog:1', 'Ben Baker', 'sherdog')")
    merge(conn, "sherdog:1", "b" * 16)
    assert conn.execute("SELECT new_id FROM fighter_redirects").fetchall() == [("b" * 16,)]


def test_parse_card():
    from canon_db.sources.sherdog.pages import parse_card, parse_org_events
    page = (FIXTURES / "event_card.html").read_text(encoding="utf-8")
    assert parse_event(page)["date"] == "2018-12-08"
    card = parse_card(page)
    assert [b["match"] for b in card] == list(range(19, 0, -1))
    main = card[0]
    assert [(s["sid"], s["name"], s["result"]) for s in main["sides"]] == [
        ("110177", "Jack Shore", "W"), ("218081", "Mike Ekundayo", "L")]
    assert (main["weight_class"], main["title"], main["method"], main["referee"], main["round"], main["time"]) == (
        "Bantamweight", True, "TKO (Punches)", "Marc Goddard", "3", "4:07")
    assert sum(b["title"] for b in card) == 2
    events, older = parse_org_events((FIXTURES / "organization.html").read_text(encoding="utf-8"))
    assert events[0] == ("114289", "2026-10-30") and len(events) == 102 and older


def test_parse_fighter_sections_and_bio():
    from canon_db.sources.sherdog.pages import bio
    p = parse_fighter((FIXTURES / "fighter.html").read_text(encoding="utf-8"))
    assert len(p["fights"]) == 20 and isinstance(p["exhibition"], list) and p["amateur"]
    assert bio(p) == dict(nickname="Tank", dob="1995-02-06", height_in=69, weight_lbs=145, nationality="Wales")
