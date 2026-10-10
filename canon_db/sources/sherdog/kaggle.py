"""Load whole Sherdog cards from Kaggle datasets (one per organization in cards.ORGS, scraped
from Sherdog's event pages, CC0): a one-off base for those organizations' cards, so the
event-page crawl (cards.py) only has to keep them current. Not part of the daily update.

    python -m canon_db.sources.sherdog.kaggle            # download (pinned versions) and load

The datasets are older than the database's own Sherdog fights, so they only fill empty
columns of those (see cards.write). Title fights are left for the event-page crawl.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import zipfile
from pathlib import Path

import requests

from canon_db import db
from canon_db.sources.sherdog import cards
from canon_db.sources.sherdog.load import NAME, State
from canon_db.sources.ufcstats.client import HEADERS

OWNER = "leandroiber"
# dataset -> (pinned version, the date it was published, our label for its new events)
DATASETS: dict[str, tuple[int, str, str]] = {
    "aca-mma-dataset": (6, "2026-05-24", "Absolute Championship Akhmat"),
    "bellator-mma-complete-dataset": (6, "2026-05-24", "Bellator MMA"),
    "cage-warriors-mma-dataset": (8, "2026-05-24", "CWFC"),
    "jungle-fight-complete-dataset": (6, "2026-05-24", "Jungle Fight"),
    "ksw-mma-dataset": (6, "2026-05-24", "Konfrontacja Sztuk Walki"),
    "lfa-mma-dataset": (6, "2026-05-24", "Legacy Fighting Alliance (LFA)"),
    "oktagon-mma-dataset": (6, "2026-05-24", "Oktagon MMA"),
    "pfl-complete-dataset": (7, "2026-05-24", "Professional Fighters League"),
    "rizin-mma-dataset": (6, "2026-05-24", "Rizin Fighting Federation"),
}
DONE = "REALIZADO"  # the event took place (the others: AGENDADO scheduled, CANCELADO cancelled)
RESULTS = {"win": "W", "loss": "L", "draw": "D", "nc": "NC"}

log = logging.getLogger(NAME)


def sid(url: str) -> str | None:
    """'https://www.sherdog.com/fighter/Name-12345' -> '12345'."""
    m = re.search(r"-(\d+)/?$", url or "")
    return m.group(1) if m else None


def download(raw: Path) -> dict[str, list[dict]]:
    """{dataset: rows of its clean_sherdog_*.csv}, each zip saved once under raw/."""
    out = {}
    for name, (version, _, _) in DATASETS.items():
        path = raw / f"{name}-v{version}.zip"
        if not path.exists():
            r = requests.get(f"https://www.kaggle.com/api/v1/datasets/download/{OWNER}/{name}",
                             params={"datasetVersionNumber": version}, headers=HEADERS, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"couldn't download {name} v{version}: HTTP {r.status_code}")
            path.with_suffix(".tmp").write_bytes(r.content)
            path.with_suffix(".tmp").replace(path)
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.startswith("clean_sherdog_"))
            out[name] = list(csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8")))
    return out


def bouts(dataset: str, rows: list[dict]) -> list[dict]:
    """The bouts of events that took place, in cards.write()'s shape."""
    version, published, label = DATASETS[dataset]
    out = []
    for r in rows:
        if r["event_status"] != DONE:
            continue
        b = dict(event=sid(r["event_url"]), event_name=r["event_name"], date=r["event_date"][:10],
                 location=r["event_location"] or None, label=label,
                 via=f"kaggle:{OWNER}/{dataset} v{version}", fetched_at=published,
                 weight_class=r["weight_class"] or None, method=r["method"] or None,
                 referee=r["referee"] or None, round=int(float(r["round_num"])) if r["round_num"] else None,
                 time=re.sub(r"^0(\d:)", r"\1", r["time"]) or None, title=None, sides=[])
        for i in "12":
            dob, cm, kg = r[f"f{i}_birthDate"], r[f"f{i}_height_cm"], r[f"f{i}_weight_kg"]
            b["sides"].append(dict(
                sid=sid(r[f"fighter_{i}_url"]), name=r[f"fighter_{i}"],
                result=RESULTS.get(r[f"fighter_{i}_result"]), nickname=r[f"f{i}_nickname"] or None,
                nationality=r[f"f{i}_nationality"] or None,
                dob=dob if re.fullmatch(r"\d{4}-\d\d-\d\d", dob) else None,
                height_in=round(float(cm) / 2.54) if cm else None,
                weight_lbs=round(float(kg) * 2.20462) if kg else None))
        out.append(b)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    ap.add_argument("--raw", type=Path, default=db.RAW / "kaggle")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    args.raw.mkdir(parents=True, exist_ok=True)
    every = [b for name, rows in download(args.raw).items() for b in bouts(name, rows)]
    with db.updating(args.db) as (conn, result):
        counts = cards.write(conn, State(conn), every, fresh=False)
        db.sync_promotions(conn)
    log.info("Kaggle: %s", ", ".join(f"{k} {v}" for k, v in counts.items()))
    log.info("Database %s.", "changed" if result.changed else "unchanged")


if __name__ == "__main__":
    main()
