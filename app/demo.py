"""Synthetic statements for the demo: three fictional cards, a year and a bit
of made-up spending, as PDFs.

Why PDFs and not rows written straight into the database: the point of a demo
is to show what the app does, and what it does is parse a statement, check
the parse against the statement's own printed figures, and only then file it.
A demo that skipped the parser would be a demo of a different app. So every
statement here goes through `ingest_statement` exactly as an upload does —
same parser, same reconciliation gate, same categorizer — and if the
generator ever prints a summary that does not add up, the gate says so on the
Statements page instead of the report quietly showing wrong numbers.

The banks are invented. Meridian, Harbour and Northgate are not real issuers,
the card numbers are the industry's published test numbers, and every PDF says
on its face that it is synthetic. The merchants are the kind of names that
appear on a Singapore statement, because the categorizer's shipped guesses
(`categorize.SEED_MEMORY`) are keyed to those, and a demo whose rows all came
up uncategorized would misrepresent the app in the other direction. A few are
deliberately outside the seed list so the Merchants sweep has something to do.

Deterministic on purpose. Each cycle's rows come from a generator seeded by
the cycle, `invariant=1` fixes the PDF metadata, so the same period always
yields byte-identical files — pressing the demo button twice files nothing
twice, because the second set is caught by the same `file_sha256` check that
catches a real statement uploaded twice. The window slides with the calendar:
the newest statement closes on the most recent 14th, so the demo never looks
stale, and next month's press adds one cycle per card and skips the rest.

    python app/demo.py [out_dir]    writes the PDFs to a folder (default: demo-statements/)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# Statements close on this day of the month; each cycle runs from the 15th to
# the 14th. Mid-month closes are the normal case in the real corpus, and they
# are what makes the newest month show as part-billed — which the demo should
# show, because that indicator is one of the things worth demonstrating.
CLOSE_DAY = 14
# How many cycles per card the demo files. Sixteen closes gives fourteen
# complete calendar months, enough for the year-over-year overlay on
# /analytics to have two Julys and two Augusts to draw.
CYCLES = 16
# History is generated from here so a cycle's opening balance is the previous
# cycle's close all the way back, whichever window is emitted.
EPOCH = dt.date(2024, 1, 15)
ROWS_PER_PAGE = 40
FILENAME_PREFIX = "demo-"


class Card(NamedTuple):
    issuer: str
    product: str
    number: str        # a published test number, never a real one
    slug: str

    @property
    def last4(self) -> str:
        return self.number[-4:]


CARDS = [
    Card("Meridian Bank", "Platinum Credit Card", "4111 1111 1111 1111", "meridian-bank"),
    Card("Harbour Bank", "Everyday Credit Card", "5555 5555 5555 4444", "harbour-bank"),
    Card("Northgate Bank", "Cashback Credit Card", "4012 8888 8888 1881", "northgate-bank"),
]


@dataclass(frozen=True)
class Habit:
    """One recurring kind of charge, as the statement would print it.

    `desc` may carry `{n}`, filled with a per-charge reference so the row reads
    like a real descriptor — and so that `merchants.py` has junk to cut off.
    `cards` are indexes into CARDS; a charge lands on one of them at random.
    `per_cycle` is the expected count per cycle; `day` pins a subscription to a
    day of the month instead. `fx` prints the charge as a foreign one, in the
    three-line shape `rows.py` reassembles: the merchant on the line above, the
    foreign figure beside the SGD one on the dated line, and the statement's
    own rate (`1 JPY = 0.008750 SGD`) on the line below.
    """
    desc: str
    lo: int                     # cents
    hi: int                     # cents
    per_cycle: float = 1.0
    cards: tuple[int, ...] = (0,)
    day: int | None = None
    fx: tuple[str, str] | None = None     # (currency, rate as printed)
    season: dict[int, float] | None = None  # multiplier by calendar month


# Grouped by what the categorizer will make of them. Comments say which tier
# answers, so the mix stays honest when the seed list changes.
HABITS: list[Habit] = [
    # Groceries — seeded roots
    Habit("FAIRPRICE FINEST {n} SINGAPORE SG", 3800, 14500, 3.0, (0,)),
    Habit("NTUC FP-JURONG POINT SINGAPORE SG", 2200, 9000, 1.5, (0,)),
    Habit("GIANT HYPERMARKET TAMPINES SG", 4500, 12000, 0.7, (0, 2)),
    Habit("REDMART SINGAPORE SG", 6000, 16000, 1.2, (1,)),
    Habit("DON DON DONKI ORCHARD CENTRAL SG", 1800, 6500, 0.6, (0,)),
    # Groceries — NOT seeded (root "sheng"): the biggest uncategorized line,
    # so the Merchants sweep shows one decision settling many rows.
    Habit("SHENG SIONG SUPERMARKET SG", 3000, 9500, 2.0, (0,)),
    # Dining — seeded
    Habit("STARBUCKS @ RAFFLES CITY SG", 680, 1420, 2.5, (0, 1)),
    Habit("STARBUCKS @ JEWEL CHANGI SG", 680, 1420, 1.0, (0, 1)),     # a second outlet, for /merchants/groups
    Habit("KFC TAMPINES MALL SG", 900, 2600, 1.2, (0,)),
    Habit("SUBWAY RAFFLES PLACE SG", 850, 1500, 1.0, (0,)),
    Habit("FOODPANDA SINGAPORE SG", 1500, 4800, 3.0, (1,), season={12: 1.3}),
    Habit("DELIVEROO SINGAPORE SG", 1800, 5200, 1.5, (1,)),
    Habit("CHAGEE SINGAPORE SG", 550, 980, 1.5, (0, 1)),
    Habit("LIHO TEA SG", 380, 720, 1.0, (0,)),
    Habit("KOI THE BUGIS SG", 380, 760, 0.8, (0,)),
    Habit("JOLLIBEE SG", 900, 2200, 0.5, (0,)),
    # Dining — NOT seeded
    Habit("MCDONALD'S BEDOK MALL SG", 700, 1900, 1.5, (0,)),
    Habit("TIONG BAHRU BAKERY SG", 900, 2800, 0.8, (0, 1)),
    Habit("TOAST BOX JEM SG", 450, 1100, 0.7, (0,)),
    Habit("DIN TAI FUNG PARAGON SG", 4800, 11000, 0.4, (0, 2)),
    Habit("ATLAS COFFEEHOUSE SG", 1200, 3600, 0.5, (1,)),
    # Transport — seeded
    Habit("GRAB *TRIP {n} SINGAPORE SG", 800, 3200, 6.0, (0, 1)),
    Habit("BUS/MRT {n} SINGAPORE SG", 300, 900, 5.0, (0,)),
    Habit("COMFORTDELGRO TAXI SG", 1200, 3400, 1.0, (0,)),
    Habit("SHELL STATION 118 SINGAPORE SG", 6500, 9800, 1.5, (2,)),
    Habit("ESSO MOUNT FABER SG", 6000, 9500, 0.5, (2,)),
    Habit("GETGO SINGAPORE SG", 2400, 7800, 0.5, (2,)),
    # Bills & Utilities — seeded, on a fixed day
    Habit("SP GROUP SINGAPORE SG", 11000, 19500, cards=(0,), day=3, season={6: 1.25, 7: 1.3, 8: 1.2}),
    Habit("SINGTEL MOBILE SINGAPORE SG", 4580, 4580, cards=(0,), day=21),
    Habit("MYREPUBLIC SINGAPORE SG", 3999, 3999, cards=(1,), day=8),
    Habit("M1 LIMITED SINGAPORE SG", 2500, 2500, cards=(1,), day=12),
    # Bills — NOT seeded (insurance)
    Habit("AIA SINGAPORE PREMIUM SG", 18650, 18650, cards=(2,), day=1),
    # Health — seeded
    Habit("WATSONS PERSONAL CARE SG", 900, 4200, 1.0, (0, 2)),
    Habit("GUARDIAN HEALTH SG", 800, 3800, 0.8, (0,)),
    Habit("RAFFLES MEDICAL SINGAPORE SG", 3800, 12000, 0.25, (2,)),
    Habit("UNITY PHARMACY SG", 600, 2600, 0.4, (0,)),
    # Entertainment — seeded, subscriptions on a fixed day
    Habit("NETFLIX.COM 866-579-7172 SG", 1998, 1998, cards=(1,), day=17),
    Habit("SPOTIFY SINGAPORE SG", 1098, 1098, cards=(1,), day=23),
    Habit("DISNEY PLUS SINGAPORE SG", 1198, 1198, cards=(1,), day=27),
    Habit("SHAW THEATRES LIDO SG", 1300, 3900, 0.6, (0, 1)),
    Habit("PLAYSTATION NETWORK SG", 2990, 8990, 0.3, (1,), season={11: 1.5, 12: 1.8}),
    # Entertainment — NOT seeded on purpose (apple is subscriptions on one row
    # and hardware on the next; categorize.py refuses to guess)
    Habit("APPLE.COM/BILL ITUNES.COM SG", 1298, 1298, cards=(1,), day=5),
    # Shopping — seeded
    Habit("UNIQLO ION ORCHARD SINGAPORE SG", 2990, 15900, 0.7, (0, 1), season={11: 1.4, 12: 2.0}),
    Habit("UNIQLO JEM SINGAPORE SG", 2990, 12900, 0.3, (0, 1), season={12: 2.0}),
    Habit("AMZN Mktp SG*{n} AMAZON.SG", 1500, 24000, 1.5, (1,), season={11: 1.5, 12: 2.0}),
    Habit("SHOPEE SINGAPORE SG", 800, 9800, 2.0, (1,), season={11: 1.6, 12: 1.6}),
    Habit("LAZADA SINGAPORE SG", 1200, 8800, 1.0, (1,), season={11: 1.6}),
    Habit("IKEA TAMPINES SG", 2500, 32000, 0.25, (2,)),
    Habit("DECATHLON SINGAPORE SG", 1900, 14000, 0.3, (2,)),
    Habit("MUJI PLAZA SINGAPURA SG", 1500, 8900, 0.4, (0,)),
    Habit("DAISO SINGAPORE SG", 480, 2600, 0.6, (0,)),
    Habit("POPULAR BOOKSTORE SG", 900, 6500, 0.3, (0,)),
    Habit("CHALLENGER SINGAPORE SG", 2900, 38000, 0.2, (2,)),
    # Travel — seeded; the fx ones print as foreign charges
    Habit("AGODA.COM SINGAPORE SG", 18000, 62000, 0.15, (2,)),
    Habit("KLOOK SINGAPORE SG", 4500, 21000, 0.2, (2,), season={6: 3.0, 12: 3.0}),
    Habit("CHANGI AIRPORT GROUP SG", 800, 4200, 0.2, (0, 2)),
    # Education — seeded
    Habit("UDEMY ONLINE SG", 1499, 3999, 0.2, (1,)),
    Habit("COURSERA.ORG SG", 6500, 6500, 0.1, (1,)),
]

# Once-a-year events, keyed by the calendar month of the cycle's *close*. A
# trip is booked two months out and paid for on the ground; the foreign rows
# are what exercise the three-line parse and the fx columns on /transactions.
EVENTS: dict[int, list[Habit]] = {
    4: [Habit("SINGAPORE AIRLINES SINGAPORE SG", 84000, 168000, cards=(2,), day=22)],
    6: [Habit("HOTEL GRACERY SHINJUKU TOKYO JP", 6800000, 9800000, cards=(2,), day=3, fx=("JPY", "0.008750")),
        Habit("7-ELEVEN SHINJUKU TOKYO JP", 80000, 240000, cards=(2,), day=5, fx=("JPY", "0.008750"))],
    10: [Habit("SCOOT SINGAPORE SG", 32000, 68000, cards=(2,), day=9)],
    12: [Habit("BOOKING.COM AMSTERDAM NL", 28500, 41000, cards=(2,), day=2, fx=("EUR", "1.4456")),
         Habit("ALBERT HEIJN 1122 AMSTERDAM NL", 1800, 6400, cards=(2,), day=4, fx=("EUR", "1.4456"))],
}
# Every October, one card charges its annual fee (flow: fee); every cycle the
# cashback card returns a little (flow: income).
ANNUAL_FEE = Habit("ANNUAL MEMBERSHIP FEE", 19260, 19260, cards=(0,), day=16)
CASHBACK = "CASHBACK REBATE"


class Row(NamedTuple):
    date: dt.date
    posted: dt.date
    desc: str
    cents: int              # SGD, always positive; `credit` says which way
    credit: bool = False
    foreign: tuple[str, int, str] | None = None   # (currency, minor units, rate as printed)


class Statement(NamedTuple):
    card: Card
    start: dt.date
    end: dt.date
    opening: int            # cents; negative means the card is in credit
    rows: list[Row]

    @property
    def debits(self) -> int:
        return sum(r.cents for r in self.rows if not r.credit)

    @property
    def credits(self) -> int:
        return sum(r.cents for r in self.rows if r.credit)

    @property
    def closing(self) -> int:
        return self.opening + self.debits - self.credits

    @property
    def filename(self) -> str:
        return f"{FILENAME_PREFIX}{self.card.slug}-{self.end.isoformat()}.pdf"


# ------------------------------------------------------------------- cycles

def latest_close(today: dt.date) -> dt.date:
    """The most recent statement close on or before `today`."""
    close = today.replace(day=CLOSE_DAY)
    if close > today:
        close = (close.replace(day=1) - dt.timedelta(days=1)).replace(day=CLOSE_DAY)
    return close


def cycle_before(close: dt.date) -> dt.date:
    """The previous month's close."""
    return (close.replace(day=1) - dt.timedelta(days=1)).replace(day=CLOSE_DAY)


def cycles(today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Every (start, end) from EPOCH up to the latest close, oldest first."""
    out = []
    close = latest_close(today)
    while close > EPOCH:
        out.append((cycle_before(close) + dt.timedelta(days=1), close))
        close = cycle_before(close)
    return list(reversed(out))


def _day_in(start: dt.date, end: dt.date, day: int) -> dt.date:
    """The one date in the cycle with that day-of-month."""
    candidate = start.replace(day=min(day, 28))
    if candidate < start:
        candidate = end.replace(day=min(day, 28))
    return candidate


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while p > limit:
        k += 1
        p *= rng.random()
    return k - 1


def _charge(rng: random.Random, habit: Habit, on: dt.date, start: dt.date, end: dt.date) -> tuple[int, Row]:
    """One charge of this habit, on this day. Returns (card index, row)."""
    card = rng.choice(habit.cards)
    # A charge that posts after the close belongs to the next statement, so the
    # posting lag is clipped at the cycle end rather than spilling past it.
    posted = min(on + dt.timedelta(days=rng.choice((0, 1, 1, 2))), end)
    desc = habit.desc.replace("{n}", str(rng.randint(1000, 9999)))
    if habit.fx:
        currency, rate = habit.fx
        foreign = rng.randint(habit.lo, habit.hi)
        # The SGD figure is what the printed totals add up, so it is fixed here
        # and the rate is what the statement claims — the same as a real one.
        return card, Row(on, posted, desc, round(foreign * float(rate)),
                         foreign=(currency, foreign, rate))
    return card, Row(on, posted, desc, rng.randint(habit.lo, habit.hi))


def cycle_rows(start: dt.date, end: dt.date) -> dict[int, list[Row]]:
    """Every charge in one cycle, by card. Seeded by the cycle, so stable."""
    rng = random.Random(f"demo|{start.isoformat()}")
    days = (end - start).days + 1
    by_card: dict[int, list[Row]] = {i: [] for i in range(len(CARDS))}

    def add(habit: Habit, on: dt.date) -> None:
        card, row = _charge(rng, habit, on, start, end)
        by_card[card].append(row)

    for habit in HABITS:
        if habit.day is not None:
            add(habit, _day_in(start, end, habit.day))
            continue
        scale = (habit.season or {}).get(end.month, 1.0)
        for _ in range(_poisson(rng, habit.per_cycle * scale)):
            add(habit, start + dt.timedelta(days=rng.randrange(days)))

    for habit in EVENTS.get(end.month, []):
        add(habit, _day_in(start, end, habit.day or 10))
    if end.month == 10:
        add(ANNUAL_FEE, _day_in(start, end, ANNUAL_FEE.day or 16))

    return by_card


def statements(today: dt.date | None = None, count: int = CYCLES) -> list[Statement]:
    """The last `count` statements per card, balances carried from EPOCH."""
    today = today or dt.date.today()
    balance = [0] * len(CARDS)
    out: list[Statement] = []
    for start, end in cycles(today):
        by_card = cycle_rows(start, end)
        for i, card in enumerate(CARDS):
            rows = by_card[i]
            rng = random.Random(f"demo|{card.slug}|{start.isoformat()}")
            # Last cycle's balance, paid in full — the row every real
            # statement has and the one the flow classifier must not count as
            # spending.
            if balance[i] > 0:
                on = _day_in(start, end, 8 + rng.randrange(4))
                rows.append(Row(on, on, "PAYMENT - THANK YOU", balance[i], credit=True))
            # The cashback card gives a little back each cycle (flow: income).
            if i == 2 and rows:
                on = _day_in(start, end, 14)
                rows.append(Row(on, on, CASHBACK, rng.randint(400, 2400), credit=True))
            # Now and then something goes back — the same merchant plus a
            # marker, so normalization keys it to the purchase (flow: refund).
            purchases = [r for r in rows
                         if not r.credit and r.foreign is None and r.cents >= 2000
                         and ("UNIQLO" in r.desc or "AMZN" in r.desc)]
            if purchases and rng.random() < 0.35:
                back = rng.choice(purchases)
                on = min(back.posted + dt.timedelta(days=rng.randint(3, 9)), end)
                rows.append(Row(on, on, back.desc.split(" SINGAPORE")[0].split(" SG*")[0] + " - REFUND",
                                back.cents, credit=True))
            rows.sort(key=lambda r: (r.date, r.posted, r.desc))
            stmt = Statement(card, start, end, balance[i], rows)
            balance[i] = stmt.closing
            out.append(stmt)
    # Cycle-major order, so the tail is exactly the last `count` cycles.
    return out[-count * len(CARDS):]


# ---------------------------------------------------------------------- pdf

def _date(d: dt.date) -> str:
    return d.strftime("%d %b").upper()


def _long(d: dt.date) -> str:
    return d.strftime("%d %b %Y")


def _money(cents: int) -> str:
    return f"{abs(cents) / 100:,.2f}" + (" CR" if cents < 0 else "")


def render(stmt: Statement) -> bytes:
    """One statement as a PDF, in the layout the parser was proven on."""
    buf = io.BytesIO()
    # invariant=1 pins the creation date and document id, which is what makes
    # the bytes — and so the sha256 the app dedups on — a pure function of the
    # statement.
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    w, h = A4
    left, right = 60, w - 60
    card = stmt.card

    def header(continued: bool) -> float:
        c.setFont("Helvetica-Bold", 16)
        c.drawString(left, h - 60, card.issuer.upper())
        c.setFont("Helvetica", 9)
        c.drawString(left, h - 76, f"{card.product} Statement")
        c.drawRightString(right, h - 60, f"Card No: {card.number}" + (" (continued)" if continued else ""))
        c.drawRightString(right, h - 76, f"Statement Period: {_long(stmt.start)} to {_long(stmt.end)}")
        c.drawRightString(right, h - 90, f"Payment Due Date: {_long(stmt.end + dt.timedelta(days=21))}")
        return h - 120

    def table_head(y: float) -> float:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(left, y, "TRANS DATE")
        c.drawString(130, y, "POST DATE")
        c.drawString(200, y, "DESCRIPTION")
        c.drawRightString(right, y, "AMOUNT (SGD)")
        y -= 6
        c.line(left, y, right, y)
        return y - 14

    def footer(y: float) -> None:
        c.setFont("Helvetica-Oblique", 7.5)
        for line in [
            f"This is a synthetic statement generated by the spending tracker's demo mode. "
            f"{card.issuer} is not a real bank and these transactions never happened.",
            "Minimum payment 3% of the outstanding balance or SGD 50, whichever is higher.",
            "Interest at 27.8% p.a. applies to unpaid balances from the transaction date.",
        ]:
            c.drawString(left, y, line)
            y -= 11

    y = header(continued=False)
    c.setFont("Helvetica", 9)
    for label, value, bold in [
        ("Previous Balance", stmt.opening, False),
        ("Total Payments and Credits", stmt.credits, False),
        ("Total Purchases and Charges", stmt.debits, False),
        ("New Balance", stmt.closing, True),
    ]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(left, y, label)
        c.drawRightString(right, y, _money(value))
        y -= 14

    y -= 20
    y = table_head(y)
    c.setFont("Helvetica", 8.5)
    on_page = 0
    for row in stmt.rows:
        lines = 3 if row.foreign else 1
        if on_page + lines > ROWS_PER_PAGE:
            c.showPage()
            y = table_head(header(continued=True))
            c.setFont("Helvetica", 8.5)
            on_page = 0
        if row.foreign:
            # Merchant above, both figures on the dated line, the rate below.
            currency, minor, rate = row.foreign
            c.drawString(200, y, row.desc)
            y -= 13
            c.drawString(left, y, _date(row.date))
            c.drawString(130, y, _date(row.posted))
            c.drawRightString(right - 110, y, f"{minor / 100:,.2f} {currency}")
            c.drawRightString(right, y, _money(row.cents))
            y -= 13
            c.drawString(200, y, f"1 {currency} = {rate} SGD")
            y -= 13
        else:
            c.drawString(left, y, _date(row.date))
            c.drawString(130, y, _date(row.posted))
            c.drawString(200, y, row.desc)
            c.drawRightString(right, y, _money(-row.cents if row.credit else row.cents))
            y -= 13
        on_page += lines

    y -= 10
    c.line(left, y, right, y)
    footer(y - 24)
    c.save()
    return buf.getvalue()


class DemoFile(NamedTuple):
    filename: str
    payload: bytes
    card: Card


def files(today: dt.date | None = None) -> list[DemoFile]:
    """The demo set, ready for `ingest_statement`."""
    return [DemoFile(s.filename, render(s), s.card) for s in statements(today)]


def is_demo(filename: str) -> bool:
    return filename.startswith(FILENAME_PREFIX)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("demo-statements")
    out.mkdir(parents=True, exist_ok=True)
    demo = files()
    for f in demo:
        (out / f.filename).write_bytes(f.payload)
    digest = hashlib.sha256(b"".join(f.payload for f in demo)).hexdigest()[:12]
    print(f"wrote {len(demo)} statements for {len(CARDS)} cards to {out}/ (set {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
