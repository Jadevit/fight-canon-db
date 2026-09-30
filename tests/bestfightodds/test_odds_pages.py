"""BestFightOdds page-parser tests against a saved fighter page."""

from pathlib import Path

from canon_db.sources.bestfightodds.pages import american, parse_date, parse_fighter_page

FIX = Path(__file__).parent / "fixtures"


def test_fighter_page_lists_every_fight_with_both_sides():
    fights = parse_fighter_page((FIX / "fighter.html").read_text(encoding="utf-8"))
    assert len(fights) == 21
    assert all(len(f["fighters"]) == 2 for f in fights)
    oldest = fights[-1]
    assert (oldest["date"], oldest["event"]) == ("2007-08-25", "UFC 74: Respect")
    mir, hardonk = oldest["fighters"]
    assert (mir["name"], mir["open"], mir["close_low"], mir["close_high"]) == ("Frank Mir", -120, -165, -165)
    assert hardonk["close_high"] == 145


def test_parse_date_and_odds():
    assert parse_date("UFC 92: The Ultimate 2008 Dec 27th 2008") == "2008-12-27"
    assert parse_date("Some League Week 4 2026") is None   # not a date
    assert american("+145") == 145 and american("-165") == -165 and american("n/a") is None
