"""The home cockpit's view model: every label a tile prints is the text a
sentence beside it prints, and every bar is drawn from the same number.

Properties cover the parts that turn numbers into widths (mix, flows,
verdict gauges); the real-book tests check the sentences agree with the
tiles on data built the way an import builds it.
"""

from __future__ import annotations

from datetime import date

import cas_fixtures as fx
from hypothesis import example, given
from hypothesis import strategies as st
from test_insights import STOCKS, _five_holdings, _prices  # type: ignore[attr-defined]

from stockin.cockpit import MAX_DECIDE, cockpit, decide, flows, known, mix
from stockin.facts import parse_isin
from stockin.insights import (
    Insight,
    Weight,
    all_insights,
    cost_coverage,
    pct,
    rupees,
    tenths_of,
    weights,
)
from stockin.money import Bps, Money
from stockin.story import GROUP_SLOT
from stockin.thesis import Thesis, verdict

GROUPS = list(GROUP_SLOT)
ISINS = ["INE009A01021", "INE467B01029", "INE062A01020", "INE040A01034", "INE002A01018"]


def _weights(values: list[tuple[int, int]]) -> tuple[Weight, ...]:
    total = sum(v for v, _ in values)
    out = [
        Weight(
            isin=parse_isin(ISINS[i]),
            name=f"H{i}",
            group=GROUPS[g],
            value=Money(paise=v),
            bps=Money(paise=v).share_of(Money(paise=total)),
            cost=None,
        )
        for i, (v, g) in enumerate(values)
    ]
    return tuple(sorted(out, key=lambda w: -w.value.paise))


holdings = st.lists(
    st.tuples(st.integers(1, 10**12), st.integers(0, len(GROUPS) - 1)), min_size=1, max_size=5
)


@given(holdings)
def test_mix_shares_add_up_and_each_label_is_rounded_once_from_its_own_sum(
    values: list[tuple[int, int]],
) -> None:
    ws = _weights(values)
    parts = mix(ws)
    total = sum(v for v, _ in values)
    assert abs(sum(p.share for p in parts) - 100) < 1e-6
    for p in parts:
        group_sum = sum(w.value.paise for w in ws if w.group == p.group)
        assert p.text == pct(tenths_of(group_sum, total))
        assert p.slot == GROUP_SLOT[p.group]
    assert [p.slot for p in parts] == sorted(p.slot for p in parts)  # colour order, never rank


@given(st.integers(0, 10**12), st.integers(0, 10**12))
def test_flows_draw_in_and_out_on_one_scale_with_the_sentence_amounts(added: int, out: int) -> None:
    changed = Insight(
        kind="what_changed",
        tone="info",
        headline="",
        detail="",
        numbers={"added_paise": added, "out_paise": out},
    )
    rows = flows(changed)
    expect = [(lbl, v) for lbl, v in (("Added", added), ("Took out", out)) if v > 0]
    assert [(r.label, r.amount) for r in rows] == [
        (lbl, rupees(Money(paise=v))) for lbl, v in expect
    ]
    if rows:
        assert max(r.width for r in rows) == 100
        for (_, v), r in zip(expect, rows, strict=True):
            assert abs(r.width - v / max(added, out) * 100) < 1e-9


@given(
    w=st.integers(0, 10_000),
    target=st.one_of(st.none(), st.integers(0, 10_000)),
    broken=st.booleans(),
)
@example(w=1746, target=1250, broken=False)  # the sentence prints hundredths here
def test_a_gauges_labels_are_the_verdict_sentences_own_numbers(
    w: int, target: int | None, broken: bool
) -> None:
    isin = parse_isin(ISINS[0])
    weight = Weight(
        isin=isin, name="X", group="Stocks", value=Money(paise=w + 1), bps=Bps(w), cost=None
    )
    t = Thesis(
        isin=isin,
        why="because",
        target=Bps(target) if target is not None else None,
        broken=broken,
        written=date(2026, 11, 22),
    )
    v = verdict(isin, "X", weight, t)
    rows, _ = decide((v,))
    for d in rows:
        if v.weight_text:  # the sentence prints a weight: the gauge repeats it
            assert d.weight_text == v.weight_text and f"X is {d.weight_text} of" in v.because
        else:  # an exit sentence prints none: the gauge rounds the same bps once
            assert d.weight_text == pct(tenths_of(w, 10_000))
        if d.target is not None and v.target_text:
            assert f"target of {d.target_text}" in v.because
        assert d.weight == w / 100


def test_decide_lists_the_urgent_calls_first_and_counts_the_rest() -> None:
    isin = [parse_isin(i) for i in ISINS]
    vs = tuple(
        verdict(
            i,
            f"H{n}",
            Weight(
                isin=i,
                name=f"H{n}",
                group="Stocks",
                value=Money(paise=1),
                bps=Bps(3000 + n),
                cost=None,
            ),
            Thesis(isin=i, why="w", target=Bps(500), written=date(2026, 11, 22)),
        )
        for n, i in enumerate(isin)
    )
    rows, more = decide(vs)
    assert len(rows) == MAX_DECIDE and more == len(vs) - MAX_DECIDE
    assert [r.call for r in rows] == ["trim"] * MAX_DECIDE
    assert [r.weight for r in rows] == sorted((r.weight for r in rows), reverse=True)


def test_on_a_real_book_the_cost_bar_repeats_the_cost_sentence() -> None:
    book = _five_holdings()
    ps = {fx.HDFC_MF: 3_000 * 1_000_000, fx.ICICI_MF: 1_000 * 1_000_000}
    ps |= {i: 100 * 1_000_000 for i in STOCKS}
    prices = _prices(ps)
    cost = cost_coverage(book, prices)
    assert cost is not None
    k, k_text, u_text = known(cost)
    assert k is not None and u_text in cost.headline
    assert abs((k + float(u_text[:-1])) - 100) < 1e-9 and k_text == f"{k:.1f}%"
    ws, _, _ = weights(book, prices)
    view = cockpit(all_insights(book, prices, date(2026, 11, 22)), ws, ())
    assert view.known == k and [t.isin for t in view.top] == [w.isin for w in ws][: len(view.top)]
    assert all(t.call == "review" for t in view.top)  # no theses yet
