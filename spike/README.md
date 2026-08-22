# Phase 0 spike

**Question this answers:** can we extract transactions from a statement PDF and *prove* the extraction is correct — and on which of your banks does it fail?

Nothing here is production code. Answer the question, throw it away, then build Phase 1 knowing what you're up against.

All commands below are Windows PowerShell.

## There is no model in this pipeline

The spike originally sent each page to an LLM and asked it to find the transaction table. That was the wrong shape of problem. `pdfplumber.extract_text(layout=True)` already recovers the table — columns aligned, amounts on the right row — on every bank tested. Asking a model to "detect the table" was asking it to redo work that was already done, and it is precisely the step small local models fail at, silently, by returning an empty array.

So the table is parsed in code, in [`rows.py`](rows.py). The consequences are worth stating plainly:

- **Nothing leaves your machine.** Not "redacted before it leaves" — nothing leaves. No API key, no provider choice, no data terms to read.
- **A whole statement parses in well under a second**, versus minutes per page against a local model on CPU.
- **A wrong answer is a bug you can fix**, not a sampling artefact you can only re-roll. The same PDF always gives the same output.
- **It costs nothing**, so there is no reason to economise on which statements you check.

What this gives up is the ability to read a page with no text layer. A scanned statement is now reported and skipped; see [Scanned statements](#scanned-statements).

## Grading a tier-3 model

`eval_categories.py` answers the question DESIGN.md §9.4 leaves open — cloud
model or local one — with a number instead of a preference. It grades a
candidate against the merchants **you** categorized by hand, which is the only
ground truth that exists for your own spending.

```powershell
python spike\eval_categories.py --list
python spike\eval_categories.py --model baseline
python spike\eval_categories.py --model qwen2.5:3b-instruct
python spike\eval_categories.py --model claude-sonnet-5 --anthropic
```

It measures what would actually ship, gate included. Every response goes through
the same contract tier 3 would enforce — no invented merchants, none dropped,
every category inside the fixed thirteen — and a model that scores well but
breaks the contract is reported as unusable, because it is. That is the same
silent-failure shape as the empty array above: correct-looking content, broken
format.

Read the three numbers in this order:

- **WRONG** is the one that decides it. A confident disagreement is what tier 3
  would write into merchant memory and mislabel your spending with.
- **abstained** is not a failure. `unknown` leaves a row uncategorized, which is
  honest and one click from fixed.
- **correct** means nothing without the baseline printed beside it — always
  answering your single most common category already scores 53% on this set.

Nothing leaves the machine without `--anthropic`, and even then only merchant
names — no amounts, dates, balances or card numbers. Pin `-instruct` tags for
local models; a bare tag is often the thinking build, which narrates into the
response and breaks the format contract before the categories are looked at.

## Setup

Statements are among the most sensitive documents you own. `spike/statements/`, `spike/out/`, and `passwords.json` are all gitignored — check that before you copy anything in.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r spike\requirements.txt
```

If activation fails with *"running scripts is disabled on this system"*, PowerShell's execution policy is blocking it. Allow it for this window only — no permanent change to your machine:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Step 1 — smoke-test (no real data)

```powershell
python spike\selftest.py          # encoding, parser, and the reconciliation gate
python spike\make_sample.py       # writes a synthetic statement
python spike\extract.py
```

Confirms the plumbing works before you point it at anything real. `selftest.py`
touches no statements; run it first if anything misbehaves, since it isolates a
broken environment from a bad parse.

### A note on text encoding

All output is written as UTF-8 with LF endings on every platform, via the
`write_text`/`read_text` helpers in `extract.py`. Nothing calls Python's
`Path.read_text`/`write_text` directly — those inherit the platform's locale
encoding (cp1252 on Windows), which cannot represent characters that genuinely
appear in statements, and the failure is a mid-run crash. `selftest.py` audits
the source for that mistake, so a future edit that reintroduces it fails loudly.

Files we *don't* write — currently just `passwords.json` — are decoded
tolerantly (UTF-8, UTF-8-with-BOM, then cp1252), so it doesn't matter which
editor you used to create it. Notepad's BOM is handled.

## Step 2 — look at the extracted text

```powershell
python spike\extract.py --dry-run
```

Writes each statement's extracted text to `spike\out\<name>.txt` and stops. Open a few and check whether the transaction table survived — columns aligned, amounts on the right row, dates intact.

```powershell
notepad spike\out\dbs.txt
```

This step is worth doing carefully. If the text comes out scrambled or empty, no parser can fix it downstream, and you've learned that immediately.

The `rows` column counts lines that start with a date and end with an amount. That count is deliberately computed by a *separate* regex from the real parser, so comparing the two numbers tells you whether the parser found everything the page actually contains — see the note under [What the result means](#what-the-result-means) for when a smaller number is legitimate. `TABLE-OK` means the table structure made it through; `NO-TABLE` means inspect the `.txt`.

### Encrypted statements

Many issuers encrypt statements only to set permission flags (no printing, no
copying) and leave the user password *empty* — DBS does this. Those open with no
password at all, and the tool tries that automatically before asking for one.

If a statement is genuinely locked, create `spike\passwords.json`:

```json
{ "uob-jun-2026.pdf": "S1234567A", "ocbc-jun-2026.pdf": "0512" }
```

## Step 3 — parse and reconcile

```powershell
python spike\extract.py
```

Per page: text → parsed rows → merged → **reconciled against the statement's own printed totals.** Writes `spike\out\<name>.json`.

Card numbers are masked to last-4 on the way in (`redact()` in `extract.py`), so the `.txt` and `.json` dumps are safe to paste into a bug report.

## How the parser works

[`rows.py`](rows.py) does five things:

1. **Peels dates off the front of a line.** Up to two — a transaction date and a posting date. An inline year must be four digits, because a two-digit year is indistinguishable from the day of the *next* date, and `27 JUN 28 JUN` silently losing its second date halves every issuer that prints both.
2. **Takes the amount off the end**, along with however the issuer signals direction: a trailing `CR`/`DR`, a leading `+`/`-`, or accounting parentheses. Absent any of those it assumes a debit, which reconciliation contradicts loudly if it's wrong.
3. **Resolves the year once per document**, not per page. Only page 1 tends to print the statement period or any four-digit year; continuation pages carry bare `12 JUN` rows. Resolving per page throws away every transaction after page one and looks exactly like a quiet month — the worst failure mode available, because nothing reports it.
4. **Reads the summary figures** — previous balance, new balance, printed totals — from a deliberately narrow label list. When a label isn't clearly recognised the field stays empty and the statement is reported `UNVERIFIED`. A loose match that puts the wrong figure in `total_debits` doesn't produce an obvious error; it produces a confident `FAIL` on a correct extraction, or a `PASS` on a wrong one.
5. **Reads horizontal summary grids**, where the figures sit in one row and the labels are stacked above them, aligned by column — MariBank and Trust both do this, and nothing on the figures row says what any of it means. Only the opening and closing balance are taken. The component columns look useful and are a trap: MariBank splits what the transaction table calls a credit across two of them (repayment 216.18, cashback 0.14), so lifting one into `total_credits` would FAIL a perfectly correct extraction by 14 cents. This pass runs last and never overwrites a directly labelled line, so a grid can only add what nothing else said.

## Reading the output

```
statement               pages  rows  verdict     detail
dbs.pdf                     4    45  PASS        balance rolls forward (card convention)
maribank.pdf                5    18  PASS        balance rolls forward (card convention)
sample-statement.pdf        1    15  PASS        matches printed totals
uob.pdf                     8    89  PASS        balance rolls forward (card convention)
                               !  1 identical row(s) within one statement (page double-read?)

6/6 verifiable statements reconciled.
```

In `--dry-run` the verdict is `TABLE-OK` or `NO-TABLE` instead, since nothing has been parsed yet.

| Verdict | Meaning |
|---|---|
| `PASS` | Numbers reconcile. Trustworthy. |
| `FAIL` | Extraction is wrong, or the statement has a quirk the parser missed. **Investigate every one** — the `detail` delta often names the missing transaction outright. |
| `UNVERIFIED` | Parsed, but the statement prints no totals to check against. Not the same as correct. |
| `ERROR` | Couldn't read the file at all. |

## What the result means

- **Most statements PASS** → the design in `../DESIGN.md` holds. Go to Phase 1.
- **Systematic FAILs at one bank** → look at that bank's `out\*.txt`, then at the label list in `rows.py`. Usually a section (fees, FX sublines, instalments) not being counted, or a summary label spelled differently.
- **Row count below the `--dry-run` count** → worth a look, but not automatically a defect. Some issuers rule their opening and closing balance into the transaction table as dated rows; those are claimed as summary figures and dropped, so Trust legitimately parses 7 of its 9 date-and-amount lines. A gap larger than two or three is the single most useful signal here.
- **Mostly `UNVERIFIED`** → the trust gate can't function. This is the worst outcome, because you can never distinguish a good parse from a bad one automatically. Revisit before building further.

Record the verdict table in the PR or an issue. It's the input to every Phase 1 decision.

## Scanned statements

A page with no text layer has nothing to parse. It is counted, warned about, and skipped — never silently treated as a page with no transactions.

Reading one needs OCR. If you hit this, the options in rough order of effort are Tesseract via `pytesseract`, or a small local vision model (`glm-ocr` is ~2.2GB and purpose-built for document layout) used *only* to turn the page into text, with `rows.py` still doing the parsing. Don't reintroduce a model that emits the final JSON — that's the arrangement this spike removed.
