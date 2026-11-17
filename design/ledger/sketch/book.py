"""Book: the whole ledger in memory, immutable, and every unit-level
derivation. One investor's lifetime is ~1e4 facts; folding all of it is
milliseconds, so there is no incremental state to keep in sync.

The single fold is `Book.timeline(account, isin)`. Holdings, lots, P&L and
reconcile all read the SAME Timeline, so they cannot disagree.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Mapping

from .facts import (
    Account, AccountId, Balance, Coverage, Fact, FixedDeposit, GoldHolding,
    Instrument, Isin, StatedBalance, Thesis, Txn,
)
from .money import Bps, Money, Qty, QtyDelta, Value


class Inferred(Value):
    """A unit change no statement itemised: the gap between what recorded
    txns explain and what a Balance asserts, in an uncovered window. Derived
    on every read, never stored, so importing the missing trades later simply
    makes it vanish. Dated at the asserting Balance (latest possible date)."""

    account: AccountId
    isin: Isin
    on: date
    delta: QtyDelta


class Check(Value):
    """One Balance assertion, judged."""

    account: AccountId
    isin: Isin
    on: date
    stated: Qty
    computed: Qty              # from recorded txns only, before inference
    status: Literal["match", "inferred", "mismatch"]


class Lot(Value):
    """Open FIFO lot. `cost is None` means genuinely unknown (acquired via an
    Inferred movement), which is different from zero (a Bonus)."""

    acquired_on: date
    qty: Qty
    cost: Money | None


class Disposal(Value):
    acquired_on: date
    sold_on: date
    qty: Qty
    cost: Money | None
    proceeds: Money | None     # None when the disposal itself was Inferred

    @property
    def gain(self) -> Money | None:
        raise NotImplementedError


class Timeline(Value):
    """Everything known about one (account, isin), in date order."""

    account: AccountId
    isin: Isin
    movements: tuple[Txn | Inferred, ...]
    checks: tuple[Check, ...]
    qty: Qty                   # current units. Equals the last Balance when every
                               # check is match/inferred and no txn follows it;
                               # after a mismatch it is what the txns explain
    lots: tuple[Lot, ...]      # sum(lot.qty) == qty, always
    disposals: tuple[Disposal, ...]


class Holding(Value):
    """`stockin import cas` prints these, grouped by account."""

    account: AccountId
    isin: Isin
    qty: Qty
    cost: Money | None         # None if any open lot has unknown cost


class Position(Value):
    """One ISIN across all accounts. The unit a Thesis and a Verdict attach to."""

    isin: Isin
    qty: Qty
    cost: Money | None
    holdings: tuple[Holding, ...]


class Book(Value):
    """Immutable snapshot of every fact with date <= as_of. Constructed only
    by `Ledger.book()` and by tests via `Book.of(...)`."""

    as_of: date | None
    instruments: Mapping[Isin, Instrument]
    accounts: Mapping[AccountId, Account]
    facts: tuple[Fact, ...]

    @classmethod
    def of(cls, facts: tuple[Fact, ...], instruments: tuple[Instrument, ...] = (),
           accounts: tuple[Account, ...] = (), as_of: date | None = None) -> Book:
        raise NotImplementedError

    def timeline(self, account: AccountId, isin: Isin) -> Timeline:
        """THE fold. Cached per Book instance (Book is immutable).

        TODO pseudocode:
          txns     = this pair's Txn facts sorted by (on, intra-day order, nth)
                     intra-day order: split, bonus, buy, sell, income
          balances = Balance facts sorted by on (dedupe equal; two different
                     qty on one day -> both become status=mismatch)
                     PLUS an implied Balance(qty=0) on every Complete(account, D)
                     date where this isin had an earlier Balance and none on D
          windows  = merged Coverage intervals
          running = 0; lots = deque()
          for each checkpoint B in balances (then a final open-ended segment):
              apply every txn with prev.on < txn.on <= B.on:
                  buy    -> running += units; lots.append(Lot(on, units, amount))
                  bonus  -> running += units; lots.append(Lot(on, units, Money(0)))
                  split  -> running, each lot.qty *= num/den (floor to micro;
                            remainder micro-units go to the oldest lot so
                            sum(lots) == running stays exact)
                  sell   -> if units > running: emit mismatch at B, stop pair
                            consume lots FIFO; partial lot uses
                            lot.cost.split(part, whole) so cost is conserved;
                            proceeds apportioned across consumed lots the same way
                  income -> no unit effect
              gap = B.qty - running
              if gap == 0:                         Check(match)
              elif (prev.on, B.on] subset of windows: Check(mismatch); do NOT
                                                   adjust running: the error
                                                   stays visible downstream
              else:                                Check(inferred)
                  emit Inferred(on=B.on, delta=gap)
                  gap > 0 -> lots.append(Lot(B.on, gap, cost=None))
                  gap < 0 -> consume FIFO, Disposal(proceeds=None)
                  running = B.qty
          txns after the last Balance are applied and trusted (nothing to check yet).
        """
        raise NotImplementedError

    def holdings(self) -> tuple[Holding, ...]:
        """Non-zero timelines, ordered by (account, isin)."""
        raise NotImplementedError

    def positions(self) -> tuple[Position, ...]:
        """holdings() grouped by ISIN."""
        raise NotImplementedError

    def manual_assets(self) -> tuple[StatedBalance | FixedDeposit | GoldHolding, ...]:
        """Current ones only: latest as_of per (asset, label); every FD whose
        term has not ended before as_of."""
        raise NotImplementedError

    def thesis(self, isin: Isin) -> Thesis | None:
        """Latest by recorded_on (ties: fact id). History via `journal`."""
        raise NotImplementedError

    def journal(self, isin: Isin | None = None) -> tuple[Thesis, ...]:
        raise NotImplementedError

    def resolve(self, text: str) -> Isin:
        """'INFY' | AMFI code | ISIN -> Isin. Raises LookupError listing
        candidates when ambiguous or unknown. The CLI's only symbol lookup."""
        raise NotImplementedError


# ---------------------------------------------------------------- reconcile

class ReconcileReport(Value):
    checks: tuple[Check, ...]

    @property
    def mismatches(self) -> tuple[Check, ...]:
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        """No mismatch. `inferred` is not a failure: it is the honest label
        for 'the depository told us the balance, nobody told us the trades'."""
        raise NotImplementedError


def reconcile(book: Book) -> ReconcileReport:
    """Collect `timeline(...).checks` over every (account, isin) that has a
    Balance. No logic of its own: reconcile cannot drift from holdings because
    it is a view of the same fold."""
    raise NotImplementedError


# ------------------------------------------------------------------ returns

class Returns(Value):
    invested: Money | None
    realised: Money | None
    unrealised: Money | None
    xirr: Bps | None           # None when any cash flow in scope is unknown


def returns(book: Book, value_now: Mapping[Isin, Money], today: date,
            isin: Isin | None = None) -> Returns:
    """Per ISIN or overall. Unknown cost propagates as None, never as zero:
    a made-up cost basis is worse than no P&L. XIRR solves on integer-paise
    flows; the rate is the only non-integer and leaves as Bps."""
    raise NotImplementedError
