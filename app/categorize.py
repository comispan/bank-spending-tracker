"""Tiers 1 and 2 of the categorizer, and the flow_type axis (DESIGN.md Section 3).

Pure functions over plain data — no database, no network — so the whole
resolution order is testable in the spike harness the same way `rows.py` is.
`db.py` supplies the rules and the memory map and writes the results back.

**Tier 3 is deliberately absent.** Everything here is free, offline and exact,
and it is worth seeing how far that gets before anything is sent anywhere. The
resolver returns `None` for a merchant nothing knows, and the UI shows that as
an honest gap rather than a guess — which is also the shape tier 3 will slot
into when it arrives: it fills the `None`s and nothing else.

Two axes, and they are not the same question:

  category   what the money was spent on           Groceries, Transport, …
  flow_type  whether it was spending at all        spend, refund, transfer, fee, income

Section 3 keeps them separate because a card payment is not a category of
spending — it is the same money moving, and counting it as spend double-counts
against the bank statement that also shows it. Reports sum `spend` and net out
`refund`.
"""

from __future__ import annotations

import re

from merchants import merchant_root

# Fixed in v1, per Section 3: a large taxonomy makes both the user and a model
# worse at choosing. User-defined categories are v1.1.
CATEGORIES = [
    "Groceries", "Dining", "Transport", "Shopping", "Bills & Utilities",
    "Health", "Entertainment", "Travel", "Education", "Fees & Interest",
    "Cash & Transfers", "Income/Refunds", "Other",
]

FLOW_TYPES = ["spend", "refund", "transfer", "fee", "income"]

# Where a flow_type already answers the category question. A refund is
# deliberately absent: Section 3 nets refunds against the *original merchant*,
# and normalization already keys `UNIQLO … - REFUND` to the same merchant as
# the purchase, so the refund inherits that merchant's category instead of
# being filed under a category of its own.
CATEGORY_FOR_FLOW = {
    "transfer": "Cash & Transfers",
    "fee": "Fees & Interest",
    "income": "Income/Refunds",
}

# ---------------------------------------------------------------- flow_type
#
# The bias throughout is toward `spend`. Under-classifying leaves a row in the
# spend total where the user can see it and fix it; over-classifying silently
# removes real spending from the report, and a total that is quietly too low is
# indistinguishable from a good month. So only unambiguous non-spend is
# reclassified, and phrases are matched rather than words:
#
#   - `giro` is not here. A GIRO to a utility is a real bill; only a GIRO to a
#     card is a transfer, and the statement rarely says which.
#   - `paynow` is not here either. PayNow to a hawker is lunch. The rows that
#     matter (`PAYNOW TRANSFER …`) are caught by `transfer to/from` instead.
#   - `top-up` is not here. Topping up a transit card is transport spending.
#
# Order matters: fees are tested before transfers, or `LATE PAYMENT CHARGE`
# reads as a card payment.
FLOW_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fee", re.compile(r"""(?ix)
        \b(?: interest \s+ (?:charge|on|charged)
            | finance \s+ charge
            | late \s+ payment \s+ (?:charge|fee)
            | annual \s+ (?:fee|membership)
            | service \s+ (?:charge|fee)
            | cash \s+ advance \s+ fee
            | (?:foreign|overseas) \s+ (?:transaction|currency) \s+ fee
            | over(?:\s|-)?limit \s+ (?:charge|fee)
            | admin(?:istrative)? \s+ fee
            | card \s+ replacement
            | minimum \s+ charge
            | \bgst\b
        )""")),
    ("transfer", re.compile(r"""(?ix)
        (?: payment \s* -? \s* thank \s+ you
          | payment \s+ received
          | (?:credit \s+ )? card \s+ payment
          | \bccrd\b
          | balance \s+ transfer
          | funds? \s+ transfer
          | \btransfer \s+ (?:to|from)\b
          | payment \s+ via \b
          | \bautopay \s+ (?:to \s+ )?card\b
        )""")),
    ("refund", re.compile(r"""(?ix)
        \b(?: refund(?:ed)? | reversal | reversed | chargeback
            | credit \s+ voucher | goods \s+ returned
        )\b""")),
    ("income", re.compile(r"""(?ix)
        \b(?: cash \s? back | rebate | rewards? \s+ (?:redemption|credit)
            | interest \s+ earned | salary | payroll | dividend
        )\b""")),
]


def default_flow(description: str, direction: str) -> str:
    """What kind of money movement this row is, before anyone overrides it."""
    text = description or ""
    for flow, pattern in FLOW_PATTERNS:
        if pattern.search(text):
            # A refund is a credit by definition; the same words on a debit are
            # the merchant's name ("REFUND SPECIALISTS PTE LTD") or a fee being
            # charged, not money coming back.
            if flow in ("refund", "income") and direction != "credit":
                continue
            return flow
    if direction == "credit":
        # An unrecognized credit: cashback under a name we don't know, a
        # transfer in, something else. `income` leaves the spend total exactly
        # where it was; calling it a refund would net it against spending and
        # under-report the month, which is the worse of the two errors for a
        # spending tracker. It shows in the UI either way.
        return "income"
    return "spend"


# -------------------------------------------------------------------- tiers

def match_rule(rule, description: str, merchant: str) -> bool:
    """Does a user rule (tier 1) cover this row?

    Matched against both the merchant key and what the statement actually
    printed, because either is a reasonable thing for a person to write a rule
    about — the key is what the app shows, the raw line is what they remember
    seeing on the statement.
    """
    pattern = (rule["pattern"] or "").strip().lower()
    if not pattern:
        return False
    haystacks = [(merchant or "").lower(), (description or "").lower()]
    kind = rule["match_type"]
    if kind == "exact":
        return any(pattern == h for h in haystacks)
    if kind == "contains":
        return any(pattern in h for h in haystacks)
    if kind == "regex":
        try:
            return any(re.search(pattern, h) for h in haystacks)
        except re.error:
            # A rule that no longer compiles must not take the whole page down
            # with it. It matches nothing until the user fixes it, and
            # valid_regex() below stops most of them being saved at all.
            return False
    return False


def valid_regex(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def resolve(description: str, merchant: str, direction: str,
            rules: list, memory: dict) -> tuple[str | None, str, str | None]:
    """Resolve one row to (category, flow_type, source).

    Cheapest tier first, and the first hit wins:

      1. a user rule            always wins, that is what makes it tier 1
      2. learned memory         the merchant key, then its root (Section 3)
      3. the flow_type itself   a card payment needs no merchant lookup
      -  nothing                category None, an honest gap for tier 3 or the user

    `rules` must arrive already ordered by priority. A rule that names no
    flow_type leaves the derived one alone, and memory never carries one at
    all: "this merchant is Groceries" says nothing about whether a particular
    row was a purchase or a refund, and Section 5 keeps flow_type off
    merchant_memory for exactly that reason.
    """
    flow = default_flow(description, direction)

    for rule in rules:
        if match_rule(rule, description, merchant):
            return rule["category"], rule["flow_type"] or flow, "rule"

    # The precise key first, its root second. Section 3: `grab` and `grab trip`
    # are one merchant, and the root is what reunites them without
    # normalization having to guess that `trip` is a service word.
    for key in (merchant, merchant_root(merchant or "")):
        hit = memory.get(key) if key else None
        if hit:
            return hit["category"], flow, hit.get("source", "memory")

    implied = CATEGORY_FOR_FLOW.get(flow)
    if implied:
        return implied, flow, "flow"

    return None, flow, None


# --------------------------------------------------------------------- seed
#
# A starting guess so tier 2 is not empty on day one, stored with source
# `seed` so it is never mistaken for something the user taught the app — the
# UI marks seeded rows differently, and any correction overwrites the seed
# permanently.
#
# Keyed at root level, which is where `merchant_root()` fallback finds them:
# `fairprice` covers `fairprice finest` and `fairprice xpress` alike.
#
# Deliberately excluded: anything genuinely ambiguous. `apple` is subscriptions
# on one row and a laptop on the next, and both normalize to the same key —
# guessing there would file real purchases under a category the user never
# chose, which is the failure mode this list exists to avoid, not to cause.
SEED_MEMORY: dict[str, str] = {
    # Groceries
    "fairprice": "Groceries", "ntuc": "Groceries", "cold storage": "Groceries",
    "giant": "Groceries", "sheng siong": "Groceries", "don don donki": "Groceries",
    "donki": "Groceries", "prime supermarket": "Groceries", "redmart": "Groceries",
    "hao mart": "Groceries", "cs fresh": "Groceries", "little farms": "Groceries",
    # Dining
    "mcdonald": "Dining", "mcdonalds": "Dining", "kfc": "Dining", "subway": "Dining",
    "starbucks": "Dining", "coffee bean": "Dining", "ya kun": "Dining",
    "toast box": "Dining", "din tai fung": "Dining", "jollibee": "Dining",
    "pizza hut": "Dining", "burger king": "Dining", "foodpanda": "Dining",
    "deliveroo": "Dining", "grabfood": "Dining", "chagee": "Dining",
    "liho": "Dining", "koi": "Dining",
    # Transport
    "grab": "Transport", "gojek": "Transport", "tada": "Transport",
    "ryde": "Transport", "comfortdelgro": "Transport", "bus/mrt": "Transport",
    "ez-link": "Transport", "ezlink": "Transport", "simplygo": "Transport",
    "shell": "Transport", "esso": "Transport", "caltex": "Transport",
    "sinopec": "Transport", "getgo": "Transport", "bluesg": "Transport",
    # Bills & Utilities
    "sp digital": "Bills & Utilities", "sp services": "Bills & Utilities",
    "sp group": "Bills & Utilities", "singtel": "Bills & Utilities",
    "starhub": "Bills & Utilities", "m1": "Bills & Utilities",
    "simba": "Bills & Utilities", "simbatelecom": "Bills & Utilities",
    "myrepublic": "Bills & Utilities", "city energy": "Bills & Utilities",
    "geneco": "Bills & Utilities", "senoko": "Bills & Utilities",
    # Health
    "watsons": "Health", "guardian": "Health", "unity": "Health",
    "raffles medical": "Health", "healthway": "Health", "parkway": "Health",
    # Entertainment
    "netflix": "Entertainment", "spotify": "Entertainment", "disney": "Entertainment",
    "steam": "Entertainment", "playstation": "Entertainment", "nintendo": "Entertainment",
    "golden village": "Entertainment", "shaw": "Entertainment", "cathay": "Entertainment",
    # Shopping
    "lazada": "Shopping", "shopee": "Shopping", "qoo10": "Shopping",
    "amazon": "Shopping", "taobao": "Shopping", "uniqlo": "Shopping",
    "zara": "Shopping", "muji": "Shopping", "ikea": "Shopping",
    "decathlon": "Shopping", "challenger": "Shopping", "courts": "Shopping",
    "harvey norman": "Shopping", "popular": "Shopping", "kinokuniya": "Shopping",
    "daiso": "Shopping",
    # Travel
    "agoda": "Travel", "booking": "Travel", "airbnb": "Travel", "klook": "Travel",
    "expedia": "Travel", "trip": "Travel", "singapore airlines": "Travel",
    "scoot": "Travel", "jetstar": "Travel", "changi": "Travel",
    # Education
    "udemy": "Education", "coursera": "Education", "skillsfuture": "Education",
}
