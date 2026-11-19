"""Your thesis and the verdict it gives: the rules as a property, the
sentence agreeing with its numbers, the store, the CLI and the web form."""

from __future__ import annotations

import json
import os
import stat
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.cli import app as cli
from stockin.facts import Isin, parse_isin
from stockin.insights import CONCENTRATED_ONE, Weight, pct, tenths_of
from stockin.money import Bps, Money
from stockin.thesis import (
    BAND,
    ThesesDamaged,
    Thesis,
    ThesisStore,
    parse_percent,
    verdict,
    verdicts,
)
from stockin.web import create_app

FIX = Path(__file__).parent / "fixtures"
INFY = parse_isin("INE009A01021")
HDFC = parse_isin("INF179K01BE2")
runner = CliRunner()
PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"


def _w(isin: Isin, bps: int) -> Weight:
    return Weight(
        isin=isin, name="X", group="Stocks", value=Money(paise=bps), bps=Bps(bps), cost=None
    )


def _t(isin: Isin, target: int | None = None, broken: bool = False, exit_if: str = "") -> Thesis:
    return Thesis(
        isin=isin,
        why="a reason",
        exit_if=exit_if,
        target=Bps(target) if target is not None else None,
        broken=broken,
        written=date(2026, 11, 18),
    )


def _p(bps: int) -> str:
    return pct(tenths_of(bps, 10_000))


def _expected(w: int | None, thesis: Thesis | None) -> str:
    """The rule written out once more, plainly, as the oracle."""
    if thesis is None:
        return "review"
    if thesis.broken:
        return "exit"
    if w is None:
        return "review"
    if thesis.target is not None:
        if w - thesis.target > BAND:
            return "trim"
        if thesis.target - w > BAND:
            return "add"
        return "hold"
    return "trim" if w > CONCENTRATED_ONE else "hold"


weights_ = st.none() | st.integers(0, 10_000)
theses_ = st.none() | st.builds(
    lambda target, broken: _t(INFY, target, broken),
    st.none() | st.integers(0, 10_000),
    st.booleans(),
)


@given(weights_, theses_)
def test_the_verdict_follows_your_rule_and_its_sentence_carries_its_numbers(
    w: int | None, thesis: Thesis | None
) -> None:
    v = verdict(INFY, "Infosys", _w(INFY, w) if w is not None else None, thesis)
    assert v.call == _expected(w, thesis)
    assert v.weight == w
    assert "Infosys" in v.because
    if v.call in ("trim", "add", "hold") and w is not None:
        assert _p(w) in v.because  # the weight said is the weight used
        if thesis is not None and thesis.target is not None:
            assert _p(thesis.target) in v.because
            assert f"{_p(BAND)[:-1]} points" in v.because  # points, not a relative %
            word = {"trim": "above", "add": "below", "hold": "within"}[v.call]
            assert word in v.because


@pytest.mark.parametrize(
    ("w", "call"),
    [(1500, "hold"), (1501, "trim"), (500, "hold"), (499, "add"), (1000, "hold")],
)
def test_the_band_is_five_points_inclusive(w: int, call: str) -> None:
    assert verdict(INFY, "I", _w(INFY, w), _t(INFY, 1000)).call == call


@pytest.mark.parametrize(("w", "call"), [(2500, "hold"), (2501, "trim")])
def test_without_a_target_the_25_percent_line_decides(w: int, call: str) -> None:
    assert verdict(INFY, "I", _w(INFY, w), _t(INFY)).call == call


def test_a_broken_thesis_says_exit_with_your_own_exit_condition() -> None:
    v = verdict(INFY, "I", _w(INFY, 100), _t(INFY, broken=True, exit_if="margins fall"))
    assert v.call == "exit"
    assert v.because.endswith("You said you would exit if: margins fall.")
    # a broken thesis says exit even with no price
    assert verdict(INFY, "I", None, _t(INFY, broken=True)).call == "exit"


def test_verdicts_that_ask_for_action_come_first() -> None:
    names = {INFY: "Infosys", HDFC: "HDFC"}
    vs = verdicts(names, (_w(INFY, 6000), _w(HDFC, 4000)), {INFY: _t(INFY, 6000)})
    assert [(v.isin, v.call) for v in vs] == [(HDFC, "review"), (INFY, "hold")]
    vs = verdicts(names, (_w(INFY, 6000), _w(HDFC, 4000)), {HDFC: _t(HDFC, broken=True)})
    assert [v.call for v in vs] == ["exit", "review"]


@given(st.integers(0, 10_000))
def test_a_percent_reads_back_exactly(bps: int) -> None:
    whole, frac = divmod(bps, 100)
    for text in (f"{whole}.{frac:02d}", f"{whole}.{frac:02d}%", f" {whole}.{frac:02d} % "):
        assert parse_percent(text) == bps
    if frac == 0:
        assert parse_percent(str(whole)) == bps
    if frac % 10 == 0:
        assert parse_percent(f"{whole}.{frac // 10}") == bps


@pytest.mark.parametrize("bad", ["", "abc", "-1", "100.01", "101", "1.234", "1e2", "12,5", "nan"])
def test_a_percent_that_is_not_one_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match=r"percent|above 100%"):
        parse_percent(bad)


# ----------------------------------------------------------------- store


def test_the_store_keeps_what_you_wrote_privately(tmp_path: Path) -> None:
    path = tmp_path / "theses.db"
    with ThesisStore.open(path) as store:
        store.put(_t(INFY, 1000))
        store.put(_t(INFY, 2000, exit_if="new"))  # rewriting replaces
        store.put(_t(HDFC))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with ThesisStore.open(path) as store:
        found = store.all()
    assert found == {INFY: _t(INFY, 2000, exit_if="new"), HDFC: _t(HDFC)}


@pytest.mark.parametrize("damage", ["text", "foreign-row", "directory"])
def test_a_damaged_theses_file_is_refused_not_misread(tmp_path: Path, damage: str) -> None:
    import sqlite3

    path = tmp_path / "theses.db"
    if damage == "text":
        path.write_bytes(b"not a database, just some words long enough to matter")
    elif damage == "directory":
        path.mkdir()
    else:
        with ThesisStore.open(path):
            pass
        with sqlite3.connect(path) as c:
            c.execute("INSERT INTO theses VALUES ('INE009A01021', '{\"why\": 1}')")
    with pytest.raises(ThesesDamaged):
        ThesisStore.open(path)


# ------------------------------------------------------------ CLI and web


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)])
        assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["prices", "import", str(FIX / "eod" / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    return tmp_path


def _verdicts_json() -> dict[str, dict[str, object]]:
    r = runner.invoke(cli, ["verdicts", "--json"])
    assert r.exit_code == 0, r.output
    return {v["isin"]: v for v in json.loads(r.output)["verdicts"]}


def test_cli_thesis_set_broken_and_verdicts(data: Path) -> None:
    assert {v["call"] for v in _verdicts_json().values()} == {"review"}

    r = runner.invoke(cli, ["thesis", "set", INFY, "--why", "cash machine", "--target", "30"])
    assert r.exit_code == 0, r.output
    v = _verdicts_json()[INFY]
    # Infosys is the only priced holding, so it is all of the priced money
    assert (v["call"], v["weight"], v["target"]) == ("trim", 10_000, 3000)
    assert "your priced money" in str(v["because"])

    r = runner.invoke(cli, ["thesis", "broken", INFY])
    assert r.exit_code == 0 and _verdicts_json()[INFY]["call"] == "exit"
    r = runner.invoke(cli, ["thesis", "broken", INFY, "--undo"])
    assert r.exit_code == 0 and _verdicts_json()[INFY]["call"] == "trim"
    assert stat.S_IMODE(os.stat(data / "theses.db").st_mode) == 0o600

    text = runner.invoke(cli, ["verdicts"]).output
    assert text.splitlines()[0].startswith("TRIM") and "not advice" in text


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["thesis", "set", "INE000000000", "--why", "x"], "not in your holdings"),
        (["thesis", "set", INFY, "--why", "  "], "--why is empty"),
        (["thesis", "set", INFY, "--why", "x", "--target", "120"], "above 100%"),
        (["thesis", "set", INFY, "--why", "x", "--target", "ten"], "not a percent"),
        (["thesis", "broken", INFY], "no thesis for"),
    ],
)
def test_cli_refuses_what_it_cannot_save(data: Path, args: list[str], said: str) -> None:
    r = runner.invoke(cli, args)
    assert r.exit_code == 2
    assert said in r.output
    assert not (data / "theses.db").exists() or not ThesisStore.open(data / "theses.db").all()


def test_cli_says_a_damaged_theses_file_without_a_traceback(data: Path) -> None:
    (data / "theses.db").write_bytes(b"not a database, just some words long enough to matter")
    r = runner.invoke(cli, ["verdicts"])
    assert r.exit_code == 2 and "only copy of what you wrote" in r.output


@pytest.fixture
def web(data: Path) -> TestClient:
    return TestClient(create_app(data / "ledger.db", PORT), base_url=HOST)


def test_web_form_saves_your_thesis_and_home_shows_the_verdict(web: TestClient) -> None:
    home = web.get("/").text
    assert 'id="verdicts"' in home and "Write thesis" in home and "? review" in home

    form = web.get(f"/thesis/{INFY}").text
    assert 'name="why"' in form and "INFOSYS" in form

    r = web.post(
        f"/thesis/{INFY}",
        data={"why": "cash machine", "target": "12.5", "exit_if": "margins fall"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/#verdicts"
    home = web.get("/").text
    v = _verdicts_json()[INFY]
    assert v["call"] == "trim" and v["target"] == 1250
    assert str(v["because"]).replace("'", "&#39;") in home  # page and CLI say the same
    assert "▼ trim" in home and "Why you hold it: cash machine" in home
    assert 'value="12.50"' in web.get(f"/thesis/{INFY}").text  # reads back what you saved

    web.post(f"/thesis/{INFY}", data={"why": "cash machine", "broken": "on"})
    assert "✕ exit" in web.get("/").text


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"why": " "}, "Write why you hold it first."),
        ({"why": "w", "target": "x"}, "not a percent"),
    ],
)
def test_web_form_keeps_what_you_typed_when_it_cannot_save(
    web: TestClient, data: Path, fields: dict[str, str], said: str
) -> None:
    r = web.post(f"/thesis/{INFY}", data=fields)
    assert r.status_code == 200 and said in r.text
    if fields.get("target"):
        assert f'value="{fields["target"]}"' in r.text
    assert not ThesisStore.open(data / "theses.db").all()


def test_web_refuses_a_thesis_for_what_you_do_not_hold_or_from_another_site(
    web: TestClient, data: Path
) -> None:
    assert web.get("/thesis/INE000000000").status_code == 404
    assert web.post("/thesis/INE000000000", data={"why": "x"}).status_code == 404
    r = web.post(f"/thesis/{INFY}", data={"why": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert not (data / "theses.db").exists() or not ThesisStore.open(data / "theses.db").all()


def test_web_says_a_damaged_theses_file_and_still_shows_holdings(
    web: TestClient, data: Path
) -> None:
    (data / "theses.db").write_bytes(b"not a database, just some words long enough to matter")
    r = web.get("/")
    assert r.status_code == 200
    assert "Your theses could not be read" in r.text and "INFOSYS LIMITED" in r.text
    assert "could not be read" in web.get(f"/thesis/{INFY}").text
