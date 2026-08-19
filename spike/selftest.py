"""Self-checks for the spike. No network, no API key, no real statements.

    python spike/selftest.py

Covers the two things that can silently produce wrong answers: text encoding
(which broke on Windows) and the reconciliation gate (which is the whole point
of the exercise).
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

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
    period, year = rows.document_context([page1, page2])
    check("period found across the document", period == ("2026-06-15", "2026-07-14"), repr(period))
    later = rows.parse_page(page2, period, year)
    check("a page with no printed year still dates its rows",
          [t["date"] for t in later["transactions"]] == ["2026-06-20"],
          repr(later["transactions"]))

    # Due dates and rate tables are date-and-number lines too, and must not be
    # mistaken for transactions.
    noise = rows.parse_page("Payment Due Date: 04 Aug 2026\nInterest 27.80\n", ("2026-06-15", "2026-07-14"), 2026)
    check("a due date is not a transaction", noise["transactions"] == [],
          repr(noise["transactions"]))


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
