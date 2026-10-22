"""Serve site/ for a local look at the site, like fightcanon.com does: HTTP range requests (the
SQL page reads the database in pieces; python -m http.server always sends whole files), and
index.html for any path that isn't a file (the page routes on the path).

    python scripts/site_json.py build data/canon.db site/db      # build site/db/ first
    python scripts/export_parquet.py data/canon.db site/db
    python scripts/serve.py [port]                               # then open http://localhost:8000/
"""

from __future__ import annotations

import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        self.remaining = None  # one handler serves every request on a kept-alive connection
        if not os.path.exists(self.translate_path(self.path)):
            self.path = "/index.html"  # a page of the site, not a file
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        path = self.translate_path(self.path)
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
        if start > end:
            self.send_error(416)
            return None
        fh = open(path, "rb")
        fh.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.remaining = end - start + 1
        return fh

    def copyfile(self, source, outputfile):
        left = self.remaining
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            data = source.read(min(left, 64 * 1024))
            if not data:
                break
            outputfile.write(data)
            left -= len(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "site")
    print(f"http://localhost:{port}/")
    ThreadingHTTPServer(("", port), partial(RangeHandler, directory=root)).serve_forever()
