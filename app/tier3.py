"""Tier 3 of the categorizer: a model, for the merchants nothing else knows.

DESIGN.md Section 3 leaves this as the last tier and Section 9.4 as the only
question in the app about anything leaving the machine. Both are settled here,
and narrowly: what goes out is a **list of normalized merchant names** —
`grab`, `fairprice`, `netflix` — and nothing else. No amounts, no dates, no
balances, no card numbers, no statement text, no account identifiers. The
payload is built in `prompt_payload()` below and the UI shows it verbatim
before anything is sent.

**This module is what the eval grades.** `spike/eval_categories.py` imports the
prompt, the schema and the gate from right here rather than keeping its own
copy, because a harness that scores a different prompt than the one that ships
is measuring a component nobody is going to run. Section 2.3's rule — a
component nobody can grade does not ship — only means something if the graded
thing and the shipped thing are the same code.

Two properties carried over from the Phase 0 findings, and neither is optional:

  - **The gate is all-or-nothing per batch.** A small model fails *silently*:
    it returns something plausible and incomplete rather than an error. A reply
    that dropped nine merchants is not "mostly fine", it is a reply you cannot
    reason about, so the whole batch is discarded and those merchants stay
    uncategorized.
  - **`unknown` is a legal answer.** A row left uncategorized is honest and one
    click from fixed; a confident wrong answer is stored and quietly mislabels
    the spending. Abstentions are never written to memory.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from categorize import CATEGORIES

ABSTAIN = "unknown"
ALLOWED = set(CATEGORIES) | {ABSTAIN}

# Google's REST surface for Gemini. The key travels in a header rather than the
# `?key=` query parameter both forms accept — a credential in a URL ends up in
# logs, history and error messages, none of which is a place for it.
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
DEFAULT_MODEL = "gemini-3.7-flash"
TIMEOUT_SECONDS = 120

# Merchants per request. One call for the whole batch is what Section 3
# describes and 34 unknown merchants — the real monthly figure — fits in one
# comfortably. The chunking matters at the other end of the range: with a large
# first-import backlog, a batch that fails the gate takes only its own chunk
# down with it instead of every merchant in the app.
BATCH_SIZE = 60

# Measured 2026-08-28 on a free-tier key: the Flash models answer a 43-merchant
# batch in a few seconds when they answer at all, but return 500 "experiencing
# high demand" under load and 429 at 5 requests/minute. Both are temporary and
# both say so, so the only wrong response is to give up on the first one — that
# is a merchant left uncategorized for a reason that had nothing to do with the
# merchant. 400/401/403 are not in here on purpose: a bad key or a rejected
# schema will fail identically forever, and retrying it just spends the quota.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
# A cap, because the 429 body cheerfully suggests waiting 58 seconds and this
# runs inside a web request. Past this, failing with a legible message beats
# holding the page open.
MAX_TOTAL_WAIT_SECONDS = 75

# ------------------------------------------------------------------ grounding
#
# Off by default and behind a flag rather than simply on, because it changes
# what the Section 9.4 disclosure has to say. Without it the merchant names are
# read by one model; with it they are also issued as Google Search queries.
# Still names only — no amounts, dates, balances or statement text — but "sent
# to a model" and "typed into a search engine" are two different promises, and
# the screen only ever asked for the first.
#
# What it buys is the abstention this tier is built to produce. `kintsugi pte.
# ltd` is a restaurant group; nothing in that string says so, and no amount of
# model scale recovers a fact that was never in the name. A registry lookup
# settles it in one query.
#
# What it costs is billed searches, latency inside a synchronous web request,
# and a model with *reasons* to be confident about a merchant it has still
# misidentified. Section 3 already priced that trade: `WRONG` is the column
# that decides this, not `correct`, and a sourced wrong answer is stored and
# mislabels the spending exactly as an unsourced one does.
#
# Structured output alongside a built-in tool is Gemini 3-series only, which is
# less of a constraint than it sounds: the 2.5 models that documented a free
# grounding allowance are already closed to new API keys ("no longer available
# to new users"), so the 3-series is what there is. A model that does reject the
# combination fails with a 4xx, which is not retryable and surfaces the API's
# own message rather than being swallowed.
GROUNDING_TOOL = [{"type": "google_search"}]

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

# Appended to SYSTEM only when the tool is actually attached, rather than
# folded into it, so an ungrounded run is byte-identical to the prompt Section 3
# graded at 77% correct / 9% wrong. Turning grounding on has to move one
# variable, or the eval compares two changes and credits the tool with both.
GROUNDING_NOTE = f"""
You have Google Search. Use it on merchant names you do not recognize: many are
small Singapore companies whose registered name says nothing about what they
sell, and one lookup of the company or the brand usually settles it.

Search does not lower the bar for "{ABSTAIN}". If a lookup does not identify the
business, answer "{ABSTAIN}" anyway — a sourced guess is still a guess, and it is
stored and mislabels someone's spending exactly as an unsourced one does.
"""


def system_prompt(grounding: bool) -> str:
    """The system turn, with the search instructions attached only when they apply."""
    return SYSTEM + GROUNDING_NOTE if grounding else SYSTEM


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


# ------------------------------------------------------------------ the key

def _from_env_file(name: str) -> str | None:
    """One setting out of the gitignored `.env` beside the app.

    `.env` is already in `.gitignore` alongside `data/`, so reading it costs
    nothing and saves setting variables in every shell that launches uvicorn.
    """
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


def _setting(name: str) -> str | None:
    """One setting, environment first so a shell can always override the file."""
    return (os.environ.get(name) or "").strip() or _from_env_file(name)


def api_key() -> str | None:
    """The Gemini key, from the environment or a gitignored `.env`.

    Never a constant, never a file inside `app/`, and never anything the app
    writes: the key is the user's credential and this module only ever reads it.

    Both names are tried in the environment before either is tried in the file,
    so a shell variable wins over the file whichever of the two names it uses.
    """
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = _from_env_file(name)
        if value:
            return value
    return None


def configured() -> bool:
    return api_key() is not None


def model_name() -> str:
    return _setting("GEMINI_MODEL") or DEFAULT_MODEL


def grounding_enabled() -> bool:
    """Whether tier 3 hands the model Google Search. Off unless asked for.

    Read the same way as the key and the model, so the one file that already
    configures this tier configures all of it.
    """
    return (_setting("GEMINI_GROUNDING") or "").lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------- the payload

def prompt_payload(keys: list[str]) -> str:
    """Exactly what would be sent, for the screen that asks permission to send it.

    Section 9.4 is a disclosure question, and a disclosure the user cannot
    check is not one. This returns the literal user-turn content so the UI can
    print it rather than describe it.
    """
    return "\n".join(keys)


# ------------------------------------------------------------------ backend

class Tier3Error(RuntimeError):
    """The call did not produce a response to gate. Nothing has been written."""


def _output_text(body: dict) -> str:
    """The model's answer, out of an Interactions response.

    `output_text` is the convenience field the SDKs expose; the raw body keeps
    the same text in `steps`, where a reasoning step can sit alongside the
    answer — so the model_output step is selected rather than the last one.
    """
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    chunks: list[str] = []
    for step in body.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "\n".join(chunks)


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    """How long to wait, preferring what the API asked for over a guess.

    Gemini puts the real figure in the 429 body ("Please retry in 57.8s") and
    sometimes in `Retry-After`. Backing off blindly either wastes time or comes
    back too early and spends another request against the same exhausted quota.
    """
    header = error.headers.get("Retry-After") if error.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    body = getattr(error, "_body", "") or ""
    match = re.search(r"retry in ([\d.]+)s", body, re.I)
    if match:
        return float(match.group(1)) + 0.5
    return float(2 ** attempt)


def ask_gemini(keys: list[str], model: str | None = None,
               key: str | None = None, thinking: str = "low",
               grounding: bool | None = None) -> str:
    """One batch out to Gemini, raw response text back. No parsing, no writing.

    Temperature is deliberately left at the model default: Google's guidance for
    the Gemini 3 models is to leave it alone, and the determinism that a zero
    would buy is not worth being the one caller fighting the model's tuning. The
    gate is what makes the output safe to use, not the sampling settings.

    `grounding=None` takes the configured default; True and False force it, so
    the eval can grade both without editing the environment it is measuring.
    """
    key = key or api_key()
    if not key:
        raise Tier3Error(
            "No Gemini API key. Set GEMINI_API_KEY in the environment, or put "
            "GEMINI_API_KEY=... in a .env file at the project root.")

    grounding = grounding_enabled() if grounding is None else grounding
    payload = {
        "model": model or model_name(),
        "system_instruction": system_prompt(grounding),
        "input": prompt_payload(keys),
        "generation_config": {"thinking_level": thinking},
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": SCHEMA,
        },
    }
    if grounding:
        # The schema stays. Search arrives as extra steps in the response; the
        # answer is still in a model_output step, and `_output_text` already
        # picks that out rather than taking the last step it sees.
        payload["tools"] = GROUNDING_TOOL
    def build() -> urllib.request.Request:
        return urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
                "Api-Revision": API_REVISION,
            },
        )

    waited = 0.0
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(build(), timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            e._body = detail                                  # for retry_delay
            # A bad key or a rejected schema fails the same way every time, so
            # these are final. The body says which, and surfacing it beats
            # "the call failed".
            if e.code not in RETRYABLE_STATUS:
                raise Tier3Error(f"Gemini returned HTTP {e.code}: {detail}") from e
            last = f"HTTP {e.code}: {detail}"
            delay = retry_delay(e, attempt)
            if attempt == MAX_ATTEMPTS - 1 or waited + delay > MAX_TOTAL_WAIT_SECONDS:
                raise Tier3Error(
                    f"Gemini is unavailable after {attempt + 1} attempt(s) — {last}. "
                    f"Nothing was stored.") from e
            time.sleep(delay)
            waited += delay
            continue
        except urllib.error.URLError as e:
            raise Tier3Error(f"Could not reach the Gemini API: {e.reason}") from e
        # A read that times out raises TimeoutError, which is a sibling of
        # URLError under OSError rather than a subclass of it — so it escaped
        # the handler above and reached the user as a stack trace. urllib only
        # wraps socket errors while connecting; anything that goes wrong once
        # the request is in flight arrives bare, which is why the OSError
        # backstop is here too. An overloaded model is the common cause, and it
        # is worth one more attempt rather than a dead end.
        except TimeoutError as e:
            last = f"no answer within {TIMEOUT_SECONDS}s"
            if attempt == MAX_ATTEMPTS - 1:
                raise Tier3Error(
                    f"Gemini did not answer within {TIMEOUT_SECONDS}s for "
                    f"{len(keys)} merchant(s), after {MAX_ATTEMPTS} attempts. "
                    f"Nothing was stored.") from e
            continue
        except OSError as e:
            raise Tier3Error(f"The Gemini request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise Tier3Error(f"The Gemini response was not JSON: {e}") from e

        if body.get("status") not in (None, "completed"):
            raise Tier3Error(f"Gemini did not complete the request: {body.get('status')}")
        return _output_text(body)

    raise Tier3Error(f"Gemini is unavailable — {last}. Nothing was stored.")


# --------------------------------------------------------------------- gate

def gate(raw: str, asked: list[str]) -> tuple[bool, list[str], dict[str, str]]:
    """The contract enforced before anything reaches the database.

    Deliberately all-or-nothing on the batch, the same way Section 2.3 refuses
    to half-trust an extraction. A response that dropped nine merchants is not
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


# ------------------------------------------------------------------ classify

def classify(keys: list[str], model: str | None = None,
             key: str | None = None,
             grounding: bool | None = None) -> dict[str, object]:
    """Categorize unknown merchants, one chunk at a time.

    Returns what happened rather than raising, because a partial result is
    genuinely useful — three chunks that passed the gate should still be applied
    when the fourth did not — and the caller has to be able to tell the user
    which merchants were left alone and why.

    Abstentions are dropped here rather than downstream: `unknown` means the
    model declined, and the only correct thing to store for a declined merchant
    is nothing at all.
    """
    keys = [k for k in keys if k]
    chunks = [keys[i:i + BATCH_SIZE] for i in range(0, len(keys), BATCH_SIZE)]
    grounding = grounding_enabled() if grounding is None else grounding

    assignments: dict[str, str] = {}
    abstained: list[str] = []
    problems: list[str] = []
    batches_ok = 0

    for chunk in chunks:
        try:
            raw = ask_gemini(chunk, model=model, key=key, grounding=grounding)
        except Tier3Error as e:
            problems.append(str(e))
            continue
        ok, batch_problems, answers = gate(raw, chunk)
        if not ok:
            problems.extend(batch_problems)
            continue
        batches_ok += 1
        for merchant, category in answers.items():
            if category == ABSTAIN:
                abstained.append(merchant)
            else:
                assignments[merchant] = category

    return {
        "assignments": assignments,
        "abstained": sorted(abstained),
        "problems": problems,
        "asked": len(keys),
        "batches": len(chunks),
        "batches_ok": batches_ok,
        # Reported rather than assumed, so the caller can say where the names
        # actually went instead of repeating what the setting said at boot.
        "grounding": grounding,
    }
