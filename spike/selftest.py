"""Self-checks for the spike. No network, no API key, no real statements.

    python spike/selftest.py

Covers the two things that can silently produce wrong answers: text encoding
(which broke on Windows) and the reconciliation gate (which is the whole point
of the exercise).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from extract import Result, read_text, reconcile, sanity_checks, write_text  # noqa: E402

HERE = Path(__file__).parent
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  — ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(label)


# Characters that actually turn up in statements and break cp1252, plus a few
# that break other single-byte encodings.
HOSTILE = "SINGAPORE AIRLINES ✈ KrisFlyer\ncafé Zürich —dash 'quotes'\n₹ ¥ € 中文 emoji 🏦\n"


def test_write_is_platform_independent() -> None:
    print("\ntext output")
    tmp = Path(tempfile.mkdtemp())

    p = tmp / "out.txt"
    write_text(p, HOSTILE)
    check("hostile characters survive a write/read round-trip", read_text(p) == HOSTILE)
    check("bytes on disk are utf-8", p.read_bytes().decode("utf-8") == HOSTILE)

    # The bug that started this: the platform default would have been used.
    write_text(tmp / "lf.txt", "a\nb\n")
    check("line endings are LF on every platform",
          (tmp / "lf.txt").read_bytes() == b"a\nb\n")


def test_read_is_tolerant() -> None:
    print("\ntext input (files we did not write)")
    tmp = Path(tempfile.mkdtemp())
    text = '{"stmt.pdf": "café"}'

    plain = tmp / "utf8.json"
    plain.write_bytes(text.encode("utf-8"))
    check("plain utf-8", read_text(plain) == text)

    bom = tmp / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    check("utf-8 with BOM (Windows Notepad)", read_text(bom) == text)

    legacy = tmp / "cp1252.json"
    legacy.write_bytes(text.encode("cp1252"))
    check("cp1252 (legacy Windows editor)", read_text(legacy) == text)

    junk = tmp / "junk.json"
    junk.write_bytes(b'{"a": "\xff\xfe\x00garbage"}')
    try:
        read_text(junk)
        check("undecodable bytes do not raise", True)
    except Exception as e:  # noqa: BLE001
        check("undecodable bytes do not raise", False, repr(e))


def test_no_call_site_bypasses_the_helpers() -> None:
    """The per-call fix was easy to forget. This makes forgetting it loud."""
    print("\nsource audit")
    src = read_text(HERE / "extract.py")
    offenders = []
    for n, line in enumerate(src.splitlines(), start=1):
        if line.strip().startswith("#"):      # prose about the rule is not a violation
            continue
        if re.search(r"\.(read|write)_text\(", line):
            offenders.append(f"{n}: {line.strip()}")
        # open() in text mode without an explicit encoding
        if re.search(r"\bopen\(", line) and "pdfplumber" not in line:
            if '"wb"' not in line and '"rb"' not in line and "encoding=" not in line:
                offenders.append(f"{n}: {line.strip()}")
    check("no direct Path.read_text/write_text or unencoded open()",
          not offenders, "; ".join(offenders))


def test_encryption_paths() -> None:
    """A statement encrypted only to set permission flags has an empty user
    password and must open without one. DBS ships statements like this."""
    print("\nencrypted pdfs")
    import pypdf
    from extract import read_pdf

    tmp = Path(tempfile.mkdtemp())

    def make(name: str, user_pw: str | None) -> Path:
        w = pypdf.PdfWriter()
        w.add_blank_page(width=595, height=842)
        if user_pw is not None:
            w.encrypt(user_password=user_pw, owner_password="owner")
        dst = tmp / name
        with open(dst, "wb") as fh:
            w.write(fh)
        return dst

    empty_pw = make("empty.pdf", "")
    locked = make("locked.pdf", "S1234567A")
    plain = make("plain.pdf", None)

    for label, path, pw, want_err in [
        ("permissions-only encryption opens with no password", empty_pw, None, False),
        ("locked file opens with the right password", locked, "S1234567A", False),
        ("locked file rejects the wrong password", locked, "nope", True),
        ("locked file reports a missing password", locked, None, True),
        ("unencrypted file opens", plain, None, False),
    ]:
        _, err = read_pdf(path, pw)
        check(label, (err is not None) == want_err, f"err={err}")


def test_row_detection() -> None:
    """The dry-run row count is what tells you the table survived extraction."""
    print("\ntransaction-row detection")
    from extract import Page, count_txn_shaped_lines

    def page(*lines: str) -> Page:
        text = "\n".join(lines)
        return Page(number=1, text=text, char_count=len(text))

    formats = {
        "DD MMM":       "  15 JUN    16 JUN    FAIRPRICE FINEST 203              82.15",
        "DD/MM":        "  15/06  GRAB *TRIP 4821                              14.20",
        "DD-MM-YYYY":   "  15-06-2026  NETFLIX.COM                             19.98",
        "MMM DD":       "  JUN 15  STARBUCKS RAFFLES CITY                       7.80",
        "ISO":          "  2026-06-15  UNIQLO ION ORCHARD                      89.90",
        "credit (CR)":  "  10 JUL    PAYMENT - THANK YOU                  1,200.00 CR",
        "thousands":    "  03 JUL    BOOKING.COM AMSTERDAM                 1,412.00",
    }
    for label, line in formats.items():
        check(f"matches {label}", count_txn_shaped_lines([page(line)]) == 1, repr(line))

    non_rows = [
        "  Interest at 27.8% p.a. applies to unpaid balances.",
        "  Previous Balance                                    1,204.55",   # no leading date
        "       FOREIGN CURRENCY EUR 285.00 @ 1.4456",                      # subline, no amount col
        "  TRANS DATE POST DATE DESCRIPTION            AMOUNT (SGD)",
        "",
    ]
    check("ignores non-transaction lines", count_txn_shaped_lines([page(*non_rows)]) == 0)


def test_row_parsing() -> None:
    """The parser is the spike now, so this is where wrong answers come from."""
    print("\nrow parsing")
    import rows

    # Two dates in a row must not be read as one date with a two-digit year.
    # "27 JUN 28 JUN" losing its second date was the bug that silently halved
    # every issuer that prints a posting date.
    dates, rest = rows.leading_dates("27 JUN 28 JUN  MERCHANT  12.34")
    check("two adjacent dates stay two dates", len(dates) == 2, repr(dates))
    check("a four-digit year is still consumed",
          rows.leading_dates("15 Jun 2026  X  1.00")[0] == [(15, 6, 2026)],
          repr(rows.leading_dates("15 Jun 2026  X  1.00")[0]))

    # Direction, in each of the ways issuers express it.
    cases = [
        ("SHOP  12.34", "debit", "12.34"),
        ("REFUND  89.90 CR", "credit", "89.90"),
        ("CHARGE  5.00 DR", "debit", "5.00"),
        ("TRANSFER  -55.94", "debit", "55.94"),
        ("TOPUP  +250.00", "credit", "250.00"),
        ("ADJUSTMENT  (18.20)", "debit", "18.20"),
        ("BIG  1,234.56", "debit", "1234.56"),
    ]
    for text, want_dir, want_amt in cases:
        got = rows.parse_amount(text)
        ok = got and str(got[0]) == want_amt and got[1] == want_dir
        check(f"{text.split()[0].lower()} -> {want_dir} {want_amt}", bool(ok), repr(got))

    # A row printing only day and month takes its year from the period, and a
    # December row on a statement ending in January belongs to the year before.
    check("year comes from the period",
          rows.resolve_year(15, 6, ("2026-06-15", "2026-07-14"), None) == 2026)
    check("year boundary resolves backwards",
          rows.resolve_year(28, 12, ("2025-12-15", "2026-01-14"), None) == 2025)

    # Whole-document context: the year is on page 1 only, and every later page
    # needs it. Resolving per page drops every row after the first.
    page1 = "Statement Period: 15 Jun 2026 to 14 Jul 2026\n15 JUN  16 JUN  SHOP  10.00"
    page2 = "20 JUN  21 JUN  SHOP  20.00"
    ctx = rows.document_context([page1, page2])
    check("period found across the document", ctx.period == ("2026-06-15", "2026-07-14"), repr(ctx.period))
    later = rows.parse_page(page2, ctx.period, ctx.year, ctx.statement_date)
    check("a page with no printed year still dates its rows",
          [t["date"] for t in later["transactions"]] == ["2026-06-20"],
          repr(later["transactions"]))

    # Most issuers print a closing date, not a range. It is a better year anchor
    # than "whichever year appears most often", which is wrong every January.
    ctx = rows.document_context(["Statement Date: 15 Jan 2027", "28 DEC  29 DEC  SHOP  10.00"])
    check("a lone statement date is picked up", ctx.statement_date == "2027-01-15", repr(ctx.statement_date))
    rolled = rows.parse_page("28 DEC  29 DEC  SHOP  10.00", ctx.period, ctx.year, ctx.statement_date)
    check("a December row on a January statement lands in the previous year",
          [t["date"] for t in rolled["transactions"]] == ["2026-12-28"],
          repr(rolled["transactions"]))

    # Due dates and rate tables are date-and-number lines too, and must not be
    # mistaken for transactions.
    noise = rows.parse_page("Payment Due Date: 04 Aug 2026\nInterest 27.80\n", ("2026-06-15", "2026-07-14"), 2026)
    check("a due date is not a transaction", noise["transactions"] == [],
          repr(noise["transactions"]))

    # A foreign charge is three lines, not one: the merchant above, both
    # figures on the dated line, the rate below (DESIGN.md Section 4). Parsed
    # as one line the row reconciles to the cent while its description becomes
    # `102.67 HKD` — right money, no merchant, and nothing to categorize.
    fx = (
        "          25 Jun   26 Jun    Apple                        3.98\n"
        "\n"
        "                             Widgetorium\n"
        "          02 Jul   04 Jul                  102.67 HKD    16.99\n"
        "                              1 HKD = 0.1655 SGD\n"
    )
    got = rows.parse_page(fx, ("2026-06-17", "2026-07-17"), 2026)
    row = next((t for t in got["transactions"] if t["date"] == "2026-07-02"), None)
    check("a foreign charge keeps the merchant from the line above",
          bool(row) and row["description"] == "Widgetorium", repr(row))
    check("the statement's billed amount is still what reconciles",
          bool(row) and row["amount"] == "16.99", repr(row and row["amount"]))
    check("the original amount and currency survive",
          bool(row) and row["foreign"] == {"amount": "102.67", "currency": "HKD"},
          repr(row and row["foreign"]))
    check("the statement's own printed rate is kept, not today's",
          bool(row) and row["fx_rate"] == "0.1655", repr(row and row["fx_rate"]))
    # The merchant above is claimed from the gap, never lifted off the row
    # before it — that would rename one transaction after another.
    check("the previous transaction keeps its own name",
          any(t["description"] == "Apple" for t in got["transactions"]),
          repr([t["description"] for t in got["transactions"]]))

    # A rate for a currency this row was not charged in belongs to something
    # else on the page, so it is not claimed.
    mismatch = rows.parse_page(
        "                             Widgetorium\n"
        "          02 Jul   04 Jul                  102.67 HKD    16.99\n"
        "                              1 USD = 1.3400 SGD\n",
        ("2026-06-17", "2026-07-17"), 2026)
    check("a rate naming another currency is not this row's rate",
          mismatch["transactions"][0]["fx_rate"] is None,
          repr(mismatch["transactions"][0]))

    # Both halves or neither: with no name above, the figure is left where it
    # is. It is honest about not being a merchant, which an invented name
    # would not be.
    orphan = rows.parse_page(
        "          02 Jul   04 Jul                  102.67 HKD    16.99\n",
        ("2026-06-17", "2026-07-17"), 2026)
    check("a foreign figure with no merchant above is left alone",
          orphan["transactions"][0]["description"] == "102.67 HKD",
          repr(orphan["transactions"][0]["description"]))

    # A row billed in the statement's own currency has no conversion to record,
    # so `SGD` must not open the foreign path and go looking for a name above.
    home = rows.parse_page(
        "                             Widgetorium\n"
        "          02 Jul   04 Jul                  16.99 SGD    16.99\n",
        ("2026-06-17", "2026-07-17"), 2026)
    check("a figure in the statement's own currency is not a foreign charge",
          home["transactions"][0]["foreign"]["currency"] is None,
          repr(home["transactions"][0]))


    # Trust rules its opening and closing balance into the transaction table,
    # dated like any other row. Counted as purchases they roughly double the
    # debit total, and the statement then reports UNVERIFIED, so nothing
    # contradicts it.
    table = (
        "17 Jun  17 Jun  Previous balance  1,722.18" + "\n" +
        "17 Jun  17 Jun  FAST Credit Payment  1,722.18 CR" + "\n" +
        "19 Jun  19 Jun  FairPrice App  0.92" + "\n" +
        "17 Jul  17 Jul  Total outstanding balance  115.82" + "\n"
    )
    got = rows.parse_page(table, ("2026-06-17", "2026-07-17"), 2026)
    check("a dated balance row is not a transaction", len(got["transactions"]) == 2,
          repr([t["description"] for t in got["transactions"]]))
    check("it becomes the opening balance instead", got["opening_balance"] == "1722.18",
          repr(got["opening_balance"]))

    # ...but only when the label opens the line. A merchant may contain the word.
    shop = rows.parse_page("19 Jun  19 Jun  BALANCE SPA TOTAL WELLNESS  40.00" + "\n",
                           ("2026-06-17", "2026-07-17"), 2026)
    check("a merchant containing 'balance' is still a transaction",
          len(shop["transactions"]) == 1, repr(shop["transactions"]))

    # Standard Chartered's front-page summary is a table, not a labelled value:
    # `TOTAL` sits under `New Balance | Min. Payment Due`, so the last figure on
    # the line is the minimum payment. Reading it as the closing balance turned
    # a byte-perfect extraction of standard-chartered-2.pdf into a FAIL 8.40
    # short — the difference between 58.40 owed and 50.00 due. DBS and UOB print
    # the same shape and escaped only because an earlier page had already filled
    # the field. Nothing on the line says which column is which, so take neither.
    sc = (
        "     Account/Card No.    New Balance      Min. Payment Due\n"
        "     TOTAL                     58.40                 50.00\n"
    )
    got = rows.parse_page(sc, ("2026-07-16", "2026-08-15"), 2026)
    check("a multi-column TOTAL row claims no balance",
          got["closing_balance"] is None, repr(got["closing_balance"]))

    # ...and the figure is not lost: the same statement labels it plainly further
    # down, which is the source that actually says what it is.
    got = rows.parse_page(sc + "                  NEW BALANCE          58.40\n",
                          ("2026-07-16", "2026-08-15"), 2026)
    check("the labelled line supplies it instead",
          got["closing_balance"] == "58.40", repr(got["closing_balance"]))

    # The guard is about ambiguity, not about the word TOTAL. One figure, one
    # meaning — DBS and UOB close their card section exactly like this.
    got = rows.parse_page("     TOTAL                     2,985.97\n",
                          ("2026-07-16", "2026-08-15"), 2026)
    check("a single-figure TOTAL is still read",
          got["closing_balance"] == "2985.97", repr(got["closing_balance"]))

    # The issuer's reference. UOB and DBS print it on a continuation line under
    # the row; without it, two genuine same-day, same-amount charges to one
    # merchant are indistinguishable from a page read twice. UOB bills two ZERO1
    # lines at 7.06 on the same date every month, so this is not hypothetical.
    # Note the `. :` — a full stop, a space, then a colon.
    uob = (
        "   08 JUL 07 JUL ZERO1 PTE LTD SINGAPORE               7.06\n"
        "                Ref No. : 74143256188100091281157\n"
        "\n"
        "   08 JUL 07 JUL ZERO1 PTE LTD SINGAPORE               7.06\n"
        "                Ref No. : 74143256188100092450348\n"
    )
    got = rows.parse_page(uob, ("2026-06-17", "2026-07-16"), 2026)
    refs = [t["reference"] for t in got["transactions"]]
    check("a reference on the line below is captured",
          refs == ["74143256188100091281157", "74143256188100092450348"], repr(refs))

    # A row the statement gives no reference for must not borrow the next row's.
    # UOB's payment and instalment rows print none, and the very next line is
    # already the following transaction.
    gap = (
        "   17 JUN 17 JUN CCRD-Credit Card Payment           3,105.18CR\n"
        "   16 JUN 13 JUN BUS/MRT 870632419 SINGAPORE            2.56\n"
        "                Ref No. : 74541836167288080019886\n"
    )
    got = rows.parse_page(gap, ("2026-06-17", "2026-07-16"), 2026)
    refs = [t["reference"] for t in got["transactions"]]
    check("a reference is not stolen from the row below",
          refs == [None, "74541836167288080019886"], repr(refs))

    # DBS writes the same shape with an alphanumeric value and no space before
    # the colon, so the reader cannot assume digits or one fixed punctuation.
    dbs = (
        "   16 JUN PAYMENT RECEIVED VIA FAST                   686.06 CR\n"
        "         REF NO: 60616OCBCSGSGBRT8330182\n"
    )
    got = rows.parse_page(dbs, ("2026-05-17", "2026-06-16"), 2026)
    check("an alphanumeric reference is captured",
          got["transactions"][0]["reference"] == "60616OCBCSGSGBRT8330182",
          repr(got["transactions"][0]["reference"]))

    # Standard Chartered writes it inline instead, inside the description.
    sc_ref = "  02 Aug 06 Aug BUS/MRT 901852743 SINGAPORE SG Transaction Ref 74541836217288081589523 4.26\n"
    got = rows.parse_page(sc_ref, ("2026-07-16", "2026-08-15"), 2026)
    check("an inline reference is captured",
          got["transactions"][0]["reference"] == "74541836217288081589523",
          repr(got["transactions"][0]["reference"]))

    # Prose is not a reference: `refer` is not `ref`, and a short word is not an
    # identifier. Inventing one would be worse than finding none — it would make
    # two rows that really are a double-read look distinct.
    prose = (
        "   19 Jun  19 Jun  FairPrice App                          0.92\n"
        "         Please refer to page 3\n"
        "   20 Jun  20 Jun  Kopitiam                               1.40\n"
        "         Reference: see page 3\n"
    )
    got = rows.parse_page(prose, ("2026-06-17", "2026-07-16"), 2026)
    check("prose is not read as a reference",
          [t["reference"] for t in got["transactions"]] == [None, None],
          repr([t["reference"] for t in got["transactions"]]))


    # A horizontal summary grid: figures in one row, labels stacked above and
    # aligned by column. Nothing on the figures row says what any of it means.
    # MariBank separates its headers with single spaces and stacks them over
    # three lines; the (A)..(I) row is the only one that lines up cleanly.
    mari = (
        "        PREVIOUS  REPAYMENT/ WAIVER/ PURCHASE LOANS CASHBACK BANK SPLIT OTHERS" + "\n"
        "        OUTSTANDING CONVERSION REFUND                    CHARGE PAYMENT" + "\n"
        "        (A)       (B)      (C)   (D)     (E)      (F)    (G)    (H)    (I)" + "\n"
        "        216.32    216.18   0.00  235.20  0.00     0.14   0.00   0.00   0.00" + "\n"
        "        CURRENT OUTSTANDING (A-B-C+D+E-F+G+H+I)                  235.20" + "\n"
    )
    got = rows.parse_page(mari, ("2026-05-21", "2026-06-20"), 2026)
    check("grid: opening balance found under stacked headers",
          got["opening_balance"] == "216.32", repr(got["opening_balance"]))
    check("grid: closing balance still read from its own line",
          got["closing_balance"] == "235.20", repr(got["closing_balance"]))
    # Lifting a component column into a total is the trap: MariBank's credits are
    # repayment 216.18 plus cashback 0.14, so "REPAYMENT (B)" alone would FAIL a
    # correct extraction by 14 cents.
    check("grid: component columns are not read as totals",
          got["total_credits"] is None and got["total_debits"] is None,
          repr((got["total_debits"], got["total_credits"])))

    # Trust writes the same grid as an equation on one line instead.
    tr = (
        "     Previous balance + Purchases + Cash advance + Interest/Fees - = Current outstanding balance" + "\n"
        "      S$1,722.18 S$115.82 S$0.00   S$0.00   S$1,722.18  S$115.82" + "\n"
    )
    got = rows.parse_page(tr, ("2026-06-17", "2026-07-17"), 2026)
    check("grid: operator-separated headers align too",
          got["opening_balance"] == "1722.18", repr(got["opening_balance"]))

    # Two numbers next to each other are a coincidence, not a grid.
    pair = rows.parse_page("Previous balance   Interest" + "\n" + "   100.91  0.12" + "\n",
                           ("2026-06-17", "2026-07-17"), 2026)
    check("grid: needs at least three columns", pair["opening_balance"] is None,
          repr(pair["opening_balance"]))


def test_month_coverage() -> None:
    """How much of a calendar month the statements actually cover (Section 4 trap 1).

    The number this protects is the headline of the whole report. On the real
    corpus August reads as 1,319.55 against July's 4,756.64 — a 72% collapse —
    and it is nothing of the kind: the month is two-thirds unbilled. Wrong in
    the direction that would make someone change their behaviour.
    """
    print("\nmonth coverage")
    import months

    check("month bounds", months.month_bounds("2026-08") == ("2026-08-01", "2026-08-31"))
    check("february in a leap year",
          months.month_bounds("2028-02") == ("2028-02-01", "2028-02-29"),
          repr(months.month_bounds("2028-02")))
    check("december rolls the year",
          months.month_bounds("2026-12") == ("2026-12-01", "2026-12-31"),
          repr(months.month_bounds("2026-12")))

    # Touching cycles are contiguous. One ending the 15th and the next starting
    # the 16th cover the boundary; treating that as a hole would report every
    # card as permanently incomplete.
    check("touching windows merge",
          months.merge([("2026-06-16", "2026-07-15"), ("2026-07-16", "2026-08-15")])
          == [("2026-06-16", "2026-08-15")])
    check("a real gap survives merging",
          len(months.merge([("2026-05-21", "2026-06-20"), ("2026-07-21", "2026-08-20")])) == 2)

    # DBS prints no period, only a statement date. Consecutive statements tile
    # the timeline, so the start of one is the day after the end of the last —
    # which is what a billing cycle is.
    dbs = [{"period_start": None, "period_end": None, "statement_date": "2026-08-14",
            "first_txn": "2026-07-14"},
           {"period_start": None, "period_end": None, "statement_date": "2026-07-14",
            "first_txn": "2026-06-13"}]
    check("a statement date alone still yields a window",
          months.statement_windows(dbs) == [("2026-06-13", "2026-08-14")],
          repr(months.statement_windows(dbs)))

    # A statement with no end at all bounds nothing, and inventing one would
    # claim coverage that may not exist.
    check("a statement with no dates is dropped",
          months.statement_windows([{"first_txn": "2026-06-01"}]) == [],
          repr(months.statement_windows([{"first_txn": "2026-06-01"}])))

    # Two statements on one card can share an end date — a cycle the bank
    # re-issued, uploaded beside the original, which `file_sha256` does not
    # catch because the file really is different. Sorting `(end, statement)`
    # pairs then falls through to comparing the dicts, which do not order, and
    # the months page raised a TypeError instead of rendering.
    same_day = [{"period_start": "2026-06-01", "period_end": "2026-06-30", "first_txn": "2026-06-02"},
                {"period_start": "2026-06-01", "period_end": "2026-06-30", "first_txn": "2026-06-03"}]
    try:
        got, raised = months.statement_windows(same_day), None
    except TypeError as exc:
        got, raised = None, exc
    check("two statements ending the same day do not raise",
          raised is None and got == [("2026-06-01", "2026-06-30")], repr(raised or got))

    full = [("2026-08-01", "2026-08-31")]
    check("a fully covered month", months.card_coverage(full, "2026-08")["state"] == "complete")
    check("no window at all is missing",
          months.card_coverage([("2026-06-01", "2026-06-30")], "2026-08")["state"] == "missing")

    # Both ends matter. The first version of this only asked how far coverage
    # reached, so a month missing its opening fortnight looked complete.
    late = months.card_coverage([("2026-06-17", "2026-08-17")], "2026-06")
    check("a month covered only from mid-month is partial", late["state"] == "partial")
    check("...and says where it starts", late["from"] == "2026-06-17", repr(late))

    early = months.card_coverage([("2026-07-16", "2026-08-14")], "2026-08")
    check("a month billed only to the 14th is partial", early["state"] == "partial")
    check("...and says how far it reaches", early["through"] == "2026-08-14", repr(early))

    holed = months.card_coverage(
        [("2026-06-21", "2026-07-05"), ("2026-07-20", "2026-07-31")], "2026-07")
    check("a missing statement leaves a gap inside the month", holed["gap"], repr(holed))

    # Across cards: the trustworthy window is the intersection, because outside
    # it the total is a sum of however many cards happened to be covered.
    coverage = {
        "A": [("2026-06-13", "2026-08-14")],
        "B": [("2026-06-17", "2026-08-17")],
        "C": [("2026-06-16", "2026-08-16")],
    }
    aug = months.month_completeness("2026-08", coverage)
    check("the month ends where the first card's coverage ends",
          aug["covered_through"] == "2026-08-14", repr(aug["covered_through"]))
    check("...and begins at the month start when every card reaches it",
          aug["covered_from"] == "2026-08-01", repr(aug["covered_from"]))
    check("a partial month is not complete", not aug["is_complete"])

    jun = months.month_completeness("2026-06", coverage)
    check("a month opening in a gap begins at the last card's start",
          jun["covered_from"] == "2026-06-17", repr(jun["covered_from"]))

    # One card absent means no window is trustworthy — not "the window the
    # others agree on", which would quietly present a total missing a card.
    absent = dict(coverage, D=[("2026-01-01", "2026-01-31")])
    gone = months.month_completeness("2026-08", absent)
    check("a card missing outright makes the month unboundable",
          gone["covered_from"] is None and gone["missing"] == ["D"], repr(gone["missing"]))
    check("...and it is not silently comparable", months.comparable_days(gone) is None)

    whole = months.month_completeness(
        "2026-08", {"A": [("2026-07-01", "2026-09-30")]})
    check("a fully covered month is complete", whole["is_complete"], repr(whole))
    check("a complete month needs no day restriction",
          months.comparable_days(whole) is None, repr(months.comparable_days(whole)))
    check("a partial month reports its comparable days",
          months.comparable_days(aug) == (1, 14), repr(months.comparable_days(aug)))

    # Section 4's three-month average. The averaging is arithmetic; what
    # decides whether the figure means anything is which months are let into
    # it. An average hides a short month better than a single comparison does —
    # three part-billed months make one low number with nothing on its face to
    # say so, and every month measured against it then reads as an overspend.
    def month_status(ym: str, window: tuple[str, str] | None) -> dict:
        return months.month_completeness(ym, {"A": [window] if window else []})

    full = [month_status(ym, (f"{ym}-01", f"{ym}-30")) for ym in ("2026-05", "2026-06", "2026-07")]
    part = month_status("2026-08", ("2026-07-20", "2026-08-14"))
    usable, short, note = months.trailing_window(part, full)
    check("three fully billed months make an average",
          [m["month"] for m in usable] == ["2026-05", "2026-06", "2026-07"] and note is None,
          repr(([m["month"] for m in usable], note)))

    # The month being averaged against has to cover the days being reported.
    # A June billed only from the 17th cannot speak for August 1-14, and an
    # average that includes it is low by however much fell in the first
    # fortnight — the same trap the delta guards, compounded three times.
    mixed = [month_status("2026-05", ("2026-05-01", "2026-05-31")),
             month_status("2026-06", ("2026-06-17", "2026-06-30")),
             month_status("2026-07", ("2026-07-01", "2026-07-31"))]
    usable, short, note = months.trailing_window(part, mixed)
    check("a month that does not cover the days is left out",
          [m["month"] for m in usable] == ["2026-05", "2026-07"], repr([m["month"] for m in usable]))
    check("...and is named, so the missing statement can be found",
          short == ["2026-06"], repr(short))

    # One contributor is not an average, it is last month — which the delta
    # already shows, and shows honestly.
    usable, short, note = months.trailing_window(part, mixed[1:])
    check("a single qualifying month is not called an average",
          usable == [] and note and "fewer than two" in note, repr((usable, note)))

    # Only the three most recent count, whatever else is stored.
    many = [month_status(f"2026-{m:02d}", (f"2026-{m:02d}-01", f"2026-{m:02d}-28"))
            for m in range(1, 8)]
    usable, _, _ = months.trailing_window(part, many)
    check("the average looks back three months, not further",
          [m["month"] for m in usable] == ["2026-05", "2026-06", "2026-07"],
          repr([m["month"] for m in usable]))

    # A month nothing bounds cannot host a comparison at all, and says which.
    usable, _, note = months.trailing_window(month_status("2026-08", None), full)
    check("an unbounded month gets no average and says why",
          usable == [] and note == "this month cannot be bounded", repr(note))

    # Months are different lengths, and comparing raw day numbers across them
    # disqualifies every shorter one. A complete June is billed 1-30 and can
    # never answer "were you billed for days 1-31?" however complete it is, so
    # a fair June-to-July comparison refused itself and blamed the calendar on
    # a missing statement. Both the average and the delta ran this test.
    june = month_status("2026-06", ("2026-06-01", "2026-06-30"))
    july = month_status("2026-07", ("2026-07-01", "2026-07-31"))
    check("a complete 30-day month covers a 31-day month's whole window",
          months.covers_days(june, (1, 31)), repr(months.covered_day_range(june)))
    check("a complete month is complete regardless of length",
          june["is_complete"] and july["is_complete"])
    usable, short, note = months.trailing_window(
        month_status("2026-08", ("2026-08-01", "2026-08-31")),
        [month_status("2026-05", ("2026-05-01", "2026-05-31")), june, july])
    check("a 30-day month is not excluded from a 31-day month's average",
          [m["month"] for m in usable] == ["2026-05", "2026-06", "2026-07"],
          repr(([m["month"] for m in usable], short)))
    # The clamp must not become a way for a genuinely short month to sneak in:
    # a June billed only to the 20th still fails a 1-31 window.
    check("a genuinely part-billed month still fails the window",
          not months.covers_days(month_status("2026-06", ("2026-06-01", "2026-06-20")), (1, 31)))


def test_cycle_dates() -> None:
    """When the statement date is a column header rather than a line label.

    Four of ten statements in the corpus reported no period at all, which makes
    Section 4's "is this month complete for this card" unanswerable — and all
    four print the answer, just not on a line the parser could read.
    """
    print("\ncycle dates")
    import rows

    # DBS: labels stacked over their values, one row of columns.
    dbs = (
        "        STATEMENT DATE     CREDIT LIMIT    MINIMUM PAYMENT   PAYMENT DUE DATE\n"
        "          14 Jul 2026       $21,000.00         $50.00          11 Aug 2026\n"
    )
    got = rows.document_context([dbs])
    check("a statement date under its column header is found",
          got.statement_date == "2026-07-14", repr(got.statement_date))

    # The trap: the same grid also carries the payment due date, nearly a month
    # later and inside the *next* cycle. Taking the wrong column dates every row
    # in the statement to the wrong month.
    check("the payment due date is not mistaken for it",
          got.statement_date != "2026-08-11", repr(got.statement_date))

    # MariBank: three stacked header lines, a units row, and a value row that
    # opens with a card name — so the label and its value never line up by
    # column. Order is what survives.
    mari = (
        "       ACCOUNT   STATEMENT DATE CREDIT LIMIT STATEMENT DUE MINIMUM PAYMENT\n"
        "                                                    PAYMENT DUE DUE DATE\n"
        "                            (SGD)       (SGD)       (SGD)\n"
        "\n"
        "       MARI CREDIT 21 JUN 2026 21,000.00 235.20      10.00       10 JUL 2026\n"
    )
    got = rows.document_context([mari])
    check("stacked headers three deep still pair up",
          got.statement_date == "2026-06-21", repr(got.statement_date))

    # If the counts disagree there is nothing on the row saying which date is
    # which, so claim nothing — the same rule as the multi-column TOTAL guard.
    ambiguous = (
        "        STATEMENT DATE     PAYMENT DUE DATE\n"
        "          14 Jul 2026\n"
    )
    got = rows.document_context([ambiguous])
    check("two headers and one date claims nothing",
          got.statement_date is None, repr(got.statement_date))

    # A labelled line still wins over the grid: it is the issuer saying so,
    # rather than an inference from where the ink landed.
    both = "Statement Date: 16 Jul 2026\n" + dbs
    check("an explicit label outranks the grid",
          rows.document_context([both]).statement_date == "2026-07-16",
          repr(rows.document_context([both]).statement_date))

    # Transaction rows print no year, which is what keeps this out of the table.
    table = (
        "        STATEMENT DATE     PAYMENT DUE DATE\n"
        "   13 JUN 14 JUN SOME MERCHANT SINGAPORE            12.34\n"
    )
    check("a dated transaction row is not read as a grid value",
          rows.document_context([table]).statement_date is None,
          repr(rows.document_context([table]).statement_date))

    # Trust prints a full cycle on page 1, with its address bleeding into the
    # same line — so the label cannot be anchored to the start of the line.
    trust = "Block 90B Statement cycle 17 Jun 2026 - 17 Jul 2026\n"
    got = rows.document_context([trust])
    check("a statement cycle is read as a period",
          got.period == ("2026-06-17", "2026-07-17"), repr(got.period))


def test_merchant_normalization() -> None:
    """The key tier 2 of the categorizer looks up (DESIGN.md Section 3).

    Every string here is either synthetic or already in git — the real corpus is
    checked on the machine that holds it, never from a session.

    What is being asserted throughout is *stability*, not tidiness: the same
    merchant reaches the same key next month, and two merchants do not share
    one. A prettier key that moves is worse than an ugly key that does not.
    """
    print("\nmerchant normalization")
    import merchants

    def key(s: str) -> str:
        return merchants.normalize(s)

    # The case DESIGN.md Section 3 names outright. Without this, next month's
    # receipt number is a new merchant and the user categorizes Grab all over
    # again.
    starred = {key("GRAB *TRIP 4821 SINGAPORE SG"), key("GRAB *TRIP 9903 SINGAPORE SG"),
               key("GRAB* TRIP 1174")}
    check("a receipt number does not invent a new merchant", starred == {"grab"}, repr(starred))

    # The third spelling in Section 3 has no star to cut at, so it keeps a word
    # the others dropped. It meets them at the root instead — which is the
    # whole reason there is a root, rather than normalization guessing that
    # `trip` is a service word and not part of somebody's name.
    check("a starless spelling meets the others at the root",
          merchants.merchant_root(key("Grab Trip SG")) == "grab", repr(key("Grab Trip SG")))

    check("a store number and its city are dropped",
          key("FAIRPRICE FINEST 203 SINGAPORE SG") == "fairprice finest",
          repr(key("FAIRPRICE FINEST 203 SINGAPORE SG")))

    # Order matters, and this is the regression: the place is not at the end
    # until the reference has been cut off it. 62 rows of one statement end
    # `... SINGAPORE 065`, so stripping places first leaves `singapore` in
    # every one of them.
    check("a place is stripped even when a reference follows it",
          key("PAYNOW TRANSFER SINGAPORE 065") == "paynow transfer",
          repr(key("PAYNOW TRANSFER SINGAPORE 065")))

    # ...but a place at the *front* is a name.
    check("a leading place name survives",
          key("SINGAPORE AIRLINES SINGAPORE SG") == "singapore airlines",
          repr(key("SINGAPORE AIRLINES SINGAPORE SG")))

    # Eleven rows in the corpus open with a token holding a digit. Cutting at
    # the first junk token would leave nothing at all, so position 0 is never a
    # cut point.
    check("a merchant whose name starts with a code is not cut away",
          key("ZERO1 PTE LTD SINGAPORE") == "zero1 pte ltd",
          repr(key("ZERO1 PTE LTD SINGAPORE")))
    check("short numbers are names, not references",
          key("HOTEL 81 PRINCESS SG") == "hotel 81 princess",
          repr(key("HOTEL 81 PRINCESS SG")))

    # Two issuers, two ways of printing the same transit ride: UOB puts the
    # reference in the middle, Standard Chartered writes it inline at the end.
    # Both are one merchant and must key as one.
    uob = key("BUS/MRT 870632419 SINGAPORE")
    sc = key("BUS/MRT 901852743 SINGAPORE SG Transaction Ref 74541836217288081589523")
    check("two issuers' reference styles reach one key", uob == sc == "bus/mrt", repr((uob, sc)))

    # A domain names the merchant, and everything after it is contact detail.
    for raw, want in [("NETFLIX.COM 866-579-7172 SG", "netflix"),
                      ("APPLE.COM/BILL ITUNES.COM SG", "apple"),
                      ("BOOKING.COM AMSTERDAM NL", "booking")]:
        check(f"a domain resolves to its name ({want})", key(raw) == want, repr(key(raw)))

    # ...but only for real TLDs, or `MR.DIY` becomes a merchant called `mr` and
    # collects every other `mr.` alongside it.
    check("an unknown suffix is not treated as a domain",
          key("MR.DIY TAMPINES SG").startswith("mr.diy"), repr(key("MR.DIY TAMPINES SG")))

    # A star means two different things. A processor prints itself first...
    check("a processor prefix keeps what follows",
          key("SQ *BLUE BOTTLE COFFEE") == "blue bottle coffee",
          repr(key("SQ *BLUE BOTTLE COFFEE")))
    # ...and everyone else prints their own order number after their name.
    # Guessing wrong the other way is worse: it keys on a number that changes.
    check("a merchant prefix keeps what precedes",
          key("WIDGETCO SG*ORD8821 WIDGETCO.SG") == "widgetco",
          repr(key("WIDGETCO SG*ORD8821 WIDGETCO.SG")))

    # An *unlisted* gateway is the case that shipped wrong: `PROCESSOR_PREFIXES`
    # can only name the gateways already met, and the one it has not met merges
    # every shop behind it into a single key. Two real merchants sat under one
    # three-letter code in the corpus before this. Recognized by shape now — a
    # short code on the left, a whole shop name on the right.
    zzz = {key("ZZ**Li Xin Fish Ball SG"), key("ZZ**OLD TEA HUT (CHANGI SG")}
    check("an unlisted gateway does not merge two shops into its own code",
          zzz == {"li xin fish ball", "old tea hut (changi"}, repr(zzz))
    check("a digit-carrying gateway code is one too",
          key("2C2*ShopBack Chicha Sa GO.2C2P.COM/") == "shopback chicha",
          repr(key("2C2*ShopBack Chicha Sa GO.2C2P.COM/")))

    # Both halves of the test have to hold, and these are the rows that say why.
    # A four-letter name is not a code, so `GRAB *TRIP` keeps its left even
    # though `TRIP` reads like a word...
    check("a short name is still a name, not a gateway code",
          key("GRAB *TRIP 4821 SINGAPORE SG") == "grab",
          repr(key("GRAB *TRIP 4821 SINGAPORE SG")))
    # ...and one word before the digits is a reference, not a shop, so a code
    # on the left is not on its own enough to hand the key to the right.
    check("a code starring one word keeps what precedes",
          key("M1*DATA 88213 SG") == "m1", repr(key("M1*DATA 88213 SG")))
    # The whole reason not to simply invert the default: what follows an
    # unrecognized star is usually a receipt number, and a receipt number is a
    # new merchant every month.
    for raw in ["ACME* A-9FO4IOEWWOHCAV Singapore", "ACME****************2269"]:
        check(f"a starred reference does not become the key ({raw[:12]}…)",
              key(raw) == "acme", repr(key(raw)))

    # One statement prints the issuer's abbreviation and the merchant's own
    # domain in a single description; without the alias that row is two keys.
    check("an issuer abbreviation and the merchant's own name agree",
          key("AMZN Mktp SG*RT4G91 AMAZON.SG") == "amazon",
          repr(key("AMZN Mktp SG*RT4G91 AMAZON.SG")))

    # Section 3 nets refunds against the original merchant, which needs the
    # refund to land on the merchant's key rather than one of its own.
    check("a refund keys as the merchant it reverses",
          key("UNIQLO ION ORCHARD - REFUND") == key("UNIQLO ION ORCHARD SINGAPORE SG"),
          repr((key("UNIQLO ION ORCHARD - REFUND"), key("UNIQLO ION ORCHARD SINGAPORE SG"))))

    # Merging two merchants is worse than keeping two keys for one: a merge
    # applies a learned category to a shop the user never categorized.
    check("merchants sharing a first word stay apart",
          key("ROYAL PLAZA SINGAPORE") != key("ROYAL SPORTING HOUSE SINGAPORE"),
          repr(key("ROYAL PLAZA SINGAPORE")))

    # An empty key is one bucket that every unreadable row falls into and is
    # then categorized together — the silent wrongness Section 2.3 exists to
    # prevent.
    for odd in ["***", "065", "SG", "   ", "-"]:
        k = key(odd)
        check(f"never empty for {odd!r}", k != "" or odd.strip() == "", repr(k))

    # Normalizing a key must not move it, or a backfill drifts every time it
    # runs and merchant memory slowly stops matching what is stored.
    inputs = ["GRAB *TRIP 4821 SINGAPORE SG", "FAIRPRICE FINEST 203 SINGAPORE SG",
              "NETFLIX.COM 866-579-7172 SG", "BUS/MRT 870632419 SINGAPORE",
              "ZERO1 PTE LTD SINGAPORE", "UNIQLO ION ORCHARD - REFUND",
              "AMZN Mktp SG*RT4G91 AMAZON.SG", "SQ *BLUE BOTTLE COFFEE"]
    drift = [s for s in inputs if key(key(s)) != key(s)]
    check("normalizing a key leaves it alone", not drift, repr(drift))

    # Ordering for the bulk-categorize screen. Sorting purely by value scatters
    # the two halves of a split merchant across the page — the corpus has one
    # sitting under both `… ab cd` and `… ab-cd`, nine rows and one — so groups
    # are ordered by their combined weight and kept together inside.
    entries = [{"key": "acme mart ab cd", "weight": 900},
               {"key": "zenith fuel", "weight": 800},
               {"key": "acme mart ab-cd", "weight": 100},
               {"key": "beta cafe", "weight": 50}]
    order = [e["key"] for e in merchants.cluster_order(entries)]
    check("split spellings of one merchant are listed together",
          order.index("acme mart ab-cd") == order.index("acme mart ab cd") + 1, repr(order))
    check("groups still lead with the money", order[0].startswith("acme"), repr(order))
    check("a bigger single merchant outranks a smaller group",
          order.index("zenith fuel") < order.index("beta cafe"), repr(order))
    # Grouping is for the eye only. Merging them would be the eager root
    # collapse normalize() refuses, and would file two different shops as one.
    check("clustering does not merge or drop anything",
          sorted(order) == sorted(e["key"] for e in entries), repr(order))


def test_flow_type() -> None:
    """Whether a row is spending at all (DESIGN.md Section 3).

    The bias is toward `spend` throughout. A row wrongly left as spend is
    visible in the total and one click from being fixed; a row wrongly excluded
    is money that silently vanishes from the report, and a total that is quietly
    too low looks exactly like a frugal month.
    """
    print("\nflow type")
    import categorize

    def flow(desc, direction="debit"):
        return categorize.default_flow(desc, direction)

    # The rows Section 4 is about: a card payment appears on the card statement
    # *and* on the bank statement that paid it. Counted as spend, it
    # double-counts.
    check("a card payment is a transfer", flow("PAYMENT - THANK YOU", "credit") == "transfer",
          flow("PAYMENT - THANK YOU", "credit"))
    check("UOB's spelling too", flow("CCRD-Credit Card Payment", "credit") == "transfer",
          flow("CCRD-Credit Card Payment", "credit"))
    check("DBS's spelling too", flow("PAYMENT RECEIVED VIA FAST", "credit") == "transfer",
          flow("PAYMENT RECEIVED VIA FAST", "credit"))
    check("a balance transfer is not spending",
          flow("BALANCE TRANSFER INSTALMENT", "debit") == "transfer",
          flow("BALANCE TRANSFER INSTALMENT", "debit"))

    # Order matters: `LATE PAYMENT CHARGE` contains the word payment, and fees
    # are tested first for exactly this reason.
    check("a late payment charge is a fee, not a payment",
          flow("LATE PAYMENT CHARGE") == "fee", flow("LATE PAYMENT CHARGE"))
    check("interest is a fee", flow("INTEREST CHARGE ON PURCHASES") == "fee",
          flow("INTEREST CHARGE ON PURCHASES"))
    check("an annual fee is a fee", flow("ANNUAL FEE") == "fee", flow("ANNUAL FEE"))

    # Direction disambiguates the words that mean opposite things on each side.
    check("a credit with refund wording is a refund",
          flow("UNIQLO ION ORCHARD - REFUND", "credit") == "refund",
          flow("UNIQLO ION ORCHARD - REFUND", "credit"))
    check("the same wording on a debit is not",
          flow("REFUND SPECIALISTS PTE LTD", "debit") == "spend",
          flow("REFUND SPECIALISTS PTE LTD", "debit"))
    check("cashback is income", flow("CASHBACK CREDIT", "credit") == "income",
          flow("CASHBACK CREDIT", "credit"))

    # The conservative half of the rule: ambiguous rows stay spending.
    check("a GIRO bill is still spending", flow("GIRO PAYMENT TO SP SERVICES") == "spend",
          flow("GIRO PAYMENT TO SP SERVICES"))
    check("a PayNow to a shop is still spending", flow("PAYNOW TO HAWKER STALL") == "spend",
          flow("PAYNOW TO HAWKER STALL"))
    check("topping up a transit card is still spending",
          flow("EZ-LINK TOP UP 118") == "spend", flow("EZ-LINK TOP UP 118"))
    check("an ordinary purchase is spend", flow("FAIRPRICE FINEST 203") == "spend",
          flow("FAIRPRICE FINEST 203"))

    # An unrecognized credit leaves the spend total alone rather than netting
    # against it. Understating spending is the worse error here.
    check("an unknown credit does not net against spending",
          flow("SOME CREDIT WE DO NOT KNOW", "credit") == "income",
          flow("SOME CREDIT WE DO NOT KNOW", "credit"))


def test_resolution_order() -> None:
    """Tiers 1 and 2, and what happens when neither knows (DESIGN.md Section 3)."""
    print("\ncategory resolution")
    import categorize
    import merchants

    def rule(pattern, category=None, flow_type=None, match_type="contains"):
        return {"pattern": pattern, "match_type": match_type,
                "category": category, "flow_type": flow_type}

    memory = {"grab": {"category": "Transport", "source": "memory"},
              "grab trip": {"category": "Travel", "source": "memory"},
              "fairprice": {"category": "Groceries", "source": "seed"}}

    def resolve(desc, merchant, direction="debit", rules=()):
        return categorize.resolve(desc, merchant, direction, list(rules), memory)

    check("nothing known is an honest gap, not Other",
          resolve("WHO KNOWS PTE LTD", "who knows") == (None, "spend", None),
          repr(resolve("WHO KNOWS PTE LTD", "who knows")))

    check("memory answers on the exact key",
          resolve("GRAB *TRIP 4821", "grab")[:2] == ("Transport", "spend"),
          repr(resolve("GRAB *TRIP 4821", "grab")))

    # The precise key wins over its own root, or teaching the app about one
    # outlet would be overruled by whatever the root happens to say.
    check("the exact key beats the root",
          resolve("Grab Trip SG", "grab trip")[0] == "Travel",
          repr(resolve("Grab Trip SG", "grab trip")))

    # ...and the root is the fallback that reunites spellings normalization
    # cannot merge on its own.
    check("the root answers when the exact key is unknown",
          resolve("FAIRPRICE FINEST 203", "fairprice finest")[0] == "Groceries",
          repr(resolve("FAIRPRICE FINEST 203", "fairprice finest")))

    check("a seeded answer says it is a seed",
          resolve("FAIRPRICE FINEST 203", "fairprice finest")[2] == "seed",
          repr(resolve("FAIRPRICE FINEST 203", "fairprice finest")))

    # Tier 1 always wins. That is the entire point of it being tier 1.
    check("a rule beats memory",
          resolve("GRAB *TRIP 4821", "grab", rules=[rule("grab", "Travel")])[0] == "Travel",
          repr(resolve("GRAB *TRIP 4821", "grab", rules=[rule("grab", "Travel")])))

    # Rules arrive already ordered by priority; the first match wins.
    ordered = [rule("grab", "Dining"), rule("grab", "Travel")]
    check("the first matching rule wins",
          resolve("GRAB *TRIP", "grab", rules=ordered)[0] == "Dining",
          repr(resolve("GRAB *TRIP", "grab", rules=ordered)))

    # The two axes stay independent: a rule about what something *is* must not
    # silently decide whether it was a purchase or a refund.
    check("a rule with no flow leaves the derived flow alone",
          resolve("GRAB REFUND", "grab", "credit", [rule("grab", "Transport")])[1] == "refund",
          repr(resolve("GRAB REFUND", "grab", "credit", [rule("grab", "Transport")])))
    check("a rule that sets a flow overrides it",
          resolve("SOMETHING", "something", "debit",
                  [rule("something", "Other", "transfer")])[1] == "transfer",
          repr(resolve("SOMETHING", "something", "debit",
                       [rule("something", "Other", "transfer")])))

    # A row that is not spending needs no merchant lookup — the flow already
    # answered the category question.
    check("a card payment categorizes itself",
          resolve("PAYMENT - THANK YOU", "payment thank you", "credit")
          == ("Cash & Transfers", "transfer", "flow"),
          repr(resolve("PAYMENT - THANK YOU", "payment thank you", "credit")))
    check("a fee categorizes itself",
          resolve("INTEREST CHARGE", "interest charge")[0] == "Fees & Interest",
          repr(resolve("INTEREST CHARGE", "interest charge")))

    # ...but a refund does not, because Section 3 nets it against the original
    # merchant, and normalization already keyed it to that merchant.
    refund = resolve("GRAB *TRIP - REFUND", "grab", "credit")
    check("a refund keeps the merchant's category", refund[:2] == ("Transport", "refund"),
          repr(refund))

    # A rule the user broke must fail closed, not take the page down.
    check("an uncompilable regex rule matches nothing",
          resolve("ANYTHING", "anything", rules=[rule("[unclosed", "Other", None, "regex")])
          == (None, "spend", None),
          repr(resolve("ANYTHING", "anything", rules=[rule("[unclosed", "Other", None, "regex")])))
    check("valid_regex rejects it up front", not categorize.valid_regex("[unclosed"))
    check("valid_regex accepts a real one", categorize.valid_regex(r"^bus/mrt\b"))

    # Every seeded key has to survive normalization unchanged, or it can never
    # be looked up and the whole seed list is dead weight that looks alive.
    unreachable = [k for k in categorize.SEED_MEMORY if merchants.normalize(k) != k]
    check("every seeded key is a key normalize() can produce", not unreachable,
          repr(unreachable[:6]))
    unknown = sorted({c for c in categorize.SEED_MEMORY.values()
                      if c not in categorize.CATEGORIES})
    check("every seeded category is one of the thirteen", not unknown, repr(unknown))
    check("every flow implies a category or is deliberately absent",
          set(categorize.CATEGORY_FOR_FLOW) == {"transfer", "fee", "income"},
          repr(sorted(categorize.CATEGORY_FOR_FLOW)))


def test_tier3_gate() -> None:
    """The contract a tier-3 response must pass before anything is stored.

    Phase 0's finding was that a small model fails *silently* — it returns
    something well-formed and empty rather than an error. Every check here is a
    way that can happen, and the gate has to be all-or-nothing about it: a
    response that dropped nine merchants is not "mostly fine", it is a response
    you cannot reason about. Same argument as Section 2.3 refusing to
    half-trust an extraction.
    """
    print("\ntier 3 gate")
    # `ev` for the scoring helpers, `tier3` for the gate itself — the eval
    # re-exports the same function, and naming the source here is what stops
    # this drifting back into testing a copy of it.
    import eval_categories as ev
    import tier3

    asked = ["grab", "bus/mrt", "fairprice"]

    def response(pairs):
        return json.dumps({"assignments": [{"merchant": m, "category": c} for m, c in pairs]})

    ok, problems, answers = ev.gate(
        response([("grab", "Transport"), ("bus/mrt", "Transport"),
                  ("fairprice", "Groceries")]), asked)
    check("a complete, well-formed answer passes", ok and len(answers) == 3, repr(problems))

    # The Phase 0 failure, exactly: valid JSON, correct shape, nothing in it.
    ok, problems, _ = ev.gate('{"assignments": []}', asked)
    check("an empty answer FAILS rather than looking like a clean run",
          not ok and any("dropped" in p for p in problems), repr(problems))

    ok, problems, _ = ev.gate("I think grab is Transport!", asked)
    check("prose instead of JSON fails", not ok, repr(problems))

    ok, problems, _ = ev.gate('{"results": []}', asked)
    check("the right JSON with the wrong shape fails", not ok, repr(problems))

    ok, problems, _ = ev.gate(response([("grab", "Transport"), ("bus/mrt", "Transport")]), asked)
    check("one merchant quietly dropped fails",
          not ok and any("dropped" in p for p in problems), repr(problems))

    ok, problems, _ = ev.gate(
        response([(m, "Transport") for m in asked] + [("netflix", "Entertainment")]), asked)
    check("a merchant it was never asked about fails",
          not ok and any("invented" in p for p in problems), repr(problems))

    ok, problems, _ = ev.gate(
        response([("grab", "Transport"), ("bus/mrt", "Transport"),
                  ("fairprice", "Food & Drink")]), asked)
    check("a category outside the fixed thirteen fails",
          not ok and any("outside" in p for p in problems), repr(problems))

    ok, problems, _ = ev.gate(
        response([("grab", "Transport"), ("grab", "Dining"),
                  ("bus/mrt", "Transport"), ("fairprice", "Groceries")]), asked)
    check("answering one merchant twice fails",
          not ok and any("more than once" in p for p in problems), repr(problems))

    # Abstaining is a legal answer and must not be mistaken for a wrong one.
    # A row that stays uncategorized is honest; a confident wrong guess is
    # stored and silently mislabels the spending.
    ok, _problems, answers = ev.gate(
        response([("grab", "Transport"), ("bus/mrt", ev.ABSTAIN),
                  ("fairprice", "Groceries")]), asked)
    truth = {"grab": "Transport", "bus/mrt": "Transport", "fairprice": "Groceries"}
    s = ev.score(answers, truth)
    check("'unknown' passes the gate", ok, repr(_problems))
    check("...and scores as an abstention, not as wrong",
          (s["correct"], s["wrong"], s["abstained"]) == (2, 0, 1), repr(s))

    # A disagreement is counted as wrong even though it is a real category —
    # that is the number that decides whether tier 3 can be trusted to write.
    _ok, _p, answers = ev.gate(
        response([("grab", "Dining"), ("bus/mrt", "Transport"),
                  ("fairprice", "Groceries")]), asked)
    s = ev.score(answers, truth)
    check("a plausible-but-different answer counts as wrong", s["wrong"] == 1, repr(s))

    # The whole argument for this harness is that it grades the code that runs.
    # An import that quietly becomes a copy would leave every number above
    # describing a prompt and a contract nothing uses.
    check("the eval grades the shipping gate, not a copy of it", ev.gate is tier3.gate)
    check("...and the shipping prompt and schema", ev.SYSTEM is tier3.SYSTEM
          and ev.SCHEMA is tier3.SCHEMA)

    # Grounding is a flag on that same prompt, not a second prompt. An
    # ungrounded run has to stay byte-identical to what Section 3 graded, or
    # the eval is comparing two changes at once and crediting the tool with
    # both of them.
    check("grounding off leaves the graded prompt untouched",
          tier3.system_prompt(False) is tier3.SYSTEM)
    check("...and grounding on only appends to it",
          tier3.system_prompt(True).startswith(tier3.SYSTEM)
          and len(tier3.system_prompt(True)) > len(tier3.SYSTEM))


def test_tier3_client() -> None:
    """Everything around the network call, without making one.

    The API shape is Google's to change; what has to hold here is that a
    reasoning step is not mistaken for the answer, that a batch too big for one
    call is still all-or-nothing per chunk, and that an abstention is never
    written anywhere.
    """
    print("\ntier 3 client")
    import tier3

    # A thinking model's response carries its reasoning in the steps array
    # alongside the answer. Taking the last text block, or joining all of them,
    # would feed prose to the gate and fail every batch.
    body = {"steps": [
        {"type": "thinking", "content": [{"type": "text", "text": "Let me think..."}]},
        {"type": "model_output", "content": [{"type": "text", "text": '{"assignments": []}'}]},
    ]}
    check("the answer is read past a reasoning step",
          tier3._output_text(body) == '{"assignments": []}', repr(tier3._output_text(body)))

    check("the SDK's convenience field is honoured when present",
          tier3._output_text({"output_text": "xyz", "steps": []}) == "xyz")

    check("a response with no output is empty rather than an exception",
          tier3._output_text({}) == "")

    # What actually leaves the machine: merchant keys, one per line, nothing else.
    payload = tier3.prompt_payload(["grab", "fairprice"])
    check("only merchant names are sent", payload == "grab\nfairprice", repr(payload))

    # Batch behaviour, with the network stubbed out. The stub answers the first
    # chunk properly and drops a merchant from the second, which is the failure
    # the gate exists for: the good chunk must still be applied and the bad one
    # must contribute nothing at all.
    keys = [f"m{i}" for i in range(tier3.BATCH_SIZE + 3)]
    calls: list[list[str]] = []
    grounded: list[bool] = []

    def fake_ask(chunk, model=None, key=None, thinking="low", grounding=False):
        calls.append(list(chunk))
        grounded.append(grounding)
        pairs = [(m, "Dining") for m in chunk]
        if len(calls) == 2:
            pairs = pairs[:-1]          # a merchant silently dropped
        else:
            pairs[0] = (pairs[0][0], tier3.ABSTAIN)
        return json.dumps({"assignments": [{"merchant": m, "category": c} for m, c in pairs]})

    # `grounding_enabled` is stubbed too, or this asserts the developer's own
    # `.env` rather than the default the app ships with.
    real_ask, tier3.ask_gemini = tier3.ask_gemini, fake_ask
    real_flag, tier3.grounding_enabled = tier3.grounding_enabled, lambda: False
    try:
        result = tier3.classify(keys, key="test")
    finally:
        tier3.ask_gemini = real_ask
        tier3.grounding_enabled = real_flag

    check("a batch larger than one call is split", len(calls) == 2, repr([len(c) for c in calls]))
    check("no chunk exceeds the batch size",
          all(len(c) <= tier3.BATCH_SIZE for c in calls))
    check("the chunk that failed the gate contributes nothing",
          set(result["assignments"]) == set(keys[:tier3.BATCH_SIZE]) - {"m0"},
          f'{len(result["assignments"])} assignments')
    check("...and says why", any("dropped" in p for p in result["problems"]),
          repr(result["problems"]))
    check("a good chunk still applies when another fails", result["batches_ok"] == 1)

    # Section 9.4 again: the search tool is opt-in, and the screen that asks
    # permission builds its wording from what comes back rather than from what
    # the setting said at boot. So the flag has to reach the client and it has
    # to be reported, and neither is worth taking on trust.
    check("grounding is off unless it is asked for", grounded == [False, False],
          repr(grounded))
    check("...and the result says so", result["grounding"] is False)

    calls.clear()
    grounded.clear()
    real_ask, tier3.ask_gemini = tier3.ask_gemini, fake_ask
    try:
        forced = tier3.classify(["grab"], key="test", grounding=True)
    finally:
        tier3.ask_gemini = real_ask
    check("grounding forced on reaches the client", grounded == [True], repr(grounded))
    check("...and is reported back", forced["grounding"] is True)
    check("an abstention is never returned as an assignment",
          "m0" not in result["assignments"] and result["abstained"] == ["m0"],
          repr(result["abstained"]))

    # A missing key must be a clear error, not an empty result that reads like
    # "the model had nothing to say".
    #
    # `api_key()` is stubbed rather than passing key="" and hoping: the empty
    # string falls through to the real lookup by design, so on a machine that
    # has a key configured this check would sail past the guard and make a live
    # API call — which this file promises never to do.
    real_api_key, tier3.api_key = tier3.api_key, lambda: None
    try:
        tier3.ask_gemini(["grab"], model="x")
        raised = ""
    except tier3.Tier3Error as e:
        raised = str(e)
    except Exception as e:                                   # noqa: BLE001
        raised = f"wrong exception: {e!r}"
    finally:
        tier3.api_key = real_api_key
    check("no API key raises a Tier3Error naming the variable",
          "GEMINI_API_KEY" in raised, repr(raised))

    # And the inverse, which is what makes the stub above trustworthy: a
    # configured key must actually be picked up without being passed in.
    real_api_key, tier3.api_key = tier3.api_key, lambda: "from-the-environment"
    try:
        looked_up = tier3.configured()
    finally:
        tier3.api_key = real_api_key
    check("a configured key is found without being passed in", looked_up)


def test_tier3_retries() -> None:
    """Transient failures, with the network and the clock stubbed out.

    Measured on a free-tier key 2026-08-28: Gemini answers a 43-merchant batch
    in seconds when it answers, but returns 500 "experiencing high demand" under
    load and 429 at five requests a minute. Both are temporary and both say so.
    Giving up on the first one leaves merchants uncategorized for a reason that
    has nothing to do with the merchants.
    """
    print("\ntier 3 retries")
    import io
    import urllib.error

    import tier3

    def http_error(code, body=""):
        return urllib.error.HTTPError(
            tier3.ENDPOINT, code, "err", {}, io.BytesIO(body.encode()))

    def run(outcomes):
        """Play `outcomes` in order; returns (result-or-error, calls, slept)."""
        calls, slept = [], []
        def fake_urlopen(request, timeout=None):
            calls.append(1)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return io.BytesIO(outcome.encode())
        real_open, real_sleep = urllib.request.urlopen, tier3.time.sleep
        tier3.urllib.request.urlopen = fake_urlopen
        tier3.time.sleep = lambda s: slept.append(s)
        try:
            return tier3.ask_gemini(["grab"], key="k"), len(calls), slept
        except tier3.Tier3Error as e:
            return e, len(calls), slept
        finally:
            tier3.urllib.request.urlopen = real_open
            tier3.time.sleep = real_sleep

    good = json.dumps({"steps": [{"type": "model_output", "content": [
        {"type": "text", "text": '{"assignments": []}'}]}]})

    out, calls, _ = run([http_error(500, "high demand"), good])
    check("a 500 is retried and the second attempt is used",
          out == '{"assignments": []}' and calls == 2, f"{out!r} in {calls} call(s)")

    # The quota body carries the real number; guessing either wastes time or
    # comes back early and spends another request against the same empty quota.
    out, calls, slept = run([
        http_error(429, "Quota exceeded. Please retry in 12.5s."), good])
    check("a 429 waits as long as the API asked", slept == [13.0], repr(slept))

    # A rejected schema or a bad key fails identically forever. Retrying it just
    # spends quota and delays the message that would let someone fix it.
    out, calls, _ = run([http_error(400, "bad model id")])
    check("a 400 is final, not retried",
          isinstance(out, tier3.Tier3Error) and calls == 1 and "400" in str(out),
          f"{out!r} in {calls} call(s)")

    out, calls, _ = run([http_error(401, "bad key")])
    check("a 401 is final, not retried", calls == 1, f"{calls} call(s)")

    # The failure that started all this: a read timeout is not a URLError, so
    # it used to escape as a stack trace.
    out, calls, _ = run([TimeoutError("timed out"), good])
    check("a read timeout is retried, not raised as a traceback",
          out == '{"assignments": []}' and calls == 2, f"{out!r} in {calls} call(s)")

    out, calls, _ = run([TimeoutError("t")] * tier3.MAX_ATTEMPTS)
    check("a persistent timeout ends as a Tier3Error saying nothing was stored",
          isinstance(out, tier3.Tier3Error) and "Nothing was stored" in str(out),
          repr(out))

    out, calls, _ = run([http_error(500, "high demand")] * tier3.MAX_ATTEMPTS)
    check("attempts are capped rather than looping",
          isinstance(out, tier3.Tier3Error) and calls == tier3.MAX_ATTEMPTS,
          f"{calls} call(s)")

    # A 60s wait is legal per the API and unacceptable inside a web request.
    out, calls, slept = run([
        http_error(429, "Please retry in 300s."), good])
    check("a wait longer than the cap fails fast instead of hanging the page",
          isinstance(out, tier3.Tier3Error) and slept == [] and calls == 1,
          f"{out!r} slept={slept}")


def test_statement_sort() -> None:
    """Ordering the statement list (`db.STATEMENT_ORDERS`, `main.sort_headers`).

    Two things are being protected. One is that a sort key from a URL never
    reaches SQL as text — an ORDER BY cannot take a bound parameter, so the
    whitelist is the only thing standing between a query string and the query.
    The other is that "sort by period" means the *start* of the period, which
    most issuers never print: getting the fallback wrong would silently clump
    six of eleven statements at one end and look like a sort that half works.
    """
    print("\nstatement sort")
    import db
    import main

    # Every offered ordering is a fixed string with one substitution point, and
    # nothing else. If a key ever grows an f-string this check is the tripwire.
    for key, clause in db.STATEMENT_ORDERS.items():
        check(f"the {key} ordering is a literal", clause.count("{d}") >= 1 and
              "{" not in clause.replace("{d}", ""), repr(clause))

    # An unknown key is the default, not an error and not an injection point.
    check("an unknown sort key falls back to the default",
          db.STATEMENT_ORDERS.get("'; DROP TABLE txn --", db.STATEMENT_ORDERS["statement"])
          == db.STATEMENT_ORDERS["statement"])

    # The list opens grouped by card, A-Z. The route default and the db default
    # are two separate defaults for one behaviour, and `descending` is what
    # makes the named column read forwards, so all three have to agree or the
    # page opens Z-A while every link on it says otherwise.
    import inspect
    route = inspect.signature(main.index).parameters
    db_default = inspect.signature(db.list_statements).parameters
    check("the list defaults to the statement column",
          route["sort"].default == "statement"
          and db_default["sort"].default == "statement",
          f'{route["sort"].default!r} / {db_default["sort"].default!r}')
    check("the default column reads A-Z",
          main.SORT_FIRST_CLICK_DESC["statement"] is False
          and db_default["descending"].default is False,
          f'{db_default["descending"].default!r}')

    # Ordering by period start where the issuer prints one, and by the earliest
    # parsed row where it does not. The DBS shape — statement date only — is
    # the one that has to work, because it is six of the eleven statements.
    check("period sorts on the start, with a floor for issuers that print none",
          "period_start_floor" in db.STATEMENT_ORDERS["period"]
          and "period_start_floor" not in db.STATEMENT_ORDERS["newest"],
          repr(db.STATEMENT_ORDERS["period"]))

    # `newest` is by period END and `period` is by period START. They are not
    # the same ordering even where a corpus makes them look alike, which is why
    # a bookmarked `newest` shows no arrow rather than lighting up the Period
    # header.
    check("the newest order is not the period order",
          db.STATEMENT_ORDERS["newest"] != db.STATEMENT_ORDERS["period"])
    h = main.sort_headers("newest", descending=True)
    check("a bookmarked newest lights up neither header",
          not h["statement"]["active"] and not h["period"]["active"])

    # Clicking the active column flips it; clicking another opens it at its own
    # natural direction. A name reads A-Z, a date reads newest first, and
    # carrying the direction across would open one of them backwards.
    h = main.sort_headers("period", descending=True)
    check("the active column offers the flip", "direction=asc" in h["period"]["href"],
          h["period"]["href"])
    check("the active column shows which way it runs", h["period"]["arrow"] == "▾")
    check("an inactive name column opens A-Z", "direction=asc" in h["statement"]["href"],
          h["statement"]["href"])
    check("an inactive column shows no arrow", h["statement"]["arrow"] == "")

    h = main.sort_headers("statement", descending=False)
    check("the active name column offers the flip",
          "direction=desc" in h["statement"]["href"], h["statement"]["href"])
    check("an inactive date column opens newest first",
          "direction=desc" in h["period"]["href"], h["period"]["href"])

    # Every ordering the route will accept has a first-click direction, or the
    # header for it raises a KeyError the moment somebody adds a fourth sort.
    check("every ordering has a first-click direction",
          set(main.SORT_FIRST_CLICK_DESC) == set(db.STATEMENT_ORDERS),
          repr(set(main.SORT_FIRST_CLICK_DESC) ^ set(db.STATEMENT_ORDERS)))


def test_cli_runs() -> None:
    """Actually invoke the CLI.

    Everything else here imports functions directly, so a NameError in main()
    sails straight through — which is how a broken --dry-run once shipped.
    """
    print("\ncli smoke test")
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("SPIKE_")}

    r = subprocess.run(
        [sys.executable, str(HERE / "extract.py"), "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    check("--dry-run exits 0", r.returncode == 0, r.stderr.strip()[-300:])
    check("--dry-run raises no traceback", "Traceback" not in r.stderr,
          r.stderr.strip()[-300:])

    # The real run must also survive with no arguments and no configuration.
    r2 = subprocess.run(
        [sys.executable, str(HERE / "extract.py")],
        capture_output=True, text=True, env=env,
    )
    check("full run exits 0", r2.returncode == 0, r2.stderr.strip()[-300:])
    check("full run raises no traceback", "Traceback" not in r2.stderr,
          r2.stderr.strip()[-300:])


def test_reconciliation() -> None:
    print("\nreconciliation gate")

    def t(date, amount, direction="debit"):
        return {"date": date, "description": "X", "amount": amount, "direction": direction}

    cases = [
        ("printed totals match", "PASS", dict(
            total_debits="100.00", total_credits="20.00",
            transactions=[t("2026-06-01", "60.00"), t("2026-06-02", "40.00"),
                          t("2026-06-03", "20.00", "credit")])),
        ("a missed transaction is caught", "FAIL", dict(
            total_debits="100.00", transactions=[t("2026-06-01", "60.00")])),
        ("card roll-forward", "PASS", dict(
            opening_balance="1204.55", closing_balance="1016.61",
            transactions=[t("2026-06-01", "1101.96"), t("2026-06-02", "1289.90", "credit")])),
        ("deposit roll-forward (opposite sign convention)", "PASS", dict(
            opening_balance="500.00", closing_balance="600.00",
            transactions=[t("2026-06-01", "100.00"), t("2026-06-02", "200.00", "credit")])),
        ("no totals is UNVERIFIED, not PASS", "UNVERIFIED", dict(
            transactions=[t("2026-06-01", "10.00")])),
        ("a cent of rounding is tolerated", "PASS", dict(
            total_debits="100.01", transactions=[t("2026-06-01", "100.00")])),
        ("ten cents is not", "FAIL", dict(
            total_debits="100.10", transactions=[t("2026-06-01", "100.00")])),
        ("amounts with separators parse", "PASS", dict(
            total_debits="1,204.55", transactions=[t("2026-06-01", "1,204.55")])),
    ]
    for label, expected, stmt in cases:
        r = Result(name=label)
        reconcile(stmt, r)
        check(label, r.verdict == expected, f"got {r.verdict} ({r.detail})")


def test_sanity_checks() -> None:
    print("\nsanity checks")

    def t(date, amount):
        return {"date": date, "description": "X", "amount": amount, "direction": "debit"}

    r = Result(name="s")
    sanity_checks(dict(statement_period_start="2026-06-15", statement_period_end="2026-07-14",
                       transactions=[t("2026-06-20", "1.00"), t("2026-06-20", "1.00"),
                                     t("2026-09-01", "5.00")]), r)
    check("out-of-period date flagged", any("outside period" in w for w in r.warnings))
    check("duplicate row flagged", any("identical row" in w for w in r.warnings))

    # Two rows alike in every field the key used to hold, told apart only by the
    # reference the statement printed for each. This is every UOB statement.
    def ref_row(reference):
        return {"date": "2026-07-08", "description": "ZERO1 PTE LTD SINGAPORE",
                "amount": "7.06", "direction": "debit", "reference": reference}

    r3 = Result(name="refs")
    sanity_checks(dict(transactions=[ref_row("74143256188100091281157"),
                                     ref_row("74143256188100092450348")]), r3)
    check("distinct references are not a duplicate",
          not any("identical row" in w for w in r3.warnings), repr(r3.warnings))

    # ...but a row with no reference is still judged on everything else, so a
    # statement that prints none is no quieter than it was before.
    r4 = Result(name="norefs")
    sanity_checks(dict(transactions=[ref_row(None), ref_row(None)]), r4)
    check("a repeat with no reference is still flagged",
          any("identical row" in w for w in r4.warnings), repr(r4.warnings))

    r2 = Result(name="empty")
    sanity_checks(dict(transactions=[]), r2)
    check("empty statement flagged", any("no transactions" in w for w in r2.warnings))


if __name__ == "__main__":
    test_write_is_platform_independent()
    test_read_is_tolerant()
    test_no_call_site_bypasses_the_helpers()
    test_encryption_paths()
    test_row_detection()
    test_row_parsing()
    test_month_coverage()
    test_cycle_dates()
    test_merchant_normalization()
    test_flow_type()
    test_resolution_order()
    test_tier3_gate()
    test_tier3_client()
    test_tier3_retries()
    test_statement_sort()
    test_cli_runs()
    test_reconciliation()
    test_sanity_checks()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")
