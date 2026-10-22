"""Write the site's browsing pages as JSON, from the site's database (scripts/site_db.py keeps
it): a file per fighter and per event, the event lists, a search index, the leaderboards'
opening views and the landing page's numbers. A page then reads one small file instead of
the database; only the SQL page and filtered leaderboards still query it. The files are the
site's public API too, under /v1/.

    python scripts/site_json.py build <site.db> <out dir>     # writes <out>/v1/
    python scripts/site_json.py upload <out dir>              # to R2: only what changed

Search: names (fighters) and words (events) are split into files by prefix. A prefix with
more than SEARCH_CAP matches gets a file of its top SEARCH_TOP (most fights; newest events)
and the top SEARCH_EXACT of those whose word is the prefix itself (a common word, such as a
country, would otherwise bring tens of thousands), and a file for each prefix a letter longer.
Upload compares each file's hash with the manifest of the last upload (v1-manifest.json in
the bucket) and sends only the changed ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from site_db import STATS, SUMS, words  # noqa: E402

VERSION = "v1"
SEARCH_CAP = 800
SEARCH_TOP = 100
SEARCH_EXACT = 300
CACHE = "public, max-age=300"  # files keep their names, so a daily update shows up within minutes


def key(id_: str) -> str:
    """A file name for an id (ids have colons: sherdog:123)."""
    return id_.replace(":", "~")


def hexname(prefix: str) -> str:
    return prefix.encode().hex()


def shard(id_: str) -> str:
    """Which fx/ file a fight is in: the low byte of its FNV-1a hash (app.js does the same)."""
    h = 0x811C9DC5
    for b in id_.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return f"{h & 0xFF:02x}"


def table(cur: sqlite3.Cursor, rows=None) -> dict:
    """Rows as {cols, rows}: a header and lists, much smaller than an object per row."""
    return {"cols": [d[0] for d in cur.description], "rows": [list(r) for r in (cur if rows is None else rows)]}


class Writer:
    def __init__(self, out: Path):
        self.root = out / VERSION
        if self.root.exists():
            shutil.rmtree(self.root)
        self.n = 0
        self.bytes = 0

    def __call__(self, path: str, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.n += 1
        self.bytes += len(data)


FIGHT_HIST = ["date", "event_id", "event", "promotion", "fight_id", "division", "has_stats", "method", "end_round",
              "end_time", "time_format", "title_fight"]
STAT_SUMS = """SELECT fight_id, fighter_id, source, SUM(knockdowns) kd, SUM(sig_str_land) sl, SUM(sig_str_att) sa,
    SUM(CASE WHEN sig_str_att IS NOT NULL THEN sig_str_land END) sla, SUM(td_land) tl, SUM(td_att) ta,
    SUM(CASE WHEN td_att IS NOT NULL THEN td_land END) tla, SUM(sub_att) sub, SUM(ctrl_sec) ctrl
    FROM round_stats GROUP BY fight_id, fighter_id ORDER BY fight_id, fighter_id"""


def fighters(conn: sqlite3.Connection, w: Writer) -> None:
    """f/<id>.json: the fighter, their fights newest first (from their side), and per fight the
    stat totals of each side (the opponent's for strike defense)."""
    hist = defaultdict(list)
    for r in conn.execute(f"""SELECT {", ".join(FIGHT_HIST)}, a_id, a_name, a_res, b_id, b_name, b_res
                              FROM site_fights ORDER BY date DESC, event_id, card"""):
        base, (a_id, a_name, a_res, b_id, b_name, b_res) = list(r[:len(FIGHT_HIST)]), r[len(FIGHT_HIST):]
        if a_id:
            hist[a_id].append(base + [a_res, b_id, b_name])
        if b_id:
            hist[b_id].append(base + [b_res, a_id, a_name])
    sums = defaultdict(dict)
    for r in conn.execute(STAT_SUMS):
        sums[r[0]][r[1]] = r
    stats = defaultdict(list)
    for fight, sides in sums.items():
        for fid, r in sides.items():
            opp = next((o for o in sides.values() if o[1] != fid), None)
            stats[fid].append([fight, *r[2:], opp[6] if opp else None, opp[5] if opp else None])
    sherdog = defaultdict(list)
    for fid, sid in conn.execute("SELECT fighter_id, source_id FROM fighter_aliases WHERE source = 'sherdog' "
                                 "ORDER BY fighter_id, source_id"):
        sherdog[fid].append(sid)
    cur = conn.execute("""SELECT fighter_id, name, nickname, dob, height_in, reach_in, weight_lbs, stance, nationality,
                                 wins, losses, draws, no_contests FROM fighters ORDER BY fighter_id""")
    cols = [d[0] for d in cur.description]
    for r in cur:
        fid = r[0]
        w(f"f/{key(fid)}.json", {
            "fighter": dict(zip(cols, r)),
            "sherdog": sherdog.get(fid, []),
            "fights": {"cols": FIGHT_HIST + ["result", "opp_id", "opp"], "rows": hist.get(fid, [])},
            "stats": {"cols": ["fight_id", "source", "kd", "sl", "sa", "sla", "tl", "ta", "tla", "sub", "ctrl",
                               "opp_sl", "opp_sa"], "rows": stats.get(fid, [])},
        })


def grouped(conn: sqlite3.Connection, sql: str, by: int = 0) -> tuple[list[str], dict]:
    """Run sql; return its columns without column `by`, and its rows grouped by that column."""
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    out = defaultdict(list)
    for r in cur:
        out[r[by]].append([v for i, v in enumerate(r) if i != by])
    return [c for i, c in enumerate(cols) if i != by], out


def events(conn: sqlite3.Connection, w: Writer) -> None:
    """e/<id>.json: the event and its card, each bout with its round stats, judges' cards and
    odds; fx/<xx>.json: which event each fight is on, for links to a fight alone."""
    fcols, fights = grouped(conn, """SELECT event_id, fight_id, card, division, has_stats, a_id, a_name, a_res,
        b_id, b_name, b_res, weight_class, title_fight, scheduled_rounds, method, end_round, end_time, time_format,
        referee, details, overturned, no_contest, source FROM site_fights ORDER BY event_id, card""")
    rcols, rounds = grouped(conn, "SELECT * FROM round_stats ORDER BY fight_id, round, fighter_id")
    ccols, cage = grouped(conn, "SELECT * FROM smartcage_round_stats ORDER BY fight_id, round, fighter_id")
    scols, scores = grouped(conn, "SELECT fight_id, fighter_id, round, judge, score, deduction FROM judge_scores "
                                  "ORDER BY fight_id, judge, round, fighter_id")
    ocols, odds = grouped(conn, "SELECT fight_id, fighter_id, open, close, close_low, close_high FROM odds "
                                "WHERE book = 'all' ORDER BY fight_id, fighter_id, source")
    aliases = defaultdict(lambda: defaultdict(list))
    for eid, src, sid in conn.execute("SELECT event_id, source, source_id FROM event_aliases "
                                      "WHERE source IN ('sherdog', 'pfl') ORDER BY event_id, source, source_id"):
        aliases[eid][src].append(sid)
    src_at = rcols.index("source")
    shards = defaultdict(dict)
    cur = conn.execute("""SELECT e.event_id, e.name, e.date, e.location, e.promotion, e.source, p.coverage,
                                 k.complete card_complete
                          FROM events e LEFT JOIN promotions p USING (promotion)
                          LEFT JOIN event_cards k USING (event_id) ORDER BY e.event_id""")
    cols = [d[0] for d in cur.description]
    for r in cur:
        eid = r[0]
        card = fights.get(eid, [])
        stats, cards, lines = {}, {}, {}
        for f in card:
            fight = f[0]
            if fight in rounds:
                stats[fight] = {"source": rounds[fight][0][src_at], "cage": False, "cols": rcols, "rows": rounds[fight]}
            elif fight in cage:
                stats[fight] = {"source": "smartcage", "cage": True, "cols": ccols, "rows": cage[fight]}
            if fight in scores:
                cards[fight] = scores[fight]
            if fight in odds:
                lines[fight] = odds[fight]
            shards[shard(fight)][fight] = eid
        w(f"e/{key(eid)}.json", {
            "event": dict(zip(cols, r)), "aliases": aliases.get(eid, {}),
            "fights": {"cols": fcols, "rows": card},
            "stats": stats,
            "scores": {"cols": scols, "fights": cards},
            "odds": {"cols": ocols, "fights": lines},
        })
    for name, m in shards.items():
        w(f"fx/{name}.json", dict(sorted(m.items())))


def event_lists(conn: sqlite3.Connection, w: Writer) -> None:
    """events/y/<year>.json, events/p/<promotion code>.json (listed ones)."""
    cols = "event_id, name, date, location, promotion, n"
    years = [y for (y,) in conn.execute("SELECT DISTINCT substr(date, 1, 4) FROM site_events ORDER BY 1")]
    for y in years:
        w(f"events/y/{y}.json", table(conn.execute(
            f"SELECT {cols} FROM site_events WHERE date >= ? AND date < ? ORDER BY date DESC, event_id",
            (y, str(int(y) + 1)))))
    for code, promo in conn.execute("SELECT code, promotion FROM site_promotions WHERE listed ORDER BY code").fetchall():
        w(f"events/p/{code}.json", table(conn.execute(
            f"SELECT {cols} FROM site_events WHERE promotion = ? ORDER BY date DESC, event_id", (promo,))))


def trie(w: Writer, folder: str, cols: list[str], entries: list[tuple[set[str], list]]) -> None:
    """Search files for entries (their words, the row shown), already in rank order: a file per
    two-letter prefix, split a letter deeper while a prefix has more than SEARCH_CAP matches."""
    def emit(prefix: str, items: list[int]) -> None:
        if len(items) <= SEARCH_CAP or len(prefix) >= 16:
            w(f"{folder}/{hexname(prefix)}.json", {"split": False, "cols": cols, "rows": [entries[i][1] for i in items]})
            return
        exact = [i for i in items if prefix in entries[i][0]][:SEARCH_EXACT]
        keep = sorted(set(items[:SEARCH_TOP]) | set(exact))
        w(f"{folder}/{hexname(prefix)}.json", {"split": True, "cols": cols, "rows": [entries[i][1] for i in keep]})
        children = defaultdict(list)
        for i in items:
            for nxt in {wd[:len(prefix) + 1] for wd in entries[i][0] if wd.startswith(prefix) and len(wd) > len(prefix)}:
                children[nxt].append(i)
        for nxt, its in sorted(children.items()):
            emit(nxt, its)

    top = defaultdict(list)
    for i, (ws, _) in enumerate(entries):
        for p in sorted({wd[:2] for wd in ws if len(wd) >= 2}):
            top[p].append(i)
    for p, items in sorted(top.items()):
        emit(p, items)


def search(conn: sqlite3.Connection, w: Writer) -> None:
    """sf/: fighters by every word of every name they go by, most fights first.
    se/: events by every word of their name and place, newest first."""
    people = {}
    for r in conn.execute("""SELECT fighter_id, search, n, name, nickname, wins, losses, draws, no_contests,
                                    w, l, d, nc FROM site_names ORDER BY n DESC, name, fighter_id"""):
        fid = r[0]
        if fid not in people:
            people[fid] = (set(), [fid, *r[3:9], r[2], *r[9:]])  # the columns below, in order
        people[fid][0].update(words(r[1]))
    entries = [(ws, row + [" ".join(sorted(ws))]) for ws, row in people.values()]
    trie(w, "sf", ["fighter_id", "name", "nickname", "wins", "losses", "draws", "no_contests", "n", "w", "l", "d",
                   "nc", "words"], entries)
    evs = []
    for eid, name, date, loc, promo, n in conn.execute(
            "SELECT event_id, name, date, location, promotion, n FROM site_events ORDER BY date DESC, event_id"):
        ws = words(f"{name} {loc or ''}")
        evs.append((ws, [eid, name, date, loc, promo, n, " ".join(sorted(ws))]))
    trie(w, "se", ["event_id", "name", "date", "location", "promotion", "n", "words"], evs)


def leaders(conn: sqlite3.Connection, w: Writer) -> None:
    """l/<board>/<stat>.json: each board's opening view (all promotions and years, min. 1 fight),
    the top 50 per division code (0 = all)."""
    out = defaultdict(lambda: defaultdict(list))
    for board, stat, div, fid, name, n, val in conn.execute(
            "SELECT board, stat, division, fighter_id, name, n, val FROM site_top ORDER BY board, stat, division, rank"):
        out[(board, stat)][str(div)].append([fid, name, n, val])
    for (board, stat), divs in out.items():
        w(f"l/{board}/{stat}.json", divs)


def meta(conn: sqlite3.Connection, w: Writer) -> None:
    """meta.json: the landing page's numbers, the latest event, and the lists the filters offer."""
    stats = table(conn.execute("SELECT * FROM site_stats"))
    w("meta.json", {
        "stats": dict(zip(stats["cols"], stats["rows"][0])),
        "latest": table(conn.execute("SELECT event_id, name, date, location, promotion, n FROM site_events "
                                     "ORDER BY date DESC, event_id LIMIT 1")),
        "promotions": table(conn.execute("SELECT code, promotion, events FROM site_promotions WHERE listed "
                                         "ORDER BY events DESC, code")),
        "divisions": table(conn.execute("SELECT code, division FROM site_divisions ORDER BY code")),
        "boards": json.loads(conn.execute("SELECT json FROM site_boards").fetchone()[0]),
        "sums": SUMS, "stats_cols": STATS,  # stats boards: column -> its stats table's column
        "top_fighters": table(conn.execute("SELECT * FROM site_top_fighters ORDER BY n DESC, name")),
    })


def build(db: Path, out: Path) -> Writer:
    w = Writer(out)
    conn = sqlite3.connect(db)
    try:
        for step in (meta, leaders, event_lists, search, events, fighters):
            step(conn, w)
    finally:
        conn.close()
    return w


def upload(out: Path, workers: int = 32) -> None:
    """Send the files that changed since the last upload to R2 (R2_BUCKET; AWS_ENDPOINT_URL and
    keys from the environment), delete the ones that are gone, then save the new manifest."""
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
    bucket, manifest = os.environ["R2_BUCKET"], f"{VERSION}-manifest.json"
    root = out / VERSION
    files = {f"{VERSION}/{p.relative_to(root).as_posix()}": p for p in root.rglob("*.json")}
    hashes = {k: hashlib.md5(p.read_bytes()).hexdigest() for k, p in files.items()}
    try:
        old = json.loads(s3.get_object(Bucket=bucket, Key=manifest)["Body"].read())
    except s3.exceptions.NoSuchKey:
        old = {}
    changed = [k for k, h in hashes.items() if old.get(k) != h]
    gone = [k for k in old if k not in hashes]

    def put(k: str) -> None:
        s3.put_object(Bucket=bucket, Key=k, Body=files[k].read_bytes(), ContentType="application/json",
                      CacheControl=CACHE)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(put, changed))
    for i in range(0, len(gone), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in gone[i:i + 1000]], "Quiet": True})
    s3.put_object(Bucket=bucket, Key=manifest, Body=json.dumps(hashes).encode(), ContentType="application/json")
    print(f"{len(files)} files: {len(changed)} uploaded, {len(gone)} deleted")


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    if cmd == "build":
        w = build(Path(rest[0]), Path(rest[1]))
        print(f"{w.n} files, {w.bytes / 1e6:.0f} MB")
    elif cmd == "upload":
        upload(Path(rest[0]))
    else:
        sys.exit(__doc__)
