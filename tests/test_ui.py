"""The shared UI kit: the app shell on every page, and the _ui.html macros.

A macro only places numbers that Python already computed, so these tests
check placement: each mark's width is the number it was given, clamped to
its track, and each label repeats the same number.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from jinja2 import Environment, PackageLoader, select_autoescape

from stockin.web import create_app

PORT = 8765
HOST = f"http://127.0.0.1:{PORT}"

_env = Environment(loader=PackageLoader("stockin", "templates"), autoescape=select_autoescape())
ui: Any = _env.get_template("_ui.html").module


def _widths(html: str, cls: str) -> list[float]:
    return [
        float(m)
        for m in re.findall(rf'class="{cls}[^"]*" style="(?:left:[^;]*;)?width:([0-9.]+)%', html)
    ]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "data" / "ledger.db", PORT), base_url=HOST)


@pytest.mark.parametrize(
    ("path", "active"), [("/", "Home"), ("/reconcile", "Reconcile"), ("/networth", "Net worth")]
)
def test_every_page_has_the_shell_and_marks_where_you_are(
    client: TestClient, path: str, active: str
) -> None:
    html = client.get(path).text
    assert html.startswith("<!doctype html>")
    assert '<link rel="stylesheet" href="/static/tokens.css">' in html
    assert 'aria-label="Main menu"' in html and 'aria-label="Main (phone)"' in html
    current = re.findall(r'aria-current="page"><svg.*?</svg>(\w[\w ]*)</a>', html, re.S)
    assert current == [active, active]  # side menu and phone tabs agree
    # Net worth (Sprint 2) is a real page now, in both menus.
    assert html.count('href="/networth"') == 2
    assert not re.search(r'nav-item soon"[^>]*>.*?Net worth<span', html, re.S)


def test_the_menu_links_land_on_real_anchors(client: TestClient) -> None:
    fixture = Path(__file__).parent / "fixtures" / "cas" / "mf-2026-01.json"
    with fixture.open("rb") as fh:
        client.post("/import", files={"statement": ("mf.json", fh, "application/json")})
    html = client.get("/").text
    side, tabs = html.split('aria-label="Main (phone)"')
    anchors = re.findall(r'href="/#([\w-]+)"', side)
    assert set(anchors) == {"holdings", "verdicts", "import"}
    for a in anchors:
        assert f'id="{a}"' in html
    # the phone tabs carry every live link and no "soon" entry
    assert re.findall(r'href="(/[^"]*)"', tabs) == re.findall(
        r'class="nav-item" href="(/[^"]*)"', side
    )
    assert "soon" not in tabs and "Alerts" not in tabs
    assert "Net worth" in tabs  # live since the /networth page
    assert tabs.count('class="nav-item"') == side.count('class="nav-item" href=')


def test_static_files_are_served_locally_and_only_locally(client: TestClient) -> None:
    for name in ("tokens.css", "ui.css"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
        assert r.headers["cache-control"] == "no-cache"
    r = client.get("/static/ui.css", headers={"host": "evil.example:8765"})
    assert r.status_code == 403


def test_ui_css_uses_only_tokens_for_colour() -> None:
    css = (Path(__file__).parents[1] / "src/stockin/static/ui.css").read_text()
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|lab)\(", css)


@given(
    weight=st.floats(-50, 250, allow_nan=False),
    target=st.floats(-50, 250, allow_nan=False),
)
def test_gauge_marks_stay_on_the_track_and_the_label_repeats_the_weight(
    weight: float, target: float
) -> None:
    html = str(ui.gauge(weight, "X", target=target))
    fill = _widths(html, "gauge-fill")
    band = _widths(html, "gauge-band")
    assert fill == [pytest.approx(min(max(weight, 0), 100), abs=0.006)]
    lefts = [float(x) for x in re.findall(r'gauge-band" style="left:([0-9.]+)%', html)]
    assert all(0 <= x <= 100 for x in fill + band + lefts)
    assert f"<b>{weight:.1f}%</b>" in html


@given(st.lists(st.floats(0, 100, allow_nan=False), min_size=1, max_size=8))
def test_seg_draws_each_part_at_its_share_and_names_it(shares: list[float]) -> None:
    parts = [(f"P{i}", s, i % 6) for i, s in enumerate(shares)]
    html = str(ui.seg(parts, "Mix"))
    assert _widths(html, "seg-part") == [pytest.approx(s, abs=0.006) for s in shares]
    for name, s, _ in parts:
        assert f"{name} {s:.1f}%" in html


def test_known_bar_and_flow_and_pill_and_fresh() -> None:
    k = str(ui.known_bar(2.3))
    assert _widths(k, "kbar-known") == [2.3] and "unknown 97.7%" in k
    rows = [("Added", "+₹95,000", 100.0, "in"), ("Took out", "\u2212₹28,880", 30.4, "out")]
    f = str(ui.flow(rows))
    assert _widths(f, "flow-bar") == [100.0, 30.4] and "\u2212₹28,880" in f
    assert "▼ trim" in str(ui.pill("trim")) and "pill-trim" in str(ui.pill("trim"))
    for state, word in (("live", "live"), ("delayed", "delayed"), ("eod", "end of day")):
        assert f"{word} · 20 Nov" in str(ui.fresh(state, "20 Nov"))
    assert "fresh-stale" in str(ui.fresh("stale", "18 Nov"))
    assert "<details" in str(ui.why("Because.")) and "Because." in str(ui.why("Because."))


@given(
    st.lists(st.floats(-20, 150, allow_nan=False), min_size=1, max_size=6),
    st.one_of(st.none(), st.floats(-20, 150, allow_nan=False)),
)
def test_bar_list_clamps_bars_and_line_and_links_rows(
    widths: list[float], line: float | None
) -> None:
    rows = [
        (f"H{i}", f"{w:.1f}%", w, 1, f"/holding/X{i}" if i % 2 else None)
        for i, w in enumerate(widths)
    ]
    html = str(ui.bar_list(rows, "Weights", line=line, line_label="25% line"))
    assert _widths(html, "bar s") == [pytest.approx(min(max(w, 0), 100), abs=0.006) for w in widths]
    assert html.count('<a class="bar-row"') == len(widths) // 2
    lines = [float(x) for x in re.findall(r'bar-line" style="left:([0-9.]+)%', html)]
    assert lines == (
        [] if line is None else [pytest.approx(min(max(line, 0), 100), abs=0.006)] * len(widths)
    )


def test_stat_insight_and_empty() -> None:
    assert "₹1,000" in str(ui.stat("Value", "₹1,000", "price on 20 Nov"))
    assert '<span class="unknown">unknown</span>' in str(ui.stat("Cost", None))
    card = str(
        _env.from_string(
            '{% import "_ui.html" as ui %}'
            '{% call ui.insight("warn", "One fund is 81.7% of your money", "Because.") %}'
            "CHART{% endcall %}"
        ).render()
    )
    assert "! look at this" in card and "CHART" in card and "<details" in card and "81.7%" in card
    e = str(ui.empty("No net worth yet", "Add an entry.", "/networth/add", "Add one"))
    assert 'href="/networth/add"' in e and "Add one" in e
