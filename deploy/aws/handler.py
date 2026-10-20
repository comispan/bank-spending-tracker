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

In front of all of it: a login page checked against a PBKDF2 hash, which sets
a signed session cookie, and a refusal of cross-site writes. The cookie's key
and a count of recent wrong passwords live under `auth/` in the bucket, so a
flood of guesses is cut off across every instance. Not HTTP Basic
auth: a Function URL renames the WWW-Authenticate header on the way out, so
the browser never shows its sign-in prompt.

Signing out replaces that key, which ends every session at once — a cookie
is only a signature, so there is no one session to cross off. Every response
carries the security headers below, and every sign-in, wrong password and
sign-out is one JSON line in the function's log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

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
# Here there is a sign-in, so the app's header gets a button to end it.
main.templates.env.globals["sign_out"] = True

_etag: str | None = None   # the S3 version our local app.db came from
_stamp: tuple[int, int] | None = None  # (mtime_ns, size) as last synced
_started = False


# --------------------------------------------------------------------- auth

COOKIE = "tracker_session"
SESSION_SECONDS = 30 * 24 * 3600

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Spending Tracker</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f6f4; --card: #fff; --fg: #1d1d1b; --muted: #666; --line: #d8d8d4; --err: #b42318; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #161616; --card: #202020; --fg: #eee; --muted: #999; --line: #3a3a3a; --err: #f97066; }} }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--fg); font: 16px/1.4 system-ui, sans-serif; }}
  form {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 28px; width: min(320px, calc(100vw - 32px)); box-sizing: border-box; display: grid; gap: 12px; }}
  h1 {{ margin: 0 0 4px; font-size: 20px; }}
  label {{ display: grid; gap: 4px; font-size: 14px; color: var(--muted); }}
  input {{ font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }}
  button {{ font: inherit; padding: 9px; border: 0; border-radius: 6px; background: #2f6f5e; color: #fff; cursor: pointer; }}
  .err {{ color: var(--err); font-size: 14px; margin: 0; }}
  p {{ margin: 0; color: var(--muted); font-size: 14px; }}
</style></head>
<body>{body}</body></html>"""

LOGIN_FORM = """<form method="post" action="/login">
  <h1>Spending Tracker</h1>
  {error}
  <input type="hidden" name="next" value="{next}">
  <label>Username <input name="username" autocomplete="username" required autofocus></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in</button>
</form>"""

# What a bare GET of /logout shows. Signing out is a post: as a link, any page
# could sign you out of every browser with an <img src=".../logout">.
LOGOUT_FORM = """<form method="post" action="/logout">
  <h1>Sign out?</h1>
  <p>This signs out every browser that is signed in, not just this one.</p>
  <button type="submit">Sign out</button>
</form>"""

# On every response. The app has no business inside anyone's frame, a file is
# the type it says it is, and a link out names this site, never its URL: the
# query strings here carry merchants and filters. `same-origin`, not
# `no-referrer`: under that, a browser sends `Origin: null` on its own form
# posts, and the cross-site check below would refuse every one of them.
SECURITY_HEADERS = {
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": ("frame-ancestors 'none'; base-uri 'none'; "
                                "form-action 'self'; object-src 'none'"),
    "referrer-policy": "same-origin",
}


def _check_password(user: str, password: str) -> bool:
    algo, iterations, salt, expected = os.environ["PASSWORD_HASH"].split("$")
    if algo != "pbkdf2_sha256":
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt),
                              int(iterations))
    ok = hmac.compare_digest(got, base64.b64decode(expected))
    ok &= hmac.compare_digest(user.encode(), os.environ["AUTH_USER"].encode())
    return ok


SESSION_KEY = "auth/session-key"
FAILURES_KEY = "auth/login-failures.json"
MAX_FAILURES = 10            # wrong passwords allowed per window, across all instances
FAILURE_WINDOW = 15 * 60

_session_key: bytes | None = None
_session_etag: str | None = None


def _refresh_secret() -> bytes:
    """32 random bytes kept in the bucket, made on first use.

    Not in the function's environment: anyone who can read the function's
    configuration would otherwise be able to mint a session cookie. Here they
    need the bucket too, which holds the statements anyway.

    Re-read once per request that checks or makes a cookie — a conditional
    GET, a 304 when nothing moved — because signing out replaces the key, and
    every warm instance has to stop honouring the old one then, not whenever
    it happens to be recycled."""
    global _session_key, _session_etag
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=SESSION_KEY,
                            **({"IfNoneMatch": _session_etag} if _session_etag else {}))
        _session_key, _session_etag = obj["Body"].read(), obj["ETag"]
    except ClientError as e:
        if _code(e) in ("304", "NotModified"):
            return _session_key
        if _code(e) != "NoSuchKey":
            raise
        key = os.urandom(32)
        try:
            out = s3.put_object(Bucket=BUCKET, Key=SESSION_KEY, Body=key, IfNoneMatch="*")
            _session_key, _session_etag = key, out["ETag"]
        except ClientError as e2:
            if _code(e2) not in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise
            # Another instance made one first: use theirs.
            obj = s3.get_object(Bucket=BUCKET, Key=SESSION_KEY)
            _session_key, _session_etag = obj["Body"].read(), obj["ETag"]
    return _session_key


def _secret() -> bytes:
    """The key as of this request's refresh."""
    return _session_key if _session_key is not None else _refresh_secret()


def _end_all_sessions() -> None:
    """A new key: every cookie signed with the old one stops verifying."""
    global _session_key, _session_etag
    key = os.urandom(32)
    out = s3.put_object(Bucket=BUCKET, Key=SESSION_KEY, Body=key)
    _session_key, _session_etag = key, out["ETag"]


def _audit(event: dict, what: str) -> None:
    """One line in the function's log per sign-in event: when, from where,
    with what browser. Never what was typed — not even the username, which is
    the password often enough when it goes in the wrong box."""
    http = event["requestContext"]["http"]
    print(json.dumps({"auth": what, "ip": http.get("sourceIp"),
                      "agent": (http.get("userAgent") or "")[:200]}))


def _sign(expires: int) -> str:
    # Also keyed on the password hash, so changing the password signs out
    # every browser that had a session — the one way to revoke a stolen cookie.
    key = hmac.new(_secret(), b"session|" + os.environ["PASSWORD_HASH"].encode(),
                   hashlib.sha256).digest()
    return f"{expires}.{hmac.new(key, str(expires).encode(), hashlib.sha256).hexdigest()}"


def _recent_failures() -> list[float]:
    try:
        raw = s3.get_object(Bucket=BUCKET, Key=FAILURES_KEY)["Body"].read()
    except ClientError as e:
        if _code(e) == "NoSuchKey":
            return []
        raise
    try:
        stamps = [float(t) for t in json.loads(raw)]
    except (ValueError, TypeError):
        return []
    return [t for t in stamps if t > time.time() - FAILURE_WINDOW]


def _note_failure(recent: list[float]) -> None:
    # Read-modify-write without a lock: two guesses at the same instant can
    # count as one. Close enough to cap guessing at about MAX_FAILURES a window.
    s3.put_object(Bucket=BUCKET, Key=FAILURES_KEY,
                  Body=json.dumps(recent + [time.time()]).encode())


def _signed_in(event: dict) -> bool:
    # Payload v2 carries cookies in their own list, not in the headers.
    ours = [c.partition("=")[2] for c in event.get("cookies") or []
            if c.partition("=")[0].strip() == COOKIE]
    if not ours:
        return False   # no S3 read for a visitor with no cookie at all
    _refresh_secret()
    for value in ours:
        expires = value.partition(".")[0]
        if (expires.isdigit() and int(expires) > time.time()
                and hmac.compare_digest(value, _sign(int(expires)))):
            return True
    return False


def _safe_next(target: str) -> str:
    # Only a path on this site: "//evil.example" would be an open redirect. A
    # browser drops tabs and newlines from a URL and reads "\" as "/", so
    # "/\t/evil.example" is that same redirect in disguise: refuse any of them.
    if (not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in target)):
        return "/"
    return target


def _page(title: str, body: str, status: int = 200) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "text/html; charset=utf-8",
                                              "Cache-Control": "no-store"},
            "body": PAGE.format(title=title, body=body)}


def _login_page(target: str, error: str = "", status: int = 200) -> dict:
    return _page("Sign in", LOGIN_FORM.format(
        next=html.escape(target), error=f'<p class="err">{error}</p>' if error else ""), status)


def _harden(response: dict, path: str) -> dict:
    """The security headers, on whatever is going out. Pages are not cached:
    they are bank statements, and the next person at a shared computer has a
    back button. The stylesheet is, because its URL changes when it does."""
    headers = response.setdefault("headers", {})
    present = {k.lower() for k in headers}
    for name, value in SECURITY_HEADERS.items():
        if name not in present:
            headers[name] = value
    if "cache-control" not in present and not path.startswith("/static/"):
        headers["cache-control"] = "no-store"
    return response


def _refuse(event: dict) -> dict | None:
    """A response that stands in for the app's — login, logout, a refusal —
    or None when the request may go through."""
    headers = event.get("headers") or {}
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    query = {k: v[0] for k, v in parse_qs(event.get("rawQueryString", "")).items()}

    if method not in ("GET", "HEAD"):
        # A browser attaches the session cookie to a post from any site that
        # targets this one; SameSite=Lax stops most of that, this stops the rest.
        site = headers.get("sec-fetch-site")
        origin = headers.get("origin")
        if (site not in (None, "same-origin", "none")
                or (origin is not None and origin != f"https://{headers.get('host')}")):
            return {"statusCode": 403, "body": "Cross-site request refused",
                    "headers": {"Content-Type": "text/plain"}}

    if path == "/login":
        if method != "POST":
            return _login_page(_safe_next(query.get("next", "/")))
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        form = {k: v[0] for k, v in parse_qs(body).items()}
        target = _safe_next(form.get("next", "/"))
        # Checked before the password, so a flood of guesses is turned away
        # without paying for PBKDF2 on each. A browser already signed in keeps
        # its session meanwhile; only signing in anew waits.
        recent = _recent_failures()
        if len(recent) >= MAX_FAILURES:
            wait = int(min(recent) + FAILURE_WINDOW - time.time()) // 60 + 1
            _audit(event, "sign-in refused: too many wrong passwords")
            return _login_page(target, f"Too many wrong passwords. Try again in {wait} "
                                       "minute(s).", 429)
        if not _check_password(form.get("username", ""), form.get("password", "")):
            _note_failure(recent)
            _audit(event, "wrong username or password")
            return _login_page(target, "Wrong username or password.", 401)
        if recent:
            s3.delete_object(Bucket=BUCKET, Key=FAILURES_KEY)
        _refresh_secret()   # sign with the current key, not one a sign-out replaced
        session = _sign(int(time.time()) + SESSION_SECONDS)
        _audit(event, "signed in")
        return {"statusCode": 303, "headers": {"Location": target},
                "cookies": [f"{COOKIE}={session}; Max-Age={SESSION_SECONDS}; Path=/; "
                            "Secure; HttpOnly; SameSite=Lax"]}

    if path == "/logout":
        if method != "POST":
            return _page("Sign out", LOGOUT_FORM)
        # Only a signed-in browser can end everyone's session; anyone else
        # just has their own (absent) cookie cleared.
        if _signed_in(event):
            _end_all_sessions()
            _audit(event, "signed out: every session ended")
        return {"statusCode": 303, "headers": {"Location": "/login"},
                "cookies": [f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"]}

    if not _signed_in(event):
        if method in ("GET", "HEAD"):
            target = path + (f"?{event['rawQueryString']}" if event.get("rawQueryString") else "")
            return {"statusCode": 303, "headers": {"Location": "/login?next=" + quote(target, safe="")}}
        return {"statusCode": 401, "body": "Signed out: reload the page to sign in again",
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
    path = event.get("rawPath", "/")
    return _harden(_respond(event, context), path)


def _respond(event: dict, context) -> dict:
    global _started
    refused = _refuse(event)
    if refused:
        return refused
    # The app signs its banner text (main.signed_banners). Its own key is per
    # process; this one is the same on every instance, so a redirect answered
    # by one and followed to another still shows its notice.
    main.banner_key = hmac.new(_secret(), b"banners", hashlib.sha256).digest()

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
