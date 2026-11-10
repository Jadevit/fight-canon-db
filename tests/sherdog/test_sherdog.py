import sqlite3
from pathlib import Path

from canon_db import db, ids
from canon_db.sources.sherdog.fetch import Pages
from canon_db.sources.sherdog.linking import State, follow
from canon_db.sources.sherdog.pages import parse_event, parse_fighter, parse_sitemap
from canon_db.sources.sherdog.write import Labels, write

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_fighter():
    p = parse_fighter((FIXTURES / "fighter.html").read_text(encoding="utf-8"))
    assert p["name"] == "Jack Shore" and p["nickname"] == "Tank" and p["dob"] == "Feb 6, 1995"
    assert len(p["fights"]) == 20  # pro only: the amateur table is left out
    # The printed record (no draws or no contests printed: none); the team.
    assert p["record"] == dict(wins=17, losses=3, draws=0, no_contests=0)
    assert p["association"] == "Tillery Combat MMA Academy"
    assert parse_fighter("<html></html>")["record"] == dict.fromkeys(("wins", "losses", "draws", "no_contests"))
    f = p["fights"][0]
    assert (f["date"], f["result"], f["opponent"], f["opponent_id"]) == (
        "2024-11-02", "L", "Youssef Zalal", "229985")
    assert f["event_id"] and f["method"] and f["round"].isdigit()


def test_parse_event():
    e = parse_event((FIXTURES / "event.html").read_text(encoding="utf-8"))
    assert (e["name"], e["org_id"], e["org"]) == ("AFC - Brazil 1", "331", "Absolute Fighting Championship")


def test_labels():
    conn = _db()
    labels = Labels(conn)
    assert labels.label("2", "Absolute Fighting Championship") == "Absolute Fighting Championship"
    assert labels.label("3", "Absolute Fighting Championship") == "Absolute Fighting Championship (3)"
    assert labels.label("2", "Absolute Fighting Championship") == "Absolute Fighting Championship"


def test_parse_event_sitemap():
    from canon_db.sources.sherdog.pages import parse_event_sitemap
    xml = ("<url><loc>https://www.sherdog.com/events/UFC-1-The-Beginning-7</loc></url>"
           "<url><loc>https://www.sherdog.com/events/Cage-Warriors-180-114941</loc></url>")
    assert parse_event_sitemap(xml) == ["7", "114941"]


def test_parse_sitemap():
    xml = "<url><loc>https://www.sherdog.com/fighter/Jack-Shore-110177</loc></url>"
    assert parse_sitemap(xml) == [("110177", "Jack Shore")]


def _db():
    """Alan Able (1) and Ben Baker (2), who fought at UFC 1: all UFC Stats rows."""
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?,?)", [(1, "Alan Able"), (2, "Ben Baker")])
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (?, 'ufc', 'ufcstats.com', ?, ?)",
                     [(1, "a" * 16, "Alan Able"), (2, "b" * 16, "Ben Baker")])
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (1, 'UFC 1', '2020-01-02', 'UFC')")
    conn.execute("INSERT INTO event_aliases (event_id, source, site, source_id, name) VALUES (1, 'ufc', 'ufcstats.com', 'e1', NULL)")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (1, 1)")
    conn.execute("INSERT INTO fight_aliases (fight_id, source, site, source_id, name) VALUES (1, 'ufc', 'ufcstats.com', 'f1', NULL)")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)", [(1, 0, "W"), (2, 1, "L")])
    return conn


def _fight(opp, opp_id, ev_id, day, result="W"):
    return dict(result=result, opponent=opp, opponent_id=opp_id, event_id=ev_id, event="X 1 - X",
                date=day, title=False, method="TKO (Punches)", referee="", round="1", time="1:00")


def test_link_through_shared_fight_then_merge():
    conn = _db()
    st = State(conn)
    st.link(1, "100", "seed")
    page = dict(name="Alan Able", fights=[
        _fight("Ben Baker", "200", "900", "2020-01-01"),       # the UFC fight, a day off
        _fight("Carl Cole", "300", "901", "2019-05-05")])      # regional, unknown to UFC Stats
    assert follow(st, 1, page) == [2]
    write(conn, st, {1: page})
    assert ids.find(conn, "fighter", "sherdog", "200") == 2
    assert ids.find(conn, "event", "sherdog", "900") == 1
    assert ids.find(conn, "fight", "sherdog", "900:100-200") == 1   # the UFC fight gets Sherdog's alias
    regional = ids.find(conn, "fight", "sherdog", "901:100-300")
    carl = ids.find(conn, "fighter", "sherdog", "300")
    assert regional == 2 and carl == 3 and conn.execute("SELECT name FROM fighters WHERE fighter_id = 3").fetchone() == (
        "Carl Cole",)
    assert conn.execute("SELECT method, bout_type FROM fights WHERE fight_id = 1").fetchone() == ("TKO (Punches)", "pro")

    # Carl Cole turns up on UFC Stats (a new row, 4), fighting Ben Baker: the two are one, the
    # older id stays, and Carl gets the UFC Stats alias.
    conn.execute("INSERT INTO fighters (fighter_id, name) VALUES (4, 'Carl Cole')")
    conn.execute("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (4, 'ufc', 'ufcstats.com', ?, 'Carl Cole')", ("c" * 16,))
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (5, 'UFC 2', '2021-01-01', 'UFC')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (5, 5)")
    conn.execute("INSERT INTO fight_aliases (fight_id, source, site, source_id, name) VALUES (5, 'ufc', 'ufcstats.com', 'f2', NULL)")
    conn.executemany("INSERT INTO fight_participants VALUES (5, ?, ?, 'W')", [(2, 0), (4, 1)])
    st = State(conn)
    follow(st, 2, dict(name="Ben Baker", fights=[_fight("Carl Cole", "300", "902", "2021-01-01")]))
    write(conn, st, {})
    assert conn.execute("SELECT kind, old_id, new_id FROM retired_ids WHERE reason = 'merged'").fetchall() == [("fighter", 4, 3)]
    assert ids.find(conn, "fighter", "ufc", "c" * 16) == 3
    assert conn.execute("SELECT fighter_id FROM fight_participants WHERE fight_id = 5 AND corner = 1").fetchone() == (3,)
    assert conn.execute("SELECT 1 FROM fighters WHERE fighter_id = 4").fetchone() is None


def test_a_fight_against_oneself_is_skipped():
    conn = _db()
    st = State(conn)
    st.link(1, "100", "seed")
    write(conn, st, {1: dict(name="Alan Able", fights=[_fight("Alan Able", "100", "903", "2018-03-03")])})
    assert conn.execute("SELECT count(*) FROM fights").fetchone() == (1,)


def test_name_mismatch_needs_two_pages():
    conn = _db()
    conn.execute("INSERT INTO fighters (fighter_id, name) VALUES (4, 'Dan Dent')")
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (3, 'UFC 3', '2022-01-01', 'UFC')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (3, 3)")
    conn.executemany("INSERT INTO fight_participants VALUES (3, ?, ?, 'W')", [(4, 0), (2, 1)])
    st = State(conn)
    st.link(1, "100", "seed")
    st.link(4, "400", "seed")
    # Sherdog knows Ben Baker by his real name: one page isn't enough, a second one is.
    assert follow(st, 1, dict(name="", fights=[_fight("Robert Smith", "200", "900", "2020-01-02")])) == []
    assert st.match(1, _fight("Robert Smith", "200", "900", "2020-01-02"))  # still the same fight
    assert follow(st, 4, dict(name="", fights=[_fight("Robert Smith", "200", "903", "2022-01-01")])) == [2]


def test_tournament_night_needs_names():
    conn = _db()
    st = State(conn)
    st.link(1, "100", "seed")
    page = dict(name="", fights=[_fight("Ben Baker", "200", "900", "2020-01-02"),
                                 _fight("Zed Zane", "500", "900", "2020-01-02")])
    assert follow(st, 1, page) == [2]          # the fight UFC Stats has
    assert "500" not in st.fid                 # the one it doesn't isn't forced onto it
    write(conn, st, {1: page})
    assert conn.execute("SELECT count(*) FROM fights").fetchone() == (2,)
    assert ids.find(conn, "fight", "sherdog", "900:100-500") == 2


def test_duplicate_sherdog_profile():
    conn = _db()
    st = State(conn)
    st.link(2, "200", "seed")
    assert st.link(2, "201", "second profile") is False  # extra id, nothing new to read
    assert st.fid["201"] == 2 and st.sd[2] == "200"
    st.link(1, "100", "seed")
    assert st.link(1, "201", "clash") is False and st.conflicts  # both have UFC Stats ids


def test_an_id_two_fighters_share_is_not_passed_back_and_forth():
    conn = _db()
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, 'Carl Cole')", [(3,), (4,)])
    st = State(conn)
    st.link(3, "300", "seed")
    assert st.link(4, "300", "same person") is True  # 4 takes it; the pair is merged on write
    assert st.link(3, "300", "again") is False and st.fid["300"] == 4
    assert st.new_links == [(3, "300", None), (4, "300", 3)]


def test_merge_keeps_older_id_and_official_values():
    conn = _db()
    conn.execute("INSERT INTO fighters (fighter_id, name, nationality) VALUES (3, 'Ben Baker Jr', 'England')")
    conn.execute("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (3, 'sherdog', 'sherdog.com', '200', 'Ben Baker Jr')")
    assert ids.merge(conn, "fighter", 3, 2) == 2
    assert conn.execute("SELECT name, nationality FROM fighters WHERE fighter_id = 2").fetchone() == (
        "Ben Baker", "England")  # UFC Stats' name, Sherdog's nationality
    assert conn.execute("SELECT fighter_id FROM fighter_aliases WHERE source = 'sherdog'").fetchall() == [(2,)]
    assert conn.execute("SELECT kind, old_id, new_id FROM retired_ids WHERE reason = 'merged'").fetchall() == [("fighter", 3, 2)]


def test_parse_card():
    from canon_db.sources.sherdog.pages import parse_card
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


def test_parse_fighter_sections_and_bio():
    from canon_db.sources.sherdog.pages import bio
    p = parse_fighter((FIXTURES / "fighter.html").read_text(encoding="utf-8"))
    assert len(p["fights"]) == 20 and isinstance(p["exhibition"], list) and p["amateur"]
    assert bio(p) == dict(nickname="Tank", dob="1995-02-06", height_in=69, weight_lbs=145, nationality="Wales")


def test_only_pages_named_by_an_id_are_pages(tmp_path):
    pages = Pages(tmp_path, None)
    for name in ("100.html", "100 copy.html"):
        (pages.dir / name).write_text("", encoding="utf-8")
    assert pages.on_disk() == ["100"]
