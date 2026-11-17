"""Prices are public, reproducible data, so they are NOT ledger facts. They
live in their own SQLite file (prices.db) written only by the nightly price
job; ledger.db is written only by the owner's CLI. Two writers, two files,
no shared rows. Pure code sees prices only as a PriceBook argument.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Mapping, Protocol

from .facts import Isin
from .money import Price, Value

GOLD: Literal["GOLD_GRAM_24K"] = "GOLD_GRAM_24K"
PriceKey = Isin | Literal["GOLD_GRAM_24K"]


class Quote(Value):
    price: Price
    at: datetime                                   # IST, tz-aware
    source: Literal["amfi", "bhavcopy", "live", "manual"]


class PriceBook(Value):
    """Best known quote per key at one moment. Missing key = unpriced; callers
    get None, never a zero. Staleness travels with every value via Quote.at."""

    quotes: Mapping[PriceKey, Quote]

    def get(self, key: PriceKey) -> Quote | None:
        raise NotImplementedError

    def overlay(self, newer: PriceBook) -> PriceBook:
        """Per key, the quote with the later `at`. EOD.overlay(live)."""
        raise NotImplementedError


class QuoteSource(Protocol):
    """Port for the live broker adapter (Upstox/Angel/Kite). One batch call.
    Read-only by shape: there is no method here an order could go through."""

    def quotes(self, isins: frozenset[Isin]) -> PriceBook: ...


class PriceStore:
    """prices.db. `eod()` for readers; `put()` only for the price job."""

    @classmethod
    def open(cls, path: str, *, write: bool = False) -> PriceStore:
        raise NotImplementedError

    def eod(self, keys: frozenset[PriceKey], on_or_before: datetime | None = None) -> PriceBook:
        raise NotImplementedError

    def put(self, book: PriceBook) -> int:
        """Upsert keyed (key, date, source): re-running a bhavcopy is a no-op."""
        raise NotImplementedError
