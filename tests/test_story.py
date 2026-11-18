"""The home screen's story: the map's geometry, cards that agree with their
insight, and the page itself through HTTP with synthetic statements."""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from stockin.cli import app as cli
from stockin.facts import parse_isin
from stockin.insights import Insight, Point, Weight, pct, tenths_of
from stockin.money import Money
from stockin.story import GROUP_SLOT, MAP_H, MAP_W, MAX_CARDS, card, squarify, story, tiles
from stockin.web import create_app

FIX = Path(__file__).parent / "fixtures"
EOD = FIX / "eod"
runner = CliRunner()
PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"

values = st.lists(st.integers(1, 10**12), min_size=1, max_size=40).map(
    lambda v: sorted(v, reverse=True)
)


@given(values)
def test_the_map_fills_its_box_with_areas_in_proportion_and_no_overlap(vs: list[int]) -> None:
    rects = squarify(vs, MAP_W, MAP_H)
    assert len(rects) == len(vs)
    total, box = sum(vs), MAP_W * MAP_H
    # floating point: a ten-thousandth of a percent of the box is invisible
    tol = 1e-4
    eps = 1e-6 * box
    for v, (x, y, w, h) in zip(vs, rects, strict=True):
        assert w >= 0 and h >= 0
        assert x >= -tol and y >= -tol
        assert x + w <= MAP_W + tol and y + h <= MAP_H + tol
        assert abs(w * h - v / total * box) <= eps  # area is its share of the box
    assert abs(sum(w * h for _, _, w, h in rects) - box) <= eps
    for (ax, ay, aw, ah), (bx, by, bw, bh) in combinations(rects, 2):
        ox = min(ax + aw, bx + bw) - max(ax, bx)
        oy = min(ay + ah, by + bh) - max(ay, by)
        assert ox <= tol or oy <= tol  # touching edges, never overlapping


def test_the_map_keeps_tiles_close_to_square() -> None:
    """What squarifying is for: nine equal holdings make a 3 by 3 grid of
    near-square tiles, not nine thin strips."""
    rects = squarify([1] * 9, MAP_W, MAP_H)
    worst = max(max(w / h, h / w) for _, _, w, h in rects)
    assert worst < 2.5, worst


def _weight(isin: str, name: str, group: str, paise: int, total: int) -> Weight:
    m = Money(paise=paise)
    return Weight(
        isin=parse_isin(isin),
        name=name,
        group=group,
        value=m,
        bps=m.share_of(Money(paise=total)),
        cost=None,
    )


def test_a_tile_shows_its_own_share_and_keeps_its_kinds_colour() -> None:
    total = 1_000_000
    ws = (
        _weight("INE009A01021", "Infosys", "Stocks", 600_000, total),
        _weight("INF179K01BE2", "HDFC Fund", "Equity funds", 400_000, total),
    )
    a, b = tiles(ws)
    assert (a.share, b.share) == ("60.0%", "40.0%")
    assert (a.slot, b.slot) == (GROUP_SLOT["Stocks"], GROUP_SLOT["Equity funds"])
    assert abs(a.w * a.h / (b.w * b.h) - 1.5) < 1e-9  # 600 : 400


def _concentration(paise: list[int]) -> Insight:
    total = sum(paise)
    top_t = tenths_of(paise[0], total)
    return Insight(
        kind="concentration",
        tone="warn",
        headline=f"One holding is {pct(top_t)} of your money.",
        detail="",
        numbers={"top_tenths": top_t, "total_paise": total},
        unit="paise",
        series=tuple(
            Point(label=f"H{i}", value=v, group="Equity funds", key=f"k{i}")
            for i, v in enumerate(paise)
        ),
    )


@given(
    st.lists(st.integers(1, 10**12), min_size=1, max_size=8).map(lambda v: sorted(v, reverse=True))
)
def test_a_concentration_card_shows_the_headlines_number_and_the_25_percent_line(
    paise: list[int],
) -> None:
    """Insights are computed, not written: the largest bar's label is the
    percent in the sentence above it, and the dashed line sits at 25% on the
    bars' own scale."""
    ins = _concentration(paise)
    c = card(ins)
    assert c.bars[0].value in c.headline
    total = sum(paise)
    for b, v in zip(c.bars, paise, strict=False):
        assert b.value == pct(tenths_of(v, total))
    assert c.line is not None
    limit = total * 2500 // 10_000
    top = max(paise[0], limit)
    assert abs(c.line - limit / top * 100) < 1e-9
    assert abs(c.bars[0].width - paise[0] / top * 100) < 1e-9


def test_no_more_than_four_cards_and_what_changed_opens_the_story() -> None:
    changed = Insight(
        kind="what_changed", tone="info", headline="March: up.", detail="", numbers={}
    )
    others = tuple(
        Insight(kind="invested", tone="info", headline=f"n{i}", detail="", numbers={})
        for i in range(6)
    )
    s = story((changed, *others), (), 0, 0)
    assert s.changed == changed
    assert [c.headline for c in s.cards] == ["n0", "n1", "n2", "n3"]  # four, no more
    assert s.total is None and s.tiles == ()


# --------------------------------------------------------------- the page


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("STOCKIN_DATA", str(tmp_path))
    for name in ("mf-2026-01.json", "demat-2026-02.json"):
        r = runner.invoke(cli, ["import", "cas", str(FIX / "cas" / name)])
        assert r.exit_code == 0, r.output
    return TestClient(create_app(tmp_path / "ledger.db", PORT), base_url=HOST)


def test_without_prices_the_page_says_how_to_get_them(home: TestClient) -> None:
    page = home.get("/").text
    assert "No prices yet" in page and "stockin prices fetch" in page
    assert 'class="map"' not in page
    assert "INFOSYS LIMITED" in page  # the holdings table still shows


def test_with_prices_the_story_and_map_come_from_the_insights(home: TestClient) -> None:
    r = runner.invoke(cli, ["prices", "import", str(EOD / "bhavcopy-sample.csv")])
    assert r.exit_code in (0, 1), r.output
    insights = runner.invoke(cli, ["insights", "--json"])
    assert insights.exit_code == 0, insights.output
    import json

    found = json.loads(insights.output)["insights"]
    page = home.get("/").text
    for i in found[: MAX_CARDS + 1]:
        assert i["headline"].replace("'", "&#39;") in page
    priced = [p for i in found if i["kind"] == "concentration" for p in i["series"]]
    assert page.count('class="tile ') == len(priced) > 0
    assert "No prices yet" not in page
    assert re.search(r"value \(₹\)", page, re.I)


def test_a_damaged_prices_file_is_said_on_the_page_not_a_traceback(
    home: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "prices.db").write_bytes(b"not a database at all, just text, long enough to matter")
    r = home.get("/")
    assert r.status_code == 200
    assert "Prices could not be read" in r.text and "stockin prices fetch" in r.text
    assert "INFOSYS LIMITED" in r.text


@pytest.mark.parametrize("months", [[1, 2, 4, 5], [1, 2, 3, 4, 5]])
def test_a_sip_card_shows_each_sips_months_and_agrees_with_its_sentence(months: list[int]) -> None:
    from test_insights import _book, _sip_history, _statement

    from stockin.insights import sip_discipline

    ins = sip_discipline(_book([_statement(_sip_history(months), 1, 5)]))
    assert ins is not None
    c = card(ins)
    shown = [m for row in c.streaks for m in row.months]
    assert sum(m.paid for m in shown) == ins.numbers["paid"]
    assert sum(not m.paid for m in shown) == ins.numbers["missed"]
    assert len(c.streaks) == ins.numbers["sips"]
    assert c.bars == ()
