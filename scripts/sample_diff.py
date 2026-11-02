"""Check a refactor of a source's loader on a sample of its pages: copy every Nth event or page
from data/raw/<source>/ to a temp folder, load it offline with the committed code (HEAD) and
with the working tree, each on its own copy of data/canon.db, and diff the two results. Minutes
instead of the whole load, for each step; run the whole load once before a piece is done.

    python scripts/sample_diff.py bestfightodds [--every 20]

Sherdog's load crawls from page to page, so it can't be sampled; UFC Stats has no pages on disk.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from canon_db.http import read_json  # noqa: E402
from canon_db.sources.sofascore.pages import bouts  # noqa: E402
from diffdb import diff  # noqa: E402

RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "canon.db"


def every(folder: Path, n: int) -> list[Path]:
    return sorted(folder.iterdir())[::n]


def copy(path: Path, raw: Path, out: Path) -> None:
    """`path`, a file or folder under `raw`, to the same place under `out`, if it exists."""
    to = out / path.relative_to(raw)
    to.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, to)
    elif path.exists():
        shutil.copyfile(path, to)


def sample(source: str, raw: Path, out: Path, n: int) -> int:
    """Copy every Nth event or page, with the files it refers to. Returns how many."""
    if source in ("bestfightodds", "mmadecisions"):
        picked = every(raw / {"bestfightodds": "fighters", "mmadecisions": "decisions"}[source], n)
    elif source == "pfl":
        picked = every(raw, n)
    elif source == "oktagon":
        picked = every(raw / "tournaments", n)
        for t in picked:
            for m in (read_json(t) or {}).get("matches") or []:
                copy(raw / "matches" / f"{m['id']}.json", raw, out)
        for card in (raw / "fightcards").iterdir():
            copy(card, raw, out)
    elif source == "sofascore":
        picked = every(raw / "events", n)
        for season in picked:
            for b in bouts(read_json(season) or {"events": []}):
                copy(raw / "stats" / f"{b['id']}.json", raw, out)
    else:
        raise SystemExit(f"Can't sample {source}: see this script's docstring.")
    for path in picked:
        copy(path, raw, out)
    return len(picked)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--every", type=int, default=20, help="take every Nth event or page (default: 20)")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="sample-diff-"))
    old_code = tmp / "old"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(old_code), "HEAD"], cwd=ROOT, check=True)
    try:
        n = sample(args.source, RAW / args.source, tmp / "raw" / args.source, args.every)
        print(f"{n} of {args.source}'s events or pages sampled; loading with HEAD and the working tree...", flush=True)
        runs = {}
        for name, code in (("old", old_code), ("new", ROOT)):
            shutil.copyfile(DB, tmp / f"{name}.db")
            runs[name] = subprocess.Popen(
                [sys.executable, "-m", "canon_db", "--db", str(tmp / f"{name}.db"), "--raw", str(tmp / "raw"),
                 "update", "--allow-drop", args.source, "--offline"],
                cwd=code, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        for name, run in runs.items():
            lines = run.communicate()[0].splitlines()
            print(f"--- {name} (exit {run.returncode})", *lines[-3:], sep="\n")
            if run.returncode:
                return 1
        differs = diff(str(tmp / "old.db"), str(tmp / "new.db"))
        print("Differences above." if differs else "No differences.")
        return int(differs)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(old_code)], cwd=ROOT, check=False)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
