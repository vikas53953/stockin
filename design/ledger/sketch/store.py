"""SQLite as a durable set of facts. Two handle types make read-only a
compile-time property: web, MCP and alerts are given `Ledger`, which has no
write method and sets `PRAGMA query_only=ON` on its connection. (Not
`mode=ro`: on a WAL database that fails when the -shm file is absent.) Only cli.py constructs a
`LedgerWriter`. (CI: an import-linter rule forbids `LedgerWriter` and
`sources` outside `stockin.app`.)

Schema (ledger.db, WAL). No autoincrement, no wall-clock column:
  facts(id TEXT PRIMARY KEY, fact TEXT, account TEXT, isin TEXT, on_date TEXT, body TEXT JSON) WITHOUT ROWID
  provenance(fact_id TEXT, statement_sha TEXT, PRIMARY KEY(fact_id, statement_sha))
  statements(sha256 TEXT PRIMARY KEY, source TEXT, period_start TEXT, period_end TEXT)
  instruments(isin TEXT PRIMARY KEY, body TEXT, as_of TEXT)
  accounts(id TEXT PRIMARY KEY, body TEXT)

Idempotency contract (the test the control CLI runs): `digest()` after
importing files {A, B} is identical for orders A,B / B,A / A,B,A. The ledger
is a function of the SET of inputs, which is stronger than "same file twice".
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Self

from .book import Book, Holding
from .facts import OwnerFact, Statement
from .money import Value


class ImportReport(Value):
    statement_sha: str
    seen_before: bool             # this exact file was already imported
    facts_added: int              # 0 on a repeat
    facts_already_present: int
    holdings: tuple[Holding, ...] # after import, for accounts in this statement


class Ledger:
    """Read handle."""

    @classmethod
    def open(cls, db: Path) -> Self:
        """sqlite3.connect(db) + PRAGMA query_only=ON. Raises
        LedgerMissing with the `stockin doctor` hint if absent."""
        raise NotImplementedError

    def book(self, as_of: date | None = None) -> Book:
        """Load every fact (dated <= as_of) in one read transaction and
        parse through the pydantic `Fact` union: the DB is a boundary too."""
        raise NotImplementedError

    def digest(self) -> str:
        """sha256 over all tables in primary-key order. The verification
        lever for idempotency and for backup-restore checks."""
        raise NotImplementedError

    def __enter__(self) -> Self: raise NotImplementedError
    def __exit__(self, *exc: object) -> None: raise NotImplementedError


class LedgerWriter(Ledger):
    """Write handle. Exactly two verbs; both are set-union."""

    @classmethod
    def open(cls, db: Path) -> Self:
        """Creates and migrates if needed. Takes the single writer lock."""
        raise NotImplementedError

    def ingest(self, statement: Statement) -> ImportReport:
        """One transaction, all or nothing:
          INSERT OR IGNORE statements(sha256, ...)
          for f in statement.facts: INSERT OR IGNORE facts(fact_id(f), ...)
                                    INSERT OR IGNORE provenance(fact_id(f), sha)
          accounts: INSERT OR IGNORE (id is content-derived)
          instruments: upsert only if statement.period_end > stored as_of
                       (ties: lower sha wins) -> deterministic, order-free
        Crash halfway -> rollback -> rerun is the same as a first run.
        Never deletes. A statement cannot retract a fact; a wrong fact shows
        up as a reconcile mismatch and is removed by `retract`."""
        raise NotImplementedError

    def record(self, *facts: OwnerFact) -> int:
        """INSERT OR IGNORE each. Returns how many were new."""
        raise NotImplementedError

    def retract(self, statement_sha: str) -> int:
        """Delete that statement's provenance rows, then facts left with no
        provenance. The only delete path; `--dry-run` prints the count."""
        raise NotImplementedError
