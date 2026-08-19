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
    (?:statement\s+period|period|statement\s+date|from)\D{{0,12}}
    (?P<a>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{2,4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})
    \s*(?:to|-|–|until)\s*
    (?P<b>\d{{1,2}}[ /-](?:{MONTH_RE})[a-z]*[ /-]\d{{2,4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})
    """
)
LAST4_RE = re.compile(r"(?:\*{2,}|x{4,}|X{4,}|•{2,})[\s-]*(\d{4})\b")
CURRENCY_RE = re.compile(r"\b(SGD|USD|EUR|GBP|AUD|JPY|MYR|HKD)\b")
YEAR_RE = re.compile(r"\b(20[1-4]\d)\b")


def document_context(texts: list[str]) -> tuple[tuple[str | None, str | None], int | None]:
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

    # Most frequent year, not the largest: statements print next year's payment
    # due date, and a card's expiry, neither of which dates the transactions.
    years = YEAR_RE.findall(joined)
    fallback = None
    if years:
        counts: dict[int, int] = {}
        for y in years:
            counts[int(y)] = counts.get(int(y), 0) + 1
        fallback = max(counts, key=lambda y: (counts[y], y))

    return period, fallback


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
                 fallback_year: int | None) -> int | None:
    """Pick the year for a row that printed only day and month.

    A statement spanning a year boundary is normal — a December row on a
    statement ending in January belongs to the earlier year — so choose the
    candidate that actually lands inside the period rather than assuming the
    end year.
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


def parse_page(text: str, period: tuple[str | None, str | None] = (None, None),
               fallback_year: int | None = None) -> dict:
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
            if not description:
                # Some issuers put the merchant on its own line under the row,
                # leaving date/date/amount alone on the first. Two dates is a
                # strong enough row signal to keep it and go looking; a single
                # bare date with a number is not, and is usually a due date or
                # a rate table.
                if len(dates) < 2:
                    continue
                description = _description_below(lines, i)

            # Some issuers rule the opening and closing balance into the
            # transaction table itself, dated like any other row — Trust prints
            # "Previous balance" and "Total outstanding balance" that way. Taken
            # as purchases they inflate the debit total by roughly twice the
            # statement balance, and because the same statement then reports
            # UNVERIFIED, nothing contradicts it. Claim the figure for the
            # summary field it belongs to and drop the row.
            field = _summary_field(description, anchored=True)
            if field:
                if out[field] is None:
                    out[field] = str(amount)
                continue

            day, month, year = dates[0]
            year = year or resolve_year(day, month, period, fallback_year)
            if year is None:
                continue
            try:
                iso = dt.date(year, month, day).isoformat()
            except ValueError:
                continue
            posted = None
            if len(dates) > 1:
                pd, pm, py = dates[1]
                py = py or resolve_year(pd, pm, period, fallback_year)
                posted = _iso((pd, pm, py))
            out["transactions"].append({
                "date": iso, "posted_date": posted, "description": description,
                "amount": str(amount), "direction": direction,
            })
            out["_ambiguous_rows"] += int(ambiguous)
            continue

        # No leading date: candidate summary row.
        field = _summary_field(line)
        if field and out[field] is None:
            parsed = parse_amount(line)
            if parsed:
                out[field] = str(parsed[0])

    read_summary_grid(lines, out)
    return out
