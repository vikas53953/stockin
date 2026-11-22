"""The shell: parse argv, read the environment, make one call, print.

Every command takes --json and prints the same pydantic object the human
output is rendered from, so the verify skill and the owner see one truth.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import shlex
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import typer

from . import __version__, notify
from .alerts import AlertsBlocked, AlertsDamaged, AlertStore, Sent
from .book import Book, Holding, ReconcileReport, reconcile
from .facts import Isin
from .insights import Insight, all_insights
from .money import Bps, Money, Price, Value
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
from .sources import cas, eod, live
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
    if notify.others_can_read(root):
        path = notify.config_path(root)
        return DoctorCheck(
            name="telegram",
            status="warn",
            detail=f"connected: {bot}, but {path} can be read by other users",
            hint=f"chmod 600 {shlex.quote(str(path))}",
        )
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


JsonFlag = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]


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
    JUMP_FACTOR away from the last earlier one is held back unless
    `accept_jumps`. A held fund's price that was stored for that day and is
    not in the file is removed like the rest and reported (`left_out`)."""
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
                "  a price more than double or less than half the last one is held back. "
                "If it is real (a split, a bonus), run again with --accept-jumps; "
                "if not, the file is damaged: download it again."
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
        help="Store prices even when more than double or less than half the last one.",
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


@app.command("verdicts")
def verdicts_cmd(as_json: JsonFlag = False) -> None:
    """Add, trim, exit, hold or review for each holding, from your own thesis and target."""
    with _open_ledger() as ledger:
        book = ledger.book()
    held = frozenset(h.isin for h in book.holdings())
    prices = _price_book(held) or PriceBook(quotes={})
    with _open_theses() as store:
        found = for_book(book, prices, store.all())
    days = [q.on for q in prices.quotes.values()]
    view = VerdictsView(priced_on=max(days).isoformat() if days else None, verdicts=found)
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
        return
    for v in found:
        typer.echo(f"{v.call.upper():<6} {v.because}")
    typer.echo("(your own rules applied to your own numbers; not advice)")


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
    into add, trim or exit. Running it again on the same numbers adds nothing."""
    with _open_ledger() as ledger:
        book = ledger.book()
    held = frozenset(h.isin for h in book.holdings())
    prices = _price_book(held) or PriceBook(quotes={})
    with _open_theses() as theses:
        found = for_book(book, prices, theses.all())
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


def _send_pending(quiet: bool = False) -> None:
    bot = _bot()
    call = _transport(bot)
    with _open_alerts() as store:
        done, error = notify.deliver(store, call, bot.chat, india_now)
        left = len(store.outbox(pending_only=True))
    n = len(done)
    typer.echo(f"sent {n} alert{'' if n == 1 else 's'} to Telegram", err=quiet)
    if error is not None:
        typer.echo(f"error: {error}", err=True)
        typer.echo(f"  {left} still pending; they go out on the next send", err=True)
        raise typer.Exit(1)


@alerts_app.command("send")
def alerts_send() -> None:
    """Send every pending alert to Telegram, oldest first. Exit 1 if any is left pending."""
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


# ------------------------------------------------------------------ live

LIVE_KINDS = ("stock", "etf")
"""What trades on NSE through the day. Mutual funds have one NAV a day."""


class Fallback(Value):
    isin: str
    reason: str


class LiveView(Value):
    at: datetime
    market_open: bool
    live: tuple[live.LiveQuote, ...]
    end_of_day: tuple[Fallback, ...]


def symbols_path() -> Path:
    return data_dir() / "nse-symbols.json"


def _symbols(today: date) -> dict[Isin, str]:
    """NSE's ISIN-to-symbol list, downloaded at most once a day (public
    data, kept beside prices.db)."""
    path = symbols_path()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached["day"] == today.isoformat():
            return {Isin(k): str(v) for k, v in dict(cached["symbols"]).items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = live.fetch_symbols()
    try:
        body = {"day": today.isoformat(), "symbols": dict(sorted(found.items()))}
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        pass  # a cache only
    return found


def _live_pass(book: Book) -> tuple[PriceBook, LiveView]:
    """End-of-day prices with today's live trades laid over them. A holding
    whose live quote is missing, late or odd keeps its close, and says why."""
    now = india_now()
    held = frozenset(h.isin for h in book.holdings())
    eod_book = _price_book(held) or PriceBook(quotes={})
    wanted = sorted(
        i for i in held if (inst := book.instrument(i)) is not None and inst.kind in LIVE_KINDS
    )
    got: list[live.LiveQuote] = []
    fallbacks: list[Fallback] = []
    symbols: dict[Isin, str] = {}
    if wanted:
        try:
            symbols = _symbols(now.date())
        except live.LiveRejected as exc:
            fallbacks = [Fallback(isin=i, reason=str(exc)) for i in wanted]
            wanted = []
    quotes = list(eod_book.quotes.values())
    for isin in wanted:
        symbol = symbols.get(isin)
        if symbol is None:
            fallbacks.append(Fallback(isin=isin, reason="not listed on NSE under this ISIN"))
            continue
        try:
            q = live.fetch_quote(isin, symbol)
        except live.LiveRejected as exc:
            fallbacks.append(Fallback(isin=isin, reason=str(exc)))
            continue
        as_price = live.as_quote(q, now)
        if as_price is None:
            fallbacks.append(
                Fallback(
                    isin=isin, reason=f"{symbol}: last trade {q.at:%Y-%m-%d %H:%M} is not live"
                )
            )
            continue
        got.append(q)
        quotes.append(as_price)
    view = LiveView(
        at=now, market_open=live.market_open(now), live=tuple(got), end_of_day=tuple(fallbacks)
    )
    return PriceBook.best(quotes), view


def _print_live(view: LiveView, book: Book) -> None:
    state = "open" if view.market_open else "closed"
    typer.echo(f"{view.at:%Y-%m-%d %H:%M} IST, market {state}")
    for q in view.live:
        inst = book.instrument(q.isin)
        name = inst.name if inst else q.isin
        typer.echo(f"  LIVE  {q.symbol:<12} \u20b9{_rupees(q.price)}  at {q.at:%H:%M}  {name}")
    for f in view.end_of_day:
        typer.echo(f"  CLOSE {f.isin}  kept the end-of-day price: {f.reason}")


def _rupees(p: Price) -> str:
    whole, frac = divmod(p.micro_rupees, 1_000_000)
    return f"{whole:,}.{frac // 10_000:02d}"


@prices_app.command("live")
def prices_live(as_json: JsonFlag = False) -> None:
    """Today's last traded price for each share and ETF you hold (public quotes,
    a few minutes behind, no login). Nothing is stored."""
    with _open_ledger() as ledger:
        book = ledger.book()
    _, view = _live_pass(book)
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
    else:
        _print_live(view, book)


@app.command()
def watch(
    every: Annotated[
        int, typer.Option(help="Seconds between checks while the market is open.", min=60)
    ] = 300,
    once: Annotated[bool, typer.Option("--once", help="Check once and stop.")] = False,
    send: Annotated[bool, typer.Option("--send", help="Send new alerts to Telegram.")] = False,
) -> None:
    """While NSE is open, re-price your shares from live quotes, work out the
    verdicts and keep (and with --send, send) an alert when one turns into add,
    trim or exit. Read-only: it never places an order. Ctrl+C stops it."""
    try:
        while True:
            now = india_now()
            if once:
                _watch_pass(send)
                return
            if live.market_open(now):
                with suppress(typer.Exit):  # said on stderr; tried again next pass
                    _watch_pass(send)
            wait = every if live.market_open(now) else _until_open(now)
            if not live.market_open(now):
                typer.echo(
                    f"market closed; next check {now + timedelta(seconds=wait):%a %H:%M} IST"
                )
            _sleep(wait)
    except KeyboardInterrupt:
        typer.echo("stopped")


def _until_open(now: datetime) -> int:
    """Seconds to the next 09:15 IST on a weekday, at most an hour (so a
    sleeping laptop catches up soon after it wakes)."""
    t = now.astimezone(live.IST)
    nxt = t.replace(hour=9, minute=15, second=0, microsecond=0)
    while nxt <= t or nxt.weekday() >= 5:
        nxt = (nxt + timedelta(days=1)).replace(hour=9, minute=15)
    return max(60, min(3600, int((nxt - t).total_seconds())))


def _sleep(seconds: int) -> None:
    time.sleep(seconds)


def _watch_pass(send: bool) -> None:
    with _open_ledger() as ledger:
        book = ledger.book()
    prices, view = _live_pass(book)
    with _open_theses() as theses:
        found = for_book(book, prices, theses.all())
    with _open_alerts() as store:
        added = store.record(found, made=view.at)
    typer.echo(
        f"{view.at:%H:%M} IST: {len(view.live)} live, {len(view.end_of_day)} at close, "
        f"{len(added)} new alert{'' if len(added) == 1 else 's'}"
    )
    _alert_lines(added)
    if send:
        with suppress(typer.Exit):  # said already; pending ones go out next pass
            _send_pending()


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
