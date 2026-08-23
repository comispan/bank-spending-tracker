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

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent))

import categorize  # noqa: E402
import db          # noqa: E402
import merchants   # noqa: E402
import parsing     # noqa: E402

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
    # that no longer exist. Counts, never descriptions: DESIGN.md §7.
    with db.connect() as conn:
        periods = db.backfill_periods(conn)
        moved = db.renormalize_merchants(conn)
        seeded = db.seed_memory(conn)
        recategorized = db.recategorize_all(conn)
        conn.commit()
    for label, n in (("statement periods re-read", periods),
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

@app.get("/", response_class=HTMLResponse)
def index(request: Request, error: str | None = None, notice: str | None = None):
    with db.connect() as conn:
        statements = db.list_statements(conn)
    return templates.TemplateResponse(
        request, "index.html",
        {"statements": statements, "error": error, "notice": notice},
    )


@app.post("/upload")
async def upload(file: UploadFile, password: str = Form("")):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/?error=Only+PDF+files+can+be+uploaded", status_code=303)

    payload = await file.read()
    if not payload:
        return RedirectResponse("/?error=That+file+was+empty", status_code=303)
    if len(payload) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/?error=That+file+is+over+25MB", status_code=303)

    digest = parsing.sha256(payload)
    with db.connect() as conn:
        existing = db.statement_by_hash(conn, digest)
    if existing:
        # The cheapest duplicate to catch, and the most common one: the same
        # file uploaded twice. Caught before parsing rather than after.
        return RedirectResponse(
            f"/statements/{existing['id']}?notice=Already+uploaded+"
            f"{existing['filename'].replace(' ', '+')}", status_code=303)

    # Parse from a temp copy. Nothing is stored until it parses, so a file that
    # cannot be read doesn't leave an orphan in the statements directory.
    tmp_dir = Path(tempfile.mkdtemp())
    tmp = tmp_dir / "upload.pdf"
    tmp.write_bytes(payload)
    try:
        result = parsing.parse(tmp, file.filename, password or None)
    finally:
        tmp.unlink(missing_ok=True)
        tmp_dir.rmdir()

    if result.error:
        return RedirectResponse(f"/?error={result.error.replace(' ', '+')}", status_code=303)

    stored = db.STATEMENT_DIR / f"{digest[:16]}.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)

    stmt = result.statement
    with db.connect() as conn:
        account_id = db.find_or_create_account(
            conn, result.issuer, stmt.get("account_last4"), stmt.get("currency") or "SGD")
        statement_id = db.insert_statement(conn, account_id, {
            "filename": file.filename,
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
                # The key tier 2 of the categorizer will look up (DESIGN.md §3).
                # Stored rather than computed on read so a query can group by it,
                # and recomputed on boot when the rules change.
                "merchant_normalized": merchants.normalize(t["description"]),
                "amount_minor": db.to_minor(t["amount"]),
                "currency": currency,
                # Statements bill in their own currency, so the printed amount
                # is already SGD here. A foreign-currency subline lives in the
                # description and is not yet split out — see DESIGN.md §4.
                "amount_sgd_minor": db.to_minor(t["amount"]),
                "direction": t["direction"],
                "source_page": t.get("source_page"),
                "reference": t.get("reference"),
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

    return RedirectResponse(f"/statements/{statement_id}", status_code=303)


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
        "warnings": json.loads(stmt["warnings"]), "notice": notice, "error": error,
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
        # Hard delete means the PDF goes too, per DESIGN.md §7.
        Path(stored).unlink(missing_ok=True)
    return RedirectResponse("/?notice=Statement+deleted", status_code=303)


@app.get("/transactions", response_class=HTMLResponse)
def all_transactions(request: Request, error: str | None = None, notice: str | None = None,
                     uncategorized: int = 0, month: str | None = None):
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        month = None
    with db.connect() as conn:
        clauses, args = [], []
        if uncategorized:
            clauses.append("t.category IS NULL")
        if month:
            clauses.append("substr(t.txn_date, 1, 7) = ?")
            args.append(month)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        txns = conn.execute(
            f"""SELECT t.*, a.issuer, a.last4 FROM txn t
                JOIN account a ON a.id = t.account_id
                {where}
                ORDER BY t.txn_date DESC, t.id DESC""", args
        ).fetchall()
        stats = db.coverage(conn)
    return templates.TemplateResponse(request, "transactions.html", {
        "txns": txns, "stats": stats, "uncategorized_only": bool(uncategorized),
        "month_filter": month,
        "categories": categorize.CATEGORIES, "flow_types": categorize.FLOW_TYPES,
        "error": error, "notice": notice,
    })


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
    """One row's category and flow, and optionally every row like it (§3)."""
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
    biggest = max((r["v"] for r in detail["by_category"]), default=0)
    return templates.TemplateResponse(request, "month.html", {
        "m": row, "d": detail, "biggest": biggest,
    })


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
    return templates.TemplateResponse(request, "merchants.html", {
        "entries": entries, "stats": stats, "show_all": bool(all),
        "categories": categorize.CATEGORIES,
        "unknown_value_minor": sum(e["value_minor"] or 0 for e in entries if e["unknown"]),
        "error": error, "notice": notice,
    })


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
