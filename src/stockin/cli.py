"""The shell: parse argv, read the environment, make one call, print.

Every command takes --json and prints the same pydantic object the human
output is rendered from, so the verify skill and the owner see one truth.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer

from . import __version__
from .money import Value

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Private, read-only add/trim/exit decisions for your holdings.",
)

Status = Literal["ok", "warn", "fail"]


class Check(Value):
    name: str
    status: Status
    detail: str
    hint: str | None = None


class Doctor(Value):
    version: str
    data_dir: str
    ok: bool
    checks: tuple[Check, ...]


def data_dir() -> Path:
    """$STOCKIN_DATA, else ./data. Real data lives only here; git ignores it."""
    return Path(os.environ.get("STOCKIN_DATA", "data")).expanduser()


def diagnose(root: Path) -> Doctor:
    """Read-only. Never creates, writes or deletes anything."""
    checks: list[Check] = []
    py = sys.version_info
    checks.append(
        Check(name="python", status="ok", detail=f"{py.major}.{py.minor}.{py.micro}")
        if py >= (3, 12)
        else Check(
            name="python",
            status="fail",
            detail=f"{py.major}.{py.minor}",
            hint="Stockin needs Python 3.12 or newer",
        )
    )
    if not root.is_dir():
        checks.append(
            Check(
                name="data_dir",
                status="fail",
                detail=f"{root} does not exist",
                hint=f"mkdir -p {root} (or set STOCKIN_DATA)",
            )
        )
    elif not os.access(root, os.W_OK):
        checks.append(Check(name="data_dir", status="fail", detail=f"{root} is not writable"))
    else:
        checks.append(Check(name="data_dir", status="ok", detail=str(root)))
    ledger = root / "ledger.db"
    checks.append(
        Check(name="ledger", status="ok", detail=str(ledger))
        if ledger.is_file()
        else Check(
            name="ledger",
            status="warn",
            detail="no ledger yet",
            hint="created by your first `stockin import cas`",
        )
    )
    checks.append(
        Check(
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


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def main() -> None:
    app()
