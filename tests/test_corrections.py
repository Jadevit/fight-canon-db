import csv
import json
import sqlite3

import pytest

from canon_db import claims, corrections, db, ids


def _conn():
    conn = sqlite3.connect(":memory:")
    db.create(conn)
    db.sync_sources(conn)
    conn.execute("INSERT INTO promotions VALUES ('UFC', NULL, 'full')")
    return conn


def _bout(conn):
    """Two fighters in one UFC Stats fight, Ann winning, with its claims."""
    fa, fb = ids.add(conn, "fighter", {"name": "Ann Avery"}), ids.add(conn, "fighter", {"name": "Bea Bell"})
    e = ids.add(conn, "event", {"name": "E", "promotion": "UFC"})
    f = ids.add(conn, "fight", {"event_id": e})
    for kind, id_, sid in (("fighter", fa, "a"), ("fighter", fb, "b"), ("event", e, "e"), ("fight", f, "f")):
        ids.alias(conn, kind, id_, "ufc", sid)
    claims.claim(conn, "fighter", fa, "ufc", "a", dict(name="Ann Avery", height_in=66))
    claims.claim(conn, "fighter", fb, "ufc", "b", dict(name="Bea Bell"))
    claims.claim(conn, "event", e, "ufc", "e", dict(name="E", date="2020-01-01", promotion="UFC"))
    claims.claim(conn, "fight", f, "ufc", "f", dict(event_id=e, method="KO/TKO"))
    claims.sides(conn, f, "ufc", "f", [dict(fighter_id=fa, result="W", corner=0), dict(fighter_id=fb, result="L", corner=1)])
    claims.resolve(conn)
    return fa, fb, e, f


def _row(**kw):
    r = dict(id="1", kind="fighter", target="1", field="height_in", value="67", citation="https://example.org/a",
             explanation="The page had a typo; the commission's sheet says 67.", submitted_by="jaden",
             accepted_by="jaden", date="2026-11-09")
    r.update({k: str(v) for k, v in kw.items()})
    return r


def _file(tmp_path, *rows):
    path = tmp_path / "corrections.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, corrections.FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


def test_the_file_in_the_repo_is_valid():
    assert corrections.validate(corrections.read()) == []


@pytest.mark.parametrize("bad, why", [
    (dict(explanation=""), "no explanation"),
    (dict(accepted_by=""), "no accepted_by"),
    (dict(citation=""), "no citation"),
    (dict(date="9 Nov 2026"), "`date`"),
    (dict(field="wins"), "no column 'wins'"),  # counted from fights, not set
    (dict(kind="result", field="2", value="won"), "`value` must be"),
    (dict(kind="same", field="fighter", value="1"), "another id"),
    (dict(kind="flag", field="results disagree", target="fight 1"), "row_key"),
    (dict(kind="rename"), "no kind"),
])
def test_a_row_without_what_it_needs_is_refused(bad, why):
    errors = corrections.validate([_row(**bad)])
    assert len(errors) == 1 and why in errors[0]
    assert corrections.validate([_row(), _row()]) == ["line 3 (id 1): `id` is used twice"]


def test_a_refused_row_fails_the_update(tmp_path):
    with pytest.raises(RuntimeError, match="no explanation"):
        corrections.apply(_conn(), _file(tmp_path, _row(explanation="")))


def test_a_value_correction_beats_every_source_and_going_undoes_it(tmp_path):
    conn = _conn()
    fa, _, e, f = _bout(conn)
    corrections.apply(conn, _file(tmp_path, _row(target=fa), _row(id=2, kind="event", target=e, field="date",
                                                                  value="2020-01-02"),
                                  _row(id=3, kind="fight", target=f, field="end_round", value="2")))
    claims.resolve(conn)
    assert conn.execute("SELECT height_in FROM fighters WHERE fighter_id = ?", (fa,)).fetchone() == (67,)
    assert conn.execute("SELECT date FROM events").fetchone() == ("2020-01-02",)
    assert conn.execute("SELECT end_round, method FROM fights").fetchone() == (2, "KO/TKO")
    assert conn.execute("SELECT * FROM corrections WHERE id = 1").fetchone()[2:5] == (str(fa), "height_in", "67")
    corrections.apply(conn, _file(tmp_path))
    claims.resolve(conn)
    assert conn.execute("SELECT height_in FROM fighters WHERE fighter_id = ?", (fa,)).fetchone() == (66,)
    assert conn.execute("SELECT count(*) FROM corrections").fetchone() == (0,)


def test_a_result_correction_sets_both_sides_and_keeps_corners(tmp_path):
    conn = _conn()
    fa, fb, _, f = _bout(conn)
    corrections.apply(conn, _file(tmp_path, _row(kind="result", target=f, field=fb, value="W")))
    claims.resolve(conn)
    assert conn.execute("SELECT fighter_id, corner, result FROM fight_participants ORDER BY corner").fetchall() == [
        (fa, 0, "L"), (fb, 1, "W")]
    assert conn.execute("SELECT wins FROM fighters WHERE fighter_id = ?", (fb,)).fetchone() == (1,)


def test_corrections_follow_merged_ids_and_skip_gone_ones(tmp_path):
    conn = _conn()
    fa, fb, _, _ = _bout(conn)
    other = ids.add(conn, "fighter", {"name": "Ann Avery"})
    ids.merge(conn, "fighter", fa, other)  # `other` became fa
    ids.retire(conn, "fighter", 999, None, "deleted")
    counts = corrections.apply(conn, _file(tmp_path, _row(target=other), _row(id=2, target=999)))
    claims.resolve(conn)
    assert conn.execute("SELECT height_in FROM fighters WHERE fighter_id = ?", (fa,)).fetchone() == (67,)
    assert counts["values"] == 1 and counts["not applied"] == 1


def test_same_merges_and_different_keeps_apart(tmp_path):
    conn = _conn()
    fa, fb, _, _ = _bout(conn)
    twin = ids.add(conn, "fighter", {"name": "Ann Avery"})
    corrections.apply(conn, _file(tmp_path, _row(kind="same", field="fighter", target=twin, value=fa),
                                  _row(id=2, kind="different", field="fighter", target=fa, value=fb)))
    assert ids.now(conn, "fighter", twin) == fa
    assert ids.merge(conn, "fighter", fa, fb) is None  # nothing merges them
    assert conn.execute("SELECT count(*) FROM fighters").fetchone() == (2,)


def test_a_flag_corrected_stays_cleared(tmp_path):
    conn = _conn()
    fa, _, _, f = _bout(conn)
    claims.sides(conn, f, "sherdog", "9:1-2", [dict(fighter_id=fa, result="L"), dict(fighter_id=fa + 1, result="W")])
    claims.resolve(conn)
    assert conn.execute("SELECT count(*) FROM review_flags").fetchone() == (2,)
    key = json.dumps({"fight_id": f, "fighter_id": fa})
    corrections.apply(conn, _file(tmp_path, _row(kind="flag", field="results disagree", target=key, value="")))
    assert claims.resolve(conn)["flags corrected"] == 1
    assert conn.execute("SELECT row_key FROM review_flags").fetchall() == [
        (json.dumps({"fight_id": f, "fighter_id": fa + 1}),)]
