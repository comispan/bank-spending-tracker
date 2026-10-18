"""Build the Lambda zip, or hash a password for the stack.

    python deploy/aws/build.py zip     -> deploy/aws/build/lambda.zip, prints its path
    python deploy/aws/build.py hash    -> prompts for a password, prints the PasswordHash

The zip is the app/ folder, handler.py, and the dependencies as Linux arm64
wheels — pip downloads those for the target platform, so this runs the same
from Windows. Nothing under data/ or .env goes in.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
BUILD = HERE / "build"
ITERATIONS = 200_000


def build_zip() -> Path:
    pkg = BUILD / "pkg"
    shutil.rmtree(BUILD, ignore_errors=True)
    pkg.mkdir(parents=True)
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--quiet", "--no-warn-conflicts",
        "-r", str(HERE / "requirements-lambda.txt"), "--target", str(pkg),
        "--platform", "manylinux2014_aarch64", "--implementation", "cp",
        "--python-version", "3.12", "--only-binary=:all:",
    ], check=True)

    out = BUILD / "lambda.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(pkg.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(pkg).as_posix())
        for path in sorted((ROOT / "app").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(ROOT / "app").as_posix())
        z.write(HERE / "handler.py", "handler.py")
    return out


def hash_password() -> str:
    password = getpass.getpass("Password for the tracker: ")
    if len(password) < 12:
        sys.exit("Use at least 12 characters; this is the only lock on your statements.")
    if password != getpass.getpass("Again: "):
        sys.exit("The two didn't match.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "$".join(["pbkdf2_sha256", str(ITERATIONS),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "zip":
        print(build_zip())
    elif command == "hash":
        print(hash_password())
    else:
        sys.exit(__doc__)
