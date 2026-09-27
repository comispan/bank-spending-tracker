"""A static, read-only snapshot of the app with the demo set filed, for GitHub Pages.

Pages serves files, not Python, so this runs the real app in-process, files the
synthetic demo statements through `/demo` exactly as the button does, then
follows every link from `/` and writes what each page rendered to disk. What a
visitor sees is the app's own output on the demo set, not a mock-up of it.

The data folder is a fresh temporary directory, set before the app is imported.
`TRACKER_DATA` from the environment is ignored on purpose: whatever lands in
the output is published to the open web, so the only database this script ever
reads is the one it just made from synthetic PDFs.

Mapping a dynamic app onto files:

  * `/months/2025-08`           → `months/2025-08/index.html`
  * `/transactions?month=…`     → `transactions/q/<hash>/index.html`. Pages
    ignores query strings, so each distinct query gets its own folder, named by
    a hash of its canonical form. `static.js` computes the same hash for the
    Analytics month picker, so a pre-rendered pick still works from the form.
  * `/statements/3/pdf`         → `statements/3/pdf.pdf`
  * Every root-absolute link is prefixed with `--base` (`/bank-spending-tracker`
    for a project page), and forms that write are disabled by `static.js`.

    python deploy/snapshot.py [--out _site] [--base /bank-spending-tracker]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import sys
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# Characters encodeURIComponent leaves alone, so Python and static.js agree on
# the canonical query, and so on its hash.
UNRESERVED = "-_.!~*'()"
# A runaway guard, not a budget: the demo set renders well under this.
MAX_PAGES = 20000

ATTR = re.compile(r'\b(href|src|action)="([^"]*)"')
EXTENSIONS = {"application/pdf": ".pdf", "text/css": ".css",
              "application/javascript": ".js", "text/javascript": ".js"}


def canonical_query(query: str) -> str:
    return "&".join(f"{quote(k, safe=UNRESERVED)}={quote(v, safe=UNRESERVED)}"
                    for k, v in parse_qsl(query, keep_blank_values=True))


def query_slug(query: str) -> str:
    return hashlib.sha1(canonical_query(query).encode()).hexdigest()[:12]


def key(url: str) -> str:
    """The identity of a page: path plus canonical query, no fragment."""
    parts = urlsplit(url)
    q = canonical_query(parts.query)
    return parts.path + (f"?{q}" if q else "")


def output_path(url: str, content_type: str) -> str:
    """Where a fetched URL lives in the site, relative to its root, with a
    leading slash — the same string serves as the file path and the link."""
    parts = urlsplit(url)
    path = parts.path
    if path.startswith("/static/"):
        return path  # the ?v= cache-buster has nothing to select statically
    if content_type.startswith("text/html"):
        folder = path.rstrip("/")
        if parts.query and canonical_query(parts.query):
            folder += f"/q/{query_slug(parts.query)}"
        return folder + "/"
    ext = EXTENSIONS.get(content_type.split(";")[0].strip(), "")
    return path if not ext or path.endswith(ext) else path + ext


def links(page_url: str, body: str) -> list[str]:
    found = []
    for attr, raw in ATTR.findall(body):
        if attr == "action":
            continue  # forms are not crawled; the month picker's presets are links
        url = urljoin(page_url, html.unescape(raw))
        if url.startswith("/") and not url.startswith("//"):
            found.append(url.split("#")[0])
    return found


def rewrite(page_url: str, body: str, where: dict[str, str], base: str) -> str:
    def sub(m: re.Match) -> str:
        attr, raw = m.groups()
        url = urljoin(page_url, html.unescape(raw))
        if not url.startswith("/") or url.startswith("//"):
            return m.group(0)
        fragment = "#" + url.split("#", 1)[1] if "#" in url else ""
        target = where.get(key(url.split("#")[0]))
        if target is None:
            # A POST action, or a GET the crawl never reached: keep the path so
            # the page still reads right, and let 404.html explain.
            target = urlsplit(url).path
        return f'{attr}="{html.escape(base + target + fragment, quote=True)}"'

    body = ATTR.sub(sub, body)
    banner = ('<div class="banner notice static-demo">Static demo: synthetic statements '
              'from three invented banks, filed by the real parser. Read-only here — '
              'uploading, recategorizing and rules need the app running locally.</div>')
    script = f'<script src="{base}/static/static.js" data-base="{base}"></script>'
    body = body.replace("<main>", "<main>\n    " + banner, 1)
    return body.replace("</body>", f"  {script}\n</body>", 1)


def build(out: Path, base: str) -> None:
    data = Path(tempfile.mkdtemp(prefix="tracker-snapshot-"))
    os.environ["TRACKER_DATA"] = str(data)
    sys.path.insert(0, str(ROOT / "app"))
    from fastapi.testclient import TestClient
    import main  # noqa: E402 — must follow TRACKER_DATA

    fetched: dict[str, tuple[str, str, bytes]] = {}  # key → (url, content type, body)
    try:
        with TestClient(main.app) as client:
            r = client.post("/demo", follow_redirects=False)
            if r.status_code != 303:
                sys.exit(f"/demo answered {r.status_code}, not a redirect")
            if "error=" in r.headers.get("location", ""):
                sys.exit(f"/demo did not file cleanly: {r.headers['location']}")

            queue, seen = deque(["/"]), {key("/")}
            while queue:
                if len(fetched) >= MAX_PAGES:
                    sys.exit(f"stopped at {MAX_PAGES} pages; is a link generating new URLs?")
                url = queue.popleft()
                r = client.get(url, follow_redirects=False)
                if r.status_code != 200:
                    print(f"skip {url}: {r.status_code}", file=sys.stderr)
                    continue
                ctype = r.headers.get("content-type", "")
                fetched[key(url)] = (url, ctype, r.content)
                if ctype.startswith("text/html"):
                    for link in links(url, r.text):
                        k = key(link)
                        if k not in seen:
                            seen.add(k)
                            queue.append(link)
    finally:
        shutil.rmtree(data, ignore_errors=True)

    where = {k: output_path(url, ctype) for k, (url, ctype, _) in fetched.items()}

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for k, (url, ctype, body) in fetched.items():
        rel = where[k]
        dest = out / (rel.lstrip("/") + ("index.html" if rel.endswith("/") else ""))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if ctype.startswith("text/html"):
            dest.write_text(rewrite(url, body.decode(), where, base), encoding="utf-8")
        else:
            dest.write_bytes(body)

    # The stylesheet may not be linked with every page's cache-buster, so copy
    # the static folder whole, then the snapshot's own script and 404 page.
    shutil.copytree(ROOT / "app" / "static", out / "static", dirs_exist_ok=True)
    shutil.copy(HERE / "pages" / "static.js", out / "static" / "static.js")
    page404 = (HERE / "pages" / "404.html").read_text(encoding="utf-8")
    (out / "404.html").write_text(page404.replace("{{BASE}}", base), encoding="utf-8")
    (out / ".nojekyll").write_text("")

    pages = sum(1 for _, c, _ in fetched.values() if c.startswith("text/html"))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"{pages} pages, {len(fetched) - pages} other files, "
          f"{size / 1e6:.1f} MB → {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "_site")
    ap.add_argument("--base", default="", help="URL prefix, e.g. /bank-spending-tracker")
    args = ap.parse_args()
    build(args.out, args.base.rstrip("/"))
