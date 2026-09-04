"""Deterministic transaction-row parsing from a statement's text layer.

Why this exists: `pdfplumber.extract_text(layout=True)` already recovers the
transaction table — columns aligned, amounts on the right row. Asking a model
to "find the table" is asking it to redo work that is already done, and it is
the step small local models fail at, badly and silently.

So the table is parsed here, in code, and the model (if used at all) is left
only the genuinely ambiguous residue. Everything in this module runs offline in
milliseconds and is exactly reproducible, which also means a wrong answer is a
bug you can fix rather than a sampling artefact you can only re-roll.

The output dict is the same shape the model path produced, so `reconcile()`
grades both identically.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = "|".join(MONTHS)

# ---------------------------------------------------------------- date tokens
#
# One date, and only one. The inline year must be four digits: a two-digit year
# is indistinguishable from the day of the *next* date, and statements that
# print a posting date alongside a transaction date ("27 JUN 28 JUN") are
# common enough that a greedy year would swallow half of them. Numeric forms
# use separators, so `05/06/26` stays unambiguous.
DATE_TOKEN = re.compile(
    rf"""(?ix)
    \s* (?:
        (?P<d1>\d{{1,2}}) [ /-] (?P<m1>{MONTH_RE})[a-z]* (?: [ /-] (?P<y1>\d{{4}}) )?
      | (?P<m2>{MONTH_RE})[a-z]* [ /-] (?P<d2>\d{{1,2}}) (?: [ /-] (?P<y2>\d{{4}}) )?
      | (?P<d3>\d{{1,2}}) [/-] (?P<m3>\d{{1,2}}) (?: [/-] (?P<y3>\d{{2,4}}) )?
      | (?P<y4>\d{{4}}) - (?P<m4>\d{{2}}) - (?P<d4>\d{{2}})
    ) (?![\w/-])
    """
)

# An amount at the end of a line, with whatever the issuer wraps it in:
# a trailing CR/DR marker, a leading sign, parentheses, a currency prefix.
AMOUNT_TAIL = re.compile(
    r"""(?ix)
    (?P<open>\()?
    (?P<sign>[+-])?
    (?:S\$|SGD|USD|\$)?\s?
    (?P<num>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})
    (?P<close>\))?
    \s* (?P<marker>CR|DR)?
    \s*$
    """
)
ANY_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2}")

# The issuer's own reference for a single transaction. Two shapes in the corpus,
# and they need different handling:
#   UOB and DBS put it on a continuation line under the row
#       Ref No. : 74143256188100091281157
#   Standard Chartered writes it inline, inside the description
#       BUS/MRT 901852743 SINGAPORE SG Transaction Ref 74541836217288081589523
# It is the only thing that distinguishes two genuine same-day, same-amount
# charges to the same merchant, which is why it is worth carrying: UOB bills two
# ZERO1 lines at 7.06 on the same date every month, and without the reference
# those two rows are indistinguishable from one page read twice.
#
# `\bref\b` will not match "refer" or "referred", and the value must be at least
# six characters, so prose like "Reference: see page 3" claims nothing.
# The punctuation class is a single run rather than `\s*[.:#]*\s*` because UOB
# writes `Ref No. : 7414...` — a full stop, a space, and a colon.
REF_LABEL = r"(?:transaction\s+)?ref(?:erence)?\b(?:\s*(?:no|num(?:ber)?|id)\b)?[\s.:#]*"
REF_VALUE = r"([A-Z0-9][A-Z0-9/-]{5,})"
REF_LINE = re.compile(rf"(?i)^\s*{REF_LABEL}{REF_VALUE}\s*$")
REF_INLINE = re.compile(rf"(?i)\b{REF_LABEL}{REF_VALUE}\b")

# A charge in another currency is printed across three lines, not one (Section 4):
#
#                          Pinduoduo
#     02 Jul   04 Jul                   102.67 HKD    16.99
#                           1 HKD = 0.1655 SGD
#
# The dated line carries both figures, so the amount comes off the end as
# usual and what is left over is the *foreign* amount rather than a merchant.
# Left alone it becomes the whole description, which is how the Trust HKD row
# reconciled to the cent while losing the merchant name entirely — a row no
# categorizer can ever resolve, because there is nothing in it to resolve.
FOREIGN_DESC = re.compile(
    r"""(?ix) ^\s*
    (?:(?P<cur_pre>[A-Z]{3})\s*)?
    (?P<num>\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})
    (?:\s*(?P<cur_post>[A-Z]{3}))?
    \s*$"""
)
# `1 HKD = 0.1655 SGD`. The rate the *statement* printed, which Section 4
# requires over today's rate: it is the rate the money actually changed at.
FX_RATE_LINE = re.compile(
    r"""(?ix) \b1\s+(?P<from>[A-Z]{3})\s*=\s*
    (?P<rate>\d+(?:\.\d+)?)\s*(?P<to>[A-Z]{3})\b"""
)
# The other wording issuers use for the same subline: `FOREIGN CURRENCY EUR
# 285.00 @ 1.4456` rather than `1 EUR = ... SGD`. Same job, same risk if it is
# ever mistaken for the *next* row's merchant: it always sits directly above
# whatever transaction follows it.
FOREIGN_NOTE_LINE = re.compile(r"(?i)^\s*foreign\s+currency\b")


class Row:
    __slots__ = ("day", "month", "year", "posted", "description", "amount",
                 "direction", "ambiguous")


def _token_to_parts(m: re.Match) -> tuple[int, int, int | None]:
    """(day, month, year|None) from whichever alternative matched."""
    if m.group("d1"):
        return int(m.group("d1")), MONTHS[m.group("m1").lower()[:3]], _year(m.group("y1"))
    if m.group("d2"):
        return int(m.group("d2")), MONTHS[m.group("m2").lower()[:3]], _year(m.group("y2"))
    if m.group("d3"):
        return int(m.group("d3")), int(m.group("m3")), _year(m.group("y3"))
    return int(m.group("d4")), int(m.group("m4")), _year(m.group("y4"))


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    v = int(raw)
    return 2000 + v if v < 100 else v


def leading_dates(line: str, limit: int = 2) -> tuple[list[tuple[int, int, int | None]], str]:
    """Peel up to `limit` date tokens off the front. Returns (dates, remainder)."""
    dates, rest = [], line
    while len(dates) < limit:
        m = DATE_TOKEN.match(rest)
        if not m:
            break
        dates.append(_token_to_parts(m))
        rest = rest[m.end():]
    return dates, rest


def parse_amount(rest: str) -> tuple[Decimal, str, str, bool] | None:
    """Split a row remainder into (amount, direction, description, ambiguous).

    Direction, in order of how much the statement is actually telling us:
      1. an explicit CR/DR marker
      2. a sign, or accounting parentheses
      3. debit — the overwhelmingly common case, and the one reconciliation
         will contradict loudly if it is wrong
    """
    m = AMOUNT_TAIL.search(rest)
    if not m:
        return None
    try:
        amount = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None

    marker = (m.group("marker") or "").upper()
    if marker:
        direction = "credit" if marker == "CR" else "debit"
    elif m.group("sign") == "+":
        direction = "credit"
    elif m.group("sign") == "-" or (m.group("open") and m.group("close")):
        direction = "debit"
    else:
        direction = "debit"

    description = re.sub(r"\s{2,}", " ", rest[:m.start()]).strip(" .-–—")
    # More than one amount-shaped token before the final column usually means a
    # running-balance column, where "the last number" is not the transaction.
    ambiguous = len(ANY_AMOUNT.findall(rest)) > 1
    return amount, direction, description, ambiguous


# ------------------------------------------------------------ summary fields
#
# Deliberately narrow. A loose label match that maps the wrong figure into
# `total_debits` does not produce an obvious error — it produces a confident
# FAIL on a correct extraction, or worse, a PASS on a wrong one. When a label
# is not clearly recognised the field stays None and the statement is reported
# UNVERIFIED, which is the honest answer.
# Order matters: the first pattern that matches a line wins, so the specific
# "total purchases" style labels must be tried before the bare "TOTAL" that
# several issuers use for the new balance.
SUMMARY_LABELS: list[tuple[str, str]] = [
    ("total_debits", r"total\s+(?:purchases|charges|debits|withdrawals)"),
    ("total_credits", r"total\s+(?:payments|credits|deposits|refunds)"),
    ("opening_balance", r"previous\s+balance"),
    ("opening_balance", r"balance\s+from\s+previous\s+statement"),
    ("opening_balance", r"balance\s+(?:brought\s+)?forward"),
    ("opening_balance", r"opening\s+balance"),
    ("closing_balance", r"new\s+balance"),
    ("closing_balance", r"closing\s+balance"),
    ("closing_balance", r"statement\s+balance"),
    ("closing_balance", r"current\s+outstanding"),
    # DBS and UOB print the new balance as a bare TOTAL at the foot of the card
    # section. Anchored to the line start so it cannot pick up SUB-TOTAL, and
    # reached only after the specific totals above have had their turn.
    ("closing_balance", r"^\s*(?:grand\s+)?total\b(?!\s*(?:for\s+)?(?:purchases|charges|debits|credits|payments))"),
]
SUMMARY_RE = [(field, re.compile(pat, re.I)) for field, pat in SUMMARY_LABELS]

PERIOD_RE = re.compile(
    rf"""(?ix)
    (?:statement\s+period|statement\s+cycle|billing\s+cycle|period|statement\s+date|from)\D{{0,12}}
    (?P<a>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{2,4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})
    \s*(?:to|-|–|until)\s*
    (?P<b>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{2,4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})
    """
)

# The same cycle range, for the one layout where PERIOD_RE's anchors have moved
# out of its reach. Trust normally prints "Statement cycle 18 May 2026 - 16 Jun
# 2026" on one line; on one statement pdfplumber's layout mode wrapped the
# trailing year below the label and left the label wedged between the dates:
#     18 May 2026 - 16 Jun
#     Statement cycle
#     2026
# so PERIOD_RE finds neither a leading label nor an end-year. This matches on
# the *start* date's four-digit year alone — a transaction row is a bare "18
# May" and never carries one — and requires a cycle/period label within the
# same neighbourhood. The end year, when absent, is completed by rollover from
# the start: a cycle is contiguous and about a month, so a range that starts
# "18 May 2026" and ends "16 Jun" can only end in 2026.
PERIOD_WRAPPED_RE = re.compile(
    rf"""(?ix)
    (?P<a>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{4}})
    \s*(?:to|-|–|until)\s*
    (?P<b>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*)(?![ /-]?\d)
    """
)
CYCLE_LABEL_RE = re.compile(r"(?i)statement\s+cycle|statement\s+period|billing\s+cycle")
PERIOD_MAX_DAYS = 45      # a billing cycle is about a month; past this it is not one
LAST4_RE = re.compile(r"(?:\*{2,}|x{4,}|X{4,}|•{2,})[\s-]*(\d{4})\b")
CURRENCY_RE = re.compile(r"\b(SGD|USD|EUR|GBP|AUD|JPY|MYR|HKD)\b")
YEAR_RE = re.compile(r"\b(20[1-4]\d)\b")

# Most issuers print a closing date rather than a range: "Statement Date: 16 Jul
# 2026". That single date is a far better year anchor than counting which year
# appears most often on the page, because it says which cycle the rows are in.
# The date must be on the same line, so DBS's bare "STATEMENT DATE" column
# header — whose value sits in a row underneath — correctly does not match.
STATEMENT_DATE_RE = re.compile(
    rf"""(?ix)
    statement \s+ date \s*:?\s+
    (?P<d>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{2,4}}
        |\d{{1,2}}/\d{{1,2}}/\d{{2,4}}
        |\d{{4}}-\d{{2}}-\d{{2}})
    """
)


# A column header that names a *date* rather than a figure. Longest first, so
# `PAYMENT DUE DATE` is never read as the bare `DUE DATE` sitting inside it.
DATE_HEADER = re.compile(
    r"(?i)\b(?:next\s+statement\s+date|payment\s+due\s+date|statement\s+date"
    r"|closing\s+date|due\s+date)\b")

# A date carrying a four-digit year. The year is what separates a grid value
# from a transaction row: rows print `13 JUN`, headers' values print
# `13 Jun 2026`, so this cannot wander into the transaction table.
FULL_DATE = re.compile(
    rf"(?i)\b\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{4}}\b"
    rf"|\b\d{{1,2}}/\d{{1,2}}/\d{{4}}\b"
    rf"|\b\d{{4}}-\d{{2}}-\d{{2}}\b")

# MariBank stacks three header lines plus a units row over its values, so the
# date grid needs a longer reach than the figure grid above.
DATE_GRID_HEADER_LINES = 6


def read_statement_date_grid(lines: list[str]) -> str | None:
    """The statement date when its label is a column header, not a prefix.

    Two issuers print it this way and neither can be read line-by-line:

        DBS         STATEMENT DATE   CREDIT LIMIT  MINIMUM PAYMENT  PAYMENT DUE DATE
                      14 Jul 2026     $21,000.00       $50.00         11 Aug 2026

        MariBank    ACCOUNT  STATEMENT DATE CREDIT LIMIT STATEMENT DUE MINIMUM PAYMENT
                                                              PAYMENT DUE DUE DATE
                    CREDIT CARD 20 Jun 2026  21,000.00    235.20   10.00   10 Jul 2026

    Matching *columns* is what `read_summary_grid` does for figures, but it does
    not survive here: MariBank stacks headers three deep and its value row opens
    with a card name, so the label and the value it belongs to are several
    characters apart and drift further with every column.

    What does hold for both is order. The headers that name a date and the dates
    in the value row come in the same left-to-right sequence, so they can be
    paired positionally — and the count has to match exactly. If it does not,
    nothing on the line says which date is which, so **claim nothing**. That is
    the same rule as the multi-column `TOTAL` guard: taking the wrong column
    here means reading the payment due date as the statement date, which is
    wrong by nearly a month and lands inside the *next* cycle.
    """
    for i, line in enumerate(lines):
        dates = sorted((m.start(), m.group()) for m in FULL_DATE.finditer(line))
        if not dates:
            continue

        headers: list[tuple[int, str]] = []
        for j in range(i - 1, max(i - 1 - DATE_GRID_HEADER_LINES, -1), -1):
            above = lines[j]
            if not above.strip():
                continue
            if FULL_DATE.search(above):
                break          # another value row: this grid's headers end here
            headers += [(m.start(), m.group().lower()) for m in DATE_HEADER.finditer(above)]

        if not headers or len(headers) != len(dates):
            continue
        headers.sort()

        for (_, label), (_, value) in zip(headers, dates):
            if "due" not in label and "next" not in label:
                return _parse_loose_date(value)
    return None


class Context(NamedTuple):
    """What the whole document says about when it is from."""
    period: tuple[str | None, str | None] = (None, None)
    year: int | None = None
    statement_date: str | None = None


def _wrapped_period(joined: str) -> tuple[str | None, str | None]:
    """A statement cycle whose end-year has wrapped out of PERIOD_RE's reach.

    Anchored on the start date's four-digit year and a nearby cycle label; the
    end year, if the wrap took it, is completed by rollover from the start.
    Returns (None, None) unless the result is a sane forward cycle.
    """
    for m in PERIOD_WRAPPED_RE.finditer(joined):
        # A wide-ish window: layout mode pads every line to the page width, so
        # a label two lines from the range is a couple of hundred characters
        # away by the time the text is joined.
        window = joined[max(0, m.start() - 220):m.end() + 220]
        if not CYCLE_LABEL_RE.search(window):
            continue
        start = _parse_loose_date(m.group("a"))
        end_tok = DATE_TOKEN.match(m.group("b"))
        if not start or not end_tok:
            continue
        sd = dt.date.fromisoformat(start)
        day, month, year = _token_to_parts(end_tok)
        if year is None:
            year = sd.year + 1 if month < sd.month else sd.year
        try:
            ed = dt.date(year, month, day)
        except ValueError:
            continue
        if sd < ed <= sd + dt.timedelta(days=PERIOD_MAX_DAYS):
            return start, ed.isoformat()
    return None, None


def document_context(texts: list[str]) -> Context:
    """Resolve the period and year once for the whole statement, not per page.

    Only the first page tends to print the statement period or any four-digit
    year at all; continuation pages carry bare `12 JUN` rows and nothing else.
    Resolving the year per page therefore silently discards every transaction
    after page one — which looks exactly like a statement with few transactions,
    and is the single worst failure mode here because nothing reports it.
    """
    joined = "\n".join(texts)

    period: tuple[str | None, str | None] = (None, None)
    m = PERIOD_RE.search(joined)
    if m:
        period = (_parse_loose_date(m.group("a")), _parse_loose_date(m.group("b")))
    else:
        period = _wrapped_period(joined)

    # Most frequent year, not the largest: statements print next year's payment
    # due date, and a card's expiry, neither of which dates the transactions.
    years = YEAR_RE.findall(joined)
    fallback = None
    if years:
        counts: dict[int, int] = {}
        for y in years:
            counts[int(y)] = counts.get(int(y), 0) + 1
        fallback = max(counts, key=lambda y: (counts[y], y))

    # A labelled line first — it is the issuer saying so directly. Only then the
    # column grid, which is an inference from layout and can be wrong in ways a
    # label cannot.
    m = STATEMENT_DATE_RE.search(joined)
    statement_date = _parse_loose_date(m.group("d")) if m else None
    if statement_date is None:
        statement_date = read_statement_date_grid(joined.splitlines())

    return Context(period, fallback, statement_date)


def _iso(parts: tuple[int, int, int | None]) -> str | None:
    day, month, year = parts
    if year is None:
        return None
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_loose_date(raw: str) -> str | None:
    m = DATE_TOKEN.match(raw)
    return _iso(_token_to_parts(m)) if m else None


def resolve_year(day: int, month: int, period: tuple[str | None, str | None],
                 fallback_year: int | None, statement_date: str | None = None) -> int | None:
    """Pick the year for a row that printed only day and month.

    A statement spanning a year boundary is normal — a December row on a
    statement ending in January belongs to the earlier year — so choose the
    candidate that actually lands inside the period rather than assuming the
    end year.

    "Whichever year appears most often on the page" is the last resort, and it
    is wrong exactly in January, which is when nobody is looking. A printed
    statement date avoids it for most issuers.
    """
    start, end = period
    if start and end:
        lo, hi = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        for year in {lo.year, hi.year}:
            try:
                if lo - dt.timedelta(days=7) <= dt.date(year, month, day) <= hi + dt.timedelta(days=14):
                    return year
            except ValueError:
                continue
        return hi.year

    if statement_date:
        # Rows fall on or before the closing date, so a month *later* than the
        # statement's own month must belong to the previous year.
        closing = dt.date.fromisoformat(statement_date)
        return closing.year if month <= closing.month else closing.year - 1

    return fallback_year


# --------------------------------------------------------- summary grids
#
# Several issuers print the statement summary as a horizontal grid: a row of
# figures with the labels stacked in the lines above, aligned by column. Nothing
# on the figures row says what any of it means, so the line-oriented matcher
# above sees only numbers and the statement reports UNVERIFIED — even though the
# opening balance it needs is right there.
#
# Only the opening and closing balance are read back out. The component columns
# are tempting but wrong to use: MariBank splits what the transaction table
# calls a credit across two of them (repayment 216.18 and cashback 0.14), so
# lifting one column into `total_credits` would produce a confident FAIL, 14
# cents off, on a completely correct extraction. Balances have no such problem,
# and the roll-forward check they enable is the stronger test anyway.

GRID_LABELS: list[tuple[str, str]] = [
    ("opening_balance", r"previous\s+(?:balance|outstanding)"),
    ("opening_balance", r"balance\s+(?:brought\s+)?forward"),
    ("opening_balance", r"opening\s+balance"),
    ("closing_balance", r"current\s+outstanding"),
    ("closing_balance", r"total\s+outstanding"),
    ("closing_balance", r"new\s+balance"),
    ("closing_balance", r"closing\s+balance"),
]
GRID_RE = [(field, re.compile(pat, re.I)) for field, pat in GRID_LABELS]

FIGURE = re.compile(r"(?:S\$|SGD|USD|\$)?\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\s*(?:CR|DR)?", re.I)
FIGURES_ONLY = re.compile(
    r"^[\s+\-=()]*(?:(?:S\$|SGD|USD|\$)?\s?\d[\d,]*\.\d{2}\s*(?:CR|DR)?[\s+\-=()]*)+$", re.I)
GRID_MIN_COLUMNS = 3      # two numbers side by side is a coincidence, not a grid
GRID_HEADER_LINES = 4     # how far above the figures the labels may sit
SEPARATOR = re.compile(r"^[+\-=(),.:*]+$")


def _figure_columns(line: str) -> list[tuple[int, Decimal]]:
    out = []
    for m in FIGURE.finditer(line):
        try:
            out.append((m.start(), Decimal(m.group(1).replace(",", ""))))
        except InvalidOperation:
            continue
    return out


def _header_groups(line: str, n: int) -> list[str] | None:
    """Split a header line into exactly n groups, or give up.

    Issuers separate summary headers three different ways and the right one is
    whichever produces one group per figure: arithmetic operators (Trust writes
    `Previous balance + Purchases + ...`), wide gaps, or plain single spaces.
    """
    text = line.strip()
    if not text:
        return None
    for parts in (re.split(r"\s+[+\-=]\s+", text), re.split(r"\s{2,}", text), text.split()):
        if len(parts) == n:
            return [p.strip() for p in parts]
    return None


def _header_by_column(line: str, starts: list[int]) -> list[str]:
    """Assign each word to the figure column it sits over.

    A header word belongs to the last column starting at or before it. No
    tolerance: MariBank's `REFUND` begins one character left of the *next*
    column, and rounding it the wrong way relabels two fields at once.
    """
    groups = [""] * len(starts)
    for m in re.finditer(r"\S+", line):
        if SEPARATOR.match(m.group()):
            continue
        idx = 0
        for k, start in enumerate(starts):
            if m.start() >= start:
                idx = k
        groups[idx] = (groups[idx] + " " + m.group()).strip()
    return groups


def read_summary_grid(lines: list[str], out: dict) -> None:
    """Fill any balance still missing from a column-aligned summary grid.

    Runs after the line-by-line pass and never overwrites it, so a grid can only
    add what a directly labelled line did not already say.
    """
    if out["opening_balance"] is not None and out["closing_balance"] is not None:
        return

    for i, line in enumerate(lines):
        if not line.strip() or not FIGURES_ONLY.match(line.strip()):
            continue
        columns = _figure_columns(line)
        if len(columns) < GRID_MIN_COLUMNS:
            continue

        starts = [c for c, _ in columns]
        labels = [""] * len(columns)
        for j in range(i - 1, max(i - 1 - GRID_HEADER_LINES, -1), -1):
            above = lines[j]
            if not above.strip():
                continue
            if FIGURES_ONLY.match(above.strip()):
                break      # a second figures row: this grid's headers end here
            groups = _header_groups(above, len(columns)) or _header_by_column(above, starts)
            labels = [f"{g} {lab}".strip() for g, lab in zip(groups, labels)]

        for label, (_, amount) in zip(labels, columns):
            for field, pat in GRID_RE:
                if out[field] is None and pat.search(label):
                    out[field] = str(amount)
                    break


def _summary_field(text: str, anchored: bool = False) -> str | None:
    """Which summary field this line is labelled as, if any.

    `anchored` requires the label at the very start, and is used when the line
    is already a dated row: a merchant can easily contain the word "balance" or
    "total" mid-name, and misreading a real purchase as a summary figure loses a
    transaction silently. A row that *opens* with the label is a summary row.
    """
    for field, pat in SUMMARY_RE:
        m = pat.search(text)
        if m and (not anchored or m.start() == 0):
            return field
    return None


def _description_below(lines: list[str], i: int, lookahead: int = 2) -> str:
    """Merchant text on the line(s) under a bare date/date/amount row."""
    for j in range(i + 1, min(i + 1 + lookahead, len(lines))):
        candidate = lines[j].strip()
        if not candidate:
            continue
        if DATE_TOKEN.match(lines[j]) or AMOUNT_TAIL.search(lines[j]):
            break
        if re.search(r"[A-Za-z]{2,}", candidate):
            return re.sub(r"\s{2,}", " ", candidate)
    return ""


# The closed vocabulary of a transaction-table column header, seen across
# every issuer's own wording ("Post Trans Date", "Transaction Posting Date",
# "POSTED DATE TRANSACTION DATE", "DATE DESCRIPTION AMOUNT (S$)"). A line built
# entirely from these words is a header wherever it lands, never a merchant —
# no real business is named only "Description" or "Date Date SGD" — which
# matters because a header sits two lines above the first row of every page,
# exactly where `_description_above` looks for MariBank's merchant line.
_HEADER_WORDS = frozenset({
    "date", "dates", "description", "amount", "amounts", "transaction",
    "transactions", "posted", "posting", "post", "trans", "of", "sgd", "usd",
    "s", "currency", "particulars", "details",
})


def _is_header_line(text: str) -> bool:
    words = re.findall(r"[A-Za-z$]+", text)
    return bool(words) and all(w.strip("$").lower() in _HEADER_WORDS for w in words)


def _description_above(lines: list[str], i: int, lookback: int = 1) -> str:
    """Merchant text on the line(s) over a row.

    Bounded like `_description_below`: it stops at a dated line or one
    carrying an amount, so a merchant name can only ever be claimed from the
    gap above its own row and never lifted off the previous transaction.

    A reference line, an FX-rate subline (either wording), or a column header
    is noise here, not a merchant — UOB and DBS print a reference directly
    above the *next* row's date, a foreign-currency row's rate note sits
    directly above whatever transaction follows it, and a header sits directly
    above the first row of a page (sometimes wrapped across two, as Trust's
    "Transaction / date" split) — so all are skipped like a numeric line. The
    default of 1 is deliberate, not just conservative: every merchant-above
    shape actually seen (MariBank, and the foreign-currency row below) has the
    name on the line immediately above, and reaching further is what let a
    two-line header two rows up get claimed as a merchant.
    """
    examined = 0
    for j in range(i - 1, -1, -1):
        if examined >= lookback:
            break
        candidate = lines[j].strip()
        if not candidate:
            continue
        if DATE_TOKEN.match(lines[j]) or AMOUNT_TAIL.search(lines[j]):
            break
        if (REF_LINE.match(lines[j]) or FX_RATE_LINE.search(lines[j])
                or FOREIGN_NOTE_LINE.match(lines[j]) or _is_header_line(candidate)):
            examined += 1
            continue
        if re.search(r"[A-Za-z]{2,}", candidate):
            return re.sub(r"\s{2,}", " ", candidate)
        examined += 1
    return ""


def _foreign_amount(description: str) -> tuple[Decimal, str] | None:
    """A leftover description that is a figure and a currency, not a merchant.

    Requires the currency: a bare number left over is a column the parser has
    not understood, and guessing a currency for it would invent data. `102.67
    HKD` and `HKD 102.67` both occur in the wild; `SGD` is excluded because a
    row billed in the statement's own currency has no conversion to record.
    """
    m = FOREIGN_DESC.match(description)
    if not m:
        return None
    currency = (m.group("cur_pre") or m.group("cur_post") or "").upper()
    if not currency or currency == "SGD":
        return None
    return Decimal(m.group("num").replace(",", "")), currency


def _fx_rate_below(lines: list[str], i: int, currency: str, lookahead: int = 2) -> str | None:
    """The conversion rate the statement printed under the row.

    Only accepted when it names the same currency the row was charged in, so a
    rate table further down the page cannot be read as this row's rate.
    """
    examined = 0
    for j in range(i + 1, len(lines)):
        if examined >= lookahead:
            break
        if not lines[j].strip():
            continue
        if DATE_TOKEN.match(lines[j]):
            break
        m = FX_RATE_LINE.search(lines[j])
        if m and m.group("from").upper() == currency:
            return m.group("rate")
        examined += 1
    return None


def _reference_inline(text: str) -> str | None:
    """A reference the issuer wrote into the description itself."""
    m = REF_INLINE.search(text)
    return m.group(1) if m else None


def _reference_below(lines: list[str], i: int, lookahead: int = 2) -> str | None:
    """A reference on its own continuation line beneath the row.

    Bounded, and stops at the next dated row, so a reference can only ever be
    claimed by the transaction it actually sits under. Blank lines are skipped
    rather than counted: UOB leaves one between some rows and their reference.
    """
    examined = 0
    for j in range(i + 1, len(lines)):
        if examined >= lookahead:
            break
        if not lines[j].strip():
            continue
        if DATE_TOKEN.match(lines[j]):
            break
        m = REF_LINE.match(lines[j])
        if m:
            return m.group(1)
        examined += 1
    return None


def parse_page(text: str, period: tuple[str | None, str | None] = (None, None),
               fallback_year: int | None = None, statement_date: str | None = None) -> dict:
    """Parse one page of layout-preserved statement text into the record shape."""
    out: dict = {
        "issuer": None, "account_last4": None,
        "statement_period_start": period[0], "statement_period_end": period[1],
        "currency": None, "opening_balance": None, "closing_balance": None,
        "total_debits": None, "total_credits": None,
        "transactions": [], "_ambiguous_rows": 0,
    }

    lines = text.splitlines()

    if not out["statement_period_start"]:
        m = PERIOD_RE.search(text)
        if m:
            out["statement_period_start"] = _parse_loose_date(m.group("a"))
            out["statement_period_end"] = _parse_loose_date(m.group("b"))
    period = (out["statement_period_start"], out["statement_period_end"])

    if fallback_year is None:
        years = YEAR_RE.findall(text)
        if years:
            fallback_year = max(int(y) for y in years)

    m = LAST4_RE.search(text)
    if m:
        out["account_last4"] = m.group(1)
    m = CURRENCY_RE.search(text)
    if m:
        out["currency"] = m.group(1)

    for i, line in enumerate(lines):
        dates, rest = leading_dates(line)
        if dates:
            if not rest.strip():
                continue
            parsed = parse_amount(rest)
            if not parsed:
                continue
            amount, direction, description, ambiguous = parsed
            if len(dates) >= 2 and not (description and _foreign_amount(description)):
                # MariBank (and Trust's real layout, as opposed to the synthetic
                # fixture) print the merchant on its own line above the row and
                # a payment-channel tag ("Card Payment", "Instant Checkout")
                # below it — never the reverse. A merchant name that wraps
                # leaves its second word on the dated line itself, ahead of the
                # amount: "Nintendo Official Store" above, "Singapore -620.69"
                # on the row. `_description_above` already excludes reference
                # lines, so it costs nothing to try on issuers that put the
                # whole description inline (UOB, DBS) — there it finds nothing
                # and this is a no-op. A row whose leftover is itself a bare
                # foreign figure is excluded so the dedicated handling below,
                # which claims the same "above" line for a different field,
                # still gets an untouched figure to work with.
                above = _description_above(lines, i)
                if above:
                    description = f"{above} {description}".strip() if description else above
                elif not description:
                    # Some issuers put the merchant on its own line under the
                    # row instead, leaving date/date/amount alone on the first.
                    description = _description_below(lines, i)
            elif not description:
                # A single bare date with a number is not a strong enough row
                # signal to go looking for a description — usually a due date
                # or a rate table.
                continue

            # Some issuers rule the opening and closing balance into the
            # transaction table itself, dated like any other row — Trust prints
            # "Previous balance" and "Total outstanding balance" that way. Taken
            # as purchases they inflate the debit total by roughly twice the
            # statement balance, and because the same statement then reports
            # UNVERIFIED, nothing contradicts it. Claim the figure for the
            # summary field it belongs to and drop the row.
            field = _summary_field(description, anchored=True)
            if field:
                # Still dropped from the transactions either way — it is a
                # summary row, not a purchase. Only the figure is withheld,
                # and for the same reason as the undated case below.
                if out[field] is None and not ambiguous:
                    out[field] = str(amount)
                continue

            day, month, year = dates[0]
            year = year or resolve_year(day, month, period, fallback_year, statement_date)
            if year is None:
                continue
            try:
                iso = dt.date(year, month, day).isoformat()
            except ValueError:
                continue
            posted = None
            if len(dates) > 1:
                pd, pm, py = dates[1]
                py = py or resolve_year(pd, pm, period, fallback_year, statement_date)
                posted = _iso((pd, pm, py))
            # A charge in another currency leaves its foreign figure where the
            # merchant should be, with the real name on the line above and the
            # rate on the line below. Claim all three or none: a description
            # replaced by a name that was never found would be worse than the
            # figure it replaced, because at least the figure is honest about
            # not being a merchant.
            foreign_amount, foreign_currency, fx_rate = None, None, None
            foreign = _foreign_amount(description)
            if foreign:
                above = _description_above(lines, i)
                if above:
                    foreign_amount, foreign_currency = str(foreign[0]), foreign[1]
                    fx_rate = _fx_rate_below(lines, i, foreign_currency)
                    description = above

            # Inline first: if the issuer put the reference in the description
            # it belongs to this row for certain, with no lookahead to get wrong.
            reference = _reference_inline(description) or _reference_below(lines, i)
            out["transactions"].append({
                "date": iso, "posted_date": posted, "description": description,
                "amount": str(amount), "direction": direction,
                "reference": reference,
                "foreign": {"amount": foreign_amount, "currency": foreign_currency},
                "fx_rate": fx_rate,
                # The row carried more than one amount-shaped number, so the one
                # taken as the transaction figure may be a running balance. Kept
                # on the row, not just tallied, so the warning can point at it.
                "amount_ambiguous": ambiguous,
            })
            out["_ambiguous_rows"] += int(ambiguous)
            continue

        # No leading date: candidate summary row.
        field = _summary_field(line)
        if field and out[field] is None:
            parsed = parse_amount(line)
            # A summary label followed by more than one figure is a row of a
            # multi-column table, not a labelled value, and "the last number"
            # is then the wrong column. Standard Chartered prints
            # `TOTAL  58.40  50.00` under `New Balance | Min. Payment Due`;
            # taking 50.00 as the closing balance turns a perfect extraction
            # into a FAIL 8.40 short. DBS and UOB print the same shape and are
            # only saved by an earlier page having already filled the field.
            # There is nothing on the line itself that says which column is
            # which, so claim nothing: an explicitly labelled line elsewhere,
            # or read_summary_grid() below, supplies the figure from a source
            # that does say.
            if parsed and not parsed[3]:
                out[field] = str(parsed[0])

    read_summary_grid(lines, out)
    return out
