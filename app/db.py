"""SQLite schema and access. Single-user, single file, no ORM.

Money is stored as integer minor units everywhere — never a float. SQLite has
no decimal type and its REAL is IEEE754, so `0.1 + 0.2` in a SUM would drift a
statement out of reconciliation for reasons that have nothing to do with the
parser. Dates are ISO strings; a transaction date has no timezone.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import categorize
import merchants
import months
import rows

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
CREATE INDEX IF NOT EXISTS txn_by_merchant ON txn(merchant_normalized);

-- Tier 1 (DESIGN.md §3): the user's own patterns, which always win. Kept
-- separate from merchant_memory because a rule is a standing instruction and
-- memory is an observation — a rule survives being contradicted by a later
-- click, and that is the whole point of it being tier 1.
CREATE TABLE IF NOT EXISTS merchant_rule (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT NOT NULL,
    match_type  TEXT NOT NULL DEFAULT 'contains'
                CHECK (match_type IN ('exact', 'contains', 'regex')),
    category    TEXT,
    flow_type   TEXT,
    -- Higher first. Ties break by id, so the older rule wins and adding a rule
    -- never silently changes what an existing one was already doing.
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tier 2: what the app has learned, keyed by the merchant key from
-- merchants.py. `source` separates a decision the user made from a seeded
-- guess shipped with the app — they are not the same claim, and the UI says
-- so. A user decision overwrites a seed permanently; a seed never overwrites a
-- user decision.
-- No flow_type column here, unlike merchant_rule, and §5 has it that way for a
-- reason: a category is a property of the merchant, but whether a particular
-- row was a purchase or a refund is a property of the row. Learning "Uniqlo is
-- Shopping" from one purchase must not then declare every future Uniqlo refund
-- to be spending.
-- `llm` is tier 3's own source, and it is a third kind of claim rather than a
-- variety of the other two: a seed is the app's guess about everyone, memory is
-- this user's decision, and llm is a model's guess about this user's merchant.
-- Keeping it distinct is what lets the UI show which categories nobody has
-- actually checked, and what stops a correction being written back as though
-- the user had made it.
CREATE TABLE IF NOT EXISTS merchant_memory (
    merchant_normalized TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'memory' CHECK (source IN ('memory', 'seed', 'llm')),
    hit_count   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
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


def widen_memory_source(conn: sqlite3.Connection) -> bool:
    """Let `merchant_memory.source` hold 'llm' on a database made before tier 3.

    SQLite cannot alter a CHECK constraint, so the table has to be rebuilt —
    the one migration here that ADD COLUMN cannot express. Guarded on the stored
    DDL rather than a version number, so it runs exactly once and is a no-op on
    a fresh database that already has the constraint from SCHEMA.

    Nothing references this table by foreign key, which is what makes the
    rebuild a local operation rather than the full twelve-step dance.
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'merchant_memory'"
    ).fetchone()
    if not ddl or "'llm'" in ddl["sql"]:
        return False

    conn.executescript("""
        CREATE TABLE merchant_memory_new (
            merchant_normalized TEXT PRIMARY KEY,
            category    TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'memory'
                        CHECK (source IN ('memory', 'seed', 'llm')),
            hit_count   INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO merchant_memory_new
            SELECT merchant_normalized, category, source, hit_count, updated_at
            FROM merchant_memory;
        DROP TABLE merchant_memory;
        ALTER TABLE merchant_memory_new RENAME TO merchant_memory;
    """)
    return True


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, definition in MIGRATIONS:
            column = definition.split()[0]
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        widen_memory_source(conn)
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
                            amount_minor, currency, amount_sgd_minor, fx_rate,
                            direction, source_page, reference, dedup_key)
           VALUES (:account_id, :statement_id, :txn_date, :posted_date,
                   :description_raw, :merchant_normalized,
                   :amount_minor, :currency, :amount_sgd_minor, :fx_rate,
                   :direction, :source_page, :reference, :dedup_key)""",
        [dict(r, account_id=account_id, statement_id=statement_id) for r in rows],
    )


def backfill_periods(conn: sqlite3.Connection) -> int:
    """Re-read the cycle dates of statements that reported none.

    Only fills gaps — a period the parser already found is never overwritten,
    so this cannot quietly move a statement that was right. Runs off the stored
    `page_text` rather than the PDF, which is exactly what §5 keeps it for: the
    extraction is deterministic, so improving the parser can be replayed over
    what is already uploaded instead of asking for the files again.
    """
    updates = []
    for row in conn.execute(
        """SELECT id, page_text FROM statement
           WHERE (period_start IS NULL OR period_end IS NULL) AND statement_date IS NULL"""
    ):
        ctx = rows.document_context(json.loads(row["page_text"] or "[]"))
        if ctx.period[0] or ctx.statement_date:
            updates.append((ctx.period[0], ctx.period[1], ctx.statement_date, row["id"]))
    conn.executemany(
        """UPDATE statement SET period_start = ?, period_end = ?, statement_date = ?
           WHERE id = ?""", updates)
    return len(updates)


def backfill_foreign_amounts(conn: sqlite3.Connection) -> int:
    """Recover merchant, original amount and rate on rows parsed before the split.

    A foreign charge used to store its own figure as the description — `102.67
    HKD` where the merchant should be — because the issuer prints the name a
    line above and the rate a line below (§4). Those rows reconcile to the cent
    and can never be categorized, which is the quietest kind of wrong.

    Replayed off `page_text` for the same reason as `backfill_periods`, and
    matched on `(date, amount)` within the statement rather than on row order,
    because a re-parse is free to produce a different number of rows than the
    one stored. Only rows whose description still *is* the foreign figure are
    touched: anything the user has since corrected by hand is left alone.
    """
    updates = []
    for stmt in conn.execute(
        """SELECT DISTINCT s.id, s.page_text FROM statement s
           JOIN txn t ON t.statement_id = s.id
           WHERE t.fx_rate IS NULL AND s.page_text IS NOT NULL"""
    ):
        pages = json.loads(stmt["page_text"] or "[]")
        if not pages:
            continue
        ctx = rows.document_context(pages)
        fixed = {}
        for page in pages:
            for t in rows.parse_page(page, ctx.period, ctx.year, ctx.statement_date)["transactions"]:
                if (t.get("foreign") or {}).get("currency"):
                    fixed[(t["date"], t["amount"])] = t
        if not fixed:
            continue
        for row in conn.execute(
            """SELECT id, txn_date, amount_sgd_minor, description_raw FROM txn
               WHERE statement_id = ? AND fx_rate IS NULL""", (stmt["id"],)
        ):
            t = fixed.get((row["txn_date"], str(to_major(row["amount_sgd_minor"]))))
            if not t or not rows._foreign_amount(row["description_raw"]):
                continue
            updates.append((t["description"], merchants.normalize(t["description"]),
                            to_minor(t["foreign"]["amount"]), t["foreign"]["currency"],
                            t.get("fx_rate"), row["id"]))
    conn.executemany(
        """UPDATE txn SET description_raw = ?, merchant_normalized = ?,
                          amount_minor = ?, currency = ?, fx_rate = ?
           WHERE id = ?""", updates)
    return len(updates)


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


# -------------------------------------------------------- categorization

def seed_memory(conn: sqlite3.Connection) -> int:
    """Put the shipped guesses into tier 2, once, without ever overwriting.

    `INSERT OR IGNORE` is doing the load-bearing work: a key the user has
    already decided on is left exactly as it is, on every boot, forever. A seed
    is only ever allowed to fill a gap.
    """
    cur = conn.executemany(
        """INSERT OR IGNORE INTO merchant_memory (merchant_normalized, category, source)
           VALUES (?, ?, 'seed')""",
        list(categorize.SEED_MEMORY.items()),
    )
    return cur.rowcount


def merchant_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tier 1, highest priority first; ties to the older rule."""
    return conn.execute(
        "SELECT * FROM merchant_rule ORDER BY priority DESC, id ASC"
    ).fetchall()


def memory_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        r["merchant_normalized"]: {"category": r["category"], "source": r["source"]}
        for r in conn.execute("SELECT * FROM merchant_memory")
    }


def recategorize_all(conn: sqlite3.Connection) -> int:
    """Re-resolve every row the user did not set by hand. Returns rows moved.

    Same argument as `renormalize_merchants`: the resolution is a pure function
    of the row plus the rules plus the memory, so it can be replayed whenever
    any of those change, and a new rule reaches the statements already uploaded
    instead of only the next one.

    `category_source = 'user'` is the one thing this will not touch. A person
    who clicked a category on a specific row has said something more specific
    than any rule, and having that quietly reverted on the next boot would make
    the app untrustworthy in exactly the way §2.3 is about.
    """
    rules = merchant_rules(conn)
    memory = memory_map(conn)
    updates, hits = [], Counter()

    for row in conn.execute(
        """SELECT id, description_raw, merchant_normalized, direction,
                  category, flow_type, category_source FROM txn"""
    ):
        if row["category_source"] == "user":
            continue
        merchant = row["merchant_normalized"] or ""
        category, flow, source = categorize.resolve(
            row["description_raw"], merchant, row["direction"], rules, memory)
        if source in ("memory", "seed"):
            hits[merchant if merchant in memory else merchants.merchant_root(merchant)] += 1
        if (category, flow, source) != (row["category"], row["flow_type"], row["category_source"]):
            updates.append((category, flow, source, row["id"]))

    conn.executemany(
        "UPDATE txn SET category = ?, flow_type = ?, category_source = ? WHERE id = ?",
        updates,
    )
    # hit_count is what makes a stale seed visible: an entry nothing matches is
    # a guess about a merchant this user does not have.
    conn.executemany(
        "UPDATE merchant_memory SET hit_count = ? WHERE merchant_normalized = ?",
        [(hits.get(key, 0), key) for key in memory],
    )
    return len(updates)


def remember(conn: sqlite3.Connection, merchant: str, category: str) -> None:
    """Write a user decision into tier 2, replacing whatever was there.

    §3: every recategorization feeds the memory, which is the loop that makes
    the app feel smart by month three. The source flips to `memory` here, so a
    seeded guess that gets corrected stops being labelled a guess.
    """
    conn.execute(
        """INSERT INTO merchant_memory (merchant_normalized, category, source, updated_at)
           VALUES (?, ?, 'memory', datetime('now'))
           ON CONFLICT(merchant_normalized) DO UPDATE SET
               category = excluded.category,
               source = 'memory',
               updated_at = excluded.updated_at""",
        (merchant, category),
    )


def remember_llm(conn: sqlite3.Connection, assignments: dict[str, str]) -> int:
    """Write tier 3's answers into memory as guesses, filling gaps only.

    `INSERT OR IGNORE` is doing the same load-bearing work it does in
    `seed_memory`, and for a stronger reason: a model must never overwrite
    something the user decided. Tier 3 is only ever asked about merchants
    nothing knows, so in practice every row here is a gap — the OR IGNORE is
    what makes that a property of the schema rather than of the caller getting
    the query right.

    Stored as `llm`, never `memory`. The distinction is the whole safety
    argument for letting a model write at all: these categories are visible as
    unreviewed on /merchants and /rules, and the moment the user confirms or
    corrects one it becomes a real `memory` entry through `remember()`.
    """
    if not assignments:
        return 0
    cur = conn.executemany(
        """INSERT OR IGNORE INTO merchant_memory (merchant_normalized, category, source)
           VALUES (?, ?, 'llm')""",
        list(assignments.items()),
    )
    return cur.rowcount


def set_category(conn: sqlite3.Connection, txn_id: int, category: str | None,
                 flow_type: str, apply_to_matching: bool) -> tuple[int, str]:
    """Apply a user's choice to one row, and optionally to its siblings.

    Returns (rows changed, merchant key). The chosen row is always marked
    `user`; the siblings are marked `memory`, because they were not individually
    decided and should keep following the memory entry if it changes later.

    Siblings already marked `user` are left alone. An earlier explicit decision
    outranks a bulk apply — the user can still open that row and change it.

    **`apply_to_matching` also controls whether this is remembered at all**, and
    that is a deliberate reading of §3's "write it back to tier 2 *and* offer to
    apply it to past matches". Those cannot be two independent choices: memory
    feeds `recategorize_all`, so anything written here reaches the past rows on
    the next pass whatever the checkbox said. Rather than let the checkbox
    quietly do nothing, it means what a person would expect it to mean —
    remember this merchant and move every row like it, or touch this one row
    only.
    """
    row = conn.execute(
        """SELECT merchant_normalized, description_raw, direction
           FROM txn WHERE id = ?""", (txn_id,)).fetchone()
    if not row:
        return 0, ""
    merchant = row["merchant_normalized"] or ""

    # Clearing the category and leaving the flow at what the parser derived is
    # how a person says "forget what I told you about this row", so it goes back
    # under automatic resolution rather than being frozen as a user decision
    # with nothing in it — a state nothing could ever move again. Any other
    # combination is a real choice and is marked as one.
    derived = categorize.default_flow(row["description_raw"], row["direction"])
    source = None if (category is None and flow_type == derived) else "user"

    conn.execute(
        "UPDATE txn SET category = ?, flow_type = ?, category_source = ? WHERE id = ?",
        (category, flow_type, source, txn_id),
    )
    changed = 1

    if category and apply_to_matching and merchant:
        remember(conn, merchant, category)
    if apply_to_matching and merchant:
        # flow_type is deliberately not applied to the siblings: it is a
        # property of the individual row, and a refund sitting among purchases
        # at the same merchant must stay a refund.
        # COALESCE, not `category_source <> 'user'`: an uncategorized row has
        # NULL there, and `NULL <> 'user'` is NULL rather than true, so the
        # plain comparison skips exactly the rows this is for. It looked like it
        # worked only because recategorize_all() picked them up a moment later
        # through the memory entry — while the count reported back to the user
        # said nothing had been applied.
        cur = conn.execute(
            """UPDATE txn SET category = ?, category_source = 'memory'
               WHERE merchant_normalized = ? AND id <> ?
                 AND COALESCE(category_source, '') <> 'user'""",
            (category, merchant, txn_id),
        )
        changed += cur.rowcount
    return changed, merchant


def add_rule(conn: sqlite3.Connection, pattern: str, match_type: str,
             category: str | None, flow_type: str | None, priority: int) -> None:
    conn.execute(
        """INSERT INTO merchant_rule (pattern, match_type, category, flow_type, priority)
           VALUES (?, ?, ?, ?, ?)""",
        (pattern.strip(), match_type, category or None, flow_type or None, priority),
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM merchant_rule WHERE id = ?", (rule_id,))


def forget_merchant(conn: sqlite3.Connection, merchant: str) -> None:
    conn.execute("DELETE FROM merchant_memory WHERE merchant_normalized = ?", (merchant,))


def delete_statement(conn: sqlite3.Connection, statement_id: int) -> str | None:
    """Hard delete, per DESIGN.md §7. Returns the stored file path to unlink."""
    row = conn.execute("SELECT storage_path FROM statement WHERE id = ?", (statement_id,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM txn WHERE statement_id = ?", (statement_id,))
    conn.execute("DELETE FROM statement WHERE id = ?", (statement_id,))
    return row["storage_path"]


# ------------------------------------------------------------------- reads

# The orderings the statement list offers, as a fixed map. The key arrives
# from a query string, so it is looked up here and never interpolated: an
# ORDER BY is the one clause that cannot take a bound parameter, which makes it
# the one place a sort control invites string-building into SQL.
#
# `period` sorts by where a statement *starts*, and most issuers never print
# that — six of the ten here give a closing date and nothing else. The fallback
# is the earliest row parsed off the statement, which is the same floor the
# `period` column already displays and labels "(from rows)". A floor is enough
# to order by; it is not the cycle start and is not claimed to be.
#
# This is deliberately not `months.statement_windows()`, which infers a true
# start by tiling consecutive cycles. That inference is right for a coverage
# figure someone will trust, and too much machinery for deciding which row
# draws first.
STATEMENT_ORDERS = {
    "newest": "COALESCE(s.period_end, s.statement_date, '') {d}, s.uploaded_at DESC",
    "statement": "a.issuer {d}, a.last4 {d}, period_start_floor ASC",
    "period": "period_start_floor {d}, a.issuer ASC",
}


def list_statements(conn: sqlite3.Connection, sort: str = "newest",
                    descending: bool = True) -> list[sqlite3.Row]:
    """The statement list, ordered by one of `STATEMENT_ORDERS`.

    An unknown `sort` falls back to the default rather than raising: the value
    comes from a URL, and a stale bookmark should show the list.
    """
    order = STATEMENT_ORDERS.get(sort, STATEMENT_ORDERS["newest"])
    return conn.execute(
        f"""SELECT s.*, a.issuer, a.last4,
                  (SELECT COUNT(*) FROM txn WHERE statement_id = s.id) AS txn_count,
                  (SELECT MIN(txn_date) FROM txn WHERE statement_id = s.id) AS first_txn,
                  (SELECT MAX(txn_date) FROM txn WHERE statement_id = s.id) AS last_txn,
                  COALESCE(s.period_start,
                           (SELECT MIN(txn_date) FROM txn WHERE statement_id = s.id),
                           '') AS period_start_floor
           FROM statement s JOIN account a ON a.id = s.account_id
           ORDER BY {order.format(d='DESC' if descending else 'ASC')}"""
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


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """Which tier answered, over every transaction stored.

    §3 predicts roughly 10% from rules and 75% from memory once warm. Showing
    the real split is how you find out whether that is happening — and the
    `none` bucket is the honest size of the gap tier 3 (or the user) has to
    close, rather than a pile of rows quietly filed under Other.
    """
    by_source = {r["src"] or "none": r["n"] for r in conn.execute(
        "SELECT category_source AS src, COUNT(*) AS n FROM txn GROUP BY 1")}
    by_flow = {r["flow"] or "unset": r["n"] for r in conn.execute(
        "SELECT flow_type AS flow, COUNT(*) AS n FROM txn GROUP BY 1")}
    total = sum(by_source.values())
    return {
        "total": total,
        "by_source": by_source,
        "by_flow": by_flow,
        "categorized": total - by_source.get("none", 0),
        "spend_minor": conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN flow_type = 'spend'  THEN amount_sgd_minor
                                        WHEN flow_type = 'refund' THEN -amount_sgd_minor
                                   END), 0) AS n FROM txn"""
        ).fetchone()["n"],
    }


def coverage_by_account(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """Merged covered windows per card, keyed by a human label."""
    per_account: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        """SELECT a.id, a.issuer, a.last4, s.period_start, s.period_end, s.statement_date,
                  (SELECT MIN(txn_date) FROM txn WHERE statement_id = s.id) AS first_txn
           FROM statement s JOIN account a ON a.id = s.account_id"""
    ):
        label = f'{r["issuer"]}{" ····" + r["last4"] if r["last4"] else ""}'
        per_account.setdefault(label, []).append(dict(r))
    return {label: months.statement_windows(rows_) for label, rows_ in per_account.items()}


def month_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per calendar month, bucketed by transaction date (§4).

    Each month carries its own completeness, and a like-for-like figure: when a
    month is only billed to the 14th, the honest comparison is against the
    earlier month's first fourteen days, not against the whole of it.
    """
    coverage = coverage_by_account(conn)

    totals = {
        r["m"]: dict(r) for r in conn.execute(
            """SELECT substr(txn_date, 1, 7) AS m, COUNT(*) AS rows_total,
                      COALESCE(SUM(CASE WHEN flow_type = 'spend'  THEN amount_sgd_minor
                                        WHEN flow_type = 'refund' THEN -amount_sgd_minor
                                        ELSE 0 END), 0) AS spend_minor,
                      SUM(CASE WHEN category IS NULL THEN 1 ELSE 0 END) AS uncategorized,
                      COUNT(DISTINCT account_id) AS cards_seen
               FROM txn GROUP BY 1 ORDER BY 1""")
    }

    def spend_in_days(ym: str, days: tuple[int, int] | None) -> int:
        if days is None:
            return conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN flow_type = 'spend'  THEN amount_sgd_minor
                                            WHEN flow_type = 'refund' THEN -amount_sgd_minor
                                            ELSE 0 END), 0) AS n
                   FROM txn WHERE substr(txn_date, 1, 7) = ?""", (ym,)).fetchone()["n"]
        return conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN flow_type = 'spend'  THEN amount_sgd_minor
                                        WHEN flow_type = 'refund' THEN -amount_sgd_minor
                                        ELSE 0 END), 0) AS n
               FROM txn WHERE substr(txn_date, 1, 7) = ?
                 AND CAST(substr(txn_date, 9, 2) AS INTEGER) BETWEEN ? AND ?""",
            (ym, days[0], days[1])).fetchone()["n"]

    out = []
    for ym in sorted(totals):
        status = months.month_completeness(ym, coverage)
        days = months.comparable_days(status)
        row = dict(totals[ym], **status)
        row["comparable_days"] = days
        row["spend_comparable"] = spend_in_days(ym, days)
        out.append(row)

    # Deltas last, and only where both sides are genuinely covered over the same
    # days. The subtle failure this guards is not the obvious one: it is easy to
    # remember that *this* month may be part-billed, and easy to forget that the
    # month being compared against may be too. August 1-14 against July 1-14
    # looks like a fair comparison and is not, because one card has no statement
    # covering early July — the delta would flatter August by whatever that card
    # spent. No number is better than a number that is wrong in an invisible
    # direction, so the reason is carried instead and the report says which
    # statement would fix it.
    for i, row in enumerate(out):
        prior = out[i - 1] if i else None
        row["prior"] = prior["month"] if prior else None
        row["prior_comparable"] = None
        row["delta_minor"] = None
        row["not_comparable"] = None

        if not prior:
            row["not_comparable"] = "nothing earlier to compare against"
            continue
        if not (row["is_complete"] or row["comparable_days"]):
            row["not_comparable"] = "this month cannot be bounded"
            continue

        days = row["comparable_days"] or (1, int(row["end"][8:10]))
        prior_days = months.covered_day_range(prior)
        if prior_days is None:
            gap = ", ".join(prior["missing"] + prior["gaps"]) or "a card"
            row["not_comparable"] = f"{prior['month']} has no statement covering it for {gap}"
            continue
        # `covers_days` rather than a raw day-number comparison: a complete
        # June is billed 1–30 and can never satisfy "days 1–31", so a fair
        # comparison between two complete months used to refuse itself and
        # blame the calendar on a missing statement.
        if not months.covers_days(prior, days):
            row["not_comparable"] = (
                f"{prior['month']} is only billed for days "
                f"{prior_days[0]}–{prior_days[1]}, so the same days cannot be compared")
            continue

        base = spend_in_days(prior["month"], row["comparable_days"])
        row["prior_comparable"] = base
        row["delta_minor"] = row["spend_comparable"] - base

    # The three-month average §4 asks for. Which months are allowed into it is
    # the part that decides whether the figure means anything, so that decision
    # lives in `months.trailing_window` beside the rest of the coverage
    # reasoning and is tested as a pure function.
    for i, row in enumerate(out):
        usable, short, note = months.trailing_window(row, out[:i])
        row["trailing_months"] = [m["month"] for m in usable]
        row["trailing_short"] = short
        row["trailing_note"] = note
        row["trailing_avg_minor"] = None if not usable else round(
            sum(spend_in_days(m["month"], row["comparable_days"]) for m in usable) / len(usable))
    return list(reversed(out))


def month_notable(conn: sqlite3.Connection, ym: str, trailing: list[str],
                  days: tuple[int, int] | None) -> dict[str, Any]:
    """§4's "new and unusual": merchants never seen, categories running hot.

    Two questions with different standards of proof, deliberately not merged.

    **A new merchant** is one with no earlier row anywhere in the data. That is
    honest without needing a complete month, but it is "new to the statements
    you have uploaded" and not "new to your spending" — an unbilled fortnight
    can hide a first visit. The UI says so rather than the name implying it.

    **A category running hot** needs an average to be hot against, so it is
    held to the trailing test in `month_report`: the same days, in months
    billed across them. Given nothing qualifying, nothing is reported — a
    category flagged against a part-billed average is flagged for being
    compared with half a month, which is a fact about the upload and not about
    the spending.
    """
    start, end = months.month_bounds(ym)
    spend = """CASE WHEN t.flow_type='spend' THEN t.amount_sgd_minor
                    WHEN t.flow_type='refund' THEN -t.amount_sgd_minor ELSE 0 END"""

    new_merchants = conn.execute(
        f"""SELECT t.merchant_normalized AS label, COUNT(*) AS n,
                   COALESCE(SUM({spend}), 0) AS v
            FROM txn t
            WHERE t.txn_date BETWEEN ? AND ? AND t.merchant_normalized <> ''
              AND NOT EXISTS (SELECT 1 FROM txn p
                              WHERE p.merchant_normalized = t.merchant_normalized
                                AND p.txn_date < ?)
            GROUP BY 1 HAVING v > 0 ORDER BY v DESC LIMIT 12""",
        (start, end, start)).fetchall()

    hot: list[dict[str, Any]] = []
    if len(trailing) >= 2 and days:
        day_clause = "AND CAST(substr(t.txn_date, 9, 2) AS INTEGER) BETWEEN ? AND ?"
        now = {r["label"]: r["v"] for r in conn.execute(
            f"""SELECT t.category AS label, COALESCE(SUM({spend}), 0) AS v FROM txn t
                WHERE substr(t.txn_date, 1, 7) = ? {day_clause}
                  AND t.category IS NOT NULL GROUP BY 1""",
            (ym, days[0], days[1]))}
        marks = ",".join("?" * len(trailing))
        before: dict[str, int] = {}
        for r in conn.execute(
            f"""SELECT t.category AS label, COALESCE(SUM({spend}), 0) AS v FROM txn t
                WHERE substr(t.txn_date, 1, 7) IN ({marks}) {day_clause}
                  AND t.category IS NOT NULL GROUP BY 1""",
            (*trailing, days[0], days[1])):
            before[r["label"]] = r["v"]
        for label, value in now.items():
            base = before.get(label, 0) / len(trailing)
            # A category with no history is new rather than hot, and dividing
            # by its absent average is how a $3 first coffee becomes an
            # infinite overspend. It belongs to the merchant list above.
            if base <= 0 or value <= base * 1.5:
                continue
            hot.append({"label": label, "v": value, "base": round(base),
                        "over": round(100 * (value - base) / base)})
        hot.sort(key=lambda h: -h["over"])

    return {"new_merchants": new_merchants, "hot_categories": hot}


def month_detail(conn: sqlite3.Connection, ym: str) -> dict[str, Any]:
    """Category, card and merchant breakdown for one calendar month."""
    start, end = months.month_bounds(ym)
    args = (start, end)

    def rows_for(sql: str) -> list[sqlite3.Row]:
        return conn.execute(sql, args).fetchall()

    return {
        "by_category": rows_for(
            """SELECT COALESCE(t.category, '(uncategorized)') AS label, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN t.flow_type='spend' THEN t.amount_sgd_minor
                                        WHEN t.flow_type='refund' THEN -t.amount_sgd_minor
                                        ELSE 0 END), 0) AS v
               FROM txn t WHERE t.txn_date BETWEEN ? AND ?
               GROUP BY 1 ORDER BY v DESC"""),
        "by_card": rows_for(
            """SELECT a.issuer || CASE WHEN a.last4 IS NULL THEN '' ELSE ' ····' || a.last4 END
                        AS label, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN t.flow_type='spend' THEN t.amount_sgd_minor
                                        WHEN t.flow_type='refund' THEN -t.amount_sgd_minor
                                        ELSE 0 END), 0) AS v
               FROM txn t JOIN account a ON a.id = t.account_id
               WHERE t.txn_date BETWEEN ? AND ? GROUP BY 1 ORDER BY v DESC"""),
        "by_merchant": rows_for(
            """SELECT t.merchant_normalized AS label, COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN t.flow_type='spend' THEN t.amount_sgd_minor
                                        WHEN t.flow_type='refund' THEN -t.amount_sgd_minor
                                        ELSE 0 END), 0) AS v
               FROM txn t WHERE t.txn_date BETWEEN ? AND ?
               GROUP BY 1 HAVING v > 0 ORDER BY v DESC LIMIT 12"""),
        "excluded": rows_for(
            """SELECT t.flow_type AS label, COUNT(*) AS n,
                      COALESCE(SUM(t.amount_sgd_minor), 0) AS v
               FROM txn t WHERE t.txn_date BETWEEN ? AND ? AND t.flow_type <> 'spend'
               GROUP BY 1 ORDER BY v DESC"""),
    }


def merchant_summary(conn: sqlite3.Connection, unknown_only: bool = True) -> list[dict[str, Any]]:
    """One row per merchant key, for categorizing in bulk.

    The backlog is far smaller than it looks: 103 uncategorized rows in the
    corpus are 43 merchants, and five of them account for 59 rows. Deciding once
    per merchant instead of once per transaction is the difference between a
    few minutes and an afternoon — and it is also how the decision gets stored,
    since merchant_memory is keyed by merchant, not by row.

    `value_minor` is net spend, not the gross sum: a merchant whose rows are all
    transfers has moved a lot of money and spent none of it, and sorting it to
    the top of a spending screen would be a lie about where the money went.
    """
    having = "HAVING unknown > 0" if unknown_only else ""
    rows = conn.execute(
        f"""SELECT t.merchant_normalized               AS key,
                   COUNT(*)                            AS rows_total,
                   SUM(CASE WHEN t.category IS NULL THEN 1 ELSE 0 END) AS unknown,
                   SUM(CASE WHEN t.flow_type = 'spend'  THEN t.amount_sgd_minor
                            WHEN t.flow_type = 'refund' THEN -t.amount_sgd_minor
                            ELSE 0 END)                AS value_minor,
                   MIN(t.txn_date)                     AS first_seen,
                   MAX(t.txn_date)                     AS last_seen,
                   MAX(t.description_raw)              AS example,
                   MAX(t.category)                     AS current_category,
                   m.source                            AS memory_source
            FROM txn t
            LEFT JOIN merchant_memory m ON m.merchant_normalized = t.merchant_normalized
            WHERE t.merchant_normalized IS NOT NULL AND t.merchant_normalized <> ''
            GROUP BY t.merchant_normalized
            {having}"""
    ).fetchall()
    # Cluster in Python rather than SQL: the ordering wants each root's combined
    # weight, which is a second pass over the same rows and reads far better as
    # a tested function than as a correlated subquery.
    return merchants.cluster_order([dict(r, weight=abs(r["value_minor"] or 0)) for r in rows])


def list_memory(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM merchant_memory
           ORDER BY hit_count DESC, source ASC, merchant_normalized ASC"""
    ).fetchall()
