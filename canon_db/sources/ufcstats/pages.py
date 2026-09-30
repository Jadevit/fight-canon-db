"""Parse raw UFC Stats pages into plain Python records.

One function per page type — events list, event, fight, fighter. Each takes the
page HTML and returns plain dicts of *strings as shown on the page*; turning those
strings into numbers/dates/NULLs is `fields.py`'s job, so the two stay testable
separately.

UFC Stats pages are server-rendered with stable BEM class names, so targeted
regexes over those classes are enough; no HTML library needed. Pure standard
library.
"""

from __future__ import annotations

import html as _html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_FIGHTER_LINK_RE = re.compile(r"fighter-details/([0-9a-f]{16})")
_FIGHT_LINK_RE = re.compile(r"fight-details/([0-9a-f]{16})")


def text(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _unique(ids) -> list[str]:
    return list(dict.fromkeys(ids))


# --- events list (statistics/events/completed?page=all) --------------------------

_EVENTS_ROW_RE = re.compile(
    r'event-details/([0-9a-f]{16})"[^>]*>(.*?)</a>\s*'
    r'<span class="b-statistics__date">(.*?)</span>.*?</td>\s*<td[^>]*>(.*?)</td>', re.S)


def parse_events_list(html: str) -> list[dict]:
    """Every event row, newest first: {event_id, name, date, location}.

    Note the "completed" list also carries the next upcoming card at the top; callers
    filter by date."""
    return [dict(event_id=eid, name=text(name), date=text(date), location=text(loc))
            for eid, name, date, loc in _EVENTS_ROW_RE.findall(html)]


# --- event page -------------------------------------------------------------------

def _info_item(html: str, label: str) -> str:
    m = re.search(rf"{label}:\s*</i>(.*?)</li>", html, re.S)
    return text(m.group(1)) if m else ""


def parse_event(html: str) -> dict:
    """{name, date, location, fight_ids (card order), fighter_ids}."""
    m = re.search(r'b-content__title-highlight">(.*?)</span>', html, re.S)
    body = html[html.find("b-fight-details__table-body"):]
    return dict(
        name=text(m.group(1)) if m else "",
        date=_info_item(html, "Date"),
        location=_info_item(html, "Location"),
        fight_ids=_unique(_FIGHT_LINK_RE.findall(body)),
        fighter_ids=_unique(_FIGHTER_LINK_RE.findall(body)),
    )


# --- fight page -------------------------------------------------------------------

_PERSON_RE = re.compile(
    r'b-fight-details__person-status[^"]*">(.*?)</i>.*?'
    r"fighter-details/([0-9a-f]{16})[^>]*>(.*?)</a>", re.S)
_LABEL_RE = re.compile(
    r'b-fight-details__label">\s*(Method|Round|Time|Time format|Referee):\s*</i>(.*?)</i>',
    re.S)
_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
_ROUND_OR_ROW_RE = re.compile(
    r'colspan="\d+">\s*Round (\d+)\s*<|<tr class="b-fight-details__table-row">(.*?)</tr>', re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

# Column order of the two per-round tables (after the fighter column).
TOTALS_COLS = ["kd", "sig", "sig_pct", "total", "td", "td_pct", "sub", "rev", "ctrl"]
SIG_COLS = ["sig", "sig_pct", "head", "body", "leg", "distance", "clinch", "ground"]


def _per_round(table: str, cols: list[str]) -> dict[tuple[int, str], dict]:
    """{(round, fighter_id): {col: cell text}} from one per-round table."""
    out: dict[tuple[int, str], dict] = {}
    rnd = None
    for m in _ROUND_OR_ROW_RE.finditer(table):
        if m.group(1):
            rnd = int(m.group(1))
            continue
        if rnd is None:
            continue
        tds = [[text(p) for p in _P_RE.findall(td)] for td in _TD_RE.findall(m.group(2))]
        if not tds:
            continue
        fighter_ids = _FIGHTER_LINK_RE.findall(_TD_RE.findall(m.group(2))[0])
        for corner, fid in enumerate(fighter_ids):
            out[(rnd, fid)] = {c: (cells[corner] if corner < len(cells) else "")
                               for c, cells in zip(cols, tds[1:])}
    return out


def parse_fight(html: str) -> dict:
    """{event_id, fighters: [{fighter_id, name, status}] in page order, bout, belt, method,
    round, time, time_format, referee, details, rounds: {(round, fighter_id): cells}}.

    `status` is the page's W/L/D/NC flag, blank for a bout with no result yet.
    `rounds` merges the two per-round tables (totals + significant-strike breakdown);
    it is empty for the few early fights with no recorded stats."""
    start = html.find("b-fight-details__persons")
    head = html[start:html.find("b-fight-details__fight", start + 30)]
    fighters = [dict(fighter_id=fid, name=text(name), status=text(status))
                for status, fid, name in _PERSON_RE.findall(head)]

    m = re.search(r'b-fight-details__fight-title">(.*?)</i>', html, re.S)
    bout_html = m.group(1) if m else ""
    labels = {k: text(v) for k, v in _LABEL_RE.findall(html)}
    m = re.search(r"Details:\s*</i>\s*</i>(.*?)</p>", html, re.S)
    details = text(m.group(1)) if m else ""

    rounds: dict[tuple[int, str], dict] = {}
    for table in _TABLE_RE.findall(html):
        if "table-head_rnd" not in table:
            continue  # fight-total tables: derivable from rounds, not stored
        head_text = text(table[:table.find("</thead>")])
        cols = SIG_COLS if "Head" in head_text else TOTALS_COLS
        for key, cells in _per_round(table, cols).items():
            rounds.setdefault(key, {}).update(cells)

    ev = re.search(r"event-details/([0-9a-f]{16})", html)
    return dict(
        event_id=ev.group(1) if ev else None,
        fighters=fighters,
        bout=text(bout_html),
        belt="belt.png" in bout_html,
        method=labels.get("Method", ""),
        round=labels.get("Round", ""),
        time=labels.get("Time", ""),
        time_format=labels.get("Time format", ""),
        referee=labels.get("Referee", ""),
        details=details,
        rounds=rounds,
    )


# --- fighter page -----------------------------------------------------------------

def parse_fighter(html: str) -> dict:
    """{name, record, nickname, height, weight, reach, stance, dob, fight_ids} as shown on
    the page. `fight_ids` covers every fight UFC Stats has for the fighter, including events
    missing from its events list (e.g. PRIDE)."""
    m = re.search(r'b-content__title-highlight">(.*?)</span>', html, re.S)
    nick = re.search(r'b-content__Nickname">(.*?)</p>', html, re.S)
    rec = re.search(r'b-content__title-record">(.*?)</span>', html, re.S)
    return dict(
        fight_ids=_unique(_FIGHT_LINK_RE.findall(html)),
        name=text(m.group(1)) if m else "",
        record=text(rec.group(1)) if rec else "",
        nickname=text(nick.group(1)) if nick else "",
        height=_info_item(html, "Height"),
        weight=_info_item(html, "Weight"),
        reach=_info_item(html, "Reach"),
        stance=_info_item(html, "STANCE"),
        dob=_info_item(html, "DOB"),
    )
