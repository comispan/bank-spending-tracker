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
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent))

import db          # noqa: E402
import parsing     # noqa: E402

HERE = Path(__file__).parent
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app = FastAPI(title="Spending Tracker")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.on_event("startup")
def startup() -> None:
    db.init()


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
        conn.commit()

    return RedirectResponse(f"/statements/{statement_id}", status_code=303)


@app.get("/statements/{statement_id}", response_class=HTMLResponse)
def statement_detail(request: Request, statement_id: int, notice: str | None = None):
    with db.connect() as conn:
        stmt = db.get_statement(conn, statement_id)
        if not stmt:
            return RedirectResponse("/?error=No+such+statement", status_code=303)
        txns = db.transactions_for(conn, statement_id)
        totals = db.totals_for(conn, statement_id)
    return templates.TemplateResponse(request, "statement.html", {
        "s": stmt, "txns": txns, "totals": totals,
        "warnings": json.loads(stmt["warnings"]), "notice": notice,
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
def all_transactions(request: Request):
    with db.connect() as conn:
        txns = conn.execute(
            """SELECT t.*, a.issuer, a.last4 FROM txn t
               JOIN account a ON a.id = t.account_id
               ORDER BY t.txn_date DESC, t.id DESC"""
        ).fetchall()
    return templates.TemplateResponse(request, "transactions.html", {"txns": txns})
