"""Generate a synthetic statement PDF so the pipeline can be smoke-tested
without real financial data. Deliberately imitates the awkward parts of a real
statement: right-aligned amount column, marketing filler, a payment row, a
refund, a foreign-currency subline, and printed totals to reconcile against.

    python make_sample.py    ->  statements/sample-statement.pdf
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "statements" / "sample-statement.pdf"

ROWS = [
    ("15 JUN", "16 JUN", "FAIRPRICE FINEST 203 SINGAPORE SG", "82.15"),
    ("16 JUN", "17 JUN", "GRAB *TRIP 4821 SINGAPORE SG", "14.20"),
    ("17 JUN", "18 JUN", "NETFLIX.COM 866-579-7172 SG", "19.98"),
    ("18 JUN", "20 JUN", "AMZN Mktp SG*RT4G91 AMAZON.SG", "156.40"),
    ("19 JUN", "20 JUN", "STARBUCKS @ RAFFLES CITY SG", "7.80"),
    ("21 JUN", "22 JUN", "SHELL STATION 118 SINGAPORE SG", "68.00"),
    ("23 JUN", "24 JUN", "APPLE.COM/BILL ITUNES.COM SG", "12.98"),
    ("25 JUN", "26 JUN", "UNIQLO ION ORCHARD SINGAPORE SG", "89.90"),
    ("28 JUN", "29 JUN", "GRAB *TRIP 9903 SINGAPORE SG", "22.60"),
    ("01 JUL", "02 JUL", "SP DIGITAL UTILITIES SINGAPORE", "143.25"),
    ("03 JUL", "04 JUL", "BOOKING.COM AMSTERDAM NL", "412.00"),
    ("03 JUL", "04 JUL", "    FOREIGN CURRENCY EUR 285.00 @ 1.4456", None),
    ("05 JUL", "06 JUL", "COLD STORAGE 88 SINGAPORE SG", "54.30"),
    ("08 JUL", "09 JUL", "UNIQLO ION ORCHARD - REFUND", "-89.90"),
    ("10 JUL", "10 JUL", "PAYMENT - THANK YOU", "-1200.00"),
    ("12 JUL", "13 JUL", "GRAB *TRIP 1174 SINGAPORE SG", "18.40"),
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(OUT), pagesize=A4)
    w, h = A4
    right = w - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, h - 60, "MERIDIAN BANK")
    c.setFont("Helvetica", 9)
    c.drawString(60, h - 76, "Platinum Credit Card Statement")
    c.drawRightString(right, h - 60, "Card No: 4532 1122 8891 7743")
    c.drawRightString(right, h - 76, "Statement Period: 15 Jun 2026 to 14 Jul 2026")
    c.drawRightString(right, h - 90, "Payment Due Date: 04 Aug 2026")

    y = h - 120
    c.setFont("Helvetica", 9)
    c.drawString(60, y, "Previous Balance")
    c.drawRightString(right, y, "1,204.55")
    y -= 14
    c.drawString(60, y, "Total Payments and Credits")
    c.drawRightString(right, y, "1,289.90")
    y -= 14
    c.drawString(60, y, "Total Purchases and Charges")
    c.drawRightString(right, y, "1,101.96")
    y -= 14
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "New Balance")
    c.drawRightString(right, y, "1,016.61")

    y -= 34
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "TRANS DATE")
    c.drawString(130, y, "POST DATE")
    c.drawString(200, y, "DESCRIPTION")
    c.drawRightString(right, y, "AMOUNT (SGD)")
    y -= 6
    c.line(60, y, right, y)

    y -= 14
    c.setFont("Helvetica", 8.5)
    for trans, post, desc, amt in ROWS:
        if amt is None:                      # continuation subline, no amount
            c.setFont("Helvetica-Oblique", 7.5)
            c.drawString(200, y, desc.strip())
            c.setFont("Helvetica", 8.5)
        else:
            c.drawString(60, y, trans)
            c.drawString(130, y, post)
            c.drawString(200, y, desc)
            c.drawRightString(right, y, amt.replace("-", "") + (" CR" if amt.startswith("-") else ""))
        y -= 13

    y -= 10
    c.line(60, y, right, y)
    y -= 24
    c.setFont("Helvetica-Oblique", 7.5)
    for line in [
        "Earn 4X rewards points on dining this quarter. Terms and conditions apply.",
        "Minimum payment 3% of the outstanding balance or SGD 50, whichever is higher.",
        "Interest at 27.8% p.a. applies to unpaid balances from the transaction date.",
    ]:
        c.drawString(60, y, line)
        y -= 11

    c.save()
    print(f"wrote {OUT}")
    print("Expected: 13 debits totalling 1101.96, 2 credits totalling 1289.90")
    print("Roll-forward: 1204.55 + 1101.96 - 1289.90 = 1016.61")


if __name__ == "__main__":
    main()
