"""CAS mapping, the SQLite ledger and the import/holdings/reconcile commands,
all on synthetic casparser output (tests/cas_fixtures.py)."""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import cas_fixtures as fx
import pytest
from typer.testing import CliRunner

from stockin.book import reconcile
from stockin.cli import app
from stockin.facts import Balance, Buy, Complete, Coverage, Income, Sell, Txn
from stockin.money import Money, Qty
from stockin.sources import cas
from stockin.store import Ledger, LedgerMissing, LedgerWriter

runner = CliRunner()


def _files(tmp_path: Path) -> dict[str, Path]:
    return {p.name: p for p in fx.write_all(tmp_path / "in")}


def _ledger(tmp_path: Path, *names: str, tag: str = "a") -> Path:
    files = _files(tmp_path)
    db = tmp_path / f"{tag}.db"
    with LedgerWriter.open(db) as w:
        for n in names:
            w.ingest(cas.read(files[n]))
    return db


def _rta(txns: list[dict[str, Any]], open_: str = "0", close: str = "10") -> fx.CASData:
    return fx.rta(
        "01-Jan-2026",
        "31-Jan-2026",
        [fx.folio("1/1", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", open_, close, txns)])],
    )


# ------------------------------------------------------------------ mapping


def test_detailed_mf_statement_maps_to_checkpoints_coverage_and_txns() -> None:
    st = cas.from_casparser(fx.JAN, "sha")
    kinds = [type(f).__name__ for f in st.facts]
    assert kinds == ["Balance", "Coverage", "Txn", "Txn", "Balance"]
    opening, cover, *_, closing = st.facts
    assert isinstance(opening, Balance) and str(opening.on) == "2025-12-31"
    assert opening.qty == Qty(micros=0)
    assert isinstance(cover, Coverage) and (str(cover.start), str(cover.end)) == (
        "2026-01-01",
        "2026-01-31",
    )
    assert isinstance(closing, Balance) and closing.qty == Qty.parse("150.5")
    sip = st.facts[2]
    assert isinstance(sip, Txn)
    assert isinstance(sip.event, Buy)
    assert sip.event.amount == Money.rupees("10000.50")  # stamp duty folded into cost


def test_redemption_proceeds_are_net_of_stt() -> None:
    st = cas.from_casparser(fx.JAN_FEB, "sha")
    sells = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Sell)]
    assert len(sells) == 1
    assert sells[0].amount == Money.rupees("2099.98")


def test_summary_statement_gives_only_a_closing_balance() -> None:
    data = fx.rta(
        "01-Jan-2026",
        "31-Jan-2026",
        [fx.folio("1/1", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", "0", "42", [])])],
        detailed=False,
    )
    st = cas.from_casparser(data, "sha")
    assert [type(f).__name__ for f in st.facts] == ["Balance"]


def test_demat_statement_lists_balances_and_completes_the_account() -> None:
    st = cas.from_casparser(fx.DEMAT_JAN, "sha")
    assert st.source == "nsdl_cas"
    assert sum(isinstance(f, Balance) for f in st.facts) == 2
    assert isinstance(st.facts[-1], Complete)
    (acct,) = st.accounts
    assert acct.masked == "Test Broker ...0001"
    assert "10000001" not in acct.model_dump_json()  # the client id is never stored


def test_same_day_identical_purchases_stay_two_facts() -> None:
    st = cas.from_casparser(
        _rta(
            [
                fx.txn("2026-01-05", "PURCHASE", "5", "500"),
                fx.txn("2026-01-05", "PURCHASE", "5", "500"),
            ]
        ),
        "sha",
    )
    txns = [f for f in st.facts if isinstance(f, Txn)]
    assert [t.nth for t in txns] == [0, 1]


def test_reversal_cancels_its_pair() -> None:
    st = cas.from_casparser(
        _rta(
            [
                fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-07", "PURCHASE", "3", "300"),
                fx.txn("2026-01-09", "REVERSAL", "-3", "-300"),
            ]
        ),
        "sha",
    )
    buys = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Buy)]
    assert [b.units for b in buys] == [Qty.parse("10")]
    assert sum(isinstance(f, Txn) for f in st.facts) == 1


@pytest.mark.parametrize(
    ("txns", "close", "reason"),
    [
        ([fx.txn("2026-01-05", "PURCHASE", "10", "1000")], "11", "closing"),
        ([fx.txn("2026-01-05", "GIFT_IN", "10", "1000")], "10", "unsupported GIFT_IN"),
        ([fx.txn("2026-01-05", "REVERSAL", "-10", "-1000")], "10", "no matching earlier row"),
        ([fx.txn("2026-01-05", "PURCHASE", "1.0000001", "1")], "1.0000001", "6 decimals"),
        (
            [
                fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-06", "STAMP_DUTY_TAX", None, "0.05"),
            ],
            "10",
            "no purchase that day",
        ),
    ],
)
def test_statement_we_cannot_trust_is_rejected_whole(
    txns: list[dict[str, Any]], close: str, reason: str
) -> None:
    with pytest.raises(cas.CasRejected, match=reason):
        cas.from_casparser(_rta(txns, close=close), "sha")


def test_identical_reversals_each_cancel_their_own_row() -> None:
    st = cas.from_casparser(
        _rta(
            [
                fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-25", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-25", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-26", "REVERSAL", "-10", "-1000"),
                fx.txn("2026-01-26", "REVERSAL", "-10", "-1000"),
            ]
        ),
        "sha",
    )
    assert [t.on.day for t in st.facts if isinstance(t, Txn)] == [5]


def test_reversal_never_cancels_a_later_row() -> None:
    txns = [
        fx.txn("2026-01-03", "REVERSAL", "-10", "-1000"),
        fx.txn("2026-01-25", "PURCHASE", "10", "1000"),
    ]
    with pytest.raises(cas.CasRejected, match="no matching earlier row"):
        cas.from_casparser(_rta(txns, close="0"), "sha")


def test_each_stamp_duty_goes_to_the_buy_it_follows() -> None:
    st = cas.from_casparser(
        _rta(
            [
                fx.txn("2026-01-05", "PURCHASE", "5", "500"),
                fx.txn("2026-01-05", "STAMP_DUTY_TAX", None, "0.03"),
                fx.txn("2026-01-05", "PURCHASE", "5", "600"),
                fx.txn("2026-01-05", "STAMP_DUTY_TAX", None, "0.04"),
            ]
        ),
        "sha",
    )
    buys = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Buy)]
    assert [b.amount for b in buys] == [Money.rupees("500.03"), Money.rupees("600.04")]


def test_trailing_zero_amount_is_whole_paise() -> None:
    st = cas.from_casparser(_rta([fx.txn("2026-01-05", "PURCHASE", "10", "1000.100")]), "sha")
    (buy,) = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Buy)]
    assert buy.amount == Money.rupees("1000.10")


def test_redemption_without_an_amount_is_rejected() -> None:
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-01-06", "REDEMPTION", "-10", None),
    ]
    with pytest.raises(cas.CasRejected, match="needs negative units and an amount"):
        cas.from_casparser(_rta(txns, close="0"), "sha")


@pytest.mark.parametrize(
    ("txns", "reason"),
    [
        ([fx.txn("2026-01-05", "PURCHASE", "10", "1000.005")], "whole paise"),
        ([fx.txn("2026-01-05", "PURCHASE", "10", "1E+40")], "whole paise|bad amount"),
        ([fx.txn("2026-01-05", "PURCHASE", "10", "-1000")], "positive units and amount"),
        (
            [
                fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
                fx.txn("2026-01-06", "REDEMPTION", "-10", "-5"),
                fx.txn("2026-01-06", "STT_TAX", None, "6"),
            ],
            "larger than the amount",
        ),
    ],
)
def test_amounts_we_cannot_represent_exactly_are_rejected(
    txns: list[dict[str, Any]], reason: str
) -> None:
    close = "0" if any(t["type"] == "REDEMPTION" for t in txns) else "10"
    with pytest.raises(cas.CasRejected, match=reason):
        cas.from_casparser(_rta(txns, close=close), "sha")


def _income(st: Any) -> list[Money]:
    return [f.event.amount for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Income)]


def test_tds_on_an_idcw_payout_comes_off_the_payout() -> None:
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-02-25", "DIVIDEND_PAYOUT", None, "12124.43"),
        fx.txn("2026-02-25", "TDS_TAX", None, "1212.44"),
    ]
    st = cas.from_casparser(_rta(txns), "sha")
    assert _income(st) == [Money.rupees("10911.99")]


def test_tds_after_a_reinvestment_is_already_deducted() -> None:
    """The reinvested amount is what was left after TDS: Income equals the
    Buy, and the TDS row does not take it off a second time."""
    txns = [
        fx.txn("2026-02-25", "DIVIDEND_REINVEST", "10", "900"),
        fx.txn("2026-02-25", "TDS_TAX", None, "100"),
    ]
    st = cas.from_casparser(_rta(txns), "sha")
    assert _income(st) == [Money.rupees("900")]
    (buy,) = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Buy)]
    assert buy.amount == Money.rupees("900")


def test_tds_on_a_redemption_still_comes_off_the_proceeds() -> None:
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-01-06", "REDEMPTION", "-10", "1100"),
        fx.txn("2026-01-06", "TDS_TAX", None, "10"),
    ]
    st = cas.from_casparser(_rta(txns, close="0"), "sha")
    (sell,) = [f.event for f in st.facts if isinstance(f, Txn) and isinstance(f.event, Sell)]
    assert sell.amount == Money.rupees("1090")


def test_tds_with_nothing_to_come_off_is_rejected() -> None:
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-01-05", "TDS_TAX", None, "10"),
    ]
    with pytest.raises(cas.CasRejected, match="follows no redemption or IDCW payout"):
        cas.from_casparser(_rta(txns), "sha")


@pytest.mark.parametrize(
    "body",
    [
        "{this is not json",
        "[1, 2]",
        '{"folios": "nope"}',
        None,  # a blank folio number
    ],
)
def test_malformed_files_are_rejected_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | None
) -> None:
    path = tmp_path / "x.json"
    if body is None:
        data = fx.rta(
            "01-Jan-2026",
            "31-Jan-2026",
            [fx.folio("   ", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", "0", "0", [])])],
        )
        fx.write(data, path)
    else:
        path.write_text(body)
    with pytest.raises(cas.CasRejected):
        cas.read(path)
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    r = runner.invoke(app, ["import", "cas", str(path)])
    assert r.exit_code == 2
    assert "Traceback" not in r.output


def test_null_scheme_is_rejected_not_crashed(tmp_path: Path) -> None:
    body = json.loads(fx.JAN.model_dump_json(by_alias=True))
    body["folios"][0]["schemes"][0] = None  # casparser's validator calls .items() on it
    path = tmp_path / "n.json"
    path.write_text(json.dumps(body))
    with pytest.raises(cas.CasRejected):
        cas.read(path)


_BLANK_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.mark.parametrize("body", [_BLANK_PDF, b"\x00\x01 not a pdf at all " * 50])
def test_a_pdf_that_is_not_a_cas_is_said_in_plain_words(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "statement.pdf"
    path.write_bytes(body)
    with pytest.raises(cas.CasRejected, match="does not look like a CAS") as err:
        cas.read(path, "some-password")
    assert "some-password" not in str(err.value)


def test_ledger_file_is_private(tmp_path: Path) -> None:
    db = _ledger(tmp_path, "mf-2026-01.json")
    assert db.stat().st_mode & 0o077 == 0


def test_bad_isin_is_rejected() -> None:
    data = fx.demat("01-Jan-2026", "31-Jan-2026", {"INE009A01022": "1"})
    with pytest.raises(cas.CasRejected, match="invalid ISIN"):
        cas.from_casparser(data, "sha")


# ------------------------------------------------------------------- ledger


def test_reimport_adds_nothing(tmp_path: Path) -> None:
    files = _files(tmp_path)
    with LedgerWriter.open(tmp_path / "l.db") as w:
        first = w.ingest(cas.read(files["mf-2026-01.json"]))
        again = w.ingest(cas.read(files["mf-2026-01.json"]))
    assert (first.seen_before, first.facts_added) == (False, 5)
    assert (again.seen_before, again.facts_added, again.facts_already_present) == (True, 0, 5)


def test_ledger_depends_on_the_set_of_files_not_their_order(tmp_path: Path) -> None:
    names = list(fx.FIXTURES)
    digests = {
        Ledger.open(_ledger(tmp_path, *order, tag=str(i))).digest()
        for i, order in enumerate(itertools.permutations(names))
    }
    repeated = Ledger.open(_ledger(tmp_path, *names, *names, tag="twice")).digest()
    assert len(digests) == 1
    assert repeated in digests


def test_overlapping_statements_do_not_double_count(tmp_path: Path) -> None:
    db = _ledger(tmp_path, "mf-2026-01.json", "mf-2026-01-to-02.json")
    with Ledger.open(db) as led:
        book = led.book()
    (h,) = book.holdings()
    assert h.qty == Qty.parse("130.5")
    assert h.cost == Money.rupees("13010.60")  # 15000.75 less 20/100.5 of the SIP lot
    assert reconcile(book).ok


def test_sold_out_demat_isin_drops_from_holdings(tmp_path: Path) -> None:
    db = _ledger(tmp_path, "demat-2026-01.json", "demat-2026-02.json")
    with Ledger.open(db) as led:
        book = led.book()
    assert [(h.isin, str(h.qty)) for h in book.holdings()] == [(fx.INFY, "12")]
    report = reconcile(book)
    assert report.ok
    assert {c.status for c in report.checks} == {"inferred"}


def test_retract_removes_only_what_no_other_statement_asserts(tmp_path: Path) -> None:
    files = _files(tmp_path)
    with LedgerWriter.open(tmp_path / "l.db") as w:
        w.ingest(cas.read(files["mf-2026-01.json"]))
        before = w.digest()
        later = w.ingest(cas.read(files["mf-2026-01-to-02.json"]))
        removed = w.retract(later.statement_sha)
        after = w.digest()
    assert removed == 3  # Feb sell, new coverage, Feb balance; Jan facts stay
    assert after == before


def test_read_handle_cannot_write(tmp_path: Path) -> None:
    db = _ledger(tmp_path, "mf-2026-01.json")
    with Ledger.open(db) as led, pytest.raises(sqlite3.OperationalError, match="readonly"):
        led._conn.execute("DELETE FROM facts")  # pyright: ignore[reportPrivateUsage]


def test_read_handle_needs_an_existing_ledger(tmp_path: Path) -> None:
    with pytest.raises(LedgerMissing):
        Ledger.open(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


# ---------------------------------------------------------------------- cli


def test_cli_import_holdings_reconcile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _files(tmp_path)
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    for name in fx.FIXTURES:
        r = runner.invoke(app, ["import", "cas", str(files[name])])
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["import", "cas", str(files["mf-2026-01.json"]), "--json"])
    report = json.loads(r.output)
    assert (report["seen_before"], report["facts_added"]) == (True, 0)

    r = runner.invoke(app, ["holdings", "--json"])
    held = {h["isin"]: h for h in json.loads(r.output)["holdings"]}
    assert held.keys() == {fx.INFY, fx.HDFC_MF}
    assert held[fx.INFY]["cost"] is None

    r = runner.invoke(app, ["holdings"])
    assert "INFOSYS LIMITED" in r.output and "cost unknown" in r.output

    r = runner.invoke(app, ["reconcile"])
    assert r.exit_code == 0
    assert "0 mismatch" in r.output


def test_cli_reconcile_exits_1_on_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    jan = _rta([fx.txn("2026-01-05", "PURCHASE", "10", "1000")])
    # A later statement whose opening balance contradicts January's close.
    feb = fx.rta(
        "01-Feb-2026",
        "28-Feb-2026",
        [fx.folio("1/1", "Test AMC", [fx.scheme(fx.HDFC_MF, "Test Fund", "7", "7", [])])],
    )
    a, b = fx.write(jan, tmp_path / "jan.json"), fx.write(feb, tmp_path / "feb.json")
    assert runner.invoke(app, ["import", "cas", str(a)]).exit_code == 0
    assert runner.invoke(app, ["import", "cas", str(b)]).exit_code == 0
    r = runner.invoke(app, ["reconcile"])
    assert r.exit_code == 1
    assert "MISMATCH balance" in r.output


def test_cli_rejected_file_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    bad = fx.write(
        _rta([fx.txn("2026-01-05", "PURCHASE", "10", "1000")], close="9"), tmp_path / "b.json"
    )
    r = runner.invoke(app, ["import", "cas", str(bad)])
    assert r.exit_code == 2
    assert "nothing was imported" in r.output
    assert not (tmp_path / "data" / "ledger.db").exists()


def test_cli_without_ledger_says_how_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    for cmd in ("holdings", "reconcile"):
        r = runner.invoke(app, [cmd])
        assert r.exit_code == 2
        assert "stockin import cas" in r.output


def test_a_reinvestment_is_labelled_at_import_and_keeps_its_stamp_duty() -> None:
    """The statement's own row type marks both halves of a reinvestment. The
    stamp duty after it is added to the Buy's cost and the label survives, so
    nothing downstream has to pair the Buy with its Income by amount."""
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "5", "500"),
        fx.txn("2026-01-05", "STAMP_DUTY_TAX", None, "0.03"),
        fx.txn("2026-01-25", "DIVIDEND_REINVEST", "5", "800"),
        fx.txn("2026-01-25", "STAMP_DUTY_TAX", None, "0.04"),
    ]
    st = cas.from_casparser(_rta(txns), "sha")
    events = [f.event for f in st.facts if isinstance(f, Txn)]
    assert events == [
        Buy(units=Qty.parse("5"), amount=Money.rupees("500.03")),
        Income(amount=Money.rupees("800"), reinvested=True),
        Buy(units=Qty.parse("5"), amount=Money.rupees("800.04"), reinvested=True),
    ]


def test_a_payout_is_not_labelled_reinvested() -> None:
    txns = [
        fx.txn("2026-01-05", "PURCHASE", "10", "1000"),
        fx.txn("2026-01-25", "DIVIDEND_PAYOUT", None, "80"),
        fx.txn("2026-01-25", "TDS_TAX", None, "8"),
    ]
    st = cas.from_casparser(_rta(txns), "sha")
    events = [f.event for f in st.facts if isinstance(f, Txn)]
    assert [getattr(e, "reinvested", None) for e in events] == [False, False]
    assert _income(st) == [Money.rupees("72")]


def test_the_label_does_not_change_the_id_of_any_earlier_fact() -> None:
    """`reinvested=False` is left out of the canonical JSON, so a ledger
    written before the label existed converges with a fresh import instead of
    holding every purchase twice. The ids are the ones the code gave before."""
    from datetime import date

    from stockin.facts import AccountId, fact_id, parse_isin

    acct, isin, on = AccountId("0123456789abcdef"), parse_isin("INF179K01UT0"), date(2026, 1, 5)
    buy = Txn(
        account=acct,
        isin=isin,
        on=on,
        event=Buy(units=Qty.parse("10"), amount=Money.rupees("1000")),
    )
    pay = Txn(account=acct, isin=isin, on=on, event=Income(amount=Money.rupees("50")))
    assert fact_id(buy) == "2025eae7761ab93d930fdd368e27b5febde234c19eae1e535c0c90ff45cd3826"
    assert fact_id(pay) == "fe66a6599372dc4b3bdd79b65ec720a0acca7f0ff1ccf8a3be04fc34b65b7792"
    again = buy.model_copy(update={"event": buy.event.model_copy(update={"reinvested": True})})
    assert fact_id(again) != fact_id(buy)  # the label is part of the fact when it is set


def test_a_reinvestment_survives_the_ledger_round_trip(tmp_path: Path) -> None:
    txns = [
        fx.txn("2026-01-25", "DIVIDEND_REINVEST", "10", "800"),
        fx.txn("2026-01-25", "STAMP_DUTY_TAX", None, "0.04"),
    ]
    st = cas.from_casparser(_rta(txns), "sha")
    db = tmp_path / "ledger.db"
    with LedgerWriter.open(db) as w:
        w.ingest(st)
    with Ledger.open(db) as led:
        facts = led.book().facts
    flags = sorted(
        (type(f.event).__name__, f.event.reinvested)  # type: ignore[union-attr]
        for f in facts
        if isinstance(f, Txn)
    )
    assert flags == [("Buy", True), ("Income", True)]


# ------------------------------------------------------- ledger format 1


def _format1_ledger(db: Path, txns: list[dict[str, Any]]) -> Path:
    """A ledger as Stockin wrote it before reinvestments were labelled:
    the same tables, user_version 1, and the unlabelled facts it held."""
    with LedgerWriter.open(db) as w:
        w.ingest(cas.from_casparser(_rta(txns), "old"))
    conn = sqlite3.connect(db)
    for fid, body in list(conn.execute("SELECT id, body FROM facts")):
        conn.execute(
            "UPDATE facts SET body = ? WHERE id = ?", (body.replace(',"reinvested":true', ""), fid)
        )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return db


_REINVEST = [
    fx.txn("2026-01-05", "PURCHASE", "5", "500"),
    fx.txn("2026-01-25", "DIVIDEND_REINVEST", "5", "800"),
    fx.txn("2026-01-25", "STAMP_DUTY_TAX", None, "0.04"),
]


def test_a_format1_ledger_with_dividends_is_refused_not_double_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stockin.store import LedgerOutdated

    db = _format1_ledger(tmp_path / "data" / "ledger.db", _REINVEST)
    before = db.read_bytes()
    with pytest.raises(LedgerOutdated):
        Ledger.open(db)
    with pytest.raises(LedgerOutdated):
        LedgerWriter.open(db)
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path / "data"))
    stmt = tmp_path / "again.json"
    fx.write(_rta(_REINVEST), stmt)
    r = runner.invoke(app, ["import", "cas", str(stmt)])
    assert r.exit_code == 2 and "Traceback" not in r.output
    assert "did not label dividend reinvestments" in r.output
    assert "nothing was imported" in r.output
    assert db.read_bytes() == before  # the refused import changed nothing
    for cmd in (["holdings"], ["insights"]):
        r = runner.invoke(app, cmd)
        assert r.exit_code == 2 and "import your statements again" in r.output
    r = runner.invoke(app, ["doctor", "--json"])
    (check,) = [c for c in json.loads(r.output)["checks"] if c["name"] == "ledger"]
    assert check["status"] == "fail"


def test_a_format1_ledger_without_dividends_is_upgraded_and_converges(tmp_path: Path) -> None:
    txns = [fx.txn("2026-01-05", "PURCHASE", "10", "1000")]
    db = _format1_ledger(tmp_path / "ledger.db", txns)
    with Ledger.open(db) as led:
        assert led.book().holdings()
    with LedgerWriter.open(db) as w:
        report = w.ingest(cas.from_casparser(_rta(txns), "new"))
    assert report.facts_added == 0
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone() == (2,)
    conn.close()


def test_a_rebuilt_ledger_holds_a_reinvestment_once(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    for sha in ("a", "b"):
        with LedgerWriter.open(db) as w:
            w.ingest(cas.from_casparser(_rta(_REINVEST), sha))
    with Ledger.open(db) as led:
        (h,) = led.book().holdings()
    assert h.qty == Qty.parse("10")
