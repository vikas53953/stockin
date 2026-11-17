"""The shell: parse argv, read the clock, open the Desk or Clerk, make ONE
call, print. Every command takes --json and prints `model_dump_json()` of
the same object the human table is rendered from. These bodies are the
real call sites the types were derived from.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .app import Clerk, Config, Desk
from .facts import Thesis
from .money import Price, percent


def _show(result: object, as_json: bool) -> None:
    raise NotImplementedError


def import_cas(cfg: Config, pdf: Path, password: str, as_json: bool) -> int:
    """stockin import cas ~/data/cas/2026-10-cams.pdf   (twice = facts_added 0)"""
    _show(Clerk.open(cfg).import_cas(pdf, password), as_json)
    return 0


def reconcile_cmd(cfg: Config, as_json: bool) -> int:
    """stockin reconcile  -> OK, or ISIN + both numbers and exit 1"""
    report = Desk.open(cfg).reconcile()
    _show(report, as_json)
    return 0 if report.ok else 1


def thesis_add(cfg: Config, symbol: str, why: str, invalidate_below: str | None,
               target: str | None, max_weight: str | None, review_on: date | None) -> int:
    """stockin thesis add INFY --why ... --invalidate-below 1350 --target 1900
    --max-weight 8 --review 2027-01-15"""
    clerk = Clerk.open(cfg)
    clerk.record(Thesis(
        isin=clerk.book().resolve(symbol),             # INFY -> INE009A01021
        why=why,
        invalidate_below=Price.rupees(invalidate_below) if invalidate_below else None,
        target=Price.rupees(target) if target else None,
        max_weight=percent(max_weight) if max_weight else None,   # "8" -> Bps(800)
        review_on=review_on, recorded_on=date.today(),
    ))
    return 0


def review_cmd(cfg: Config, as_json: bool) -> int:
    """stockin review"""
    _show(Desk.open(cfg).review(date.today()), as_json)
    return 0


def networth_cmd(cfg: Config, as_json: bool) -> int:
    """stockin networth"""
    _show(Desk.open(cfg).networth(date.today()), as_json)
    return 0
