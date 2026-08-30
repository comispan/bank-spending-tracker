# Spending Tracker — Design

A single-purpose web app: **upload card statement PDFs → get a categorized monthly spending report across all your cards.**

---

## 1. Scope

**In scope**
- Upload one or more PDF statements (credit card, debit card, bank).
- Extract transactions from the PDF.
- Assign each transaction a spending category.
- Merge statements from multiple banks/cards into one monthly report.
- Correct mistakes (recategorize, fix a bad parse, delete a duplicate).

**Explicitly not in scope (v1)**
- Bank API / Plaid-style live sync. The whole premise is "I have PDFs."
- Budgets, goals, alerts, net worth, investments.
- Multi-user households, sharing, accountants.
- Mobile app. Responsive web is enough.
- Receipt matching, line-item splitting.

**The one metric that matters:** a user uploads 3 statements from 3 different banks and gets a correct monthly report without hand-editing more than a few rows.

---

## 2. The hard part (read this first)

Everything else in this app is CRUD. **The entire risk is in PDF extraction.** Statement PDFs are:

- **Structurally unlike each other.** Every issuer has its own layout, and layouts change without notice.
- **Not tables.** A PDF has no rows or columns — just glyphs at (x, y) coordinates. "Table" is something you infer.
- **Sometimes not text at all.** Scans and some issuers' exports are images; those need OCR.
- **Often password-protected.** Many banks encrypt with a DOB/last-4 password.
- **Full of things that are not spending.** Payments to the card, balance transfers, interest, previous balance, rewards redemptions, FX conversion sublines, "continued on next page" fragments.

A design that assumes "parse the PDF into a table" will fail on statement #2. The design below assumes extraction **must be verified**, whatever produced it.

> **Phase 0 result (2026-08-19).** Extraction turned out to be the *easy* part, and needs no model at all. `pdfplumber.extract_text(layout=True)` recovers the transaction table on all five issuers tested; parsing it is ~300 lines of Python. Six statements, six reconciliations, 182 ms/page, fully offline. The sections below have been rewritten to match. See [spike/README.md](spike/README.md) for the evidence.

### 2.1 Extraction pipeline

```
PDF
 │
 ├─ 0. Decrypt (prompt user for password if needed)
 │
 ├─ 1. Text-layer extraction  (pdfplumber, layout=True → aligned columns)
 │       │
 │       └─ if < ~100 chars of text → it's a scan → report and skip
 │          (OCR is Phase 4; never treat a scan as "no transactions")
 │
 ├─ 2. Parse rows deterministically (see 2.2) — dates off the front of each
 │       line, amount off the end, summary figures by label
 │
 ├─ 3. Normalize → Transaction[] (date, description, amount, sign, currency)
 │
 ├─ 4. VERIFY (see 2.3)  ── fails ──→ quarantine, ask user to review
 │
 └─ 5. Persist + hand to categorizer
```

**Why there is no LLM path.** There was one, and removing it was the main finding of Phase 0. The layout-preserved text already *is* the table — asking a model to find it was asking it to redo solved work, and that was the step small local models failed at silently, by returning an empty array. One parser, no fallback, no provider choice, nothing leaving the machine.

**Why no issuer templates either, yet.** The generic parser handled five different issuers with no per-bank code. Templates are the answer if a sixth bank breaks it, not before — and the trust gate below tells you the moment one does.

### 2.2 Deterministic extraction

Implemented in `spike/rows.py`; lift it into the app as-is. Page text with layout preserved (`extract_text(layout=True)` — **not** naive `extract_text()`, which interleaves columns and destroys the table), then per line:

- **Peel up to two dates off the front** — transaction date and posting date. An inline year must be four digits: a two-digit year is indistinguishable from the day of the next date, and `27 JUN 28 JUN` collapsing to one date silently halves every issuer that prints both.
- **Take the amount off the end**, with whatever signals direction — trailing `CR`/`DR`, leading `+`/`-`, accounting parentheses. Default debit; reconciliation contradicts it loudly if wrong.
- **Resolve the year once per document.** Only page 1 prints a period or a four-digit year; doing it per page discards every row after page one and looks exactly like a quiet month.
- **Read summary figures from a narrow label list**, plus horizontal summary grids where figures sit in one row with labels stacked above (MariBank and Trust both do this). Take only opening and closing balance from a grid — MariBank splits one credit across two component columns, so lifting one into `total_credits` FAILs a correct extraction by 14 cents.
- **A dated row whose text opens with a summary label is not a transaction.** Trust rules its opening and closing balance into the table; counted as purchases they inflated debits from 115.82 to 1953.82.

The output shape, unchanged from the original design:

```json
{
  "issuer": "…", "account_last4": "1234",
  "statement_period": { "start": "2026-06-15", "end": "2026-07-14" },
  "currency": "SGD",
  "opening_balance": 1204.55, "closing_balance": 2311.09,
  "total_debits": 1892.40, "total_credits": 785.86,
  "transactions": [
    { "date": "2026-06-18", "posted_date": "2026-06-19",
      "description": "GRAB *TRIP 4821 SINGAPORE",
      "amount": 14.20, "direction": "debit",
      "foreign": { "amount": null, "currency": null } }
  ]
}
```

Notes that matter:
- **Page-at-a-time, stitched afterwards**, so a transaction straddling a page break survives — but the *period and year* are resolved from the whole document first, then handed to each page.
- **Capture the summary figures too** (opening/closing/totals). They are the checksum. This is still the single highest-leverage line in this design.
- Amounts as decimal strings → parse to integer minor units. Never floats in the DB.
- **Keep an independent row-shape count** (`count_txn_shaped_lines`), computed by a *separate* regex from the parser. Comparing the two numbers is how you find out the parser dropped rows the page actually had. It is the check that caught both real bugs in Phase 0.

### 2.3 Verification — the trust gate

An extraction is **accepted** only if it self-reconciles:

1. `sum(debits) ≈ total_debits` and `sum(credits) ≈ total_credits` (exact to the cent), **or**
2. `opening_balance + debits − credits ≈ closing_balance`, **or**
3. If the statement prints no summary figures at all → mark `confidence: unverified`.

Also check: every transaction date falls inside the statement period (±5 days for posting lag); no duplicate (date, amount, description) triples within one statement; transaction count is plausible for the page count.

On failure: **do not silently import.** Put the statement in a "Needs review" state and show the user a side-by-side of the PDF page and the parsed rows so they can fix it in a few clicks. Silently-wrong financial data is far worse than an honest "I couldn't read this one."

This gate is what makes the app trustworthy. Build it in the same PR as the parser, not later.

Phase 0 ran it on six statements from five issuers and all six reconciled. Two of the bugs it caught would have been invisible otherwise — a wrong parse that reported `unverified` looks identical to a statement that simply can't be checked. **Treat `unverified` as a defect to investigate, not a resting state**; both Phase 0 bugs were hiding under it.

---

## 3. Categorization

Three tiers, cheapest first. Each transaction is resolved by the first tier that hits.

| Tier | Mechanism | Cost | Covers after warm-up |
|---|---|---|---|
| 1 | **User rules** — merchant pattern → category, set by the user, always wins | free | ~10% |
| 2 | **Merchant memory** — normalized merchant string → category learned from every prior decision (the user's own + a shared seed list) | free, one index lookup | ~75% |
| 3 | **LLM** — batch of unknown merchants → category, one call for the whole batch | ~cents | the rest |

**This is now the only place a model appears in the app.** Extraction (Section 2.2) sends nothing anywhere, so the disclosure question from Section 9 lands here and only here — and it is a much smaller question: tier 3 sends a *list of normalized merchant names* (`grab`, `fairprice`, `netflix`), not statement text. No amounts, no dates, no balances, no card numbers. A local model is entirely adequate for this shape of task if you would rather it stayed offline; classifying a short string is what small models are good at, which is precisely what Phase 0 found.

**Merchant normalization** is what makes tier 2 work. `GRAB *TRIP 4821 SINGAPORE`, `GRAB* TRIP 9903`, and `Grab Trip SG` must all collapse to `grab`. Strip: trailing numerics, `*` segments, city/country suffixes, POS terminal IDs, dates embedded in the description, `SQ *`/`PAYPAL *`/`AMZN Mktp` style processor prefixes (keep what follows). Lowercase, squash whitespace. Store both `description_raw` and `merchant_normalized`.

> **Built 2026-08-22** as `app/merchants.py`. The rule that generalized is *the merchant is at the front, and the junk starts somewhere*: cut at the first token that cannot be part of a name, then strip the place off what is left — in that order, because 62 rows of one statement end `… SINGAPORE 065` and the place is not last until the reference has been cut off it. Enumerating suffixes per issuer was not needed, for the same reason Section 2.2 parses rows by shape rather than by bank. On the real corpus: 195 distinct descriptions → 112 keys, none empty, and normalizing a key never moves it.
>
> **It produces two keys, and tier 2 must try both** — the precise key, then its first word:
>
> ```
> normalize("STARBUCKS @ RAFFLES CITY SG")  ->  "starbucks raffles city"
> merchant_root("starbucks raffles city")   ->  "starbucks"
> ```
>
> This is the honest version of "all three collapse to `grab`" above. The two starred spellings do reach `grab` exactly; `Grab Trip SG` has no star to cut at and reaches `grab trip`, which meets the other two **at the root**. Reducing everything to one word eagerly would have closed that gap and silently merged `royal plaza` with `royal sporting house` — a learned category applied to a shop the user never categorized. A merchant split across two keys costs one extra click; two merchants merged into one key is wrong data, quietly. The root is also what absorbs the outlet-suffix and hyphen-vs-space variants the corpus turns out to be full of.

Any time a user recategorizes a transaction, write the mapping back into tier 2 **and offer to apply it to all past and future matches**. This is the loop that makes the app feel smart by month three.

> **Tiers 1 and 2 built 2026-08-22** as `app/categorize.py`, pure functions with
> no database and no network so the resolution order is testable the way
> `rows.py` is. An unknown merchant resolves to *nothing* and is counted as
> unknown on the transactions page, rather than being filed under `Other` where
> the gap would look answered. That is also the shape tier 3 slots into — it
> fills the `None`s and touches nothing else.
>
> **Tier 3 built 2026-08-28** as `app/tier3.py`, against Gemini Flash. Four
> things about it are load-bearing, and each is a position this document already
> took somewhere else:
>
> - **It is a button on `/merchants`, not a step in the upload.** This is the
>   app's only outbound request, so it happens when the user asks for it. The
>   panel prints the literal payload — merchant keys, one per line — before
>   sending, because a disclosure the user cannot check is not one.
> - **It writes guesses, marked as guesses.** Answers land in `merchant_memory`
>   with source `llm`, a third kind of claim alongside `seed` (the app's guess
>   about everyone) and `memory` (this user's decision). `INSERT OR IGNORE`
>   makes "a model can only fill a gap" a property of the schema rather than of
>   the caller. Correcting one promotes it to `memory` permanently, exactly as
>   correcting a seed does.
> - **`unknown` is a legal answer and is stored as nothing.** The model
>   declining leaves the row uncategorized, which is honest and one click from
>   fixed. This is the same argument Section 2.3 makes about `unverified`, and it is
>   why the eval scores abstentions separately from errors.
> - **The gate is all-or-nothing per batch.** A response that invents a
>   merchant, drops one, or answers outside the fixed thirteen is discarded
>   whole, and the screen says so rather than folding the loss into the success
>   count. Phase 0's finding was that a model fails *silently*; a 90%-complete
>   response is not 90% useful.
>
> The prompt, the schema and the gate live in `tier3.py` and are *imported* by
> `spike/eval_categories.py`, which previously held its own copies. A harness
> that grades a different prompt than the one that ships is measuring a
> component nobody will run — so `selftest.py` now asserts the identity to stop
> it drifting back apart.
>
> **Graded 2026-08-28, `gemini-3.7-flash` over the 43 hand-labelled merchants:**
>
> | | | |
> |---|---:|---|
> | correct | **33 (77%)** | against a 53% floor — the baseline that always answers the most common category |
> | WRONG | **4 (9%)** | what would have been stored and mislabelled |
> | abstained | 6 (14%) | left uncategorized, which is the honest outcome |
> | gate | **PASS** | no invented merchants, none dropped, every category inside the fixed thirteen |
>
> It clears the floor by 24 points, and the 9% is the number that decides it:
> four wrong answers, of which **none are careless**. One is `apple`, which
> `categorize.py` already refuses to seed for exactly this reason — subscriptions
> on one row and a laptop on the next. One is a beauty shop the model filed under
> Shopping and the user files under Groceries, where the model gave the
> conventional answer and the user's is personal. The other two are opaque
> four-character strings. This is the failure profile you want: it is wrong where
> the merchant is genuinely ambiguous, not where it is legible.
>
> **The abstentions found two bugs, which is the more valuable half of the run.**
> Six merchants the model declined to name, and four of them are not merchants:
>
> - **Unlisted payment-processor prefixes keep the processor and discard the
>   shop.** `merchants.py` implements Section 3's "keep what follows the `*`" against an
>   enumerated list — `SQ *` and `PAYPAL *` resolve correctly — and everything
>   not on that list does the opposite of what the rule intends, normalizing to
>   the *prefix*. Every merchant behind a given gateway then collapses into one
>   key. That is precisely the `royal plaza` / `royal sporting house` merge Section 3
>   designed the two-key scheme to prevent, arriving from the other direction:
>   two merchants merged into one key is wrong data, quietly.
>
>   **Fixed 2026-08-29, by shape rather than by inverting the default.** The
>   obvious repair — an unrecognized `XXX*` keeps what *follows* the star — was
>   measured against all 16 starred rows in the corpus first, and it trades four
>   wrong keys for eight: `GRAB *TRIP 4821` becomes `trip`, `SIMBATELECOM****2269`
>   becomes `2269`, `AMZN Mktp SG*RT4G91` becomes `rt4g91 amazon.sg`. All three
>   are keys that change next month, which is worse than a coarse key that does
>   not, and it is the failure the enumerated list was avoiding in the first
>   place. What actually separates the two shapes is legible on both sides at
>   once: a gateway is a *code* (`SMP`, `GRB`, `2C2` — one token, three
>   characters or carrying a digit) and it hands over a *whole shop name* (two
>   name-shaped tokens before the first reference or place). A merchant starring
>   its own reference is a real word and gives one token at most before the
>   digits start. Both halves must hold before the right side wins, which keeps
>   `M1*DATA 88213` on the left. The list survives as an override for a known
>   gateway that hands over a single word (`SQ *BLUE BOTTLE COFFEE`).
>
>   Same lesson as Section 2.2 and for the same reason: an enumerated list only ever
>   names the gateways already met, and the one it has not met is the one that
>   merges two shops. Six rows re-keyed on the next boot — `renormalize_merchants`
>   replays the rule over everything already stored — and the five that had
>   inherited a category from the gateway code correctly went back to
>   uncategorized, because a decision made about `smp` was never a decision about
>   Li Xin Fish Ball. Coverage 341/343 → 336/343; that drop is the bug becoming
>   visible, not a regression.
> - **A foreign-currency subline is stored as a transaction's whole
>   description.** The Trust HKD charge from Section 4 has the right amount and
>   reconciles clean, but its merchant name is gone, so it can never be
>   categorized by anything. Section 4 anticipated the split and it is still unbuilt.
>
>   **Fixed 2026-08-29.** The row is three lines, which is why one-line parsing
>   lost it — merchant above, both figures on the dated line, rate below:
>
>   ```
>                            Pinduoduo
>       02 Jul   04 Jul                   102.67 HKD    16.99
>                             1 HKD = 0.1655 SGD
>   ```
>
>   The amount comes off the end as always, so what is left over is the
>   *foreign* figure rather than a merchant. `rows.py` now recognizes that
>   shape, claims the name from the line above and the rate from the line
>   below, and stores all three — or none: with no name above, the figure stays
>   where it is, because a description replaced by a name that was never found
>   is worse than one that is honestly not a merchant. A rate naming a
>   different currency is not claimed either. This is what finally implements
>   Section 4's "store both, at the statement's own rate": `amount_minor`/`currency`
>   hold 102.67 HKD, `amount_sgd_minor`/`fx_rate` hold 16.99 at 0.1655, and the
>   SGD side is still what reconciles. `db.backfill_foreign_amounts` replays it
>   over statements already uploaded, off `page_text` and matched on
>   `(date, amount)` — the same argument as `backfill_periods`, and Section 5's reason
>   for keeping the text at all.
>
> Neither is a tier-3 defect and both were invisible before tier 3 ran. A model
> that abstains honestly turns out to be a detector for keys that carry no
> merchant — which is an argument for the abstention rule that nobody made when
> it was written.
>
> **Run against the real backlog, same day.** 34 unknown merchants in, 32
> categorized and 2 declined, moving 37 transactions and taking coverage from
> **89% to 99.4%** (341 of 343). Both survivors are keys with no merchant in
> them — one truncated to three characters, one a holding company — so the
> remaining gap is the normalization bug above, not a categorization gap.
> Fixing that bug on 2026-08-29 took coverage back to 336/343, because the four
> shops it un-merged arrived as merchants nobody had decided about yet. That is
> the number doing its job: 99.4% was counting a gateway code as an answer.
>
> Worth stating plainly, because the Section 8 note argued the opposite and was right
> to: tier 3 did not clear this backlog *better* than the `/merchants` screen
> would have. It cleared it *unattended*, at 9% wrong, and the errors it makes
> are on merchants a person would also have to think about. The screen remains
> the honest way to answer 43 merchants you have opinions about; tier 3 is for
> the handful each month that you don't.
>
> **Cost and latency, free tier.** The 43-merchant batch is one request. The
> model answers in seconds, but `gemini-3.7-flash` returns HTTP 500 "experiencing
> high demand" under load and the free tier allows five requests a minute, so the
> graded run took 148s wall-clock — nearly all of it waiting between retries.
> Both failures are temporary and both say so in the response body, so
> `ask_gemini` retries them, prefers the delay the API itself asks for over a
> guessed backoff, and refuses to retry a 400 or 401 that would fail identically
> forever. A read timeout is retried too: it is not a `URLError` but a sibling of
> it under `OSError`, which is how the first live run escaped the error handling
> entirely and surfaced as a stack trace.
>
> Three things came out differently from the sketch above, each for a reason:
>
> - **"Apply to all matching" also decides whether the choice is remembered.**
>   "Write it back to tier 2" and "offer to apply it to past matches" cannot be
>   independent switches: memory feeds the re-resolution pass, so anything
>   written reaches the past rows on the next boot whatever the checkbox said.
>   One switch that means what it appears to mean beats two where one is a lie.
> - **`category_source` gained `seed` and `flow`** beyond Section 5's
>   `rule|memory|llm|user`. A shipped guess and a decision the user made are
>   different claims and the UI renders them differently; `flow` marks the rows
>   that need no merchant lookup at all. Both are visible on every row, because
>   a UI that renders a guess identically to a decision is asking to be trusted
>   more than it has earned.
> - **`merchant_memory` carries no `flow_type`**, matching Section 5 and not the
>   `merchant_rule` row above it. A category is a property of the merchant;
>   whether a given row was a purchase or a refund is a property of the row.
>   Learning "Uniqlo is Shopping" must not declare every future Uniqlo refund to
>   be spending.
>
> The flow classifier is deliberately biased toward `spend`: ambiguous rows — a
> GIRO that might be a bill or might be a card payment, a PayNow that might be
> lunch — stay spending. A row wrongly left in the total is visible and one
> click from fixed; a row wrongly excluded is money that vanishes from the
> report, and a total that is quietly too low looks exactly like a frugal month.
> This is Section 2.3's argument applied to the second half of the app.

> **Alternative backends, graded 2026-08-30.** Three of them, against the same
> 43 hand-labelled merchants and through the same `gate()` and `score()`:
> `nvidia/nemotron-3-ultra-550b-a55b:free` over OpenRouter, `claude-sonnet-5`
> and `claude-opus-5`. Recorded because Section 9.4 claims tier 3 is a swap
> rather than a lock-in, and the claim is worth more with numbers attached.
>
> | | `gemini-3.7-flash` | `claude-opus-5` | `claude-sonnet-5` | Nemotron (free) | baseline |
> |---|---:|---:|---:|---:|---:|
> | correct | **33 (77%)** | 30 (70%) | 30 (70%) | 26 (60%) | 23 (53%) |
> | WRONG | 4 (9%) | **2 (5%)** | 3 (7%) | 7 (16%) | 20 (47%) |
> | abstained | 6 (14%) | 11 (26%) | 10 (23%) | 10 (23%) | 0 |
> | gate | PASS | PASS | PASS | PASS | PASS |
>
> **Nemotron is rejected on WRONG**, which nearly doubled against Flash. That is
> the number that decides it: a confident disagreement is what gets written into
> `merchant_memory` and silently mislabels the spending, and 60% against a 53%
> floor is a seven-point margin over answering "Dining" every time.
>
> **The other three rank differently depending on which column you read, and
> that is the finding rather than a complication.** Flash has the most correct
> answers and the most wrong ones; Opus has the fewest of both. Going from Flash
> to Opus costs 3 correct answers and buys back 2 wrong ones, with 5 more
> abstentions. Section 2.3 already priced that trade: an abstention leaves the
> row uncategorized, visible on `/merchants` and one click from fixed, while a
> wrong answer is stored as an `llm` guess that silently mislabels the spending
> and is never looked at again. Three extra clicks against two fewer silent
> errors is a good deal, so **`correct` is the wrong column to rank on** and the
> stronger models are stronger precisely where it is hard to see.
>
> Caveats. Both Claude runs were done by hand rather than through the harness —
> same prompt, same 43 merchants, same gate and scorer, but single runs with
> unrecorded sampling settings; `--model claude-opus-5 --anthropic` reproduces
> them properly. Both are paid per call, where the Flash/Nemotron comparison was
> free tier against free tier. And 43 merchants is a small set: a 2-vs-4
> difference in WRONG is four merchants, not a law of nature.
>
> **Two entries in the answer key are worth more scrutiny than the models are.**
>
> - `venus beauty pte ltd` is scored WRONG for all three non-Flash models, and
>   not one of them chose `Groceries` — Nemotron and Sonnet said Health, Opus
>   said Shopping. Three independent models rejecting the label is better
>   evidence about the label than about the models.
> - `apple` is labelled `Bills & Utilities`, which is almost certainly right —
>   it is a recurring subscription — but that fact lives in the *transaction*,
>   not in the merchant name. Nothing reading the string `apple` can recover it.
>   Sonnet abstained, which is the honest answer; Flash, Nemotron and Opus all
>   guessed Shopping. It is scored as three models failing, and it is really the
>   eval asking a question the payload cannot answer.
>
> Both cap what any model can score here, which is worth knowing before chasing
> the last few points: the ceiling on this set is below 100% for reasons that
> have nothing to do with the model.
>
> Two findings about Nemotron specifically, worth keeping if anyone revisits it:
>
> - **It answers at most ~32 merchants and then silently stops.** Batches of 61
>   and 45 both came back with exactly 32 assignments and a normal `stop`
>   finish — not truncated, just short. The gate caught both, but `BATCH_SIZE`
>   is 60, so tier 3 would have discarded every batch and stored nothing at all.
>   Any swap to a weaker model has to re-measure that ceiling first; it is not a
>   number that carries over.
> - **Latency is the hidden cost of a reasoning model.** 60-90s per batch
>   against Flash's few seconds, and tier 3 runs synchronously inside the web
>   request.
>
> **Four of Nemotron's seven errors were one mistake** — `chateraise`, `four
> leaves`, `paris baguette` and `pullman bakery` all filed under Groceries
> rather than Dining. The first reading of that was that the prompt should name
> bakeries and cafés; both Claude models killed it. Given the *identical*
> prompt, each put all four under Dining, along with `ambakerycuisine` and
> `shopback swee heng bakery`. The prompt is not underspecified — some models
> know what a Singapore mall bakery is and one does not. Worth stating plainly,
> because it is the more expensive mistake to make: a prompt tweak is cheap,
> would have been measured against the wrong hypothesis, and every hour spent
> tuning wording is an hour not spent on the thing that actually differed.

**Category set** — keep it small and fixed in v1; a huge taxonomy makes both the LLM and the user worse at choosing:

`Groceries · Dining · Transport · Shopping · Bills & Utilities · Health · Entertainment · Travel · Education · Fees & Interest · Cash & Transfers · Income/Refunds · Other`

Allow user-defined categories in v1.1, not v1.

**Non-spending must be excluded from spend totals**, not categorized as spend:
- Card payments (`PAYMENT - THANK YOU`) — these are transfers, and if you have both a bank statement and a card statement, counting them double-counts.
- Balance transfers, refunds/reversals (net them against the original merchant), rewards redemptions.

Model these as `flow_type ∈ {spend, refund, transfer, fee, income}` — a separate axis from category. Reports sum `spend` and net out `refund`.

---

## 4. Consolidation

Two traps here, both worth designing around explicitly:

**Trap 1 — statement periods are not calendar months.** A card cycle might run Jun 15 → Jul 14. Users think in calendar months. **Always bucket by transaction date, never by which statement a transaction came from.** A single monthly report will therefore draw from parts of two statements per card. This also means a month is "incomplete" until every card's covering statement is uploaded — show that state honestly ("Jul 2026: 2 of 3 cards uploaded").

**Trap 2 — duplicates.** The same transaction appears twice when (a) the user re-uploads a statement, (b) two statements' periods overlap, (c) a card statement and a bank statement both show the same debit.

Dedup key: `(account_id, date ±3 days, amount, merchant_normalized)`. Two matches → keep the earlier-ingested one, flag the other as `suspected_duplicate` rather than hard-deleting, and let the user confirm. Also hash the file (SHA-256) so re-uploading the identical PDF is caught instantly and cheaply.

**Multi-currency — decided: SGD base, store both.** Reports are single-currency SGD. Every transaction also keeps its original amount and currency, converted at the FX rate the *statement itself* printed, not today's rate. This is live, not hypothetical: the Trust statement in the Phase 0 set has an HKD charge with the rate printed on the page (`102.67 HKD × 0.1655 = 16.99 SGD`). If no rate is printed, fall back to a daily rate table and mark the figure approximate.

**The monthly report** (the actual product):
- Total spend, vs. prior month, vs. 3-month average.
- Breakdown by category — a bar or treemap, plus the numeric table. A donut of 13 categories is unreadable; don't.
- Per-card contribution.
- Top merchants.
- New/unusual: merchants not seen before, and categories >50% above their trailing average.
- Drill-through from any figure to the transaction list, and from any transaction to the source PDF page.

That last one — **every number traces back to a page in a PDF** — is what makes users trust it. Store the page number and bounding box at extraction time; it costs nothing then and is impossible to reconstruct later.

---

## 5. Data model

Single-user, so there is no `user` table and no `user_id` anywhere. Base currency is SGD, a constant.

```
account         id, issuer, kind(credit|debit|bank), last4, nickname, currency
statement       id, account_id, file_sha256, storage_key, period_start,
                period_end, opening_balance, closing_balance, page_count,
                parser_version, status(parsed|needs_review|failed),
                verdict(pass|unverified|failed), verdict_detail,
                rows_expected, rows_parsed, raw_extraction jsonb
transaction     id, account_id, statement_id,
                txn_date, posted_date,
                description_raw, merchant_normalized,
                amount_minor bigint, currency,          -- as printed
                amount_sgd_minor bigint, fx_rate,       -- converted, statement's own rate
                direction(debit|credit), flow_type(spend|refund|transfer|fee|income),
                category, category_source(rule|memory|llm|user), category_confidence,
                source_page,
                dedup_key, duplicate_of_id nullable
merchant_rule   id, pattern, match_type(exact|contains|regex),
                category, flow_type, priority
merchant_memory merchant_normalized, category, hit_count, updated_at
```

`issuer_template` is gone — one generic parser handles all five issuers. Add it back when a bank actually breaks, not in anticipation.

`rows_expected` / `rows_parsed` store the independent row-shape count against what the parser produced. A gap is the earliest warning that a layout changed; it is worth a column precisely because it is invisible otherwise.

Conventions worth locking in now: **money is `bigint` minor units, never float.** Dates are plain `DATE` (a transaction date has no timezone). `raw_extraction` is kept so a parser improvement can be replayed over old statements without re-uploading — and because extraction is now deterministic and free, replaying the whole corpus through a new parser version takes seconds and gives byte-identical results for anything that didn't change. That is a much stronger position than the original design assumed.

---

## 6. Stack

**Decided: Python throughout.** The extractor already works in Python and is the one component proven against real statements; a service boundary around a 182 ms function is not worth crossing.

- **FastAPI + Jinja + HTMX.** Server-rendered, one process, no client build step. The interactions here are upload, a table, and a recategorize dropdown — HTMX covers all of them.
- **SQLite**, single file, on the same machine. Single-user with a few thousand rows a year: Postgres buys nothing here and costs setup. `bigint` minor units and plain `DATE` behave fine. Move to Postgres if this ever becomes multi-user.
- **PDF text**: `pdfplumber`. Its word/layout primitives are stronger than anything in JS, which is what made Section 2.2 possible.
- **No queue, no worker.** Parsing measured 182 ms/page — a 4-page statement is under a second, a 20-page one about 3.6s. Parse inside the request with a spinner. `pg-boss`, job status polling, and the whole async subsystem in the original design existed to hide a 10–60s LLM round-trip that no longer happens.
- **Local files, not object storage.** Single-user and self-hosted: PDFs go in a directory the app owns. No buckets, no signed URLs, no lifecycle rules.
- **Categorization LLM** (Section 3, tier 3 only): Anthropic `claude-sonnet-5`, or a local model — it classifies short merchant strings, which is well within a small model.
- **OCR fallback**: deferred to Phase 4. No statement in the Phase 0 set needed it.
- **Charts**: server-rendered SVG, or Observable Plot if you want interactivity.

---

## 7. Privacy & security

This app holds a complete record of someone's spending. Treat that seriously — it's a category of data where a leak is genuinely damaging.

Single-user and self-hosted removes most of the original surface here — there is no tenant boundary to breach, no bucket to misconfigure, and no `user_id` to forget in a `WHERE` clause. What remains still matters:

- **Extraction discloses nothing.** No API key, no provider, no data terms to read. This was the largest item on this list and Phase 0 deleted it.
- Statement passwords: use to decrypt in memory, **never store**.
- The only outbound call is categorization (Section 3, tier 3), and only normalized merchant names — no amounts, dates, or balances. Say so plainly in the UI. Use a provider with no-training-on-inputs terms, or run it locally.
- Full card numbers are masked to last-4 at parse time (`redact()`), before anything is stored or written to disk.
- Don't bind the server to `0.0.0.0`. Localhost only unless you have deliberately decided otherwise.
- Offer hard delete: purge PDFs, transactions, and derived data.
- Don't log statement text or transaction descriptions.
- No third-party analytics on any page that renders financial data.

---

## 8. Build plan

Each phase ends with something you can actually use.

**Phase 0 — Spike. ✅ Done 2026-08-19.**
Six statements from five Singapore issuers (DBS, MariBank, Standard Chartered, Trust, UOB) plus a synthetic control. **6/6 reconcile.** Extraction needs no model; see Section 2.2 and [spike/README.md](spike/README.md). The answer is "easy" — the remaining risk is in the CRUD, which is the good outcome.

**Phase 1 — Single statement, end to end (~3–4 days, revised down).**
Upload → parse → verify → transactions table. One card, no categories yet. Ship the "needs review" state now, not later.

Smaller than originally planned, because Phase 0 removed the queue, the worker, the job-status polling, the object storage, and the auth layer. What's actually left:

1. Lift `spike/rows.py` in unchanged. It is the proven component — don't rewrite it while porting.
2. SQLite schema per Section 5, plus the migration discipline to add to it later.
3. Upload form → save PDF to a local directory → SHA-256 → parse → persist. Synchronous.
4. The gate (Section 2.3) writing `verdict` onto the statement, and a **statement list that shows it**. A `pass` you can't see is worth nothing.
5. Needs-review screen: parsed rows beside the source page. This is where a `fail` or `unverified` gets resolved, and Phase 0 proved you will use it.
6. Dedup on `file_sha256` at minimum — re-uploading the same PDF must be caught. Full transaction-level dedup can wait for Phase 3, but note that UOB already has two genuinely identical same-day rows, so **never dedup silently within a single statement**.

**Phase 2 — Categorization (~1 week). Tiers 1 and 2 done 2026-08-22; tier 3 built 2026-08-28.**
Merchant normalization, the `flow_type` axis, tiers 1 and 2, inline recategorize with "apply to all matching", and a rules & memory page. Seeded with ~100 common merchants, stored as `seed` so a shipped guess is never mistaken for the user's own decision.

**Tier 3 is built against Gemini Flash**, settling Section 9.4: a cloud model, sending normalized merchant names and nothing else, off unless a key is configured and never triggered by an upload. The design is in Section 3; what makes it safe to let a model write at all is that it can only fill gaps, its answers are labelled `llm` rather than `memory`, and a batch that breaks the contract is discarded whole. **Graded before it shipped: 77% correct against a 53% floor, 9% wrong, gate PASS** — and its abstentions found two bugs in components that were considered done (Section 3).

> **What the gap actually is, measured 2026-08-22.** The 103 uncategorized rows are **43 merchants**, five of which account for 59 rows. But they carry **91% of the spending by value** — the seeds cover a lot of small repeating charges and almost none of the money, so a monthly report built today would be describing 9% of the spending. Clearing this is a prerequisite for Phase 3, not a tidying task.
>
> That reshapes what tier 3 is *for*. A screen that categorizes by merchant instead of by row clears the whole backlog in minutes at full accuracy, and a model would have to be checked on all 43 anyway to find out which of its answers were wrong. So the backlog is a UI problem, and tier 3's real job is **the next statement** — the handful of merchants that arrive each month. Built as `/merchants` in the same pass; two decisions on that screen moved 44 rows and took coverage from 36% to 63%.
>
> It also produces the thing that makes tier 3 gradeable: a set of merchant keys the user labelled by hand. Run a candidate model over those with the labels hidden and the disagreements are a real accuracy number — the same discipline as Section 2.3, which is that a component nobody can grade does not ship.
>
> **The grader exists: `spike/eval_categories.py` (2026-08-22).** It scores a candidate against those hand-labelled merchants and enforces the exact contract tier 3 does — no invented merchants, none dropped, every category inside the fixed thirteen — so a model that is accurate but cannot hold a format is reported as unusable rather than promising. `unknown` is a legal answer and is scored as an abstention, not as an error: a row left uncategorized is honest, a confident wrong answer is stored and mislabels the spending. Since 2026-08-28 it imports the prompt, schema and gate from `app/tier3.py` instead of holding copies, so the score describes the shipping code.
>
> Two numbers from it already shape the decision. **The eval set's most common category alone scores 53%** — that is the floor any model has to clear, and it is high because real spending is lopsided. And **a full second month across all five cards leaves 34 merchants (39 rows, 24% of spend) for tier 3**, of which 30 are one-offs — so this is a permanent monthly cost that merchant memory cannot learn away, which is the actual argument for building tier 3 at all.

**Phase 3 — Consolidation & report (~1 week). Month view + completeness done 2026-08-22; report finished 2026-08-29.**
Multiple accounts, dedup, calendar-month bucketing, the monthly report page with drill-through. This is the point where the app becomes the thing you described.

> **What finishing it added, 2026-08-29.** Drill-through, the three-month average, and new/unusual — the three items Section 4 listed that the first pass left out.
>
> **Every figure is now a link.** Category, card, merchant and each excluded flow open the rows behind them, and each row links to the statement page it was read from. That was Section 4's "what makes users trust it" and it cost almost nothing, because `source_page` was already stored — which is exactly why Section 4 said to store it at extraction time.
>
> The half worth stating is the destination, not the links. A filtered list that renders identically to the full list is how a slice gets read as the total, so `/transactions` now prints which filters are active and the net spend of *what is on screen* — a figure that can be checked against the one that was clicked. The whole-corpus coverage line stays beside it rather than being quietly rescoped, because it answers a different question.
>
> **The average is held to the same standard as the delta, and for a sharper reason.** An average hides a part-billed month better than a single comparison does: three short months make one low figure with nothing on its face to say it is short, and every month measured against it then reads as an overspend. So a month contributes only if it is billed across the days being reported, and contributes its spend over *those* days; two contributors minimum, since an average of one month is last month. On this corpus **no month qualifies**, and the report says which months fall short instead of printing a number — the same instruction-to-go-find-a-statement the delta already gives.
>
> **Two bugs in the shipped comparison, both found by making the average produce a number.** Neither was reachable from this corpus, which is why building the feature found them and reading the code did not.
>
> - **Months are different lengths, and the comparison ignored it.** A complete June is billed 1–30 and can never satisfy "billed across days 1–31", so a fair June-to-July comparison refused itself — and the reason it printed, "only billed for days 1–30", reads like a missing statement when it is really just the calendar. The delta had shipped with this since 2026-08-22; `months.covers_days` now clamps the window to the month's own length, and the same helper gates the average.
> - **Two statements on one card sharing an end date raised a `TypeError`.** `statement_windows` sorted `(end, statement)` pairs, so a tie fell through to comparing the dicts, which do not order. Reachable in real use: a bank re-issues a cycle, the corrected file is uploaded beside the original, and `file_sha256` does not catch it because the file genuinely differs. The months page 500s rather than rendering.
>
> **New and unusual are two different standards of proof and are not merged.** A merchant with no earlier row is reportable without a complete month, but it is "new to the statements uploaded", not "new to your spending" — an unbilled fortnight hides a first visit, and the screen says so. A category running hot needs an average to be hot against, so it inherits the trailing gate and reports nothing when nothing qualifies. A category with no history is *new*, not hot: dividing by an absent average is how a first $3 coffee becomes an infinite overspend, and it belongs in the merchant list instead.

> **Trap 1, measured.** The five cards close on **four different days** — DBS ~13th, Standard Chartered and UOB 15th–16th, Trust 17th, MariBank 20th. So redefining a "month" to follow the cycle does not work: any single boundary still slices four cards mid-cycle, and it costs the one framing users actually think in. Calendar months stay; what the report publishes instead is **how far the data reaches**, in `app/months.py`.
>
> Coverage is a window with **two** ends. The newest month is part-billed — obvious. The oldest month begins partway through, because that is when the earliest statement begins — not obvious, and it made the first version of this report call a month complete that was missing its opening fortnight. The trustworthy figure is the intersection across every card.
>
> Two things fell out of building it that no amount of design would have found:
>
> - **A missing statement, detected.** MariBank's 21 Jun – 20 Jul cycle was never uploaded, so July has 4 of 5 cards and every month-on-month comparison in the corpus is currently unsound. The report names it rather than averaging over it.
> - **The comparison has two sides.** It is easy to remember that *this* month may be part-billed and easy to forget that the month being compared against may be too. August 1–14 against July 1–14 reads as fair and is not. A delta is shown only when both months are billed across the same days, and otherwise the reason is printed — which doubles as an instruction for which statement to go and find.
>
> Deferred on evidence: **transaction-level dedup**. Across 343 rows and 10 statements there are zero cross-statement duplicate candidates; the `file_sha256` check and the issuer reference already cover what actually occurs. Build it when a duplicate appears, not in anticipation — the same call Section 5 made about `issuer_template`.

**Phase 4 — Hardening (ongoing).**
OCR path for scanned statements, CSV/Excel export, replay-old-statements-with-new-parser tooling, issuer-specific summary labels as new banks arrive. Encrypted-PDF passwords and multi-currency are handled — Phase 0 shipped the empty-password path (DBS needs it), and the foreign-charge split landed 2026-08-29 (Section 3). Until then this line claimed multi-currency was done on the strength of Section 4 having *decided* it, which is not the same thing: the code stored one currency and no rate. A decision recorded in the design reads exactly like a feature in the code when you skim for what is left.

Replay tooling is now half-built by accident and worth finishing deliberately: `backfill_periods` and `backfill_foreign_amounts` both re-parse `page_text` on boot for one field each. A third would be the point to generalize them into "re-run the parser over every stored statement and diff", which is the thing that makes a parser fix reach old data without re-uploading.

**Deliberately deferred:** budgets, forecasting, recurring-subscription detection (nice, and easy once you have clean data — but it's a different product surface), bank API sync, mobile.

---

## 9. Decisions — settled 2026-08-19

1. **Which banks?** DBS, MariBank, Standard Chartered, Trust, UOB. All five parse and reconcile today.
2. **Base currency?** SGD, with each transaction keeping its original amount, currency and the statement's own FX rate. Driven by a real HKD charge in the Trust statement, not a hypothetical.
3. **Single-user or multi-tenant?** **Single-user, self-hosted.** No auth, no RLS, no tenant scoping. Roughly halves the build and removes most of Section 7.
4. **Is sending statement text to an LLM acceptable?** Moot for extraction — nothing leaves the machine. The question survives only for categorization (Section 3, tier 3), which sends normalized merchant names and no figures at all. **Settled 2026-08-28: yes, narrowly — a cloud model (Gemini Flash), opt-in, for merchant names only.** What made it acceptable was not the model choice but the shape of the payload: `grab`, `fairprice`, `netflix`, one per line, printed on screen before it is sent. Tier 3 is off until a key is configured, is never triggered by an upload, and its answers are stored as `llm` guesses that can only fill gaps. A local model remains a legitimate answer and the eval grades one the same way — `--model qwen2.5:3b-instruct` — so this is a swap, not a lock-in. **Exercised 2026-08-30** and recorded in Section 3: a free OpenRouter model was wired up, graded against the same 43 merchants and rejected at 60% correct / 16% wrong against Flash's 77% / 9%. The swap mechanism works; that particular model did not, which is the distinction this decision rests on.

---

## 10. Known weak points

Stated honestly, because they will bite:

- **A layout change breaks the parser silently.** Mitigation: the gate catches it as a reconciliation failure rather than as wrong data, and `rows_expected` vs `rows_parsed` catches the subtler case where rows vanish but the remainder still balances. There is no LLM fallback to fall back to — if a new issuer defeats the parser, you write code, and the gate tells you when you are done.
- **`unverified` is where wrong data hides.** Both real bugs in Phase 0 were sitting under it: a statement whose totals can't be found looks exactly like a statement whose parse is wrong. Trust reported `unverified` while over-counting debits by 1838.00. Treat every `unverified` as a defect to chase down, and show it in the report as an honest gap rather than a quiet zero.
- **Two of five issuers hide their summary figures** in a horizontal grid with labels stacked above the numbers. A sixth bank will find a sixth way. Budget for summary-label work per new issuer even though row parsing generalizes well.
- **OCR on a bad scan will produce wrong digits**, and `8`/`3` and `1`/`7` confusions survive plausibility checks. Reconciliation catches most; force manual review on any OCR'd statement that doesn't reconcile exactly.
- **Cash spending is invisible.** The app can never be a complete picture of spending, only of card spending. Don't let the UI imply otherwise.
- **Refund/original matching across statement boundaries** (buy in June, refund in August) will misstate both months. v1: net within the month only, and note it.
