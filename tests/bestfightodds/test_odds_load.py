import sqlite3

from canon_db import db
from canon_db.sources.bestfightodds.fetch import fighters_since, slugs_for
from canon_db.sources.bestfightodds.load import NAME, load


def _db():
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_promotions(conn)
    people = [(1, "Ann Avery"), (2, "Bea Brook"), (3, "Cara Cole")]
    conn.executemany("INSERT INTO fighters (fighter_id, name) VALUES (?, ?)", people)
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) VALUES (?, 'sherdog', 'sherdog.com', ?, ?)", [(i, str(i), n) for i, n in people])
    conn.execute("INSERT INTO events (event_id, name, date, promotion) VALUES (9, 'Card', '2024-05-04', 'UFC')")
    conn.execute("INSERT INTO fights (fight_id, event_id) VALUES (1, 9)")
    conn.executemany("INSERT INTO fight_participants VALUES (1, ?, ?, ?)", [(1, 0, "W"), (2, 1, "L")])
    return conn


def _side(slug, name, odds):
    cells = "".join(f"<td>{o}</td>" for o in odds)
    return f'<tr><th><a href="/fighters/{slug}">{name}</a></th>{cells}</tr>'


def _page(*sides, event="Card May 4th 2024"):
    return f"<table><tr><td>{event}</td></tr>{''.join(sides)}</table>"


def _pages(tmp_path, **pages):
    for slug, page in pages.items():
        (tmp_path / f"{slug}.html").write_text(page, encoding="utf-8")
    return sorted(pages)


def test_a_fight_on_both_pages_is_one_fight(tmp_path, monkeypatch):
    conn = _db()
    a, b = _side("ann-avery-1", "Ann Avery", ["-150", "-160", "", "-140"]), \
        _side("bea-brook-2", "Bea Brook", ["+130", "+120", "", "+140"])
    slugs = _pages(tmp_path, **{"ann-avery-1": _page(a, b), "bea-brook-2": _page(b, a), "gone-3": _page()})
    calls = []
    from canon_db import matching
    real = matching.Matcher.match
    monkeypatch.setattr(matching.Matcher, "match", lambda self, f: calls.append(f) or real(self, f))
    counts = load(conn, tmp_path, [*slugs, "not-on-disk"])
    assert len(calls) == 1  # the same fight on the second page isn't matched again
    assert (counts["pages"], counts["fight listings"], counts["fights matched"], counts["fights with odds"]) == (
        3, 2, 1, 1)
    assert conn.execute("SELECT fighter_id, book, open, close_low, close_high FROM odds ORDER BY 1").fetchall() == [
        (1, "all", -150, -160, -140), (2, "all", 130, 120, 140)]
    assert conn.execute("SELECT fighter_id, source_id FROM fighter_aliases WHERE source = ? ORDER BY 1",
                        (NAME,)).fetchall() == [(1, "ann-avery-1"), (2, "bea-brook-2")]
    load(conn, tmp_path, slugs)  # again: replaced, not added
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone() == (2,)
    assert load(conn, tmp_path, [])["fights matched"] == 0  # nothing read: what's loaded stays
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone() == (2,)


def test_a_fight_without_a_line_or_a_match(tmp_path):
    conn = _db()
    a, b = _side("ann-avery-1", "Ann Avery", ["", "", "", ""]), _side("bea-brook-2", "Bea Brook", ["", "", "", ""])
    x, y = _side("zed-zane-8", "Zed Zane", ["-110"] * 4), _side("yan-york-9", "Yan York", ["-110"] * 4)
    slugs = _pages(tmp_path, one=_page(a, b), two=_page(x, y))
    counts = load(conn, tmp_path, slugs)
    assert (counts["fights without a posted line"], counts["fights matched"], counts["fights with odds"]) == (1, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone() == (0,)


def test_choosing_pages():
    conn = _db()
    conn.executemany("INSERT INTO fighter_aliases (fighter_id, source, site, source_id, name) SELECT 1, ?1, (SELECT sites FROM sources WHERE source = ?1), ?2, 'Ann Avery'",
                     [(NAME, "ann-avery-1"), (NAME, "ann-avery-5")])
    assert fighters_since(conn, "2024-05-01") == {1, 2} and fighters_since(conn, "2024-06-01") == set()

    class Site:
        def get(self, _):
            return ("<loc>https://x/fighters/bea-brook-2</loc><loc>https://x/fighters/bea-brook-7</loc>"
                    "<loc>https://x/fighters/cara-cole-3</loc>")

    # Every page we know a fighter by; a fighter with none by name, which can give several.
    assert slugs_for({1, 2}, conn, Site(), NAME) == {"ann-avery-1", "ann-avery-5", "bea-brook-2", "bea-brook-7"}
