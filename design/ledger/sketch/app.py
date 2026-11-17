"""The one door every surface uses. (Graft from candidate C.)

CLI, dashboard, MCP and the Telegram job each make ONE call here and get a
frozen pydantic result. This module is the only place that assembles
Ledger + PriceStore + live QuoteSource + `today`, so no surface can price
a book differently from another (the base candidate's cli `_prices` helper
would have been copied into four places, and its networth call passed an
empty price set).

Two halves keep read-only structural:
  Desk   read: given to web, MCP, alerts and read-only CLI commands
  Clerk  write: given only to `stockin import|asset|thesis|retract`
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Mapping, Self

from .book import Book, Holding, ReconcileReport
from .facts import AssetClass, Isin, OwnerFact, Thesis
from .money import Bps, Value
from .networth import NetWorth
from .prices import PriceBook, QuoteSource
from .review import PositionReview, Review
from .store import ImportReport


class Config(Value):
    """Loaded and validated once at the shell. Paths and targets only;
    secrets are fetched by the caller (sops) and never land here."""

    ledger_db: Path
    prices_db: Path
    targets: Mapping[AssetClass, Bps]    # validated to sum to 10_000


class Desk:
    """Read facade. Every method is one Book fold plus one price lookup."""

    @classmethod
    def open(cls, cfg: Config, live: QuoteSource | None = None) -> Self:
        raise NotImplementedError

    def book(self) -> Book:
        """Escape hatch for tests and `doctor`; surfaces use the methods."""
        raise NotImplementedError

    def prices(self, book: Book) -> PriceBook:
        """EOD for every held key (positions + GOLD) overlaid with live.
        The ONLY price assembly in the codebase."""
        raise NotImplementedError

    def holdings(self) -> tuple[Holding, ...]: raise NotImplementedError
    def reconcile(self) -> ReconcileReport: raise NotImplementedError
    def networth(self, today: date) -> NetWorth: raise NotImplementedError
    def review(self, today: date) -> Review: raise NotImplementedError

    def position(self, symbol: str, today: date) -> PositionReview:
        """'INFY' | AMFI code | ISIN -> one review row (MCP position_review)."""
        raise NotImplementedError

    def journal(self, isin: Isin | None = None) -> tuple[Thesis, ...]:
        raise NotImplementedError

    def digest(self) -> str: raise NotImplementedError


class Clerk(Desk):
    """Write facade. Three verbs, all set-union, all idempotent."""

    def import_cas(self, pdf: Path, password: str) -> ImportReport:
        """sources.cas.read -> LedgerWriter.ingest. Password used once."""
        raise NotImplementedError

    def record(self, *facts: OwnerFact) -> int: raise NotImplementedError
    def retract(self, statement_sha: str, dry_run: bool = True) -> int: raise NotImplementedError
