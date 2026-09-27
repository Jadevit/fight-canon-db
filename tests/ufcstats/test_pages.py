"""Page-parser tests against saved UFC Stats pages in tests/fixtures/.

Run: python -m pytest tests/
"""

from pathlib import Path

from canon_db.sources.ufcstats.pages import parse_event, parse_events_list, parse_fight, parse_fighter

FIX = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_events_list_rows():
    html = """
      <i class="b-statistics__table-content">
        <a href="http://ufcstats.com/event-details/7e654edcddd71550" class="b-link">
          UFC Fight Night: Rosas Jr. vs. Barcelos
        </a>
        <span class="b-statistics__date">
          September 26, 2026
        </span>
      </i>
    </td>
    <td class="b-statistics__table-col b-statistics__table-col_style_big-top-padding">
      Las Vegas, Nevada, USA
    </td>"""
    assert parse_events_list(html) == [dict(
        event_id="7e654edcddd71550", name="UFC Fight Night: Rosas Jr. vs. Barcelos",
        date="September 26, 2026", location="Las Vegas, Nevada, USA")]


def test_event_page():
    ev = parse_event(page("event.html"))
    assert ev["name"] == "UFC Fight Night: Rosas Jr. vs. Barcelos"
    assert ev["date"] == "September 26, 2026"
    assert ev["location"] == "Las Vegas, Nevada, USA"
    assert ev["fight_ids"][0] == "0e55d8a3d7a73912"   # main event first
    assert len(ev["fight_ids"]) == 12
    assert len(ev["fighter_ids"]) == 24


def test_modern_fight():
    f = parse_fight(page("fight_modern.html"))
    assert [(p["fighter_id"], p["status"]) for p in f["fighters"]] == [
        ("fe2babf95de24fb1", "W"), ("b9f28e7045fdfce7", "L")]
    assert f["bout"] == "Bantamweight Bout" and not f["belt"]
    assert (f["method"], f["round"], f["time"]) == ("KO/TKO", "5", "1:38")
    assert f["time_format"] == "5 Rnd (5-5-5-5-5)"
    assert f["referee"] == "Herb Dean"
    # 5 rounds x 2 fighters, each merging both per-round tables
    assert len(f["rounds"]) == 10
    r1 = f["rounds"][(1, "fe2babf95de24fb1")]
    assert r1["sig"] == "29 of 73" and r1["total"] == "49 of 98"
    assert r1["td_pct"] == "---"          # sentinel preserved for fields.py
    assert r1["head"] == "22 of 64" and r1["ground"] == "0 of 0"
    assert f["rounds"][(1, "b9f28e7045fdfce7")]["ctrl"] == "0:30"


def test_1994_fight():
    f = parse_fight(page("fight_1994.html"))
    assert [p["name"] for p in f["fighters"]] == ["Royce Gracie", "Patrick Smith"]
    assert f["bout"] == "UFC 2 Tournament Title Bout" and f["belt"]
    assert f["time_format"] == "No Time Limit"
    assert f["details"] == "Punches to Head From Mount Submission to Strikes"
    assert f["rounds"][(1, "429e7d3725852ce9")]["ctrl"] == "--"


def test_upcoming_fight_has_no_result():
    f = parse_fight(page("fight_upcoming.html"))
    assert len(f["fighters"]) == 2
    assert [p["status"] for p in f["fighters"]] == ["", ""]
    assert f["method"] == "" and f["rounds"] == {}


def test_fighter_page():
    assert parse_fighter(page("fighter.html")) == dict(
        name="Danny Abbadi", nickname="The Assassin", height="5' 11\"",
        weight="155 lbs.", reach="--", stance="Orthodox", dob="Jul 03, 1983")
