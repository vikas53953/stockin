"""Alerts: one message when a holding's verdict turns into add, trim or exit.

An alert is never a new opinion. It is the verdict you already see in
`stockin verdicts`, sent once at the moment it changes. Its sentence is the
verdict's own sentence, so it carries the same numbers.

The rule (`changes`) is a pure function of the last call remembered for each
holding and the verdicts now:

- a holding whose call is add, trim or exit, and differs from the call
  remembered for it, gives one alert;
- every other case gives none, and the call now is remembered, except
  review: review means a thesis or a price is missing for now, which says
  nothing new about the holding, so the call before it is kept.

So a verdict that stays trim alerts once, not every run; one that goes trim,
hold, trim alerts twice (the hold re-arms it); trim, review, trim alerts
once; and running twice on the same numbers sends nothing the second time.

An alert not yet sent is withdrawn when its holding's call changes again
(trim, then hold before it went out): only what is still true is sent.

alerts.db sits beside ledger.db and is private (0600) like it. It keeps the
remembered calls and an outbox. `record` writes the new calls and the alerts
they make in one transaction, so a crash leaves either both or neither. A
sender marks each alert sent after delivering it; an alert that could not be
delivered stays pending and is tried again on the next run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Self, get_args

from pydantic import ValidationError

from .facts import Isin, parse_isin
from .money import Value
from .thesis import Call, Verdict

ACTIONABLE: frozenset[Call] = frozenset({"add", "trim", "exit"})
"""The calls that ask you to do something. Hold and review never alert:
review means a thesis or a price is missing, which `stockin verdicts`
already says."""


class Alert(Value):
    isin: Isin
    name: str
    call: Call
    because: str


class Sent(Value):
    """An alert as kept in the outbox."""

    id: int
    made: datetime
    alert: Alert
    sent: datetime | None


def text(alert: Alert) -> str:
    """The message, from the verdict's own sentence."""
    return (
        f"{alert.call.upper()} {alert.name}\n{alert.because}\n"
        "(your own rule applied to your own numbers; not advice)"
    )


def changes(
    before: Mapping[Isin, Call], now: Iterable[Verdict]
) -> tuple[tuple[Alert, ...], dict[Isin, Call]]:
    """The alerts the verdicts `now` make, given the call remembered for each
    holding, and the calls to remember next. A holding you no longer hold is
    forgotten, so buying it again starts fresh. Alerts come in ISIN order,
    whatever order the verdicts came in."""
    after: dict[Isin, Call] = {}
    alerts: list[Alert] = []
    for v in sorted(now, key=lambda v: v.isin):
        if v.isin in after:
            raise ValueError(f"two verdicts for {v.isin}")
        after[v.isin] = before.get(v.isin, v.call) if v.call == "review" else v.call
        if v.call in ACTIONABLE and before.get(v.isin) != v.call:
            alerts.append(Alert(isin=v.isin, name=v.name, call=v.call, because=v.because))
    return tuple(alerts), after


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS calls(isin TEXT PRIMARY KEY NOT NULL, call TEXT NOT NULL)"
    " WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " made TEXT NOT NULL, body TEXT NOT NULL, sent TEXT)",
)
_CALLS = frozenset(get_args(Call))


class AlertsDamaged(ValueError):
    """alerts.db exists but cannot be read as Stockin's."""


class AlertsBlocked(AlertsDamaged):
    """alerts.db is fine but cannot be used now: busy, read-only, or its
    folder cannot be written. Nothing needs moving aside."""


class AlertStore:
    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    def open(cls, path: Path) -> Self:
        if path.is_symlink() and not path.exists():
            raise AlertsDamaged(f"{path} points at a file that is not there")
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600)  # what you hold and what you were told is private
            except OSError as exc:
                raise AlertsBlocked(f"{path} could not be created ({exc.strerror})") from None
        if not path.is_file():
            raise AlertsDamaged(f"{path} is not a file")
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        store = cls(conn, path)
        try:
            with _guard(path), conn:
                for stmt in _SCHEMA:
                    conn.execute(stmt)
            store.calls()  # every row must read back
            store.outbox()
        except AlertsDamaged:
            conn.close()
            raise
        return store

    def calls(self) -> dict[Isin, Call]:
        with _guard(self._path):
            rows = self._conn.execute("SELECT isin, call FROM calls").fetchall()
            out: dict[Isin, Call] = {}
            for isin, call in rows:
                if call not in _CALLS or not isinstance(isin, str) or parse_isin(isin) != isin:
                    raise ValueError(f"unknown call {call!r} for {isin!r}")
                out[Isin(isin)] = call
        return out

    def record(self, now: Iterable[Verdict], made: datetime) -> tuple[Sent, ...]:
        """Apply `changes` to the remembered calls and put its alerts in the
        outbox, in one transaction. Returns the alerts it added."""
        added: list[Sent] = []
        with _guard(self._path), self._conn:
            self._conn.execute("BEGIN IMMEDIATE")  # read and write as one, against a second check
            alerts, after = changes(self.calls(), now)
            for p in self.outbox(pending_only=True):
                if after.get(p.alert.isin) != p.alert.call:  # no longer true: withdraw it
                    self._conn.execute("DELETE FROM outbox WHERE id = ?", (p.id,))
            self._conn.execute("DELETE FROM calls")
            self._conn.executemany("INSERT INTO calls VALUES (?, ?)", sorted(after.items()))
            for a in alerts:
                cur = self._conn.execute(
                    "INSERT INTO outbox(made, body) VALUES (?, ?)",
                    (made.isoformat(), a.model_dump_json()),
                )
                assert cur.lastrowid is not None
                added.append(Sent(id=cur.lastrowid, made=made, alert=a, sent=None))
        return tuple(added)

    def outbox(self, pending_only: bool = False) -> tuple[Sent, ...]:
        """Alerts kept, oldest first."""
        where = " WHERE sent IS NULL" if pending_only else ""
        with _guard(self._path):
            rows = self._conn.execute(
                f"SELECT id, made, body, sent FROM outbox{where} ORDER BY id"
            ).fetchall()
            return tuple(
                Sent(
                    id=i,
                    made=datetime.fromisoformat(m),
                    alert=Alert.model_validate_json(b),
                    sent=datetime.fromisoformat(s) if s is not None else None,
                )
                for i, m, b, s in rows
            )

    def mark_sent(self, id: int, when: datetime) -> None:
        with _guard(self._path), self._conn:
            self._conn.execute(
                "UPDATE outbox SET sent = ? WHERE id = ? AND sent IS NULL", (when.isoformat(), id)
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def _guard(path: Path) -> Generator[None]:
    try:
        yield
    except sqlite3.OperationalError as exc:
        said = str(exc)
        if (
            "locked" in said
            or "readonly" in said
            or "read-only" in said
            or "unable to open" in said
        ):
            raise AlertsBlocked(f"{path} could not be used now ({said})") from None
        raise AlertsDamaged(f"{path} could not be read or written ({said})") from None
    except ValidationError:
        raise AlertsDamaged(f"{path} holds an alert Stockin cannot read") from None
    except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
        raise AlertsDamaged(f"{path} could not be read or written ({exc})") from None
