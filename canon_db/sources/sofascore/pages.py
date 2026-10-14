"""Parse what Sofascore's API serves: a season's bouts, and a bout's stats.

A bout's stats come as periods ("ALL", "1ST", "2ND", ...), each a list of named items with
a value for each side (home, away). Sofascore counts them itself. Against UFC Stats:
"Strikes" is total strikes thrown and "Significant Strikes" significant strikes landed (a
little under UFC Stats' count); knockdowns, takedowns and submission attempts mostly agree.
Control time is split into clinch control and ground control ("Ground Clinch Control").
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# round_stats column -> Sofascore item. Every other round_stats column stays NULL.
ROUND = {
    "knockdowns": "Knockdowns",
    "sig_str_land": "Significant Strikes",
    "total_str_att": "Strikes",
    "td_land": "Takedowns Succeeded",
    "td_att": "Takedowns Attempted",
    "sub_att": "Submissions Attempted",
    "head_land": "Significant Strikes Head",
    "body_land": "Significant Strikes Body",
    "leg_land": "Significant Strikes Legs",
}
# sofascore_round_stats column -> Sofascore item. Its split of significant strikes by position
# doesn't add up to the total the way UFC Stats' does (the parts overlap, unevenly), so it
# stays here rather than in round_stats' dist/clinch/ground columns.
EXTRA = {
    "sig_distance_land": "Significant Strikes Distance",
    "sig_clinch_land": "Significant Strikes Clinch",
    "sig_ground_land": "Significant Strikes Ground",
    "distance_sec": "Distance Seconds",
    "clinch_sec": "Clinch Seconds",
    "ground_sec": "Ground Seconds",
    "clinch_ctrl_sec": "Clinch Control Seconds",
    "ground_ctrl_sec": "Ground Clinch Control Seconds",
    "transitions": "Transitions Succeeded",
}
ROUND_COLS = (*ROUND, "ctrl_sec")
EXTRA_COLS = tuple(EXTRA)
PERIOD_RE = re.compile(r"^(\d+)(ST|ND|RD|TH)$")


def bouts(season: dict) -> list[dict]:
    """The finished bouts of a season's events file that may be MMA: [{id, date, winner,
    fighters: [{slug, name}] x2}] (home first, as the stats' sides are; `winner` is 0 for
    home, 1 for away, None for a draw or no result). Bouts marked as another
    discipline (ONE's Muay Thai, kickboxing, grappling) are left out; some cards mark none,
    and those bouts stay in, since only an MMA bout can match one of our fights."""
    out = []
    for e in season.get("events") or []:
        if e.get("fightDiscipline") not in (None, "mma") or (e.get("status") or {}).get("type") != "finished":
            continue
        if not e.get("startTimestamp"):
            continue
        day = datetime.fromtimestamp(e["startTimestamp"], timezone.utc).date().isoformat()
        out.append(dict(id=e["id"], date=day, winner={1: 0, 2: 1}.get(e.get("winnerCode")), fighters=[
            dict(slug=str(e[k]["id"]), name=e[k]["name"]) for k in ("homeTeam", "awayTeam")]))
    return out


def _value(item: dict, side: int):
    v = item.get("homeValue" if side == 0 else "awayValue", item.get("home" if side == 0 else "away"))
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def rounds(stats: dict) -> list[dict]:
    """Per-round rows of a bout's stats: [{side (0 home, 1 away), round, *ROUND_COLS, *EXTRA_COLS}].
    ctrl_sec is clinch plus ground control, as UFC Stats' control time counts both. A bout
    whose totals are all zero wasn't counted (Sofascore lists such bouts): it gives nothing."""
    periods = {p.get("period"): {it["name"]: it for g in p.get("groups") or [] for it in g.get("statisticsItems") or []}
               for p in stats.get("statistics") or []}
    total = periods.get("ALL") or {}
    if not any(_value(it, s) for it in total.values() for s in (0, 1)):
        return []
    out = []
    for period, items in periods.items():
        m = PERIOD_RE.match(period or "")
        if not m:
            continue
        for side in (0, 1):
            row = dict(side=side, round=int(m.group(1)))
            for col, name in (*ROUND.items(), *EXTRA.items()):
                row[col] = _value(items[name], side) if name in items else None
            parts = (row["clinch_ctrl_sec"], row["ground_ctrl_sec"])
            row["ctrl_sec"] = None if None in parts else sum(parts)
            out.append(row)
    return sorted(out, key=lambda r: (r["round"], r["side"]))
