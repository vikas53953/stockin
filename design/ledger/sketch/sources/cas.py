"""CAS boundary. casparser types stop here; only `Statement` leaves.

Two entry points so the public repo never needs a PDF fixture: tests feed
`from_casparser()` a synthetic dict (casparser's JSON shape), production
feeds `read()` a real PDF that never leaves data/.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..facts import Statement


class CasRejected(ValueError):
    """Unparseable, unsupported layout, or internally inconsistent. Message
    names the folio/ISIN and the field; never includes the password."""


def read(pdf: Path, password: str) -> Statement:
    """sha256 the bytes, run casparser.read_cas_pdf, delegate. The password
    comes from a prompt or env, is used once and is not stored or logged."""
    raise NotImplementedError


def from_casparser(data: dict[str, Any], sha256: str) -> Statement:
    """Validate everything here; raise CasRejected rather than emit a fact
    we are unsure of. `Any` is allowed in this file only.

    TODO pseudocode:
      CAMS/KFin detailed CAS, per folio -> Account(mf_folio), per scheme:
        isin = parse_isin(scheme.isin)   # missing ISIN -> CasRejected
        Balance(on=period_start - 1 day, qty=scheme.open)
        Coverage(start=period_start, end=period_end)
        per transaction, mapped by casparser type:
          PURCHASE*, SWITCH_IN*        -> Buy(units, amount)
          REDEMPTION, SWITCH_OUT*      -> Sell(|units|, |amount|)
          DIVIDEND_PAYOUT              -> Income(amount)
          DIVIDEND_REINVEST            -> Income(amount) + Buy(units, amount)
          STAMP_DUTY_TAX, STT_TAX      -> folded into the amount of the Buy/Sell
                                          on the same day, not a separate fact
          REVERSAL                     -> cancels the txn it reverses in the SAME
                                          statement (same scheme, units and
                                          amount negated); both are dropped.
                                          An overlapping statement carries the
                                          pair too, so it converges. Unmatched
                                          reversal -> CasRejected
          SEGREGATION, MISC            -> CasRejected until a fixture exists
        nth = ordinal among identical (on, event) within the scheme
        Balance(on=period_end, qty=scheme.close)
        self-check: open + sum(deltas) == close, else CasRejected (do not
        import a statement that does not reconcile with itself)
      CAMS/KFin summary CAS -> Balance(on=period_end) only, no Coverage
      NSDL/CDSL eCAS, per demat account -> Account(demat), per holding:
        Balance(on=statement date, qty)
        Complete(account, on=statement date): the eCAS lists every holding,
        so Book closes any ISIN that is absent (no store read needed)
        MF folios inside an eCAS -> Balance only (CAMS CAS stays the detail source)
    """
    raise NotImplementedError
