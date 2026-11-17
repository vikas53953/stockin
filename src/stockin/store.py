"""SQLite as a durable set of facts (design/ledger/sketch/store.py).

Two handles make read-only structural: `Ledger` has no write method and sets
`PRAGMA query_only=ON`; only `LedgerWriter` can insert. Every write is a set
union keyed by content hash, so the ledger depends on the SET of imported
files, never their order, and `digest()` proves it.

Schema (ledger.db, WAL). No autoincrement, no wall-clock column:
  facts(id PK, fact, account, isin, on_date, body JSON)
  provenance(fact_id, statement_sha)       which statements asserted it
  statements(sha256 PK, source, period_start, period_end)
  instruments(isin, sha, body)             as each statement described it
  accounts(id, sha, body)                  likewise; reads take the latest
                                           statement's, ties to the lower sha
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import TypeAdapter

from .book import Book, Holding, fact_date
from .facts import Account, Fact, Instrument, OwnerFact, Statement, fact_id
from .money import Value

_FACT: TypeAdapter[Fact] = TypeAdapter(Fact)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, fact TEXT NOT NULL, account TEXT, isin TEXT,
  on_date TEXT NOT NULL, body TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS provenance(
  fact_id TEXT NOT NULL, statement_sha TEXT NOT NULL,
  PRIMARY KEY(fact_id, statement_sha)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS statements(
  sha256 TEXT PRIMARY KEY, source TEXT NOT NULL,
  period_start TEXT NOT NULL, period_end TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS instruments(
  isin TEXT NOT NULL, sha TEXT NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY(isin, sha)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT NOT NULL, sha TEXT NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY(id, sha)) WITHOUT ROWID;
PRAGMA user_version = 1;
"""

_TABLES = (
    ("facts", "id"),
    ("provenance", "fact_id, statement_sha"),
    ("statements", "sha256"),
    ("instruments", "isin, sha"),
    ("accounts", "id, sha"),
)


class LedgerMissing(FileNotFoundError):
    """Raised by a read handle when nothing has been imported yet."""


class ImportReport(Value):
    statement_sha: str
    source: str
    period_start: date
    period_end: date
    seen_before: bool  # this exact file was already imported
    facts_added: int  # 0 on a repeat
    facts_already_present: int
    holdings: tuple[Holding, ...]  # after import, for accounts in this statement
    warnings: tuple[str, ...]


class Ledger:
    """Read handle."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, db: Path) -> Self:
        if not db.is_file():
            raise LedgerMissing(f"no ledger at {db}; import a statement first")
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA query_only=ON")
        return cls(conn)

    def book(self, as_of: date | None = None) -> Book:
        """Every fact, parsed back through the pydantic union in one read
        transaction: the database is a boundary like any file."""
        with self._conn:
            facts = tuple(
                _FACT.validate_json(body)
                for (body,) in self._conn.execute("SELECT body FROM facts ORDER BY id")
            )
            instruments = tuple(
                Instrument.model_validate_json(body) for body in self._latest("instruments", "isin")
            )
            accounts = tuple(
                Account.model_validate_json(body) for body in self._latest("accounts", "id")
            )
        return Book.of(facts, instruments, accounts, as_of)

    def _latest(self, table: str, key: str) -> list[str]:
        """One body per key: from the statement with the latest period end,
        ties to the lower sha, so the answer never depends on import order."""
        rows = self._conn.execute(
            f"""SELECT t.{key}, t.body FROM {table} t JOIN statements s ON s.sha256 = t.sha
                ORDER BY t.{key}, s.period_end DESC, t.sha"""
        )
        out: dict[str, str] = {}
        for k, body in rows:
            out.setdefault(k, body)
        return list(out.values())

    def statements(self) -> int:
        (n,) = self._conn.execute("SELECT count(*) FROM statements").fetchone()
        return int(n)

    def digest(self) -> str:
        """sha256 over every table in primary-key order. Equal digests mean
        equal ledgers; used to prove import order does not matter."""
        h = hashlib.sha256()
        for table, key in _TABLES:
            h.update(f"\x00{table}\x00".encode())
            for row in self._conn.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                h.update(repr(tuple(row)).encode())
                h.update(b"\n")
        return h.hexdigest()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class LedgerWriter(Ledger):
    """Write handle. Every verb is a set union or its exact inverse."""

    @classmethod
    def open(cls, db: Path) -> Self:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.isolation_level = "DEFERRED"
        return cls(conn)

    def ingest(self, statement: Statement) -> ImportReport:
        """One transaction, all or nothing. Never deletes: a wrong fact shows
        up as a reconcile mismatch and is removed with `retract`."""
        sha = statement.sha256
        added = 0
        with self._conn:
            seen = self._conn.execute("SELECT 1 FROM statements WHERE sha256=?", (sha,)).fetchone()
            self._conn.execute(
                "INSERT OR IGNORE INTO statements VALUES (?,?,?,?)",
                (
                    sha,
                    statement.source,
                    statement.period_start.isoformat(),
                    statement.period_end.isoformat(),
                ),
            )
            for fact in statement.facts:
                added += self._insert(fact)
                self._conn.execute(
                    "INSERT OR IGNORE INTO provenance VALUES (?,?)", (fact_id(fact), sha)
                )
            for account in statement.accounts:
                self._conn.execute(
                    "INSERT OR IGNORE INTO accounts VALUES (?,?,?)",
                    (account.id, sha, account.model_dump_json()),
                )
            for inst in statement.instruments:
                self._conn.execute(
                    "INSERT OR IGNORE INTO instruments VALUES (?,?,?)",
                    (inst.isin, sha, inst.model_dump_json()),
                )
        accounts = {a.id for a in statement.accounts}
        holdings = tuple(h for h in self.book().holdings() if h.account in accounts)
        return ImportReport(
            statement_sha=sha,
            source=statement.source,
            period_start=statement.period_start,
            period_end=statement.period_end,
            seen_before=seen is not None,
            facts_added=added,
            facts_already_present=len({fact_id(f) for f in statement.facts}) - added,
            holdings=holdings,
            warnings=statement.warnings,
        )

    def record(self, *facts: OwnerFact) -> int:
        """Owner-typed facts. Returns how many were new."""
        with self._conn:
            return sum(self._insert(f) for f in facts)

    def retract(self, statement_sha: str) -> int:
        """Forget one statement: its provenance, then any statement fact no
        other statement still asserts. Returns facts removed."""
        with self._conn:
            orphans = [
                fid
                for (fid,) in self._conn.execute(
                    """SELECT fact_id FROM provenance p WHERE statement_sha=?
                       AND NOT EXISTS (SELECT 1 FROM provenance q
                                       WHERE q.fact_id=p.fact_id AND q.statement_sha<>?)""",
                    (statement_sha, statement_sha),
                )
            ]
            self._conn.execute("DELETE FROM provenance WHERE statement_sha=?", (statement_sha,))
            self._conn.executemany("DELETE FROM facts WHERE id=?", [(f,) for f in orphans])
            self._conn.execute("DELETE FROM statements WHERE sha256=?", (statement_sha,))
            self._conn.execute("DELETE FROM instruments WHERE sha=?", (statement_sha,))
            self._conn.execute("DELETE FROM accounts WHERE sha=?", (statement_sha,))
        return len(orphans)

    def _insert(self, fact: Fact) -> int:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?)",
            (
                fact_id(fact),
                fact.fact,
                getattr(fact, "account", None),
                getattr(fact, "isin", None),
                fact_date(fact).isoformat(),
                _FACT.dump_json(fact).decode(),
            ),
        )
        return cur.rowcount
