"""`stockin review`: one verdict per position, with its reasons.

One ordered rule table produces Findings. The Verdict is the first firing
rule. The CLI table, the dashboard, the MCP `position_review` tool and the
Telegram alerts all consume the same `Review`; none re-implements a rule.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Literal

from .book import Book, Position
from .facts import FactId, Isin, Thesis
from .money import Bps, Money, Value
from .prices import PriceBook, Quote

Verdict = Literal["exit-check", "review-due", "trim", "add-eligible", "hold"]
"""Declared in precedence order. README's five, nothing else."""

FindingKind = Literal[
    "below-invalidation",   # -> exit-check
    "review-overdue",       # -> review-due
    "over-max-weight",      # -> trim
    "past-target",          # -> trim
    "headroom",             # -> add-eligible
    "no-thesis",            # -> review-due
    "no-price",             # -> review-due (price rules skipped)
]
"""A missing thesis or quote never reads as a silent `hold`: you cannot be
"inside every rule" when the rules or the price are missing. (Graft from
candidates A and C.) Each kind's verdict is declared once, in KIND_VERDICT;
the order of RULES is checked against Verdict's order by a test, so the
two lists cannot drift."""

ADD_HEADROOM: Bps = Bps(100)
"""add-eligible needs at least 1 percentage point of room under max weight.
Without it the README's rule covers every `hold`. OPEN QUESTION for owner."""


class Finding(Value):
    kind: FindingKind
    verdict: Verdict
    reason: str             # "price 1,312.00 < invalidation 1,350.00"


class Facts(Value):
    """Everything a rule may look at. Rules see nothing else."""

    position: Position
    thesis: Thesis | None
    quote: Quote | None
    value: Money | None
    weight: Bps | None      # value / total value of all priced POSITIONS
                            # (market holdings, not EPF/PPF/FD/gold). OPEN
                            # QUESTION for owner: or of total net worth?
    today: date


Rule = Callable[[Facts], Finding | None]

RULES: tuple[Rule, ...] = ()
"""TODO, in this order (order IS precedence; tested case by case):
  1 below-invalidation  thesis.invalidate_below and quote and price <  level
  2 review-overdue      thesis.review_on and today > review_on
  3 over-max-weight     thesis.max_weight and weight is not None and weight > max
  4 past-target         thesis.target and quote and price >= target
  5 no-thesis           thesis is None
  6 no-price            quote is None
  7 headroom            thesis.max_weight AND thesis.invalidate_below both set,
                        quote present, price > invalidation,
                        weight <= max - ADD_HEADROOM,
                        and (no target or price < target)
Why review-due outranks trim: an expired thesis means its levels are stale,
so 'trim' computed from them is not yet trustworthy. Why exit-check outranks
everything: it is the one rule that protects capital and it only needs a price.
Verdict = first non-None finding's verdict, else "hold" ("inside every rule").
"""


class PositionReview(Value):
    isin: Isin
    name: str
    verdict: Verdict
    findings: tuple[Finding, ...]   # ALL that fired, precedence order; [0] is the reason shown
    facts: Facts


class Review(Value):
    today: date
    rows: tuple[PositionReview, ...]


def review(book: Book, prices: PriceBook, today: date) -> Review:
    """Pure. Same inputs, same Review, byte for byte in JSON."""
    raise NotImplementedError


# ------------------------------------------------------------------- alerts

class AlertKey(Value):
    """What makes an alert 'the same alert'. Includes the thesis id, so
    revising a thesis re-arms its alerts and nothing else does."""

    isin: Isin
    kind: FindingKind
    thesis: FactId | None


class Alert(Value):
    key: AlertKey
    text: str


def due_alerts(review: Review, already_sent: frozenset[AlertKey]) -> tuple[Alert, ...]:
    """Findings of kinds below-invalidation, review-overdue, over-max-weight,
    past-target (ALERTING, a frozenset next to FindingKind) whose key is not in `already_sent`. The alerts
    job owns `already_sent` in its own file (alerts.db): send, then record
    the key. A crash between the two repeats at most one message."""
    raise NotImplementedError
