"""The only things the database stores.

A Fact is something a statement or the owner asserted. Facts are immutable
and content-addressed: `fact_id(f)` is sha256 over the canonical JSON of the
fact. Two statements that both contain the 5-Oct SIP produce the same id, so
overlapping monthly CAS files converge instead of double-counting.

There is deliberately no "holding" fact and no "opening balance" transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Annotated, Literal, NewType

from pydantic import Field, StrictInt, model_validator

from .money import Bps, Money, Price, Qty, Value

Isin = NewType("Isin", str)
AccountId = NewType("AccountId", str)
FactId = NewType("FactId", str)


_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def parse_isin(text: str) -> Isin:
    """The only constructor of Isin. 12 chars, [A-Z]{2}[A-Z0-9]{9}[0-9], Luhn
    check digit. Importers call it; everything inside trusts the brand."""
    candidate = text.strip().upper()
    if not _ISIN.match(candidate):
        raise ValueError(f"not an ISIN: {text!r}")
    digits = "".join(str(int(ch, 36)) for ch in candidate)
    total = 0
    for position, ch in enumerate(reversed(digits)):
        d = int(ch)
        if position % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    if total % 10 != 0:
        raise ValueError(f"ISIN check digit is wrong: {text!r}")
    return Isin(candidate)


def account_id(kind: Literal["demat", "mf_folio"], *natural_key: str) -> AccountId:
    """sha256(kind, natural key)[:16]. Natural key is (DP id, client id) for
    demat and (AMC, folio) for MF. The provider of the statement is NOT part
    of it, so one folio seen in two statements is one account. The natural
    key is never stored; only this id is."""
    if not natural_key or any(not part.strip() for part in natural_key):
        raise ValueError("account natural key must be non-empty")
    material = "\x1f".join((kind, *(part.strip().upper() for part in natural_key)))
    return AccountId(hashlib.sha256(material.encode()).hexdigest()[:16])


# ---------------------------------------------------------------- reference

AssetClass = Literal["equity", "debt", "gold", "cash", "retirement"]


class Instrument(Value):
    """Keyed by ISIN (hard rule). `symbol` and `amfi_code` are lookup aliases
    resolved at the CLI boundary (`thesis add INFY`); nothing inside the
    domain is keyed by them."""

    isin: Isin
    name: str
    kind: Literal["stock", "mutual_fund", "etf", "bond", "sgb"]
    asset_class: AssetClass
    symbol: str | None = None
    amfi_code: str | None = None


class Account(Value):
    """One place units are held: a demat account at a broker, or an MF folio.

    `id` = sha256(kind, natural_key)[:16], where natural_key is (DP id, client
    id) for demat and (AMC, folio) for MF. The statement provider is NOT in
    the key, so a folio seen in both a CAMS CAS and an NSDL eCAS is one
    account, not two. The natural key itself is never stored: only the id
    and `masked` reach the database, so no output surface can leak an
    account number. (Graft from candidate A; fixes a cross-source double
    count the cross-judge found in all three candidates.)"""

    id: AccountId
    kind: Literal["demat", "mf_folio"]
    institution: str  # broker / AMC name
    masked: str  # "Zerodha ...4321"


# ------------------------------------------------------------ ledger facts


class Buy(Value):
    kind: Literal["buy"] = "buy"
    units: Qty
    amount: Money  # total paid, as the statement states it


class Sell(Value):
    kind: Literal["sell"] = "sell"
    units: Qty
    amount: Money  # net proceeds, as stated


class Split(Value):
    """Every open lot's qty is multiplied by num/den; cost is unchanged."""

    kind: Literal["split"] = "split"
    num: StrictInt = Field(gt=0)
    den: StrictInt = Field(gt=0)


class Bonus(Value):
    kind: Literal["bonus"] = "bonus"
    units: Qty  # new zero-cost lot, acquired on the txn date


class Income(Value):
    """Dividend/interest paid out. No unit effect; feeds XIRR only.
    A dividend reinvestment is imported as Income + Buy on the same day."""

    kind: Literal["income"] = "income"
    amount: Money


Event = Annotated[Buy | Sell | Split | Bonus | Income, Field(discriminator="kind")]


class Txn(Value):
    """One movement in one account. `nth` is the 0-based ordinal among txns
    in the same statement that are otherwise identical (two equal SIPs on one
    day). Statements list whole days, so `nth` is stable across overlapping
    statements and the content id stays unique AND convergent."""

    fact: Literal["txn"] = "txn"
    account: AccountId
    isin: Isin
    on: date
    event: Event
    nth: StrictInt = Field(ge=0, default=0)


class Balance(Value):
    """ "At close of `on`, this account held `qty` of `isin`." The master
    record. A demat eCAS is nothing but these."""

    fact: Literal["balance"] = "balance"
    account: AccountId
    isin: Isin
    on: date
    qty: Qty


class Coverage(Value):
    """ "I list EVERY transaction for (account, isin) in [start, end]."
    This one fact replaces any per-account 'has transactions' flag. Inside a
    covered window an unexplained unit change is a Mismatch (hard error).
    Outside one it is an Inferred movement (cost unknown, expected for
    demat). An MF CAS emits Balance(open) + Coverage + txns + Balance(close);
    a demat eCAS emits Balance only; a future tradebook importer emits
    Coverage + txns and reconcile tightens with no code change."""

    fact: Literal["coverage"] = "coverage"
    account: AccountId
    isin: Isin
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Coverage:
        if self.end < self.start:
            raise ValueError("coverage window ends before it starts")
        return self


class Complete(Value):
    """ "The Balances I carry for `account` on `on` are ALL its holdings."
    An ISIN with an earlier Balance and none on this date is therefore zero.
    This is how a demat position that was sold out, and so simply vanishes
    from the next eCAS, closes without the importer reading prior state:
    `Book` derives the implied Balance(qty=0) on read. Import stays a pure
    function of the file. (Graft from candidate A's complete Snapshot.)"""

    fact: Literal["complete"] = "complete"
    account: AccountId
    on: date


# ------------------------------------------------------------- owner facts


class StatedBalance(Value):
    """EPF / PPF / NPS: the owner reads a rupee balance off a passbook."""

    fact: Literal["stated_balance"] = "stated_balance"
    asset: Literal["epf", "ppf", "nps"]
    label: str = ""
    balance: Money
    as_of: date


class FixedDeposit(Value):
    """A contract, valued by formula, not by restating a balance."""

    fact: Literal["fd"] = "fd"
    label: str  # required: two identical FDs opened the same
    # day are told apart only by label
    principal: Money
    rate: Bps  # 7.1% -> 710
    start: date
    months: StrictInt = Field(gt=0)


class GoldHolding(Value):
    """ "As of `as_of` I hold `grams` in total." A balance, not an increment:
    re-running `asset add gold --grams 20` is a no-op, not 40 g."""

    fact: Literal["gold"] = "gold"
    label: str = ""
    grams: Qty
    as_of: date


class Thesis(Value):
    """One journal entry. Append-only: a revision is a new Thesis with a
    later `recorded_on`; the latest per ISIN is current, the rest is the
    decision journal. Keyed by ISIN, so it spans every account holding it.
    At least one of the four rule fields must be set (validator)."""

    fact: Literal["thesis"] = "thesis"
    isin: Isin
    why: str
    invalidate_below: Price | None = None
    target: Price | None = None
    max_weight: Bps | None = None  # --max-weight 8 -> 800
    review_on: date | None = None
    recorded_on: date  # supplied by the shell, never a clock

    @model_validator(mode="after")
    def _has_a_rule(self) -> Thesis:
        rules = (self.invalidate_below, self.target, self.max_weight, self.review_on)
        if all(rule is None for rule in rules):
            raise ValueError(
                "a thesis needs at least one rule: invalidate_below, "
                "target, max_weight or review_on"
            )
        return self


StatementFact = Annotated[Txn | Balance | Coverage | Complete, Field(discriminator="fact")]
OwnerFact = Annotated[
    StatedBalance | FixedDeposit | GoldHolding | Thesis, Field(discriminator="fact")
]
Fact = Annotated[StatementFact | OwnerFact, Field(discriminator="fact")]


def fact_id(fact: Fact) -> FactId:
    """sha256 of the fact's canonical JSON (sorted keys, no whitespace). Pure.
    The ONLY identity a fact has; there are no autoincrement ids anywhere."""
    body = json.dumps(fact.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return FactId(hashlib.sha256(body.encode()).hexdigest())


class Statement(Value):
    """What every importer returns and the only thing LedgerWriter ingests.
    Carries domain types only: no casparser object crosses this line.
    `sha256` is of the original file bytes and is provenance, not identity."""

    sha256: str
    source: Literal["cams_kfin_cas", "nsdl_cas", "cdsl_cas"]
    period_start: date
    period_end: date
    instruments: tuple[Instrument, ...]
    accounts: tuple[Account, ...]
    facts: tuple[StatementFact, ...]
    warnings: tuple[str, ...] = ()
