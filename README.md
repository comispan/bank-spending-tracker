# bank-spending-tracker

Upload card statement PDFs → get a categorized monthly spending report across all your cards.

**Status:** design phase. No code yet.

See **[DESIGN.md](DESIGN.md)** for the architecture, data model, and build plan.

## The short version

1. Upload a statement PDF.
2. It's parsed into transactions, and the parse is **verified against the statement's own totals** — if the numbers don't reconcile, it goes to a review queue instead of silently importing wrong data.
3. Transactions are categorized (user rules → learned merchant memory → LLM for the rest).
4. Statements from every card are merged and bucketed by **calendar month**, deduplicated, and rendered as one report.

## Next step

Phase 0 spike: take 5–10 real statements and check whether extraction reconciles. Everything else waits on that answer. See DESIGN.md §8.
