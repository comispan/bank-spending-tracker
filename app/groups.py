"""Merchant groups: many outlet keys, one company — for reporting only.

`merchants.py` refuses to collapse `sheng siong ss jl` and `sheng siong
supermarket` into one key, and it is right to. A key is what tier 2 remembers a
category against, so merging two keys that turn out to be different shops files
real rows under the wrong merchant, silently. That module's own docstring names
the shape of the accident: `royal plaza` and `royal sporting house` share a
word and nothing else.

But the report has the opposite problem. Nine McDonald's outlets are nine rows
in "Top merchants", none of them large, and the fact that McDonald's is one of
the biggest lines in the year is invisible. Both things are true at once, which
is why grouping lives *above* the key rather than inside it:

    txn.merchant_normalized   the key. Never changes. Tier 2, rules and
                              memory keep working exactly as they did.
    merchant_group            key -> company name. Reporting labels only.

Nothing here can misfile a transaction. The worst a wrong group can do is add
two numbers together on a page that also lets you expand the row and see which
two.

**Suggestions, not merges.** `suggest()` proposes; the user confirms on
/merchants/groups; only a confirmed group is ever stored. That is the same
stance `merchants.cluster_order` takes when it sits variants next to each other
without merging them — the person reading can see that two lines are one shop,
and that judgement is not a machine's to make unassisted.

What the proposal is made of: keys that share a *leading name*, not merely a
leading word. Two tokens is enough on its own — `paris baguette -ctp` and
`paris baguette -plq` are not going to be different companies. One token is
only enough when something else confirms it:

  - the company also bills under the bare name (`grab` alongside `grab rides-ec
    petaling`, `decathlon` alongside `decathlon singapore pte`), or
  - every remainder is an outlet marker rather than a word — `mcdonald's (apm)`,
    `mcdonald's (bdml)`, bracketed or hyphen-led or a bare number.

`royal plaza` / `royal sporting house` fails all three: one shared token, no
bare `royal`, and `plaza` and `sporting` are ordinary name words. It is not
proposed, which is the test this rule exists to pass.
"""

from __future__ import annotations

from collections.abc import Iterable

# An outlet marker is punctuation-led or numeric: `(apm)`, `-ctp`, `88`. A word
# is a word. This is the whole difference between McDonald's outlets and two
# unrelated shops that both start with `royal`.
MARKER_LEAD = "(-[#/*"

# Below this a shared token is too thin to mean anything — `hp`, `mr`, `sp`.
# Prefixes of two tokens or more are exempt, since the second token is the
# corroboration.
MIN_SOLO_PREFIX = 3


def suggest(keys: Iterable[str], taken: Iterable[str] = ()) -> list[dict]:
    """Candidate company groups over merchant keys, largest coverage first.

    `taken` is the keys already assigned to a group; they are left out entirely
    rather than re-proposed, so the screen shrinks as it is worked through.

    Returns `[{"name": <shared prefix>, "members": [<key>, ...]}, ...]`. The
    name is a default the user is expected to overwrite — `sheng siong` reads
    fine, `001shopeepay sg ipp` does not.
    """
    pool = sorted({k for k in keys if k} - set(taken))
    index = set(pool)

    # Every token prefix that two or more keys share, shortest first: the
    # shortest qualifying one is the widest real group, and the longer ones
    # inside it (`mcdonald's (pw)` under `mcdonald's`) are the same company
    # sliced finer.
    candidates: dict[str, list[str]] = {}
    for key in pool:
        tokens = key.split()
        for n in range(1, len(tokens) + 1):
            candidates.setdefault(" ".join(tokens[:n]), []).append(key)

    out: list[dict] = []
    claimed: set[str] = set()
    for prefix in sorted(candidates, key=lambda p: (len(p.split()), p)):
        members = [k for k in candidates[prefix] if k not in claimed]
        if len(members) < 2 or not _qualifies(prefix, members, index):
            continue
        claimed.update(members)
        out.append({"name": _display_name(prefix, members), "members": members})
    # Lead with the groups that actually change a report: ten outlets folding
    # into one line matters more than two.
    out.sort(key=lambda g: (-len(g["members"]), g["name"]))
    return out


def _display_name(prefix: str, members: list[str]) -> str:
    """The shortest prefix decides membership; a longer one usually reads better.

    Membership is settled on the shortest qualifying prefix, which is often a
    fragment — `guzman y`, `shopback swee`. Every member shares more than that,
    so the default name walks forward through the tokens they all agree on.

    It stops at the first marker token, because that is the branch talking and
    not the company: `toys'r'us (s) pl hq` and `toys'r'us (s) pl parkwa…` agree
    on `(s) pl`, and `toys'r'us` is the name a person would write.
    """
    tokens = prefix.split()
    rest = [m.split()[len(tokens):] for m in members]
    for i in range(min((len(r) for r in rest), default=0)):
        word = rest[0][i]
        if any(r[i] != word for r in rest) or _is_outlet_marker(word):
            break
        tokens.append(word)
    return " ".join(tokens)


def _qualifies(prefix: str, members: list[str], index: set[str]) -> bool:
    """Is this shared prefix a company name, or just a shared word?"""
    tokens = prefix.split()
    if len(tokens) >= 2:
        return True
    if len(prefix) < MIN_SOLO_PREFIX:
        return False
    if prefix in index:
        # The company bills under its bare name too, so the name is attested
        # rather than inferred from a coincidence of first words.
        return True
    return all(_is_outlet_marker(k[len(prefix):]) for k in members if k != prefix)


def _is_outlet_marker(rest: str) -> bool:
    """What follows the name: an outlet code, or another name word?

    `(apm)`, `-ctp`, ` 88` are the shop saying which branch. ` sporting house`
    is a different shop.
    """
    rest = rest.strip()
    if not rest:
        return True
    if rest[0] in MARKER_LEAD:
        return True
    return all(any(c.isdigit() for c in t) for t in rest.split())
