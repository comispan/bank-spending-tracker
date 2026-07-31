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
    test_reconciliation()
    test_sanity_checks()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")
