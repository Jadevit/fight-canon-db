"""Parse Sherdog pages: a fighter's page (bio + pro fight history) and the fighter sitemap.

Sherdog's IDs are the number at the end of each URL: /fighter/Name-12345, /events/Name-678.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_FIGHTER_RE = re.compile(r'href="/fighter/[^"]*?-(\d+)"[^>]*>(.*?)</a>', re.S)
_EVENT_RE = re.compile(r'href="/events/[^"]*?-(\d+)"[^>]*>(.*?)</a>', re.S)
_SUB_RE = re.compile(r'<span class="sub_line">(.*?)</span>', re.S)
RESULTS = {"win": "W", "loss": "L", "draw": "D", "nc": "NC"}


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def parse_date(s: str) -> str | None:
    """'Apr / 16 / 2026' -> '2026-04-16'."""
    try:
        return datetime.strptime(re.sub(r"\s", "", s), "%b/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _section(page: str, title: str) -> str:
    """The table under a 'FIGHT HISTORY - <title>' heading, or ''."""
    i = page.find(f"FIGHT HISTORY - {title}<")  # PRO, not PRO EXHIBITION
    if i < 0:
        return ""
    j = page.find("</table>", i)
    return page[i:j]


def parse_fights(table: str) -> list[dict]:
    out = []
    for row in _ROW_RE.findall(table):
        tds = _TD_RE.findall(row)
        if len(tds) != 6 or "final_result" not in tds[0]:
            continue
        opp, ev = _FIGHTER_RE.search(tds[1]), _EVENT_RE.search(tds[2])
        sub = _SUB_RE.search(tds[2])
        method = re.search(r"<b>(.*?)</b>", tds[3], re.S)
        ref = _SUB_RE.search(tds[3])
        out.append(dict(
            result=RESULTS.get(text(tds[0]).lower()),
            opponent_id=opp.group(1) if opp else None,
            opponent=text(opp.group(2)) if opp else text(tds[1]),
            event_id=ev.group(1) if ev else None,
            event=text(ev.group(2)) if ev else "",
            date=parse_date(text(sub.group(1))) if sub else None,
            title='itemprop="award"' in tds[2],
            method=text(method.group(1)) if method else "",
            referee=text(ref.group(1)) if ref else "",
            round=text(tds[4]),
            time=text(tds[5]),
        ))
    return out


def parse_fighter(page: str) -> dict:
    """{name, nickname, nationality, dob, height, weight, association, fights, exhibition,
    amateur}: `fights` is the pro history; the other two are those sections (upcoming fights
    are left out)."""
    name = re.search(r'<span class="fn">(.*?)</span>', page, re.S)
    nick = re.search(r'<span class="nickname">(.*?)</span>', page, re.S)
    nat = re.search(r'itemprop="nationality">(.*?)<', page, re.S)
    dob = re.search(r'itemprop="birthDate"[^>]*>(.*?)<', page, re.S)
    height = re.search(r'itemprop="height">(.*?)<', page, re.S)
    weight = re.search(r'itemprop="weight">(.*?)<', page, re.S)
    assoc = re.search(r'class="association".*?itemprop="name">(.*?)<', page, re.S)
    return dict(
        name=text(name.group(1)) if name else "",
        nickname=text(nick.group(1)).strip(' "') if nick else "",
        nationality=text(nat.group(1)) if nat else "",
        dob=text(dob.group(1)) if dob else "",
        height=text(height.group(1)) if height else "",
        weight=text(weight.group(1)) if weight else "",
        association=text(assoc.group(1)) if assoc else "",
        fights=parse_fights(_section(page, "PRO")),
        exhibition=parse_fights(_section(page, "PRO EXHIBITION")),
        amateur=parse_fights(_section(page, "AMATEUR")),
    )


def bio(fighter: dict) -> dict:
    """A parsed fighter page's bio in the database's units: {nickname, dob, height_in,
    weight_lbs, nationality}, None where the page has nothing."""
    try:
        dob = datetime.strptime(fighter["dob"], "%b %d, %Y").date().isoformat()
    except ValueError:
        dob = None
    h = re.fullmatch(r"(\d)'(\d+)\"", fighter["height"])
    w = re.fullmatch(r"(\d+) lbs", fighter["weight"])
    return dict(nickname=fighter["nickname"] or None, dob=dob,
                height_in=int(h.group(1)) * 12 + int(h.group(2)) if h else None,
                weight_lbs=int(w.group(1)) if w else None, nationality=fighter["nationality"] or None)


def parse_event(page: str) -> dict:
    """{name, date, org_id, org, location} from an event page."""
    name = re.search(r'<h1><span itemprop="name">(.*?)</span>', page, re.S)
    day = re.search(r'class="info">\s*<span><meta itemprop="startDate" content="(\d{4}-\d\d-\d\d)', page)
    org = re.search(r'class="organization".*?href=\'/organizations/[^\']*?-(\d+)\'>'
                    r'<span itemprop="name">(.*?)</span>', page, re.S)
    loc = re.search(r'<span itemprop="location">(.*?)</span>', page, re.S)
    return dict(name=text(name.group(1)) if name else "", date=day.group(1) if day else None,
                org_id=org.group(1) if org else None, org=text(org.group(2)) if org else "",
                location=text(loc.group(1)) if loc else "")


def _side(block: str) -> dict:
    links = [(i, text(n)) for i, n in _FIGHTER_RE.findall(block)]  # the first may wrap a photo
    who = next((x for x in links if x[1]), links[0] if links else None)
    res = re.search(r'<span class="final_result[^"]*">(.*?)</span>', block, re.S)
    return dict(sid=who[0] if who else None, name=who[1] if who else "",
                result=RESULTS.get(text(res.group(1)).lower()) if res else None)


def _int(s: str) -> int | None:
    return int(s) if s.isdigit() else None


def parse_card(page: str) -> list[dict]:
    """Every bout on an event page, main event first: [{match, sides: [{sid, name, result}] x2,
    weight_class, title, method, referee, round, time}]."""
    out = []
    main = re.search(r'<div class="fight_card">(.*?)<table class="fight_card_resume">(.*?)</table>',
                     page, re.S)
    if main:
        card, resume = main.groups()
        cells = {text(k).lower(): text(v) for k, v in re.findall(r"<em>(.*?)</em><br />(.*?)</td>", resume, re.S)}
        wc = re.search(r'<span class="weight_class">(.*?)</span>', card, re.S)
        out.append(dict(
            match=_int(cells.get("match", "")),
            sides=[_side(b) for b in re.findall(r'<div class="fighter (?:left|right)_side"(.*?)</div>', card, re.S)],
            weight_class=text(wc.group(1)) if wc else "", title='class="title_fight"' in card,
            method=cells.get("method", ""), referee=cells.get("referee", ""),
            round=cells.get("round", ""), time=cells.get("time", "")))
    for row in re.findall(r'<tr itemprop="subEvent".*?</tr>', page, re.S):
        tds = _TD_RE.findall(row)
        if len(tds) != 7:
            continue
        wc = re.search(r'<span class="weight_class">(.*?)</span>', tds[2], re.S)
        method = re.search(r"<b>(.*?)</b>", tds[4], re.S)
        ref = _SUB_RE.search(tds[4])
        out.append(dict(
            match=_int(text(tds[0])), sides=[_side(tds[1]), _side(tds[3])],
            weight_class=text(wc.group(1)) if wc else "", title='class="title_fight"' in tds[2],
            method=text(method.group(1)) if method else "", referee=text(ref.group(1)) if ref else "",
            round=text(tds[5]), time=text(tds[6])))
    return out


def parse_org_events(page: str) -> tuple[list[tuple[str, str]], bool]:
    """([(event id, date)], whether an older page follows) from an organization's events page."""
    rows = re.findall(r'<meta itemprop="startDate" content="(\d{4}-\d\d-\d\d)[^"]*">.*?'
                      r'href="/events/[^"]*?-(\d+)"', page, re.S)
    return [(eid, day) for day, eid in rows], "Older Events" in page


def parse_sitemap(xml: str) -> list[tuple[str, str]]:
    """[(sherdog_id, name from the URL slug)] from a fighter sitemap."""
    return [(sid, slug.replace("-", " "))
            for slug, sid in re.findall(r"/fighter/([^<]*?)-(\d+)</loc>", xml)]
