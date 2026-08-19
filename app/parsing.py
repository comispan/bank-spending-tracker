"""PDF -> verified transactions.

Everything here runs offline. `rows.py` does the parsing; this module wraps it
with the parts the spike proved were necessary around it: decryption, card-number
redaction, the reconciliation gate, and the independent row count that catches a
parser silently dropping rows.

Ported from spike/extract.py, which remains the regression harness — it runs the
same parser against real statements and reports a verdict per bank.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
import pypdf

import rows

PARSER_VERSION = "1.0.0"
TOLERANCE = Decimal("0.01")       # a cent of rounding slack
SCAN_CHAR_THRESHOLD = 100         # below this, the page is almost certainly an image
DATE_SLACK_DAYS = 5               # posting lag outside the statement period

# Independent of rows.py on purpose: this is the check that the parser found
# everything the page contains, so it must not share the parser's code. When
# these two counts disagree, one of them is wrong and you want to know.
TXN_DATE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[ /-](?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:[ /-]\d{2,4})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ /-]\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b", re.I)
TXN_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}\s*(?:cr|dr)?\s*$", re.I)

# Issuer detection. rows.py deliberately doesn't guess at this — it parses what
# is printed, and the issuer is printed as a logo as often as as text. A short
# keyword list over the first page is honest about what it is, and the fallback
# is the filename rather than a wrong guess.
ISSUERS = [
    ("DBS", r"\bDBS\b|\bPOSB\b"),
    ("UOB", r"\bUOB\b|United Overseas Bank"),
    ("OCBC", r"\bOCBC\b|Oversea-Chinese"),
    ("Standard Chartered", r"Standard Chartered|\bSCB\b"),
    ("Citibank", r"\bCiti(?:bank)?\b"),
    ("HSBC", r"\bHSBC\b"),
    ("Maybank", r"\bMaybank\b"),
    ("American Express", r"American Express|\bAMEX\b"),
    ("Trust", r"\bTrust Bank\b|\bTrust\b"),
    ("MariBank", r"\bMari ?Bank\b|\bMARI CREDIT\b"),
]
ISSUER_RE = [(name, re.compile(pat, re.I)) for name, pat in ISSUERS]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact(text: str) -> str:
    """Strip full card numbers. Only the last 4 is ever needed.

    Runs before anything is stored or written to disk, so nothing downstream —
    database, page-text dump, review screen — ever holds a full card number.
    """
    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "*" * (len(digits) - 4) + digits[-4:]

    return re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _mask, text)


@dataclass
class Page:
    number: int
    text: str
    char_count: int

    @property
    def looks_scanned(self) -> bool:
        return self.char_count < SCAN_CHAR_THRESHOLD


@dataclass
class ParseResult:
    filename: str
    pages: list[Page] = field(default_factory=list)
    issuer: str = "Unknown"
    statement: dict = field(default_factory=dict)
    transactions: list[dict] = field(default_factory=list)
    statement_date: str | None = None
    verdict: str = "error"
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def scanned_pages(self) -> int:
        return sum(1 for p in self.pages if p.looks_scanned)

    @property
    def rows_expected(self) -> int:
        return sum(
            1
            for p in self.pages
            for line in p.text.splitlines()
            if TXN_DATE.match(line) and TXN_AMOUNT.search(line)
        )

    @property
    def status(self) -> str:
        if self.verdict == "error":
            return "failed"
        return "parsed" if self.verdict == "pass" else "needs_review"


def read_pdf(path: Path, password: str | None = None) -> tuple[list[Page], str | None]:
    """Return (pages, error). Decrypts first if needed."""
    source, tmp = path, None

    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted:
        # Plenty of issuers encrypt a statement purely to set permission flags
        # (no printing, no copying) and leave the user password empty, so the
        # file opens with no password at all. Always try that first — DBS does it.
        if reader.decrypt("") == 0:
            if not password:
                return [], "encrypted (needs a password)"
            if reader.decrypt(password) == 0:
                return [], "encrypted (supplied password rejected)"

        # pdfplumber can't take an already-decrypted pypdf reader, so write a
        # decrypted copy to a temp path and read that. The password itself is
        # never stored — it only exists for the life of this call.
        writer = pypdf.PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        tmp = Path(tempfile.mkdtemp()) / "decrypted.pdf"
        with open(tmp, "wb") as fh:
            writer.write(fh)
        source = tmp

    try:
        pages: list[Page] = []
        with pdfplumber.open(str(source)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                # layout=True keeps column alignment, which is the whole point.
                # Plain extract_text() interleaves columns and destroys the table.
                raw = page.extract_text(layout=True) or ""
                pages.append(Page(number=i, text=redact(raw), char_count=len(raw.strip())))
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()

    return pages, None


def detect_issuer(pages: list[Page], filename: str) -> str:
    head = "\n".join(p.text for p in pages[:1])
    for name, pat in ISSUER_RE:
        if pat.search(head):
            return name
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def merge(pages: list[dict]) -> dict:
    """Concatenate transactions; take each summary field from the first page that has it."""
    out: dict = {"transactions": []}
    scalars = ["issuer", "account_last4", "statement_period_start", "statement_period_end",
               "currency", "opening_balance", "closing_balance", "total_debits", "total_credits"]
    for page in pages:
        out["transactions"].extend(page.get("transactions", []))
        for key in scalars:
            if out.get(key) is None and page.get(key) is not None:
                out[key] = page[key]
    return out


def reconcile(stmt: dict, result: ParseResult) -> None:
    """The trust gate. Accept only if the numbers agree with the statement itself."""
    txns = stmt.get("transactions", [])
    debits = sum((dec(t["amount"]) or Decimal(0) for t in txns if t["direction"] == "debit"), Decimal(0))
    credits = sum((dec(t["amount"]) or Decimal(0) for t in txns if t["direction"] == "credit"), Decimal(0))

    td, tc = dec(stmt.get("total_debits")), dec(stmt.get("total_credits"))
    ob, cb = dec(stmt.get("opening_balance")), dec(stmt.get("closing_balance"))

    # Rule 1: printed totals.
    if td is not None or tc is not None:
        deltas = []
        if td is not None:
            deltas.append(("debits", abs(debits - td)))
        if tc is not None:
            deltas.append(("credits", abs(credits - tc)))
        if max(d for _, d in deltas) <= TOLERANCE:
            result.verdict, result.detail = "pass", "matches printed totals"
            return
        result.verdict = "fail"
        result.detail = "; ".join(f"{k} off by {d}" for k, d in deltas if d > TOLERANCE)
        return

    # Rule 2: balance roll-forward. The sign convention differs between a credit
    # card (charges raise the balance) and a deposit account (they lower it), so
    # accept either orientation and record which one held.
    if ob is not None and cb is not None:
        card = abs((ob + debits - credits) - cb)
        bank = abs((ob - debits + credits) - cb)
        if min(card, bank) <= TOLERANCE:
            result.verdict = "pass"
            result.detail = "balance rolls forward (%s convention)" % ("card" if card <= bank else "deposit")
            return
        result.verdict, result.detail = "fail", f"balance off by {min(card, bank)}"
        return

    result.verdict = "unverified"
    result.detail = "statement prints no totals or balances to check against"


def sanity_checks(stmt: dict, result: ParseResult) -> None:
    txns = stmt.get("transactions", [])
    if not txns:
        result.warnings.append("no transactions found")

    start, end = stmt.get("statement_period_start"), stmt.get("statement_period_end")
    if start and end:
        try:
            lo = dt.date.fromisoformat(start) - dt.timedelta(days=DATE_SLACK_DAYS)
            hi = dt.date.fromisoformat(end) + dt.timedelta(days=DATE_SLACK_DAYS)
            stray = [t["date"] for t in txns if not (lo <= dt.date.fromisoformat(t["date"]) <= hi)]
            if stray:
                result.warnings.append(f"{len(stray)} date(s) outside the statement period, e.g. {stray[0]}")
        except ValueError:
            result.warnings.append("unparseable date in the period or a transaction")

    # Two identical rows in one statement are usually genuine — the same shop
    # twice in a day. Flag, never merge: silently dropping a real purchase is
    # worse than showing one the user can delete.
    seen, dupes = set(), 0
    for t in txns:
        key = (t["date"], t["amount"], t["description"])
        if key in seen:
            dupes += 1
        seen.add(key)
    if dupes:
        result.warnings.append(f"{dupes} identical row(s) — genuine repeats, or a page read twice")

    missing = result.rows_expected - len(txns)
    if missing > 2:
        result.warnings.append(
            f"{missing} date-and-amount line(s) were not parsed as transactions "
            f"({result.rows_expected} found by an independent scan, {len(txns)} parsed)")

    if result.scanned_pages:
        result.warnings.append(
            f"{result.scanned_pages}/{result.page_count} page(s) have no text layer — "
            f"skipped (a scan needs OCR before this can read it)")


def parse(path: Path, filename: str, password: str | None = None) -> ParseResult:
    result = ParseResult(filename=filename)

    pages, err = read_pdf(path, password)
    if err:
        result.verdict, result.detail, result.error = "error", err, err
        return result

    result.pages = pages
    result.issuer = detect_issuer(pages, filename)

    # One pass over the whole document first: the period and year are almost
    # always printed only on page 1, and every later page needs them to date a
    # bare "12 JUN" row.
    ctx = rows.document_context([p.text for p in pages])
    result.statement_date = ctx.statement_date
    extracted, ambiguous = [], 0
    for page in pages:
        if page.looks_scanned:
            continue
        page_data = rows.parse_page(page.text, ctx.period, ctx.year, ctx.statement_date)
        ambiguous += page_data.pop("_ambiguous_rows", 0)
        for txn in page_data["transactions"]:
            txn["source_page"] = page.number
        extracted.append(page_data)

    if ambiguous:
        result.warnings.append(
            f"{ambiguous} row(s) carried more than one amount — a running-balance "
            f"column may have been read as the transaction amount")

    stmt = merge(extracted)
    result.statement = stmt
    result.transactions = stmt["transactions"]
    reconcile(stmt, result)
    sanity_checks(stmt, result)
    return result
