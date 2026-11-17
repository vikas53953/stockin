"""The local web app: `stockin web` serves it on 127.0.0.1 only.

Three pages, all drawn from the same objects the CLI prints:
  /            holdings by account, the reconcile summary, an upload form
  /import      POST a CAS (PDF + password, or casparser JSON); shows the report
  /reconcile   every check, mismatches first

Nothing here can place an order. The only write is importing a statement,
through the same `cas.read` -> `LedgerWriter.ingest` path as the CLI. The
upload is written to a private temp file, read once and deleted; the
password is passed to casparser and never stored, logged or echoed.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from .book import Book, Check, Holding, ReconcileReport, reconcile
from .sources import cas
from .store import ImportReport, Ledger, LedgerWriter

MAX_UPLOAD = 20 * 1024 * 1024  # a CAS PDF is well under 5 MB

_env = Environment(
    loader=PackageLoader("stockin", "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class Row:
    isin: str
    name: str
    qty: str
    cost: str | None


@dataclass(frozen=True)
class AccountGroup:
    label: str
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class CheckRow:
    status: str
    kind: str
    on: str
    isin: str
    name: str
    account: str
    stated: str
    computed: str


def _groups(book: Book, holdings: tuple[Holding, ...]) -> tuple[AccountGroup, ...]:
    by_account: dict[str, list[Row]] = {}
    for h in sorted(holdings, key=lambda h: (h.account, h.isin)):
        acct = book.account(h.account)
        inst = book.instrument(h.isin)
        by_account.setdefault(acct.masked if acct else h.account, []).append(
            Row(
                isin=h.isin,
                name=inst.name if inst else h.isin,
                qty=str(h.qty),
                cost=str(h.cost) if h.cost is not None else None,
            )
        )
    return tuple(AccountGroup(label=k, rows=tuple(v)) for k, v in by_account.items())


def _check_rows(book: Book, report: ReconcileReport) -> tuple[CheckRow, ...]:
    order = {"mismatch": 0, "inferred": 1, "match": 2}

    def row(c: Check) -> CheckRow:
        acct = book.account(c.account)
        inst = book.instrument(c.isin)
        return CheckRow(
            status=c.status,
            kind=c.kind,
            on=c.on.isoformat(),
            isin=c.isin,
            name=inst.name if inst else c.isin,
            account=acct.masked if acct else c.account,
            stated=str(c.stated),
            computed=str(c.computed),
        )

    ranked = sorted(report.checks, key=lambda c: (order[c.status], c.on, c.isin))
    return tuple(row(c) for c in ranked)


def _summary(report: ReconcileReport) -> dict[str, int]:
    return {s: sum(c.status == s for c in report.checks) for s in ("match", "inferred", "mismatch")}


def create_app(db: Path, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Refuse any Host but our own, so a web page elsewhere cannot reach
        this app through DNS rebinding, and refuse cross-site form posts."""
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("stockin web only answers on this computer", 403)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin not in (
            None,
            *(f"http://{h}" for h in allowed_hosts),
        ):
            return PlainTextResponse("cross-site request refused", 403)
        return await call_next(request)

    def page(name: str, **ctx: object) -> HTMLResponse:
        return HTMLResponse(_env.get_template(name).render(**ctx))

    def read_book() -> Book | None:
        if not db.is_file():
            return None
        with Ledger.open(db) as ledger:
            return ledger.book()

    @app.get("/", response_class=HTMLResponse)
    def home() -> Response:
        book = read_book()
        if book is None:
            return page("home.html", groups=(), summary=None, empty=True)
        report = reconcile(book)
        return page(
            "home.html",
            groups=_groups(book, book.holdings()),
            summary=_summary(report),
            ok=report.ok,
            empty=False,
        )

    @app.get("/reconcile", response_class=HTMLResponse)
    def reconcile_page() -> Response:
        book = read_book()
        if book is None:
            return page("reconcile.html", checks=(), summary=None, ok=True)
        report = reconcile(book)
        return page(
            "reconcile.html",
            checks=_check_rows(book, report),
            summary=_summary(report),
            ok=report.ok,
        )

    @app.post("/import", response_class=HTMLResponse)
    async def import_statement(
        statement: Annotated[UploadFile, File()],
        password: Annotated[str, Form()] = "",
    ) -> Response:
        filename = Path(statement.filename or "statement").name
        suffix = ".json" if filename.lower().endswith(".json") else ".pdf"
        body = await statement.read(MAX_UPLOAD + 1)
        if len(body) > MAX_UPLOAD:
            return page(
                "import.html",
                error="That file is over 20 MB; a CAS is much smaller.",
                filename=filename,
                report=None,
                groups=(),
            )
        if not body:
            return page(
                "import.html",
                error="That file is empty.",
                filename=filename,
                report=None,
                groups=(),
            )
        db.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=db.parent, prefix=".upload-") as tmp:
            path = Path(tmp) / f"upload{suffix}"
            path.write_bytes(body)
            path.chmod(0o600)
            try:
                st = cas.read(path, password)
            except cas.CasRejected as exc:
                why = str(exc).replace(path.name, filename)
                return page(
                    "import.html",
                    error=f"Not imported: {why}",
                    filename=filename,
                    report=None,
                    groups=(),
                )
        with LedgerWriter.open(db) as writer:
            report: ImportReport = writer.ingest(st)
            groups = _groups(writer.book(), report.holdings)
        return page("import.html", error=None, filename=filename, report=report, groups=groups)

    return app
