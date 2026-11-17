"""Book: the whole ledger in memory, immutable, and every unit-level derivation.

One investor's lifetime is about 1e4 facts, so folding all of them takes
milliseconds and there is no incremental state to keep in sync.

The single fold is `Book.timeline(account, isin)`. Holdings, lots and
reconcile all read the same Timeline, so they cannot disagree.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
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

Pair = tuple[AccountId, Isin]
Window = tuple[date, date]

_INTRADAY = {"split": 0, "bonus": 1, "buy": 2, "sell": 3, "income": 4}
"""Same-day order: corporate actions first, then buys before sells, so a
same-day buy and sell never looks like selling what you don't hold."""


class Inferred(Value):
    """A unit change no statement itemised, in an uncovered window: either the
    gap between recorded txns and a Balance, or units a sell needs that no
    recorded buy explains. Derived on every read, never stored, so importing
    the missing trades later makes it vanish."""

    account: AccountId
    isin: Isin
    on: date
    delta: QtyDelta


class Check(Value):
    """One judged assertion about units.

    kind="balance": a statement said `stated` units were held on `on`.
    kind="oversell": a statement said `stated` units were sold on `on`, inside
    a covered window, but only `computed` were held."""

    kind: Literal["balance", "oversell"]
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

    Invariants: sum(lot.qty) == qty and no lot is empty. `qty` equals the last
    Balance when every check is match or inferred and no txn follows it; after
    a mismatch it is what the txns explain, so the error stays visible."""

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


def fact_date(fact: Fact) -> date:
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


def _sum_known(costs: Iterable[Money | None]) -> Money | None:
    """Sum, or None as soon as any part is unknown."""
    total = Money(paise=0)
    for cost in costs:
        if cost is None:
            return None
        total = total + cost
    return total


def _txn_key(txn: Txn) -> tuple[date, int, int, str]:
    """Total order. fact_id breaks ties between different same-day txns (a
    lump sum and a SIP), so FIFO never depends on input order."""
    return (txn.on, _INTRADAY[txn.event.kind], txn.nth, fact_id(txn))


_OF = object()


class Book:
    """Immutable snapshot of every fact dated on or before `as_of`.

    Build it only with `Book.of(...)` (tests) or `Ledger.book()`. A plain class
    rather than a pydantic model: it is never serialized, it indexes the facts
    once, and it caches timelines (design/ledger/rationale.md, reconciliation)."""

    def __init__(
        self,
        facts: tuple[Fact, ...],
        instruments: dict[Isin, Instrument],
        accounts: dict[AccountId, Account],
        as_of: date | None,
        *,
        token: object = None,
    ) -> None:
        if token is not _OF:
            raise TypeError("use Book.of(...) to build a Book")
        self._facts = facts
        self._instruments = instruments
        self._accounts = accounts
        self._as_of = as_of
        self._cache: dict[Pair, Timeline] = {}
        txns: dict[Pair, list[Txn]] = defaultdict(list)
        balances: dict[Pair, dict[date, set[int]]] = defaultdict(lambda: defaultdict(set))
        coverage: dict[Pair, list[Window]] = defaultdict(list)
        complete: dict[AccountId, list[date]] = defaultdict(list)
        theses: list[Thesis] = []
        manual: list[ManualAsset] = []
        for f in facts:  # the only scan of the fact tuple
            match f:
                case Txn():
                    txns[(f.account, f.isin)].append(f)
                case Balance():
                    balances[(f.account, f.isin)][f.on].add(f.qty.micros)
                case Coverage():
                    coverage[(f.account, f.isin)].append((f.start, f.end))
                case Complete():
                    complete[f.account].append(f.on)
                case Thesis():
                    theses.append(f)
                case StatedBalance() | FixedDeposit() | GoldHolding():
                    manual.append(f)
        self._txns = {pair: sorted(ts, key=_txn_key) for pair, ts in txns.items()}
        self._balances = {pair: dict(days) for pair, days in balances.items()}
        self._coverage = {pair: _merge(ws) for pair, ws in coverage.items()}
        self._complete = {acct: sorted(days) for acct, days in complete.items()}
        self._theses = sorted(theses, key=lambda t: (t.isin, t.recorded_on, fact_id(t)))
        self._manual = manual

    @classmethod
    def of(
        cls,
        facts: tuple[Fact, ...],
        instruments: tuple[Instrument, ...] = (),
        accounts: tuple[Account, ...] = (),
        as_of: date | None = None,
    ) -> Book:
        kept = facts if as_of is None else tuple(f for f in facts if fact_date(f) <= as_of)
        unique = tuple({fact_id(f): f for f in kept}.values())  # asserted twice is one fact
        return cls(
            unique,
            {i.isin: i for i in instruments},
            {a.id: a for a in accounts},
            as_of,
            token=_OF,
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

    def pairs(self) -> tuple[Pair, ...]:
        """Every (account, isin) any Txn or Balance mentions, sorted."""
        return tuple(sorted(self._txns.keys() | self._balances.keys()))

    def timeline(self, account: AccountId, isin: Isin) -> Timeline:
        """THE fold. Cached per Book instance (a Book never changes)."""
        key = (account, isin)
        if key not in self._cache:
            self._cache[key] = self._fold(key)
        return self._cache[key]

    def _fold(self, pair: Pair) -> Timeline:
        txns = self._txns.get(pair, [])
        stated, implied = self._checkpoints(pair, txns)
        windows = self._coverage.get(pair, [])
        fold = _Fold(pair, windows)
        i = 0
        for on in sorted(stated):
            while i < len(txns) and txns[i].on <= on:
                fold.apply(txns[i])
                i += 1
            claims = stated[on]
            if on in implied and fold.running == 0:
                fold.checkpoint = on  # omitting a position already at zero says nothing new
                continue
            if len(claims) > 1:  # two statements disagree about one day
                for qty in sorted(claims):
                    fold.check("balance", on, qty, "mismatch")
            else:
                (qty,) = claims
                gap = qty - fold.running
                if gap == 0:
                    fold.check("balance", on, qty, "match")
                elif _covered(windows, fold.checkpoint, on):
                    fold.check("balance", on, qty, "mismatch")
                else:
                    fold.check("balance", on, qty, "inferred")
                    fold.infer(on, gap)
            fold.checkpoint = on
        for txn in txns[i:]:  # after the last Balance: applied, nothing to check yet
            fold.apply(txn)
        return fold.timeline()

    def _checkpoints(self, pair: Pair, txns: list[Txn]) -> tuple[dict[date, set[int]], set[date]]:
        """Stated balances, plus an implied zero on each Complete listing that
        omits this ISIN after it was first seen. Returns (claims, implied)."""
        stated = {on: set(qtys) for on, qtys in self._balances.get(pair, {}).items()}
        first_seen = min([*(t.on for t in txns), *stated], default=None)
        implied: set[date] = set()
        if first_seen is None:
            return stated, implied
        for on in self._complete.get(pair[0], []):
            if on > first_seen and on not in stated:
                stated[on] = {0}
                implied.add(on)
        return stated, implied

    # ----------------------------------------------------------------- views

    def holdings(self) -> tuple[Holding, ...]:
        """Non-zero timelines, ordered by (account, isin)."""
        out: list[Holding] = []
        for account, isin in self.pairs():
            tl = self.timeline(account, isin)
            if tl.qty.micros:
                cost = _sum_known(lot.cost for lot in tl.lots)
                out.append(Holding(account=account, isin=isin, qty=tl.qty, cost=cost))
        return tuple(out)

    def positions(self) -> tuple[Position, ...]:
        """holdings() grouped by ISIN."""
        by_isin: dict[Isin, list[Holding]] = defaultdict(list)
        for h in self.holdings():
            by_isin[h.isin].append(h)
        return tuple(
            Position(
                isin=isin,
                qty=Qty(micros=sum(h.qty.micros for h in group)),
                cost=_sum_known(h.cost for h in group),
                holdings=tuple(group),
            )
            for isin, group in sorted(by_isin.items())
        )

    def manual_assets(self, today: date) -> tuple[ManualAsset, ...]:
        """Current on `today`: the latest StatedBalance per (asset, label), the
        latest GoldHolding per label, and every FD not yet matured. `today`
        comes from the caller, like every other clock read."""
        latest: dict[tuple[str, str], StatedBalance | GoldHolding] = {}
        fds: list[FixedDeposit] = []
        for f in self._manual:
            if isinstance(f, FixedDeposit):
                if _fd_maturity(f) >= today:
                    fds.append(f)
                continue
            key = (f.asset, f.label) if isinstance(f, StatedBalance) else ("gold", f.label)
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
        return tuple(t for t in self._theses if isin is None or t.isin == isin)

    def resolve(self, text: str) -> Isin:
        """'INFY' | AMFI code | ISIN -> Isin, from instruments this book knows.
        Raises LookupError listing candidates when ambiguous or unknown."""
        needle = text.strip().upper()
        if not needle:
            raise LookupError("give a symbol, AMFI code or ISIN")
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
        raise LookupError(f"{text!r} matches no known instrument; use its ISIN")


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


def _merge(intervals: list[Window]) -> list[Window]:
    merged: list[Window] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered(windows: list[Window], prev: date | None, on: date) -> bool:
    """True when every day in (prev, on] lies inside one merged window. Before
    the first checkpoint (prev is None) the span reaches back forever, so it
    is never covered."""
    if prev is None:
        return False
    first = prev + timedelta(days=1)
    return any(start <= first and on <= end for start, end in windows)


@dataclass
class _OpenLot:
    acquired_on: date
    micros: int
    cost: Money | None


class _Fold:
    """Mutable working state for one fold. Never escapes `Book._fold`."""

    def __init__(self, pair: Pair, windows: list[Window]) -> None:
        self.account, self.isin = pair
        self.windows = windows
        self.checkpoint: date | None = None  # date of the last judged Balance
        self.running = 0
        self.lots: list[_OpenLot] = []
        self.movements: list[Txn | Inferred] = []
        self.checks: list[Check] = []
        self.disposals: list[Disposal] = []

    def apply(self, txn: Txn) -> None:
        match txn.event:
            case Buy(units=units, amount=amount):
                self.movements.append(txn)
                self.running += units.micros
                self.lots.append(_OpenLot(txn.on, units.micros, amount))
            case Bonus(units=units):
                self.movements.append(txn)
                self.running += units.micros
                self.lots.append(_OpenLot(txn.on, units.micros, Money(paise=0)))
            case Split(num=num, den=den):
                self.movements.append(txn)
                self._split(num, den)
            case Sell(units=units, amount=amount):
                self._sell(txn, units.micros, amount)
            case Income():
                self.movements.append(txn)

    def _sell(self, txn: Txn, units: int, amount: Money) -> None:
        short = units - self.running
        if short > 0 and _covered(self.windows, self.checkpoint, txn.on):
            # A covered window lists every txn, so selling more than it
            # explains is an error: record it, then sell only what is held,
            # with its share of the stated proceeds.
            self.check("oversell", txn.on, units, "mismatch")
            self.movements.append(txn)
            held = self.running
            if held:
                share = amount.split(Qty(micros=held), Qty(micros=units))[0]
                self._consume(txn.on, held, share)
            return
        if short > 0:  # uncovered: an unrecorded acquisition explains the gap
            self.infer(txn.on, short)
        self.movements.append(txn)
        self._consume(txn.on, units, amount)

    def infer(self, on: date, gap: int) -> None:
        self.movements.append(
            Inferred(account=self.account, isin=self.isin, on=on, delta=QtyDelta(micros=gap))
        )
        if gap > 0:
            self.running += gap
            self.lots.append(_OpenLot(on, gap, None))
        else:
            self._consume(on, -gap, None)

    def check(
        self,
        kind: Literal["balance", "oversell"],
        on: date,
        stated: int,
        status: Literal["match", "inferred", "mismatch"],
    ) -> None:
        self.checks.append(
            Check(
                kind=kind,
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
        self.lots = [lot for lot in self.lots if lot.micros]  # a lot floored to 0 is gone
        self.running = target

    def _consume(self, on: date, micros: int, proceeds: Money | None) -> None:
        """FIFO. Cost and proceeds are apportioned exactly with Money.split."""
        remaining_units = micros
        remaining_proceeds = proceeds
        while remaining_units and self.lots:
            lot = self.lots[0]
            take = min(lot.micros, remaining_units)
            cost: Money | None = None
            if lot.cost is not None:
                cost, lot.cost = lot.cost.split(Qty(micros=take), Qty(micros=lot.micros))
            share: Money | None = None
            if remaining_proceeds is not None:
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
                del self.lots[0]

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
