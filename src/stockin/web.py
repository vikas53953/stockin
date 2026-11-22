"""The local web app: `stockin web` serves it on 127.0.0.1 only.

Pages, all drawn from the same objects the CLI prints:
  /            your verdicts, the story, holdings by account, an upload form
  /thesis/ISIN write why you hold a holding, its target weight, or mark it broken
  /import      POST a CAS (PDF + password, or casparser JSON); shows the report
  /reconcile   every check, mismatches first
  /static      the shared design tokens and components (tokens.css, ui.css)

Nothing here can place an order. The writes are importing a statement,
through the same `cas.read` -> `LedgerWriter.ingest` path as the CLI, and
saving your own thesis to theses.db, through the same `ThesisStore.put`. The
upload is written to a private temp file, read once and deleted; the
password is passed to casparser and never stored, logged or echoed.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, select_autoescape

from .book import Book, Check, Holding, ReconcileReport, reconcile
from .facts import Isin
from .holding import holding_page
from .insights import all_insights, short_name, weights
from .prices import PriceBook, PricesDamaged, PricesMissing, PriceStore, india_today
from .sources import cas
from .store import REBUILD_HINT, ImportReport, Ledger, LedgerOutdated, LedgerWriter
from .story import story
from .thesis import MAX_TEXT, ThesesDamaged, Thesis, ThesisStore, for_book, parse_percent

PRICES_HINT = "Move prices.db aside, then run `stockin prices fetch` to download prices again."

STATIC = Path(__file__).parent / "static"

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
    value: str | None = None
    price_on: str | None = None


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


def _groups(
    book: Book, holdings: tuple[Holding, ...], prices: PriceBook | None = None
) -> tuple[AccountGroup, ...]:
    by_account: dict[str, list[Row]] = {}
    for h in sorted(holdings, key=lambda h: (h.account, h.isin)):
        acct = book.account(h.account)
        inst = book.instrument(h.isin)
        quote = prices.get(h.isin) if prices else None
        value = prices.value(h.isin, h.qty) if prices else None
        by_account.setdefault(acct.masked if acct else h.account, []).append(
            Row(
                isin=h.isin,
                name=inst.name if inst else h.isin,
                qty=str(h.qty),
                cost=str(h.cost) if h.cost is not None else None,
                value=str(value) if value is not None else None,
                price_on=quote.on.isoformat() if quote else None,
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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def page(name: str, **ctx: object) -> HTMLResponse:
        return HTMLResponse(_env.get_template(name).render(**ctx))

    @app.exception_handler(LedgerOutdated)
    async def outdated(request: Request, exc: LedgerOutdated) -> Response:
        html = _env.get_template("outdated.html").render(error=str(exc), hint=REBUILD_HINT)
        return HTMLResponse(html, status_code=409)

    def read_book() -> Book | None:
        if not db.is_file():
            return None
        with Ledger.open(db) as ledger:
            return ledger.book()

    def read_prices(isins: frozenset[Isin]) -> tuple[PriceBook | None, str | None]:
        """The price book for `isins`, or None with no prices yet; a damaged
        prices.db is said on the page, never a traceback."""
        path = db.parent / "prices.db"
        try:
            with PriceStore.open(path) as store:
                return store.book(isins), None
        except PricesMissing:
            return None, None
        except PricesDamaged as exc:
            return None, str(exc)
        except (sqlite3.DatabaseError, ValueError) as exc:
            # damage inside rows surfaces only while reading them: a bad date,
            # source or bytes. Same contract as PricesDamaged; a bug of any
            # other kind (RuntimeError, TypeError, ...) still surfaces.
            return None, f"{path} could not be read ({type(exc).__name__}: {str(exc)[:200]})"

    theses_db = db.parent / "theses.db"

    def read_theses() -> tuple[dict[Isin, Thesis], str | None]:
        try:
            with ThesisStore.open(theses_db) as store:
                return store.all(), None
        except ThesesDamaged as exc:
            return {}, str(exc)

    @app.get("/", response_class=HTMLResponse)
    def home() -> Response:
        book = read_book()
        if book is None:
            return page("home.html", groups=(), summary=None, empty=True, story=None)
        report = reconcile(book)
        holdings = book.holdings()
        prices, damaged = read_prices(frozenset(h.isin for h in holdings))
        priced = prices or PriceBook(quotes={})
        ws, total, _ = weights(book, priced)
        today = india_today()
        days = sorted(q.on for q in priced.quotes.values())
        theses, theses_damaged = read_theses()
        return page(
            "home.html",
            groups=_groups(book, holdings, prices),
            summary=_summary(report),
            ok=report.ok,
            empty=False,
            story=story(all_insights(book, priced, today), ws, total.paise, len(holdings)),
            priced_on=days[-1].isoformat() if days else None,
            damaged=damaged,
            prices_hint=PRICES_HINT,
            verdicts=for_book(book, priced, theses) if theses_damaged is None else (),
            theses=theses,
            theses_damaged=theses_damaged,
        )

    @app.get("/holding/{isin}", response_class=HTMLResponse)
    def holding(isin: str) -> Response:
        book = read_book()
        if book is None:
            return PlainTextResponse(f"{isin} is not in your holdings", 404)
        prices, damaged = read_prices(frozenset(h.isin for h in book.holdings()))
        theses, theses_damaged = read_theses()
        view = holding_page(book, prices or PriceBook(quotes={}), theses, Isin(isin), india_today())
        if view is None:
            return PlainTextResponse(f"{isin} is not in your holdings", 404)
        return page(
            "holding.html",
            h=view,
            damaged=damaged,
            prices_hint=PRICES_HINT,
            theses_damaged=theses_damaged,
        )

    def thesis_form(isin: str, error: str | None = None, **typed: str) -> Response:
        book = read_book()
        held = {h.isin for h in book.holdings()} if book else set[Isin]()
        if isin not in held or book is None:
            return PlainTextResponse(f"{isin} is not in your holdings", 404)
        inst = book.instrument(Isin(isin))
        theses, damaged = read_theses()
        if damaged:
            return page("thesis.html", isin=isin, holding=isin, error=damaged, t=None, typed={})
        t = theses.get(Isin(isin))
        return page(
            "thesis.html",
            isin=isin,
            holding=short_name(inst.name) if inst else isin,
            error=error,
            t=t,
            typed=typed,
            max_text=MAX_TEXT,
        )

    @app.get("/thesis/{isin}", response_class=HTMLResponse)
    def thesis_page(isin: str) -> Response:
        return thesis_form(isin)

    @app.post("/thesis/{isin}", response_class=HTMLResponse)
    def thesis_save(
        isin: str,
        why: Annotated[str, Form()] = "",
        target: Annotated[str, Form()] = "",
        exit_if: Annotated[str, Form()] = "",
        broken: Annotated[str, Form()] = "",
    ) -> Response:
        typed = {"why": why, "target": target, "exit_if": exit_if, "broken": broken}
        book = read_book()
        if book is None or isin not in {h.isin for h in book.holdings()}:
            return PlainTextResponse(f"{isin} is not in your holdings", 404)
        if not why.strip():
            return thesis_form(isin, "Write why you hold it first.", **typed)
        try:
            aim = parse_percent(target) if target.strip() else None
        except ValueError as exc:
            return thesis_form(isin, f"Target {exc}.", **typed)
        t = Thesis(
            isin=Isin(isin),
            why=why.strip()[:MAX_TEXT],
            exit_if=exit_if.strip()[:MAX_TEXT],
            target=aim,
            broken=broken == "on",
            written=india_today(),
        )
        try:
            with ThesisStore.open(theses_db) as store:
                store.put(t)
        except ThesesDamaged as exc:
            return thesis_form(isin, str(exc), **typed)
        return RedirectResponse(f"/holding/{isin}", status_code=303)

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
    def import_statement(
        statement: Annotated[UploadFile, File()],
        password: Annotated[str, Form()] = "",
    ) -> Response:
        filename = Path(statement.filename or "statement").name
        suffix = ".json" if filename.lower().endswith(".json") else ".pdf"
        body = statement.file.read(MAX_UPLOAD + 1)  # sync handler: runs in a worker thread
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
