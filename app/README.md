# The app

Phase 1: upload a statement PDF, parse it, verify it against the statement's own
printed figures, and show the result. Single-user, self-hosted, offline.

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --app-dir app --port 8000 --reload
```

Then open <http://localhost:8000>. Data lives in `data/` — the SQLite file and
the uploaded PDFs — and is gitignored.

## What's here

| File | Does |
|---|---|
| `rows.py` | The parser. Moved here from the Phase 0 spike unchanged; it is the one component proven against real statements. |
| `parsing.py` | PDF → verified transactions: decrypt, redact, parse, reconcile. |
| `merchants.py` | Description → the merchant key the categorizer learns against. Phase 2, step 1. |
| `categorize.py` | Tiers 1 and 2, and the `flow_type` axis. Pure functions; no DB, no network. |
| `db.py` | SQLite schema and queries. Money is integer minor units, never a float. |
| `main.py` | FastAPI routes and the four pages. |

`spike/` still runs the same `rows.py` against real statements and prints a
verdict per bank. It is the regression harness — run it after touching the
parser:

```powershell
python spike\selftest.py
python spike\extract.py
```

## Things that look like mistakes and aren't

**Parsing happens inside the request.** It measures ~182 ms/page, so a four-page
statement is under a second. The queue, worker and job-status polling in the
original design existed to hide a 10–60s LLM round-trip that no longer happens.

**The row count can be lower than the "rows found" figure.** `rows_expected`
comes from a regex deliberately independent of the parser, and some issuers rule
their opening and closing balance into the transaction table as dated rows —
those are claimed as summary figures, not transactions. Trust legitimately
parses 7 of its 9 date-and-amount lines. A gap of more than two or three is
worth investigating; that comparison is what caught both Phase 0 bugs.

**The review screen shows extracted text, not a picture of the page.** When a
row is missing, the only useful question is what the parser was looking at, and
a rendered image cannot answer it. The PDF is one click away for the times you
want to see the original.

**Merchant keys keep more than one word.** `uniqlo ion orchard` rather than
`uniqlo`. A key only has to be the same next month and different from other
merchants — that outlet does not move, so the longer key is stable, and cutting
to one word would merge `royal plaza` with `royal sporting house`. Where one
merchant does end up with two keys, `merchant_root()` — the first word — is the
fallback that reunites them, and tier 2 will look up the precise key first and
the root second.

**The bulk screen categorizes merchants, not transactions.** `/merchants` is
the fast way to a complete report, and it needs no model: the uncategorized
rows are far fewer merchants than they look, and one decision there settles
every past row from that merchant and every future one. It sorts by money, and
lists keys sharing a first word together — a merchant the statement spells two
ways shows up as adjacent rows. They are never merged automatically; that is
the eager root-collapse `merchants.py` refuses, and the person reading the
screen can see what a machine should not assume.

**An unknown merchant stays uncategorized, not `Other`.** There is no third
tier yet, so a merchant no rule and no memory entry knows gets nothing, and the
transactions page counts it honestly as unknown. Filing it under `Other` would
make the same gap look answered — and `Other` is a real category a user may
genuinely choose, so it cannot double as "we don't know".

**"Apply to all matching" also controls whether the choice is remembered.**
§3 asks for the mapping to be written to tier 2 *and* for applying it to past
rows to be offered. Those cannot be two independent switches: memory feeds the
re-resolution pass, so anything remembered reaches the past rows on the next
boot regardless of what the checkbox said. Rather than let the checkbox quietly
do nothing, it means what it appears to mean.

**Non-spend rows are categorized without any merchant lookup.** A card payment
is `Cash & Transfers` because of what kind of row it is, not because anyone
learned the merchant — that is the `flow` source. A refund is deliberately not
treated this way: §3 nets refunds against the original merchant, and
normalization already keys the refund to that merchant, so it inherits that
merchant's category instead.

**The flow classifier is biased toward `spend`.** Ambiguous rows — a GIRO that
might be a bill or might be a card payment, a PayNow that might be lunch — stay
spending. A row wrongly left in the total is visible and one click from fixed;
a row wrongly excluded is money that vanishes from the report, and a total
that is quietly too low looks exactly like a frugal month.

**Merchant keys and categories are recomputed on every boot.** Both are derived
from the stored row by pure functions, so improving `merchants.py` or
`categorize.py` re-keys and re-resolves the statements already uploaded instead
of leaving them on the old rules. A normal boot moves zero rows and prints
nothing. The one thing a re-resolution never touches is a row whose
`category_source` is `user`.

**`unverified` is a defect state, not a resting state.** It means the statement
printed no totals to check against — which is indistinguishable from a parse
that is simply wrong. Both real bugs found in Phase 0 were hiding under it.

## Not here yet

**Tier 3** — a model for the merchants tiers 1 and 2 do not know (§3, step 5).
Everything in the app today is free, offline and exact; the resolver returns
nothing for an unknown merchant and the UI shows that as a gap, which is the
shape tier 3 slots into when it arrives. It fills the `None`s and touches
nothing else. The open question in §9.4 — a cloud model or a local one — is
still open, and is the only place in the app where anything would leave the
machine.

Also absent: the cross-statement monthly report and transaction-level dedup
(Phase 3), OCR for scanned statements (Phase 4), and `category_confidence` from
§5 — nothing produces a confidence today, because a rule, a memory hit and a
derived flow are all certain. It goes in with tier 3, which is the first thing
that will have an opinion rather than an answer. Foreign-currency sublines are parsed into the description but not yet
split into `amount_minor` + `fx_rate`; the columns exist and DESIGN.md §4 says
how they should be filled.
