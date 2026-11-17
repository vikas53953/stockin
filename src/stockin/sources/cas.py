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
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from casparser import read_cas_pdf  # pyright: ignore[reportUnknownVariableType]
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
_TAX_ON_SELL = {"STT_TAX", "TDS_TAX"}


def read(path: Path, password: str = "") -> Statement:
    """A CAS PDF or casparser JSON file -> Statement. The password is used
    once by casparser and is not stored or logged."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".json":
        body: dict[str, Any] = json.loads(raw)
        data: CASData | NSDLCASData = (
            CASData.model_validate(body) if "folios" in body else NSDLCASData.model_validate(body)
        )
    else:
        try:
            parsed: object = read_cas_pdf(str(path), password)
        except Exception as exc:  # casparser raises several types; the message is safe
            raise CasRejected(f"could not read {path.name}: {type(exc).__name__}") from None
        if not isinstance(parsed, CASData | NSDLCASData):
            raise CasRejected(f"{path.name}: unexpected parser output")
        data = parsed
    return from_casparser(data, sha)


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
    """casparser rows -> Txns. Taxes fold into the same-day buy or sell;
    a REVERSAL cancels the row it reverses; the statement must reconcile
    with itself (open + units == close) or it is rejected."""
    name = scheme.scheme
    rows = [r for r in scheme.transactions if not _cancelled(r, scheme.transactions)]
    events: list[tuple[date, Event]] = []
    taxes: dict[tuple[date, str], Decimal] = defaultdict(Decimal)
    units_seen = Decimal(0)
    for row in rows:
        kind = _tag(row.type)
        on = _date(row.date, name)
        units = row.units or Decimal(0)
        amount = abs(row.amount or Decimal(0))
        if kind in _BUY or kind == "DIVIDEND_REINVEST":
            if units <= 0:
                raise CasRejected(f"{name}: {kind} on {on} has non-positive units")
            if kind == "DIVIDEND_REINVEST":
                events.append((on, Income(amount=_money(amount, name))))
            events.append((on, Buy(units=_qty(units, name), amount=_money(amount, name))))
        elif kind in _SELL:
            if units >= 0:
                raise CasRejected(f"{name}: {kind} on {on} has non-negative units")
            events.append((on, Sell(units=_qty(-units, name), amount=_money(amount, name))))
        elif kind == "DIVIDEND_PAYOUT":
            events.append((on, Income(amount=_money(amount, name))))
        elif kind in _TAX_ON_BUY:
            taxes[(on, "buy")] += amount
        elif kind in _TAX_ON_SELL:
            taxes[(on, "sell")] += amount
        elif units:
            raise CasRejected(f"{name}: unsupported {kind} row on {on} moves units")
        units_seen += units
    if scheme.open + units_seen != scheme.close:
        raise CasRejected(
            f"{name}: opening {scheme.open} + transactions {units_seen} != closing {scheme.close}"
        )
    events = _apply_taxes(events, taxes, name)
    seen: Counter[tuple[date, Event]] = Counter()
    txns: list[Txn] = []
    for on, event in events:
        nth = seen[(on, event)]
        seen[(on, event)] += 1
        txns.append(Txn(account=acct, isin=isin, on=on, event=event, nth=nth))
    return txns


def _cancelled(row: TransactionData, rows: list[TransactionData]) -> bool:
    """True for a REVERSAL and for the one row it reverses (same units and
    amount, opposite sign, on or before it). An unmatched reversal rejects."""
    if _tag(row.type) == "REVERSAL":
        if _reversed_by(row, rows) is None:
            raise CasRejected(f"reversal on {row.date} matches no earlier row")
        return True
    return any(_tag(r.type) == "REVERSAL" and _reversed_by(r, rows) is row for r in rows)


def _reversed_by(rev: TransactionData, rows: list[TransactionData]) -> TransactionData | None:
    for r in rows:
        if r is rev or _tag(r.type) == "REVERSAL":
            continue
        if (r.units or 0) == -(rev.units or 0) and abs(r.amount or 0) == abs(rev.amount or 0):
            return r
    return None


def _apply_taxes(
    events: list[tuple[date, Event]], taxes: dict[tuple[date, str], Decimal], name: str
) -> list[tuple[date, Event]]:
    out = list(events)
    for (on, side), amount in taxes.items():
        for i, (when, event) in enumerate(out):
            if when != on:
                continue
            if side == "buy" and isinstance(event, Buy):
                out[i] = (when, Buy(units=event.units, amount=event.amount + _money(amount, name)))
                break
            if side == "sell" and isinstance(event, Sell):
                out[i] = (when, Sell(units=event.units, amount=event.amount - _money(amount, name)))
                break
        else:
            raise CasRejected(f"{name}: {side} tax on {on} has no {side} that day")
    return out


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
    try:
        return Money.rupees(format(value.quantize(Decimal("0.01")), "f"))
    except ValueError:
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
