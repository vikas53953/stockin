"""The local web app: `stockin web` serves it on 127.0.0.1 only.

Pages, all drawn from the same objects the CLI prints:
  /            your verdicts, the story, holdings by account, an upload form
  /thesis/ISIN write why you hold a holding, its target weight, or mark it broken
  /import      POST a CAS (PDF + password, or casparser JSON); shows the report
  /reconcile   every check, mismatches first
  /networth    what you own minus loans: the mix, drift from your target,
               your typed entries, and forms to add, close and set the mix
  /static      the shared design tokens and components (tokens.css, ui.css)

Nothing here can place an order. The writes are importing a statement,
through the same `cas.read` -> `LedgerWriter.ingest` path as the CLI, and
saving your own thesis to theses.db, through the same `ThesisStore.put`. The
upload is written to a private temp file, read once and deleted; the
password is passed to casparser and never stored, logged or echoed.
"""

from __future__ import annotations

import mimetypes
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
from .cockpit import cockpit
from .facts import Isin
from .holding import holding_page
from .importview import Coverage, coverage, source_label, tally, twin
from .insights import all_insights, short_name, weights
from .money import Bps
from .networth import (
    AssetsDamaged,
    AssetStore,
    Bucket,
    Entry,
    build_entry,
    close_entry,
    networth,
    parse_kind,
    parse_targets,
)
from .networth_page import page as networth_page
from .prices import PriceBook, PricesDamaged, PricesMissing, PriceStore, india_today
from .sources import cas
from .store import REBUILD_HINT, ImportReport, Ledger, LedgerOutdated, LedgerWriter
from .story import story
from .thesis import MAX_TEXT, ThesesDamaged, Thesis, ThesisStore, for_book, parse_percent

PRICES_HINT = "Move prices.db aside, then run `stockin prices fetch` to download prices again."

STATIC = Path(__file__).parent / "static"

# A Windows registry can map .css to text/plain, which browsers refuse as a
# stylesheet; say what our own files are.
mimetypes.add_type("text/css", ".css")

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
    stated_w: float  # bar widths on one scale, from importview.twin
    computed_w: float


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
        bars = twin(c.stated, c.computed)
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
            stated_w=bars.stated,
            computed_w=bars.computed,
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
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # revalidate, so an upgrade's new ui.css is used straight away
            response.headers["Cache-Control"] = "no-cache"
        return response

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
        insights = all_insights(book, priced, today)
        verdicts = for_book(book, priced, theses, today) if theses_damaged is None else ()
        return page(
            "home.html",
            groups=_groups(book, holdings, prices),
            summary=_summary(report),
            ok=report.ok,
            empty=False,
            story=story(insights, ws, total.paise, len(holdings)),
            cockpit=cockpit(insights, ws, verdicts, frozenset(theses or {})),
            priced_on=days[-1].isoformat() if days else None,
            damaged=damaged,
            prices_hint=PRICES_HINT,
            verdicts=verdicts,
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

    assets_db = db.parent / "assets.db"
    _FIELD = {
        "label": "Name",
        "value": "Value",
        "as_of": "As of",
        "principal": "Principal",
        "rate": "Interest rate",
        "start": "Opened on",
        "months": "Months",
        "grams": "Grams",
        "rate_per_gram": "Rate per gram",
    }

    def read_assets() -> tuple[tuple[Entry, ...], dict[Bucket, Bps]]:
        """Read-only: looking at the page never creates or changes assets.db.

        One case needs more. A write that died mid-commit leaves
        `assets.db-journal` beside the file, and SQLite must roll it back
        before anyone can read; a read-only connection is not allowed to. So
        when the read-only open fails and that journal is there, open once the
        way every CLI command does, which rolls back to the last saved state.
        A missing or zero-byte assets.db never gets this far."""
        try:
            with AssetStore.open(assets_db, readonly=True) as store:
                return store.entries(), store.targets()
        except AssetsDamaged:
            journal = assets_db.with_name(assets_db.name + "-journal")
            if not (journal.is_file() and journal.stat().st_size > 0):
                raise
        with AssetStore.open(assets_db) as store:
            return store.entries(), store.targets()

    def networth_view(
        error: str | None = None,
        typed: dict[str, str] | None = None,
        status: int = 200,
        typed_targets: dict[str, str] | None = None,
    ) -> Response:
        book = read_book()
        held = frozenset(h.isin for h in book.holdings()) if book else frozenset[Isin]()
        prices, damaged = read_prices(held) if held else (None, None)
        priced = prices or PriceBook(quotes={})
        assets_damaged = None
        entries, targets = (), {}
        try:
            entries, targets = read_assets()
        except FileNotFoundError:
            pass  # nothing typed yet
        except AssetsDamaged as exc:
            assets_damaged = str(exc)
        nw = networth(book, priced, entries, targets, india_today())
        days = sorted(q.on for q in priced.quotes.values())
        html = _env.get_template("networth.html").render(
            p=networth_page(nw, days[-1].isoformat() if days else None),
            error=error,
            typed=typed or {},
            typed_targets=typed_targets,
            damaged=damaged,
            prices_hint=PRICES_HINT,
            assets_damaged=assets_damaged,
            has_ledger=book is not None,
        )
        return HTMLResponse(html, status_code=status)

    @app.get("/networth", response_class=HTMLResponse)
    def networth_get() -> Response:
        return networth_view()

    def write_assets(
        change: Callable[[AssetStore], None],
        typed: dict[str, str],
        typed_targets: dict[str, str] | None = None,
    ) -> Response:
        try:
            with AssetStore.open(assets_db) as store:
                change(store)
        except AssetsDamaged as exc:
            return networth_view(str(exc), typed, 409, typed_targets)
        except ValueError as exc:
            return networth_view(str(exc), typed, 400, typed_targets)
        return RedirectResponse("/networth", status_code=303)

    @app.post("/networth/entry", response_class=HTMLResponse)
    def networth_entry(
        kind: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        value: Annotated[str, Form()] = "",
        as_of: Annotated[str, Form()] = "",
        principal: Annotated[str, Form()] = "",
        rate: Annotated[str, Form()] = "",
        start: Annotated[str, Form()] = "",
        months: Annotated[str, Form()] = "",
        grams: Annotated[str, Form()] = "",
        rate_per_gram: Annotated[str, Form()] = "",
    ) -> Response:
        typed = {
            "kind": kind,
            "label": label,
            "value": value,
            "as_of": as_of,
            "principal": principal,
            "rate": rate,
            "start": start,
            "months": months,
            "grams": grams,
            "rate_per_gram": rate_per_gram,
        }
        fields = {k: v[:200] for k, v in typed.items() if k != "kind"}
        try:
            entry = build_entry(kind, fields, india_today(), lambda n: _FIELD.get(n, n))
        except ValueError as exc:
            return networth_view(str(exc), typed, 400)
        return write_assets(lambda store: store.put(entry), typed)

    @app.post("/networth/close", response_class=HTMLResponse)
    def networth_close(
        kind: Annotated[str, Form()] = "", label: Annotated[str, Form()] = ""
    ) -> Response:
        try:
            k = parse_kind(kind)
        except ValueError as exc:
            return networth_view(str(exc), None, 400)

        def close(store: AssetStore) -> None:
            store.put(close_entry(store.entries(), k, label, india_today()))

        return write_assets(close, {})

    @app.post("/networth/targets", response_class=HTMLResponse)
    def networth_targets(
        t_equity: Annotated[str, Form()] = "",
        t_debt: Annotated[str, Form()] = "",
        t_gold: Annotated[str, Form()] = "",
        t_cash: Annotated[str, Form()] = "",
        t_retirement: Annotated[str, Form()] = "",
        t_real_estate: Annotated[str, Form()] = "",
        t_other: Annotated[str, Form()] = "",
    ) -> Response:
        typed = {
            "equity": t_equity,
            "debt": t_debt,
            "gold": t_gold,
            "cash": t_cash,
            "retirement": t_retirement,
            "real_estate": t_real_estate,
            "other": t_other,
        }
        pairs = [f"{b}={v.strip()[:20]}" for b, v in typed.items() if v.strip()]
        try:
            targets = parse_targets(pairs)
        except ValueError as exc:
            return networth_view(f"Target mix: {exc}", None, 400, typed)
        return write_assets(lambda store: store.set_targets(targets), {}, typed)

    @app.get("/reconcile", response_class=HTMLResponse)
    def reconcile_page() -> Response:
        book = read_book()
        if book is None:
            return page("reconcile.html", checks=(), summary=None, ok=True, tally=())
        report = reconcile(book)
        return page(
            "reconcile.html",
            checks=_check_rows(book, report),
            summary=_summary(report),
            ok=report.ok,
            tally=tally(report.checks),
        )

    def read_coverage() -> Coverage | str | None:
        """The month grid, or why it could not be read. It only decorates the
        page, so damage here never hides an import's result or its error,
        and never turns a finished import into a 500."""
        if not db.is_file():
            return None
        try:
            with Ledger.open(db) as ledger:
                return coverage(ledger.periods())
        except LedgerOutdated as exc:
            return f"{exc}. {REBUILD_HINT}"
        except (ValueError, sqlite3.DatabaseError) as exc:
            return f"Your statement list in {db} could not be read ({exc})."

    @app.get("/import", response_class=HTMLResponse)
    def import_page() -> Response:
        return page(
            "import.html",
            error=None,
            filename=None,
            report=None,
            groups=(),
            cover=read_coverage(),
            source_label=source_label,
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
                cover=read_coverage(),
                source_label=source_label,
            )
        if not body:
            return page(
                "import.html",
                error="That file is empty.",
                filename=filename,
                report=None,
                groups=(),
                cover=read_coverage(),
                source_label=source_label,
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
                    cover=read_coverage(),
                    source_label=source_label,
                )
        with LedgerWriter.open(db) as writer:
            report: ImportReport = writer.ingest(st)
            groups = _groups(writer.book(), report.holdings)
        return page(
            "import.html",
            error=None,
            filename=filename,
            report=report,
            groups=groups,
            cover=read_coverage(),
            source_label=source_label,
        )

    return app
