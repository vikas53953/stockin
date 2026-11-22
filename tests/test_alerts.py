"""Alerts: the transition rule as properties, the store's one-transaction
record and outbox, and the CLI on the same fixtures `verdicts` uses."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.alerts import ACTIONABLE, AlertsDamaged, AlertStore, changes, text
from stockin.cli import app as cli
from stockin.facts import Isin, parse_isin
from stockin.thesis import Call, Verdict

FIX = Path(__file__).parent / "fixtures"
INFY = parse_isin("INE009A01021")
runner = CliRunner()
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

ISINS = [parse_isin(i) for i in ("INE009A01021", "INF179K01BE2", "INE002A01018", "INE040A01034")]
CALLS: list[Call] = ["add", "trim", "exit", "hold", "review"]


def v(isin: Isin, call: Call, because: str = "") -> Verdict:
    return Verdict(
        isin=isin,
        name=f"name of {isin}",
        call=call,
        because=because or f"{isin} says {call}",
        weight=None,
        target=None,
    )


snapshot = st.dictionaries(st.sampled_from(ISINS), st.sampled_from(CALLS)).map(
    lambda d: [v(i, c) for i, c in d.items()]
)
remembered = st.dictionaries(st.sampled_from(ISINS), st.sampled_from(CALLS))


# --------------------------------------------------------------- the rule


@given(remembered, snapshot)
def test_an_alert_is_exactly_a_turn_into_add_trim_or_exit(
    before: dict[Isin, Call], now: list[Verdict]
) -> None:
    alerts, after = changes(before, now)
    expected = {x.isin for x in now if x.call in ACTIONABLE and before.get(x.isin) != x.call}
    assert {a.isin for a in alerts} == expected
    assert len(alerts) == len(expected)  # one per holding, never two
    by = {x.isin: x for x in now}
    for a in alerts:  # the alert carries the verdict's own call and sentence
        assert (a.call, a.because, a.name) == (by[a.isin].call, by[a.isin].because, by[a.isin].name)
    assert after == {x.isin: x.call for x in now}  # sold holdings are forgotten


@given(remembered, snapshot)
def test_the_same_numbers_twice_alert_nothing_the_second_time(
    before: dict[Isin, Call], now: list[Verdict]
) -> None:
    _, after = changes(before, now)
    again, after2 = changes(after, now)
    assert again == () and after2 == after


@given(remembered, snapshot, st.randoms())
def test_verdict_order_never_changes_the_alerts(
    before: dict[Isin, Call], now: list[Verdict], rnd: object
) -> None:
    shuffled = list(now)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert changes(before, now) == changes(before, shuffled)


@given(st.lists(snapshot, max_size=12))
def test_over_any_run_of_checks_each_alert_marks_a_change_and_none_repeats(
    runs: list[list[Verdict]],
) -> None:
    """Walk a history of checks. Per holding, the alerts sent equal the
    number of times its call became actionable and differed from the call
    before (a missing holding has no call), and the same call never alerts
    twice in a row."""
    state: dict[Isin, Call] = {}
    sent: dict[Isin, list[Call]] = {}
    prev: dict[Isin, Call | None] = {}
    want: dict[Isin, int] = {}
    for now in runs:
        alerts, state = changes(state, now)
        calls: dict[Isin, Call] = {x.isin: x.call for x in now}
        for isin in ISINS:
            c = calls.get(isin)
            if c is not None and c in ACTIONABLE and prev.get(isin) != c:
                want[isin] = want.get(isin, 0) + 1
            prev[isin] = c
        for a in alerts:
            sent.setdefault(a.isin, []).append(a.call)
    for isin in ISINS:
        assert len(sent.get(isin, [])) == want.get(isin, 0)


def test_hold_re_arms_a_trim() -> None:
    a1, s = changes({}, [v(INFY, "trim")])
    a2, s = changes(s, [v(INFY, "trim")])
    a3, s = changes(s, [v(INFY, "hold")])
    a4, s = changes(s, [v(INFY, "trim")])
    assert (len(a1), len(a2), len(a3), len(a4)) == (1, 0, 0, 1)


def test_trim_to_exit_alerts_again_and_review_never_alerts() -> None:
    a1, s = changes({}, [v(INFY, "trim")])
    a2, s = changes(s, [v(INFY, "exit")])
    a3, _ = changes(s, [v(INFY, "review")])
    assert [a.call for a in a1 + a2] == ["trim", "exit"] and a3 == ()


def test_two_verdicts_for_one_holding_are_refused() -> None:
    with pytest.raises(ValueError, match="two verdicts"):
        changes({}, [v(INFY, "trim"), v(INFY, "add")])


def test_the_message_is_the_verdicts_sentence() -> None:
    (a,), _ = changes({}, [v(INFY, "trim", "INFOSYS is 31.2% of your money, ...")])
    assert text(a).splitlines()[:2] == [
        f"TRIM name of {INFY}",
        "INFOSYS is 31.2% of your money, ...",
    ]
    assert "not advice" in text(a)


# --------------------------------------------------------------- the store


def test_store_is_private_and_records_calls_and_alerts_together(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    with AlertStore.open(path) as store:
        added = store.record([v(INFY, "trim"), v(ISINS[1], "hold")], made=T0)
        assert [s.alert.call for s in added] == ["trim"] and added[0].sent is None
        assert store.record([v(INFY, "trim"), v(ISINS[1], "hold")], made=T0) == ()
        assert store.calls() == {INFY: "trim", ISINS[1]: "hold"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with AlertStore.open(path) as store:  # survives a reopen
        assert store.outbox() == added
        assert store.outbox(pending_only=True) == added
        store.mark_sent(added[0].id, T0)
        store.mark_sent(added[0].id, datetime(2030, 1, 1, tzinfo=UTC))  # first send stands
        (kept,) = store.outbox()
        assert kept.sent == T0 and store.outbox(pending_only=True) == ()


def test_a_failed_record_keeps_neither_the_calls_nor_the_alerts(tmp_path: Path) -> None:
    with AlertStore.open(tmp_path / "alerts.db") as store:
        store.record([v(INFY, "hold")], made=T0)
        with pytest.raises(ValueError):
            store.record([v(INFY, "trim"), v(INFY, "exit")], made=T0)
        assert store.calls() == {INFY: "hold"} and store.outbox() == ()


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO calls VALUES ('INE009A01021', 'panic')",
        "INSERT INTO calls VALUES ('not-an-isin', 'trim')",
        "INSERT INTO calls VALUES ('ine009a01021', 'trim')",
        "INSERT INTO outbox(made, body) VALUES ('2026-11-20T10:00:00+00:00', '{not json')",
        "INSERT INTO outbox(made, body) VALUES ('yesterday', '{}')",
        "DROP TABLE outbox; CREATE TABLE outbox(id INTEGER PRIMARY KEY)",
    ],
)
def test_a_damaged_row_is_said_not_raised(tmp_path: Path, sql: str) -> None:
    path = tmp_path / "alerts.db"
    AlertStore.open(path).close()
    with sqlite3.connect(path) as conn:
        conn.executescript(sql)
    with pytest.raises(AlertsDamaged):
        AlertStore.open(path)


def test_a_file_that_is_not_a_database_is_said_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "alerts.db"
    path.write_bytes(b"not a database, just some words long enough to matter")
    with pytest.raises(AlertsDamaged):
        AlertStore.open(path)


# --------------------------------------------------------------- the CLI


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


def _check() -> list[dict[str, object]]:
    r = runner.invoke(cli, ["alerts", "check", "--json"])
    assert r.exit_code == 0, r.output
    return [s["alert"] for s in json.loads(r.output)["alerts"]]


def test_cli_alerts_once_per_turn_and_lists_them(data: Path) -> None:
    assert _check() == []  # every verdict is review: no thesis yet

    r = runner.invoke(cli, ["thesis", "set", INFY, "--why", "cash machine", "--target", "30"])
    assert r.exit_code == 0, r.output
    (a,) = _check()
    verdicts = json.loads(runner.invoke(cli, ["verdicts", "--json"]).output)["verdicts"]
    infy = next(x for x in verdicts if x["isin"] == INFY)
    assert (a["isin"], a["call"], a["because"]) == (INFY, "trim", infy["because"])
    assert _check() == []  # same numbers, nothing new

    assert runner.invoke(cli, ["thesis", "broken", INFY]).exit_code == 0
    assert [x["call"] for x in _check()] == ["exit"]

    r = runner.invoke(cli, ["alerts", "list"])
    assert r.exit_code == 0
    lines = r.output.splitlines()
    assert len(lines) == 2 and "TRIM" in lines[0] and "EXIT" in lines[1]
    assert all("[pending]" in line for line in lines)


def test_cli_list_before_any_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    r = runner.invoke(cli, ["alerts", "list"])
    assert r.exit_code == 0 and "no alerts yet" in r.output


def test_cli_check_without_a_ledger_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    r = runner.invoke(cli, ["alerts", "check"])
    assert r.exit_code == 2 and "import a statement first" in r.output


def test_cli_says_a_damaged_alerts_file_without_a_traceback(data: Path) -> None:
    (data / "alerts.db").write_bytes(b"not a database, just some words long enough to matter")
    for args in (["alerts", "check"], ["alerts", "list"]):
        r = runner.invoke(cli, args)
        assert r.exit_code == 2 and "move alerts.db aside" in r.output
        assert "Traceback" not in r.output
