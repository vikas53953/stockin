"""Hypothesis profiles. `ci` (the default) runs 200 examples per property;
`HYPOTHESIS_PROFILE=deep` runs 2,000 for a slower local sweep.

And one clock for every test: see `a_fixed_today`."""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("deep", parent=settings.get_profile("ci"), max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


FIXED_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


@pytest.fixture(autouse=True, scope="session")
def a_fixed_today() -> Generator[None]:
    """Today is 2026-11-21 in India unless a test moves the clock itself.

    The price fixtures are dated 2026-11-14, and a price more than 7 days
    old gives no add, trim or hold (thesis.doubts). On the real clock every
    test that reads a verdict from those fixtures would, a week after they
    were written, quietly test only "review" (#39 verify-2). Session-wide,
    so Hypothesis tests get it too; a test's own monkeypatch of
    `stockin.prices._now` still wins and is undone back to this."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("stockin.prices._now", lambda: FIXED_NOW)
        yield
