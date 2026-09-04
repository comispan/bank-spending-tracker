"""Spending tracker — Phase 1: one statement, end to end.

Upload a statement PDF, parse it, verify it against the statement's own printed
figures, and show the result. No categories yet (Phase 2), no cross-statement
report yet (Phase 3).

Parsing happens inside the request. It measured 182 ms/page in Phase 0, so a
four-page statement is under a second — the queue, worker and job-status polling
in the original design existed to hide a 10-60s LLM round-trip that no longer
happens.

    uvicorn main:app --reload --port 8000    (from the app/ directory)
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent))

import categorize  # noqa: E402
import db          # noqa: E402
import merchants   # noqa: E402
import parsing     # noqa: E402
import tier3       # noqa: E402

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app = FastAPI(title="Spending Tracker")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.on_event("startup")
def startup() -> None:
    db.init()
    # Merchant keys and categories are derived, not entered, so a change to
    # merchants.py or categorize.py is allowed to re-key and re-resolve what is
    # already stored — the alternative is old statements silently keeping rules
    # that no longer exist. Counts, never descriptions: DESIGN.md Section 7.
    with db.connect() as conn:
        periods = db.backfill_periods(conn)
        foreign = db.backfill_foreign_amounts(conn)
        parse_flags = db.backfill_parse_flags(conn)
        descriptions = db.backfill_descriptions(conn)
        moved = db.renormalize_merchants(conn)
        seeded = db.seed_memory(conn)
        recategorized = db.recategorize_all(conn)
        conn.commit()
    for label, n in (("statement periods re-read", periods),
                     ("foreign charges split", foreign),
                     ("row parse-flags replayed", parse_flags),
                     ("descriptions replayed", descriptions),
                     ("merchant keys recomputed", moved),
                     ("merchants seeded", seeded),
                     ("transactions recategorized", recategorized)):
        if n:
            print(f"{label}: {n}")


def money(minor: int | None) -> str:
    if minor is None:
        return "—"
    return f"{minor / 100:,.2f}"


def period(s) -> str:
    """What the statement covers, in descending order of how much we know.

    An empty period column tells the reader nothing and hides which case they
    are in. Most issuers print a closing date rather than a range; where even
    that is absent, the span of the parsed rows is a floor, not the cycle, and
    is labelled as such.
    """
    if s["period_start"] and s["period_end"]:
        return f'{s["period_start"]} → {s["period_end"]}'
    if s["statement_date"]:
        return f'to {s["statement_date"]}'
    first, last = s["first_txn"], s["last_txn"]
    if first and last:
        return f"{first} → {last} (from rows)"
    return "—"


templates.env.filters["money"] = money
templates.env.filters["period"] = period


# ------------------------------------------------------------------- views

# Which way a column runs on its first click. A name reads A-Z and a date reads
# newest first, so inheriting the direction from whichever column was clicked
# last would make one of the two open backwards.
SORT_FIRST_CLICK_DESC = {"newest": True, "statement": False, "period": True}


def sort_headers(sort: str, descending: bool) -> dict[str, dict[str, object]]:
    """Link target and arrow for each sortable column on the statement list.

    Clicking the active column flips it; clicking any other starts that column
    at its own natural direction rather than carrying over the current one.

    `newest` has no header of its own — it sorts by period *end*, where the
    Period column sorts by start, and the two are not the same ordering even
    though this corpus makes them look close. It is no longer the default, but
    an old bookmark can still ask for it, and when it does no arrow shows at
    all rather than pointing the Period header at an ordering it does not
    describe.
    """
    out: dict[str, dict[str, object]] = {}
    for key, first_click_desc in SORT_FIRST_CLICK_DESC.items():
        active = key == sort
        nxt = not descending if active else first_click_desc
        out[key] = {
            "href": f"/?sort={key}&direction={'desc' if nxt else 'asc'}",
            "active": active,
            "arrow": ("▾" if descending else "▴") if active else "",
        }
    return out


@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str | None = None, notice: str | None = None,
          sort: str = "statement", direction: str = ""):
    """The upload form and the statement list (Section 8, Phase 1 step 4).

    The list opens grouped by card, A-Z: `statement` ascending, which is the
    column's own natural direction, so the default and a first click on the
    Statement header land on the same order.

    `sort` and `direction` come from a URL, so both are checked against a fixed
    set here and an unknown value falls back to the default — a stale bookmark
    should render the list, not a 422.
    """
    if sort not in db.STATEMENT_ORDERS:
        sort = "statement"
    descending = (direction != "asc") if direction else SORT_FIRST_CLICK_DESC[sort]
    with db.connect() as conn:
        statements = db.list_statements(conn, sort, descending)
    return templates.TemplateResponse(
        request, "index.html",
        {"statements": statements, "error": error, "notice": notice,
         "sort": sort, "descending": descending,
         "h": sort_headers(sort, descending)},
    )


class Ingested(NamedTuple):
    """The outcome of trying to file one uploaded PDF.

    `kind` is one of "stored", "duplicate", "rejected". `statement_id` is set
    for "stored" and "duplicate" (the existing row, in that case). `message` is
    a human phrase already prefixed with the filename, ready to drop into a
    notice or an error line.
    """
    kind: str
    statement_id: int | None
    message: str


def ingest_statement(payload: bytes, filename: str, password: str | None) -> Ingested:
    """Validate, parse and file one statement PDF — the shared body of both
    upload routes.

    Nothing is written to disk or the database until the file parses, so a
    reject leaves no orphan. The single-file and bulk routes differ only in how
    they surface the `Ingested` this returns.
    """
    name = filename or "file"
    if not filename or not filename.lower().endswith(".pdf"):
        return Ingested("rejected", None, f"{name}: only PDF files can be uploaded")
    if not payload:
        return Ingested("rejected", None, f"{name}: that file was empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        return Ingested("rejected", None, f"{name}: over 25MB")

    digest = parsing.sha256(payload)
    with db.connect() as conn:
        existing = db.statement_by_hash(conn, digest)
    if existing:
        # The cheapest duplicate to catch, and the most common one: the same
        # file uploaded twice. Caught before parsing rather than after.
        return Ingested("duplicate", existing["id"],
                        f"{name}: already uploaded as {existing['filename']}")

    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / "upload.pdf"
    tmp.write_bytes(payload)
    try:
        result = parsing.parse(tmp, filename, password or None)
    finally:
        tmp.unlink(missing_ok=True)
        tmp_dir.rmdir()

    if result.error:
        return Ingested("rejected", None, f"{name}: {result.error}")

    stored = db.STATEMENT_DIR / f"{digest[:16]}.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)

    stmt = result.statement
    with db.connect() as conn:
        account_id = db.find_or_create_account(
            conn, result.issuer, stmt.get("account_last4"), stmt.get("currency") or "SGD")
        statement_id = db.insert_statement(conn, account_id, {
            "filename": filename,
            "file_sha256": digest,
            "storage_path": str(stored),
            "period_start": stmt.get("statement_period_start"),
            "period_end": stmt.get("statement_period_end"),
            "statement_date": result.statement_date,
            "opening_balance_minor": db.to_minor(stmt.get("opening_balance")),
            "closing_balance_minor": db.to_minor(stmt.get("closing_balance")),
            "page_count": result.page_count,
            "scanned_pages": result.scanned_pages,
            "parser_version": parsing.PARSER_VERSION,
            "status": result.status,
            "verdict": result.verdict,
            "verdict_detail": result.detail,
            "rows_expected": result.rows_expected,
            "rows_parsed": len(result.transactions),
            "warnings": result.warnings,
            "raw_extraction": stmt,
            "page_text": [p.text for p in result.pages],
        })
        currency = stmt.get("currency") or "SGD"
        db.insert_transactions(conn, statement_id, account_id, [
            {
                "txn_date": t["date"],
                "posted_date": t.get("posted_date"),
                "description_raw": t["description"],
                # The key tier 2 of the categorizer will look up (DESIGN.md
                # Section 3). Stored rather than computed on read so a query
                # can group by it, and recomputed on boot when the rules
                # change.
                "merchant_normalized": merchants.normalize(t["description"]),
                # Section 5's two money columns: what the statement printed,
                # and what it billed. They differ only on a foreign charge,
                # where the parser recovers the original figure and the
                # statement's *own* printed rate (Section 4) — never today's
                # rate, because that is not the rate the money changed at. Both
                # figures come off the same line, so the SGD side is what
                # reconciles either way.
                "amount_minor": db.to_minor(
                    (t.get("foreign") or {}).get("amount") or t["amount"]),
                "currency": (t.get("foreign") or {}).get("currency") or currency,
                "amount_sgd_minor": db.to_minor(t["amount"]),
                "fx_rate": t.get("fx_rate"),
                "direction": t["direction"],
                "source_page": t.get("source_page"),
                "reference": t.get("reference"),
                # A parse-time note (rows.py): the row carried more than one
                # amount, so this figure may be a running balance. Surfaced per
                # row on the statement page so the aggregate warning is
                # traceable.
                "amount_ambiguous": t.get("amount_ambiguous", False),
                # Same reasoning as the duplicate warning in parsing.py: without
                # the issuer's reference, two real charges collide here and a
                # future cross-statement check would mark one a duplicate of the
                # other. Empty when the statement printed none, which leaves the
                # key exactly as it was.
                "dedup_key": "|".join(
                    [t["date"], t["amount"], t["description"].lower(), t.get("reference") or ""]),
            }
            for t in result.transactions
        ])
        # Resolve through the same pass the boot uses rather than a second copy
        # of the tier order living here — one resolution path, always.
        db.recategorize_all(conn)
        conn.commit()

    return Ingested("stored", statement_id, f"{name}: filed")


@app.post("/upload")
async def upload(file: UploadFile, password: str = Form("")):
    payload = await file.read()
    out = ingest_statement(payload, file.filename or "", password or None)
    if out.kind == "duplicate":
        return RedirectResponse(
            f"/statements/{out.statement_id}?notice=Already+uploaded+"
            f"{(file.filename or '').replace(' ', '+')}", status_code=303)
    if out.kind == "rejected":
        return redirect("/", error=out.message)
    return RedirectResponse(f"/statements/{out.statement_id}", status_code=303)


@app.post("/upload-bulk")
async def upload_bulk(files: list[UploadFile]):
    """File a whole folder of statements in one go (Section 8).

    No password field: a bulk run cannot stop to prompt for each locked file,
    so any encrypted PDF is reported as rejected and the rest still go through.
    Decrypt those first and upload them singly. Every file is processed
    independently — one bad PDF never blocks the others — and the redirect
    carries a per-file summary so a silent partial success is impossible.
    """
    stored, duplicate, failures = [], 0, []
    for f in files:
        payload = await f.read()
        out = ingest_statement(payload, f.filename or "", None)
        if out.kind == "stored":
            stored.append(out.statement_id)
        elif out.kind == "duplicate":
            duplicate += 1
        else:
            failures.append(out.message)

    if not files or (not stored and not duplicate and not failures):
        return redirect("/", error="No files were selected")

    parts = []
    if stored:
        parts.append(f"{len(stored)} statement(s) filed")
    if duplicate:
        parts.append(f"{duplicate} already on file, skipped")
    notice = ", ".join(parts) if parts else None
    error = None
    if failures:
        error = f"{len(failures)} file(s) not filed — " + "; ".join(failures)
    return redirect("/", notice=notice, error=error)


def row_parse_notes(txns: list) -> dict[int, list[dict[str, str]]]:
    """Which rows each row-level warning on a statement is actually about.

    The statement's `warnings` are printed as aggregate counts ("41 row(s)
    carried more than one amount"); this maps them back onto the rows so the
    table can mark them. Two kinds:

    * `amount_ambiguous` — a parse-time flag stored on the row (rows.py): the
      line carried more than one amount, so the figure may be a running balance.
    * identical rows — recomputed here from `dedup_key`, which is exactly the
      tuple `sanity_checks` counts, so this names the same rows the warning does.
    """
    notes: dict[int, list[dict[str, str]]] = {}

    def add(txn_id: int, tag: str, detail: str) -> None:
        notes.setdefault(txn_id, []).append({"tag": tag, "detail": detail})

    for t in txns:
        if t["amount_ambiguous"]:
            add(t["id"], "multi-amount",
                "the row carried more than one amount — the figure shown may be "
                "a running balance, not the transaction")

    by_key: dict[str, list[int]] = {}
    for t in txns:
        if t["dedup_key"]:
            by_key.setdefault(t["dedup_key"], []).append(t["id"])
    for ids in by_key.values():
        if len(ids) > 1:
            for txn_id in ids:
                add(txn_id, "repeat",
                    f"identical date, amount and description to {len(ids) - 1} "
                    f"other row(s) here — a genuine repeat, or a page read twice")

    return notes


@app.get("/statements/{statement_id}", response_class=HTMLResponse)
def statement_detail(request: Request, statement_id: int,
                     notice: str | None = None, error: str | None = None):
    with db.connect() as conn:
        stmt = db.get_statement(conn, statement_id)
        if not stmt:
            return RedirectResponse("/?error=No+such+statement", status_code=303)
        txns = db.transactions_for(conn, statement_id)
        totals = db.totals_for(conn, statement_id)
    return templates.TemplateResponse(request, "statement.html", {
        "s": stmt, "txns": txns, "totals": totals,
        "categories": categorize.CATEGORIES, "flow_types": categorize.FLOW_TYPES,
        "warnings": json.loads(stmt["warnings"]), "notes": row_parse_notes(txns),
        "notice": notice, "error": error,
    })


@app.get("/statements/{statement_id}/review", response_class=HTMLResponse)
def review(request: Request, statement_id: int, page: int = 1):
    """Parsed rows beside the text the parser actually saw.

    Deliberately the extracted text rather than a picture of the page: when a
    row is missing, the question is always what the parser was looking at, and
    a rendered image cannot answer that.
    """
    with db.connect() as conn:
        stmt = db.get_statement(conn, statement_id)
        if not stmt:
            return RedirectResponse("/?error=No+such+statement", status_code=303)
        txns = db.transactions_for(conn, statement_id)

    page_text = json.loads(stmt["page_text"])
    page = max(1, min(page, len(page_text) or 1))
    return templates.TemplateResponse(request, "review.html", {
        "s": stmt,
        "page": page,
        "page_count": len(page_text),
        "text": page_text[page - 1] if page_text else "",
        "txns": [t for t in txns if t["source_page"] == page],
        "warnings": json.loads(stmt["warnings"]),
        # Computed over the whole statement, not just this page: a row read
        # twice can land its copy on another page.
        "notes": row_parse_notes(txns),
    })


@app.get("/statements/{statement_id}/pdf")
def statement_pdf(statement_id: int):
    with db.connect() as conn:
        stmt = db.get_statement(conn, statement_id)
    if not stmt:
        return Response(status_code=404)
    path = Path(stmt["storage_path"])
    if not path.exists():
        return Response(status_code=404)
    return Response(
        path.read_bytes(), media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{stmt["filename"]}"'},
    )


@app.post("/statements/{statement_id}/delete")
def delete(statement_id: int):
    with db.connect() as conn:
        stored = db.delete_statement(conn, statement_id)
        conn.commit()
    if stored:
        # Hard delete means the PDF goes too, per DESIGN.md Section 7.
        Path(stored).unlink(missing_ok=True)
    return RedirectResponse("/?notice=Statement+deleted", status_code=303)


@app.post("/statements/delete-all")
def delete_all():
    """Purge every statement at once (Section 7 hard delete).

    Same effect as deleting each statement in turn: transactions and PDFs go,
    accounts and merchant memory stay. Guarded by a confirm dialog in the UI
    because there is no undo.
    """
    with db.connect() as conn:
        stored = db.delete_all_statements(conn)
        conn.commit()
    for path in stored:
        Path(path).unlink(missing_ok=True)
    n = len(stored)
    return RedirectResponse(
        f"/?notice={n}+statement(s)+deleted", status_code=303)


@app.get("/transactions", response_class=HTMLResponse)
def all_transactions(request: Request, error: str | None = None, notice: str | None = None,
                     uncategorized: int = 0, month: str | None = None,
                     category: str | None = None, account: int | None = None,
                     merchant: str | None = None, flow: str | None = None,
                     page: int = 1):
    """The rows themselves, and where every figure in the report drills through to.

    Section 4 asks that every number trace back to the transactions behind it,
    which makes this page the other end of four different links. The filters
    are therefore shown, not just applied: a filtered list that looks identical
    to the whole list is how a total gets read as the total when it is a slice.

    The rows are paged — a category control per row makes the unpaged page
    multi-megabyte once the corpus is a few thousand rows — but `total` and the
    net-spend figure are computed over the whole filtered slice by
    `db.transaction_page`, so a report figure still reconciles against what the
    page says even when the slice runs to many pages.
    """
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        month = None
    if category and category not in categorize.CATEGORIES and category != "(uncategorized)":
        category = None
    if flow and flow not in categorize.FLOW_TYPES:
        flow = None
    with db.connect() as conn:
        result = db.transaction_page(
            conn, uncategorized=bool(uncategorized), month=month, category=category,
            account=account, merchant=merchant, flow=flow, page=page)
        stats = db.coverage(conn)
        account_name = conn.execute(
            """SELECT issuer || CASE WHEN last4 IS NULL THEN '' ELSE ' ····' || last4 END
                 AS label FROM account WHERE id = ?""", (account,)).fetchone() if account else None
    active = [("month", month, month), ("category", category, category),
              ("account", account, account_name["label"] if account_name else None),
              ("merchant", merchant, merchant), ("flow", flow, flow)]
    return templates.TemplateResponse(request, "transactions.html", {
        "txns": result["rows"], "total": result["total"],
        "page": result["page"], "pages": result["pages"], "per_page": result["per_page"],
        "stats": stats, "uncategorized_only": bool(uncategorized),
        "filters": [(k, v, label) for k, v, label in active if v],
        # Filters only — the page number is added to links separately, so a
        # filter change always lands back on page 1 rather than off the end.
        "query": urlencode({k: v for k, v in
                            [("month", month), ("category", category), ("account", account),
                             ("merchant", merchant), ("flow", flow),
                             ("uncategorized", uncategorized or None)] if v}),
        "shown_minor": result["shown_minor"],
        "categories": categorize.CATEGORIES, "flow_types": categorize.FLOW_TYPES,
        "error": error, "notice": notice,
    })


def redirect(path: str, **params: str | None) -> RedirectResponse:
    """A 303 back to `path` carrying notice/error text.

    Properly encoded rather than the `.replace(' ', '+')` used by the older
    routes, because the messages here can carry an API error body — colons,
    quotes, ampersands and all — and one stray `&` would silently truncate the
    message the user needs to read.
    """
    query = urlencode({k: v for k, v in params.items() if v})
    joiner = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{joiner}{query}" if query else path, status_code=303)


def safe_back(back: str) -> str:
    """Only ever redirect within this app.

    The value comes from a form field, so it is user input even on a
    single-user localhost app, and `//evil.example` is a same-looking path that
    is not one.
    """
    return back if back.startswith("/") and not back.startswith("//") else "/transactions"


@app.post("/transactions/{txn_id}/category")
def recategorize(txn_id: int, category: str = Form(""), flow_type: str = Form("spend"),
                 apply_all: str = Form(""), back: str = Form("/transactions")):
    """One row's category and flow, and optionally every row like it (Section 3)."""
    target = safe_back(back)
    if category and category not in categorize.CATEGORIES:
        return RedirectResponse(f"{target}?error=Unknown+category", status_code=303)
    if flow_type not in categorize.FLOW_TYPES:
        return RedirectResponse(f"{target}?error=Unknown+flow+type", status_code=303)

    with db.connect() as conn:
        changed, merchant = db.set_category(
            conn, txn_id, category or None, flow_type, bool(apply_all))
        if not changed:
            return RedirectResponse(f"{target}?error=No+such+transaction", status_code=303)
        # The memory entry moved, so anything resolving through it re-resolves.
        db.recategorize_all(conn)
        conn.commit()

    others = changed - 1
    note = f"Categorized{f' and applied to {others} matching row(s)' if others else ''}"
    return RedirectResponse(f"{target}?notice={note.replace(' ', '+')}#t{txn_id}",
                            status_code=303)


@app.get("/months", response_class=HTMLResponse)
def month_list(request: Request, error: str | None = None, notice: str | None = None):
    """Spending by calendar month, with how much of each month is actually billed.

    The completeness line is not decoration. Without it the newest month reads
    as a collapse in spending when it is simply half unbilled — every card in
    the corpus closes mid-month, so this is the normal state of the newest row,
    not an edge case.
    """
    with db.connect() as conn:
        report = db.month_report(conn)
        coverage = db.coverage_by_account(conn)
    return templates.TemplateResponse(request, "months.html", {
        "report": report, "coverage": coverage, "error": error, "notice": notice,
    })


@app.get("/months/{ym}", response_class=HTMLResponse)
def month_view(request: Request, ym: str):
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        return RedirectResponse("/months?error=Not+a+month", status_code=303)
    with db.connect() as conn:
        row = next((m for m in db.month_report(conn) if m["month"] == ym), None)
        if not row:
            return RedirectResponse("/months?error=No+transactions+that+month", status_code=303)
        detail = db.month_detail(conn, ym)
        notable = db.month_notable(conn, ym, row["trailing_months"], row["comparable_days"])
        cards = {r["label"]: r["id"] for r in conn.execute(
            """SELECT id, issuer || CASE WHEN last4 IS NULL THEN '' ELSE ' ····' || last4 END
                 AS label FROM account""")}
    biggest = max((r["v"] for r in detail["by_category"]), default=0)
    return templates.TemplateResponse(request, "month.html", {
        "m": row, "d": detail, "biggest": biggest, "notable": notable, "cards": cards,
    })


@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    """The month page's breakdowns, side by side across every complete month.

    Only complete months (every card billed for all of it) are compared — the
    part-billed ones are named and left out rather than dropped into the grid
    as short columns that read as a dip in spending.
    """
    with db.connect() as conn:
        data = db.analytics(conn)
        cards = {r["label"]: r["id"] for r in conn.execute(
            """SELECT id, issuer || CASE WHEN last4 IS NULL THEN '' ELSE ' ····' || last4 END
                 AS label FROM account""")} if data["enough"] else {}
    return templates.TemplateResponse(request, "analytics.html", {"a": data, "cards": cards})


@app.get("/merchants", response_class=HTMLResponse)
def merchant_sweep(request: Request, all: int = 0,
                   error: str | None = None, notice: str | None = None):
    """Categorize by merchant instead of by transaction.

    The fastest route to a complete report, and it needs no model: the rows that
    are uncategorized are far fewer merchants than they look, so one decision
    here settles every past and future transaction from that merchant at once.
    """
    with db.connect() as conn:
        entries = db.merchant_summary(conn, unknown_only=not all)
        stats = db.coverage(conn)
        # Always the undecided set, whatever the screen is currently showing:
        # `?all=1` lists merchants that already have a category, and asking a
        # model about those would spend money to re-answer settled questions.
        unknown_keys = [e["key"] for e in db.merchant_summary(conn, unknown_only=True)]
    return templates.TemplateResponse(request, "merchants.html", {
        "entries": entries, "stats": stats, "show_all": bool(all),
        "categories": categorize.CATEGORIES,
        "unknown_value_minor": sum(e["value_minor"] or 0 for e in entries if e["unknown"]),
        "tier3_ready": tier3.configured(),
        "tier3_model": tier3.model_name(),
        # Part of the same disclosure: with search on, the names reach Google
        # as queries and not only the model, and the screen has to say so
        # before the button is pressed rather than after.
        "tier3_grounding": tier3.grounding_enabled(),
        # The literal request body, so Section 9.4's disclosure is something
        # the user can read rather than a promise they have to take on trust.
        "tier3_payload": tier3.prompt_payload(unknown_keys),
        "tier3_count": len(unknown_keys),
        "error": error, "notice": notice,
    })


@app.post("/merchants/tier3")
def run_tier3(all: int = Form(0)):
    """Ask the model about the merchants nothing else knows (Section 3, tier 3).

    Explicitly triggered rather than automatic on upload, and that is Section
    9.4 rather than caution for its own sake: this is the only outbound request
    the app makes, so it happens when the user presses the button and not as a
    side effect of filing a statement.

    Results are written as guesses, never as decisions. What comes back is
    reported in full — categorized, abstained, and any batch the gate threw
    away — because a tier that quietly did nothing looks exactly like a tier
    that had nothing to do.
    """
    back = f"/merchants{'?all=1' if all else ''}"

    with db.connect() as conn:
        keys = [e["key"] for e in db.merchant_summary(conn, unknown_only=True)]
    if not keys:
        return redirect(back, notice="Nothing to ask about — every merchant is categorized")
    if not tier3.configured():
        return redirect(back, error="No Gemini API key. Set GEMINI_API_KEY in the "
                                    "environment or in a .env file at the project root.")

    result = tier3.classify(keys)
    assignments = result["assignments"]

    with db.connect() as conn:
        stored = db.remember_llm(conn, assignments)
        moved = db.recategorize_all(conn)
        conn.commit()

    searched = " with Google Search" if result["grounding"] else ""
    parts = [f"{stored} merchant(s) categorized by {tier3.model_name()}{searched}",
             f"{moved} transaction(s) moved"]
    if result["abstained"]:
        parts.append(f"{len(result['abstained'])} left unknown by the model")
    notice = ", ".join(parts)

    # A discarded batch is the failure this whole tier is designed around, so it
    # is reported as an error next to the notice rather than folded into it —
    # those merchants are still uncategorized and the user needs to know that
    # rather than assume the run covered everything.
    error = None
    if result["problems"]:
        error = (f"{result['batches'] - result['batches_ok']} of {result['batches']} "
                 f"batch(es) discarded, nothing stored from them: "
                 + "; ".join(result["problems"][:3]))
    return redirect(back, notice=notice, error=error)


@app.post("/merchants")
def save_merchant_categories(key: list[str] = Form(default=[]),
                             category: list[str] = Form(default=[]),
                             all: int = Form(0)):
    """Apply a whole screen of merchant decisions at once.

    The two lists arrive index-matched because every row emits exactly one
    hidden `key` and one `category`, in document order. Blank categories mean
    "left alone" and are dropped here rather than being written as nulls.
    """
    pairs = [(k, c) for k, c in zip(key, category) if k and c]
    unknown = sorted({c for _, c in pairs if c not in categorize.CATEGORIES})
    if unknown:
        return RedirectResponse("/merchants?error=Unknown+category", status_code=303)
    if not pairs:
        return RedirectResponse("/merchants?notice=Nothing+to+save", status_code=303)

    with db.connect() as conn:
        for merchant, chosen in pairs:
            db.remember(conn, merchant, chosen)
        # Resolution runs through the one existing path, so a merchant decided
        # here behaves exactly like one decided from a transaction row.
        moved = db.recategorize_all(conn)
        conn.commit()

    note = f"{len(pairs)} merchant(s) saved, {moved} transaction(s) categorized"
    return RedirectResponse(
        f"/merchants{'?all=1&' if all else '?'}notice={note.replace(' ', '+')}",
        status_code=303)


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request, error: str | None = None, notice: str | None = None):
    with db.connect() as conn:
        return templates.TemplateResponse(request, "rules.html", {
            "rules": db.merchant_rules(conn),
            "memory": db.list_memory(conn),
            "stats": db.coverage(conn),
            "categories": categorize.CATEGORIES, "flow_types": categorize.FLOW_TYPES,
            "error": error, "notice": notice,
        })


@app.post("/rules")
def add_rule(pattern: str = Form(""), match_type: str = Form("contains"),
             category: str = Form(""), flow_type: str = Form(""),
             priority: int = Form(0)):
    pattern = pattern.strip()
    if not pattern:
        return RedirectResponse("/rules?error=A+rule+needs+a+pattern", status_code=303)
    if match_type not in ("exact", "contains", "regex"):
        return RedirectResponse("/rules?error=Unknown+match+type", status_code=303)
    if not category and not flow_type:
        return RedirectResponse(
            "/rules?error=A+rule+must+set+a+category+or+a+flow+type", status_code=303)
    if category and category not in categorize.CATEGORIES:
        return RedirectResponse("/rules?error=Unknown+category", status_code=303)
    if flow_type and flow_type not in categorize.FLOW_TYPES:
        return RedirectResponse("/rules?error=Unknown+flow+type", status_code=303)
    # A regex that does not compile would match nothing and say nothing about
    # why, which is the kind of silent no-op this app keeps refusing to ship.
    if match_type == "regex" and not categorize.valid_regex(pattern):
        return RedirectResponse("/rules?error=That+is+not+a+valid+regex", status_code=303)

    with db.connect() as conn:
        db.add_rule(conn, pattern, match_type, category, flow_type, priority)
        moved = db.recategorize_all(conn)
        conn.commit()
    return RedirectResponse(f"/rules?notice=Rule+added,+{moved}+row(s)+changed",
                            status_code=303)


@app.post("/rules/{rule_id}/delete")
def remove_rule(rule_id: int):
    with db.connect() as conn:
        db.delete_rule(conn, rule_id)
        moved = db.recategorize_all(conn)
        conn.commit()
    return RedirectResponse(f"/rules?notice=Rule+deleted,+{moved}+row(s)+changed",
                            status_code=303)


@app.post("/memory/forget")
def forget(merchant: str = Form("")):
    """Drop one learned merchant, seeded or taught.

    Worth having because a seeded guess is the app's opinion, not the user's,
    and there has to be a way to say "you were wrong about this one" that is
    not recategorizing every row by hand.
    """
    with db.connect() as conn:
        db.forget_merchant(conn, merchant)
        moved = db.recategorize_all(conn)
        conn.commit()
    return RedirectResponse(f"/rules?notice=Forgotten,+{moved}+row(s)+changed",
                            status_code=303)
