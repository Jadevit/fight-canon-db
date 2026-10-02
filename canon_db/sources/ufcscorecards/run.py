"""Read official UFC scorecard images into judge_scores (not a registered source: the
cards can't be fetched automatically, so this runs by hand on a folder of images).

    python -m canon_db.sources.ufcscorecards.run templates data/raw/ufcscorecards/v2
    python -m canon_db.sources.ufcscorecards.run read data/raw/ufcscorecards/v2 --out results.jsonl
    python -m canon_db.sources.ufcscorecards.run load results.jsonl

`templates` learns the digit glyphs from a random sample of cards that pass every check
with the bootstrap readers. `read` reads every card, matches it to its fight, checks it,
and writes one JSON line per card. `load` writes the cards that passed into judge_scores,
replacing the rows of every fight it has a card for.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from canon_db import db
from canon_db.db import DEFAULT_DB
from canon_db.sources.ufcscorecards import reader
from canon_db.sources.ufcscorecards.match import Matcher, check, norm, sim


def cards_in(folder):
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))


def build_templates(folder, matcher, n=40, seed=7):
    reader.TEMPLATES = None
    files = cards_in(folder)
    random.Random(seed).shuffle(files)
    samples = {d: [] for d in range(10)}

    def add(crop, value):
        gs = reader.glyphs(crop)
        digits = [int(c) for c in str(value)]
        if len(gs) == len(digits):
            for g, d in zip(gs, digits):
                samples[d].append(g)

    used = 0
    for f in files[:n]:
        card = reader.read_card(f, keep_crops=True)
        if "error" in card:
            continue
        for j in card["judges"]:  # printed round numbers are known on every card
            for i, c in enumerate(j["_crops"]["round_no"]):
                add(c, i + 1)
        fight, _, err = matcher.match(card)
        if err or check(card, fight)[1]:
            continue
        used += 1
        for j in card["judges"]:
            for r, (rc, bc) in zip(j["rounds"], j["_crops"]["rounds"]):
                if isinstance(r["red"], int):
                    add(rc, r["red"])
                if isinstance(r["blue"], int):
                    add(bc, r["blue"])
            for v, c in zip(j["total"], j["_crops"]["total"]):
                if isinstance(v, int):
                    add(c, v)
    missing = [d for d, v in samples.items() if not v]
    if missing:
        raise SystemExit(f"no samples for digits {missing}; use more cards (--n)")
    np.savez(reader.TEMPLATES_PATH, **{str(d): np.mean(v, axis=0) for d, v in samples.items()})
    print(f"templates from {used} passing cards; glyphs per digit:",
          {d: len(v) for d, v in samples.items()})


def _read_one(path):
    try:
        return reader.read_card(path)
    except Exception as e:  # never let one odd image stop the run
        return {"error": f"crash: {e!r}"}


def read_all(folder, matcher, out_path):
    files = cards_in(folder)
    stats, reasons, by_fight = Counter(), Counter(), {}
    # Reading is the slow part (tesseract), and cards are independent: use every core.
    with open(out_path, "w") as out, ProcessPoolExecutor(initializer=reader.load_templates) as pool:
        for i, (f, card) in enumerate(zip(files, pool.map(_read_one, files, chunksize=4)), 1):
            rec = {"file": f.name}
            if "error" in card:
                rec.update(status="read error", problems=[card["error"]])
            else:
                rec["card"] = card
                fight, red_is_0, err = matcher.match(card)
                if err:
                    rec.update(status="unmatched", problems=[err])
                else:
                    n, problems = check(card, fight)
                    rec.update(fight_id=fight["fight_id"], event=fight["event"], red_is_corner0=red_is_0,
                               kind="decision" if fight["method"].startswith("Decision") else "finish",
                               rounds=n, status="failed" if problems else "passed", problems=problems)
                    by_fight.setdefault(fight["fight_id"], []).append(f.name)
            stats[rec["status"]] += 1
            if rec["status"] == "passed":
                stats["passed " + rec["kind"]] += 1
            for p in rec.get("problems", []):
                reasons[re.sub(r"\d", "#", p.split(":")[-1])[:45]] += 1
            out.write(json.dumps(rec) + "\n")
            if i % 250 == 0 or i == len(files):
                print(f"{i}/{len(files)}", dict(stats), flush=True)
    dups = {k: v for k, v in by_fight.items() if len(v) > 1}
    print("cards sharing a fight:", len(dups), list(dups.items())[:10])
    print("failure types:", reasons.most_common(15))


# --- writing judge_scores -----------------------------------------------------------------

_JUDGE_RE = re.compile(r"([A-Za-z][A-Za-z'’. -]*?)\s+\d+\s*-\s*\d+\.")


def judges_in(details):
    """Judge names in UFC Stats' decision details, in order."""
    return [n.strip() for n in _JUDGE_RE.findall(details or "")]


class Judges:
    """Snap an OCR'd judge name to how UFC Stats spells it: first among the judges listed
    for that fight, then among every judge listed at least `min_seen` times."""

    def __init__(self, conn, min_seen=3):
        seen = Counter(n for (d,) in conn.execute("SELECT details FROM fights") for n in judges_in(d))
        self.known = [n for n, k in seen.items() if k >= min_seen and len(n.split()) <= 4]

    def name(self, ocr, fight_judges):
        for pool, cutoff in ((fight_judges, 0.6), (self.known, 0.8)):
            best = max(pool, key=lambda n: sim(ocr, n), default=None)
            if best and sim(ocr, best) >= cutoff:
                return best
        return norm(ocr).title() or None


def card_rows(rec, conn, judges):
    """judge_scores rows for one passing card, or None if a judge's name is unreadable."""
    fid = rec["fight_id"]
    corners = dict(conn.execute(
        "SELECT corner, fighter_id FROM fight_participants WHERE fight_id = ?", (fid,)))
    details = conn.execute("SELECT details FROM fights WHERE fight_id = ?", (fid,)).fetchone()[0]
    red, blue = (corners[0], corners[1]) if rec["red_is_corner0"] else (corners[1], corners[0])
    rows = set()
    for j in rec["card"]["judges"]:
        name = judges.name(j["judge"], judges_in(details))
        if name is None:
            return None
        for rnd, r in enumerate(j["rounds"][:rec["rounds"]], 1):
            for fighter, score, ded in ((red, r["red"], r["red_ded"]), (blue, r["blue"], r["blue_ded"])):
                rows.add((fid, fighter, rnd, name, score, ded if isinstance(ded, int) else 0))
    return rows


def load(results, conn):
    """Write passing cards to judge_scores, replacing those fights' rows. A fight with two
    cards that disagree is left out."""
    judges = Judges(conn)
    by_fight, skipped = {}, Counter()
    for line in open(results):
        rec = json.loads(line)
        if rec["status"] != "passed":
            skipped[rec["status"]] += 1
            continue
        rows = card_rows(rec, conn, judges)
        if rows is None:
            skipped["unreadable judge name"] += 1
            continue
        fid = rec["fight_id"]
        by_fight[fid] = rows if by_fight.get(fid, rows) == rows else None
    conflicts = sorted(fid for fid, rows in by_fight.items() if rows is None)
    with conn:
        for fid, rows in by_fight.items():
            conn.execute("DELETE FROM judge_scores WHERE fight_id = ? AND source = 'ufc.com'", (fid,))
            conn.executemany("INSERT INTO judge_scores (fight_id, fighter_id, round, judge, score, "
                             "deduction, source) VALUES (?,?,?,?,?,?,'ufc.com')", sorted(rows or ()))
    print(f"loaded {len(by_fight) - len(conflicts)} fights; duplicate cards that disagree: "
          f"{conflicts}; skipped: {dict(skipped)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["templates", "read", "load"])
    ap.add_argument("folder", help="card images (templates, read) or read's output (load)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", default="scorecards.jsonl")
    ap.add_argument("--n", type=int, default=40, help="cards to learn templates from")
    args = ap.parse_args()
    if args.command == "load":
        with db.updating(args.db) as (conn, _):
            load(args.folder, conn)
        return
    matcher = Matcher(sqlite3.connect(args.db))
    if args.command == "templates":
        build_templates(args.folder, matcher, args.n)
    else:
        read_all(args.folder, matcher, args.out)


if __name__ == "__main__":
    main()
