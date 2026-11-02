"""Send the build to R2 and clear what changed from Cloudflare's cache. Every write counts
against R2's free tier, so only changed files go.

Upload compares each file's hash with the bucket's own (its listing's ETags), so it sends only
the changed ones even after a run that stopped partway. Browsers keep a file 5 minutes and
Cloudflare a day, so visits don't read R2; purge clears the files that changed (or were
deleted) since the last finished upload (v1-manifest.json in the bucket) from Cloudflare's
cache, so an update still shows within minutes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "v1"
# Files keep their names: browsers check again after 5 minutes, Cloudflare after a day (purge
# clears changed files from it sooner), so a visit reads R2 at most once a day per data center.
CACHE = "public, max-age=300, s-maxage=86400"
PUT_WAITS = (5, 15, 30)  # seconds, when R2 still says "not now" after the client's own quick retries
PURGE_BATCH = 30  # URLs per purge request (the free plan's limit)
PURGE_ALL = 3000  # more changed files than this: clear the whole cache in one request instead


def upload(out: Path, workers: int = 32) -> None:
    """Send the files whose hash differs from the bucket's copy to R2 (R2_BUCKET; AWS_ENDPOINT_URL
    and keys from the environment), delete the bucket's files the build doesn't have, then save
    the manifest. The files to purge are those that differ from the last manifest."""
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                      config=Config(max_pool_connections=workers, read_timeout=300,
                                    retries={"max_attempts": 10, "mode": "adaptive"}))
    bucket, manifest = os.environ["R2_BUCKET"], f"{VERSION}-manifest.json"
    root = out / VERSION
    files = {f"{VERSION}/{p.relative_to(root).as_posix()}": p for p in root.rglob("*.json")}
    hashes = {k: hashlib.md5(p.read_bytes()).hexdigest() for k, p in files.items()}
    try:
        old = json.loads(s3.get_object(Bucket=bucket, Key=manifest)["Body"].read())
    except s3.exceptions.NoSuchKey:
        old = {}
    live = {o["Key"]: o["ETag"].strip('"') for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{VERSION}/") for o in page.get("Contents", [])}
    same = [k for k, h in hashes.items() if old.get(k) == h and k in live]
    if same and not any(live[k] == hashes[k] for k in same[:1000]):  # ETags that aren't MD5s
        print("The bucket's ETags don't match file hashes; comparing with the manifest instead.")
        live = {k: old.get(k) for k in live}
    changed = [k for k, h in hashes.items() if live.get(k) != h]
    gone = [k for k in live if k not in hashes]

    def put(k: str) -> None:
        for wait in (*PUT_WAITS, None):
            try:
                s3.put_object(Bucket=bucket, Key=k, Body=files[k].read_bytes(), ContentType="application/json",
                              CacheControl=CACHE)
                return
            except s3.exceptions.ClientError as e:
                if wait is None or e.response["Error"]["Code"] not in ("ServiceUnavailable", "SlowDown"):
                    raise
                time.sleep(wait)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(put, changed))
    for i in range(0, len(gone), 200):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in gone[i:i + 200]], "Quiet": True})
    s3.put_object(Bucket=bucket, Key=manifest, Body=json.dumps(hashes).encode(), ContentType="application/json")
    stale = [k for k, h in hashes.items() if old.get(k) != h] + [k for k in old if k not in hashes]
    (out / "purge.txt").write_text("\n".join(stale))
    print(f"{len(files)} files: {len(changed)} uploaded, {len(gone)} deleted, {len(stale)} to purge")


def purge(out: Path) -> None:
    """Clear the files the last upload changed or deleted (<out>/purge.txt) from Cloudflare's
    cache (R2_URL's zone: CLOUDFLARE_ZONE_ID, and CLOUDFLARE_API_TOKEN with Cache Purge)."""
    keys = (out / "purge.txt").read_text().split()
    if not keys:
        return
    base, zone, token = os.environ["R2_URL"], os.environ["CLOUDFLARE_ZONE_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
    urls = [f"{base}/{k}" for k in keys]
    batches = ([{"purge_everything": True}] if len(urls) > PURGE_ALL
               else [{"files": urls[i:i + PURGE_BATCH]} for i in range(0, len(urls), PURGE_BATCH)])
    for body in batches:
        for attempt in range(5):
            req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30):
                    break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 4:  # too many requests: wait, then again
                    raise SystemExit(f"purge: HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
                time.sleep(10)
    print("cleared Cloudflare's cache" if len(urls) > PURGE_ALL else f"cleared {len(urls)} files from Cloudflare's cache")
