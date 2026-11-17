"""Book: the whole ledger in memory, immutable, and every unit-level derivation.

One investor's lifetime is about 1e4 facts, so folding all of them takes
milliseconds and there is no incremental state to keep in sync.

The single fold is `Book.timeline(account, isin)`. Holdings, lots and
reconcile all read the same Timeline, so they cannot disagree.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .facts import (
    Account,
    AccountId,
    Balance,
    Bonus,
    Buy,
    Complete,
    Coverage,
    Fact,
    FixedDeposit,
    GoldHolding,
    Income,
    Instrument,
    Isin,
    Sell,
    Split,
    StatedBalance,
    Thesis,
    Txn,
    fact_id,
)
from .money import Money, Qty, QtyDelta, Value

_INTRADAY = {"split": 0, "bonus": 1, "buy": 2, "income": 3, "sell": 4}
"""Same-day order: corporate actions first, then buys before sells, so a
same-day buy and sell never looks like selling what you don't hold."""


class Inferred(Value):
    """A unit change no statement itemised: the gap between what recorded
    txns explain and what a Balance asserts, in an uncovered window. Derived
    on every read, never stored, so importing the missing trades later makes
    it vanish. Dated at the asserting Balance (the latest possible date)."""

    account: AccountId
    isin: Isin
    on: date
    delta: QtyDelta


class Check(Value):
    """One Balance assertion (or an oversell), judged."""

    account: AccountId
    isin: Isin
    on: date
    stated: Qty
    computed: Qty
    status: Literal["match", "inferred", "mismatch"]


class Lot(Value):
    """Open FIFO lot. `cost is None` means genuinely unknown (acquired through
    an Inferred movement), which is different from zero (a Bonus)."""

    acquired_on: date
    qty: Qty
    cost: Money | None


class Disposal(Value):
    acquired_on: date
    sold_on: date
    qty: Qty
    cost: Money | None
    proceeds: Money | None  # None when the disposal itself was Inferred

    @property
    def gain(self) -> Money | None:
        if self.cost is None or self.proceeds is None:
            return None
        return self.proceeds - self.cost


class Timeline(Value):
    """Everything known about one (account, isin), in date order.

    Invariant: sum(lot.qty) == qty. `qty` equals the last Balance when every
    check is match or inferred and no txn follows it; after a mismatch it is
    what the txns explain, so the error stays visible downstream."""

    account: AccountId
    isin: Isin
    movements: tuple[Txn | Inferred, ...]
    checks: tuple[Check, ...]
    qty: Qty
    lots: tuple[Lot, ...]
    disposals: tuple[Disposal, ...]


class Holding(Value):
    """`stockin import cas` prints these, grouped by account."""

    account: AccountId
    isin: Isin
    qty: Qty
    cost: Money | None  # None if any open lot has unknown cost


class Position(Value):
    """One ISIN across all accounts. The unit a Thesis and a Verdict attach to."""

    isin: Isin
    qty: Qty
    cost: Money | None
    holdings: tuple[Holding, ...]


ManualAsset = StatedBalance | FixedDeposit | GoldHolding


def _fact_date(fact: Fact) -> date:
    match fact:
        case Txn() | Balance() | Complete():
            return fact.on
        case Coverage():
            return fact.start
        case StatedBalance() | GoldHolding():
            return fact.as_of
        case FixedDeposit():
            return fact.start
        case Thesis():
            return fact.recorded_on


def _sum_cost(lots: tuple[Lot, ...]) -> Money | None:
    total = Money(paise=0)
    for lot in lots:
        if lot.cost is None:
            return None
        total = total + lot.cost
    return total


@dataclass
class _OpenLot:
    acquired_on: date
    micros: int
    cost: Money | None


class Book:
    """Immutable snapshot of every fact dated on or before `as_of`.

    Built by `Ledger.book()` and, in tests, by `Book.of(...)`. A plain class
    rather than a pydantic model, because it is never serialized and it
    caches timelines (see design/ledger/rationale.md, reconciliation)."""

    __slots__ = ("_accounts", "_as_of", "_cache", "_facts", "_instruments")

    def __init__(
        self,
        facts: tuple[Fact, ...],
        instruments: dict[Isin, Instrument],
        accounts: dict[AccountId, Account],
        as_of: date | None,
    ) -> None:
        self._facts = facts
        self._instruments = instruments
        self._accounts = accounts
        self._as_of = as_of
        self._cache: dict[tuple[AccountId, Isin], Timeline] = {}

    @classmethod
    def of(
        cls,
        facts: tuple[Fact, ...],
        instruments: tuple[Instrument, ...] = (),
        accounts: tuple[Account, ...] = (),
        as_of: date | None = None,
    ) -> Book:
        kept = facts if as_of is None else tuple(f for f in facts if _fact_date(f) <= as_of)
        # Set semantics: a fact asserted twice is one fact.
        unique = tuple({fact_id(f): f for f in kept}.values())
        return cls(
            unique,
            {i.isin: i for i in instruments},
            {a.id: a for a in accounts},
            as_of,
        )

    @property
    def as_of(self) -> date | None:
        return self._as_of

    @property
    def facts(self) -> tuple[Fact, ...]:
        return self._facts

    def account(self, account: AccountId) -> Account | None:
        return self._accounts.get(account)

    def instrument(self, isin: Isin) -> Instrument | None:
        return self._instruments.get(isin)

    # ------------------------------------------------------------------ fold

    def pairs(self) -> tuple[tuple[AccountId, Isin], ...]:
        """Every (account, isin) any Txn or Balance mentions, sorted."""
        seen = {(f.account, f.isin) for f in self._facts if isinstance(f, Txn | Balance)}
        return tuple(sorted(seen))

    def timeline(self, account: AccountId, isin: Isin) -> Timeline:
        """THE fold. Cached per Book instance (a Book never changes)."""
        key = (account, isin)
        if key not in self._cache:
            self._cache[key] = self._fold(account, isin)
        return self._cache[key]

    def _fold(self, account: AccountId, isin: Isin) -> Timeline:
        txns = sorted(
            (
                f
                for f in self._facts
                if isinstance(f, Txn) and f.account == account and f.isin == isin
            ),
            key=lambda t: (t.on, _INTRADAY[t.event.kind], t.nth),
        )
        stated: dict[date, set[int]] = defaultdict(set)
        for f in self._facts:
            if isinstance(f, Balance) and f.account == account and f.isin == isin:
                stated[f.on].add(f.qty.micros)
        first_seen = min([t.on for t in txns] + list(stated), default=None)
        for f in self._facts:
            if (
                isinstance(f, Complete)
                and f.account == account
                and first_seen is not None
                and first_seen < f.on
                and f.on not in stated
            ):
                stated[f.on].add(0)  # complete listing omits it: it is zero
        windows = _merge(
            [
                (f.start, f.end)
                for f in self._facts
                if isinstance(f, Coverage) and f.account == account and f.isin == isin
            ]
        )

        fold = _Fold(account, isin)
        pending = list(txns)
        prev: date | None = None
        for on in sorted(stated):
            while pending and pending[0].on <= on:
                fold.apply(pending.pop(0))
            claims = stated[on]
            if len(claims) > 1:  # two statements disagree about one day
                for qty in sorted(claims):
                    fold.check(on, qty, "mismatch")
                prev = on
                continue
            (qty,) = claims
            gap = qty - fold.running
            if gap == 0:
                fold.check(on, qty, "match")
            elif _covered(windows, prev, on):
                fold.check(on, qty, "mismatch")
            else:
                fold.check(on, qty, "inferred")
                fold.infer(on, gap)
            prev = on
        for txn in pending:  # after the last Balance: applied, nothing to check yet
            fold.apply(txn)
        return fold.timeline()

    # ----------------------------------------------------------------- views

    def holdings(self) -> tuple[Holding, ...]:
        """Non-zero timelines, ordered by (account, isin)."""
        out: list[Holding] = []
        for account, isin in self.pairs():
            tl = self.timeline(account, isin)
            if tl.qty.micros:
                out.append(Holding(account=account, isin=isin, qty=tl.qty, cost=_sum_cost(tl.lots)))
        return tuple(out)

    def positions(self) -> tuple[Position, ...]:
        """holdings() grouped by ISIN."""
        by_isin: dict[Isin, list[Holding]] = defaultdict(list)
        for h in self.holdings():
            by_isin[h.isin].append(h)
        out: list[Position] = []
        for isin in sorted(by_isin):
            group = tuple(by_isin[isin])
            costs = [h.cost for h in group]
            cost: Money | None = Money(paise=0)
            for c in costs:
                cost = None if cost is None or c is None else cost + c
            out.append(
                Position(
                    isin=isin,
                    qty=Qty(micros=sum(h.qty.micros for h in group)),
                    cost=cost,
                    holdings=group,
                )
            )
        return tuple(out)

    def manual_assets(self) -> tuple[ManualAsset, ...]:
        """Current ones only: the latest StatedBalance per (asset, label), the
        latest GoldHolding per label, and every FD whose term has not ended
        before as_of."""
        latest: dict[tuple[str, str], StatedBalance | GoldHolding] = {}
        fds: list[FixedDeposit] = []
        for f in self._facts:
            match f:
                case StatedBalance():
                    key = (f.asset, f.label)
                case GoldHolding():
                    key = ("gold", f.label)
                case FixedDeposit():
                    if self._as_of is None or _fd_maturity(f) >= self._as_of:
                        fds.append(f)
                    continue
                case _:
                    continue
            held = latest.get(key)
            if held is None or (f.as_of, fact_id(f)) > (held.as_of, fact_id(held)):
                latest[key] = f
        ordered = [latest[k] for k in sorted(latest)]
        return (*ordered, *sorted(fds, key=_fd_key))

    def thesis(self, isin: Isin) -> Thesis | None:
        """Latest by recorded_on (ties broken by fact id). History: `journal`."""
        entries = self.journal(isin)
        return entries[-1] if entries else None

    def journal(self, isin: Isin | None = None) -> tuple[Thesis, ...]:
        found = [
            f for f in self._facts if isinstance(f, Thesis) and (isin is None or f.isin == isin)
        ]
        return tuple(sorted(found, key=lambda t: (t.isin, t.recorded_on, fact_id(t))))

    def resolve(self, text: str) -> Isin:
        """'INFY' | AMFI code | ISIN -> Isin, from instruments this book knows.
        Raises LookupError listing candidates when ambiguous or unknown."""
        needle = text.strip().upper()
        hits = sorted(
            {
                i.isin
                for i in self._instruments.values()
                if needle in {i.isin, (i.symbol or "").upper(), (i.amfi_code or "").upper()}
            }
        )
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise LookupError(f"{text!r} is ambiguous: {', '.join(hits)}")
        raise LookupError(f"{text!r} matches no instrument you hold; use its ISIN")


def _fd_key(fd: FixedDeposit) -> tuple[date, str]:
    return (fd.start, fd.label)


def _fd_maturity(fd: FixedDeposit) -> date:
    month = fd.start.month - 1 + fd.months
    year, month = fd.start.year + month // 12, month % 12 + 1
    day = fd.start.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1  # 31 Jan + 1 month -> 28/29 Feb


def _merge(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered(windows: list[tuple[date, date]], prev: date | None, on: date) -> bool:
    """True when every day in (prev, on] lies inside one merged window. The
    first checkpoint (prev is None) reaches back forever, so is never covered."""
    if prev is None:
        return False
    first = prev + timedelta(days=1)
    return any(start <= first and on <= end for start, end in windows)


class _Fold:
    """Mutable working state for one fold. Never escapes `_fold`."""

    def __init__(self, account: AccountId, isin: Isin) -> None:
        self.account = account
        self.isin = isin
        self.running = 0
        self.lots: list[_OpenLot] = []
        self.movements: list[Txn | Inferred] = []
        self.checks: list[Check] = []
        self.disposals: list[Disposal] = []

    def apply(self, txn: Txn) -> None:
        self.movements.append(txn)
        match txn.event:
            case Buy(units=units, amount=amount):
                self.running += units.micros
                self.lots.append(_OpenLot(txn.on, units.micros, amount))
            case Bonus(units=units):
                self.running += units.micros
                self.lots.append(_OpenLot(txn.on, units.micros, Money(paise=0)))
            case Split(num=num, den=den):
                self._split(num, den)
            case Sell(units=units, amount=amount):
                if units.micros > self.running:  # selling more than we can explain
                    self.check(txn.on, units.micros, "mismatch")
                    self._consume(txn.on, self.running, None)
                else:
                    self._consume(txn.on, units.micros, amount)
            case Income():
                pass

    def infer(self, on: date, gap: int) -> None:
        self.movements.append(
            Inferred(account=self.account, isin=self.isin, on=on, delta=QtyDelta(micros=gap))
        )
        if gap > 0:
            self.running += gap
            self.lots.append(_OpenLot(on, gap, None))
        else:
            self._consume(on, -gap, None, inferred=True)

    def check(
        self, on: date, stated: int, status: Literal["match", "inferred", "mismatch"]
    ) -> None:
        self.checks.append(
            Check(
                account=self.account,
                isin=self.isin,
                on=on,
                stated=Qty(micros=stated),
                computed=Qty(micros=self.running),
                status=status,
            )
        )

    def _split(self, num: int, den: int) -> None:
        target = self.running * num // den
        for lot in self.lots:
            lot.micros = lot.micros * num // den
        if self.lots:  # flooring remainder goes to the oldest lot: sums stay exact
            self.lots[0].micros += target - sum(lot.micros for lot in self.lots)
        self.running = target

    def _consume(
        self, on: date, micros: int, proceeds: Money | None, *, inferred: bool = False
    ) -> None:
        """FIFO. Cost and proceeds are apportioned exactly with Money.split."""
        remaining_units = micros
        remaining_proceeds = proceeds
        while remaining_units and self.lots:
            lot = self.lots[0]
            take = min(lot.micros, remaining_units)
            cost: Money | None
            if lot.cost is None:
                cost = None
            elif take == lot.micros:
                cost, lot.cost = lot.cost, Money(paise=0)
            else:
                cost, lot.cost = lot.cost.split(Qty(micros=take), Qty(micros=lot.micros))
            share: Money | None = None
            if remaining_proceeds is not None and not inferred:
                share, remaining_proceeds = remaining_proceeds.split(
                    Qty(micros=take), Qty(micros=remaining_units)
                )
            self.disposals.append(
                Disposal(
                    acquired_on=lot.acquired_on,
                    sold_on=on,
                    qty=Qty(micros=take),
                    cost=cost,
                    proceeds=share,
                )
            )
            lot.micros -= take
            remaining_units -= take
            self.running -= take
            if lot.micros == 0:
                self.lots.pop(0)

    def timeline(self) -> Timeline:
        return Timeline(
            account=self.account,
            isin=self.isin,
            movements=tuple(self.movements),
            checks=tuple(self.checks),
            qty=Qty(micros=self.running),
            lots=tuple(
                Lot(acquired_on=lot.acquired_on, qty=Qty(micros=lot.micros), cost=lot.cost)
                for lot in self.lots
            ),
            disposals=tuple(self.disposals),
        )


# --------------------------------------------------------------- reconcile


class ReconcileReport(Value):
    checks: tuple[Check, ...]

    @property
    def mismatches(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status == "mismatch")

    @property
    def ok(self) -> bool:
        """No mismatch. `inferred` is not a failure: it is the honest label for
        'the depository told us the balance, nobody told us the trades'."""
        return not self.mismatches


def reconcile(book: Book) -> ReconcileReport:
    """Every check from every timeline. No logic of its own: reconcile cannot
    drift from holdings because it is a view of the same fold."""
    return ReconcileReport(
        checks=tuple(c for a, i in book.pairs() for c in book.timeline(a, i).checks)
    )
