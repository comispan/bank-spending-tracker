"""Calendar months, and how much of one the statements actually cover.

DESIGN.md Section 4 trap 1: statement cycles are not calendar months, and users
think in calendar months. So transactions are bucketed by **transaction date**,
never by which statement they arrived in — that part is a one-line SQL
group-by.

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
    # By the date alone. Sorting the pairs lets Python fall through to
    # comparing the dicts when two statements on one card share an end date —
    # a re-issued cycle uploaded alongside the original, which `file_sha256`
    # does not catch because the file genuinely differs — and dicts do not
    # order, so the months page raised instead of rendering.
    dated.sort(key=lambda pair: pair[0])

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


def covers_days(status: dict, days: tuple[int, int]) -> bool:
    """Is this month billed across `days`, allowing for its own length?

    The clamp is the whole point. Comparing raw day numbers across months
    silently disqualifies every shorter month: a complete June is billed
    1–30, and asking whether it covers "days 1–31" of a 31-day month is a
    question it can never answer yes to however complete it is. That made a
    perfectly fair June-to-July comparison refuse itself, with a reason —
    "only billed for days 1–30" — that reads like a missing statement and is
    really just the calendar.
    """
    covered = covered_day_range(status)
    if covered is None:
        return False
    last = int(status["end"][8:10])
    return covered[0] <= days[0] and min(days[1], last) <= covered[1]


def trailing_window(status: dict, earlier: list[dict]) -> tuple[list[dict], list[str], str | None]:
    """Which earlier months may contribute to a trailing average, and why not.

    Section 4 asks for "vs 3-month average". The averaging is the easy half;
    deciding what is allowed into it is the half that decides whether the
    number means anything. An average launders part-billed months better than a
    single comparison does — three short months produce one low figure with
    nothing on its face to say it is short, and every month measured against it
    then reads as an overspend.

    So a month contributes only if it is billed across at least the days being
    reported, and the caller measures it over exactly those days. Two
    contributors minimum: an "average" of one month is the previous month,
    which the delta already shows and names honestly.

    Returns `(usable, short, note)` — the months that qualify, the names of
    those that do not, and a reason when there are too few. The reason is
    printed, so it doubles as an instruction for which statement to go find.
    """
    days = comparable_days(status) or ((1, int(status["end"][8:10])) if status["is_complete"] else None)
    if days is None:
        return [], [], "this month cannot be bounded"

    usable, short = [], []
    for month in earlier[-3:]:
        if covers_days(month, days):
            usable.append(month)
        else:
            short.append(month["month"])
    if len(usable) < 2:
        return [], short, (
            f"fewer than two earlier months are billed across days {days[0]}–{days[1]}"
            + (f" — {', '.join(short)} fall short" if short else ""))
    return usable, short, None


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month_first(day: dt.date, shift: int = 0) -> dt.date:
    """The first of `day`'s month, moved `shift` whole months."""
    index = day.year * 12 + day.month - 1 + shift
    return dt.date(index // 12, index % 12 + 1, 1)


def coverage_timeline(coverage: dict[str, list[tuple[str, str]]],
                      today: str | None = None, pad_months: int = 1) -> dict | None:
    """Every card's coverage placed on one shared axis, for the /months chart.

    A list of dates per card answers "when does this card start and stop"; it
    does not answer "do the cards line up", which is the question the month
    totals actually turn on — a month is only as complete as its least-covered
    card, so the ragged right-hand edge *is* the reason the newest month is
    part-billed. On one axis that is a shape you take in at a glance, and a
    missing cycle stops being a comma in a run of dates.

    Positions come back as fractions of the axis — 0 at the left edge, 1 at the
    right — so the caller scales them to whatever it is drawing. The axis is
    padded by `pad_months` whole months at each end, snapped to month
    boundaries, and stretched further where today would otherwise fall off it:
    the bars keep clear of the edges, the gridlines land on real month starts,
    and the empty strip past the last bar is the room the statement that has
    not arrived yet will fill.

    Returns None when nothing can be placed, which is not the same as having no
    statements: a card whose statements are all unbounded (Section 4 — an end
    date is what makes a window computable) has no windows to draw.
    """
    spans = [w for windows in coverage.values() for w in windows]
    if not spans:
        return None

    now = dt.date.fromisoformat(today) if today else dt.date.today()
    axis_start = _month_first(dt.date.fromisoformat(min(s for s, _ in spans)), -pad_months)
    axis_end = _month_first(dt.date.fromisoformat(max(e for _, e in spans)), pad_months + 1) \
        - dt.timedelta(days=1)
    # A card nobody has uploaded for half a year would otherwise fall off the
    # right-hand end and read as current, so today always stays on the axis:
    # the empty run between the last bar and the marker is the measure of how
    # stale the data is, and it is exactly what the padding is for.
    axis_end = max(axis_end, _month_first(now, 1) - dt.timedelta(days=1))
    total = (axis_end - axis_start).days + 1

    def place(start: str, end: str) -> dict:
        """One bar: where it begins, and how wide, both ends counted."""
        first = dt.date.fromisoformat(start)
        days = (dt.date.fromisoformat(end) - first).days + 1
        return {"from": start, "to": end, "days": days,
                "at": (first - axis_start).days / total, "len": days / total}

    ticks = []
    tick = axis_start
    while tick <= axis_end:
        ticks.append({"date": tick.isoformat(),
                      "at": (tick - axis_start).days / total,
                      "year_start": tick.month == 1 or tick == axis_start,
                      "label": _MONTH_NAMES[tick.month - 1]
                               + (f" {tick.year}" if tick.month == 1 or tick == axis_start else "")})
        tick = _month_first(tick, 1)

    rows = []
    for label, windows in coverage.items():
        segments, previous_end = [], None
        for start, end in windows:
            # `windows` is merged, so anything between two of them is a real
            # hole — a statement nobody uploaded — and it is drawn, not skipped.
            if previous_end:
                hole = place((dt.date.fromisoformat(previous_end) + dt.timedelta(days=1)).isoformat(),
                             (dt.date.fromisoformat(start) - dt.timedelta(days=1)).isoformat())
                segments.append(dict(hole, kind="gap"))
            segments.append(dict(place(start, end), kind="covered"))
            previous_end = end
        rows.append({
            "label": label, "segments": segments,
            "from": windows[0][0] if windows else None,
            "to": windows[-1][1] if windows else None,
            "days": sum(s["days"] for s in segments if s["kind"] == "covered"),
            "gaps": sum(1 for s in segments if s["kind"] == "gap"),
        })

    return {
        "start": axis_start.isoformat(), "end": axis_end.isoformat(), "days": total,
        "ticks": ticks, "rows": rows,
        "today": (now - axis_start).days / total if axis_start <= now <= axis_end else None,
        # The last day every card is billed to: where the ragged edge becomes
        # a straight one, and the last day a month total can be trusted.
        "all_covered_to": min((r["to"] for r in rows if r["to"]), default=None),
    }
