"""Parse what Oktagon serves: a match's log from its stats host (every strike, takedown and
submission attempt, each with its round and second), and a fight card from its own API."""

from __future__ import annotations

from collections import Counter

# round_stats columns Oktagon's log can fill; the rest stay NULL (not recorded).
COLS = ("sig_str_land", "total_str_land", "head_land", "body_land", "leg_land", "td_land", "sub_att")
TARGETS = {"head": "head_land", "body": "body_land", "legs": "leg_land"}


def fighters(match: dict) -> list[dict]:
    """[{id, name, dob}] for the two fighters of a match (or of a tournament's match entry)."""
    out = []
    for key in ("fighter1", "fighter2"):
        f = match.get(key) or {}
        out.append(dict(id=str(f.get("id")), name=f"{f.get('firstname') or ''} {f.get('lastname') or ''}".strip(),
                        dob=(f.get("birthDate") or "")[:10] or None))
    return out


def rounds(match: dict) -> list[dict]:
    """Per-round totals of a match's log: [{fighter (its id), round, *COLS}], a row for each
    fighter in every round up to the last one with anything logged (the match's own round
    count is sometimes wrong). Only landed strikes count: misses are logged in some fights
    and not in others. head/body/leg are significant strikes, as in UFC Stats. A match with
    no strikes logged gives nothing."""
    hits = match.get("hits") or []
    if not hits:
        return []
    count: Counter = Counter()
    last = 0
    for h in hits:
        rnd, who = h.get("matchRound"), str((h.get("attacker") or {}).get("id"))
        if not rnd:
            continue
        last = max(last, rnd)
        if h.get("result") != "landed":
            continue
        count[who, rnd, "total_str_land"] += 1
        if h.get("type") == "significant":
            count[who, rnd, "sig_str_land"] += 1
            if h.get("target") in TARGETS:
                count[who, rnd, TARGETS[h["target"]]] += 1
    for key, col, results in (("takedowns", "td_land", ("completed",)),
                              ("submissionAttempts", "sub_att", ("attempted", "completed", None))):
        for x in match.get(key) or []:
            rnd = x.get("matchRound")
            if rnd and x.get("result") in results:
                last = max(last, rnd)
                count[str((x.get("attacker") or {}).get("id")), rnd, col] += 1
    return [dict(fighter=f["id"], round=rnd, **{c: count[f["id"], rnd, c] for c in COLS})
            for f in fighters(match) for rnd in range(1, last + 1)]


MEN_FROM_LBS = 145  # some women's bouts are entered as MALE; none is this heavy


def card_fights(card: list[dict]) -> list[dict]:
    """The bouts of a fight card from Oktagon's API: [{stats_id, legacy_id, sex}]; `stats_id`
    is the match's id on the stats host, `legacy_id` the older id it may be listed under.
    `sex` is 'F' for a FEMALE bout, 'M' for a MALE one at MEN_FROM_LBS or more, else None."""
    out = []
    for group in card or []:
        for f in group.get("fights") or []:
            wc = f.get("weightClass") or {}
            sex = "F" if wc.get("gender") == "FEMALE" else (
                "M" if wc.get("gender") == "MALE" and (wc.get("weightLbs") or 0) >= MEN_FROM_LBS else None)
            out.append(dict(stats_id=(f.get("metadata") or {}).get("esportsId"), legacy_id=f.get("legacyId"), sex=sex))
    return out
