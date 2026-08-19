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

**`unverified` is a defect state, not a resting state.** It means the statement
printed no totals to check against — which is indistinguishable from a parse
that is simply wrong. Both real bugs found in Phase 0 were hiding under it.

## Not here yet

Categories and merchant normalization (Phase 2), the cross-statement monthly
report and transaction-level dedup (Phase 3), OCR for scanned statements
(Phase 4). Foreign-currency sublines are parsed into the description but not yet
split into `amount_minor` + `fx_rate`; the columns exist and DESIGN.md §4 says
how they should be filled.
