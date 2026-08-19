# bank-spending-tracker

Upload card statement PDFs → get a categorized monthly spending report across all your cards.

**Status:** design phase. The Phase 0 spike is done — see [spike/](spike/).

See **[DESIGN.md](DESIGN.md)** for the architecture, data model, and build plan.

## The short version

1. Upload a statement PDF.
2. It's parsed into transactions, and the parse is **verified against the statement's own totals** — if the numbers don't reconcile, it goes to a review queue instead of silently importing wrong data.
3. Transactions are categorized (user rules → learned merchant memory → LLM for the rest).
4. Statements from every card are merged and bucketed by **calendar month**, deduplicated, and rendered as one report.

## Next step

Phase 0 asked whether extraction reconciles. It does: across six real statements from six
different issuers, every one reconciles against the statement's own printed figures.
It also answered a question we didn't ask — extraction needs no model at all. The PDF text
layer already carries the table, so it's parsed in code, offline, in milliseconds.
See [spike/README.md](spike/README.md).

Phase 1 next. See DESIGN.md §8.
