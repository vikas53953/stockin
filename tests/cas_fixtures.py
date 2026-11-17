"""Synthetic CAS statements, built as casparser's own typed objects and written
the way `casparser -o json` writes them. Every name, folio, DP id and number
here is invented; the repository is public and holds no real statement.

`python tests/cas_fixtures.py DIR` regenerates the JSON files the verify
skill imports (tests/fixtures/cas/)."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from casparser.types import (  # pyright: ignore[reportMissingTypeStubs]
    CASData,
    DematAccount,
    Equity,
    NSDLCASData,
)

# Real, public instrument identifiers (not personal data).
HDFC_MF = "INF179K01BE2"
ICICI_MF = "INF109K01Z48"
INFY = "INE009A01021"
TCS = "INE467B01029"
HDFCBANK = "INE040A01034"

NAMES = {INFY: "INFOSYS LIMITED", TCS: "TATA CONSULTANCY SERVICES LTD", HDFCBANK: "HDFC BANK LTD"}

_INVESTOR = {
    "name": "Test Investor",
    "email": "test@example.invalid",
    "address": "-",
    "mobile": "-",
}


def txn(
    on: str, kind: str, units: str | None, amount: str | None, desc: str = ""
) -> dict[str, Any]:
    return {
        "date": on,
        "description": desc or kind.title(),
        "amount": amount,
        "units": units,
        "nav": None,
        "balance": None,
        "type": kind,
    }


def scheme(
    isin: str,
    name: str,
    open_: str,
    close: str,
    txns: list[dict[str, Any]],
    fund_type: str = "EQUITY",
) -> dict[str, Any]:
    return {
        "scheme": name,
        "rta_code": "X1",
        "rta": "CAMS",
        "type": fund_type,
        "isin": isin,
        "amfi": None,
        "open": open_,
        "close": close,
        "close_calculated": close,
        "valuation": {"date": "2026-01-31", "nav": "100", "value": "0"},
        "transactions": txns,
    }


def rta(
    start: str,
    end: str,
    folios: list[dict[str, Any]],
    detailed: bool = True,
) -> CASData:
    return CASData.model_validate(
        {
            "statement_period": {"from": start, "to": end},
            "folios": folios,
            "investor_info": _INVESTOR,
            "cas_type": "DETAILED" if detailed else "SUMMARY",
            "file_type": "CAMS",
        }
    )


def folio(number: str, amc: str, schemes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"folio": number, "amc": amc, "schemes": schemes}


def demat(
    start: str,
    end: str,
    holdings: dict[str, str],
    *,
    dp_id: str = "IN300000",
    client_id: str = "10000001",
    file_type: str = "NSDL",
) -> NSDLCASData:
    equities = [
        Equity(
            isin=isin,
            num_shares=Decimal(qty),
            price=Decimal(1),
            value=Decimal(qty),
            name=NAMES.get(isin, isin),
        )
        for isin, qty in holdings.items()
    ]
    account = DematAccount.model_validate(
        {
            "name": "Test Broker",
            "type": "NSDL Demat Account",
            "dp_id": dp_id,
            "client_id": client_id,
            "folios": 0,
            "balance": "0",
            "owners": [{"name": "Test Investor", "PAN": "AAAAA0000A"}],
            "equities": [e.model_dump() for e in equities],
            "mutual_funds": [],
        }
    )
    return NSDLCASData.model_validate(
        {
            "accounts": [account.model_dump()],
            "statement_period": {"from": start, "to": end},
            "investor_info": _INVESTOR,
            "file_type": file_type,
        }
    )


# ---------------------------------------------------------------- scenarios

JAN = rta(
    "01-Jan-2026",
    "31-Jan-2026",
    [
        folio(
            "9000001/01",
            "HDFC Mutual Fund",
            [
                scheme(
                    HDFC_MF,
                    "HDFC Flexi Cap Fund - Direct Growth",
                    "0",
                    "150.5",
                    [
                        txn("2026-01-05", "PURCHASE_SIP", "100.5", "10000.00"),
                        txn("2026-01-05", "STAMP_DUTY_TAX", None, "0.50"),
                        txn("2026-01-20", "PURCHASE", "50", "5000.00"),
                        txn("2026-01-20", "STAMP_DUTY_TAX", None, "0.25"),
                    ],
                )
            ],
        )
    ],
)

JAN_FEB = rta(
    "01-Jan-2026",
    "28-Feb-2026",
    [
        folio(
            "9000001/01",
            "HDFC Mutual Fund",
            [
                scheme(
                    HDFC_MF,
                    "HDFC Flexi Cap Fund - Direct Growth",
                    "0",
                    "130.5",
                    [
                        txn("2026-01-05", "PURCHASE_SIP", "100.5", "10000.00"),
                        txn("2026-01-05", "STAMP_DUTY_TAX", None, "0.50"),
                        txn("2026-01-20", "PURCHASE", "50", "5000.00"),
                        txn("2026-01-20", "STAMP_DUTY_TAX", None, "0.25"),
                        txn("2026-02-10", "REDEMPTION", "-20", "2100.00"),
                        txn("2026-02-10", "STT_TAX", None, "0.02"),
                    ],
                )
            ],
        )
    ],
)

DEMAT_JAN = demat("01-Jan-2026", "31-Jan-2026", {INFY: "10", TCS: "5"})
DEMAT_FEB = demat("01-Feb-2026", "28-Feb-2026", {INFY: "12"})  # TCS sold out

FIXTURES: dict[str, CASData | NSDLCASData] = {
    "mf-2026-01.json": JAN,
    "mf-2026-01-to-02.json": JAN_FEB,
    "demat-2026-01.json": DEMAT_JAN,
    "demat-2026-02.json": DEMAT_FEB,
}


def write(data: CASData | NSDLCASData, path: Path) -> Path:
    path.write_text(data.model_dump_json(by_alias=True, indent=2) + "\n")
    return path


def write_all(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    return [write(data, root / name) for name, data in FIXTURES.items()]


if __name__ == "__main__":
    for p in write_all(Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/cas")):
        print(p)
