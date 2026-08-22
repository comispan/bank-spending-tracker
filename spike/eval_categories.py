"""Grade a candidate tier-3 model against the categories you chose yourself.

    python spike/eval_categories.py --list
    python spike/eval_categories.py --model baseline
    python spike/eval_categories.py --model qwen2.5:3b-instruct
    python spike/eval_categories.py --model claude-sonnet-5 --anthropic

Why this exists: DESIGN.md §3 leaves tier 3 as "a model, for the merchants
nothing else knows", and §9.4 leaves open whether that model runs locally or in
the cloud. Neither question is answerable by preference. This answers both with
a number, using the one piece of ground truth the app already has — the
merchants **you** categorized by hand, in `merchant_memory` with source
`memory`.

It measures the thing that would actually ship, gate included. Phase 0's finding
was that a small model fails *silently*: it returns something plausible and
empty rather than an error. So every response goes through the same contract
check tier 3 would use — no invented merchants, none dropped, every category
inside the fixed thirteen — and a model that scores well but breaks the contract
is reported as unusable, because it is. That is the qwen2.5 failure mode from
the Phase 0 notes: correct content, broken format.

Three numbers come out, and the middle one is the one that decides it:

    correct     it agreed with you
    WRONG       it disagreed, confidently — this is what tier 3 would write
                into merchant memory and quietly mislabel your spending with
    abstained   it said "unknown" — not a failure. A row that stays uncategorized
                is honest and one click from fixed. §2.3's whole argument.

A `baseline` pseudo-model (always answer your most common category) is scored
alongside, because a model that cannot beat "always guess Dining" is not
adding anything.

Notes on running local models: pin an `-instruct` tag. A bare tag is often the
thinking build, which narrates into the response and breaks the format contract
before the categories are even looked at.

Nothing leaves this machine unless you pass `--anthropic`, and even then only
the merchant names — no amounts, dates, balances or card numbers, exactly as
§3 and §7 promise. The script prints what it is about to send.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "app"))

from categorize import CATEGORIES  # noqa: E402

DB = HERE.parent / "data" / "app.db"
OUT = HERE / "out"
ABSTAIN = "unknown"
ALLOWED = set(CATEGORIES) | {ABSTAIN}

SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES + [ABSTAIN]},
                },
                "required": ["merchant", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}

SYSTEM = f"""You categorize merchants for a personal spending tracker used in Singapore.

You are given normalized merchant names taken from credit card and bank
statements. Assign each one exactly one category from this list:

{chr(10).join('- ' + c for c in CATEGORIES)}

Rules:
- Use "{ABSTAIN}" when you cannot tell what the merchant is. This is expected and
  is the right answer for an opaque company name. A wrong guess is worse than an
  honest "{ABSTAIN}", because a wrong guess is stored and silently mislabels
  someone's spending.
- Do NOT use "Other" as a way of saying you do not know. "Other" means a real
  purchase that genuinely fits none of the categories.
- Return every merchant you were given, spelled exactly as given.
- Return nothing except the JSON.
"""


# --------------------------------------------------------------- ground truth

def ground_truth() -> dict[str, str]:
    if not DB.exists():
        sys.exit(f"No database at {DB}. Upload a statement first.")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT merchant_normalized k, category c FROM merchant_memory
           WHERE source = 'memory' ORDER BY merchant_normalized"""
    ).fetchall()
    conn.close()
    return {r["k"]: r["c"] for r in rows}


# ------------------------------------------------------------------ backends

def ask_ollama(model: str, keys: list[str], host: str, loose: bool) -> str:
    payload = {
        "model": model,
        "stream": False,
        "format": "json" if loose else SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(keys)},
        ],
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach Ollama at {host}: {e}\n"
                 f"Start it with `ollama serve`, then `ollama pull {model}`.")
    return body.get("message", {}).get("content", "")


def ask_anthropic(model: str, keys: list[str], effort: str | None) -> str:
    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic")

    output_config: dict = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if effort:
        output_config["effort"] = effort

    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": "\n".join(keys)}],
            output_config=output_config,
        )
    except anthropic.AuthenticationError:
        sys.exit("No usable credentials. Run `ant auth status`, then `ant auth login`,\n"
                 "or export ANTHROPIC_API_KEY.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")

    if resp.stop_reason == "refusal":
        sys.exit("The request was declined by a safety classifier.")
    return next((b.text for b in resp.content if b.type == "text"), "")


def ask_baseline(keys: list[str], truth: dict[str, str]) -> str:
    """No model at all: always answer whatever category you use most.

    The floor any real model has to clear. It is easy to look good on a set
    where 40% of the answers are one category.
    """
    common = max(set(truth.values()), key=list(truth.values()).count)
    return json.dumps({"assignments": [{"merchant": k, "category": common} for k in keys]})


# ---------------------------------------------------------------------- gate

def gate(raw: str, asked: list[str]) -> tuple[bool, list[str], dict[str, str]]:
    """The contract tier 3 would enforce before writing anything to the database.

    Deliberately all-or-nothing on the batch, the same way §2.3 refuses to
    half-trust an extraction. A response that dropped nine merchants is not
    "mostly fine" — it is a response you cannot reason about.
    """
    problems: list[str] = []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return False, [f"the response is not JSON ({e})"], {}

    items = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False, ["no 'assignments' array in the response"], {}

    answers: dict[str, str] = {}
    malformed = duplicates = 0
    for entry in items:
        if not isinstance(entry, dict) or "merchant" not in entry or "category" not in entry:
            malformed += 1
            continue
        if entry["merchant"] in answers:
            duplicates += 1
        answers[str(entry["merchant"])] = str(entry["category"])

    invented = set(answers) - set(asked)
    dropped = set(asked) - set(answers)
    outside = sorted({c for c in answers.values() if c not in ALLOWED})

    if malformed:
        problems.append(f"{malformed} entr(ies) missing merchant or category")
    if duplicates:
        problems.append(f"{duplicates} merchant(s) answered more than once")
    if invented:
        problems.append(f"{len(invented)} merchant(s) invented that were never asked about")
    if dropped:
        problems.append(f"{len(dropped)} merchant(s) dropped from the answer")
    if outside:
        problems.append(f"category value(s) outside the fixed set: {outside[:4]}")
    return not problems, problems, answers


# --------------------------------------------------------------------- score

def score(answers: dict[str, str], truth: dict[str, str]) -> dict[str, int]:
    correct = wrong = abstained = missing = 0
    for key, want in truth.items():
        got = answers.get(key)
        if got is None:
            missing += 1
        elif got == ABSTAIN:
            abstained += 1
        elif got == want:
            correct += 1
        else:
            wrong += 1
    return {"correct": correct, "wrong": wrong, "abstained": abstained,
            "missing": missing, "total": len(truth)}


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def report(name: str, s: dict[str, int], ok: bool, problems: list[str],
           seconds: float | None) -> None:
    print(f"\n  {name}")
    print(f"    gate           {'PASS' if ok else 'FAIL'}")
    for p in problems:
        print(f"      · {p}")
    print(f"    correct        {s['correct']:3}  ({pct(s['correct'], s['total'])})")
    print(f"    WRONG          {s['wrong']:3}  ({pct(s['wrong'], s['total'])})")
    print(f"    abstained      {s['abstained']:3}  ({pct(s['abstained'], s['total'])})")
    if s["missing"]:
        print(f"    never answered {s['missing']:3}")
    if seconds is not None:
        print(f"    took           {seconds:.1f}s")


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", help="an Ollama tag, an Anthropic model id, or 'baseline'")
    ap.add_argument("--anthropic", action="store_true",
                    help="send the merchant names to the Anthropic API (leaves this machine)")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                    help="Anthropic only; omitted by default. Not supported on Haiku.")
    ap.add_argument("--host", default="http://localhost:11434", help="Ollama host")
    ap.add_argument("--loose", action="store_true",
                    help="ask Ollama for plain JSON instead of a schema (older builds)")
    ap.add_argument("--limit", type=int, help="only grade the first N merchants")
    ap.add_argument("--list", action="store_true", help="show the eval set size and exit")
    args = ap.parse_args()

    truth = ground_truth()
    if not truth:
        sys.exit("Nothing to grade against: no merchants have been categorized by hand.\n"
                 "Categorize some on /merchants first — those decisions are the answer key.")
    if args.limit:
        truth = dict(list(truth.items())[:args.limit])
    keys = list(truth)

    print(f"eval set: {len(keys)} merchants you categorized by hand")
    print(f"categories in play: {len(set(truth.values()))} of {len(CATEGORIES)}")
    common = max(set(truth.values()), key=list(truth.values()).count)
    print(f"most common answer covers {pct(list(truth.values()).count(common), len(truth))} "
          f"of the set")
    if args.list or not args.model:
        if not args.model:
            print("\nPass --model to grade one. Nothing has been sent anywhere.")
        return 0

    # The baseline is free and always worth printing next to the real result.
    base_ok, base_problems, base_answers = gate(ask_baseline(keys, truth), keys)
    report("baseline (always answer your most common category)",
           score(base_answers, truth), base_ok, base_problems, None)

    if args.model == "baseline":
        return 0

    if args.anthropic:
        print(f"\n  Sending {len(keys)} merchant names to the Anthropic API "
              f"({args.model}).")
        print("  No amounts, dates, balances or card numbers are included.")
        raw_fn = lambda: ask_anthropic(args.model, keys, args.effort)  # noqa: E731
    else:
        print(f"\n  Asking {args.model} locally at {args.host}. Nothing leaves this machine.")
        raw_fn = lambda: ask_ollama(args.model, keys, args.host, args.loose)  # noqa: E731

    started = time.time()
    raw = raw_fn()
    seconds = time.time() - started

    ok, problems, answers = gate(raw, keys)
    s = score(answers, truth)
    report(args.model, s, ok, problems, seconds)

    OUT.mkdir(parents=True, exist_ok=True)
    detail = OUT / f"eval-{args.model.replace(':', '-').replace('/', '-')}.txt"
    lines = [f"model: {args.model}", f"gate: {'PASS' if ok else 'FAIL'}"]
    lines += [f"  problem: {p}" for p in problems]
    lines += ["", f"{'merchant':34} {'you said':20} {'it said':20} verdict", ""]
    for key, want in truth.items():
        got = answers.get(key, "(no answer)")
        verdict = ("ok" if got == want else
                   "abstained" if got == ABSTAIN else
                   "missing" if got == "(no answer)" else "WRONG")
        lines.append(f"{key[:32]:34} {want:20} {got:20} {verdict}")
    lines += ["", "raw response:", raw]
    detail.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    print(f"\n  per-merchant detail: {detail}")
    if not ok:
        print("  The gate failed, so tier 3 would have discarded this whole batch "
              "and left every row uncategorized. Accuracy above is diagnostic only.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
