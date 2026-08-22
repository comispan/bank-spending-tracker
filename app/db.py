"""SQLite schema and access. Single-user, single file, no ORM.

Money is stored as integer minor units everywhere — never a float. SQLite has
no decimal type and its REAL is IEEE754, so `0.1 + 0.2` in a SUM would drift a
statement out of reconciliation for reasons that have nothing to do with the
parser. Dates are ISO strings; a transaction date has no timezone.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import merchants

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
STATEMENT_DIR = DATA / "statements"

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id          INTEGER PRIMARY KEY,
    issuer      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'credit' CHECK (kind IN ('credit', 'debit', 'bank')),
    last4       TEXT,
    nickname    TEXT,
    currency    TEXT NOT NULL DEFAULT 'SGD',
    UNIQUE (issuer, last4)
);

CREATE TABLE IF NOT EXISTS statement (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id),
    filename        TEXT NOT NULL,
    -- Re-uploading the identical PDF is the cheapest duplicate to catch, so
    -- catch it at the database rather than after parsing.
    file_sha256     TEXT NOT NULL UNIQUE,
    storage_path    TEXT NOT NULL,
    period_start    TEXT,
    period_end      TEXT,
    -- Most issuers print a closing date rather than a range. Kept because it is
    -- what dates the rows when no period is printed, and because showing it
    -- beats showing an empty period the user cannot interpret.
    statement_date  TEXT,
    opening_balance_minor INTEGER,
    closing_balance_minor INTEGER,
    page_count      INTEGER NOT NULL,
    scanned_pages   INTEGER NOT NULL DEFAULT 0,
    parser_version  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('parsed', 'needs_review', 'failed')),
    verdict         TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'unverified', 'error')),
    verdict_detail  TEXT NOT NULL DEFAULT '',
    -- rows_expected comes from a regex deliberately independent of the parser.
    -- A gap between these two is the earliest warning that a layout changed,
    -- and it is invisible without storing both.
    rows_expected   INTEGER NOT NULL DEFAULT 0,
    rows_parsed     INTEGER NOT NULL DEFAULT 0,
    warnings        TEXT NOT NULL DEFAULT '[]',
    -- Kept so a parser improvement can be replayed over old statements without
    -- re-uploading. Extraction is deterministic, so a replay either reproduces
    -- the old rows exactly or tells you precisely what your change did.
    raw_extraction  TEXT NOT NULL DEFAULT '{}',
    page_text       TEXT NOT NULL DEFAULT '[]',
    uploaded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS txn (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account(id),
    statement_id    INTEGER NOT NULL REFERENCES statement(id) ON DELETE CASCADE,
    txn_date        TEXT NOT NULL,
    posted_date     TEXT,
    description_raw TEXT NOT NULL,
    merchant_normalized TEXT,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    amount_sgd_minor INTEGER NOT NULL,
    fx_rate         TEXT,
    direction       TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    flow_type       TEXT,
    category        TEXT,
    category_source TEXT,
    source_page     INTEGER,
    -- The issuer's own reference for the row, when it prints one. Two genuine
    -- charges can match on every other field; this is the only thing that
    -- tells them apart, so dedup_key is built from it too.
    reference       TEXT,
    dedup_key       TEXT,
    duplicate_of_id INTEGER REFERENCES txn(id)
);

CREATE INDEX IF NOT EXISTS txn_by_statement ON txn(statement_id);
CREATE INDEX IF NOT EXISTS txn_by_date ON txn(txn_date);
"""


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    STATEMENT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. SQLite's ALTER TABLE ADD COLUMN is
# cheap and non-destructive, and CREATE TABLE IF NOT EXISTS will not add a
# column to a table that already exists — so new fields need this list or they
# silently never appear on an existing database.
MIGRATIONS: list[tuple[str, str]] = [
    ("statement", "statement_date TEXT"),
    ("txn", "reference TEXT"),
]


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, definition in MIGRATIONS:
            column = definition.split()[0]
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        conn.commit()


# ------------------------------------------------------------------ money

def to_minor(value: str | Decimal | None) -> int | None:
    """Decimal string -> integer minor units, half-up at 2dp."""
    if value is None or value == "":
        return None
    d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def to_major(minor: int | None) -> Decimal | None:
    return None if minor is None else Decimal(minor) / 100


# ------------------------------------------------------------------ writes

def find_or_create_account(conn: sqlite3.Connection, issuer: str, last4: str | None,
                           currency: str) -> int:
    row = conn.execute(
        "SELECT id FROM account WHERE issuer = ? AND last4 IS ?", (issuer, last4)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO account (issuer, last4, currency) VALUES (?, ?, ?)",
        (issuer, last4, currency),
    )
    return int(cur.lastrowid)


def statement_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM statement WHERE file_sha256 = ?", (sha256,)).fetchone()


def insert_statement(conn: sqlite3.Connection, account_id: int, fields: dict[str, Any]) -> int:
    cols = ["account_id"] + list(fields)
    values = [account_id] + [
        json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
        for v in fields.values()
    ]
    placeholders = ", ".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO statement ({', '.join(cols)}) VALUES ({placeholders})", values
    )
    return int(cur.lastrowid)


def insert_transactions(conn: sqlite3.Connection, statement_id: int, account_id: int,
                        rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT INTO txn (account_id, statement_id, txn_date, posted_date,
                            description_raw, merchant_normalized,
                            amount_minor, currency, amount_sgd_minor,
                            direction, source_page, reference, dedup_key)
           VALUES (:account_id, :statement_id, :txn_date, :posted_date,
                   :description_raw, :merchant_normalized,
                   :amount_minor, :currency, :amount_sgd_minor,
                   :direction, :source_page, :reference, :dedup_key)""",
        [dict(r, account_id=account_id, statement_id=statement_id) for r in rows],
    )


def renormalize_merchants(conn: sqlite3.Connection) -> int:
    """Recompute merchant_normalized wherever it no longer matches the parser.

    A merchant key is a pure function of description_raw, so this is safe to
    run on every boot: it is a few milliseconds over a few thousand rows, and
    it means improving `merchants.py` re-keys the statements already uploaded
    instead of leaving them on the old rules. Same argument as keeping
    `raw_extraction` — deterministic work can always be replayed, so nothing
    has to be re-uploaded to benefit from a fix.

    Returns how many rows moved, which is the number worth showing: on a normal
    boot it is 0, and anything else means the rules just changed under data
    that was already categorized.
    """
    updates = []
    for row in conn.execute("SELECT id, description_raw, merchant_normalized FROM txn"):
        key = merchants.normalize(row["description_raw"])
        if key != row["merchant_normalized"]:
            updates.append((key, row["id"]))
    conn.executemany("UPDATE txn SET merchant_normalized = ? WHERE id = ?", updates)
    return len(updates)


def delete_statement(conn: sqlite3.Connection, statement_id: int) -> str | None:
    """Hard delete, per DESIGN.md §7. Returns the stored file path to unlink."""
    row = conn.execute("SELECT storage_path FROM statement WHERE id = ?", (statement_id,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM txn WHERE statement_id = ?", (statement_id,))
    conn.execute("DELETE FROM statement WHERE id = ?", (statement_id,))
    return row["storage_path"]


# ------------------------------------------------------------------- reads

def list_statements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, a.issuer, a.last4,
                  (SELECT COUNT(*) FROM txn WHERE statement_id = s.id) AS txn_count,
                  (SELECT MIN(txn_date) FROM txn WHERE statement_id = s.id) AS first_txn,
                  (SELECT MAX(txn_date) FROM txn WHERE statement_id = s.id) AS last_txn
           FROM statement s JOIN account a ON a.id = s.account_id
           ORDER BY COALESCE(s.period_end, s.statement_date, '') DESC, s.uploaded_at DESC"""
    ).fetchall()


def get_statement(conn: sqlite3.Connection, statement_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT s.*, a.issuer, a.last4,
                  (SELECT MIN(txn_date) FROM txn WHERE statement_id = s.id) AS first_txn,
                  (SELECT MAX(txn_date) FROM txn WHERE statement_id = s.id) AS last_txn
           FROM statement s JOIN account a ON a.id = s.account_id WHERE s.id = ?""",
        (statement_id,),
    ).fetchone()


def transactions_for(conn: sqlite3.Connection, statement_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM txn WHERE statement_id = ? ORDER BY txn_date, id", (statement_id,)
    ).fetchall()


def totals_for(conn: sqlite3.Connection, statement_id: int) -> dict[str, int]:
    row = conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN direction = 'debit'  THEN amount_sgd_minor END), 0) AS debits,
             COALESCE(SUM(CASE WHEN direction = 'credit' THEN amount_sgd_minor END), 0) AS credits
           FROM txn WHERE statement_id = ?""",
        (statement_id,),
    ).fetchone()
    return {"debits": row["debits"], "credits": row["credits"]}
