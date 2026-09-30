"""Unit tests for the cell parsers. Run: python -m pytest tests/."""

from canon_db.sources.ufcstats import fields as F


def test_id_from_url():
    assert F.id_from_url("http://ufcstats.com/fight-details/568ec6af4008355a") == "568ec6af4008355a"
    assert F.id_from_url("http://ufcstats.com/fighter-details/93fe7332d16c6ad9") == "93fe7332d16c6ad9"
    assert F.id_from_url("") is None
    assert F.id_from_url(None) is None


def test_parse_of():
    assert F.parse_of("13 of 27") == (13, 27)
    assert F.parse_of("0 of 0") == (0, 0)
    assert F.parse_of("---") == (None, None)
    assert F.parse_of("") == (None, None)


def test_parse_int_blank_is_none_not_zero():
    assert F.parse_int("0") == 0          # a real zero stays zero
    assert F.parse_int("---") is None     # unknown becomes NULL, never 0
    assert F.parse_int("") is None


def test_parse_ctrl():
    assert F.parse_ctrl("1:30") == 90
    assert F.parse_ctrl("0:45") == 45
    assert F.parse_ctrl("--") is None


def test_bio_parsers():
    assert F.parse_height_in("5' 11\"") == 71
    assert F.parse_reach_in("76\"") == 76
    assert F.parse_weight_lbs("155 lbs.") == 155
    assert F.parse_dob("Jul 13, 1978") == "1978-07-13"
    assert F.parse_height_in("--") is None
    assert F.parse_dob("--") is None


def test_result_flags():
    assert F.is_no_contest("Overturned - No Contest")
    assert not F.is_no_contest("Decision - Unanimous")



def test_parse_record():
    assert F.parse_record("Record: 4-6-0") == (4, 6, 0, 0)
    assert F.parse_record("Record: 28-18-0 (1 NC)") == (28, 18, 0, 1)
    assert F.parse_record("") == (None, None, None, 0)


def test_promotion_from_event_name():
    from canon_db.sources.ufcstats.load import promotion
    assert promotion("PRIDE 33: The Second Coming") == "PRIDE"
    assert promotion("PRIDE Shockwave 2006") == "PRIDE"
    assert promotion("Road to UFC 4.5 + 4.6") == "UFC"
    assert promotion("Strikeforce: Nashville") == "Strikeforce"
    assert promotion("IFC - Global Domination") == "IFC"
    assert promotion("Meca 9") == "Meca"
    assert promotion("WEC 41") == "WEC"
