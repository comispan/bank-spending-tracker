# bank-spending-tracker

Upload card statement PDFs → get a categorized monthly spending report across all your cards.

**Status:** running. Phases 0–3 are done — upload, verify, categorize, read the month.
See [app/README.md](app/README.md) to run it, and **[DESIGN.md](DESIGN.md)** for the
architecture, data model, and build plan.

## The short version

1. Upload a statement PDF.
2. It's parsed into transactions, and the parse is **verified against the statement's own totals** — if the numbers don't reconcile, the statement is flagged for review instead of silently importing wrong data.
3. Transactions are categorized (user rules → learned merchant memory → an opt-in model for the rest).
4. Statements from every card are merged and bucketed by **calendar month**, and rendered as one report that says how far the data actually reaches.

## Where it got to

Phase 0 asked whether extraction reconciles. It does: across six real statements from six
different issuers, every one reconciles against the statement's own printed figures.
It also answered a question we didn't ask — extraction needs no model at all. The PDF text
layer already carries the table, so it's parsed in code, offline, in milliseconds.
See [spike/README.md](spike/README.md).

Phases 1–3 built on that: the statement list and the review screen (DESIGN.md Section 8),
merchant normalization and the three categorization tiers (Section 3), and the month
report with drill-through, the month-on-month delta and the coverage window (Section 4).

## Next step

Phase 4, hardening — OCR for scanned statements, CSV/Excel export, and generalizing the
two `page_text` backfills into replay-the-parser-over-every-statement tooling.
See DESIGN.md Section 8.

Two things are waiting on something other than code:

- **Tier 3's grounded path ships off because it was never graded** — the API key ran out
  of credits mid-run. `python spike\eval_categories.py --grounding` finishes the grade
  once a working key exists.
- **The corpus has a hole.** MariBank's 21 Jun – 20 Jul statement was never uploaded, so
  no month qualifies for the three-month average and the month-on-month deltas decline to
  render. The report is behaving correctly; it has nothing complete to describe yet.
