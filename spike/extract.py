"""
Phase 0 spike: can we extract transactions from a statement PDF and prove it?

Not production code. No DB, no UI, no abstractions worth keeping. The only
question this answers is: does extraction reconcile against the statement's
own printed totals, and on which banks does it fail?

Usage:
    python extract.py --dry-run          # text extraction only, no network
    python extract.py                    # full pipeline (needs ANTHROPIC_API_KEY)
    python extract.py --file foo.pdf     # just one

Provider is selected by environment:
    SPIKE_PROVIDER   anthropic (default) | ollama | openai
    SPIKE_MODEL      model id (default claude-opus-5)
    SPIKE_BASE_URL   endpoint; required for openai, defaults to
                     http://localhost:11434 for ollama
    SPIKE_API_KEY    key for the openai backend
    SPIKE_NUM_CTX    ollama context window (default 16384)
    SPIKE_TIMEOUT    ollama per-page timeout in seconds (default 1800)
    SPIKE_THINK      set true to allow thinking on ollama (default off: for
                     schema-constrained extraction it only costs time)

The reconciliation gate is the same either way, so provider choice is a
measurable question: run the same statements through each, compare PASS counts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
import pypdf

HERE = Path(__file__).parent
STATEMENTS = HERE / "statements"
OUT = HERE / "out"

# Opus 5 by default: the spike is measuring whether extraction is *possible*,
# so run it at full strength. The whole run costs about a dollar either way.
# Once it passes, re-run with MODEL=claude-sonnet-5 to see if the cheaper model
# holds up — that's the number that matters for production, not for this.
MODEL = os.environ.get("SPIKE_MODEL", "claude-opus-5")
MAX_TOKENS = 16000                # thinking + JSON share this budget on Opus 5
TOLERANCE = Decimal("0.01")       # a cent of rounding slack
SCAN_CHAR_THRESHOLD = 100         # below this, the page is almost certainly an image
DATE_SLACK_DAYS = 5               # posting lag outside the statement period


# ------------------------------------------------------------------- text io
#
# Every text read and write in this file goes through these two functions.
# Nothing calls Path.read_text/write_text or bare open() in text mode directly,
# because those inherit the platform's locale encoding — cp1252 on Windows —
# and statements are full of characters it cannot represent.

def write_text(path: Path, data: str) -> None:
    """Output is UTF-8 with LF endings on every platform. Not negotiable."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def read_text(path: Path) -> str:
    """Decode a file we did not write.

    We control our own output, but not passwords.json — a Windows editor may
    save it as cp1252 and Notepad prepends a BOM. Try the plausible encodings
    in order rather than making the user care which one they used.
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Last resort: never fail a run over an undecodable byte.
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- redaction

# A transaction row, heuristically: starts with a date, ends with an amount.
# Only used to judge whether the table survived text extraction — the real
# parsing is done by the model.
TXN_DATE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[ /-](?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:[ /-]\d{2,4})?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ /-]\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}"
    r")\b", re.I)
TXN_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}\s*(?:cr|dr)?\s*$", re.I)


def count_txn_shaped_lines(pages: list[Page]) -> int:
    return sum(
        1
        for p in pages
        for line in p.text.splitlines()
        if TXN_DATE.match(line) and TXN_AMOUNT.search(line)
    )


def redact(text: str) -> str:
    """Strip full card numbers before anything leaves the machine.

    Only ever need the last 4. Runs on every page, including in --dry-run,
    so the dry-run output is safe to paste into a bug report.
    """
    def _mask(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "*" * (len(digits) - 4) + digits[-4:]

    # 13-19 digits, optionally split by spaces or dashes in groups
    return re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _mask, text)


# ------------------------------------------------------------ pdf -> text

@dataclass
class Page:
    number: int
    text: str
    char_count: int

    @property
    def looks_scanned(self) -> bool:
        return self.char_count < SCAN_CHAR_THRESHOLD


def read_pdf(path: Path, password: str | None) -> tuple[list[Page], str | None]:
    """Return (pages, error). Decrypts first if needed."""
    source = path

    reader = pypdf.PdfReader(str(path))
    if reader.is_encrypted:
        # Plenty of issuers encrypt a statement purely to set permission flags
        # (no printing, no copying) and leave the user password empty, so the
        # file opens with no password at all. Always try that before asking the
        # user for one — DBS does this.
        if reader.decrypt("") == 0:
            if not password:
                return [], "encrypted (no password supplied — add it to passwords.json)"
            if reader.decrypt(password) == 0:
                return [], "encrypted (supplied password rejected)"

        # pdfplumber can't take an already-decrypted pypdf reader, so write a
        # decrypted copy to a temp path and read that.
        writer = pypdf.PdfWriter()
        for p in reader.pages:
            writer.add_page(p)
        source = OUT / f".decrypted-{path.name}"
        source.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "wb") as fh:
            writer.write(fh)

    pages: list[Page] = []
    with pdfplumber.open(str(source)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            # layout=True keeps column alignment, which is the whole point.
            # Plain extract_text() interleaves columns and destroys the table.
            raw = page.extract_text(layout=True) or ""
            pages.append(Page(number=i, text=redact(raw), char_count=len(raw.strip())))

    if source != path:
        source.unlink(missing_ok=True)

    return pages, None


# ------------------------------------------------------------ text -> json

SYSTEM = """You extract transactions from bank and credit card statements.

Rules:
- Extract ONLY rows from the transaction table. Ignore marketing text, rewards
  promos, legal fine print, and page headers/footers.
- A row that is a card payment, direct debit of the card balance, or balance
  transfer IS still a transaction — extract it, and mark direction correctly.
- Do not invent transactions. If a row is cut off at a page boundary, extract
  what is visible; the caller stitches pages.
- Amounts: positive decimal strings, no currency symbols or thousands
  separators. Use `direction` to convey sign.
- Dates: ISO YYYY-MM-DD. Infer the year from the statement period when the row
  shows only day and month. Statements spanning a year boundary are common —
  a December row on a statement ending in January belongs to the earlier year.
- Summary figures (opening/closing balance, totals): report them ONLY if they
  are printed on THIS page. Never compute or estimate them. Null if absent.
"""

TOOL = {
    "name": "record_page",
    "description": "Record structured data extracted from one statement page.",
    # strict mode guarantees the input validates against this schema exactly,
    # so a malformed extraction fails loudly at the API instead of quietly
    # producing a half-parsed statement. Requires every property listed in
    # `required` and additionalProperties false — optional fields are nullable.
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issuer": {"type": ["string", "null"], "description": "Bank/issuer name if printed on this page"},
            "account_last4": {"type": ["string", "null"]},
            "statement_period_start": {"type": ["string", "null"], "description": "ISO date"},
            "statement_period_end": {"type": ["string", "null"], "description": "ISO date"},
            "currency": {"type": ["string", "null"], "description": "ISO 4217, e.g. SGD"},
            "opening_balance": {"type": ["string", "null"], "description": "Decimal string, only if printed on this page"},
            "closing_balance": {"type": ["string", "null"]},
            "total_debits": {"type": ["string", "null"], "description": "Total charges/purchases/withdrawals, if printed"},
            "total_credits": {"type": ["string", "null"], "description": "Total payments/refunds/deposits, if printed"},
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "date": {"type": "string", "description": "ISO transaction date"},
                        "posted_date": {"type": ["string", "null"]},
                        "description": {"type": "string", "description": "Verbatim merchant text"},
                        "amount": {"type": "string", "description": "Positive decimal string"},
                        "direction": {"type": "string", "enum": ["debit", "credit"]},
                    },
                    "required": ["date", "posted_date", "description", "amount", "direction"],
                },
            },
        },
        "required": [
            "issuer", "account_last4", "statement_period_start", "statement_period_end",
            "currency", "opening_balance", "closing_balance", "total_debits",
            "total_credits", "transactions",
        ],
    },
}


def user_prompt(page: Page, hint: str) -> str:
    return f"Statement page {page.number}.{hint}\n\n<page>\n{page.text}\n</page>"


def extract_page_anthropic(client, page: Page, hint: str) -> dict:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_page"},
        messages=[{"role": "user", "content": user_prompt(page, hint)}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    return {"transactions": []}


def extract_page_openai_compatible(client, page: Page, hint: str) -> dict:
    """Any provider speaking the OpenAI chat-completions API.

    Covers Ollama (local), Groq, DeepSeek, OpenRouter, Together, and Gemini's
    compatibility endpoint. Uses JSON-schema response format rather than tool
    use — more widely supported across these providers, same guarantee.
    """
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "record_page",
                "strict": True,
                "schema": TOOL["input_schema"],
            },
        },
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt(page, hint)},
        ],
    )
    return parse_json_loosely(resp.choices[0].message.content or "")


def parse_json_loosely(raw: str) -> dict:
    """Smaller and local models often wrap JSON in prose or a ``` fence."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"transactions": []}


class OllamaClient:
    """Ollama's native /api/chat.

    Ollama does NOT accept OpenAI's `response_format: {type: "json_schema"}` on
    its /v1 compatibility endpoint — it takes the schema in its own top-level
    `format` field on /api/chat instead. Sending the OpenAI shape gets you
    unconstrained prose that happens to look like JSON, or nothing.

    Worth using the native path anyway: `format` constrains token *generation*
    to the schema, so malformed JSON is mechanically impossible rather than
    merely discouraged.
    """

    def __init__(self, base_url: str):
        import httpx
        self.url = base_url.rstrip("/").removesuffix("/v1") + "/api/chat"
        # Cold-loading a 20GB model into RAM takes a while on the first call,
        # and CPU-only generation is slow. Generous, but bounded.
        timeout = float(os.environ.get("SPIKE_TIMEOUT", "1800"))
        self.http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        self.timeout = timeout
        # Ollama 0.12+ auto-enables thinking on thinking-capable models. For
        # schema-constrained extraction that's pure cost: the model writes a
        # long reasoning trace before emitting JSON it was going to be forced
        # into anyway. Off unless explicitly asked for.
        self.think = os.environ.get("SPIKE_THINK", "").lower() in ("1", "true", "yes")
        self._send_think = True   # cleared if this server/model rejects the field

    def chat(self, system: str, user: str, schema: dict) -> str:
        import httpx
        try:
            return self._post(system, user, schema)
        except httpx.ConnectError:
            raise SystemExit(
                f"Can't reach Ollama at {self.url}.\n"
                f"Start it (`ollama serve`, or launch the Ollama app) and retry."
            ) from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise SystemExit(
                f"Ollama did not respond within {self.timeout:.0f}s on one page.\n"
                f"\n"
                f"{MODEL} is likely too slow on this machine, or is swapping to disk.\n"
                f"In rough order of what to try:\n"
                f"  1. Prove the pipeline with a small model first:\n"
                f"       ollama pull qwen3.5:9b\n"
                f"       $env:SPIKE_MODEL = \"qwen3.5:9b\"\n"
                f"  2. Check it isn't swapping — `ollama ps` shows resident size.\n"
                f"     If that approaches your free RAM, use a smaller model.\n"
                f"  3. Raise the ceiling if it's merely slow, not stuck:\n"
                f"       $env:SPIKE_TIMEOUT = \"3600\"\n"
            ) from None
        except httpx.HTTPStatusError as e:
            hint = ""
            if e.response.status_code == 404:
                hint = f"\nIs the model pulled?  ollama pull {MODEL}"
            raise SystemExit(f"Ollama returned {e.response.status_code}.{hint}") from None

    def _post(self, system: str, user: str, schema: dict) -> str:
        import httpx
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            "keep_alive": "10m",   # don't re-load the model between pages
            "options": {
                "temperature": 0,
                # Ollama defaults to a small context (often 4096). A statement
                # page plus the system prompt and schema overruns that, and the
                # overflow is silently dropped from the front — which looks
                # exactly like the model missing transactions.
                "num_ctx": int(os.environ.get("SPIKE_NUM_CTX", "16384")),
            },
        }
        if self._send_think:
            payload["think"] = self.think

        r = self.http.post(self.url, json=payload)
        # Older Ollama builds don't know the `think` field. Drop it and retry
        # rather than failing the run over an optimisation.
        if r.status_code == 400 and self._send_think:
            self._send_think = False
            payload.pop("think")
            r = self.http.post(self.url, json=payload)

        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")


def extract_page_ollama(client: OllamaClient, page: Page, hint: str) -> dict:
    raw = client.chat(SYSTEM, user_prompt(page, hint), TOOL["input_schema"])
    return parse_json_loosely(raw)


def build_client():
    """Pick a backend from SPIKE_PROVIDER: anthropic (default) | ollama | openai.

    The reconciliation gate is identical across all three, so provider choice
    becomes a measurable question — run the same statements through each and
    compare PASS counts, rather than guessing which model is good enough.
    """
    provider = os.environ.get("SPIKE_PROVIDER", "anthropic").lower()

    if provider == "ollama":
        base = os.environ.get("SPIKE_BASE_URL", "http://localhost:11434")
        return OllamaClient(base), extract_page_ollama

    if provider == "openai":
        base_url = os.environ.get("SPIKE_BASE_URL")
        if not base_url:
            raise SystemExit("SPIKE_PROVIDER=openai needs SPIKE_BASE_URL set.")
        import openai
        key = os.environ.get("SPIKE_API_KEY") or "not-needed"
        return openai.OpenAI(base_url=base_url, api_key=key), extract_page_openai_compatible

    if provider != "anthropic":
        raise SystemExit(f"Unknown SPIKE_PROVIDER={provider!r} (anthropic | ollama | openai)")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "Set ANTHROPIC_API_KEY, or pick another provider with SPIKE_PROVIDER.\n"
            "See spike/README.md for provider settings."
        )
    import anthropic
    return anthropic.Anthropic(), extract_page_anthropic


# -------------------------------------------------------------- reconcile

def dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


@dataclass
class Result:
    name: str
    pages: int = 0
    scanned_pages: int = 0
    txns: int = 0
    verdict: str = "unknown"
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def reconcile(stmt: dict, result: Result) -> None:
    """The trust gate. Accept only if the numbers agree with the statement."""
    txns = stmt.get("transactions", [])
    debits = sum((dec(t["amount"]) or Decimal(0) for t in txns if t["direction"] == "debit"), Decimal(0))
    credits = sum((dec(t["amount"]) or Decimal(0) for t in txns if t["direction"] == "credit"), Decimal(0))

    td, tc = dec(stmt.get("total_debits")), dec(stmt.get("total_credits"))
    ob, cb = dec(stmt.get("opening_balance")), dec(stmt.get("closing_balance"))

    # Rule 1: printed totals.
    if td is not None or tc is not None:
        deltas = []
        if td is not None:
            deltas.append(("debits", abs(debits - td)))
        if tc is not None:
            deltas.append(("credits", abs(credits - tc)))
        worst = max(d for _, d in deltas)
        if worst <= TOLERANCE:
            result.verdict = "PASS"
            result.detail = "matches printed totals"
            return
        result.verdict = "FAIL"
        result.detail = "; ".join(f"{k} off by {d}" for k, d in deltas if d > TOLERANCE)
        return

    # Rule 2: balance roll-forward. Sign convention differs between a credit
    # card (charges raise the balance) and a deposit account (they lower it),
    # so accept either orientation and record which one held.
    if ob is not None and cb is not None:
        card = abs((ob + debits - credits) - cb)
        bank = abs((ob - debits + credits) - cb)
        if min(card, bank) <= TOLERANCE:
            result.verdict = "PASS"
            result.detail = "balance rolls forward (%s convention)" % ("card" if card <= bank else "deposit")
            return
        result.verdict = "FAIL"
        result.detail = f"balance off by {min(card, bank)}"
        return

    result.verdict = "UNVERIFIED"
    result.detail = "statement prints no totals or balances to check against"


def sanity_checks(stmt: dict, result: Result) -> None:
    txns = stmt.get("transactions", [])
    if not txns:
        result.warnings.append("no transactions found")

    start, end = stmt.get("statement_period_start"), stmt.get("statement_period_end")
    if start and end:
        import datetime as dt
        try:
            lo = dt.date.fromisoformat(start) - dt.timedelta(days=DATE_SLACK_DAYS)
            hi = dt.date.fromisoformat(end) + dt.timedelta(days=DATE_SLACK_DAYS)
            stray = [t["date"] for t in txns if not (lo <= dt.date.fromisoformat(t["date"]) <= hi)]
            if stray:
                result.warnings.append(f"{len(stray)} date(s) outside period, e.g. {stray[0]}")
        except ValueError:
            result.warnings.append("unparseable date in period or transactions")

    seen, dupes = set(), 0
    for t in txns:
        key = (t["date"], t["amount"], t["description"])
        if key in seen:
            dupes += 1
        seen.add(key)
    if dupes:
        result.warnings.append(f"{dupes} identical row(s) within one statement (page double-read?)")


# ------------------------------------------------------------------ driver

def merge(pages: list[dict]) -> dict:
    """Concatenate transactions; take each summary field from the first page that has it."""
    out: dict = {"transactions": []}
    scalars = ["issuer", "account_last4", "statement_period_start", "statement_period_end",
               "currency", "opening_balance", "closing_balance", "total_debits", "total_credits"]
    for p in pages:
        out["transactions"].extend(p.get("transactions", []))
        for k in scalars:
            if out.get(k) is None and p.get(k) is not None:
                out[k] = p[k]
    return out


def process(path: Path, passwords: dict, dry_run: bool, client, extract_fn) -> Result:
    r = Result(name=path.name)

    pages, err = read_pdf(path, passwords.get(path.name))
    if err:
        r.verdict = "ERROR"
        r.detail = err
        return r

    r.pages = len(pages)
    r.scanned_pages = sum(1 for p in pages if p.looks_scanned)
    if r.scanned_pages:
        r.warnings.append(f"{r.scanned_pages}/{r.pages} page(s) have no text layer — needs OCR")

    if dry_run:
        dump = OUT / f"{path.stem}.txt"
        write_text(dump, "\n\n".join(f"===== page {p.number} =====\n{p.text}" for p in pages))
        r.txns = count_txn_shaped_lines(pages)
        if r.txns:
            r.verdict = "TABLE-OK"
            r.detail = f"{r.txns} transaction-shaped rows survived — see {dump.name}"
        else:
            r.verdict = "NO-TABLE"
            r.detail = f"no date+amount rows found — inspect {dump.name} before spending API calls"
        return r

    hint = ""
    extracted = []
    for p in pages:
        if p.looks_scanned:
            continue
        # A local model can sit on one page for minutes. Without this, a slow
        # run and a hung one look identical and you can't tell which you have.
        print(f"  {path.name} page {p.number}/{len(pages)} ...", end="", flush=True)
        started = time.monotonic()
        page_data = extract_fn(client, p, hint)
        elapsed = time.monotonic() - started
        print(f" {len(page_data.get('transactions', [])):>3} txns  {elapsed:6.1f}s")
        extracted.append(page_data)
        # Carry the period forward so later pages can resolve bare day/month dates.
        if not hint:
            s, e = page_data.get("statement_period_start"), page_data.get("statement_period_end")
            if s and e:
                hint = f" Statement period is {s} to {e}."

    stmt = merge(extracted)
    r.txns = len(stmt["transactions"])
    r.data = stmt
    reconcile(stmt, r)
    sanity_checks(stmt, r)

    write_text(OUT / f"{path.stem}.json", json.dumps(stmt, indent=2, ensure_ascii=False))
    return r


def main() -> int:
    # Windows consoles default to cp1252, which blows up on the symbols banks
    # like to put in statements (✈, é, —). Never let a printable character
    # crash the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="extract text only; nothing leaves this machine")
    ap.add_argument("--file", help="process a single PDF instead of the whole folder")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    STATEMENTS.mkdir(parents=True, exist_ok=True)

    pw_file = HERE / "passwords.json"
    passwords = json.loads(read_text(pw_file)) if pw_file.exists() else {}

    files = [Path(args.file)] if args.file else sorted(STATEMENTS.glob("*.pdf"))
    if not files:
        print(f"No PDFs in {STATEMENTS}/ — drop statements there and re-run.")
        return 1

    client, extract_fn = (None, None)
    if not args.dry_run:
        client, extract_fn = build_client()
        provider = os.environ.get("SPIKE_PROVIDER", "anthropic").lower()
        where = {
            "ollama": os.environ.get("SPIKE_BASE_URL", "http://localhost:11434"),
            "openai": os.environ.get("SPIKE_BASE_URL", "?"),
        }.get(provider, "api.anthropic.com")

        # Easy mistake: set SPIKE_PROVIDER but forget SPIKE_MODEL, and the
        # Claude default gets sent to Ollama, which fails with a confusing
        # "model not found" rather than telling you what actually went wrong.
        if provider != "anthropic" and MODEL.startswith("claude-"):
            raise SystemExit(
                f"SPIKE_PROVIDER={provider} but SPIKE_MODEL is still {MODEL!r}.\n"
                f"Set SPIKE_MODEL to a model that provider serves."
            )

        print(f"Extracting with {MODEL} via {provider} @ {where}")

    results = [process(f, passwords, args.dry_run, client, extract_fn) for f in files]

    width = max(len(r.name) for r in results)
    print(f"\n{'statement'.ljust(width)}  {'pages':>5} {'rows':>5}  verdict     detail")
    print("-" * (width + 60))
    for r in results:
        print(f"{r.name.ljust(width)}  {r.pages:>5} {r.txns:>5}  {r.verdict:<11} {r.detail}")
        for w in r.warnings:
            print(f"{' ' * width}         !  {w}")

    passed = sum(1 for r in results if r.verdict == "PASS")
    checkable = sum(1 for r in results if r.verdict in ("PASS", "FAIL"))
    if checkable:
        print(f"\n{passed}/{checkable} verifiable statements reconciled.")
    print(f"JSON + text output in {OUT.relative_to(HERE.parent)}/\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
