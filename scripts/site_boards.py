"""The site's divisions and leaderboards: what each board counts and how, for the build
(site_json.py) and, through meta.json, for the page, which builds filtered boards from them.
"""

from __future__ import annotations

# The divisions a bout title can name, the first that matches wins. Keep in step with
# division() in site/js/duck.js (made from this list, in meta.json).
DIVISIONS = ["Women's Strawweight", "Women's Flyweight", "Women's Bantamweight",
             "Women's Featherweight", "Atomweight", "Strawweight", "Flyweight", "Bantamweight", "Featherweight",
             "Lightweight", "Welterweight", "Middleweight", "Light Heavyweight", "Super Heavyweight", "Heavyweight",
             "Catch Weight", "Open Weight"]

# Leaderboards: which fights each board counts (the stats table and source; results count
# every fight), its stats as SQL over each fighter's summed columns (results: n, w, fin, ko,
# subw; stats boards also the STATS columns), and what the site says about it. meta.json
# carries these to the page, which builds filtered boards from them (leaderQuery in site/js/leaders.js).
LANDED = {
    "sig":    dict(label="Sig. strikes landed", expr="sl"),
    "total":  dict(label="Total strikes landed", expr="tsl"),
    "td":     dict(label="Takedowns landed", expr="tl"),
    "subatt": dict(label="Submission attempts", expr="suba"),
}
KD = dict(label="Knockdowns", expr="kd")
TDACC = dict(label="Takedown accuracy (min. 10 attempted)", expr="1.0 * tl / ta", where="ta >= 10", fmt="pct")
CTRL = dict(label="Control time", expr="ctrl", fmt="time")
FULL = {
    "kd": KD, "sig": LANDED["sig"],
    "sigacc": dict(label="Sig. strike accuracy (min. 100 attempted)", expr="1.0 * sl / sa", where="sa >= 100",
                   fmt="pct"),
    "total": LANDED["total"], "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"], "ctrl": CTRL,
}
BOARDS = {
    "results": dict(
        label="Results (all fights)", source=None,
        stats={"wins": dict(label="Wins", expr="w"),
               "fin": dict(label="Finishes (KO/TKO + submission wins)", expr="fin"),
               "ko": dict(label="KO/TKO wins", expr="ko"),
               "subw": dict(label="Submission wins", expr="subw"),
               "fights": dict(label="Fights", expr="n")},
        note="Counts pro fights in this database, which doesn't have every fighter's whole career."),
    "ufc": dict(label="UFC Stats", source=("round_stats", "ufc"), stats=FULL,
                note="Fights counted by UFC Stats: the UFC, and the other promotions it covers."),
    "pfl": dict(label="PFL (from Dec 2025)", source=("round_stats", "pfl"), stats=FULL,
                note="PFL's own stats, from December 2025 on."),
    "smartcage": dict(
        label="PFL SmartCage (2018–2025)", source=("smartcage_round_stats", "pfl"),
        stats={"kd": KD, "total": LANDED["total"],
               "totacc": dict(label="Strike accuracy (min. 100 thrown)", expr="1.0 * tsl / tsa",
                              where="tsa >= 100", fmt="pct"),
               "arm": dict(label="Arm strikes landed", expr="arm"),
               "leg": dict(label="Leg strikes landed", expr="leg"),
               "ground": dict(label="Ground strikes landed", expr="grd"),
               "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"]},
        note="PFL's older system, 2018 to November 2025. It counts all strikes, not significant ones."),
    "oktagon": dict(label="Oktagon", source=("round_stats", "oktagon"), stats=LANDED,
                    note="Oktagon's own stats. Only what landed is counted, so there are no accuracy, "
                         "knockdown or control boards."),
    "sofascore": dict(
        label="Sofascore", source=("round_stats", "sofascore"),
        stats={"kd": KD, "sig": LANDED["sig"], "thrown": dict(label="Total strikes thrown", expr="tsa"),
               "td": LANDED["td"], "tdacc": TDACC, "subatt": LANDED["subatt"], "ctrl": CTRL},
        note="Sofascore's count for promotions with no official stats (ONE, RIZIN, KSW, ACA, BRAVE, Cage "
             "Warriors and others), and for Oktagon. Not comparable with the other boards."),
}
SUMS = {  # leaderboard column -> column of the stats table
    "round_stats": dict(kd="knockdowns", sl="sig_str_land", sa="sig_str_att", tsl="total_str_land",
                        tsa="total_str_att", tl="td_land", ta="td_att", suba="sub_att", ctrl="ctrl_sec"),
    "smartcage_round_stats": dict(kd="knockdowns", tsl="total_str_land", tsa="total_str_att", arm="arm_land",
                                  leg="leg_land", grd="ground_land", tl="td_land", ta="td_att", suba="sub_att"),
}
STATS = ["kd", "sl", "sa", "tsl", "tsa", "tl", "ta", "suba", "ctrl", "arm", "leg", "grd"]


def sql_list(items: list[str]) -> str:
    return ", ".join("'" + i.replace("'", "''") + "'" for i in items)


def division_macro() -> str:
    """SQL: division(bout title), the first of DIVISIONS it names, else 'Other' (a CASE, which
    every DuckDB version takes; site/js/duck.js builds the same one)."""
    t = "regexp_replace(coalesce(wc, ''), '(?i)catchweight', 'Catch Weight')"
    whens = " ".join(f"WHEN contains({t}, {sql_list([d])}) THEN {sql_list([d])}" for d in DIVISIONS)
    return f"CREATE MACRO division(wc) AS CASE {whens} ELSE 'Other' END"


def board_totals(b: dict) -> str:
    """SQL: a board's totals per fighter and division code, and per fighter over all divisions
    (division 0), over every promotion and year."""
    # ILIKE: Sherdog writes some methods in lower case ("submission (armbar)").
    won, ko, sub = "p.result = 'W'", "x.method ILIKE '%KO%'", "x.method ILIKE '%Submission%'"
    cols = (f"COUNT(*) n, count_if({won}) w, count_if({won} AND ({ko} OR {sub})) fin, "
            f"count_if({won} AND {ko}) ko, count_if({won} AND {sub}) subw")
    source = "fight_participants p"
    if b["source"]:  # everyone in each fight this source counted, with their totals if they have rows
        table, src = b["source"]  # (a fighter with nothing in any round can have none)
        sums = SUMS[table]
        source = (f"fight_participants p JOIN (SELECT DISTINCT fight_id FROM {table} WHERE source = '{src}') c "
                  "ON c.fight_id = p.fight_id "
                  f"LEFT JOIN (SELECT fight_id, fighter_id, {', '.join(f'SUM({c}) {k}' for k, c in sums.items())} "
                  f"FROM {table} WHERE source = '{src}' GROUP BY ALL) r "
                  "ON r.fight_id = p.fight_id AND r.fighter_id = p.fighter_id")
        cols += ", " + ", ".join(f"SUM(r.{k}) {k}" if k in sums else f"NULL::BIGINT {k}" for k in STATS)
    return f"""SELECT coalesce(d.code, 0) division, p.fighter_id, {cols}
               FROM {source} JOIN fights x ON x.fight_id = p.fight_id JOIN events e ON e.event_id = x.event_id
               JOIN site_divisions d ON d.division = division(x.weight_class)
               WHERE e.date IS NOT NULL
               GROUP BY GROUPING SETS ((d.code, p.fighter_id), (p.fighter_id))"""
