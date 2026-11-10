import sqlite3

from canon_db import db
from canon_db.matching import Cards, fighter_names, name_ok


def _db():
    """One event with two fights: Ann Avery vs Bea Brook, Cara Cole vs Dina Dale."""
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    people = [(1, "Ann Avery"), (2, "Bea Brook"), (3, "Cara Cole"), (4, "Dina Dale")]
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", people)
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (?, 'sherdog', 'sherdog.com', ?, ?)", [(i, str(i), n) for i, n in people])
    conn.execute("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (1, 'sofascore', 'sofascore.com', 'x', 'Annie Avery')")
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (9, 'Card', '2024-05-04', 'UFC')")
    conn.executemany("INSERT INTO fights (fight_id, event_id) VALUES (?, 9)", [(1,), (2,)])
    conn.executemany("INSERT INTO fight_participants VALUES (?, ?, ?, 'W')",
                     [(1, 1, 0), (1, 2, 1), (2, 3, 0), (2, 4, 1)])
    return conn


def bout(day, a, b):
    return dict(date=day, fighters=[dict(slug=a, name=a), dict(slug=b, name=b)])


def test_names():
    conn = _db()
    assert sorted(fighter_names(conn)[1]) == ["Ann Avery", "Annie Avery"]
    assert name_ok("Ann Avery-Smith", ["Ann Avery"]) and not name_ok("Zed Zane", ["Ann Avery"])


def test_card_placed_whole():
    cards = Cards(_db())
    # A day off (time zones), sides swapped: still the fight, each name on its fighter.
    eid, matched, by_one = cards.place([("a", bout("2024-05-05", "Bea Brook", "Ann Avery")),
                                        ("b", bout("2024-05-04", "Cara Cole", "Dee Ring Name"))])
    assert eid == 9 and by_one == 1
    assert matched["a"] == (1, {"Bea Brook": 2, "Ann Avery": 1})
    # The bout whose second name didn't match takes the event's one fight left that has its first.
    assert matched["b"] == (2, {"Cara Cole": 3, "Dee Ring Name": 4})


def test_card_not_placed():
    cards = Cards(_db())
    # Nothing matches on the date: no event, so no leftover pass either.
    assert cards.place([("a", bout("2024-06-01", "Ann Avery", "Bea Brook"))]) == (None, {}, 0)
    # A leftover bout with neither fighter on the event's free fights stays out.
    eid, matched, by_one = cards.place([("a", bout("2024-05-04", "Ann Avery", "Bea Brook")),
                                        ("b", bout("2024-05-04", "Zed Zane", "Yan York"))])
    assert (eid, set(matched), by_one) == (9, {"a"}, 0)
