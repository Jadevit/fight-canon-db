"""What a deploy sends to R2 and clears from Cloudflare's cache. Every write to R2 counts against
its free tier, so only files that changed may be sent, whatever state the last run left."""

import hashlib
import io
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import site_upload as site


class Bucket:
    """As much of an S3 client as upload() uses, counting what's sent."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, files=None, manifest=None, etag=None):
        self.files = {k: etag or hashlib.md5(v).hexdigest() for k, v in (files or {}).items()}
        self.manifest = manifest
        self.put, self.deleted = [], []

    def get_object(self, Bucket, Key):
        if self.manifest is None:
            raise self.exceptions.NoSuchKey
        return {"Body": io.BytesIO(json.dumps(self.manifest).encode())}

    def get_paginator(self, _):
        return types.SimpleNamespace(paginate=lambda **kw: [
            {"Contents": [{"Key": k, "ETag": f'"{e}"'} for k, e in self.files.items()]}])

    def put_object(self, Bucket, Key, Body, CacheControl=None, **_):
        if Key.endswith("-manifest.json"):
            self.manifest = json.loads(Body)
        else:
            self.put.append(Key)
            self.cache = CacheControl
            self.files[Key] = hashlib.md5(Body).hexdigest()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            del self.files[o["Key"]]


@pytest.fixture
def build(tmp_path):
    def write(**files):
        for name, text in files.items():
            p = tmp_path / "v1" / f"{name}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return tmp_path
    return write


def _md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def _upload(monkeypatch, out, bucket):
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **kw: bucket))
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", types.SimpleNamespace(Config=lambda **kw: None))
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://r2.invalid")
    monkeypatch.setenv("R2_BUCKET", "b")
    site.upload(out)
    return (out / "purge.txt").read_text().split()


def test_first_upload_sends_everything(build, monkeypatch):
    bucket = Bucket()
    purge = _upload(monkeypatch, build(a="1", b="2"), bucket)
    assert sorted(bucket.put) == ["v1/a.json", "v1/b.json"] and sorted(purge) == sorted(bucket.put)
    assert bucket.cache == site.CACHE
    assert bucket.manifest == {"v1/a.json": _md5("1"), "v1/b.json": _md5("2")}


def test_nothing_changed_sends_nothing(build, monkeypatch):
    bucket = Bucket({"v1/a.json": b"1"}, {"v1/a.json": _md5("1")})
    assert _upload(monkeypatch, build(a="1"), bucket) == []
    assert bucket.put == [] and bucket.deleted == []


def test_only_what_changed_is_sent_and_what_went_is_deleted(build, monkeypatch):
    bucket = Bucket({"v1/same.json": b"1", "v1/new.json": b"2", "v1/gone.json": b"3"},
                    {"v1/same.json": _md5("1"), "v1/new.json": _md5("2"), "v1/gone.json": _md5("3")})
    purge = _upload(monkeypatch, build(same="1", new="changed", added="4"), bucket)
    assert sorted(bucket.put) == ["v1/added.json", "v1/new.json"] and bucket.deleted == ["v1/gone.json"]
    assert sorted(purge) == ["v1/added.json", "v1/gone.json", "v1/new.json"]
    assert sorted(bucket.manifest) == ["v1/added.json", "v1/new.json", "v1/same.json"]


def test_a_run_that_stopped_partway_isnt_sent_again_but_is_still_purged(build, monkeypatch):
    # The file reached the bucket, but the run died before saving the manifest.
    bucket = Bucket({"v1/a.json": b"new"}, {"v1/a.json": _md5("old")})
    assert _upload(monkeypatch, build(a="new"), bucket) == ["v1/a.json"]
    assert bucket.put == []


def test_etags_that_arent_hashes_fall_back_to_the_manifest(build, monkeypatch, capsys):
    # Comparing with them would send every file again.
    bucket = Bucket({"v1/a.json": b"1", "v1/b.json": b"2"}, {"v1/a.json": _md5("1"), "v1/b.json": _md5("old")},
                    etag="not-an-md5")
    _upload(monkeypatch, build(a="1", b="2"), bucket)
    assert bucket.put == ["v1/b.json"]
    assert "ETags" in capsys.readouterr().out


def _purge(monkeypatch, tmp_path, keys, responses=()):
    (tmp_path / "purge.txt").write_text("\n".join(keys))
    for name, value in (("R2_URL", "https://db.test"), ("CLOUDFLARE_ZONE_ID", "z"), ("CLOUDFLARE_API_TOKEN", "t")):
        monkeypatch.setenv(name, value)
    sent, pending = [], list(responses)

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(req, timeout):
        if pending and (code := pending.pop(0)):
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, io.BytesIO(b"body"))
        sent.append(json.loads(req.data))
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)
    site.purge(tmp_path)
    return sent


def test_purge_in_batches_or_all_at_once(tmp_path, monkeypatch):
    assert _purge(monkeypatch, tmp_path, []) == []
    keys = [f"v1/{i}.json" for i in range(site.PURGE_BATCH + 1)]
    sent = _purge(monkeypatch, tmp_path, keys)
    assert [len(b["files"]) for b in sent] == [site.PURGE_BATCH, 1] and sent[1]["files"] == [f"https://db.test/{keys[-1]}"]
    # More than the free plan's rate allows: the whole cache in one request.
    assert _purge(monkeypatch, tmp_path, [f"v1/{i}.json" for i in range(site.PURGE_ALL + 1)]) == [{"purge_everything": True}]


def test_purge_waits_on_429_and_stops_on_anything_else(tmp_path, monkeypatch):
    assert len(_purge(monkeypatch, tmp_path, ["v1/a.json"], [429, 429])) == 1
    with pytest.raises(SystemExit, match="HTTP 403"):
        _purge(monkeypatch, tmp_path, ["v1/a.json"], [403])
