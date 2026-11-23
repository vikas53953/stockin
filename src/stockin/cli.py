"""The shell: parse argv, read the environment, make one call, print.

Every command takes --json and prints the same pydantic object the human
output is rendered from, so the verify skill and the owner see one truth.
"""

from __future__ import annotations

import http.client
import io
import os
import shlex
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import TypeAdapter

from . import __version__, notify
from .alerts import FROM_PRICE, AlertsBlocked, AlertsDamaged, AlertStore, Sent
from .book import Book, Holding, ReconcileReport, reconcile
from .facts import Isin
from .insights import Insight, all_insights, tenths_of
from .money import Bps, Money, Value
from .networth import (
    KIND_NAME,
    KINDS,
    AssetsDamaged,
    AssetStore,
    Closed,
    CloseTooEarly,
    Entry,
    Fd,
    Gold,
    Kind,
    Stated,
    build_entry,
    close_entry,
    current,
    entry_value,
    networth,
    parse_kind,
    parse_targets,
)
from .prices import (
    Jump,
    PriceBook,
    PriceFile,
    PricesDamaged,
    PricesMissing,
    PriceStore,
    Quote,
    india_now,
    india_today,
    left_out,
)
from .sources import cas, eod
from .store import REBUILD_HINT, ImportReport, Ledger, LedgerMissing, LedgerOutdated, LedgerWriter
from .thesis import MAX_TEXT, ThesesDamaged, Thesis, ThesisStore, Verdict, for_book, parse_percent

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Private, read-only add/trim/exit decisions for your holdings.",
)

Status = Literal["ok", "warn", "fail"]


class DoctorCheck(Value):
    name: str
    status: Status
    detail: str
    hint: str | None = None


class Doctor(Value):
    version: str
    data_dir: str
    ok: bool
    checks: tuple[DoctorCheck, ...]


def data_dir() -> Path:
    """$STOCKIN_DATA, else ./data. Real data lives only here; git ignores it."""
    return Path(os.environ.get("STOCKIN_DATA", "data")).expanduser()


def diagnose(root: Path) -> Doctor:
    """Read-only. Never creates, writes or deletes anything."""
    checks: list[DoctorCheck] = []
    py = sys.version_info
    checks.append(
        DoctorCheck(name="python", status="ok", detail=f"{py.major}.{py.minor}.{py.micro}")
        if py >= (3, 12)
        else DoctorCheck(
            name="python",
            status="fail",
            detail=f"{py.major}.{py.minor}",
            hint="Stockin needs Python 3.12 or newer",
        )
    )
    if root.exists() and not root.is_dir():
        checks.append(
            DoctorCheck(
                name="data_dir",
                status="fail",
                detail=f"{root} is a file, not a folder",
                hint="set STOCKIN_DATA to a folder",
            )
        )
    elif not root.is_dir():
        checks.append(
            DoctorCheck(
                name="data_dir",
                status="fail",
                detail=f"{root} does not exist",
                hint=f"mkdir -p {shlex.quote(str(root))} (or set STOCKIN_DATA)",
            )
        )
    elif not os.access(root, os.W_OK):
        checks.append(DoctorCheck(name="data_dir", status="fail", detail=f"{root} is not writable"))
    else:
        checks.append(DoctorCheck(name="data_dir", status="ok", detail=str(root)))
    checks.append(_ledger_check(root / "ledger.db"))
    checks.append(_quotes_check(root / "prices.db"))
    checks.append(_assets_check(root / "assets.db"))
    checks.append(_telegram_check(root))
    return Doctor(
        version=__version__,
        data_dir=str(root),
        ok=all(c.status != "fail" for c in checks),
        checks=tuple(checks),
    )


def _telegram_check(root: Path) -> DoctorCheck:
    try:
        bot = notify.load(root)
    except ValueError as exc:
        return DoctorCheck(name="telegram", status="fail", detail=str(exc))
    if bot is None:
        hint = "optional: stockin alerts connect"
        return DoctorCheck(name="telegram", status="warn", detail="not connected", hint=hint)
    return DoctorCheck(name="telegram", status="ok", detail=f"connected: {bot}")


_DAMAGED_HINT = "move prices.db aside (prices download again) and run `stockin prices fetch`"


def _quotes_check(path: Path) -> DoctorCheck:
    hint = "run `stockin prices fetch` (AMFI NAVs and the NSE bhavcopy)"
    try:
        with PriceStore.open(path) as store:
            latest, n = store.latest_day(), store.count()
    except PricesMissing:
        latest, n = None, 0
    except PricesDamaged as exc:
        return DoctorCheck(name="quotes", status="fail", detail=str(exc), hint=_DAMAGED_HINT)
    if latest is None:
        return DoctorCheck(name="quotes", status="warn", detail="no prices yet", hint=hint)
    return DoctorCheck(name="quotes", status="ok", detail=f"{n} end-of-day prices, latest {latest}")


def _assets_check(path: Path) -> DoctorCheck:
    """Read-only: assets.db is opened with mode=ro and never created here."""
    try:
        with AssetStore.open(path, readonly=True) as store:
            n = len(store.entries())
    except FileNotFoundError:
        return DoctorCheck(
            name="assets",
            status="warn",
            detail="no bank, FD, EPF, gold or loan entries yet",
            hint="add one with: stockin asset add bank --value 50000",
        )
    except AssetsDamaged as exc:
        return DoctorCheck(
            name="assets",
            status="fail",
            detail=str(exc),
            hint="copy assets.db somewhere safe first: it is the only copy of what you typed",
        )
    return DoctorCheck(name="assets", status="ok", detail=f"{n} entries in {path}")


JsonFlag = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]
_ENTRY_JSON: TypeAdapter[Entry] = TypeAdapter(Entry)
_ENTRIES_JSON: TypeAdapter[tuple[Stated | Fd | Gold, ...]] = TypeAdapter(
    tuple[Stated | Fd | Gold, ...]
)


@app.command()
def doctor(as_json: JsonFlag = False) -> None:
    """Check the data folder and each configured source; print what is missing."""
    report = diagnose(data_dir())
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        typer.echo(f"stockin {report.version}  data: {report.data_dir}")
        for check in report.checks:
            line = f"  {marks[check.status]}  {check.name:<9} {check.detail}"
            typer.echo(line + (f"  ({check.hint})" if check.hint else ""))
    raise typer.Exit(0 if report.ok else 1)


class Version(Value):
    version: str


@app.command()
def version(as_json: JsonFlag = False) -> None:
    """Print the installed version."""
    typer.echo(Version(version=__version__).model_dump_json() if as_json else __version__)


def ledger_path() -> Path:
    return data_dir() / "ledger.db"


def _fail(message: str, hint: str | None = None) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"  {hint}", err=True)
    return typer.Exit(2)


def _open_ledger() -> Ledger:
    try:
        return Ledger.open(ledger_path())
    except LedgerMissing:
        raise _fail(
            f"no ledger at {ledger_path()}", "import a statement first: stockin import cas FILE"
        ) from None
    except LedgerOutdated as exc:
        raise _fail(str(exc), REBUILD_HINT) from None


def _ledger_check(ledger: Path) -> DoctorCheck:
    if not ledger.is_file():
        return DoctorCheck(
            name="ledger",
            status="warn",
            detail="no ledger yet",
            hint="created by your first `stockin import cas`",
        )
    try:
        Ledger.open(ledger).close()
    except LedgerOutdated as exc:
        return DoctorCheck(name="ledger", status="fail", detail=str(exc), hint=REBUILD_HINT)
    return DoctorCheck(name="ledger", status="ok", detail=str(ledger))


def _holding_lines(
    book: Book, holdings: tuple[Holding, ...], prices: PriceBook | None = None
) -> list[str]:
    lines: list[str] = []
    current = None
    for h in sorted(holdings, key=lambda h: (h.account, h.isin)):
        if h.account != current:
            current = h.account
            acct = book.account(h.account)
            lines.append(acct.masked if acct else h.account)
        inst = book.instrument(h.isin)
        name = inst.name if inst else h.isin
        cost = f"cost {h.cost}" if h.cost is not None else "cost unknown"
        line = f"  {h.isin}  {name[:40]:<40} {h.qty!s:>16}  {cost}"
        if prices is not None:
            v = _valued(h, prices)
            if v.quote is None:
                line += "  value unknown (no price)"
            else:
                line += f"  value {v.value} (price {_price_text(v.quote)} on {v.quote.on})"
                if v.gain is not None:
                    line += f"  gain {v.gain}"
        lines.append(line)
    return lines


def _price_text(q: Quote) -> str:
    """Exact, at least two decimals: 1,367.00, 2,378.851, 94.4817."""
    rupees, micros = divmod(q.price.micro_rupees, 1_000_000)
    frac = f"{micros:06d}".rstrip("0").ljust(2, "0")
    return f"{Money(paise=rupees * 100)!s}"[:-3] + "." + frac


class Valued(Value):
    """One holding at its latest end-of-day price. None means unknown."""

    holding: Holding
    quote: Quote | None
    value: Money | None
    gain: Money | None  # value - cost, when both are known


def _valued(h: Holding, prices: PriceBook) -> Valued:
    quote = prices.get(h.isin)
    value = prices.value(h.isin, h.qty)
    gain = value - h.cost if value is not None and h.cost is not None else None
    return Valued(holding=h, quote=quote, value=value, gain=gain)


def prices_path() -> Path:
    return data_dir() / "prices.db"


def _price_book(isins: frozenset[Isin]) -> PriceBook | None:
    try:
        with PriceStore.open(prices_path()) as store:
            return store.book(isins)
    except PricesMissing:
        return None
    except PricesDamaged as exc:
        raise _fail(str(exc), _DAMAGED_HINT) from None


import_app = typer.Typer(no_args_is_help=True, help="Import a statement into your ledger.")
app.add_typer(import_app, name="import")


@import_app.command("cas")
def import_cas(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    as_json: JsonFlag = False,
) -> None:
    """Import a CAMS/KFintech or NSDL/CDSL CAS (PDF, or casparser JSON).

    A PDF's password is read from $STOCKIN_CAS_PASSWORD, else prompted for
    without echo. It is used once and never stored or logged. Importing the
    same file twice, or overlapping months, adds nothing the second time."""
    password = ""
    if file.suffix.lower() != ".json":
        password = os.environ.get("STOCKIN_CAS_PASSWORD") or typer.prompt(
            "CAS password", hide_input=True, default="", show_default=False
        )
    try:
        statement = cas.read(file, password)
    except cas.CasRejected as exc:
        raise _fail(f"rejected {file.name}: {exc}", "nothing was imported") from None
    try:
        writer = LedgerWriter.open(ledger_path())
    except LedgerOutdated as exc:
        raise _fail(str(exc), f"nothing was imported; {REBUILD_HINT}") from None
    with writer:
        report: ImportReport = writer.ingest(statement)
        lines = _holding_lines(writer.book(), report.holdings)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    period = f"{report.period_start} to {report.period_end}"
    typer.echo(f"{file.name}: {report.source}, {period}")
    if report.seen_before:
        typer.echo("  already imported; nothing changed")
    typer.echo(
        f"  {report.facts_added} new facts, {report.facts_already_present} already in your ledger"
    )
    for w in report.warnings:
        typer.echo(f"  note: {w}")
    for line in lines:
        typer.echo(line)


class HoldingsView(Value):
    holdings: tuple[Holding, ...]
    valued: tuple[Valued, ...] | None  # None until prices have been fetched


@app.command()
def holdings(as_json: JsonFlag = False) -> None:
    """Units held per account and ISIN, with value and gain once prices are fetched."""
    with _open_ledger() as ledger:
        book = ledger.book()
    rows = book.holdings()
    prices = _price_book(frozenset(h.isin for h in rows))
    if as_json:
        valued = None if prices is None else tuple(_valued(h, prices) for h in rows)
        typer.echo(HoldingsView(holdings=rows, valued=valued).model_dump_json(indent=2))
        return
    if not rows:
        typer.echo("no holdings")
    for line in _holding_lines(book, rows, prices):
        typer.echo(line)
    if prices is None:
        typer.echo("(no prices yet: run `stockin prices fetch` to see value and gain)")


class InsightsView(Value):
    priced_on: str | None  # latest price date used, None before any fetch
    unpriced: tuple[Isin, ...]  # held, but no price yet: left out of value insights
    insights: tuple[Insight, ...]


@app.command("insights")
def insights_cmd(as_json: JsonFlag = False) -> None:
    """What your numbers say: what changed, concentration, unknown cost, SIPs, tax dates."""
    with _open_ledger() as ledger:
        book = ledger.book()
    held = frozenset(h.isin for h in book.holdings())
    prices = _price_book(held) or PriceBook(quotes={})
    found = all_insights(book, prices, _today())
    days = [q.on for q in prices.quotes.values()]
    unpriced = tuple(sorted(i for i in held if prices.get(i) is None))
    view = InsightsView(
        priced_on=max(days).isoformat() if days else None, unpriced=unpriced, insights=found
    )
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
        return
    marks = {"warn": "!", "unknown": "?", "good": "+", "info": "-"}
    for i in found:
        typer.echo(f"{marks[i.tone]} {i.headline}")
        typer.echo(f"    {i.detail}")
    if view.priced_on is None:
        typer.echo("(no prices yet: run `stockin prices fetch` for value-based insights)")
    elif unpriced:
        n = len(unpriced)
        typer.echo(
            f"({n} holding{'s have' if n != 1 else ' has'} no price yet and "
            f"{'are' if n != 1 else 'is'} left out of value: run `stockin prices fetch`)"
        )


prices_app = typer.Typer(no_args_is_help=True, help="End-of-day prices for what you hold.")
app.add_typer(prices_app, name="prices")


class PricesReport(Value):
    source: str
    read: int  # quotes in the file(s)
    kept: int  # for ISINs in your ledger
    changed: int  # rows added, changed or removed in prices.db
    latest: str | None
    unpriced: tuple[Isin, ...]  # held ISINs with no price at all
    failed: tuple[str, ...] = ()  # sources that could not be downloaded this time
    held: tuple[Jump, ...] = ()  # prices not stored: too far from the last one
    removed: tuple[Quote, ...] = ()  # prices a file took away: saved for its day, not in it


def _today() -> date:
    """Today in India, where both price files are dated."""
    return india_today()


def _held_isins() -> frozenset[Isin]:
    with _open_ledger() as ledger:
        return frozenset(h.isin for h in ledger.book().holdings())


def _store_files(
    source: str, files: tuple[PriceFile, ...], held: frozenset[Isin], accept_jumps: bool
) -> PricesReport:
    """Each file replaces what prices.db holds for its source and day, always:
    that is what lets the right file undo a damaged copy. A price more than
    JUMP_FACTOR away from the last earlier one, or more than 20% from the
    last one of the same source, is held back unless `accept_jumps`, and
    kept as held so verdicts know the price in use is in doubt. A held
    fund's price that was stored for that day and is not in the file is
    removed like the rest and reported (`left_out`)."""
    kept = changed = 0
    jumps: list[Jump] = []
    removed: list[Quote] = []
    try:
        with PriceStore.open(prices_path(), write=True) as store:
            for whole in files:
                file = eod.only(whole, held)
                kept += len(file.quotes)
                removed.extend(left_out(file, store.stored(file.source, file.day), held))
                n, found = store.take(file, accept_jumps)
                changed += n
                jumps.extend(found)
            book = store.book(held)
            latest = store.latest_day()
    except PricesDamaged as exc:
        raise _fail(str(exc), _DAMAGED_HINT) from None
    unpriced = tuple(sorted(i for i in held if book.get(i) is None))
    return PricesReport(
        source=source,
        read=sum(len(f.quotes) for f in files),
        kept=kept,
        changed=changed,
        latest=None if latest is None else latest.isoformat(),
        unpriced=unpriced,
        held=tuple(jumps),
        removed=tuple(removed),
    )


def _print_prices(report: PricesReport, as_json: bool) -> None:
    """Exit 1 when a price was held back or removed: the rest is stored, but
    the run needs the owner's eyes."""
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(
            f"{report.source}: {report.read} prices read, {report.kept} for your holdings, "
            f"{report.changed} new or changed"
        )
        if report.latest:
            typer.echo(f"  latest price date: {report.latest}")
        if report.unpriced:
            typer.echo(f"  no price yet for: {', '.join(report.unpriced)}")
        for f in report.failed:
            typer.echo(f"  not downloaded this time: {f} (prices already saved are kept)")
        for j in report.held:
            typer.echo(
                f"  NOT stored: {j.quote.isin} was {_price_text(j.last)} on {j.last.on}, "
                f"this file says {_price_text(j.quote)} on {j.quote.on}"
            )
        if report.held:
            typer.echo(
                "  a price that moved more than 20% from the same source's last price, "
                "or more than double or less than half any earlier one, is held back, "
                "and no add or trim is worked out while it is. If it is real (a split "
                "or a bonus, a real move after a long break without prices, or a share "
                "you sold and bought back months later), run again with --accept-jumps; "
                "if not, the file is damaged: download it again. An ETF's split is held "
                "twice, once in the NAV file and once in the exchange file."
            )
        for q in report.removed:
            typer.echo(
                f"  REMOVED: {q.isin} {_price_text(q)} on {q.on}, saved from an earlier copy "
                "of this day's file; this file has no price for it"
            )
        if report.removed:
            typer.echo(
                "  this file is now what is saved for its day. If the earlier copy was "
                "damaged, that is the fix and there is nothing more to do. If this file "
                "may be cut short (a download or copy that stopped partway), download it "
                "again and import it, or run `stockin prices fetch` again if this was a "
                "fetch: that puts the price back."
            )
    if report.held or report.removed:
        raise typer.Exit(1)


AcceptJumps = Annotated[
    bool,
    typer.Option(
        "--accept-jumps",
        help="Store prices even when they moved more than 20% from the last one, or 2x.",
    ),
]


@prices_app.command("fetch")
def prices_fetch(as_json: JsonFlag = False, accept_jumps: AcceptJumps = False) -> None:
    """Download today's AMFI NAVs and the latest NSE bhavcopy (public files, no login).

    Only prices for ISINs in your ledger are kept. Safe to run any number of times."""
    held = _held_isins()
    today = _today()
    got: list[PriceFile] = []
    failed: list[str] = []
    for name, fetch in (("AMFI", eod.fetch_amfi), ("NSE", eod.fetch_bhavcopy)):
        try:
            got.append(fetch(today))
        except (OSError, http.client.HTTPException, eod.EodRejected) as exc:
            failed.append(f"{name}: {exc}")
        except Exception as exc:  # one source's surprise must not cost the other's prices
            failed.append(f"{name}: unexpected {type(exc).__name__}: {exc}")
    if len(failed) == 2:
        raise _fail(
            "could not download prices: " + "; ".join(failed), "check your internet and try again"
        )
    report = _store_files("amfi + nse", tuple(got), held, accept_jumps)
    _print_prices(report.model_copy(update={"failed": tuple(failed)}), as_json)


@prices_app.command("import")
def prices_import(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    as_json: JsonFlag = False,
    accept_jumps: AcceptJumps = False,
) -> None:
    """Import a downloaded AMFI NAVAll.txt or NSE bhavcopy (.csv or .csv.zip).

    The file replaces what is stored for its day, so importing the right file
    corrects a damaged one."""
    held = _held_isins()
    data = file.read_bytes()
    try:
        if data.removeprefix(b"\xef\xbb\xbf").startswith(b"Scheme Code;"):
            text = data.decode("utf-8-sig", errors="replace")
            parsed, source = eod.parse_amfi(text, _today()), "amfi"
        else:
            parsed, source = eod.parse_bhavcopy(data, _today()), "nse"
    except (eod.EodRejected, UnicodeDecodeError, OSError) as exc:
        raise _fail(f"rejected {file.name}: {exc}", "nothing was imported") from None
    _print_prices(_store_files(f"{file.name} ({source})", (parsed,), held, accept_jumps), as_json)


thesis_app = typer.Typer(no_args_is_help=True, help="Why you hold each holding, in your words.")
app.add_typer(thesis_app, name="thesis")


def theses_path() -> Path:
    return data_dir() / "theses.db"


@contextmanager
def _open_theses() -> Generator[ThesisStore]:
    """theses.db for the length of a command. Damage found on open, on read
    or on write is said with a hint, never a traceback."""
    try:
        with ThesisStore.open(theses_path()) as store:
            yield store
    except ThesesDamaged as exc:
        hint = "copy theses.db somewhere safe first: it is the only copy of what you wrote"
        raise _fail(str(exc), hint) from None


def _percent(text: str) -> Bps:
    try:
        return parse_percent(text)
    except ValueError as exc:
        raise _fail(f"target {exc}") from None


def _held(isin: str) -> Isin:
    with _open_ledger() as ledger:
        book = ledger.book()
    held = {h.isin for h in book.holdings()}
    if isin.upper() not in held:
        raise _fail(f"{isin} is not in your holdings", "see them with: stockin holdings")
    return Isin(isin.upper())


@thesis_app.command("set")
def thesis_set(
    isin: Annotated[str, typer.Argument(help="The ISIN you hold.")],
    why: Annotated[str, typer.Option("--why", help="Why you hold it, in one or two sentences.")],
    target: Annotated[
        str | None, typer.Option(help="The weight you want it to have, e.g. 10 for 10%.")
    ] = None,
    exit_if: Annotated[str, typer.Option("--exit-if", help="What would make you sell.")] = "",
) -> None:
    """Write (or rewrite) your thesis for a holding. Rewriting clears 'broken'."""
    held = _held(isin)
    if not why.strip():
        raise _fail("--why is empty: say why you hold it")
    t = Thesis(
        isin=held,
        why=why.strip()[:MAX_TEXT],
        exit_if=exit_if.strip()[:MAX_TEXT],
        target=_percent(target) if target is not None else None,
        written=_today(),
    )
    with _open_theses() as store:
        store.put(t)
    typer.echo(f"saved your thesis for {held}")


@thesis_app.command("broken")
def thesis_broken(
    isin: Annotated[str, typer.Argument(help="The ISIN you hold.")],
    undo: Annotated[bool, typer.Option("--undo", help="Mark it as holding again.")] = False,
) -> None:
    """Mark your reason for holding as no longer true (the verdict becomes exit)."""
    held = _held(isin)
    with _open_theses() as store:
        t = store.all().get(held)
        if t is None:
            hint = f"write one: stockin thesis set {held} --why ..."
            raise _fail(f"no thesis for {held} yet", hint)
        store.put(t.model_copy(update={"broken": not undo}))
    typer.echo(f"{held}: marked {'as holding again' if undo else 'broken'}")


class VerdictsView(Value):
    priced_on: str | None
    verdicts: tuple[Verdict, ...]


def _verdicts_now() -> tuple[tuple[Verdict, ...], PriceBook]:
    """Today's verdict for every holding, and the prices they came from."""
    with _open_ledger() as ledger:
        book = ledger.book()
    held = frozenset(h.isin for h in book.holdings())
    prices = _price_book(held) or PriceBook(quotes={})
    with _open_theses() as store:
        return for_book(book, prices, store.all(), _today()), prices


@app.command("verdicts")
def verdicts_cmd(as_json: JsonFlag = False) -> None:
    """Add, trim, exit, hold or review for each holding, from your own thesis and target."""
    found, prices = _verdicts_now()
    days = [q.on for q in prices.quotes.values()]
    view = VerdictsView(priced_on=max(days).isoformat() if days else None, verdicts=found)
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
        return
    for v in found:
        typer.echo(f"{v.call.upper():<6} {v.because}")
    typer.echo("(your own rules applied to your own numbers; not advice)")


asset_app = typer.Typer(
    no_args_is_help=True, help="What the CAS does not cover: bank, FD, EPF, PPF, NPS, gold, loans."
)
app.add_typer(asset_app, name="asset")
target_app = typer.Typer(no_args_is_help=True, help="The asset mix you aim for.")
app.add_typer(target_app, name="target")


def assets_path() -> Path:
    return data_dir() / "assets.db"


_ASSETS_HINT = "copy assets.db somewhere safe first: it is the only copy of what you typed"


@contextmanager
def _open_assets() -> Generator[AssetStore]:
    """assets.db for the length of a command. Damage found on open, on read
    or on write is said with a hint, never a traceback."""
    try:
        with AssetStore.open(assets_path()) as store:
            yield store
    except AssetsDamaged as exc:
        raise _fail(str(exc), _ASSETS_HINT) from None


def _day(text: str | None, what: str) -> date:
    if text is None:
        return _today()
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise _fail(f"{what} {text!r} is not a date like 2026-10-31") from None
    if day > _today():
        raise _fail(f"{what} {day} is in the future")
    return day


def _kind(text: str) -> Kind:
    try:
        return parse_kind(text)
    except ValueError as exc:
        raise _fail(str(exc)) from None


@asset_app.command("add")
def asset_add(
    kind: Annotated[str, typer.Argument(help=f"One of: {', '.join(KINDS)}.")],
    label: Annotated[
        str | None, typer.Option(help="Your name for it, e.g. 'SBI savings'. Defaults to the kind.")
    ] = None,
    value: Annotated[
        str | None,
        typer.Option(help="Balance in rupees (bank, EPF, PPF, NPS, property, other, loan)."),
    ] = None,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Date of the balance, YYYY-MM-DD. Default today.")
    ] = None,
    principal: Annotated[str | None, typer.Option(help="FD: amount deposited, in rupees.")] = None,
    rate: Annotated[str | None, typer.Option(help="FD: interest rate a year, e.g. 7.1.")] = None,
    start: Annotated[str | None, typer.Option(help="FD: date it was opened, YYYY-MM-DD.")] = None,
    months: Annotated[int | None, typer.Option(help="FD: term in months.")] = None,
    grams: Annotated[str | None, typer.Option(help="Gold: grams you hold in total.")] = None,
    rate_per_gram: Annotated[
        str | None, typer.Option("--rate-per-gram", help="Gold: rupees a gram.")
    ] = None,
    as_json: JsonFlag = False,
) -> None:
    """Record a balance (or an FD, or gold) as of a day. Adding it again for a
    later day keeps the history; the same day again corrects it."""
    typed = {
        "label": label,
        "value": value,
        "as_of": as_of,
        "principal": principal,
        "rate": rate,
        "start": start,
        "months": str(months) if months is not None else None,
        "grams": grams,
        "rate_per_gram": rate_per_gram,
    }
    if label is not None and not label.strip():
        raise _fail("--label is blank: give it a name, or leave --label out")
    try:
        entry = build_entry(kind, typed, _today())
    except ValueError as exc:
        raise _fail(str(exc)) from None
    k = entry.kind
    with _open_assets() as store:
        store.put(entry)
    if as_json:
        typer.echo(_ENTRY_JSON.dump_json(entry, indent=2).decode())
        return
    shown = entry_value(entry, _today()) if not isinstance(entry, Closed) else Money(paise=0)
    typer.echo(f"saved {KIND_NAME[k]} '{entry.label}' as of {entry.as_of}: ₹{shown} today")


@asset_app.command("close")
def asset_close(
    kind: Annotated[str, typer.Argument(help=f"One of: {', '.join(KINDS)}.")],
    label: Annotated[str | None, typer.Option(help="The name you gave it.")] = None,
    on: Annotated[
        str | None, typer.Option(help="Date it ended, YYYY-MM-DD. Default today.")
    ] = None,
) -> None:
    """You no longer hold it (paid out, repaid, sold). Its history is kept."""
    k = _kind(kind)
    name = (label if label is not None else KIND_NAME[k]).strip()
    day = _day(on, "--on")
    with _open_assets() as store:
        try:
            closing = close_entry(store.entries(), k, name, day)
        except CloseTooEarly as exc:
            raise _fail(str(exc), f"use --on {exc.latest} or later") from None
        except ValueError as exc:
            raise _fail(str(exc), "see them with: stockin asset list") from None
        store.put(closing)
    typer.echo(f"closed {KIND_NAME[k]} '{name}' from {day}; its history is kept")


@asset_app.command("list")
def asset_list(as_json: JsonFlag = False) -> None:
    """Every entry you hold today, with its value today."""
    today = _today()
    with _open_assets() as store:
        held = current(store.entries(), today)
    if as_json:
        typer.echo(_ENTRIES_JSON.dump_json(held, indent=2).decode())
        return
    if not held:
        typer.echo("nothing yet: add one with stockin asset add KIND ...")
        return
    for e in held:
        typer.echo(
            f"{KIND_NAME[e.kind]:<9} {e.label:<30} {'₹' + str(entry_value(e, today)):>19}"
            f"  as of {e.as_of}"
        )


@target_app.command("set")
def target_set(
    pairs: Annotated[
        list[str], typer.Argument(help="CLASS=PERCENT pairs adding to 100, e.g. equity=60 debt=25.")
    ],
) -> None:
    """Set the mix you aim for. Classes: equity, debt, gold, cash, retirement,
    real_estate, other. Replaces any mix set before."""
    try:
        targets = parse_targets(pairs)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    with _open_assets() as store:
        store.set_targets(targets)
    typer.echo("target mix saved: " + ", ".join(f"{b} {_bps(v)}" for b, v in targets.items()))


@target_app.command("clear")
def target_clear() -> None:
    """Forget the target mix."""
    with _open_assets() as store:
        store.set_targets({})
    typer.echo("target mix cleared")


def _bps(bps: int) -> str:
    """Exact, no float: 6000 -> '60%', 1250 -> '12.5%', 1234 -> '12.34%'."""
    whole, frac = divmod(bps, 100)
    return f"{whole}.{frac:02d}".rstrip("0").rstrip(".") + "%"


def _drift(bps: int, target: int) -> str:
    """Shown weight minus shown target, in the tenths the notes decide on, so
    the column never contradicts the sentence under it: 2504 against 2000 is
    '+5.0', not '+5.04'. A gap of exactly whole points drops the '.0'; a gap that shows as zero
    has no direction, so it is '±'."""
    gap = tenths_of(bps, 10_000) - tenths_of(target, 10_000)
    whole, frac = divmod(abs(gap), 10)
    exact = (bps - target) % 100 == 0
    sign = "+" if gap > 0 else "-" if gap < 0 else "±"
    return sign + (str(whole) if exact else f"{whole}.{frac}")


def _optional_book() -> Book | None:
    try:
        with Ledger.open(ledger_path()) as ledger:
            return ledger.book()
    except LedgerMissing:
        return None
    except LedgerOutdated as exc:
        raise _fail(str(exc), REBUILD_HINT) from None


@app.command("networth")
def networth_cmd(as_json: JsonFlag = False) -> None:
    """Everything you own minus loans, split by asset class, with drift from your target mix."""
    book = _optional_book()
    held = frozenset(h.isin for h in book.holdings()) if book else frozenset[Isin]()
    prices = (_price_book(held) if held else None) or PriceBook(quotes={})
    with _open_assets() as store:
        entries, targets = store.entries(), store.targets()
    nw = networth(book, prices, entries, targets, _today())
    if as_json:
        typer.echo(nw.model_dump_json(indent=2))
        return
    typer.echo(f"Net worth ₹{nw.total}  (own ₹{nw.owned}, owe ₹{nw.owed})  on {nw.on}")
    for s in nw.slices:
        aim = ""
        if s.target is not None and s.drift is not None:
            aim = f"  target {_bps(s.target)}  drift {_drift(s.bps, s.target)} pts"
        typer.echo(f"  {s.name:<12} {'₹' + str(s.value):>19}  {_bps(s.bps):>7}{aim}")
    for n in nw.notes:
        typer.echo(f"- {n.text}")


@app.command("reconcile")
def reconcile_cmd(as_json: JsonFlag = False) -> None:
    """Check every statement balance against the transactions. Exit 1 on any mismatch."""
    with _open_ledger() as ledger:
        book = ledger.book()
        report: ReconcileReport = reconcile(book)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        counts = {s: sum(c.status == s for c in report.checks) for s in ("match", "inferred")}
        typer.echo(
            ("OK  " if report.ok else "")
            + f"{len(report.checks)} checks: {counts['match']} match, "
            f"{counts['inferred']} inferred, {len(report.mismatches)} mismatch"
        )
        for c in report.mismatches:
            inst = book.instrument(c.isin)
            acct = book.account(c.account)
            typer.echo(
                f"  MISMATCH {c.kind} {c.on} {c.isin} {inst.name if inst else ''} "
                f"({acct.masked if acct else c.account}): statement {c.stated}, "
                f"transactions {c.computed}"
            )
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def web(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765,
) -> None:
    """Open the local web app: upload a CAS, see holdings and reconcile.

    Listens on 127.0.0.1 only, so nothing outside this computer can reach it."""
    import socket

    import uvicorn

    from .web import create_app

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise _fail(f"port {port} is already in use", f"try: stockin web --port {port + 1}")
    typer.echo(f"Stockin is running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(ledger_path(), port), host="127.0.0.1", port=port, log_level="warning")


alerts_app = typer.Typer(
    no_args_is_help=True, help="One message when a verdict turns into add, trim or exit."
)
app.add_typer(alerts_app, name="alerts")


def alerts_path() -> Path:
    return data_dir() / "alerts.db"


@contextmanager
def _open_alerts() -> Generator[AlertStore]:
    try:
        with AlertStore.open(alerts_path()) as store:
            yield store
    except AlertsBlocked as exc:
        hint = "another stockin may be running, or the data folder is read-only; try again"
        raise _fail(str(exc), hint) from None
    except AlertsDamaged as exc:
        hint = "move alerts.db aside; Stockin starts a fresh one (past alerts are lost)"
        raise _fail(str(exc), hint) from None


class AlertsView(Value):
    priced_on: str | None
    alerts: tuple[Sent, ...]


def _alert_lines(found: tuple[Sent, ...]) -> None:
    for s in found:
        state = "sent" if s.sent else "pending"
        when = s.made.strftime("%Y-%m-%d %H:%M")
        typer.echo(f"{when}  {s.alert.call.upper():<5} {s.alert.because}  [{state}]")


SendFlag = Annotated[bool, typer.Option("--send", help="Also send pending alerts to Telegram.")]


@alerts_app.command("check")
def alerts_check(as_json: JsonFlag = False, send: SendFlag = False) -> None:
    """Work out today's verdicts and keep an alert for each one that newly turned
    into add, trim or exit. Running it again on the same numbers adds nothing.
    An add or trim not yet sent is withdrawn while its price is in doubt."""
    found, prices = _verdicts_now()
    days = [q.on for q in prices.quotes.values()]
    with _open_alerts() as store:
        added = store.record(found, made=india_now())
    view = AlertsView(priced_on=max(days).isoformat() if days else None, alerts=added)
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
    elif not added:
        typer.echo("no new alerts: no verdict turned into add, trim or exit since the last check")
    else:
        typer.echo(f"{len(added)} new alert{'s' if len(added) > 1 else ''}:")
        _alert_lines(added)
    if send:
        _send_pending(quiet=as_json)


@alerts_app.command("list")
def alerts_list(as_json: JsonFlag = False) -> None:
    """Every alert kept, oldest first, and whether it was sent."""
    found: tuple[Sent, ...] = ()
    if alerts_path().exists() or alerts_path().is_symlink():  # a view never creates the file
        with _open_alerts() as store:
            found = store.outbox()
    if as_json:
        typer.echo(AlertsView(priced_on=None, alerts=found).model_dump_json(indent=2))
        return
    if not found:
        typer.echo("no alerts yet: run `stockin alerts check` after prices are fetched")
        return
    _alert_lines(found)


def _bot() -> notify.Bot:
    try:
        bot = notify.load(data_dir())
    except ValueError as exc:
        raise _fail(str(exc)) from None
    if bot is None:
        raise _fail("Telegram is not connected yet", "connect it: stockin alerts connect")
    return bot


def _transport(bot: notify.Bot) -> notify.Transport:
    try:
        return notify.http_transport(bot.token)
    except ValueError as exc:
        raise _fail(str(exc)) from None


def _unsure(found: tuple[Verdict, ...]) -> frozenset[Isin]:
    return frozenset(v.isin for v in found if v.unsure)


class _DoubtsNow:
    """Which holdings' prices are in doubt, worked out afresh each time a
    send comes to an add or trim (#40 verifier R-1). If the verdicts cannot
    be worked out (a price or thesis file Stockin cannot read), the error is
    said once and every add and trim waits for the rest of this send; exits
    still go (#40 verifier R-2)."""

    def __init__(self) -> None:
        self.failed = False
        self.last: frozenset[Isin] = frozenset()

    def __call__(self) -> frozenset[Isin] | None:
        if self.failed:
            return None
        try:
            found, _ = _verdicts_now()
        except typer.Exit:  # said already, on stderr
            self.failed = True
            return None
        except (ValueError, sqlite3.DatabaseError, OSError) as exc:
            typer.echo(f"error: {exc}", err=True)
            self.failed = True
            return None
        self.last = _unsure(found)
        return self.last


def _send_pending(quiet: bool = False) -> None:
    bot = _bot()
    call = _transport(bot)
    doubts = _DoubtsNow()
    with _open_alerts() as store:
        done, error = notify.deliver(store, call, bot.chat, india_now, doubts)
        pending = store.outbox(pending_only=True)
    left = len(pending)
    n = len(done)
    typer.echo(f"sent {n} alert{'' if n == 1 else 's'} to Telegram", err=quiet)
    for s in pending:
        if error is not None or s.alert.call not in FROM_PRICE:
            continue
        if doubts.failed:
            why = "the verdicts could not be worked out (see the error above)"
        elif s.alert.isin in doubts.last:
            why = "its price is in doubt (see `stockin verdicts`)"
        else:
            continue
        typer.echo(
            f"  not sent: {s.alert.call.upper()} {s.alert.name}, {why}; "
            "the next `stockin alerts check` withdraws it or sends it",
            err=quiet,
        )
    if error is not None:
        typer.echo(f"error: {error}", err=True)
        typer.echo(f"  {left} still pending; they go out on the next send", err=True)
    if error is not None or doubts.failed:
        raise typer.Exit(1)


@alerts_app.command("send")
def alerts_send() -> None:
    """Send every pending alert to Telegram, oldest first. Exit 1 if any is left pending.

    An add or trim whose holding's price is in doubt now is held back."""
    _bot()  # "connect it first" before anything else is read
    _send_pending()


@alerts_app.command("connect")
def alerts_connect() -> None:
    """Connect your own Telegram bot: paste its token, message it once, done.

    The token is typed, not passed on the command line, so it never lands in
    your shell history. It is saved to telegram.json in your data folder,
    readable only by you."""
    raw = typer.prompt("Paste your bot token from @BotFather (hidden)", hide_input=True)
    try:
        token = notify.parse_token(raw)
        call = notify.http_transport(token)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    typer.echo("Now open Telegram, send any message (for example: hi) to your bot,")
    typer.prompt("then press Enter here", default="", show_default=False)
    try:
        chat = notify.find_chat(call)
    except notify.TelegramError as exc:
        raise _fail(str(exc), "check the token with @BotFather and try again") from None
    if chat is None:
        raise _fail(
            "your bot has no message from you yet",
            "send it a message in Telegram, then run stockin alerts connect again",
        )
    bot = notify.Bot(token=token, chat=chat)
    try:
        notify.send_one(call, chat, "Stockin is connected. Alerts will arrive here.")
    except notify.TelegramError as exc:
        raise _fail(str(exc)) from None
    path = notify.save(data_dir(), bot)
    typer.echo(f"connected: {bot}. Saved to {path} (only you can read it).")


@alerts_app.command("test")
def alerts_test() -> None:
    """Send one test message to your Telegram, to prove the connection."""
    bot = _bot()
    try:
        notify.send_one(_transport(bot), bot.chat, "Stockin test message: alerts reach you here.")
    except notify.TelegramError as exc:
        raise _fail(str(exc)) from None
    typer.echo(f"sent a test message ({bot})")


def _utf8_output() -> None:
    """Write UTF-8 whatever the console's code page. On Windows, output that
    is piped, captured or redirected gets the ANSI code page (cp1252), which
    has no ₹, so every line with a rupee sign raised UnicodeEncodeError (the
    Windows CI run of drop 2 found it). An interactive console is already
    UTF-8 and is left alone."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def main() -> None:
    _utf8_output()
    app()
