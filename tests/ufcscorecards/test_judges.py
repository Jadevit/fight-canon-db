import sqlite3

import pytest

pytest.importorskip("cv2")  # the scorecard reader's extra dependency; not installed in CI
from canon_db.sources.ufcscorecards.run import Judges, judges_in  # noqa: E402


def test_judges_in_details():
    assert judges_in("Sal D'amato 29 - 28. Chris Lee 28 - 29. Mike Bell 29 - 28.") == [
        "Sal D'amato", "Chris Lee", "Mike Bell"]


def test_judge_names_snap_to_ufc_stats():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fights (details TEXT)")
    conn.executemany("INSERT INTO fights VALUES (?)", [("Junichiro Kamijo 29 - 28.",)] * 3)
    j = Judges(conn)
    assert j.name("| JUNICHIRO KAMI)O", []) == "Junichiro Kamijo"
    assert j.name("BRIAN MINER .", ["Bryan Miner", "Mike Bell"]) == "Bryan Miner"
    assert j.name("JANE DOE", []) == "Jane Doe"
