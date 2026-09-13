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

Since then, mostly volume and the things volume broke: bulk upload for a whole folder,
a paginated transactions list, an analytics page across whichever complete months you pick,
merchant groups so a company's outlets read as one line, the card-coverage timeline, and
deploy scripts for a single-user tunnel-only instance ([deploy/README.md](deploy/README.md)).

**The corpus is now 40 statements across 5 cards — 1,556 rows over 8 months.** All 40 pass
the reconciliation gate, and nothing is left uncategorized.

## What's left

Phase 4, hardening. Three items from DESIGN.md Section 8 are genuinely not built:

- **OCR for scanned statements.** A scan is currently detected and refused — `skipped (a
  scan needs OCR before this can read it)` — which is honest, and is not a parse.
- **CSV/Excel export.** Nothing in the app writes either.
- **Replay the parser over stored statements.** `db.py` now has *four* `backfill_*`
  functions, each re-parsing `page_text` on boot to fill one field. Section 8 said the
  third would be the moment to generalize them into "re-run the parser over every stored
  statement and diff"; we are past it. This is what makes a parser fix reach old data
  without re-uploading.

Ongoing rather than finishable: **summary labels for each new issuer** — row parsing
generalizes, summary figures don't (Section 10). And `category_confidence` from Section 5
stays unfilled on purpose: tier 3 expresses doubt by abstaining rather than by scoring
itself, which needs no column.

One thing is still waiting on something other than code:

- **Tier 3's grounded path ships off because it has never been graded** — the API key ran
  out of credits mid-run and no grounded eval has been produced since.
  `python spike\eval_categories.py --grounding` finishes the grade once a working key
  exists. The ungrounded path *is* graded and shipping: `gemini-3.7-flash` scores 33 correct,
  4 wrong, 6 abstained of 43 hand-labelled merchants (77% / 9%), gate PASS.

Two items from the previous version of this list have since resolved themselves:

- **The corpus hole is filled.** MariBank's 21 Jun – 20 Jul statement was uploaded. Seven
  of the eight months are billed across every card, so the three-month average now renders
  from 2026-04 onward — and still declines, with a reason, for the months too early to have
  three qualifying predecessors.
- **Transaction-level dedup stays deferred, on stronger evidence.** The call was to build it
  when a duplicate actually appears. Re-run at 1,556 rows across 40 statements: still zero
  cross-statement duplicate candidates.

**Deliberately deferred:** budgets, forecasting, recurring-subscription detection, bank API
sync, mobile.
