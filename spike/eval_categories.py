"""Grade a candidate tier-3 model against the categories you chose yourself.

    python spike/eval_categories.py --list
    python spike/eval_categories.py --model baseline
    python spike/eval_categories.py --model qwen2.5:3b-instruct
    python spike/eval_categories.py --gemini
    python spike/eval_categories.py --gemini --grounding
    python spike/eval_categories.py --model claude-sonnet-5 --anthropic

Why this exists: DESIGN.md Section 3 leaves tier 3 as "a model, for the
merchants nothing else knows", and Section 9.4 leaves open whether that model
runs locally or in the cloud. Neither question is answerable by preference.
This answers both with a number, using the one piece of ground truth the app
already has — the merchants **you** categorized by hand, in `merchant_memory`
with source `memory`.

It measures the thing that would actually ship, gate included — literally, since
the prompt, the schema and the gate are imported from `app/tier3.py` rather than
copied here. A harness that scores a different prompt than the one that runs is
measuring a component nobody is going to use. Phase 0's finding was that a small
model fails *silently*: it returns something plausible and empty rather than an
error. So every response goes through the same contract check tier 3 uses — no
invented merchants, none dropped, every category inside the fixed thirteen — and
a model that scores well but breaks the contract is reported as unusable,
because it is. That is the qwen2.5 failure mode from the Phase 0 notes: correct
content, broken format.

Three numbers come out, and the middle one is the one that decides it:

    correct     it agreed with you
    WRONG       it disagreed, confidently — this is what tier 3 would write
                into merchant memory and quietly mislabel your spending with
    abstained   it said "unknown" — not a failure. A row that stays uncategorized
                is honest and one click from fixed. Section 2.3's whole
                argument.

A `baseline` pseudo-model (always answer your most common category) is scored
alongside, because a model that cannot beat "always guess Dining" is not
adding anything.

Notes on running local models: pin an `-instruct` tag. A bare tag is often the
thinking build, which narrates into the response and breaks the format contract
before the categories are even looked at.

Nothing leaves this machine unless you pass `--gemini` or `--anthropic`, and
even then only the merchant names — no amounts, dates, balances or card numbers,
exactly as Sections 3 and 7 promise. The script prints what it is about to send.

`--grounding` adds Google Search to the Gemini run, which is the one option here
that widens *where* the names go rather than which model reads them: they become
search queries too. It is off in the app unless `GEMINI_GROUNDING` says
otherwise, and this flag forces it on regardless, so a grounded and an
ungrounded run can be graded back to back without editing the setting between
them. Read the `WRONG` column first — search buys answers on merchants whose
name carries nothing, and it also lets a model be confident about one it has
misidentified.
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

import tier3  # noqa: E402
from categorize import CATEGORIES  # noqa: E402
# The three things that decide whether a result is trustworthy all come from the
# shipping module, so grading and running cannot drift apart.
from tier3 import ABSTAIN, SCHEMA, SYSTEM, gate  # noqa: E402

DB = HERE.parent / "data" / "app.db"
OUT = HERE / "out"


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


def ask_gemini(model: str, keys: list[str], grounding: bool) -> str:
    """Straight through the shipping client, so this grades the real thing.

    Including its failure modes: a bad model id or a rejected schema arrives
    here as a Tier3Error with the API's own message, which is the same thing the
    app would show. `grounding` is passed explicitly rather than left to default,
    so the run is graded on the flag you typed and not on what `.env` happens to
    say today.
    """
    try:
        return tier3.ask_gemini(keys, model=model, grounding=grounding)
    except tier3.Tier3Error as e:
        sys.exit(str(e))


def ask_baseline(keys: list[str], truth: dict[str, str]) -> str:
    """No model at all: always answer whatever category you use most.

    The floor any real model has to clear. It is easy to look good on a set
    where 40% of the answers are one category.
    """
    common = max(set(truth.values()), key=list(truth.values()).count)
    return json.dumps({"assignments": [{"merchant": k, "category": common} for k in keys]})


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
    ap.add_argument("--model", help="an Ollama tag, a hosted model id, or 'baseline'")
    ap.add_argument("--gemini", action="store_true",
                    help="send the merchant names to the Gemini API (leaves this machine). "
                         "This is what tier 3 ships with; --model defaults to "
                         f"{tier3.DEFAULT_MODEL}")
    ap.add_argument("--grounding", action="store_true",
                    help="give the Gemini run Google Search (the merchant names become "
                         "search queries as well as model input). Off in the app unless "
                         "GEMINI_GROUNDING is set; this forces it on for one run.")
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

    # --gemini is the one backend with a sensible default, because it is the one
    # the app is wired to: naming the model is then optional rather than a thing
    # you have to look up to run the grader at all.
    if args.gemini and not args.model:
        args.model = tier3.model_name()
    if args.grounding and not args.gemini:
        ap.error("--grounding applies to --gemini; the other backends have no search tool")

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

    if args.gemini:
        print(f"\n  Sending {len(keys)} merchant names to the Gemini API "
              f"({args.model}).")
        print("  No amounts, dates, balances or card numbers are included.")
        if args.grounding:
            # Said plainly and separately, because it is a different promise
            # from the one the line above makes: the names reach Google Search,
            # not only the model.
            print("  Google Search is ON: these names are also sent to Google as "
                  "search queries.")
        raw_fn = lambda: ask_gemini(args.model, keys, args.grounding)  # noqa: E731
    elif args.anthropic:
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
    label = args.model + (" + Google Search" if args.grounding else "")
    report(label, s, ok, problems, seconds)

    OUT.mkdir(parents=True, exist_ok=True)
    slug = args.model.replace(':', '-').replace('/', '-')
    if args.grounding:
        slug += "-grounded"
    detail = OUT / f"eval-{slug}.txt"
    lines = [f"model: {label}", f"gate: {'PASS' if ok else 'FAIL'}"]
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
