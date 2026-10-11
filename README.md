# bank-spending-tracker

Upload card statement PDFs → get a categorized monthly spending report across all your cards.

**[Live demo](https://comispan.github.io/bank-spending-tracker/)** — a static snapshot of the
app running on synthetic statements.

## How it works

1. **Upload** a statement PDF (or a whole folder of them).
2. **Parse and verify.** Rows are read straight from the PDF's text layer — in code, offline,
   no model — and the result is checked against the statement's own printed totals. A
   statement that doesn't reconcile is flagged for review instead of silently imported.
3. **Categorize.** Your rules first, then learned merchant memory, then an opt-in model for
   whatever is left.
4. **Report.** Statements from every card are merged by calendar month, with drill-through,
   month-on-month change, and a note on how far the data actually reaches.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --app-dir app --port 8000
```

Open <http://localhost:8000>. No statement handy? Press **Load demo statements** on the
landing page: it generates 48 synthetic statements (three invented banks, sixteen months
each) and files them through the same parser and checks a real upload goes through.
**Delete demo** removes exactly those and leaves your own statements alone.

More in [app/README.md](app/README.md).

## Your data stays on your machine

The PDF is parsed locally, and the file and its rows are kept in a `data/` folder next to
the app. There is no account, no cloud, and nothing is uploaded. Full card numbers are
masked before anything is stored, and a password for a locked PDF is used in memory and
never written down.

The one exception is off unless you configure it: **tier 3** on the Merchants page can send
merchant names (plus one example statement line each — no amounts, dates or card numbers)
to Google's Gemini API using your own key, and shows you the exact payload before you
send it. See [app/README.md](app/README.md#tier-3).

## Status

Working end to end: upload, verify, categorize, monthly and multi-month reports, merchant
groups, and card-coverage timeline. Tested against real statements from six issuers, all
of which reconcile.

Not built yet:

- **OCR for scanned statements** — scans are detected and refused rather than misread.
- **CSV / Excel export.**
- **Re-running the parser over stored statements**, so a parser fix reaches old data
  without re-uploading.

New issuers may need their summary labels added before their totals can be verified —
row parsing generalizes, summary figures don't.

Out of scope for now: budgets, forecasting, subscription detection, bank API sync, mobile.

## Docs

| | |
|---|---|
| [app/README.md](app/README.md) | Running the app, demo mode, tier 3 |
| [DESIGN.md](DESIGN.md) | Architecture, data model, build plan |
| [TIER3.md](TIER3.md) | Tier 3 model grades, and why grounding ships off |
| [spike/README.md](spike/README.md) | The extraction spike and its findings |
| [deploy/README.md](deploy/README.md) | Single-user deploy and the Pages snapshot |

## Licence

[MIT](LICENSE). This is a personal tool that reads bank statements, so the licence's
warranty disclaimer is not boilerplate: every parse is checked against the statement's own
totals, but the figures are only as good as that check, and nothing here is financial
advice.
