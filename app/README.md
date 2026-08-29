# The app

Upload statement PDFs, parse them, verify each against the statement's own
printed figures, categorize the rows, and read the month. Single-user,
self-hosted; the only outbound request is tier 3 (§9.4) and it is a button.

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
| `tier3.py` | Tier 3: the prompt, the schema, the response gate and the Gemini client. The only outbound request in the app. |
| `months.py` | Calendar months, and how much of one the statements actually cover. Pure functions. |
| `db.py` | SQLite schema and queries. Money is integer minor units, never a float. |
| `main.py` | FastAPI routes and the pages. |

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

**A star is read, not assumed.** `GRAB *TRIP 4821` puts the merchant on the left
of the star and `GRB*Dunkin Donuts` puts it on the right, so `_resolve_star`
reads both sides: a gateway is a short code (`SMP`, `GRB`, `2C2`) that hands over
a whole shop name, where a merchant starring its own reference is a real word
followed by digits. Keeping the left by default was merging every shop behind an
unlisted gateway into one key. Simply inverting the default is worse and was
measured before it was rejected — it turns `SIMBATELECOM****2269` into `2269` and
`GRAB *TRIP` into `trip`, keys that change every month. The known-processor list
stays, for a gateway that hands over a single word.

**A foreign charge is three lines, not one.** The merchant is printed above the
dated line and the rate below it, so parsing the dated line alone gives a row
with the right money and `102.67 HKD` where the merchant should be — it
reconciles perfectly and can never be categorized. `rows.py` claims all three
or none: with no name on the line above, the figure is left where it is, since
a description replaced by a name that was never found is worse than one that is
honestly not a merchant. The transactions page shows the original amount and
the statement's own rate under the SGD figure.

**The three-month average is usually absent, and says why.** It is held to the
same rule as the month-on-month delta, for a sharper reason: an average hides a
part-billed month better than a single comparison does. Three short months make
one low figure with nothing on its face to say it is short, and every month
compared against it then reads as an overspend. A month contributes only if it
is billed across the days being reported, and two contributors are the minimum
— an average of one month is last month, which the delta already shows.

**"New" merchants are new to the statements you uploaded.** Not to your
spending. An unbilled fortnight can hide a first visit, so the screen says the
thing it can actually stand behind. A category with no history at all is
reported as a new merchant rather than as a category running hot: dividing by
an absent average turns a first $3 coffee into an infinite overspend.

**A filtered transaction list says so, loudly.** Every figure in the month
report links to the rows behind it, so the page is the far end of four
different links, and a filtered list that renders identically to the full list
is how a slice gets read as the total. It prints the active filters and the net
spend of what is on screen — a number that can be checked against the one that
was clicked — while the whole-corpus coverage line beside it stays
whole-corpus, because it answers a different question.

**The month view often refuses to show a month-on-month change.** A comparison
needs both months billed over the same days, and the easy half to remember is
this month. The half that catches you is the month being compared *against*:
comparing August 1–14 to July 1–14 looks fair and is not, if a card has no
statement covering early July — the delta then flatters August by whatever that
card spent. The report says which statement would make the comparison real
rather than printing a number that is wrong in an invisible direction.

**A month can be incomplete at the start as well as the end.** The obvious case
is the newest month, part-billed because every card closes mid-month. The one
that got the first version of this wrong is the oldest month, which begins
partway through because that is when the earliest statement begins. Coverage is
a window with two ends, and the trustworthy total is the intersection across
every card.

**The bulk screen categorizes merchants, not transactions.** `/merchants` is
the fast way to a complete report, and it needs no model: the uncategorized
rows are far fewer merchants than they look, and one decision there settles
every past row from that merchant and every future one. It sorts by money, and
lists keys sharing a first word together — a merchant the statement spells two
ways shows up as adjacent rows. They are never merged automatically; that is
the eager root-collapse `merchants.py` refuses, and the person reading the
screen can see what a machine should not assume.

**An unknown merchant stays uncategorized, not `Other`.** A merchant no rule,
no memory entry and no model knows gets nothing, and the transactions page
counts it honestly as unknown. Filing it under `Other` would make the same gap
look answered — and `Other` is a real category a user may genuinely choose, so
it cannot double as "we don't know". Tier 3 obeys the same rule: `unknown` is a
legal answer from the model and is stored as nothing at all.

**Tier 3 is a button, not something that happens on upload.** It is the only
request the app makes to anything outside this machine (§9.4), so it happens
when you press it. The panel on `/merchants` prints the exact payload first —
merchant keys, one per line, and nothing else.

**Tier 3 writes guesses, and they are labelled as guesses.** A model's answer is
stored with source `llm`, never `memory`, and `INSERT OR IGNORE` means it can
only ever fill a gap — nothing a model returns can overwrite something you
decided. Correcting one turns it into a real `memory` entry permanently, exactly
like correcting a `seed`.

**A tier 3 batch is all-or-nothing.** If the response invents a merchant, drops
one, or answers outside the fixed thirteen, the whole batch is discarded and
those merchants stay uncategorized. Phase 0's finding was that a model fails
*silently* — plausible, well-formed and empty — so a response that is 90% there
is not 90% useful, it is a response you cannot reason about. The failure is
reported on screen rather than folded into the success count.

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

## Tier 3

Off unless you configure a key. Set `GEMINI_API_KEY` in the environment or in a
`.env` file at the project root (already gitignored), then use the panel on
`/merchants`. Override the model with `GEMINI_MODEL` if you want a different one.

**Grade it before you trust it.** The merchants you categorized by hand are the
answer key, and `spike/eval_categories.py` scores a candidate against them with
the model's answers hidden:

```powershell
python spike\eval_categories.py --list      # the eval set, nothing sent
python spike\eval_categories.py --gemini    # grade the model the app uses
```

It imports the prompt, the schema and the gate from `tier3.py` rather than
keeping copies, so the number it reports describes the code that actually runs.
Anything that cannot beat the `baseline` row — always answer your most common
category — is not adding anything.

`gemini-3.7-flash` scored **77% correct, 9% wrong, 14% abstained, gate PASS**
against a 53% baseline on 2026-08-28. All four wrong answers were merchants that
are genuinely ambiguous; see DESIGN.md §3 for the breakdown and for the two bugs
its abstentions uncovered.

**Expect the free tier to be slow rather than broken.** The model answers in
seconds, but it returns HTTP 500 "experiencing high demand" under load and the
free tier allows five requests a minute — the graded run took 148s, almost all
of it waiting. `ask_gemini` retries those, waits as long as the API's own
response asks it to, and does not retry a 400 or 401 that would fail the same
way forever.

## Not here yet

Transaction-level dedup, deferred on evidence: across 343 rows and 10
statements there are zero cross-statement duplicate candidates, and the
`file_sha256` check plus the issuer reference cover what actually occurs. OCR
for scanned statements is Phase 4.

`category_confidence` from §5 is still unfilled — a rule, a memory hit and a
derived flow are all certain, and tier 3 currently expresses doubt by abstaining
rather than by scoring itself, which is the more honest of the two and needs no
column. Foreign-currency sublines are parsed into the description but not yet
split into `amount_minor` + `fx_rate`; the columns exist and DESIGN.md §4 says
how they should be filled.
