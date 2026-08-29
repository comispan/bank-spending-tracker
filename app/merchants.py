"""Merchant normalization: one statement description -> one stable merchant key.

Why this exists: tier 2 of the categorizer (DESIGN.md §3) is a lookup from a
merchant key to the category the user chose last time, and it is what makes the
app feel like it learns. It covers nothing at all unless
`GRAB *TRIP 4821 SINGAPORE SG` and `GRAB *TRIP 9903 SINGAPORE SG` arrive at the
same key.

The job is stability, not tidiness. A key only has to be

  - the same every month for the same merchant, and
  - different for different merchants.

`bus/mrt` is a good key. `uniqlo ion orchard` is a good key even though
`uniqlo` reads better — that outlet does not move between statements.
`grab trip 4821` is a bad key, because next month invents 9903 and the user is
asked about Grab all over again.

The shape of the rule: **the merchant is at the front, and the junk starts
somewhere.** Every issuer in the corpus prints merchant first, then location,
then its own reference numbers — 59 rows of one statement are
`NAME NAME NAME SINGAPORE 065`, another 59 are `BUS/MRT 870632419 SINGAPORE`.
Cutting at the first token that cannot be part of a name generalizes better
than enumerating suffixes, because the suffixes are per-issuer and the names
are not. Same reasoning as §2.2 parsing rows by shape rather than by bank.

A star is the one place that shape reverses — `GATEWAY*THE REAL SHOP` puts the
merchant on the right — so `_resolve_star` reads both sides before choosing.
It is read by shape for the same reason: a list of known gateways only ever
names the ones already met, and the one it has not met yet is the one that
merges two shops into a single key.

Two keys come out of here, and tier 2 should try them in this order:

    normalize("STARBUCKS @ RAFFLES CITY SG")  -> "starbucks raffles city"
    merchant_root("starbucks raffles city")   -> "starbucks"

The precise key first, so categorizing one outlet means that outlet; the root
second, so a merchant that prints itself two ways still resolves. This is why
normalization stops short of reducing everything to one word: the one-word form
is a *fallback*, and collapsing to it eagerly would silently merge
`royal plaza` with `royal sporting house`. Confidently wrong is the failure this
project keeps refusing to ship.
"""

from __future__ import annotations

import re

# Location tokens, only ever stripped from the *end*: `SINGAPORE AIRLINES`
# opens with one and is not a location.
CITY_TOKENS = {"singapore", "s'pore", "spore", "sgp"}

# Trailing country codes, as a curated list rather than "any two letters" —
# `M1`, `A&W` and `H&M` are two-ish characters and are merchants.
COUNTRY_TOKENS = {
    "sg", "my", "id", "th", "vn", "ph", "hk", "cn", "tw", "jp", "kr", "in",
    "au", "nz", "us", "ca", "gb", "uk", "ie", "fr", "de", "nl", "it", "es",
    "ch", "se", "dk", "no", "ae", "sa", "za", "br",
}

# Payment processors that print themselves in front of the real merchant, so
# the merchant is what *follows* the star. Deliberately short, and no longer
# the only way a gateway is recognized — `_resolve_star` also reads the shape
# of both sides, because this list can only ever name the gateways already met.
PROCESSOR_PREFIXES = {"sq", "paypal", "pp", "pypl", "stripe"}

# A domain-shaped first token names the merchant outright, and everything after
# it is contact detail: `NETFLIX.COM 866-579-7172 SG`. Known TLDs only, so
# `MR.DIY` is not read as a merchant called `mr`.
TLDS = {"com", "net", "org", "sg", "co", "io", "app", "tv", "ai",
        "shop", "store", "biz", "info", "my", "uk"}
DOMAIN = re.compile(r"^(?:www\.)?(?P<name>[a-z0-9][a-z0-9&'-]*)\.(?P<tld>[a-z]{2,})")

# A refund prints as the merchant plus a marker. It has to reduce to the same
# key as the purchase, or §3's "net refunds against the original merchant" has
# nothing to net against.
KIND_SUFFIX = re.compile(
    r"(?i)\s*[-–—]?\s*\b(?:refund|refunded|reversal|reversed|chargeback|credit\s+voucher)\b\s*$"
)

# The issuer's abbreviation and the merchant's own name, in one description:
# `AMZN Mktp SG*RT4G91 AMAZON.SG`. Without this that single row is two keys.
# This is not a merchant directory — learning which name means which category is
# tier 2's job. Add an entry only when a real statement prints one merchant two
# ways, and note which statement.
ALIASES = {
    "amzn": "amazon",
    "amzn mktp": "amazon",
}

PUNCT_ONLY = re.compile(r"^[^a-z0-9]+$")
EDGE_PUNCT = " -&/.'\"*,:;#@+_"


def normalize(description: str) -> str:
    """A statement description -> the merchant key stored on the transaction."""
    text = (description or "").strip().lower()
    if not text:
        return ""

    text = KIND_SUFFIX.sub("", text)
    text = _resolve_star(text)

    tokens = [t for t in text.split() if not PUNCT_ONLY.match(t)]

    domain = _domain_name(tokens)
    if domain:
        return domain

    # Cut first, strip places second, and in that order: 62 rows in the corpus
    # end `... SINGAPORE 065`, where the place is not at the end until the
    # reference has been cut off it.
    key = " ".join(_drop_trailing_place(_cut_at_junk(tokens))).strip(EDGE_PUNCT)
    key = re.sub(r"\s+", " ", key)
    key = _alias(key)
    # Never return "" for a description that had content. An empty key is one
    # bucket that every unreadable row falls into and is then categorized
    # together — exactly the silent wrongness the gate in §2.3 exists to stop.
    return key or re.sub(r"\s+", " ", text).strip(EDGE_PUNCT) or description.strip().lower()


def merchant_root(key: str) -> str:
    """The coarse fallback key: the first word of a normalized key.

    Tier 2 looks up `normalize()` first and this second, which is how `grab`
    and `grab trip` reach the same learned category without normalization
    having to guess that `trip` is a service and not part of the name.
    """
    return key.split(" ", 1)[0] if key else ""


def cluster_order(entries: list[dict], weight: str = "weight") -> list[dict]:
    """Order merchant summaries so variants of one merchant sit next to each other.

    Sorting a bulk-categorize list purely by value scatters the two halves of a
    split merchant across the page — the corpus has one merchant sitting under
    both `… ab cd` and `… ab-cd`, nine rows and one, and by value they land
    nowhere near each other. Grouping by root puts them adjacent, and ordering
    the groups by their *combined* weight still leads with the money.

    Note what this deliberately does not do: it groups for display only. Merging
    the two keys would be the eager root-collapse that `normalize()` refuses,
    and would file `royal plaza` and `royal sporting house` as one merchant.
    Sitting next to each other is enough — the person reading can see they are
    the same shop, which is exactly the judgement a machine should not make here.
    """
    totals: dict[str, float] = {}
    for e in entries:
        root = merchant_root(e["key"])
        totals[root] = totals.get(root, 0) + (e.get(weight) or 0)
    return sorted(
        entries,
        key=lambda e: (-totals[merchant_root(e["key"])],
                       merchant_root(e["key"]),
                       -(e.get(weight) or 0),
                       e["key"]),
    )


# ------------------------------------------------------------------ pieces

def _resolve_star(text: str) -> str:
    """Split `MERCHANT*reference` / `GATEWAY*MERCHANT` at the first star.

    Both shapes are common and they mean opposite things, so the side to keep
    has to be *read* rather than assumed. Keeping the left is still the default
    — of the 16 starred rows in the corpus 11 name the merchant there — but a
    flat default is what put `SMP**Li Xin Fish Ball` and `SMP**OLD TEA HUT`
    under one key. Two merchants merged into one key is wrong data, quietly,
    and it is the same failure the two-key scheme in this module's docstring
    exists to prevent, arriving from the other direction.

    Inverting the default is not the fix; it trades four wrong keys for eight.
    `GRAB *TRIP 4821` becomes `trip`, `SIMBATELECOM****2269` becomes `2269`
    and `AMZN Mktp SG*RT4G91` becomes `rt4g91 amazon.sg` — all keys that change
    next month, which is worse than a coarse one that does not.

    What actually separates them is shape, on both sides at once:

      - a gateway is a *code*, not a name — `SMP`, `GRB`, `2C2`, against
        `GRAB`, `SIMBATELECOM`, `AMZN MKTP`; and
      - a gateway hands over the *whole shop name*, two words or more, where a
        merchant starring its own reference gives one word at most before the
        digits start.

    Both must hold before the right side wins, which is deliberately strict:
    the corpus gateways hand over three or four name words, so there is margin,
    and a false flip costs a receipt-number key. `PROCESSOR_PREFIXES` still
    overrides the pair, because a known gateway may hand over a single word.
    """
    before, sep, after = text.partition("*")
    if not sep:
        return text
    left = _drop_trailing_place(before.split())
    if not left or " ".join(left) in PROCESSOR_PREFIXES:
        return after.strip() or before.strip()
    if _is_gateway_code(left) and _names_a_merchant(after):
        return after.strip()
    return " ".join(left)


def _is_gateway_code(left: list[str]) -> bool:
    """Is what precedes the star a code rather than somebody's name?

    One token, and either no longer than three characters or carrying a digit.
    That covers the gateways in the corpus — `SMP`, `GRB`, `2C2` — while
    `GRAB` (four letters), `SIMBATELECOM` and the two-token `AMZN MKTP` are
    names and fail it, which is what keeps them on the left where they belong.
    """
    if len(left) != 1:
        return False
    token = left[0].strip(EDGE_PUNCT)
    return bool(token) and (len(token) <= 3 or any(c.isdigit() for c in token))


def _names_a_merchant(after: str) -> bool:
    """Does what follows the star open with a shop name, not a reference?

    Two name-shaped tokens before the first junk one or the first place. A
    gateway prints the whole name — `Li Xin Fish Ball`, `Dunkin Donuts Paya` —
    where a merchant that stars its own reference gets one word at the very
    most: `GRAB *TRIP 4821` has `trip` and then numbers.

    A place counts as the end of the name rather than part of it, so
    `XX*Uniqlo SG` stays on the left. That is the conservative call and it is
    revisitable — no row in the corpus takes that shape, and the reason to
    prefer it today is that `M1*Data SG` looks identical and `M1` is a real
    Singapore merchant.
    """
    names = 0
    for token in after.split():
        token = token.strip(EDGE_PUNCT)
        if not token or PUNCT_ONLY.match(token):
            continue
        if _is_junk(token) or token in CITY_TOKENS | COUNTRY_TOKENS:
            return False
        names += 1
        if names >= 2:
            return True
    return False


def _drop_trailing_place(tokens: list[str]) -> list[str]:
    """Strip city and country tokens off the end, never off the front.

    Keeps the last token no matter what: a description that is *only* a place
    name still has to key on something, and `sg` is at least stable.
    """
    out = list(tokens)
    while len(out) > 1 and out[-1].strip(EDGE_PUNCT) in CITY_TOKENS | COUNTRY_TOKENS:
        out.pop()
    return out


def _domain_name(tokens: list[str]) -> str:
    if not tokens:
        return ""
    m = DOMAIN.match(tokens[0])
    if m and m.group("tld") in TLDS:
        return _alias(m.group("name"))
    return ""


def _cut_at_junk(tokens: list[str]) -> list[str]:
    """From the first junk token on it is the issuer talking, not the merchant.

    Never cuts at position 0. Eleven rows in the corpus open with a token that
    contains a digit — `ZERO1 PTE LTD` is a merchant, not a reference — and
    cutting there would leave nothing at all.
    """
    for i, token in enumerate(tokens):
        if i and _is_junk(token):
            return tokens[:i]
    return tokens


def _is_junk(token: str) -> bool:
    """A store number, terminal id, phone number, date or reference.

    The thresholds let short numbers through: `HOTEL 81` and `COLD STORAGE 88`
    are names, and an outlet number is stable anyway, so keeping it costs a
    per-outlet key and nothing else. Three digits is where references start
    (`065`, `203`, `870632419`).
    """
    digits = sum(c.isdigit() for c in token)
    if not digits:
        return False
    if not any(c.isalpha() for c in token):
        return digits >= 3
    # Mixed letters and digits: `RT4G91`, `TID12345`, `27JUN`. Short ones are
    # names — `M1`, `A1`, `4E`.
    return len(token) >= 5


def _alias(key: str) -> str:
    """Longest-prefix alias, so `amzn mktp pte ltd` resolves like `amzn mktp`."""
    tokens = key.split()
    for n in range(len(tokens), 0, -1):
        hit = ALIASES.get(" ".join(tokens[:n]))
        if hit:
            return " ".join([hit] + tokens[n:])
    return key
