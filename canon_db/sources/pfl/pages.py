"""Parse what the PFL site (pflmma.com) serves for an event: its page, and the stats its
script requests. Stats come in two shapes: v2 (events from 2025-12) has the columns of
round_stats; v1 (SmartCage, earlier) has its own."""

from __future__ import annotations

import re


def parse_event(page: str) -> dict:
    """{name, date, v2} from an event page."""
    name = re.search(r"<title>(.*?)\s*\|", page, re.S)
    day = re.search(r'"startDate"\s*:\s*"(\d{4}-\d\d-\d\d)', page)
    return dict(name=name.group(1).strip() if name else "", date=day.group(1) if day else None,
                v2="var is_v2 = true" in page)


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _seconds(v) -> int | None:
    m = re.fullmatch(r"(\d+):(\d\d)", str(v or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


# round_stats column -> the v2 key it comes from
V2 = dict(knockdowns="Knockdowns", sig_str_land="SignificantStrikesLanded", sig_str_att="SignificantStrikesThrown",
          total_str_land="StrikesLanded", total_str_att="StrikesThrown", td_land="Takedowns",
          td_att="TakedownAttempts", sub_att="SubmissionAttempts",
          head_land="HeadSignificantStrikesLanded", head_att="HeadSignificantStrikesAttempted",
          body_land="BodySignificantStrikesLanded", body_att="BodySignificantStrikesAttempted",
          leg_land="LegsSignificantStrikesLanded", leg_att="LegsSignificantStrikesAttempted",
          dist_land="DistanceStrikesLanded", dist_att="DistanceStrikesThrown",
          clinch_land="ClinchStrikesLanded", clinch_att="ClinchStrikesThrown",
          ground_land="GroundStrikesLanded", ground_att="GroundStrikesThrown")
# smartcage_round_stats column -> the v1 key
V1 = dict(knockdowns="Knockdowns", total_str_land="StrikesLanded", total_str_att="StrikesThrown",
          arm_land="ArmStrikesLanded", arm_att="ArmStrikesThrown", leg_land="LegStrikesLanded",
          leg_att="LegStrikesThrown", ground_land="GroundStrikesLanded", ground_att="GroundStrikesThrown",
          power_land="PowerStrikesLanded", td_land="Takedowns", td_att="TakedownAttempts",
          sub_att="SubmissionAttempts", dominant_positions="DominantPositions",
          ground_sec="GroundClock", standing_sec="StandingClock")
SOMETIMES = ("power_land", "dominant_positions")  # not recorded on every event


def parse_stats(fights: list[dict], v2: bool) -> list[dict]:
    """The bouts that have stats: [{fighters: [{id, name}] x2, rounds: [{fighter (its id), round,
    <columns>}]}], columns as in round_stats (v2) or smartcage_round_stats (v1). A v1 round
    with nothing in it (no strike thrown, no time) is left out: it wasn't recorded."""
    out = []
    for f in fights:
        people = f.get("Fighters") or []
        if not f.get("HasFightStats") or len(people) != 2:
            continue
        bout = dict(fighters=[dict(id=str(p["Id"]), name=f"{p.get('FirstName') or ''} {p.get('LastName') or ''}".strip())
                              for p in people], rounds=[])
        if v2:
            corner = {str(p.get("Corner", "")).lower(): str(p["Id"]) for p in people}
            for rnd, sides in (f.get("RoundStats") or {}).items():
                for side, s in (sides or {}).items():
                    if s and side in corner and _int(rnd):
                        row = {col: _int(s.get(key)) for col, key in V2.items()}
                        bout["rounds"].append(dict(fighter=corner[side], round=int(rnd),
                                                   ctrl_sec=_seconds(s.get("ControlTime")), **row))
        else:
            for p in people:
                for s in p.get("RoundStats") or []:
                    row = {col: _int(s.get(key)) for col, key in V1.items()}
                    if _int(s.get("Round")) and (row["total_str_att"] or row["ground_sec"] or row["standing_sec"]):
                        bout["rounds"].append(dict(fighter=str(p["Id"]), round=int(s["Round"]), **row))
        if bout["rounds"]:
            out.append(bout)
    if not v2:  # a column an event never filled in wasn't recorded there
        for col in SOMETIMES:
            if not any(r[col] for b in out for r in b["rounds"]):
                for b in out:
                    for r in b["rounds"]:
                        r[col] = None
    return out
