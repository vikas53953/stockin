"""Net worth: FD accrual worked by hand, the latest-snapshot rule, the mix and
its drift, sentences that agree with their numbers, the store and its damage
contract, and the CLI the owner types."""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import stat
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stockin.cli import _drift  # pyright: ignore[reportPrivateUsage]
from stockin.cli import app as cli
from stockin.insights import pct, tenths_of, weights
from stockin.money import Bps, Money, Price, Qty
from stockin.networth import (
    DRIFT_BAND,
    STALE_DAYS,
    AssetsDamaged,
    AssetStore,
    Closed,
    Entry,
    Fd,
    Gold,
    NetWorth,
    Stated,
    current,
    fd_maturity,
    fd_value,
    networth,
    parse_targets,
    shares,
)
from stockin.prices import PriceBook
from stockin.store import Ledger

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()
TODAY = date(2026, 11, 22)


def rs(text: str) -> Money:
    return Money.rupees(text)


def _fd(principal: str, rate: int, start: date, months: int, label: str = "FD") -> Fd:
    return Fd(
        label=label,
        as_of=start,
        principal=rs(principal),
        rate=Bps(rate),
        start=start,
        months=months,
    )


# -------------------------------------------------------------------- FDs


def test_a_one_year_fd_compounds_quarterly_like_a_bank() -> None:
    """₹1,00,000 at 7.1% for a year, quarterly: 1e5 x 1.01775^4 = 1,07,291.28.
    Bank calculators show ₹1,07,291 for this deposit."""
    fd = _fd("100000", 710, date(2026, 4, 1), 12)
    assert fd_maturity(fd) == date(2027, 4, 1)
    assert fd_value(fd, date(2027, 4, 1)) == rs("107291.28")
    assert fd_value(fd, date(2030, 1, 1)) == rs("107291.28")  # stops at maturity
    assert fd_value(fd, date(2026, 4, 1)) == rs("100000")
    assert fd_value(fd, date(2026, 1, 1)) == rs("100000")  # before it opened


def test_between_quarters_the_days_since_the_last_quarter_earn_simple_interest() -> None:
    fd = _fd("100000", 710, date(2026, 4, 1), 12)
    # one full quarter (to 1 Jul) = 1,01,775; then 31 days to 1 Aug at 7.1%
    getcontext().prec = 50
    expected = Decimal("101775") * (1 + Decimal("0.071") * 31 / 365)
    want = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    assert fd_value(fd, date(2026, 8, 1)) == rs(str(want))


def test_a_short_fd_earns_simple_interest() -> None:
    fd = _fd("50000", 650, date(2026, 6, 1), 3)
    days = (fd_maturity(fd) - fd.start).days  # 92
    want = (Decimal("50000") * (1 + Decimal("0.065") * days / 365)).quantize(Decimal("0.01"))
    assert fd_value(fd, date(2027, 1, 1)) == rs(str(want))


def test_an_fd_opened_on_the_31st_matures_on_the_last_day_of_a_short_month() -> None:
    assert fd_maturity(_fd("1000", 700, date(2026, 1, 31), 1)) == date(2026, 2, 28)
    assert fd_maturity(_fd("1000", 700, date(2027, 11, 30), 3)) == date(2028, 2, 29)


# -------------------------------------------------------- latest snapshot


def _bank(amount: str, on: date, label: str = "SBI savings") -> Stated:
    return Stated(kind="bank", label=label, as_of=on, amount=rs(amount))


def test_the_latest_snapshot_is_current_and_labels_ignore_case_and_spaces() -> None:
    entries: list[Entry] = [
        _bank("100", date(2026, 9, 1)),
        _bank("300", date(2026, 11, 1), label="sbi   SAVINGS"),
        _bank("200", date(2026, 10, 1)),
        _bank("999", date(2026, 12, 1)),  # dated after today: not yet
    ]
    (held,) = current(entries, TODAY)
    assert isinstance(held, Stated) and held.amount == rs("300")
    assert current(list(reversed(entries)), TODAY) == (held,)


def test_closing_an_entry_hides_it_from_that_day_and_keeps_its_history() -> None:
    entries: list[Entry] = [
        _bank("100", date(2026, 9, 1)),
        Closed(kind="bank", label="SBI savings", as_of=date(2026, 10, 1)),
    ]
    assert current(entries, date(2026, 9, 30)) == (entries[0],)
    assert current(entries, TODAY) == ()
    # a new balance after closing opens it again
    reopened = [*entries, _bank("50", date(2026, 11, 1))]
    assert [e.as_of for e in current(reopened, TODAY)] == [date(2026, 11, 1)]


@pytest.mark.parametrize("label", ["", "   ", "x" * 61, "tab\x07bell"])
def test_a_label_must_be_printable_and_short(label: str) -> None:
    with pytest.raises(ValueError):
        _bank("1", TODAY, label=label)


@pytest.mark.parametrize(
    "bad",
    [
        lambda: Stated(kind="bank", label="x", as_of=TODAY, amount=rs("-1")),
        lambda: Stated(kind="bank", label="x", as_of=TODAY, amount=rs("100000000000000")),
        lambda: _fd("0", 700, TODAY, 12),
        lambda: _fd("1000", 5001, TODAY, 12),
        lambda: _fd("1000", 700, TODAY, 0),
        lambda: Gold(label="g", as_of=TODAY, grams=Qty.parse("20000000"), rate=Price.rupees("1")),
    ],
)
def test_typos_are_refused_not_stored(bad: object) -> None:
    with pytest.raises(ValueError):
        bad()  # type: ignore[operator]


# ----------------------------------------------------------- the picture


def _picture(targets: dict[str, int] | None = None) -> NetWorth:
    entries: list[Entry] = [
        _bank("250000", date(2026, 11, 1)),
        Stated(kind="epf", label="EPF", as_of=date(2026, 7, 31), amount=rs("845000")),
        _fd("200000", 710, date(2026, 4, 1), 12, label="HDFC FD"),
        Gold(label="Gold", as_of=TODAY, grams=Qty.parse("20"), rate=Price.rupees("7650")),
        Stated(kind="loan", label="Home loan", as_of=TODAY, amount=rs("300000")),
    ]
    t = {k: Bps(v) for k, v in (targets or {}).items()}
    return networth(None, PriceBook(quotes={}), entries, t, TODAY)  # type: ignore[arg-type]


def test_net_worth_is_what_you_own_minus_loans() -> None:
    nw = _picture()
    fd = fd_value(_fd("200000", 710, date(2026, 4, 1), 12), TODAY)
    owned = rs("250000") + rs("845000") + fd + rs("153000")
    assert nw.owned == owned and nw.owed == rs("300000")
    assert nw.total == owned - rs("300000")
    assert sum(s.bps for s in nw.slices) == 10_000
    assert [s.bucket for s in nw.slices] == ["retirement", "cash", "debt", "gold"]
    assert nw.market == rs("0") and nw.unpriced == ()


def test_drift_is_weight_minus_target_and_untargeted_classes_have_none() -> None:
    nw = _picture({"cash": 1000, "debt": 2000, "gold": 1000, "equity": 6000})
    by = {s.bucket: s for s in nw.slices}
    assert by["cash"].drift == by["cash"].bps - 1000
    assert by["equity"].bps == 0 and by["equity"].drift == -6000  # targeted, none held
    assert by["retirement"].target is None and by["retirement"].drift is None


_DRIFT = re.compile(
    r"^(?P<name>[\w ]+) is (?P<now>[\d.]+)% of what you own, (?P<pts>[\d.]+) points? "
    r"(?P<side>above|below) your target of (?P<aim>[\d.]+)%\.$"
)


def test_each_drift_sentence_agrees_with_the_slice_it_describes() -> None:
    nw = _picture({"cash": 1000, "debt": 2000, "gold": 1000, "equity": 6000})
    drifts = [n for n in nw.notes if n.kind == "drift"]
    flagged = {s.bucket for s in nw.slices if s.drift is not None and abs(s.drift) > DRIFT_BAND}
    assert {n.bucket for n in drifts} == flagged and flagged
    for n in drifts:
        s = next(s for s in nw.slices if s.bucket == n.bucket)
        m = _DRIFT.match(n.text)
        assert m, n.text
        assert m["name"] == s.name
        assert f"{m['now']}%" == pct(tenths_of(s.bps, 10_000))
        assert s.target is not None and f"{m['aim']}%" == pct(tenths_of(s.target, 10_000))
        assert Decimal(m["now"]) - Decimal(m["aim"]) == Decimal(m["pts"]) * (
            1 if m["side"] == "above" else -1
        )
        assert (s.drift or 0) > 0 if m["side"] == "above" else (s.drift or 0) < 0


def test_a_mix_inside_the_band_says_so() -> None:
    nw = _picture()
    mix = {s.bucket: s.bps for s in nw.slices}
    nw = _picture(dict(mix))  # target = exactly what you hold
    assert [n.kind for n in nw.notes if n.tone == "good"] == ["on_target"]
    assert not [n for n in nw.notes if n.kind == "drift"]


def test_old_balances_matured_fds_and_loans_are_called_out() -> None:
    nw = _picture()
    kinds = [n.kind for n in nw.notes]
    stale = next(n for n in nw.notes if n.kind == "stale")
    assert "EPF" in stale.text and f"{(TODAY - date(2026, 7, 31)).days} days" in stale.text
    assert (TODAY - date(2026, 7, 31)).days > STALE_DAYS
    loans = next(n for n in nw.notes if n.kind == "loans")
    assert loans.bps == rs("300000").share_of(nw.owned)
    assert f"₹{nw.owed}" in loans.text and pct(tenths_of(loans.bps or 0, 10_000)) in loans.text
    assert "matured" not in kinds

    matured = networth(
        None, PriceBook(quotes={}), [_fd("1000", 700, date(2025, 1, 1), 6)], {}, TODAY
    )
    (note,) = [n for n in matured.notes if n.kind == "matured"]
    assert "2025-07-01" in note.text and f"₹{matured.total}" in note.text


def test_largest_remainder_shares_add_to_exactly_100_percent() -> None:
    assert shares([1, 1, 1]) == [3334, 3333, 3333]
    assert shares([5, 0]) == [10_000, 0]
    with pytest.raises(ValueError):
        shares([0, 0])


@pytest.mark.parametrize(
    ("pairs", "said"),
    [
        (["equity=60", "debt=30"], "add to 90.0%"),
        (["stocks=100"], "is not CLASS=PERCENT"),
        (["equity=50", "equity=50"], "given twice"),
        (["equity"], "is not CLASS=PERCENT"),
        (["equity=101"], "above 100%"),
    ],
)
def test_targets_must_name_real_classes_and_add_to_100(pairs: list[str], said: str) -> None:
    with pytest.raises(ValueError, match=re.escape(said)):
        parse_targets(pairs)


def test_targets_accept_real_estate_spelled_with_a_space_or_dash() -> None:
    assert parse_targets(["Real estate=40", "equity=60"]) == {"real_estate": 4000, "equity": 6000}
    assert parse_targets(["real-estate=100", "gold=0"]) == {"real_estate": 10_000}


# ----------------------------------------------------- market holdings


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    monkeypatch.setattr("stockin.cli._today", lambda: TODAY)
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


def test_market_holdings_are_valued_exactly_as_the_home_screen_values_them(data: Path) -> None:
    from stockin.prices import PriceStore

    with Ledger.open(data / "ledger.db") as ledger:
        book = ledger.book()
    with PriceStore.open(data / "prices.db") as store:
        prices = store.book(frozenset(h.isin for h in book.holdings()))
    _, total, unpriced = weights(book, prices)
    nw = networth(book, prices, [_bank("1000", TODAY)], {}, TODAY)
    assert nw.market == total and total.paise > 0
    assert len(nw.unpriced) == len(unpriced) > 0
    (note,) = [n for n in nw.notes if n.kind == "unpriced"]
    n = len(unpriced)
    assert note.text.startswith(f"{n} market holding")
    assert note.text.endswith("leaves it out." if n == 1 else "leaves them out.")
    assert nw.owned == total + rs("1000")


def test_verdicts_stay_measured_against_market_holdings_only(data: Path) -> None:
    """Owner, 2026-11-17: adding a bank balance must not change any verdict."""
    before = runner.invoke(cli, ["verdicts", "--json"])
    r = runner.invoke(cli, ["asset", "add", "bank", "--value", "99999999"])
    assert r.exit_code == 0, r.output
    assert runner.invoke(cli, ["verdicts", "--json"]).output == before.output


# ------------------------------------------------------------------ store


def test_the_store_keeps_history_replaces_a_same_day_write_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "assets.db"
    with AssetStore.open(path) as store:
        store.put(_bank("100", date(2026, 10, 1)))
        store.put(_bank("200", date(2026, 11, 1)))
        store.put(_bank("250", date(2026, 11, 1)))  # same day: a correction
        store.put(_bank("250", date(2026, 11, 1)))  # again: nothing changes
        store.set_targets({"equity": Bps(7000), "cash": Bps(3000)})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with AssetStore.open(path) as store:
        assert [e.amount for e in store.entries() if isinstance(e, Stated)] == [
            rs("100"),
            rs("250"),
        ]
        assert store.targets() == {"cash": 3000, "equity": 7000}
        store.set_targets({})
        assert store.targets() == {}


def test_readonly_open_never_creates_the_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        AssetStore.open(tmp_path / "assets.db", readonly=True)
    assert not (tmp_path / "assets.db").exists()


def _seeded(path: Path) -> None:
    with AssetStore.open(path) as store:
        store.put(_bank("100", date(2026, 10, 1)))
        store.put(_fd("200000", 710, date(2026, 4, 1), 12))
        store.put(Gold(label="g", as_of=TODAY, grams=Qty.parse("2"), rate=Price.rupees("7000")))
        store.set_targets({"equity": Bps(10_000)})


@pytest.mark.parametrize(
    "damage",
    [
        "text",
        "directory",
        "bad-json",
        "key-mismatch",
        "wrong-type",
        "bad-date",
        "targets-sum",
        "targets-bucket",
        "targets-text",
        "zero-schema",
    ],
)
def test_a_damaged_assets_file_is_refused_not_misread(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "assets.db"
    if damage == "text":
        path.write_bytes(b"not a database, just some words long enough to matter")
    elif damage == "directory":
        path.mkdir()
    else:
        _seeded(path)
        sql = {
            "bad-json": "UPDATE entries SET body = '{\"entry\": 1}' WHERE kind = 'bank'",
            "key-mismatch": "UPDATE entries SET key = 'someone else' WHERE kind = 'bank'",
            "wrong-type": "UPDATE entries SET body = replace(body, '\"paise\":10000', "
            "'\"paise\":\"10000\"') WHERE kind = 'bank'",
            "bad-date": "UPDATE entries SET as_of = '2026-02-30' WHERE kind = 'bank'",
            "targets-sum": "UPDATE targets SET bps = 9000",
            "targets-bucket": "UPDATE targets SET bucket = 'crypto'",
            "targets-text": "UPDATE targets SET bps = 'lots'",
            "zero-schema": "DROP TABLE entries",
        }[damage]
        with sqlite3.connect(path) as c:
            c.execute(sql)
        c.close()
        if damage == "zero-schema":
            with pytest.raises(AssetsDamaged):
                AssetStore.open(path, readonly=True)
            return
    with pytest.raises(AssetsDamaged):
        AssetStore.open(path)
    if damage not in ("directory",):
        with pytest.raises(AssetsDamaged):
            AssetStore.open(path, readonly=True)


def test_random_byte_damage_is_refused_or_reads_back_valid_entries(tmp_path: Path) -> None:
    """A seeded byte-flip fuzz of a real file: every copy either opens and
    every entry it gives is a valid one, or is refused as damaged. Nothing
    else (no other exception) may come out."""
    clean = tmp_path / "clean.db"
    _seeded(clean)
    raw = clean.read_bytes()
    rng = random.Random(20261122)
    refused = 0
    for i in range(300):
        body = bytearray(raw)
        for _ in range(rng.randint(1, 4)):
            body[rng.randrange(len(body))] = rng.randrange(256)
        path = tmp_path / f"d{i}.db"
        path.write_bytes(bytes(body))
        try:
            with AssetStore.open(path) as store:
                found = store.entries()
                store.targets()
            networth(None, PriceBook(quotes={}), found, {}, TODAY)
        except AssetsDamaged:
            refused += 1
    assert refused > 0  # the fuzz did reach the rows


def test_a_bug_while_reading_is_not_called_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seeded(tmp_path / "assets.db")

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("a bug, not damage")

    monkeypatch.setattr("stockin.networth.label_key", broken)
    with pytest.raises(RuntimeError):
        AssetStore.open(tmp_path / "assets.db")


# -------------------------------------------------------------------- CLI


@pytest.fixture
def empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    monkeypatch.setattr("stockin.cli._today", lambda: TODAY)
    return tmp_path


def _ok(*args: str) -> str:
    r = runner.invoke(cli, list(args))
    assert r.exit_code == 0, r.output
    return r.output


def test_the_cli_adds_lists_closes_and_totals_without_any_cas(empty: Path) -> None:
    assert "₹2,50,000.00 today" in _ok(
        "asset",
        "add",
        "bank",
        "--label",
        "SBI savings",
        "--value",
        "250000",
        "--as-of",
        "2026-11-01",
    )
    _ok("asset", "add", "epf", "--value", "8,45,000", "--as-of", "2026-10-31")
    _ok(
        "asset", "add", "fd", "--label", "HDFC FD", "--principal", "100000", "--rate", "7.1",
        "--start", "2025-11-22", "--months", "12",
    )  # fmt: skip
    _ok("asset", "add", "gold", "--grams", "20", "--rate-per-gram", "7650")
    _ok("asset", "add", "loan", "--label", "Car loan", "--value", "100000")
    listed = _ok("asset", "list")
    assert "SBI savings" in listed and "₹1,07,291.28" in listed  # the FD matured today
    _ok("target", "set", "equity=0", "retirement=60", "cash=20", "debt=10", "gold=10")

    nw = json.loads(_ok("networth", "--json"))
    owned = 250000_00 + 845000_00 + 107291_28 + 153000_00
    assert nw["owned"]["paise"] == owned and nw["owed"]["paise"] == 100000_00
    assert nw["total"]["paise"] == owned - 100000_00
    assert sum(s["bps"] for s in nw["slices"]) == 10_000
    text = _ok("networth")
    assert text.startswith(f"Net worth ₹{Money(paise=owned - 100000_00)}")
    assert "matured on 2026-11-22" in text

    assert "history is kept" in _ok("asset", "close", "loan", "--label", "car LOAN")
    assert json.loads(_ok("networth", "--json"))["owed"]["paise"] == 0
    assert stat.S_IMODE(os.stat(empty / "assets.db").st_mode) == 0o600


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["asset", "add", "crypto", "--value", "1"], "is not a kind"),
        (["asset", "add", "bank"], "--value is required"),
        (["asset", "add", "bank", "--value", "1", "--grams", "2"], "--grams does not apply"),
        (["asset", "add", "bank", "--value", "1.234"], "more than 2 decimal places"),
        (["asset", "add", "bank", "--value", "-5"], "cannot be negative"),
        (["asset", "add", "bank", "--value", "1", "--as-of", "2026-13-01"], "not a date"),
        (["asset", "add", "bank", "--value", "1", "--as-of", "2026-11-23"], "in the future"),
        (["asset", "add", "fd", "--principal", "1"], "an FD needs"),
        (
            [
                "asset",
                "add",
                "fd",
                "--principal",
                "1",
                "--rate",
                "x",
                "--start",
                "2026-01-01",
                "--months",
                "12",
            ],
            "not a decimal number",
        ),
        (["asset", "add", "gold", "--grams", "1"], "gold needs"),
        (["asset", "add", "bank", "--label", "  ", "--value", "1"], "label"),
        (["asset", "close", "bank", "--label", "nope"], "no bank called"),
        (["target", "set", "equity=50"], "add to 50.0%"),
    ],
)
def test_the_cli_refuses_what_it_cannot_save(empty: Path, args: list[str], said: str) -> None:
    r = runner.invoke(cli, args)
    assert r.exit_code == 2, r.output
    assert said in r.output and "Traceback" not in r.output
    if (empty / "assets.db").exists():
        with AssetStore.open(empty / "assets.db") as store:
            assert store.entries() == () and store.targets() == {}


def test_the_cli_says_a_damaged_assets_file_without_a_traceback(empty: Path) -> None:
    (empty / "assets.db").write_bytes(b"not a database, just some words long enough to matter")
    for args in (["networth"], ["asset", "list"], ["asset", "add", "bank", "--value", "1"]):
        r = runner.invoke(cli, args)
        assert r.exit_code == 2 and "only copy of what you typed" in r.output, r.output
    doctor = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    (check,) = [c for c in doctor["checks"] if c["name"] == "assets"]
    assert check["status"] == "fail"


def test_doctor_reports_assets_without_creating_the_file(empty: Path) -> None:
    doctor = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    (check,) = [c for c in doctor["checks"] if c["name"] == "assets"]
    assert check["status"] == "warn" and not (empty / "assets.db").exists()
    _ok("asset", "add", "bank", "--value", "1")
    doctor = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    (check,) = [c for c in doctor["checks"] if c["name"] == "assets"]
    assert check["status"] == "ok" and "1 entries" in check["detail"]


# ------------------------------------------- verifier round 1 (PR #29)


def test_a_matured_fd_closes_on_its_maturity_day(empty: Path) -> None:
    """R1: the FD's entry is dated the day it opened, so closing it on the
    day it matured (what the matured note asks for) takes it out."""
    _ok(
        "asset", "add", "fd", "--label", "HDFC FD", "--principal", "100000", "--rate", "7.1",
        "--start", "2025-11-01", "--months", "12",
    )  # fmt: skip
    _ok("asset", "close", "fd", "--label", "HDFC FD", "--on", "2026-11-01")
    nw = json.loads(_ok("networth", "--json"))
    assert nw["owned"]["paise"] == 0 and not any(n["kind"] == "matured" for n in nw["notes"])


def test_closing_before_the_latest_entry_is_refused_not_ignored(empty: Path) -> None:
    _ok("asset", "add", "bank", "--value", "100", "--as-of", "2026-11-10")
    r = runner.invoke(cli, ["asset", "close", "bank", "--on", "2026-11-01"])
    assert r.exit_code == 2 and "dated 2026-11-10, after 2026-11-01" in r.output
    assert "--on 2026-11-10" in r.output
    assert json.loads(_ok("networth", "--json"))["owned"]["paise"] == 100_00


def test_closing_on_the_day_of_a_balance_keeps_that_balance_as_history(empty: Path) -> None:
    """R3: a close and a value can share a day; the close wins, the value stays."""
    _ok("asset", "add", "loan", "--value", "5000")
    _ok("asset", "close", "loan")
    with AssetStore.open(empty / "assets.db") as store:
        kinds = sorted(type(e).__name__ for e in store.entries())
    assert kinds == ["Closed", "Stated"]
    assert json.loads(_ok("networth", "--json"))["owed"]["paise"] == 0
    # and a correction of that day's value still replaces only the value
    _ok("asset", "add", "loan", "--value", "6000")
    with AssetStore.open(empty / "assets.db") as store:
        assert len(store.entries()) == 2


@pytest.mark.parametrize("offset", [0, 16, 28, 40, 56, 96, 100, 104])
def test_one_zeroed_header_byte_is_damage_never_an_empty_file(tmp_path: Path, offset: int) -> None:
    """R2 (lesson 21): a zeroed byte at 104 made SQLite read an empty schema,
    and a read command wrote a new one, losing every entry for good."""
    path = tmp_path / "assets.db"
    _seeded(path)
    body = bytearray(path.read_bytes())
    body[offset] = 0 if body[offset] else 1
    path.write_bytes(bytes(body))
    before = path.read_bytes()
    try:
        with AssetStore.open(path) as store:
            assert len(store.entries()) == 3  # a harmless byte: everything still there
    except AssetsDamaged:
        assert path.read_bytes() == before  # refused, and nothing written


def test_byte_104_zeroed_is_refused_by_every_command(empty: Path) -> None:
    _seeded(empty / "assets.db")
    body = bytearray((empty / "assets.db").read_bytes())
    body[104] = 0
    (empty / "assets.db").write_bytes(bytes(body))
    before = (empty / "assets.db").read_bytes()
    for args in (["asset", "list"], ["networth"], ["asset", "add", "bank", "--value", "1"]):
        r = runner.invoke(cli, args)
        assert r.exit_code == 2 and "only copy of what you typed" in r.output, (args, r.output)
    assert (empty / "assets.db").read_bytes() == before
    doctor = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    assert [c["status"] for c in doctor["checks"] if c["name"] == "assets"] == ["fail"]


def test_a_zero_byte_file_is_new_for_doctor_and_commands_alike(empty: Path) -> None:
    """C4: zero bytes means new, everywhere."""
    (empty / "assets.db").write_bytes(b"")
    doctor = json.loads(runner.invoke(cli, ["doctor", "--json"]).output)
    assert [c["status"] for c in doctor["checks"] if c["name"] == "assets"] == ["warn"]
    assert "nothing yet" in _ok("asset", "list")


def test_an_extra_or_missing_table_is_damage(tmp_path: Path) -> None:
    for sql in ("CREATE TABLE extra(x)", "DROP TABLE targets"):
        path = tmp_path / f"{len(sql)}.db"
        _seeded(path)
        with sqlite3.connect(path) as c:
            c.execute(sql)
        c.close()
        with pytest.raises(AssetsDamaged):
            AssetStore.open(path)


def test_doctor_opens_assets_read_only(empty: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V8: doctor must open with mode=ro, not merely happen not to write."""
    _seeded(empty / "assets.db")
    seen: list[str] = []
    real = sqlite3.connect

    def spy(target: object, *a: object, **k: object) -> sqlite3.Connection:
        seen.append(str(target))
        return real(target, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr("stockin.networth.sqlite3.connect", spy)
    runner.invoke(cli, ["doctor", "--json"])
    assert seen and all(t.endswith("?mode=ro") for t in seen if "assets.db" in t)


def test_drift_inside_the_shown_band_is_not_called_out() -> None:
    """R4: 25.03% against 20% shows as 5.0 points, so it is on target."""
    entries: list[Entry] = [
        _bank("2503", TODAY),
        Stated(kind="other", label="x", as_of=TODAY, amount=rs("7497")),
    ]
    nw = networth(
        None, PriceBook(quotes={}), entries, {"cash": Bps(2000), "other": Bps(8000)}, TODAY
    )
    assert {s.bucket: s.drift for s in nw.slices}["cash"] == 503
    assert not [n for n in nw.notes if n.kind == "drift"]
    assert [n.kind for n in nw.notes if n.tone == "good"] == ["on_target"]


def test_an_interest_rate_above_50_percent_is_refused_in_percent(empty: Path) -> None:
    r = runner.invoke(
        cli,
        ["asset", "add", "fd", "--principal", "1", "--rate", "51", "--start", "2026-01-01",
         "--months", "12"],
    )  # fmt: skip
    assert r.exit_code == 2 and "above 50%" in r.output and "5000" not in r.output


def test_dates_far_outside_a_lifetime_are_refused() -> None:
    """C1/C2: year 9999 made the month arithmetic loop forever."""
    with pytest.raises(ValueError, match="outside 1900 to 2199"):
        _fd("1000", 700, date(9999, 1, 1), 600)
    with pytest.raises(ValueError, match="outside 1900 to 2199"):
        _bank("1", date(1, 1, 1))
    far = _fd("1000", 700, date(2199, 12, 31), 600)  # maturity in 2249: still computes
    assert fd_value(far, date(2260, 1, 1)).paise > 1000_00


def test_a_future_fd_start_is_refused(empty: Path) -> None:
    r = runner.invoke(
        cli,
        ["asset", "add", "fd", "--principal", "1", "--rate", "7", "--start", "2026-12-01",
         "--months", "12"],
    )  # fmt: skip
    assert r.exit_code == 2 and "in the future" in r.output


def test_a_six_month_fd_compounds_and_a_five_month_one_does_not() -> None:
    """V1: the boundary is 6 months, compounding included."""
    six = _fd("100000", 800, date(2026, 1, 1), 6)
    assert fd_value(six, date(2026, 7, 1)) == rs("104040")  # 1e5 x 1.02^2
    five = _fd("100000", 800, date(2026, 1, 1), 5)
    days = (date(2026, 6, 1) - date(2026, 1, 1)).days
    want = (Decimal("100000") * (1 + Decimal("0.08") * days / 365)).quantize(Decimal("0.01"))
    assert fd_value(five, date(2026, 6, 1)) == rs(str(want))


def test_labels_fold_case_fully() -> None:
    """V6: casefold, not lower: 'Straße' and 'STRASSE' are one entry."""
    entries: list[Entry] = [
        _bank("1", date(2026, 1, 1), label="Straße"),
        _bank("2", date(2026, 2, 1), label="STRASSE"),
    ]
    assert len(current(entries, TODAY)) == 1


def test_loans_are_a_warning_only_above_half_of_what_you_own() -> None:
    """V7."""

    def tone(loan: str) -> str:
        es: list[Entry] = [
            _bank("100", TODAY),
            Stated(kind="loan", label="L", as_of=TODAY, amount=rs(loan)),
        ]
        nw = networth(None, PriceBook(quotes={}), es, {}, TODAY)
        return next(n.tone for n in nw.notes if n.kind == "loans")

    assert (tone("10"), tone("50"), tone("51")) == ("info", "info", "warn")


def test_the_cli_prints_drift_with_its_sign(empty: Path) -> None:
    """V3: the human output's sign follows weight minus target."""
    _ok("asset", "add", "bank", "--value", "70")
    _ok("asset", "add", "other", "--value", "30")
    _ok("target", "set", "cash=50", "other=50")
    text = _ok("networth")
    assert "drift +20 pts" in text and "drift -20 pts" in text
    cash = next(ln for ln in text.splitlines() if ln.strip().startswith("Cash"))
    assert "+20" in cash


# ------------------------------------------- verifier round 2 (PR #29)
# Committed red before the fix, per the two-FAIL rule (PIPELINE row 16).


def test_a_value_added_on_the_day_of_a_close_counts(empty: Path) -> None:
    """R5: an FD closed at maturity and renewed under the same name that day,
    or a balance re-entered after a mistaken close, must count. 'saved ...
    today' must never be printed for a value that is then left out."""
    _ok(
        "asset", "add", "fd", "--label", "HDFC FD", "--principal", "100000", "--rate", "7.1",
        "--start", "2025-11-01", "--months", "12",
    )  # fmt: skip
    _ok("asset", "close", "fd", "--label", "HDFC FD", "--on", "2026-11-01")
    _ok(
        "asset", "add", "fd", "--label", "HDFC FD", "--principal", "107000", "--rate", "7.0",
        "--start", "2026-11-01", "--months", "12",
    )  # fmt: skip
    nw = json.loads(_ok("networth", "--json"))
    assert nw["owned"]["paise"] > 107000_00

    _ok("asset", "add", "bank", "--value", "500")
    _ok("asset", "close", "bank")
    _ok("asset", "add", "bank", "--value", "500")  # the close was a mistake
    nw = json.loads(_ok("networth", "--json"))
    assert any(ln["kind"] == "bank" and ln["value"]["paise"] == 500_00 for ln in nw["lines"])


def test_the_cli_drift_column_never_contradicts_the_notes(empty: Path) -> None:
    """R6: 25.04% against 20% must not print '+5.04 pts' above a note that
    says every class is within 5.0 points; the column shows the same tenths."""
    _ok("asset", "add", "bank", "--value", "2504")
    _ok("asset", "add", "other", "--value", "7496")
    _ok("target", "set", "cash=20", "other=80")
    text = _ok("networth")
    assert "within 5.0 points" in text
    cash = next(ln for ln in text.splitlines() if ln.strip().startswith("Cash"))
    assert "drift +5.0 pts" in cash, cash


def test_adding_an_fd_says_the_date_it_is_stored_under(empty: Path) -> None:
    """R7: without --as-of the FD is stored as of its start; say that date."""
    out = _ok(
        "asset", "add", "fd", "--label", "X", "--principal", "1000", "--rate", "7",
        "--start", "2026-01-15", "--months", "12",
    )  # fmt: skip
    assert "as of 2026-01-15" in out and f"as of {TODAY}" not in out


@pytest.mark.parametrize(
    "extra",
    [
        "CREATE TRIGGER t AFTER DELETE ON targets BEGIN DELETE FROM entries; END",
        "CREATE INDEX i ON entries(as_of)",
        "CREATE VIEW v AS SELECT * FROM entries",
    ],
)
def test_any_schema_object_beyond_the_two_tables_is_damage(tmp_path: Path, extra: str) -> None:
    """C5: a trigger (or index, or view) can change what a write does."""
    path = tmp_path / "assets.db"
    _seeded(path)
    with sqlite3.connect(path) as c:
        c.execute(extra)
    c.close()
    with pytest.raises(AssetsDamaged):
        AssetStore.open(path)


def test_a_gap_that_shows_as_zero_has_no_sign() -> None:
    """Round 3 N4/V6: 19.96% against 20% shows 0.0 points, so neither + nor -."""
    assert _drift(1996, 2000) == "±0.0"
    assert _drift(2000, 2000) == "±0"
    assert _drift(2004, 2000) == "±0.0"
    assert _drift(2050, 2000) == "+0.5"
    assert _drift(1950, 2000) == "-0.5"
