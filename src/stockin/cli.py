"""The shell: parse argv, read the environment, make one call, print.

Every command takes --json and prints the same pydantic object the human
output is rendered from, so the verify skill and the owner see one truth.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from . import __version__
from .book import Holding, ReconcileReport, reconcile
from .money import Value
from .sources import cas
from .store import ImportReport, Ledger, LedgerMissing, LedgerWriter

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
    ledger = root / "ledger.db"
    checks.append(
        DoctorCheck(name="ledger", status="ok", detail=str(ledger))
        if ledger.is_file()
        else DoctorCheck(
            name="ledger",
            status="warn",
            detail="no ledger yet",
            hint="created by your first `stockin import cas`",
        )
    )
    checks.append(
        DoctorCheck(
            name="quotes",
            status="warn",
            detail="no live quote source configured",
            hint="arrives with the prices PR",
        )
    )
    return Doctor(
        version=__version__,
        data_dir=str(root),
        ok=all(c.status != "fail" for c in checks),
        checks=tuple(checks),
    )


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


def _holding_lines(ledger: Ledger, holdings: tuple[Holding, ...]) -> list[str]:
    book = ledger.book()
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
        lines.append(f"  {h.isin}  {name[:40]:<40} {h.qty!s:>16}  {cost}")
    return lines


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
    with LedgerWriter.open(ledger_path()) as writer:
        report: ImportReport = writer.ingest(statement)
        lines = _holding_lines(writer, report.holdings)
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


@app.command()
def holdings(as_json: JsonFlag = False) -> None:
    """Units held per account and ISIN, folded from every imported statement."""
    with _open_ledger() as ledger:
        rows = ledger.book().holdings()
        if as_json:
            typer.echo(HoldingsView(holdings=rows).model_dump_json(indent=2))
            return
        if not rows:
            typer.echo("no holdings")
        for line in _holding_lines(ledger, rows):
            typer.echo(line)


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


def main() -> None:
    app()
