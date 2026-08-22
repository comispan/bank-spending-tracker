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

**This is now the only place a model appears in the app.** Extraction (§2.2) sends nothing anywhere, so the disclosure question from §9 lands here and only here — and it is a much smaller question: tier 3 sends a *list of normalized merchant names* (`grab`, `fairprice`, `netflix`), not statement text. No amounts, no dates, no balances, no card numbers. A local model is entirely adequate for this shape of task if you would rather it stayed offline; classifying a short string is what small models are good at, which is precisely what Phase 0 found.

**Merchant normalization** is what makes tier 2 work. `GRAB *TRIP 4821 SINGAPORE`, `GRAB* TRIP 9903`, and `Grab Trip SG` must all collapse to `grab`. Strip: trailing numerics, `*` segments, city/country suffixes, POS terminal IDs, dates embedded in the description, `SQ *`/`PAYPAL *`/`AMZN Mktp` style processor prefixes (keep what follows). Lowercase, squash whitespace. Store both `description_raw` and `merchant_normalized`.

> **Built 2026-08-22** as `app/merchants.py`. The rule that generalized is *the merchant is at the front, and the junk starts somewhere*: cut at the first token that cannot be part of a name, then strip the place off what is left — in that order, because 62 rows of one statement end `… SINGAPORE 065` and the place is not last until the reference has been cut off it. Enumerating suffixes per issuer was not needed, for the same reason §2.2 parses rows by shape rather than by bank. On the real corpus: 195 distinct descriptions → 112 keys, none empty, and normalizing a key never moves it.
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
- **PDF text**: `pdfplumber`. Its word/layout primitives are stronger than anything in JS, which is what made §2.2 possible.
- **No queue, no worker.** Parsing measured 182 ms/page — a 4-page statement is under a second, a 20-page one about 3.6s. Parse inside the request with a spinner. `pg-boss`, job status polling, and the whole async subsystem in the original design existed to hide a 10–60s LLM round-trip that no longer happens.
- **Local files, not object storage.** Single-user and self-hosted: PDFs go in a directory the app owns. No buckets, no signed URLs, no lifecycle rules.
- **Categorization LLM** (§3, tier 3 only): Anthropic `claude-sonnet-5`, or a local model — it classifies short merchant strings, which is well within a small model.
- **OCR fallback**: deferred to Phase 4. No statement in the Phase 0 set needed it.
- **Charts**: server-rendered SVG, or Observable Plot if you want interactivity.

---

## 7. Privacy & security

This app holds a complete record of someone's spending. Treat that seriously — it's a category of data where a leak is genuinely damaging.

Single-user and self-hosted removes most of the original surface here — there is no tenant boundary to breach, no bucket to misconfigure, and no `user_id` to forget in a `WHERE` clause. What remains still matters:

- **Extraction discloses nothing.** No API key, no provider, no data terms to read. This was the largest item on this list and Phase 0 deleted it.
- Statement passwords: use to decrypt in memory, **never store**.
- The only outbound call is categorization (§3, tier 3), and only normalized merchant names — no amounts, dates, or balances. Say so plainly in the UI. Use a provider with no-training-on-inputs terms, or run it locally.
- Full card numbers are masked to last-4 at parse time (`redact()`), before anything is stored or written to disk.
- Don't bind the server to `0.0.0.0`. Localhost only unless you have deliberately decided otherwise.
- Offer hard delete: purge PDFs, transactions, and derived data.
- Don't log statement text or transaction descriptions.
- No third-party analytics on any page that renders financial data.

---

## 8. Build plan

Each phase ends with something you can actually use.

**Phase 0 — Spike. ✅ Done 2026-08-19.**
Six statements from five Singapore issuers (DBS, MariBank, Standard Chartered, Trust, UOB) plus a synthetic control. **6/6 reconcile.** Extraction needs no model; see §2.2 and [spike/README.md](spike/README.md). The answer is "easy" — the remaining risk is in the CRUD, which is the good outcome.

**Phase 1 — Single statement, end to end (~3–4 days, revised down).**
Upload → parse → verify → transactions table. One card, no categories yet. Ship the "needs review" state now, not later.

Smaller than originally planned, because Phase 0 removed the queue, the worker, the job-status polling, the object storage, and the auth layer. What's actually left:

1. Lift `spike/rows.py` in unchanged. It is the proven component — don't rewrite it while porting.
2. SQLite schema per §5, plus the migration discipline to add to it later.
3. Upload form → save PDF to a local directory → SHA-256 → parse → persist. Synchronous.
4. The gate (§2.3) writing `verdict` onto the statement, and a **statement list that shows it**. A `pass` you can't see is worth nothing.
5. Needs-review screen: parsed rows beside the source page. This is where a `fail` or `unverified` gets resolved, and Phase 0 proved you will use it.
6. Dedup on `file_sha256` at minimum — re-uploading the same PDF must be caught. Full transaction-level dedup can wait for Phase 3, but note that UOB already has two genuinely identical same-day rows, so **never dedup silently within a single statement**.

**Phase 2 — Categorization (~1 week).**
Three-tier resolver, merchant normalization, inline recategorize with "apply to all matching." Seed merchant memory with a few hundred common merchants.

**Phase 3 — Consolidation & report (~1 week).**
Multiple accounts, dedup, calendar-month bucketing, the monthly report page with drill-through. This is the point where the app becomes the thing you described.

**Phase 4 — Hardening (ongoing).**
OCR path for scanned statements, CSV/Excel export, replay-old-statements-with-new-parser tooling, issuer-specific summary labels as new banks arrive. Encrypted-PDF passwords and multi-currency are already handled — Phase 0 shipped the empty-password path (DBS needs it) and §4 settles currency.

**Deliberately deferred:** budgets, forecasting, recurring-subscription detection (nice, and easy once you have clean data — but it's a different product surface), bank API sync, mobile.

---

## 9. Decisions — settled 2026-08-19

1. **Which banks?** DBS, MariBank, Standard Chartered, Trust, UOB. All five parse and reconcile today.
2. **Base currency?** SGD, with each transaction keeping its original amount, currency and the statement's own FX rate. Driven by a real HKD charge in the Trust statement, not a hypothetical.
3. **Single-user or multi-tenant?** **Single-user, self-hosted.** No auth, no RLS, no tenant scoping. Roughly halves the build and removes most of §7.
4. **Is sending statement text to an LLM acceptable?** Moot for extraction — nothing leaves the machine. The question survives only for categorization (§3, tier 3), which sends normalized merchant names and no figures at all. Decide it in Phase 2; a local model is a legitimate answer.

---

## 10. Known weak points

Stated honestly, because they will bite:

- **A layout change breaks the parser silently.** Mitigation: the gate catches it as a reconciliation failure rather than as wrong data, and `rows_expected` vs `rows_parsed` catches the subtler case where rows vanish but the remainder still balances. There is no LLM fallback to fall back to — if a new issuer defeats the parser, you write code, and the gate tells you when you are done.
- **`unverified` is where wrong data hides.** Both real bugs in Phase 0 were sitting under it: a statement whose totals can't be found looks exactly like a statement whose parse is wrong. Trust reported `unverified` while over-counting debits by 1838.00. Treat every `unverified` as a defect to chase down, and show it in the report as an honest gap rather than a quiet zero.
- **Two of five issuers hide their summary figures** in a horizontal grid with labels stacked above the numbers. A sixth bank will find a sixth way. Budget for summary-label work per new issuer even though row parsing generalizes well.
- **OCR on a bad scan will produce wrong digits**, and `8`/`3` and `1`/`7` confusions survive plausibility checks. Reconciliation catches most; force manual review on any OCR'd statement that doesn't reconcile exactly.
- **Cash spending is invisible.** The app can never be a complete picture of spending, only of card spending. Don't let the UI imply otherwise.
- **Refund/original matching across statement boundaries** (buy in June, refund in August) will misstate both months. v1: net within the month only, and note it.
