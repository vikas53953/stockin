import json
from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from stockin.facts import (
    Balance,
    Buy,
    Fact,
    Thesis,
    Txn,
    account_id,
    fact_id,
    parse_isin,
)
from stockin.money import Money, Price, Qty

# Public ISINs of listed instruments, not anyone's holdings.
INFY = "INE009A01021"
TCS = "INE467B01029"


def test_valid_isins() -> None:
    assert parse_isin(INFY) == INFY
    assert parse_isin(f" {TCS.lower()} ") == TCS


@pytest.mark.parametrize("bad", ["INE009A01022", "INE009A0102", "1NE009A01021", ""])
def test_invalid_isins(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_isin(bad)


def _sip(nth: int = 0) -> Txn:
    return Txn(
        account=account_id("mf_folio", "Example AMC", "12345678"),
        isin=parse_isin(INFY),
        on=date(2026, 10, 5),
        event=Buy(units=Qty.parse("10.5"), amount=Money.rupees("5000")),
        nth=nth,
    )


def test_fact_id_is_content_derived() -> None:
    assert fact_id(_sip()) == fact_id(_sip())
    assert fact_id(_sip(0)) != fact_id(_sip(1))  # two real SIPs on one day stay apart


def test_fact_id_survives_a_storage_round_trip() -> None:
    adapter: TypeAdapter[Fact] = TypeAdapter(Fact)
    original = _sip()
    restored = adapter.validate_json(adapter.dump_json(original))
    assert restored == original
    assert fact_id(restored) == fact_id(original)


def test_account_id_ignores_statement_provider_and_case() -> None:
    a = account_id("mf_folio", "Example AMC", "12345678")
    assert a == account_id("mf_folio", " example amc ", "12345678")
    assert a != account_id("demat", "Example AMC", "12345678")
    assert len(a) == 16


def test_balance_and_txn_have_distinct_ids() -> None:
    bal = Balance(
        account=_sip().account, isin=_sip().isin, on=date(2026, 10, 31), qty=Qty.parse("10.5")
    )
    assert fact_id(bal) != fact_id(_sip())


def test_thesis_needs_a_rule() -> None:
    with pytest.raises(ValidationError):
        Thesis(isin=parse_isin(INFY), why="no rules", recorded_on=date(2026, 11, 17))
    t = Thesis(
        isin=parse_isin(INFY),
        why="Margin recovery",
        invalidate_below=Price.rupees("1350"),
        recorded_on=date(2026, 11, 17),
    )
    assert t.invalidate_below is not None


def test_coverage_window_must_be_ordered() -> None:
    from stockin.facts import Coverage

    with pytest.raises(ValidationError):
        Coverage(
            account=_sip().account,
            isin=_sip().isin,
            start=date(2026, 10, 31),
            end=date(2026, 10, 1),
        )


def test_facts_load_back_only_through_json() -> None:
    """Strict mode: stored facts must be loaded with validate_json. Pinned so a
    store that loads dicts instead fails here, not on real data."""
    adapter: TypeAdapter[Fact] = TypeAdapter(Fact)
    body = adapter.dump_json(_sip())
    assert adapter.validate_json(body) == _sip()
    with pytest.raises(ValidationError):
        adapter.validate_python(json.loads(body))
