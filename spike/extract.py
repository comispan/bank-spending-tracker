"""
Phase 0 spike: can we extract transactions from a statement PDF and prove it?

Not production code. No DB, no UI, no abstractions worth keeping. The only
question this answers is: does extraction reconcile against the statement's
own printed totals, and on which banks does it fail?

Usage:
    python extract.py --dry-run          # dump the extracted text and stop
    python extract.py                    # parse, reconcile, report
    python extract.py --file foo.pdf     # just one

Everything runs offline. There is no model and no network call: the table is
already laid out correctly by pdfplumber, so it is parsed in code by rows.py.
See that module for why, and README.md for what this replaced.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
import pypdf

import rows

HERE = Path(__file__).parent
STATEMENTS = HERE / "statements"
OUT = HERE / "out"

TOLERANCE = Decimal("0.01")       # a cent of rounding slack
SCAN_CHAR_THRESHOLD = 100         # below this, the page is almost certainly an image
DATE_SLACK_DAYS = 5               # posting lag outside the statement period


# ------------------------------------------------------------------- text io
#
# Every text read and write in this file goes through these two functions.
# Nothing calls Path.read_text/write_text or bare open() in text mode directly,
# because those inherit the platform's locale encoding — cp1252 on Windows —
# and statements are full of characters it cannot represent.

def write_text(path: Path, data: str) -> None:
    """Output is UTF-8 with LF endings on every platform. Not negotiable."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def read_text(path: Path) -> str:
    """Decode a file we did not write.

    We control our own output, but not passwords.json — a Windows editor may
    save it as cp1252 and Notepad prepends a BOM. Try the plausible encodings
    in order rather than making the user care which one they used.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Last resort: never fail a run over an undecodable byte.
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- redaction

# A transaction row, heuristically: starts with a date, ends with an amount.
# Deliberately independent of rows.py — it is the check that the real parser
# found everything the page actually contains, so it must not share its code.
TXN_DATE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[ /-](?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:[ /-]\d{2,4})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ /-]\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b", re.I)
TXN_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}\s*(?:cr|dr)?\s*$", re.I)


def count_txn_shaped_lines(pages: list[Page]) -> int:
    return sum(
        1
        for p in pages
        for line in p.text.splitlines()
        if TXN_DATE.match(line) and TXN_AMOUNT.search(line)
    )


def redact(text: str) -> str:
    """Strip full card numbers before anything leaves the machine.

    Only ever need the last 4. Runs on every page, including in --dry-run,
    so the dry-run output is safe to paste into a bug report.
    """
    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "*" * (len(digits) - 4) + digits[-4:]

    # 13-19 digits, optionally split by spaces or dashes in groups
    return re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _mask, text)


# ------------------------------------------------------------ pdf -> text

@dataclass
class Page:
    number: int
    text: str
    char_count: int

    @property
    def looks_scanned(self) -> bool:
        return self.char_count < SCAN_CHAR_THRESHOLD


def read_pdf(path: Path, password: str | None) -> tuple[list[Page], str | None]:
    """Return (pages, error). Decrypts first if needed."""
    source = path

    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted:
        # Plenty of issuers encrypt a statement purely to set permission flags
        # (no printing, no copying) and leave the user password empty, so the
        # file opens with no password at all. Always try that before asking the
        # user for one — DBS does this.
        if reader.decrypt("") == 0:
            if not password:
                return [], "encrypted (no password supplied — add it to passwords.json)"
            if reader.decrypt(password) == 0:
                return [], "encrypted (supplied password rejected)"

        # pdfplumber can't take an already-decrypted pypdf reader, so write a
        # decrypted copy to a temp path and read that.
        writer = pypdf.PdfWriter()
        for p in reader.pages:
            writer.add_page(p)
        source = OUT / f".decrypted-{path.name}"
        source.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "wb") as fh:
            writer.write(fh)

    pages: list[Page] = []
    with pdfplumber.open(str(source)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            # layout=True keeps column alignment, which is the whole point.
            # Plain extract_text() interleaves columns and destroys the table.
            raw = page.extract_text(layout=True) or ""
            pages.append(Page(number=i, text=redact(raw), char_count=len(raw.strip())))

    if source != path:
        source.unlink(missing_ok=True)

    return pages, None


# -------------------------------------------------------------- reconcile

def dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


@dataclass
class Result:
    name: str
    pages: int = 0
    scanned_pages: int = 0
    txns: int = 0
    verdict: str = "unknown"
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def reconcile(stmt: dict, result: Result) -> None:
    """The trust gate. Accept only if the numbers agree with the statement."""
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
        worst = max(d for _, d in deltas)
        if worst <= TOLERANCE:
            result.verdict = "PASS"
            result.detail = "matches printed totals"
            return
        result.verdict = "FAIL"
        result.detail = "; ".join(f"{k} off by {d}" for k, d in deltas if d > TOLERANCE)
        return

    # Rule 2: balance roll-forward. Sign convention differs between a credit
    # card (charges raise the balance) and a deposit account (they lower it),
    # so accept either orientation and record which one held.
    if ob is not None and cb is not None:
        card = abs((ob + debits - credits) - cb)
        bank = abs((ob - debits + credits) - cb)
        if min(card, bank) <= TOLERANCE:
            result.verdict = "PASS"
            result.detail = "balance rolls forward (%s convention)" % ("card" if card <= bank else "deposit")
            return
        result.verdict = "FAIL"
        result.detail = f"balance off by {min(card, bank)}"
        return

    result.verdict = "UNVERIFIED"
    result.detail = "statement prints no totals or balances to check against"


def sanity_checks(stmt: dict, result: Result) -> None:
    txns = stmt.get("transactions", [])
    if not txns:
        result.warnings.append("no transactions found")

    start, end = stmt.get("statement_period_start"), stmt.get("statement_period_end")
    if start and end:
        import datetime as dt
        try:
            lo = dt.date.fromisoformat(start) - dt.timedelta(days=DATE_SLACK_DAYS)
            hi = dt.date.fromisoformat(end) + dt.timedelta(days=DATE_SLACK_DAYS)
            stray = [t["date"] for t in txns if not (lo <= dt.date.fromisoformat(t["date"]) <= hi)]
            if stray:
                result.warnings.append(f"{len(stray)} date(s) outside period, e.g. {stray[0]}")
        except ValueError:
            result.warnings.append("unparseable date in period or transactions")

    seen, dupes = set(), 0
    for t in txns:
        key = (t["date"], t["amount"], t["description"])
        if key in seen:
            dupes += 1
        seen.add(key)
    if dupes:
        result.warnings.append(f"{dupes} identical row(s) within one statement (page double-read?)")


# ------------------------------------------------------------------ driver

def merge(pages: list[dict]) -> dict:
    """Concatenate transactions; take each summary field from the first page that has it."""
    out: dict = {"transactions": []}
    scalars = ["issuer", "account_last4", "statement_period_start", "statement_period_end",
               "currency", "opening_balance", "closing_balance", "total_debits", "total_credits"]
    for p in pages:
        out["transactions"].extend(p.get("transactions", []))
        for k in scalars:
            if out.get(k) is None and p.get(k) is not None:
                out[k] = p[k]
    return out


def process(path: Path, passwords: dict, dry_run: bool) -> Result:
    r = Result(name=path.name)

    pages, err = read_pdf(path, passwords.get(path.name))
    if err:
        r.verdict = "ERROR"
        r.detail = err
        return r

    r.pages = len(pages)
    r.scanned_pages = sum(1 for p in pages if p.looks_scanned)
    if r.scanned_pages:
        # Parsing text can't reach a page that has none. Reported rather than
        # silently skipped, because otherwise a scan looks like a statement
        # with no transactions — the one failure that would fool the gate.
        r.warnings.append(
            f"{r.scanned_pages}/{r.pages} page(s) have no text layer — "
            f"skipped (a scan needs OCR before this can read it)")

    if dry_run:
        dump = OUT / f"{path.stem}.txt"
        write_text(dump, "\n\n".join(f"===== page {p.number} =====\n{p.text}" for p in pages))
        r.txns = count_txn_shaped_lines(pages)
        if r.txns:
            r.verdict = "TABLE-OK"
            r.detail = f"{r.txns} transaction-shaped rows survived — see {dump.name}"
        else:
            r.verdict = "NO-TABLE"
            r.detail = f"no date+amount rows found — inspect {dump.name}"
        return r

    # One pass over the whole document first: the statement period and the year
    # are almost always printed only on page 1, and every later page needs them
    # to date a bare "12 JUN" row.
    (period_start, period_end), doc_year = rows.document_context([p.text for p in pages])

    extracted = [
        rows.parse_page(p.text, (period_start, period_end), doc_year)
        for p in pages if not p.looks_scanned
    ]

    stmt = merge(extracted)
    ambiguous = sum(p.pop("_ambiguous_rows", 0) for p in extracted)
    if ambiguous:
        r.warnings.append(
            f"{ambiguous} row(s) carried more than one amount — a running-balance "
            f"column may have been read as the transaction amount")

    r.txns = len(stmt["transactions"])
    r.data = stmt
    reconcile(stmt, r)
    sanity_checks(stmt, r)

    write_text(OUT / f"{path.stem}.json", json.dumps(stmt, indent=2, ensure_ascii=False))
    return r


def main() -> int:
    # Windows consoles default to cp1252, which blows up on the symbols banks
    # like to put in statements (✈, é, —). Never let a printable character
    # crash the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="extract text only; nothing leaves this machine")
    ap.add_argument("--file", help="process a single PDF instead of the whole folder")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    STATEMENTS.mkdir(parents=True, exist_ok=True)

    pw_file = HERE / "passwords.json"
    passwords = json.loads(read_text(pw_file)) if pw_file.exists() else {}

    files = [Path(args.file)] if args.file else sorted(STATEMENTS.glob("*.pdf"))
    if not files:
        print(f"No PDFs in {STATEMENTS}/ — drop statements there and re-run.")
        return 1

    if not args.dry_run:
        print("Parsing locally — no model, no network.")

    results = [process(f, passwords, args.dry_run) for f in files]

    width = max(len(r.name) for r in results)
    print(f"\n{'statement'.ljust(width)}  {'pages':>5} {'rows':>5}  verdict     detail")
    print("-" * (width + 60))
    for r in results:
        print(f"{r.name.ljust(width)}  {r.pages:>5} {r.txns:>5}  {r.verdict:<11} {r.detail}")
        for w in r.warnings:
            print(f"{' ' * width}         !  {w}")

    passed = sum(1 for r in results if r.verdict == "PASS")
    checkable = sum(1 for r in results if r.verdict in ("PASS", "FAIL"))
    if checkable:
        print(f"\n{passed}/{checkable} verifiable statements reconciled.")
    print(f"JSON + text output in {OUT.relative_to(HERE.parent)}/\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
