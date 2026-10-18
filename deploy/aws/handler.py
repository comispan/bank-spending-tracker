"""AWS Lambda entry point: the same FastAPI app, with data/ kept in S3.

The app wants a local SQLite file and a folder of PDFs. Lambda has neither
between invocations, so `data/` lives in /tmp and S3 is the copy of record:

* Before each request the database is refreshed from S3 — a conditional GET
  on its ETag, so a warm instance that already has the latest pays one 304.
* After each request that changed the file, it goes back with `If-Match` on
  the ETag it came from. If another instance wrote in between, this write is
  refused rather than silently overwriting theirs, and the request answers
  409. One user rarely hits that; the bucket is versioned regardless.
* PDFs go to `statements/` beside it. A new one is uploaded before the
  database that names it, and one the database no longer names is deleted
  after. Viewing a PDF fetches it into /tmp on demand.

In front of all of it: HTTP Basic auth against a PBKDF2 hash, and a refusal of
cross-site writes, since a browser sends saved Basic credentials with any
form post — including one from another site.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sqlite3
from pathlib import Path

# db.py reads this at import, so it is set before the app is imported.
DATA = Path(os.environ.setdefault("TRACKER_DATA", "/tmp/data"))

import boto3  # noqa: E402  (in the Lambda runtime; not in requirements.txt)
from botocore.exceptions import ClientError  # noqa: E402
from mangum import Mangum  # noqa: E402

import main  # noqa: E402

BUCKET = os.environ["DATA_BUCKET"]
DB = DATA / "app.db"
STATEMENTS = DATA / "statements"
DB_KEY = "app.db"
PDF_PREFIX = "statements/"

s3 = boto3.client("s3")
# Lifespan off: Mangum would otherwise run the app's startup — every backfill
# and a full recategorize — on every invocation. It runs once per container.
asgi = Mangum(main.app, lifespan="off")

_etag: str | None = None   # the S3 version our local app.db came from
_stamp: tuple[int, int] | None = None  # (mtime_ns, size) as last synced
_started = False
_verified: set[str] = set()  # Authorization headers already checked here


# --------------------------------------------------------------------- auth

def _check_password(header: str) -> bool:
    if header in _verified:
        return True
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    algo, iterations, salt, expected = os.environ["PASSWORD_HASH"].split("$")
    if algo != "pbkdf2_sha256":
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt),
                              int(iterations))
    ok = hmac.compare_digest(got, base64.b64decode(expected))
    ok &= hmac.compare_digest(user.encode(), os.environ["AUTH_USER"].encode())
    if ok:
        _verified.add(header)
    return ok


def _refuse(event: dict) -> dict | None:
    headers = event.get("headers") or {}
    if not _check_password(headers.get("authorization", "")):
        return {"statusCode": 401, "body": "Sign in required",
                "headers": {"WWW-Authenticate": 'Basic realm="Spending tracker", charset="UTF-8"',
                            "Content-Type": "text/plain"}}
    method = event["requestContext"]["http"]["method"]
    if method not in ("GET", "HEAD"):
        site = headers.get("sec-fetch-site")
        origin = headers.get("origin")
        cross = (site not in (None, "same-origin", "none")
                 or (origin is not None and origin != f"https://{headers.get('host')}"))
        if cross:
            return {"statusCode": 403, "body": "Cross-site request refused",
                    "headers": {"Content-Type": "text/plain"}}
    return None


# --------------------------------------------------------------------- sync

def _local_stamp() -> tuple[int, int] | None:
    try:
        st = DB.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def pull() -> None:
    """Replace the local database with S3's if S3 has moved on."""
    global _etag, _stamp
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=DB_KEY,
                            **({"IfNoneMatch": _etag} if _etag else {}))
    except ClientError as e:
        if _code(e) in ("304", "NotModified", "NoSuchKey"):
            return  # unchanged, or a first run: the app creates the file
        raise
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DB.with_suffix(".download")
    tmp.write_bytes(obj["Body"].read())
    tmp.replace(DB)
    _etag, _stamp = obj["ETag"], _local_stamp()


def _filename(storage_path: str) -> str:
    # A database carried over from Windows names D:\...\x.pdf; take the last
    # part either way.
    return re.split(r"[\\/]", storage_path)[-1]


def _s3_pdfs() -> set[str]:
    names = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PDF_PREFIX):
        names.update(o["Key"][len(PDF_PREFIX):] for o in page.get("Contents", []))
    return names


def push() -> bool:
    """Send the database back if this request changed it. False on a lost race."""
    global _etag, _stamp
    if _local_stamp() == _stamp:
        return True

    with sqlite3.connect(DB) as conn:
        wanted = {_filename(p) for (p,) in conn.execute("SELECT storage_path FROM statement")}
    conn.close()
    stored = _s3_pdfs()
    for name in wanted - stored:
        local = STATEMENTS / name
        if local.exists():
            s3.upload_file(str(local), BUCKET, PDF_PREFIX + name)

    try:
        out = s3.put_object(Bucket=BUCKET, Key=DB_KEY, Body=DB.read_bytes(),
                            **({"IfMatch": _etag} if _etag else {"IfNoneMatch": "*"}))
    except ClientError as e:
        if _code(e) not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        # Another instance wrote first. Forget our version, so the next pull
        # fetches theirs unconditionally and writes over this copy.
        _etag = _stamp = None
        return False
    _etag, _stamp = out["ETag"], _local_stamp()

    stale = sorted(stored - wanted)
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={
            "Objects": [{"Key": PDF_PREFIX + n} for n in stale[i:i + 1000]], "Quiet": True})
    return True


def fetch_pdf(path: str) -> None:
    """Bring a statement's PDF into /tmp before the app goes looking for it."""
    m = re.fullmatch(r"/statements/(\d+)/pdf", path)
    if not m or not DB.exists():
        return
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT storage_path FROM statement WHERE id = ?",
                           (int(m[1]),)).fetchone()
    conn.close()
    if row and not Path(row[0]).exists():
        Path(row[0]).parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.download_file(BUCKET, PDF_PREFIX + _filename(row[0]), row[0])
        except ClientError:
            pass  # the app answers 404, which is the truth


# ------------------------------------------------------------------ handler

def handler(event: dict, context) -> dict:
    global _started
    refused = _refuse(event)
    if refused:
        return refused

    pull()
    if not _started:
        main.startup()
        _started = True
    fetch_pdf(event.get("rawPath", ""))

    response = asgi(event, context)
    if not push():
        return {"statusCode": 409, "headers": {"Content-Type": "text/html; charset=utf-8"},
                "body": "<p>Another request changed the data at the same moment, so this "
                        "change was <strong>not saved</strong>. <a href='/'>Reload</a> and "
                        "try again.</p>"}
    return response
