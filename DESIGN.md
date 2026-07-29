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

A design that assumes "parse the PDF into a table" will fail on statement #2. The design below assumes extraction is **probabilistic and must be verified**.

### 2.1 Extraction pipeline

```
PDF
 │
 ├─ 0. Decrypt (prompt user for password if needed)
 │
 ├─ 1. Text-layer extraction  (pdfjs / pdfplumber → glyphs with x,y,font)
 │       │
 │       └─ if < ~100 chars of text → it's a scan → OCR (Tesseract, or a
 │          vision model) → produces a text layer, continue
 │
 ├─ 2. Issuer fingerprint  (regex on header text: bank name, statement
 │       format markers) → look up a known template
 │       │
 │       ├─ template hit  → deterministic parser (fast, free, exact)
 │       └─ template miss → LLM structured extraction (see 2.2)
 │
 ├─ 3. Normalize → Transaction[] (date, description, amount, sign, currency)
 │
 ├─ 4. VERIFY (see 2.3)  ── fails ──→ quarantine, ask user to review
 │
 └─ 5. Persist + hand to categorizer
```

**Why both a template path and an LLM path.** The LLM path is what makes the app work on day one with any bank. The template path is what makes it cheap, fast, and deterministic once a bank is known. Practically: an LLM parse that verifies cleanly can be used to *propose* a template (the column x-ranges, date format, section markers) that a human confirms once; after that, that issuer is free forever. Start with LLM-only, harvest templates for the top issuers your users actually upload.

### 2.2 LLM extraction

Give the model the page text **with layout preserved** (x-position-aware line reconstruction, not naive `extractText()` which scrambles columns), and ask for strict JSON:

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
- **Page-at-a-time, not whole-document.** Bounds token cost, keeps accuracy high on 20-page statements, and lets transactions that straddle a page break be stitched in step 3.
- **Ask for the summary figures too** (opening/closing/totals). They are the checksum. This is the single highest-leverage line in this design.
- Amounts as decimal strings → parse to integer minor units. Never floats in the DB.
- Model: a mid-tier model (e.g. `claude-sonnet-5`) is right for this; escalate to `claude-opus-5` only on verification failure. Cache the system prompt.

### 2.3 Verification — the trust gate

An extraction is **accepted** only if it self-reconciles:

1. `sum(debits) ≈ total_debits` and `sum(credits) ≈ total_credits` (exact to the cent), **or**
2. `opening_balance + debits − credits ≈ closing_balance`, **or**
3. If the statement prints no summary figures at all → mark `confidence: unverified`.

Also check: every transaction date falls inside the statement period (±5 days for posting lag); no duplicate (date, amount, description) triples within one statement; transaction count is plausible for the page count.

On failure: **do not silently import.** Retry once with a stronger model; if it still fails, put the statement in a "Needs review" state and show the user a side-by-side of the PDF page and the parsed rows so they can fix it in a few clicks. Silently-wrong financial data is far worse than an honest "I couldn't read this one."

This gate is what makes the app trustworthy. Build it in the same PR as the parser, not later.

---

## 3. Categorization

Three tiers, cheapest first. Each transaction is resolved by the first tier that hits.

| Tier | Mechanism | Cost | Covers after warm-up |
|---|---|---|---|
| 1 | **User rules** — merchant pattern → category, set by the user, always wins | free | ~10% |
| 2 | **Merchant memory** — normalized merchant string → category learned from every prior decision (the user's own + a shared seed list) | free, one index lookup | ~75% |
| 3 | **LLM** — batch of unknown merchants → category, one call for the whole batch | ~cents | the rest |

**Merchant normalization** is what makes tier 2 work. `GRAB *TRIP 4821 SINGAPORE`, `GRAB* TRIP 9903`, and `Grab Trip SG` must all collapse to `grab`. Strip: trailing numerics, `*` segments, city/country suffixes, POS terminal IDs, dates embedded in the description, `SQ *`/`PAYPAL *`/`AMZN Mktp` style processor prefixes (keep what follows). Lowercase, squash whitespace. Store both `description_raw` and `merchant_normalized`.

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

**Multi-currency.** If any statement is not in the user's base currency, store the original amount + currency + the FX rate the *statement itself* printed (banks include it). Convert at that rate, not at today's rate. If no rate is printed, fall back to a daily rate table and mark the figure as approximate.

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

```
user            id, email, base_currency
account         id, user_id, issuer, kind(credit|debit|bank), last4, nickname, currency
statement       id, user_id, account_id, file_sha256, storage_key, period_start,
                period_end, opening_balance, closing_balance, page_count,
                parser(template|llm), parser_version, status(parsed|needs_review|failed),
                confidence, raw_extraction jsonb
transaction     id, user_id, account_id, statement_id,
                txn_date, posted_date,
                description_raw, merchant_normalized,
                amount_minor bigint, currency, amount_base_minor bigint, fx_rate,
                direction(debit|credit), flow_type(spend|refund|transfer|fee|income),
                category, category_source(rule|memory|llm|user), category_confidence,
                source_page, source_bbox,
                dedup_key, duplicate_of_id nullable
merchant_rule   id, user_id nullable, pattern, match_type(exact|contains|regex),
                category, flow_type, priority
merchant_memory user_id, merchant_normalized, category, hit_count, updated_at
issuer_template id, fingerprint_regex, layout jsonb, version, verified_by
```

Conventions worth locking in now: **money is `bigint` minor units, never float.** Dates are plain `DATE` (a transaction date has no timezone). `raw_extraction` is kept so a parser improvement can be replayed over old statements without asking users to re-upload — this will save you at least once.

---

## 6. Stack

Recommended, optimizing for "one person can build and run this":

- **Next.js (TypeScript), App Router** — one deployable, server actions for the CRUD, React for the report UI.
- **Postgres** (Supabase/Neon) + **Prisma**. The queries here are grouped sums; Postgres is more than enough.
- **Object storage** for the PDFs (S3/R2/Supabase Storage), private buckets, signed URLs only.
- **Extraction worker**: a queue (pg-boss on the same Postgres, or Inngest/Trigger.dev) — parsing a 20-page statement takes 10–60s, which must not happen in a request handler. Upload returns immediately; UI polls or subscribes for status.
- **PDF text**: `pdfjs-dist` (Node) or **`pdfplumber` (Python)** if you want the better layout primitives — a small Python extraction service is a legitimate choice here, and `pdfplumber`'s word/table tooling is genuinely stronger than anything in JS.
- **OCR fallback**: Tesseract, or send page images to a vision model (simpler, better on statements, costs money).
- **LLM**: Anthropic API. `claude-sonnet-5` for extraction and categorization, `claude-opus-5` on retry.
- **Charts**: Recharts or Observable Plot.

If you'd rather do the whole thing in Python: FastAPI + pdfplumber + HTMX/Jinja is a smaller, very defensible v1.

---

## 7. Privacy & security

This app holds a complete record of someone's spending. Treat that seriously — it's a category of data where a leak is genuinely damaging.

- Encrypt PDFs at rest; scope every storage key to the user; signed URLs with short TTLs. Never a public bucket.
- **Every query filters by `user_id`.** Enforce it at the DB layer (RLS) so a missing `WHERE` in one handler isn't a full data breach.
- Statement passwords: use to decrypt in memory, **never store**.
- Sending statement text to an LLM API is a real disclosure — say so plainly in the UI, and use a provider with no-training-on-inputs terms. Redact full card numbers before the call (you only ever need last-4).
- Offer hard delete: purge PDFs, transactions, and derived data.
- Don't log statement text or transaction descriptions.
- No third-party analytics on any page that renders financial data.

---

## 8. Build plan

Each phase ends with something you can actually use.

**Phase 0 — Spike (1–2 days). Do this before committing to anything above.**
Collect 5–10 *real* statements from the banks you actually use. Write a throwaway script: text-extract → LLM → JSON → check the totals reconcile. No UI, no DB.
This tells you within two days whether the product is easy or hard, and which bank breaks it. If reconciliation passes on most of them, the rest of this plan is straightforward engineering. **Every downstream decision should wait on this result.**

**Phase 1 — Single statement, end to end (~1 week).**
Upload → store → parse job → verify → transactions table in the UI. One card, no categories yet. Ship the "needs review" state now, not later.

**Phase 2 — Categorization (~1 week).**
Three-tier resolver, merchant normalization, inline recategorize with "apply to all matching." Seed merchant memory with a few hundred common merchants.

**Phase 3 — Consolidation & report (~1 week).**
Multiple accounts, dedup, calendar-month bucketing, the monthly report page with drill-through. This is the point where the app becomes the thing you described.

**Phase 4 — Hardening (ongoing).**
Encrypted-PDF passwords, OCR path, issuer templates for your top banks, CSV/Excel export, multi-currency, replay-old-statements-with-new-parser tooling.

**Deliberately deferred:** budgets, forecasting, recurring-subscription detection (nice, and easy once you have clean data — but it's a different product surface), bank API sync, mobile.

---

## 9. Decisions you should make before Phase 1

1. **Which banks?** Get the real statement set. This drives everything.
2. **Base currency** — single-currency v1 is meaningfully simpler; take it if you can.
3. **Self-hosted / single-user, or a real multi-tenant product?** Single-user lets you skip auth, RLS, and most of §7 and cuts the build roughly in half. If it's just for you, say so and the plan gets much shorter.
4. **Is sending statement text to an LLM acceptable to you?** If not, the design changes substantially — it becomes template-parsers-only, which means real per-bank work up front and no zero-shot support for a new bank.

---

## 10. Known weak points

Stated honestly, because they will bite:

- **Layout changes break template parsers silently.** Mitigation: the verification gate catches it as a reconciliation failure rather than as wrong data. Keep the LLM fallback live even for templated issuers.
- **Statements without printed totals can't be verified.** Some don't have them. Those imports are `unverified` and should be visually marked as such in the report.
- **OCR on a bad scan will produce wrong digits**, and `8`/`3` and `1`/`7` confusions survive plausibility checks. Reconciliation catches most; force manual review on any OCR'd statement that doesn't reconcile exactly.
- **Cash spending is invisible.** The app can never be a complete picture of spending, only of card spending. Don't let the UI imply otherwise.
- **Refund/original matching across statement boundaries** (buy in June, refund in August) will misstate both months. v1: net within the month only, and note it.
