# pyright: reportMissingTypeStubs=false
"""CAS boundary. casparser types stop here; only `Statement` leaves.

Two inputs, one mapping:
  read(path, password)   a CAS PDF (via casparser) or the JSON that
                         `casparser -o json` writes for one
  from_casparser(data)   casparser's typed output -> Statement

Tests and the verify skill feed synthetic casparser JSON, so the public repo
never needs a real PDF. Every check here fails closed: a statement we are
unsure of is rejected whole, never half imported.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from casparser import read_cas_pdf  # pyright: ignore[reportUnknownVariableType]
from casparser.exceptions import IncorrectPasswordError, ParserException
from casparser.types import CASData, DematAccount, NSDLCASData, Scheme, TransactionData

from ..facts import (
    Account,
    AccountId,
    AssetClass,
    Balance,
    Buy,
    Complete,
    Coverage,
    Event,
    Income,
    Instrument,
    Isin,
    Sell,
    Statement,
    StatementFact,
    Txn,
    account_id,
    parse_isin,
)
from ..money import Money, Qty


class CasRejected(ValueError):
    """Unparseable, unsupported or internally inconsistent. The message names
    the scheme or ISIN and the reason; it never includes the password."""


_BUY = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
_SELL = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER"}
_TAX_ON_BUY = {"STAMP_DUTY_TAX"}
_TAX_ON_SELL = {"STT_TAX"}
_TAX_ON_PAYOUT = {"TDS_TAX"}  # on a redemption, or on an IDCW payout or reinvestment
_PAISA = Decimal("0.01")


def read(path: Path, password: str = "") -> Statement:
    """A CAS PDF or casparser JSON file -> Statement. The password is used
    once by casparser and is not stored or logged."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".json":
        data = _from_json(raw)
    else:
        try:
            parsed: object = read_cas_pdf(str(path), password)
        except Exception as exc:  # casparser raises several types; never echo the password
            raise CasRejected(_pdf_reason(path.name, exc)) from None
        if not isinstance(parsed, CASData | NSDLCASData):
            raise CasRejected("unexpected parser output")
        data = parsed
    try:
        return from_casparser(data, sha)
    except CasRejected:
        raise
    except (ValueError, ArithmeticError) as exc:  # a value our domain types refuse
        raise CasRejected(_why(exc)) from None


def _from_json(raw: bytes) -> CASData | NSDLCASData:
    try:
        body: object = json.loads(raw)
    except ValueError as exc:
        raise CasRejected(f"not JSON ({_why(exc)})") from None
    if not isinstance(body, dict):
        raise CasRejected("not a casparser statement")
    try:
        if "folios" in body:
            return CASData.model_validate(body)
        return NSDLCASData.model_validate(body)
    except (ValueError, ArithmeticError) as exc:  # pydantic's ValidationError is a ValueError
        raise CasRejected(f"not a valid casparser statement ({_why(exc)})") from None
    except (AttributeError, TypeError):  # casparser's own validators on a null section
        raise CasRejected("not a valid casparser statement (a section is empty)") from None


def _pdf_reason(filename: str, exc: BaseException) -> str:
    """Plain words for the PDF failures people actually hit. Never includes
    the password: casparser's own messages are not passed through."""
    if isinstance(exc, IncorrectPasswordError):
        return f"wrong password for {filename} (it is usually your PAN in capitals)"
    if isinstance(exc, ParserException):
        return (
            f"{filename} does not look like a CAS from CAMS, KFintech, NSDL or CDSL "
            "(download the detailed CAS PDF, not a portfolio summary or screenshot)"
        )
    return f"could not read {filename} ({type(exc).__name__})"


def _why(exc: BaseException) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {first}"[:200]


def from_casparser(data: CASData | NSDLCASData, sha256: str) -> Statement:
    start, end = _period(data.statement_period.from_, data.statement_period.to)
    if isinstance(data, CASData):
        return _from_rta(data, sha256, start, end)
    return _from_depository(data, sha256, start, end)


# ------------------------------------------------------------ CAMS / KFintech


def _from_rta(data: CASData, sha: str, start: date, end: date) -> Statement:
    detailed = _tag(data.cas_type) == "DETAILED"
    instruments: dict[Isin, Instrument] = {}
    accounts: dict[AccountId, Account] = {}
    facts: list[StatementFact] = []
    for folio in data.folios:
        if not folio.folio.strip() or not folio.amc.strip():
            raise CasRejected("a folio in this statement has no folio number or AMC")
        acct = account_id("mf_folio", folio.amc, folio.folio)
        accounts[acct] = Account(
            id=acct, kind="mf_folio", institution=folio.amc, masked=_mask(folio.amc, folio.folio)
        )
        for scheme in folio.schemes:
            isin = _isin(scheme.isin, scheme.scheme)
            instruments[isin] = Instrument(
                isin=isin,
                name=scheme.scheme,
                kind="mutual_fund",
                asset_class=_mf_class(scheme.type, scheme.scheme),
                amfi_code=scheme.amfi or None,
            )
            close = _qty(scheme.close, scheme.scheme)
            if not detailed:
                facts.append(Balance(account=acct, isin=isin, on=end, qty=close))
                continue
            facts.append(
                Balance(
                    account=acct,
                    isin=isin,
                    on=start - timedelta(days=1),
                    qty=_qty(scheme.open, scheme.scheme),
                )
            )
            facts.append(Coverage(account=acct, isin=isin, start=start, end=end))
            facts.extend(_scheme_txns(acct, isin, scheme))
            facts.append(Balance(account=acct, isin=isin, on=end, qty=close))
    return Statement(
        sha256=sha,
        source="cams_kfin_cas",
        period_start=start,
        period_end=end,
        instruments=tuple(instruments.values()),
        accounts=tuple(accounts.values()),
        facts=tuple(facts),
        warnings=tuple(data.parse_warnings),
    )


def _scheme_txns(acct: AccountId, isin: Isin, scheme: Scheme) -> list[Txn]:
    """casparser rows -> Txns, in statement order.

    - A REVERSAL cancels the latest earlier row (on or before its date) with
      the opposite units and the same amount; each row is cancelled at most
      once. A reversal with no such row in this statement rejects it.
    - Stamp duty adds to the cost of the buy it follows on the same day;
      STT comes off the proceeds of the sell it follows; TDS comes off the
      sell or IDCW payout it follows. A reinvested IDCW is already net of
      TDS, so a TDS row after it changes nothing.
    - The statement must reconcile with itself (open + units == close).
    - Rows that move no units and carry no money we use (MISC: nominee or
      address notes) are skipped; any other type that moves units rejects."""
    name = scheme.scheme
    rows = _uncancelled(scheme.transactions, name)
    events: list[tuple[date, Event]] = []
    reinvested: set[int] = set()  # indexes of Income events that are already net of TDS
    units_seen = Decimal(0)
    for row in rows:
        kind = _tag(row.type)
        on = _date(row.date, name)
        units = row.units or Decimal(0)
        amount = row.amount or Decimal(0)
        if kind in _BUY or kind == "DIVIDEND_REINVEST":
            if units <= 0 or amount <= 0:
                raise CasRejected(f"{name}: {kind} on {on} needs positive units and amount")
            if kind == "DIVIDEND_REINVEST":
                reinvested.add(len(events))
                events.append((on, Income(amount=_money(amount, name))))
            events.append((on, Buy(units=_qty(units, name), amount=_money(amount, name))))
        elif kind in _SELL:
            if units >= 0 or row.amount is None:
                raise CasRejected(f"{name}: {kind} on {on} needs negative units and an amount")
            events.append((on, Sell(units=_qty(-units, name), amount=_money(abs(amount), name))))
        elif kind == "DIVIDEND_PAYOUT":
            events.append((on, Income(amount=_money(abs(amount), name))))
        elif kind in _TAX_ON_BUY:
            _charge(events, on, (Buy,), abs(amount), kind, name)
        elif kind in _TAX_ON_SELL:
            _charge(events, on, (Sell,), abs(amount), kind, name)
        elif kind in _TAX_ON_PAYOUT:
            _charge(events, on, (Sell, Income), abs(amount), kind, name, already_net=reinvested)
        elif units:
            raise CasRejected(f"{name}: unsupported {kind} row on {on} moves units")
        units_seen += units
    if scheme.open + units_seen != scheme.close:
        raise CasRejected(
            f"{name}: opening {scheme.open} + transactions {units_seen} != closing {scheme.close}"
        )
    seen: Counter[tuple[date, Event]] = Counter()
    txns: list[Txn] = []
    for on, event in events:
        nth = seen[(on, event)]
        seen[(on, event)] += 1
        txns.append(Txn(account=acct, isin=isin, on=on, event=event, nth=nth))
    return txns


def _uncancelled(rows: list[TransactionData], name: str) -> list[TransactionData]:
    cancelled: set[int] = set()
    for i, rev in enumerate(rows):
        if _tag(rev.type) != "REVERSAL":
            continue
        on = _date(rev.date, name)
        match = next(
            (
                j
                for j in range(i - 1, -1, -1)
                if j not in cancelled
                and _tag(rows[j].type) != "REVERSAL"
                and _date(rows[j].date, name) <= on
                and (rows[j].units or 0) == -(rev.units or 0)
                and abs(rows[j].amount or 0) == abs(rev.amount or 0)
            ),
            None,
        )
        if match is None:
            raise CasRejected(
                f"{name}: reversal on {on} has no matching earlier row in this statement; "
                "import a statement whose period covers both"
            )
        cancelled.update((i, match))
    return [r for i, r in enumerate(rows) if i not in cancelled]


def _charge(
    events: list[tuple[date, Event]],
    on: date,
    onto: tuple[type[Buy] | type[Sell] | type[Income], ...],
    tax: Decimal,
    kind: str,
    name: str,
    already_net: set[int] | None = None,
) -> None:
    """Apply a tax row to the latest same-day event of a type in `onto`
    before it in the statement. Stamp duty adds to a buy's cost. STT comes
    off a sell's proceeds; TDS comes off a sell's proceeds or an IDCW
    payout. An Income in `already_net` (a reinvestment, whose amount is what
    was left after TDS) is the row the tax belongs to but is not reduced."""
    charge = _money(tax, name)
    for i in range(len(events) - 1, -1, -1):
        when, event = events[i]
        if when != on:
            break
        if not isinstance(event, onto):
            continue
        if isinstance(event, Buy):
            events[i] = (when, Buy(units=event.units, amount=event.amount + charge))
            return
        if already_net and i in already_net:
            return
        net = event.amount - charge
        if net.paise < 0:
            raise CasRejected(f"{name}: {kind} on {on} is larger than the amount it is on")
        events[i] = (
            when,
            Sell(units=event.units, amount=net) if isinstance(event, Sell) else Income(amount=net),
        )
        return
    what = " or ".join(
        {Buy: "purchase", Sell: "redemption", Income: "IDCW payout"}[t] for t in onto
    )
    raise CasRejected(f"{name}: {kind} on {on} follows no {what} that day")


# ---------------------------------------------------------------- NSDL / CDSL


def _from_depository(data: NSDLCASData, sha: str, start: date, end: date) -> Statement:
    source: Literal["nsdl_cas", "cdsl_cas"] = (
        "cdsl_cas" if _tag(data.file_type) == "CDSL" else "nsdl_cas"
    )
    instruments: dict[Isin, Instrument] = {}
    accounts: dict[AccountId, Account] = {}
    facts: list[StatementFact] = []
    skipped: list[str] = []
    for demat in data.accounts:
        if demat.type == "Mutual Fund Folios":
            skipped.append("MF folios in this statement come from your CAMS/KFintech CAS")
            continue
        acct = _demat_account(demat)
        accounts[acct.id] = acct
        held: dict[Isin, Decimal] = defaultdict(Decimal)
        for eq in demat.equities:
            isin = _isin(eq.isin, eq.name or eq.isin)
            instruments[isin] = Instrument(
                isin=isin,
                name=eq.name or isin,
                kind="stock",
                asset_class="equity",
                symbol=eq.symbol or None,
            )
            held[isin] += eq.num_shares
        for mf in demat.mutual_funds:
            isin = _isin(mf.isin, mf.name or mf.isin)
            instruments[isin] = Instrument(
                isin=isin,
                name=mf.name or isin,
                kind="mutual_fund",
                asset_class=_mf_class(mf.type, mf.name or ""),
                amfi_code=mf.amfi or None,
            )
            held[isin] += mf.balance
        for bond in demat.bonds:
            isin = _isin(bond.isin, bond.name or bond.isin)
            instruments[isin] = Instrument(
                isin=isin, name=bond.name or isin, kind="bond", asset_class="debt"
            )
            held[isin] += bond.num_bonds
        for isin, qty in held.items():
            facts.append(Balance(account=acct.id, isin=isin, on=end, qty=_qty(qty, isin)))
        facts.append(Complete(account=acct.id, on=end))
    if data.nps is not None:
        skipped.append("NPS holdings are not imported until Sprint 2")
    return Statement(
        sha256=sha,
        source=source,
        period_start=start,
        period_end=end,
        instruments=tuple(instruments.values()),
        accounts=tuple(accounts.values()),
        facts=tuple(facts),
        warnings=(*data.parse_warnings, *sorted(set(skipped))),
    )


def _demat_account(demat: DematAccount) -> Account:
    dp, client = (demat.dp_id or "").strip(), (demat.client_id or "").strip()
    if not dp and not client:
        raise CasRejected(f"demat account {demat.name!r} has no DP or client id")
    acct = account_id("demat", dp, client or "-")
    return Account(id=acct, kind="demat", institution=demat.name, masked=_mask(demat.name, client))


# ------------------------------------------------------------------- parsing


def _tag(value: object) -> str:
    """casparser enums arrive as enum members or, after model_validate with
    use_enum_values, as plain strings; str() of a member is 'Type.NAME'."""
    return str(getattr(value, "value", value))


def _period(start: str, end: str) -> tuple[date, date]:
    if not start or not end:
        raise CasRejected("statement period not found")
    s, e = _parse_date(start), _parse_date(end)
    if e < s:
        raise CasRejected(f"statement period ends before it starts: {start} to {end}")
    return s, e


def _parse_date(text: str) -> date:
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%b/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise CasRejected(f"unrecognised date {text!r}")


def _date(value: date | str, name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return _parse_date(value)
    except CasRejected:
        raise CasRejected(f"{name}: unrecognised date {value!r}") from None


def _isin(value: str | None, name: str) -> Isin:
    if not value:
        raise CasRejected(f"{name}: no ISIN in the statement")
    try:
        return parse_isin(value)
    except ValueError:
        raise CasRejected(f"{name}: invalid ISIN {value!r}") from None


def _qty(value: Decimal, name: str) -> Qty:
    if value < 0:
        raise CasRejected(f"{name}: negative quantity {value}")
    try:
        return Qty.parse(format(value, "f"))
    except ValueError:
        raise CasRejected(f"{name}: quantity {value} has more than 6 decimals") from None


def _money(value: Decimal, name: str) -> Money:
    """Exact rupees and paise only: a statement amount with more than two
    decimals, or not a finite number, is not something we can trust."""
    try:
        exact = value.is_finite() and value == value.quantize(_PAISA, rounding=ROUND_DOWN)
    except InvalidOperation:
        exact = False
    if not exact:
        raise CasRejected(f"{name}: amount {value} is not in whole paise")
    try:
        return Money.rupees(format(value.quantize(_PAISA), "f"))  # "1000.100" -> "1000.10"
    except (ValueError, ArithmeticError):
        raise CasRejected(f"{name}: bad amount {value}") from None


def _mf_class(fund_type: str | None, name: str) -> AssetClass:
    lowered = name.lower()
    if "gold" in lowered:
        return "gold"
    if (fund_type or "").upper() == "DEBT" or any(
        w in lowered for w in ("liquid", "overnight", "gilt", "debt", "money market")
    ):
        return "debt"
    return "equity"


def _mask(institution: str, key: str) -> str:
    tail = key.strip()[-4:] if key.strip() else "????"
    return f"{institution} ...{tail}"
