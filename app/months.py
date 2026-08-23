"""Calendar months, and how much of one the statements actually cover.

DESIGN.md §4 trap 1: statement cycles are not calendar months, and users think
in calendar months. So transactions are bucketed by **transaction date**, never
by which statement they arrived in — that part is a one-line SQL group-by.

The hard part is the other half of the same trap, and it is what this module is
for. Every card in the corpus closes on a different day — DBS around the 13th,
Standard Chartered and UOB the 15th–16th, Trust the 17th, MariBank the 20th — so
the newest calendar month is always partly unbilled. On the real data, August
shows 1,319.55 against July's 4,756.64 and reads as a 72% collapse in spending.
It is not: the month is two-thirds unbilled. A report that prints that number
without saying so is not slightly wrong, it is confidently wrong in the
direction that would make someone change their behaviour.

Redefining the "month" to follow the cycle does not fix it, which is worth
stating because it is the obvious first idea. The five cards close on four
different days; any single boundary still slices four of them mid-cycle. It
trades a problem everyone understands for the same problem plus an unfamiliar
calendar.

What does work is to keep calendar months and publish how far the data reaches:

    complete through  the earliest last-covered date across all cards, because
                      a month is only as complete as its least-covered card
    like for like     when a month is partial, compare it to the same slice of
                      the earlier month, not to the whole of it

Everything here is pure functions over ISO date strings so the rules can be
tested without a database, the same way `rows.py` and `categorize.py` are.
"""

from __future__ import annotations

import datetime as dt


def month_bounds(ym: str) -> tuple[str, str]:
    """'2026-08' -> ('2026-08-01', '2026-08-31')."""
    year, month = int(ym[:4]), int(ym[5:7])
    first = dt.date(year, month, 1)
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return first.isoformat(), last.isoformat()


def statement_windows(statements: list[dict]) -> list[tuple[str, str]]:
    """What each statement covers, filling in starts the issuer did not print.

    `statements` are dicts with `period_start`, `period_end`, `statement_date`
    and `first_txn`, in any order. Each is reduced to one (start, end) window:

    - **end** is the printed period end, else the statement date.
    - **start** is the printed period start; failing that, the day after the
      previous statement's end, because consecutive statements from one card
      tile the timeline with no gap — that is what a billing cycle *is*.
      Failing even that (the first statement of a card), the earliest
      transaction on it, which is a floor rather than the true cycle start.

    DBS prints no period at all, only a statement date, and this is what makes
    its coverage computable anyway. A statement with neither is dropped: a
    window with an unknown end cannot bound anything, and guessing one would
    silently claim coverage that may not exist.
    """
    dated = []
    for s in statements:
        end = s.get("period_end") or s.get("statement_date")
        if end:
            dated.append((end, s))
    dated.sort()

    windows: list[tuple[str, str]] = []
    previous_end: str | None = None
    for end, s in dated:
        start = s.get("period_start")
        if not start and previous_end:
            start = (dt.date.fromisoformat(previous_end) + dt.timedelta(days=1)).isoformat()
        if not start:
            start = s.get("first_txn")
        if start and start <= end:
            windows.append((start, end))
        previous_end = end
    return merge(windows)


def merge(windows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge overlapping or touching windows, so gaps that remain are real gaps.

    Touching counts as contiguous: a cycle ending the 15th and the next starting
    the 16th cover the boundary between them, and treating that as a hole would
    report every card as permanently incomplete.
    """
    out: list[tuple[str, str]] = []
    for start, end in sorted(windows):
        if out and start <= (dt.date.fromisoformat(out[-1][1]) + dt.timedelta(days=1)).isoformat():
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def card_coverage(windows: list[tuple[str, str]], ym: str) -> dict:
    """What one card covers *inside* one calendar month.

    Both ends matter, which the first version of this got wrong. A month is
    incomplete at the start as readily as at the end: the corpus opens mid-June
    because that is when the earliest statements begin, so June is missing its
    first twelve days on four of five cards — invisible if you only ask how far
    coverage reaches.

    `gap` marks a card whose coverage inside the month is in two pieces with a
    hole between them. That is a missing statement, not a quiet edge case: one
    card in the corpus is missing a whole cycle, and the month it belongs to
    must not be presented as merely partial.
    """
    start, end = month_bounds(ym)
    inside = merge([(max(s, start), min(e, end)) for s, e in windows if s <= end and e >= start])
    if not inside:
        return {"state": "missing", "from": None, "through": None, "gap": False}
    covered_from, covered_through = inside[0][0], inside[0][1]
    if len(inside) == 1 and covered_from == start and covered_through == end:
        return {"state": "complete", "from": start, "through": end, "gap": False}
    return {"state": "partial", "from": covered_from, "through": covered_through,
            "gap": len(inside) > 1}


def month_completeness(ym: str, coverage: dict[str, list[tuple[str, str]]]) -> dict:
    """How complete one calendar month is, across every card.

    The month is complete only when every card covers all of it. One unbilled
    card is enough to make the total wrong, and wrong in the invisible
    direction — the money is simply absent, so nothing looks amiss.

    The trustworthy window is the **intersection** across cards: it begins when
    the last card's coverage begins and ends when the first card's ends. Outside
    that window a total is the sum of however many cards happened to be covered,
    which is not a number about anyone's spending.
    """
    start, end = month_bounds(ym)
    per_card = {label: card_coverage(w, ym) for label, w in coverage.items()}

    complete = sorted(l for l, c in per_card.items() if c["state"] == "complete")
    partial = sorted(l for l, c in per_card.items() if c["state"] == "partial")
    missing = sorted(l for l, c in per_card.items() if c["state"] == "missing")
    gaps = sorted(l for l, c in per_card.items() if c["gap"])

    if missing or gaps or not per_card:
        window = (None, None)
    else:
        window = (max(c["from"] for c in per_card.values()),
                  min(c["through"] for c in per_card.values()))

    return {
        "month": ym,
        "start": start,
        "end": end,
        "cards_total": len(coverage),
        "cards_complete": len(complete),
        "partial": partial,
        "missing": missing,
        "gaps": gaps,
        "covered_from": window[0],
        "covered_through": window[1],
        "is_complete": len(complete) == len(coverage) and bool(coverage),
        "per_card": per_card,
    }


def covered_day_range(status: dict) -> tuple[int, int] | None:
    """The days of this month that every card is billed for, as day numbers.

    None when no such range exists. Used to check the *other* side of a
    month-on-month comparison: a part-billed month is easy to remember about,
    and the month being compared against is easy to forget.
    """
    if status["is_complete"]:
        return 1, int(status["end"][8:10])
    if not status["covered_from"]:
        return None
    return int(status["covered_from"][8:10]), int(status["covered_through"][8:10])


def comparable_days(status: dict) -> tuple[int, int] | None:
    """The day range this month can be fairly compared on, or None.

    None means "do not compare": either the whole month is covered and the
    comparison is unrestricted, or a card is missing entirely and no window is
    trustworthy. Those are opposite situations, so callers must read
    `is_complete` alongside this rather than treating None as one thing.
    """
    if status["is_complete"] or not status["covered_from"]:
        return None
    return int(status["covered_from"][8:10]), int(status["covered_through"][8:10])
