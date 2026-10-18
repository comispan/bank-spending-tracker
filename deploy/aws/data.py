"""Copy the tracker's data between this machine and the stack's S3 bucket.

    python deploy/aws/data.py push <bucket>             # data/ -> S3 (replaces what's there)
    python deploy/aws/data.py pull <bucket> [folder]    # S3 -> backups/aws-<time>/ by default

The database records each PDF by full path, so a copy carries the paths of the
machine it came from. Each direction rewrites them on a copy of app.db — to
/tmp/data/statements/ for Lambda, to the destination folder for a pull — and
never touches the source. Uses the AWS CLI, so no boto3 needed here.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
LAMBDA_STATEMENTS = "/tmp/data/statements"
# The same profile deploy.ps1 uses by default; AWS_PROFILE picks another.
PROFILE = os.environ.get("AWS_PROFILE") or "eugene_home_aws"


def rewrite_paths(db: Path, statement_dir: str, sep: str) -> None:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT id, storage_path FROM statement").fetchall()
        for sid, path in rows:
            name = re.split(r"[\\/]", path)[-1]
            conn.execute("UPDATE statement SET storage_path = ? WHERE id = ?",
                         (statement_dir + sep + name, sid))
    conn.close()


def aws(*args: str) -> None:
    subprocess.run(["aws", "--profile", PROFILE, *args], check=True)


def push(bucket: str) -> None:
    src = ROOT / "data"
    if not (src / "app.db").exists():
        sys.exit(f"No database at {src / 'app.db'}")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "app.db"
        shutil.copy2(src / "app.db", db)
        rewrite_paths(db, LAMBDA_STATEMENTS, "/")
        # PDFs first, then the database that names them; --delete clears any
        # PDF this database doesn't know, e.g. from an earlier trial upload.
        aws("s3", "sync", str(src / "statements"), f"s3://{bucket}/statements/",
            "--delete", "--only-show-errors")
        aws("s3", "cp", str(db), f"s3://{bucket}/app.db", "--only-show-errors")
    print(f"Pushed data/ to s3://{bucket}. The next request to the app picks it up.")


def pull(bucket: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=False)
    # auth/ is the login's own state (the session key among it): no use in a
    # backup, and a key on disk is one more copy to leak.
    aws("s3", "sync", f"s3://{bucket}/", str(dest), "--exclude", "auth/*", "--only-show-errors")
    rewrite_paths(dest / "app.db", str((dest / "statements").resolve()), "/")
    print(f"Pulled to {dest}. Run against it with TRACKER_DATA={dest}")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("push", "pull"):
        sys.exit(__doc__)
    if sys.argv[1] == "push":
        push(sys.argv[2])
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        pull(sys.argv[2], Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "backups" / f"aws-{stamp}")
